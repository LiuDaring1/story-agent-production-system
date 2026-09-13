import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
import selectors
from unittest.mock import patch

from story_production_v2 import INPUTS, PACKAGES, binding, write
from story_work_observation import append_fact, operation_observation, recover_operation
from story_operation_recovery import pending_directory, replay_pending_facts
from story_run import load_run


class PendingEndFactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'STORY_OPERATION_STATE_DIR': str(self.root/'local-recovery'),
                                          'STORY_ENCODE_STATE_DIR': str(self.root/'encoder-state')})
        self.env.start(); self.addCleanup(self.env.stop)
        source = self.root/'input'; source.write_bytes(b'input')
        self.runfile = self.root/'project/99_项目状态/story_run.json'
        write(self.runfile, {'schema_version': 'story-run-v1', 'production_contract': 'story-production/v2',
            'run_id': 'offline-end-fact', 'project_dir': str(self.root/'project'),
            'inputs': {k: binding(source) for k in INPUTS}, 'artifacts': {},
            'work_packages': {k: {'status': 'pending'} for k in PACKAGES}})

    def lose_end_write(self, run_file, fact):
        if fact['status'] == 'running':
            return append_fact(run_file, fact)
        raise OSError('simulated unavailable project volume')

    def latest(self):
        return {x['operation_id']: x for x in load_run(self.runfile)['observability']['events'] if x.get('event') == 'operation_fact'}

    def start_owned_process(self):
        code = """
import sys, time
from story_work_observation import operation_observation
with operation_observation(sys.argv[1], 'media', 'encode') as fact:
    print(fact['operation_id'], flush=True)
    while True:
        time.sleep(1)
"""
        process = subprocess.Popen([sys.executable, '-c', code, str(self.runfile)],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
        self.addCleanup(cleanup)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(10), 'owned test process did not start')
            operation_id = process.stdout.readline().strip()
        self.assertTrue(operation_id, 'owned test process exited before ready')
        return process, operation_id

    def test_real_killed_parent_is_recovered_but_live_parent_without_child_is_untouched(self):
        process, operation_id = self.start_owned_process()
        self.assertEqual(replay_pending_facts(self.runfile), [])
        self.assertEqual(self.latest()[operation_id]['status'], 'running')
        process.kill()  # Only the isolated subprocess created by this test.
        process.wait(timeout=10)
        self.assertEqual(replay_pending_facts(self.runfile), [operation_id])
        final = self.latest()[operation_id]
        self.assertEqual(final['status'], 'failed')
        self.assertEqual(final['error_type'], 'operation_owner_interrupted')
        self.assertIsNone(final['duration_seconds'])
        self.assertIsNone(final['ended_at'])
        self.assertGreaterEqual(final['recovery_wallspan_seconds'], 0)
        self.assertEqual(replay_pending_facts(self.runfile), [])

    def test_killed_parent_waits_for_nested_live_encoder_kernel_lock(self):
        process, operation_id = self.start_owned_process()
        append_fact(self.runfile, {'operation_id': 'nested-encoder', 'parent_operation_id': operation_id,
            'status': 'running', 'started_at': self.latest()[operation_id]['started_at'], 'operation': 'ffmpeg'})
        state = self.root/'encoder-state/nested.json'
        write(state, {'ledger': str(self.runfile.resolve()), 'operation_id': 'nested-encoder', 'status': 'running'})
        with state.with_suffix('.lock').open('a+') as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            process.kill()
            process.wait(timeout=10)
            self.assertEqual(replay_pending_facts(self.runfile), [])
            self.assertEqual(self.latest()[operation_id]['status'], 'running')
        self.assertEqual(replay_pending_facts(self.runfile), [operation_id])
        self.assertEqual(self.latest()[operation_id]['error_type'], 'operation_owner_interrupted')

    def test_failed_parent_is_spooled_and_replayed_once_without_adding_recovery_wait(self):
        with patch('story_work_observation.append_fact', side_effect=self.lose_end_write):
            with self.assertRaisesRegex(RuntimeError, 'original failure'):
                with operation_observation(self.runfile, 'pack', 'deterministic') as fact:
                    raise RuntimeError('original failure')
        pending = next(pending_directory(self.runfile).glob('*.json'))
        saved = json.loads(pending.read_text())['fact']
        self.assertEqual(self.latest()[fact['operation_id']]['status'], 'running')
        self.assertEqual(replay_pending_facts(self.runfile), [fact['operation_id']])
        restored = self.latest()[fact['operation_id']]
        self.assertEqual(restored['status'], 'failed')
        self.assertEqual(restored['duration_seconds'], saved['duration_seconds'])
        self.assertEqual(restored['ended_at'], saved['ended_at'])
        count = len(load_run(self.runfile)['observability']['events'])
        self.assertEqual(replay_pending_facts(self.runfile), [])
        self.assertEqual(len(load_run(self.runfile)['observability']['events']), count)

    def test_same_ledger_path_new_run_id_cannot_receive_old_fact(self):
        with patch('story_work_observation.append_fact', side_effect=self.lose_end_write):
            with self.assertRaises(OSError):
                with operation_observation(self.runfile, 'prepare', 'deterministic'):
                    pass
        run = load_run(self.runfile); run['run_id'] = 'different-project'; write(self.runfile, run)
        self.assertEqual(replay_pending_facts(self.runfile), [])
        self.assertTrue(all(x['status'] == 'running' for x in self.latest().values()))

    def test_live_child_kernel_lock_keeps_parent_pending_and_changed_output_is_not_complete(self):
        output = self.root/'project/output.bin'; output.write_bytes(b'original')
        with patch('story_work_observation.append_fact', side_effect=self.lose_end_write):
            with self.assertRaises(OSError):
                with operation_observation(self.runfile, 'media', 'encode', artifacts=[output]) as parent:
                    append_fact(self.runfile, {'operation_id': 'live-child', 'parent_operation_id': parent['operation_id'],
                        'status': 'running', 'started_at': parent['started_at'], 'operation': 'ffmpeg'})
        state = self.root/'encoder-state/child.json'
        write(state, {'ledger': str(self.runfile.resolve()), 'operation_id': 'live-child', 'status': 'running'})
        with state.with_suffix('.lock').open('a+') as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(replay_pending_facts(self.runfile), [])
        output.write_bytes(b'changed')
        self.assertEqual(replay_pending_facts(self.runfile), [parent['operation_id']])
        final = self.latest()[parent['operation_id']]
        self.assertEqual(final['status'], 'failed')
        self.assertEqual(final['error_type'], 'recovered_output_hash_changed')
        self.assertEqual(final['artifacts'], [])

    def test_reconciled_historical_span_does_not_become_service_duration(self):
        append_fact(self.runfile, {'operation_id': 'orphan', 'status': 'running', 'duration_seconds': None,
            'started_at': '2026-01-01T00:00:00+00:00', 'operation': 'ffmpeg'})
        recover_operation(self.runfile, 'orphan', error_type='interrupted', recovery_evidence={})
        final = self.latest()['orphan']
        self.assertIsNone(final['duration_seconds'])
        self.assertGreater(final['recovery_wallspan_seconds'], 0)


if __name__ == '__main__':
    unittest.main()
