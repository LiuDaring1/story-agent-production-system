"""Offline real-entry regression tests: only the provider boundary is simulated."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import run_image_video_jobs as runner
from story_requirements import write_projection, validate_run_projection
from story_run import init_run, record_run, load_run, finalize_run
from video_motion import write_video_receipt, video_receipt_issues


class NativeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.images = self.root / 'images'; self.images.mkdir()
        self.videos = self.root / 'videos'; self.videos.mkdir()
        self.text = self.root / 'text.txt'; self.text.write_text('confirmed')
        self.rules = self.root / 'rules.md'; self.rules.write_text('user override')
        self.run = self.root / '99_项目状态/story_run.json'
        init_run(run_file=self.run, confirmed_text=self.text, subtitle_txt=self.text,
                 greenscreen_video=self.text, audio=self.text, project_dir=self.root)
        self.projection = self.root / 'requirements.json'
        self.project(self.text)
        record_run(run_file=self.run, package='director_plan', status='done',
                   artifact_id='requirements_projection', artifact_path=self.projection)
        self.jobs = self.root / 'jobs.csv'
        self.row = dict(scene='1', image_filename='01.png', story_text='甲', prompt='甲',
                        target_video_filename='01.mp4', generation_duration='6', duration='6',
                        status='submitted', task_id='existing-task', provider_prompt_sha256='a'*64)
        self.save()
        self.video = self.videos / '01.mp4'
        self.client = Mock()
        self.client.download.side_effect = lambda url, path: path.write_bytes(b'offline media')
        self.result = SimpleNamespace(status='succeeded', video_url='https://example.test/video', error='', raw={})

    def project(self, text):
        write_projection(self.projection, scope='r2v', inputs={'confirmed_text': text},
                         rule_sources=[(self.rules, 'v1')], applicability={'shots':['1'], 'artifacts':['r2v_provider_group_receipt']},
                         requirements=[dict(requirement_id='r1', source='user', scope='r2v', requirement='current text')],
                         executable_checks=[], acceptance_evidence=['provider_receipt'])

    def save(self):
        runner.write_jobs_csv(self.jobs, [self.row])

    def main(self, *flags, error=None, poll_error=None):
        argv = ['runner', '--run-file', str(self.run), '--jobs-csv', str(self.jobs),
                '--images-dir', str(self.images), '--videos-dir', str(self.videos),
                '--base-url', 'https://toapis.com/v1', '--model', 'grok-video-1.0',
                '--seconds', '6', '--submit-delay', '0', *flags]
        with patch.object(sys, 'argv', argv), patch.object(runner, 'read_secret', return_value='offline'), \
             patch.object(runner, 'ToAPIsVideoClient', return_value=self.client), \
             patch.object(runner, 'create_provider_task', return_value=SimpleNamespace(task_id='new-task', raw={})) as create, \
             patch.object(runner, 'poll_until_done', return_value=self.result, side_effect=poll_error) as poll, \
             contextlib.redirect_stdout(io.StringIO()):
            if error:
                with self.assertRaises(error): runner.main()
            else: runner.main()
        self.row = runner.read_jobs_csv(self.jobs)[0]
        return create, poll

    def test_poll_existing_and_no_new_submit(self):
        for flags in [('--poll-existing',), ()]:
            with self.subTest(flags=flags):
                if self.video.exists(): self.video.unlink()
                self.row['status'] = 'submitted'; self.save()
                create, poll = self.main(*flags)
                create.assert_not_called(); poll.assert_called_once()
                self.assertEqual(self.row['status'], 'downloaded')
                self.assertEqual(video_receipt_issues(self.row, self.video, production_mode=True), [])

    def test_submit_delay_zero(self):
        self.row.update(task_id='', status='todo'); self.save()
        create, poll = self.main()
        create.assert_called_once(); poll.assert_called_once()
        self.assertEqual(self.row['status'], 'downloaded')

    def test_download_failure_resumes_same_identity(self):
        before = runner.toapis_extra_body(self.jobs, self.row, None)
        def fail(url, path):
            path.write_bytes(b'partial')
            raise OSError('offline download failure')
        self.client.download.side_effect = fail
        create, poll = self.main('--poll-existing', error=OSError)
        create.assert_not_called(); poll.assert_called_once()
        self.assertEqual(self.row['task_id'], 'existing-task')
        self.assertFalse(runner.reset_retryable_failed_row(self.row, self.video))
        self.assertEqual(before, runner.toapis_extra_body(self.jobs, self.row, None))
        self.assertFalse(self.video.exists())
        self.client.download.side_effect = lambda url, path: path.write_bytes(b'complete')
        create, poll = self.main()
        create.assert_not_called(); poll.assert_called_once()
        self.assertEqual(self.row['status'], 'downloaded')

    def test_complete_runner_sidecar_branches(self):
        for mode in ('missing', 'broken', 'wrong_hash', 'valid'):
            with self.subTest(mode=mode):
                self.row = {k:v for k,v in self.row.items() if not k.startswith('video_') and k != 'production_eligible'}
                self.row['status'] = 'submitted'; self.save()
                self.video.write_bytes(b'existing media')
                receipt, _ = write_video_receipt(self.jobs, self.row, self.video, provider='toapis',
                    model='grok-video-1.0', source_kind='provider_generated', execution_mode='production', production_eligible=True)
                if mode == 'missing': receipt.unlink()
                if mode == 'broken': receipt.write_text('[]')
                if mode == 'wrong_hash': self.video.write_bytes(b'changed media')
                media = self.video.read_bytes()
                create, poll = self.main('--poll-existing', error=None if mode == 'valid' else ValueError)
                create.assert_not_called(); poll.assert_not_called()
                self.assertEqual(self.row['task_id'], 'existing-task')
                self.assertEqual(self.video.read_bytes(), media)
                self.assertEqual(self.row['status'], 'downloaded' if mode == 'valid' else 'blocked_receipt')

    def test_wrong_projection_registration_and_real_consumer(self):
        old = self.root / 'old.txt'; old.write_text('old still exists')
        self.project(old)
        with self.assertRaisesRegex(ValueError, '错配'):
            record_run(run_file=self.run, package='director_plan', status='done',
                       artifact_id='requirements_projection', artifact_path=self.projection, replace=True)
        # Emulate an earlier ledger that accepted the wrong projection; consumer must reject it too.
        import hashlib
        ledger = load_run(self.run)
        ledger['artifacts']['requirements_projection']['sha256'] = hashlib.sha256(self.projection.read_bytes()).hexdigest()
        self.run.write_text(json.dumps(ledger))
        create, poll = self.main('--poll-existing', error=ValueError)
        create.assert_not_called(); poll.assert_not_called()

    def test_local_error_and_confirmed_failure_have_distinct_resets(self):
        for status in ('error', 'failed', 'expired'):
            self.row.update(status=status, task_id='existing-task')
            self.assertFalse(runner.reset_retryable_failed_row(self.row, self.video))
        self.row['provider_failure_confirmed'] = 'true'
        self.assertTrue(runner.reset_retryable_failed_row(self.row, self.video))
        self.assertEqual(self.row['task_id'], '')
        self.assertEqual(self.row['provider_attempt'], '1')

    def test_poll_timeout_and_local_exception_resume_without_submission(self):
        for exc in (TimeoutError("poll timeout"), RuntimeError("local processing")):
            with self.subTest(error=type(exc).__name__):
                self.row.update(status="submitted", task_id="existing-task"); self.save()
                before = runner.toapis_extra_body(self.jobs, self.row, None)
                create, poll = self.main("--poll-existing", error=type(exc), poll_error=exc)
                create.assert_not_called(); poll.assert_called_once()
                self.assertEqual(self.row["task_id"], "existing-task")
                self.assertFalse(runner.reset_retryable_failed_row(self.row, self.video))
                self.assertEqual(before, runner.toapis_extra_body(self.jobs, self.row, None))

    def test_quality_redo_changes_key_through_real_workflow(self):
        from story_workflow import reset_redo_scenes
        from r2v_retry_policy import POLICY_VERSION
        from test_r2v_retry_policy import hard_redo
        import hashlib
        self.video.write_bytes(b"reviewed defective candidate")
        self.row.update(client_business_id="old-generation", retry_policy_version=POLICY_VERSION,
                        quality_retry_count="0", provider_attempt="0")
        rows = [dict(self.row, scene=str(n), target_video_filename=f"{n:02d}.mp4") for n in range(1, 5)]
        runner.write_jobs_csv(self.jobs, rows)
        decisions = self.root / "decisions.csv"
        runner.write_jobs_csv(decisions, [dict(hard_redo(artifact_sha256=hashlib.sha256(self.video.read_bytes()).hexdigest()), scene="1")])
        self.assertEqual(reset_redo_scenes(self.jobs, self.videos, decisions), [1])
        redo = runner.read_jobs_csv(self.jobs)[0]
        self.assertEqual(redo["task_id"], "")
        self.assertNotEqual(runner.toapis_extra_body(self.jobs, redo, None)["client_business_id"], "old-generation")
        self.assertEqual(redo["provider_attempt"], "1")
        self.assertTrue(list(self.videos.glob("_review_redo_backup_*/01.mp4")))

    def test_minimal_input_binding_and_registered_plan_drift(self):
        from story_requirements import validate_projection
        from story_run import file_sha256
        validate_run_projection(self.run, consumer_scope={"shots": ["1"]})
        unrelated = self.root / "unrelated.txt"; unrelated.write_text("unused")
        record_run(run_file=self.run, package="music", status="done", artifact_id="unrelated", artifact_path=unrelated)
        unrelated.write_text("changed but not applicable to this package")
        validate_run_projection(self.run, consumer_scope={"shots": ["1"]})
        plan = self.root / "plan.json"; plan.write_text("{}")
        record_run(run_file=self.run, package="director_plan", status="done", artifact_id="plan", artifact_path=plan)
        projection = json.loads(self.projection.read_text())
        projection["inputs"]["plan"] = {"path":str(plan), "sha256":file_sha256(plan)}
        self.projection.write_text(json.dumps(projection))
        record_run(run_file=self.run, package="director_plan", status="done", artifact_id="requirements_projection", artifact_path=self.projection, replace=True)
        new = self.root / "new-plan.json"; new.write_text('{"changed": true}')
        record_run(run_file=self.run, package="director_plan", status="done", artifact_id="plan", artifact_path=new, replace=True)
        create, poll = self.main("--poll-existing", error=ValueError)
        create.assert_not_called(); poll.assert_not_called()

    def test_recovered_media_reaches_qa_assembly_and_finalize_gates(self):
        """Real CLI recovery → QA producer → ledger → assembly/finalize consumers.

        This proves safe refusal without independent delivery evidence, not a
        complete creative/paid-services end-to-end production run.
        """
        import subprocess
        from story_run import file_sha256, PACKAGE_NAMES
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'testsrc2=size=320x180:rate=30:duration=1.2', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', str(self.video)], check=True)
        write_video_receipt(self.jobs, self.row, self.video, provider='toapis',
                            model='grok-video-1.0', source_kind='provider_generated',
                            execution_mode='production', production_eligible=True)
        create, poll = self.main('--poll-existing')
        create.assert_not_called(); poll.assert_not_called()
        plan = self.root / 'plan.json'
        plan.write_text(json.dumps({'shots':[{'shot_id':'01', 'source_start':0, 'source_end':1.2, 'provider_seconds':1}]}))
        group = self.root / 'provider-group.json'
        group.write_text(json.dumps({'shots':[dict(self.row, filename=self.row['target_video_filename'], output_sha256=file_sha256(self.video))]}))
        report = self.root / 'qa.json'
        qa = subprocess.run([sys.executable, 'r2v_group_qa.py', '--plan', str(plan), '--receipt', str(group),
                             '--videos-dir', str(self.videos), '--output', str(report)], capture_output=True, text=True)
        self.assertEqual(qa.returncode, 0, qa.stdout + qa.stderr)
        record_run(run_file=self.run, package='r2v_visuals', status='done', artifact_id='r2v_provider_group_receipt', artifact_path=group)
        record_run(run_file=self.run, package='r2v_visuals', status='done', artifact_id='r2v_group_machine_qa', artifact_path=report)
        command = [sys.executable, 'assemble_r2v_story.py', '--run-file', str(self.run), '--plan', str(plan),
                   '--videos-dir', str(self.videos), '--title-video', str(self.video), '--audio', str(self.text),
                   '--output', str(self.root/'master.mp4'), '--decisions', str(self.root/'decisions.json'),
                   '--clips-dir', str(self.root/'clips'), '--ppt-plan', str(self.root/'ppt.json')]
        assembly = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(assembly.returncode, 0)
        self.assertIn('缺少', assembly.stderr)
        self.assertFalse((self.root/'master.mp4').exists())
        for package in PACKAGE_NAMES: record_run(run_file=self.run, package=package, status='done')
        with self.assertRaises(RuntimeError): finalize_run(run_file=self.run, required_artifacts=[])
        self.assertFalse(load_run(self.run).get('finalized_at'))
        self.video.write_bytes(self.video.read_bytes() + b'drift')
        with self.assertRaisesRegex(ValueError, '哈希漂移'):
            record_run(run_file=self.run, package='r2v_visuals', status='done', artifact_id='r2v_group_machine_qa', artifact_path=report)

    def test_current_producers_reject_missing_or_stale_native_ledger(self):
        from story_workflow import require_native_run_inputs
        from release_video import preflight_release_requirements
        require_native_run_inputs(self.run.parent)
        self.text.write_text('changed')
        with self.assertRaisesRegex(ValueError, '证据过期'):
            require_native_run_inputs(self.run.parent)
        with self.assertRaisesRegex(ValueError, 'story_run.json'):
            require_native_run_inputs(self.root/'missing')
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaisesRegex(ValueError, 'story_run.json'):
                preflight_release_requirements(output_dir=Path(empty), variant='main', explicit_run_file=None, output_scale=1)

    def test_nonterminal_poll_deadline_is_still_the_same_running_request(self):
        self.result.status = 'running'
        self.result.video_url = ''
        create, poll = self.main('--poll-existing')
        create.assert_not_called(); poll.assert_called_once()
        request = load_run(self.run)['observability']['requests']['toapis:existing-task']
        self.assertEqual(request['status'], 'running')
        self.assertEqual(self.row['task_id'], 'existing-task')
        self.assertFalse(runner.reset_retryable_failed_row(self.row, self.video))
