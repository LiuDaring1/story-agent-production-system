from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from story_run import init_run, load_run
from run_image_video_jobs import create_provider_task
from story_request_facts import submission_summary

class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.file=self.root/'input.png';self.file.write_bytes(b'input')
        text=self.root/'input.txt';text.write_text('fixture')
        self.run=self.root/'story_run.json'
        init_run(run_file=self.run,project_dir=self.root,confirmed_text=text,subtitle_txt=text,greenscreen_video=self.file,audio=self.file)
        self.client=Mock();self.row={'scene':'1','provider_attempt':'0'}
    def invoke(self):
        return create_provider_task(client=self.client,is_toapis=False,model='fixture-model',prompt='fixture',
            image_path=self.file,reference_paths=[],ratio='16:9',duration=3,resolution=None,frames=None,
            seconds='3',size='720p',parameter_style='native',camera_fixed=False,watermark=False,extra_body={},
            observation_row=self.row,provider='fixture-provider',run_file=self.run,
            submission_scope={'scene':'1','attempt':'0'})
    def test_failed_submission_without_provider_id_persists_and_does_not_resubmit(self):
        def fail(**kwargs):
            attempt=next(iter(load_run(self.run)['observability']['submission_attempts'].values()))
            self.assertEqual(attempt['status'],'submitting')
            self.assertIsNone(attempt['request_id'])
            raise TimeoutError('synthetic transport timeout')
        self.client.create_task.side_effect=fail
        with self.assertRaises(TimeoutError):self.invoke()
        attempt=next(iter(load_run(self.run)['observability']['submission_attempts'].values()))
        self.assertEqual(attempt['status'],'failed');self.assertEqual(attempt['error_type'],'TimeoutError')
        self.assertIsNone(attempt['request_id']);self.assertIsNone(attempt['total_tokens'])
        self.assertEqual(attempt['provider_acceptance'],'unknown');self.assertTrue(attempt['ended_at'])
        self.assertEqual(len(attempt['fingerprint']['request_sha256']),64)
        with self.assertRaisesRegex(ValueError,'Unresolved provider submission'):self.invoke()
        self.assertEqual(self.client.create_task.call_count,1)
        self.assertEqual(submission_summary(load_run(self.run))['unknown_acceptance_count'],1)
    def test_accepted_id_restores_after_jobs_csv_was_not_written(self):
        self.client.create_task.return_value=SimpleNamespace(task_id='actual-provider-id',raw={'ok':True})
        first=self.invoke();self.row={'scene':'1','provider_attempt':'0'}
        recovered=self.invoke()
        self.assertEqual(first.task_id,recovered.task_id)
        self.assertEqual(self.client.create_task.call_count,1)
        self.assertIn('recovered_from_submission_attempt',recovered.raw)
    def test_failed_final_ledger_write_does_not_mask_failure_or_resubmit(self):
        self.client.create_task.side_effect=ConnectionError('synthetic')
        with patch('story_request_facts._store_submission_end',side_effect=OSError('synthetic unavailable disk')):
            with self.assertRaises(ConnectionError):self.invoke()
        self.assertEqual(next(iter(load_run(self.run)['observability']['submission_attempts'].values()))['status'],'submitting')
        with self.assertRaisesRegex(ValueError,'Unresolved provider submission'):self.invoke()
        self.assertEqual(self.client.create_task.call_count,1)
    def test_structured_non_acceptance_keeps_prior_attempt_and_allows_later_retry(self):
        from story_video_synthesizer.toapis_video import ToAPIsRequestError
        rejection=ToAPIsRequestError(429, '{"error":{"code":"local_quota_not_enough"}}')
        self.client.create_task.side_effect=[rejection, SimpleNamespace(task_id='retried-id',raw={})]
        with self.assertRaises(ToAPIsRequestError):self.invoke()
        self.assertEqual(self.invoke().task_id,'retried-id')
        rows=list(load_run(self.run)['observability']['submission_attempts'].values())
        rows.sort(key=lambda x:x['sequence'])
        self.assertEqual([x['status'] for x in rows],['rejected','submitted'])
        self.assertEqual(self.invoke().task_id,'retried-id')
        self.assertEqual(self.client.create_task.call_count,2)
        self.assertTrue(rows[0]['rejection_evidence']['response_sha256'])
        self.assertIsNone(ToAPIsRequestError(500, '{"error":{"code":"local_quota_not_enough"}}').rejection_evidence)
        self.assertIsNone(ToAPIsRequestError(429, 'local_quota_not_enough').rejection_evidence)

    def test_reference_byte_change_changes_full_fingerprint(self):
        self.client.create_task.return_value=SimpleNamespace(task_id='id',raw={})
        self.invoke();self.file.write_bytes(b'changed input');self.invoke()
        rows=list(load_run(self.run)['observability']['submission_attempts'].values())
        self.assertEqual(len(rows),2)
        self.assertNotEqual(rows[0]['fingerprint']['request_sha256'],rows[1]['fingerprint']['request_sha256'])

if __name__=='__main__':unittest.main()
