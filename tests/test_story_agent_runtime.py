from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from story_agent import AgentContext, StoryAgent, classify_command_failure
from story_agent_runtime import (
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
    submit_video_job,
    assert_runnable,
)
from story_project import load_manifest, project_paths
from story_project import final_delivery, init_project


class StoryAgentRuntimeTests(unittest.TestCase):
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

    def test_manifest_v2_and_budget_limits(self) -> None:
        manifest = ensure_manifest_v2({}, soft_budget_cny=50, hard_budget_cny=100)
        self.assertEqual(manifest["version"], 2)
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
            self.assertIn(job_id, report.read_text(encoding="utf-8"))

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
