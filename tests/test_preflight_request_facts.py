import hashlib
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch, Mock
import zipfile

from story_materials import production_preflight_report
from story_review_schema import create_review_request, source_review_provenance
from story_request_facts import request_fingerprint, classify_requests
from story_work_observation import operation_observation, summarize_timing
from run_image_video_jobs import create_provider_task


class RequestFactsTests(unittest.TestCase):
    def test_same_prompt_different_image_model_or_parameters_changes_full_fingerprint(self):
        with tempfile.TemporaryDirectory() as d:
            a = Path(d) / 'a.png'; a.write_bytes(b'first')
            b = Path(d) / 'b.png'; b.write_bytes(b'other')
            def fp(**kwargs):
                return request_fingerprint(**dict(provider='test', model='m', prompt='same', input_paths=[a], parameters={'seconds': 5}, **kwargs))
            baseline = fp()
            for key, value in [('input_paths', [b]), ('model', 'm2'), ('parameters', {'seconds': 6})]:
                params = dict(provider='test', model='m', prompt='same', input_paths=[a], parameters={'seconds': 5})
                params[key] = value
                changed = request_fingerprint(**params)
                self.assertEqual(baseline['prompt_sha256'], changed['prompt_sha256'])
                self.assertNotEqual(baseline['request_sha256'], changed['request_sha256'])
            copied = Path(d) / 'copied.png'; copied.write_bytes(a.read_bytes())
            self.assertEqual(baseline, request_fingerprint(provider='test', model='m', prompt='same', input_paths=[copied], parameters={'seconds': 5}))

    def test_formal_adapter_captures_fingerprint_before_network_call(self):
        with tempfile.TemporaryDirectory() as d:
            image = Path(d) / 'image'; image.write_bytes(b'image')
            row = {}
            client = Mock()
            def submitted(**kwargs):
                self.assertEqual(len(row['provider_request_sha256']), 64)
                self.assertEqual(json.loads(row['provider_request_fingerprint_json'])['inputs'][0]['sha256'], hashlib.sha256(b'image').hexdigest())
                return 'mock request'
            client.create_task.side_effect = submitted
            result = create_provider_task(client=client, is_toapis=False, model='m', prompt='p', image_path=image,
                reference_paths=[], ratio='16:9', duration=5, resolution=None, frames=None, seconds='5', size='test',
                parameter_style='plain', camera_fixed=True, watermark=False, extra_body={}, observation_row=row, provider='mock')
            self.assertEqual(result, 'mock request')

    def test_prompt_only_unknown_is_never_true_duplicate(self):
        base = dict(provider='p', request_sha256='a' * 64, request_hash_kind='prompt_only', execution_mode='first_execution')
        result = classify_requests([base, dict(base, request_relation='true_duplicate')])
        self.assertEqual(result['classification'].get('true_duplicate', 0), 0)
        self.assertEqual(result['unknown_fingerprint_scope_count'], 2)
        full = dict(base, request_hash_kind='full_request')
        rows = [full, dict(full, retry_index=1), dict(full, execution_mode='rework', rework_classification='valid'), dict(full, request_relation='true_duplicate')]
        self.assertEqual(classify_requests(rows)['classification'], {'first_generation': 1, 'failed_retry': 1, 'valid_rework': 1, 'true_duplicate': 1})


class ReviewSchemaTests(unittest.TestCase):
    def test_request_is_pending_canonical_and_never_fabricates_approval(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'a'; source.write_bytes(b'evidence')
            request = create_review_request(artifact_id='storyboard_manifest_sealed', artifact=source, producer_context='producer', requirements=[source])
            self.assertEqual(request['artifact_id'], 'storyboard_review')
            self.assertIsNone(request['approved'])
            self.assertIsNone(request['independent_context'])
            with self.assertRaises(ValueError):
                create_review_request(artifact_id='made_up_review', artifact=source, producer_context='producer', requirements=[source])

    def test_legacy_adapter_keeps_original_byte_hash_and_rejects_fake_independence(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'review.json'
            payload = {'schema_version': 'story-shot-storyboards-review/v1', 'review_context': {'review_role': 'reviewer', 'independent_context': True}}
            source.write_text(json.dumps(payload, indent=3)); original = source.read_bytes()
            result = source_review_provenance(source, allow_legacy_storyboard=True)
            self.assertEqual(result['source_review']['sha256'], hashlib.sha256(original).hexdigest())
            self.assertFalse(result['new_review_performed'])
            self.assertEqual(source.read_bytes(), original)
            payload['review_context']['independent_context'] = False
            source.write_text(json.dumps(payload))
            with self.assertRaises(ValueError):
                source_review_provenance(source, allow_legacy_storyboard=True)


class TimingTests(unittest.TestCase):
    def test_nested_operations_record_parent_and_timing_does_not_add_it(self):
        facts = []
        with patch('story_work_observation.append_fact', side_effect=lambda _p, f: facts.append(dict(f))), patch('story_operation_recovery.operation_owner', side_effect=lambda *_: nullcontext('mock')), patch('story_operation_recovery.spool_end_fact'):
            with operation_observation('unused', 'media', 'encode') as parent:
                with operation_observation('unused', 'ffmpeg', 'encode') as child:
                    self.assertEqual(child['parent_operation_id'], parent['operation_id'])
        completed = [x for x in facts if x['status'] == 'complete']
        timing = summarize_timing(completed)
        self.assertEqual(timing['leaf_operation_seconds'], completed[0]['duration_seconds'])
        self.assertEqual(timing['parent_inclusive_seconds'], completed[1]['duration_seconds'])
        self.assertGreaterEqual(timing['observed_wall_clock_span_seconds'], 0)
        self.assertEqual(summarize_timing([{'duration_seconds': 10}])['unclassified_historical_operation_count'], 1)


class EarlyPreflightTests(unittest.TestCase):
    def test_formal_cli_persists_all_errors_and_pending_review_with_canonical_id(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / 'source'; source.write_bytes(b'explicit synthetic input')
            from story_production_v2 import INPUTS, PACKAGES, binding, write
            runfile = root / '99_项目状态/story_run.json'
            write(runfile, {'schema_version': 'story-run-v1', 'production_contract': 'story-production/v2',
                'run_id': 'offline-preflight', 'project_dir': str(root),
                'inputs': {k: binding(source) for k in INPUTS}, 'artifacts': {},
                'work_packages': {k: {'status': 'pending'} for k in PACKAGES}})
            request = root / 'request.json'; output = root / 'report.json'
            write(request, {'director': str(root/'missing-director'), 'plan': str(root/'missing-plan'),
                'compile_receipt': str(root/'missing-compile'), 'output': str(output)})
            entry = Path(__file__).resolve().parents[1]/'story_pipeline.py'
            result = subprocess.run([sys.executable, str(entry), 'preflight', '--run-file', str(runfile), '--request', str(request)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            report = json.loads(output.read_text())
            self.assertFalse(report['passed'])
            self.assertGreaterEqual(len(report['issues']), 2)
            write(request, {'artifact_id': 'master_director_plan', 'artifact': str(source),
                'producer_context': 'producer', 'requirements': [str(source)], 'output': str(output)})
            result = subprocess.run([sys.executable, str(entry), 'review-create', '--run-file', str(runfile), '--request', str(request)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['artifact_id'], 'director_plan_review')
            self.assertIsNone(report['approved'])

    def test_stale_plan_missing_images_and_both_card_mappings_reported_together(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); word = root / 'word.docx'
            with zipfile.ZipFile(word, 'w') as z:
                z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>confirmed text</w:t></w:r></w:p></w:body></w:document>')
            director = root / 'director.json'; director.write_text(json.dumps({'shots': []}))
            plan = root / 'plan.json'; plan.write_text(json.dumps({'slides': [{'shot_id': x, 'duration_seconds': 1, 'poster_path': str(root / 'missing.png')} for x in ['TITLE', 'MORAL']]}))
            from story_production_v2 import binding
            inputs = {k: binding(word) for k in ['final_word', 'audio', 'finished_music']}
            result = production_preflight_report(director=director, plan=plan, compile_receipt=root/'missing.json', inputs=inputs)
            self.assertFalse(result['passed'])
            self.assertIn('compile_receipt', result['issues'][0])
            self.assertTrue(any('plan:' in x for x in result['issues']))
            self.assertTrue(any('TITLE: missing exact Word text mapping' in x for x in result['issues']))
            self.assertTrue(any('MORAL: missing exact Word text mapping' in x for x in result['issues']))
            self.assertTrue(any('TITLE: image binding' in x for x in result['issues']))
            self.assertTrue(any('MORAL: image binding' in x for x in result['issues']))
            self.assertFalse(result['independent_review_performed'])


if __name__ == '__main__':
    unittest.main()
