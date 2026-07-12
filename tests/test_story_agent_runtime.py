from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path

from story_agent import AgentContext, StageResult, StoryAgent, classify_command_failure
from story_agent_runtime import (
    AgentRuntimeError,
    BudgetExceeded,
    BudgetLedger,
    JobRegistry,
    existing_artifact_hashes,
    ensure_manifest_v2,
    file_sha256,
    render_job_report,
    request_cancel,
    resume_job,
    review_passes,
    manifest_context_sha256,
    mark_stage,
    job_lock,
    submit_video_job,
    assert_runnable,
)
from story_project import load_manifest, project_paths
from story_project import final_delivery, init_project


class StoryAgentRuntimeTests(unittest.TestCase):
    def test_transient_stage_failure_retries_and_then_checkpoints(self) -> None:
        class TransientAgent(StoryAgent):
            calls = 0

            def _next_stage(self, manifest):
                if manifest["agent"]["stages"]["generate_videos"]["status"] == "passed":
                    return "done", lambda _manifest: StageResult("done", "done")

                def action(_manifest):
                    self.calls += 1
                    return StageResult("failed", "HTTP 502 temporary") if self.calls == 1 else StageResult("done", "recovered")

                return "generate_videos", action

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：重试"
            init_project(project, story_name="重试", slug="retry")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="重试",
                slug="retry",
                execute=True,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = TransientAgent(context)
            self.assertEqual(agent.run(max_steps=4), 0)
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            record = manifest["agent"]["stages"]["generate_videos"]
            self.assertEqual(record["status"], "passed")
            self.assertEqual(record["attempts"], 2)
            self.assertEqual(agent.calls, 2)

    def test_codex_subtask_failure_retries_in_a_new_attempt(self) -> None:
        class CodexFailOnceAgent(StoryAgent):
            calls = 0

            def _next_stage(self, manifest):
                stage = "codex_story_images"
                if manifest["agent"]["stages"][stage]["status"] == "passed":
                    return "done", lambda _manifest: StageResult("done", "done")

                def action(_manifest):
                    self.calls += 1
                    return StageResult("failed", "Codex CLI 子任务失败") if self.calls == 1 else StageResult("done", "Codex recovered")

                return stage, action

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：Codex重试"
            init_project(project, story_name="Codex重试", slug="codex-retry")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="Codex重试",
                slug="codex-retry",
                execute=True,
                update_latest_episode=False,
                codex_mode="cli",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = CodexFailOnceAgent(context)
            self.assertEqual(agent.run(max_steps=4), 0)
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            record = manifest["agent"]["stages"]["codex_story_images"]
            self.assertEqual(record["status"], "passed")
            self.assertEqual(record["attempts"], 2)

    def test_dead_process_lock_is_reclaimed_but_live_lock_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：锁恢复"
            init_project(project, story_name="锁恢复", slug="lock")
            lock = project_paths(project).status / "story_agent.lock"
            lock.write_text(json.dumps({"pid": 99999999}), encoding="utf-8")
            with job_lock(project):
                self.assertTrue(lock.exists())
            self.assertFalse(lock.exists())

            lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
            with self.assertRaises(AgentRuntimeError):
                with job_lock(project):
                    pass
            lock.unlink()

    def test_missing_target_video_is_not_hidden_by_unrelated_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：缺帧"
            manifest = init_project(project, story_name="缺帧", slug="missing-frame")
            paths = project_paths(project)
            jobs = paths.video_jobs / "missing-frame_image_video_jobs.csv"
            jobs.write_text(
                "scene,target_video_filename\n1,01.mp4\n2,02.mp4\n",
                encoding="utf-8-sig",
            )
            manifest["outputs"]["jobs_csv"] = str(jobs)
            from story_project import write_manifest

            write_manifest(paths, manifest)
            videos = paths.video_jobs / "videos"
            videos.mkdir()
            (videos / "01.mp4").write_bytes(b"one")
            (videos / "unrelated.mp4").write_bytes(b"extra")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="缺帧",
                slug="missing-frame",
                execute=False,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context, read_only=True)
            self.assertFalse(agent._has_generated_videos(manifest))
            (videos / "02.mp4").write_bytes(b"two")
            self.assertTrue(agent._has_generated_videos(manifest))

    def test_suno_login_or_browser_loss_writes_recoverable_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：Suno阻塞"
            manifest = init_project(project, story_name="Suno阻塞", slug="suno-block")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="Suno阻塞",
                slug="suno-block",
                execute=True,
                update_latest_episode=False,
                codex_mode="cli",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context)
            with patch.object(agent, "_codex_task", return_value=StageResult("blocked", "No browser is available")):
                result = agent._stage_suno_generate(manifest)
            self.assertEqual(result.status, "blocked")
            blocker = project / "02_图生视频" / "music" / "suno_cli_blocker.md"
            self.assertTrue(blocker.exists())
            self.assertIn("Codex 主任务", blocker.read_text(encoding="utf-8"))

    def test_stage_record_tracks_context_output_provider_cost_and_retry_reason(self) -> None:
        manifest = ensure_manifest_v2({"story": {"name": "证据测试"}})
        input_sha = manifest_context_sha256(manifest)
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "result.json"
            artifact.write_text('{"ok": true}', encoding="utf-8")
            mark_stage(
                manifest,
                "generate_videos",
                "running",
                input_hashes={"manifest_context": input_sha},
                provider="stub",
            )
            mark_stage(
                manifest,
                "generate_videos",
                "retrying",
                output_hashes=existing_artifact_hashes([artifact]),
                provider="stub",
                actual_cost=3.25,
                retry_reason="temporary timeout",
            )
            mark_stage(manifest, "generate_videos", "running")
            record = manifest["agent"]["stages"]["generate_videos"]
            self.assertEqual(record["input_hashes"]["manifest_context"], input_sha)
            self.assertEqual(record["output_hashes"][str(artifact)], file_sha256(artifact))
            self.assertEqual(record["provider"], "stub")
            self.assertEqual(record["actual_cost"], 3.25)
            self.assertEqual(record["retry_reason"], "temporary timeout")
            self.assertEqual(record["attempts"], 2)

    def test_failure_classifier_blocks_login_but_retries_network_failures(self) -> None:
        self.assertEqual(classify_command_failure("Suno CAPTCHA / login required"), "blocked")
        self.assertEqual(classify_command_failure("No browser is available: browser discovery returned an empty list"), "blocked")
        self.assertEqual(classify_command_failure("HTTP 502 connection reset"), "failed")

    def test_long_task_heartbeat_observes_cancel_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：心跳"
            init_project(project, story_name="心跳", slug="heartbeat")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="心跳",
                slug="heartbeat",
                execute=True,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context)
            self.assertFalse(agent._heartbeat_and_cancelled())
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            self.assertTrue(manifest["agent"]["heartbeat_at"])
            request_cancel(project)
            self.assertTrue(agent._heartbeat_and_cancelled())

    def test_status_reports_remaining_work_eta_and_recovery_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：状态"
            init_project(project, story_name="状态", slug="status")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="状态",
                slug="status",
                execute=False,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context, read_only=True)
            output = io.StringIO()
            with redirect_stdout(output):
                agent.status()
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["remaining_work"])
            remaining_names = [item["stage"] for item in payload["remaining_work"]]
            self.assertIn("source_text_correction", remaining_names)
            self.assertIn("source_edit_review", remaining_names)
            self.assertGreater(payload["estimated_remaining_minutes"]["nominal"], 0)
            self.assertIn("supervisor", payload)
            self.assertFalse(payload["completion_valid"])
            self.assertFalse((project / "02_图生视频" / "music").exists())

    def test_manifest_v2_and_budget_limits(self) -> None:
        manifest = ensure_manifest_v2({}, soft_budget_cny=50, hard_budget_cny=100)
        self.assertEqual(manifest["version"], 2)
        self.assertEqual(manifest["agent"]["stages"]["source_edit"]["status"], "pending")
        self.assertEqual(manifest["agent"]["stages"]["doctor"]["attempts"], 0)
        ledger = BudgetLedger(manifest)
        reservation = ledger.authorize(40, label="first")
        ledger.settle(reservation, 35, provider="stub", request_id="r1")
        self.assertEqual(ledger.data["spent"], 35)
        with self.assertRaises(BudgetExceeded):
            ledger.authorize(20, label="non-critical retry")
        critical = ledger.authorize(20, label="required output", critical=True)
        ledger.release(critical, reason="test")
        with self.assertRaises(BudgetExceeded):
            ledger.authorize(70, label="over hard", critical=True)

    def test_review_requires_score_no_critical_and_matching_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.json"
            artifact.write_text("{}", encoding="utf-8")
            payload = {
                "approved": True,
                "score": 90,
                "critical_errors": [],
                "artifact_sha256": file_sha256(artifact),
            }
            self.assertTrue(review_passes(payload, artifact=artifact))
            payload["critical_errors"] = ["semantic deletion"]
            self.assertFalse(review_passes(payload, artifact=artifact))
            payload["critical_errors"] = []
            artifact.write_text('{"changed": true}', encoding="utf-8")
            self.assertFalse(review_passes(payload, artifact=artifact))

    def test_submit_is_idempotent_and_cancel_is_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "测试故事_绿幕.mp4"
            fixture = Path(__file__).resolve().parents[1] / "tools" / "video-subtitle-remover" / "test" / "test2.mp4"
            shutil.copy2(fixture, video)
            registry = JobRegistry(root / "registry.json")
            job_id, project, created = submit_video_job(video, projects_root=root / "projects", registry=registry)
            self.assertTrue(created)
            same_id, same_project, created_again = submit_video_job(video, projects_root=root / "projects", registry=registry)
            self.assertFalse(created_again)
            self.assertEqual((same_id, same_project), (job_id, project))
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            self.assertEqual(manifest["version"], 2)
            self.assertEqual(manifest["agent"]["budget"]["hard_limit"], 100.0)
            self.assertTrue(Path(manifest["inputs"]["greenscreen_video"]).exists())
            self.assertEqual(manifest["agent"]["source"]["media"]["video"]["width"], 1280)
            self.assertEqual(manifest["agent"]["source"]["media"]["video"]["height"], 720)
            self.assertGreater(manifest["agent"]["source"]["media"]["duration_sec"], 0)

            cancelled = request_cancel(project)
            self.assertTrue(cancelled["agent"]["cancel_requested"])
            resumed = resume_job(project)
            self.assertFalse(resumed["agent"]["cancel_requested"])
            report = render_job_report(project)
            self.assertTrue(report.exists())
            report_text = report.read_text(encoding="utf-8")
            self.assertIn(job_id, report_text)
            self.assertIn("## 剩余工作", report_text)
            self.assertIn("恢复动作", report_text)

    def test_final_delivery_does_not_mark_incomplete_project_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：不完整"
            init_project(project, story_name="不完整", slug="incomplete")
            report = final_delivery(project)
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            self.assertTrue(report.exists())
            self.assertNotIn("completed_at", manifest)
            self.assertIn("部分交付", report.read_text(encoding="utf-8"))

    def test_low_disk_space_blocks_before_media_generation(self) -> None:
        manifest = ensure_manifest_v2({})
        manifest["agent"]["min_free_disk_gb"] = 10.0
        fake_usage = shutil._ntuple_diskusage(total=20 * 1024**3, used=19 * 1024**3, free=1 * 1024**3)
        with tempfile.TemporaryDirectory() as directory, patch("story_agent_runtime.shutil.disk_usage", return_value=fake_usage):
            with self.assertRaisesRegex(Exception, "磁盘可用空间不足"):
                assert_runnable(manifest, Path(directory))


if __name__ == "__main__":
    unittest.main()
