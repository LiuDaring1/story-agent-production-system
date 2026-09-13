from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import story_run
from story_encode import reconcile_encode_operations
from story_hash_cache import hash_cache_scope, sha256_file
from story_materials import MaterialsPreflightError, material_rows, materials_preflight
from story_production_v2 import directory_binding, review_provenance, write
from story_work_observation import append_fact


class CompactCliTests(unittest.TestCase):
    def payload(self):
        return {
            'schema_version': 'story-run-v1', 'run_id': 'fixture',
            'project_dir': '/tmp/fixture', 'updated_at': 'now', 'finalized_at': 'now',
            'work_packages': {'delivery': {'status': 'done', 'blocker': ''}},
            'artifacts': {'huge': {'path': '/tmp/x', 'sha256': 'a' * 64, 'padding': 'x' * 100_000}},
            'observability': {'requests': {}, 'events': [{'padding': 'y' * 100_000}]},
        }

    def invoke(self, argv, target, payload):
        output = io.StringIO()
        with patch('sys.argv', argv), patch(target, return_value=payload), contextlib.redirect_stdout(output):
            story_run.main()
        return output.getvalue()

    def test_record_and_finalize_default_compact_full_json_explicit(self):
        payload = self.payload()
        record = ['story_run.py', 'record', '--run-file', '/tmp/run.json', '--package', 'delivery', '--status', 'done']
        compact = self.invoke(record, 'story_run.record_run', payload)
        verbose = self.invoke([*record, '--verbose'], 'story_run.record_run', payload)
        self.assertLess(len(compact), 4_000)
        self.assertGreater(len(verbose), 100_000)
        self.assertEqual(json.loads(compact)['schema_version'], 'story-run-command-summary/v1')
        finalize = ['story_run.py', 'finalize', '--run-file', '/tmp/run.json']
        compact_finalize = self.invoke(finalize, 'story_run.finalize_run', payload)
        self.assertLess(len(compact_finalize), 4_000)


class HashCacheTests(unittest.TestCase):
    def test_same_transaction_reuses_but_mutation_and_directory_change_invalidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'member.bin'
            path.write_bytes(b'abcd')
            with hash_cache_scope() as cache:
                first = sha256_file(path)
                self.assertEqual(first, sha256_file(path))
                self.assertEqual(len(cache), 1)
                original_times = path.stat()
                path.write_bytes(b'wxyz')
                os.utime(path, ns=(original_times.st_atime_ns, original_times.st_mtime_ns))
                self.assertNotEqual(first, sha256_file(path))
                tree_before = directory_binding(root)['sha256']
                (root / 'new.bin').write_bytes(b'new')
                self.assertNotEqual(tree_before, directory_binding(root)['sha256'])


class ReviewCompatibilityTests(unittest.TestCase):
    def test_only_known_storyboard_v1_can_use_nested_legacy_provenance(self):
        legacy = {
            'schema_version': 'story-shot-storyboards-review/v1',
            'review_context': {'review_role': 'independent-reviewer', 'independent_context': True},
        }
        self.assertEqual(review_provenance(legacy, allow_legacy_storyboard=True)['source'], 'legacy_review_context')
        with self.assertRaises(ValueError):
            review_provenance({**legacy, 'schema_version': 'unrelated-review/v1'}, allow_legacy_storyboard=True)
        canonical = {'reviewer_context': 'independent-reviewer', 'independent_context': True}
        self.assertEqual(review_provenance(canonical)['source'], 'top_level')

    def test_compile_receipt_auto_registers_canonical_storyboard_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compile_receipt = root / 'compile.json'; compile_receipt.write_text('{}')
            review = root / 'review.json'; review.write_text('{}')
            payload = {
                'schema_version': 'story-run-v1', 'run_id': 'fixture',
                'project_dir': str(root), 'production_contract': 'story-production/v2',
                'requirements_policy': 'story-applicable-requirements/v1',
                'created_at': 'now', 'updated_at': 'now', 'finalized_at': '', 'blocker': '',
                'inputs': {}, 'artifacts': {
                    'storyboard_manifest_sealed': {'path': str(root / 'sealed.json'), 'sha256': 'a' * 64}
                },
                'work_packages': {'r2v_visuals': {'status': 'running', 'blocker': ''}},
            }
            captured = {}
            with patch('story_run.load_run', return_value=payload), patch('story_run.validate_compile_receipt', return_value={'storyboard_review_path': str(review)}), patch('story_run.validate_artifact_semantics'), patch('story_run.atomic_write_json', side_effect=lambda _path, value: captured.update(value)):
                result = story_run._record_run_unlocked(
                    run_file=root / 'run.json', package='r2v_visuals', status='running',
                    artifact_id='shot_storyboard_compile_receipt', artifact_path=compile_receipt,
                )
            self.assertEqual(result['artifacts']['storyboard_review']['path'], str(review.resolve()))
            self.assertEqual(result['artifacts']['storyboard_review']['registered_by'], 'shot_storyboard_compile_receipt')


class DuplicateClassificationTests(unittest.TestCase):
    def test_retry_explicit_parallel_and_unknown_are_separate(self):
        requests = {
            'p:1': {'provider': 'p', 'request_id': '1', 'request_sha256': 'a' * 64, 'operation': 'op', 'status': 'failed', 'observed_at': '1'},
            'p:2': {'provider': 'p', 'request_id': '2', 'request_sha256': 'a' * 64, 'operation': 'op', 'status': 'completed', 'retry_index': 1, 'observed_at': '2'},
            'p:3': {'provider': 'p', 'request_id': '3', 'request_sha256': 'b' * 64, 'operation': 'left', 'status': 'completed', 'observed_at': '1'},
            'p:4': {'provider': 'p', 'request_id': '4', 'request_sha256': 'b' * 64, 'operation': 'right', 'status': 'completed', 'request_relation': 'parallel_required', 'observed_at': '2'},
            'p:5': {'provider': 'p', 'request_id': '5', 'request_sha256': 'c' * 64, 'operation': 'a', 'status': 'completed', 'observed_at': '1'},
            'p:6': {'provider': 'p', 'request_id': '6', 'request_sha256': 'c' * 64, 'operation': 'b', 'status': 'completed', 'observed_at': '2'},
        }
        payload = {'work_packages': {}, 'artifacts': {}, 'observability': {'requests': requests, 'performance': story_run._new_performance_observation(), 'packages': {}}}
        story_run.refresh_observability_summary(payload)
        performance = payload['observability']['performance']
        self.assertEqual(performance['duplicate_provider_request_count'], 3)
        self.assertEqual(performance['duplicate_provider_request_classification'], {'parallel_required': 1, 'retry': 1, 'unknown': 1})


class MaterialsPreflightTests(unittest.TestCase):
    def test_all_missing_word_mappings_reported_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            word = root / 'word.docx'
            with zipfile.ZipFile(word, 'w') as archive:
                archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>only</w:t></w:r></w:p></w:body></w:document>')
            director = root / 'director.json'
            director.write_text(json.dumps({'shots': []}))
            slides = [
                {'shot_id': 'TITLE', 'word_text': 'missing-one', 'duration_seconds': 1, 'poster_path': str(director)},
                {'shot_id': 'MORAL', 'word_text': 'missing-two', 'duration_seconds': 1, 'poster_path': str(director)},
            ]
            with patch('static_ppt_contract.validate_plan', return_value=(['TITLE', 'MORAL'], slides)):
                with self.assertRaises(MaterialsPreflightError) as raised:
                    material_rows(director, root / 'plan.json', word)
            message = str(raised.exception)
            self.assertIn('TITLE: text does not map verbatim', message)
            self.assertIn('MORAL: text does not map verbatim', message)

    def test_compile_and_all_mapping_defects_are_reported_in_one_preflight(self):
        inputs = {'final_word': {'path': 'word.docx'}, 'audio': {}, 'finished_music': {}}
        mapping_error = MaterialsPreflightError(['TITLE: missing', 'shot-001: missing'])
        with patch('shot_storyboard_pipeline.validate_compile_receipt', side_effect=ValueError('stale plan')), patch('story_materials.current'), patch('story_materials.material_rows', side_effect=mapping_error):
            with self.assertRaises(MaterialsPreflightError) as raised:
                materials_preflight(director='director.json', plan='plan.json', compile_receipt='compile.json', inputs=inputs)
        self.assertEqual(len(raised.exception.issues), 3)
        self.assertIn('stale plan', raised.exception.issues[0])


class EncodeRecoveryTests(unittest.TestCase):
    def test_free_owner_lock_closes_orphan_running_fact(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'STORY_ENCODE_STATE_DIR': directory}):
            root = Path(directory)
            project = root / 'project'; project.mkdir()
            text = root / 'text.txt'; text.write_text('text')
            video = root / 'video.mp4'; video.write_bytes(b'video')
            audio = root / 'audio.mp3'; audio.write_bytes(b'audio')
            ledger = project / 'story_run.json'
            story_run.init_run(run_file=ledger, confirmed_text=text, subtitle_txt=text, greenscreen_video=video, audio=audio, project_dir=project)
            operation_id = 'orphan-operation'
            append_fact(ledger, {
                'operation_id': operation_id, 'operation': 'ffmpeg', 'kind': 'encode',
                'execution_mode': 'first_execution', 'started_at': '2026-09-13T00:00:00+00:00',
                'ended_at': None, 'duration_seconds': None, 'wait_seconds': None,
                'status': 'running', 'error_type': None, 'artifacts': [],
            })
            receipt = root / 'encode.json'
            write(receipt, {'schema_version': 'story-encode/v1', 'ledger': str(ledger.resolve()), 'operation_id': operation_id, 'status': 'running', 'parent_pid': 999999})
            self.assertEqual(reconcile_encode_operations(ledger), [operation_id])
            summary = __import__('story_work_observation').summarize(story_run.load_run(ledger))
            latest = next(item for item in summary['operations'] if item['operation_id'] == operation_id)
            self.assertEqual(latest['status'], 'failed')
            self.assertEqual(latest['error_type'], 'managed_process_interrupted')


if __name__ == '__main__':
    unittest.main()
