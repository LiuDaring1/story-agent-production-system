import copy
import tempfile
import unittest
from pathlib import Path
from run_image_video_jobs import reset_retryable_failed_row


class SafetyRecoveryTests(unittest.TestCase):
    def test_safety_failure_preserves_all_request_evidence(self):
        for detail in ['Safety review blocked', 'moderation_blocked', 'content_policy_violation', '安全审核拒绝']:
            with self.subTest(detail=detail), tempfile.TemporaryDirectory() as directory:
                row = {'scene': 'S003', 'status': 'failed', 'provider_failure_confirmed': 'true',
                       'task_id': 'original-task', 'client_business_id': 'original-business',
                       'query_response': detail, 'provider_attempt': '1'}
                before = copy.deepcopy(row)
                with self.assertRaisesRegex(ValueError, '禁止自动原样重提'):
                    reset_retryable_failed_row(row, Path(directory) / 'missing.mp4')
                self.assertEqual(row, before)

    def test_confirmed_infrastructure_failure_remains_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            row = {'status': 'failed', 'provider_failure_confirmed': 'true',
                   'task_id': 'old', 'error': 'internal server failure'}
            self.assertTrue(reset_retryable_failed_row(row, Path(directory) / 'missing.mp4'))
            self.assertEqual(row['status'], 'todo')
