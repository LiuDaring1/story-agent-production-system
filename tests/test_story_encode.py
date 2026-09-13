import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from story_encode import run_encode

class EncodeTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env = patch.dict(os.environ, {'STORY_ENCODE_STATE_DIR': str(self.root / 'pool'), 'STORY_ENCODE_CONCURRENCY': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def command(self, out, seconds='1'):
        return ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=10', '-t', seconds, '-c:v', 'libx264', '-preset', 'ultrafast', str(out)]

    def test_encode_decode_and_reentry_without_new_process(self):
        out = self.root / 'out.mp4'
        args = self.command(out)
        run_encode(args)
        first = out.read_bytes()
        with patch('subprocess.Popen', side_effect=AssertionError('duplicate process')):
            run_encode(args)
        self.assertEqual(first, out.read_bytes())

    def test_reuse_binds_relevant_render_code_version(self):
        out = self.root / 'versioned.mp4'
        args = self.command(out)
        run_encode(args, code_version='renderer-v1')
        receipt = self.root / 'pool' / (hashlib.sha256(str(out).encode()).hexdigest() + '.json')
        first_started = json.loads(receipt.read_text())['started_at']
        with patch('subprocess.Popen', side_effect=AssertionError('duplicate process')):
            run_encode(args, code_version='renderer-v1')
        run_encode(args, code_version='renderer-v2')
        state = json.loads(receipt.read_text())
        self.assertEqual(state['render_code_version'], 'renderer-v2')
        self.assertGreater(state['started_at'], first_started)

    def test_input_and_unmanaged_output_protection(self):
        source = self.root / 'input.mp4'
        source.write_bytes(b'original')
        with self.assertRaisesRegex(ValueError, 'never replace'):
            run_encode(['ffmpeg', '-i', str(source), '-c:v', 'libx264', str(source)])
        with self.assertRaisesRegex(ValueError, 'Existing output'):
            run_encode(self.command(source))
        self.assertEqual(source.read_bytes(), b'original')

    def test_cancel_and_resume_do_not_reuse_partial_output(self):
        out = self.root / 'out.mp4'
        args = self.command(out, '15')
        args.insert(4, '-re')
        script = 'from story_encode import run_encode;import json,sys;run_encode(json.loads(sys.argv[1]))'
        process = subprocess.Popen([sys.executable, '-c', script, json.dumps(args)], stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        receipt = self.root / 'pool' / (hashlib.sha256(str(out).encode()).hexdigest() + '.json')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if receipt.exists() and json.loads(receipt.read_text()).get('pid'):
                break
            time.sleep(0.05)
        self.assertTrue(receipt.exists())
        self.assertIn('pid', json.loads(receipt.read_text()))
        with self.assertRaisesRegex(RuntimeError, 'already active'):
            run_encode(args)
        cancel = receipt.with_suffix('.cancel')
        cancel.write_text('cancel this output')
        process.communicate(timeout=10)
        self.assertFalse(out.exists())
        self.assertEqual(json.loads(receipt.read_text())['status'], 'cancelled')
        with self.assertRaisesRegex(RuntimeError, 'Cancellation remains'):
            run_encode(self.command(out))
        cancel.unlink()
        run_encode(self.command(out))
        self.assertTrue(out.is_file())

    def test_parent_exit_preserves_child_output_lock(self):
        out = self.root / 'out.mp4'
        args = self.command(out, '2')
        args.insert(4, '-re')
        process = subprocess.Popen([sys.executable, '-c', 'from story_encode import run_encode;import json,sys;run_encode(json.loads(sys.argv[1]))', json.dumps(args)], stderr=subprocess.DEVNULL)
        receipt = self.root / 'pool' / (hashlib.sha256(str(out).encode()).hexdigest() + '.json')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if receipt.exists() and json.loads(receipt.read_text()).get('pid'):
                break
            time.sleep(0.05)
        self.assertTrue(receipt.exists())
        self.assertIn('pid', json.loads(receipt.read_text()))
        process.kill()
        process.wait()
        with self.assertRaisesRegex(RuntimeError, 'already active'):
            run_encode(args)
        time.sleep(2.5)
        run_encode(self.command(out))
        self.assertTrue(out.exists())
if __name__ == '__main__':
    unittest.main()

class ControlOwnershipTests(EncodeTests):

    def test_wrong_task_cannot_cancel_or_acknowledge(self):
        from story_encode import control_encode
        out = self.root / 'out.mp4'
        run_encode(self.command(out))
        receipt = self.root / 'pool' / (hashlib.sha256(str(out).encode()).hexdigest() + '.json')
        state = json.loads(receipt.read_text())
        with self.assertRaisesRegex(ValueError, 'identity/task'):
            control_encode(out, action='cancel', expected_fingerprint=state['fingerprint'], expected_task='unrelated')
        self.assertFalse(receipt.with_suffix('.cancel').exists())

class QueueTests(unittest.TestCase):
    setUp = EncodeTests.setUp
    command = EncodeTests.command

    def test_waiting_is_observable_cancellable_and_resumable(self):
        from story_encode import encode_slot, control_encode
        out = self.root / 'queued.mp4'
        args = self.command(out)
        receipt = self.root / 'pool' / (hashlib.sha256(str(out).encode()).hexdigest() + '.json')
        with encode_slot(limit=1):
            process = subprocess.Popen([sys.executable, '-c', 'from story_encode import run_encode;import json,sys;run_encode(json.loads(sys.argv[1]))', json.dumps(args)], stderr=subprocess.PIPE)
            self.addCleanup(lambda: process.poll() is None and process.kill())
            deadline = time.monotonic() + 5
            while not receipt.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            state = json.loads(receipt.read_text())
            self.assertEqual(state['status'], 'waiting')
            control_encode(out, action='cancel', expected_fingerprint=state['fingerprint'], expected_task=state['task'])
            process.communicate(timeout=5)
            self.assertEqual(json.loads(receipt.read_text())['status'], 'cancelled')
        control_encode(out, action='resume', expected_fingerprint=state['fingerprint'], expected_task=state['task'])
        run_encode(args)
        self.assertEqual(json.loads(receipt.read_text())['status'], 'completed')

    def test_explicit_queue_deadline_is_deferred_not_failed(self):
        from story_encode import encode_slot
        out = self.root / 'later.mp4'
        with encode_slot(limit=1):
            with self.assertRaises(TimeoutError):
                run_encode(self.command(out), wait_seconds=.02)
        receipt = self.root / 'pool' / (hashlib.sha256(str(out).encode()).hexdigest() + '.json')
        self.assertEqual(json.loads(receipt.read_text())['status'], 'waiting')
        run_encode(self.command(out))
        self.assertEqual(json.loads(receipt.read_text())['status'], 'completed')
