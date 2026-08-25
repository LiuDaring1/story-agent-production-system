from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from story_agent import AgentContext, StoryAgent
from story_agent_dashboard import _path_allowed, build_dashboard_server, render_dashboard_html
from story_agent_observability import (
    acknowledge_notification,
    append_agent_event,
    emit_notification,
    event_log_path,
    load_agent_events,
    load_notifications,
    supervisor_state_path,
    timestamp,
)
from story_agent_preflight import build_start_preflight_report
from story_project import init_project, project_paths, save_json, write_manifest


class StoryAgentObservabilityTests(unittest.TestCase):
    def test_event_log_is_append_only_and_redacts_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：事件"
            first = append_agent_event(
                project,
                event_type="stage_result",
                stage="generate_videos",
                status="failed",
                summary="api_key=very-secret-value",
            )
            second = append_agent_event(
                project,
                event_type="recovery_decided",
                status="retry_after",
                summary="provider 502",
                metadata={
                    "api_key": "short-secret",
                    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/hidden-hook-id",
                },
            )
            records = load_agent_events(project)
            self.assertEqual(len(records), 2)
            self.assertNotEqual(first["event_id"], second["event_id"])
            self.assertNotIn("very-secret-value", event_log_path(project).read_text(encoding="utf-8"))
            self.assertNotIn("short-secret", event_log_path(project).read_text(encoding="utf-8"))
            self.assertNotIn("hidden-hook-id", event_log_path(project).read_text(encoding="utf-8"))
            self.assertIn("[REDACTED]", records[0]["summary"])

    def test_notification_dedupe_acknowledgement_and_reissue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：通知"
            first = emit_notification(
                project,
                category="stage_blocked",
                severity="warning",
                message="需要恢复",
                dedupe_key="job:stage:block",
                recovery_mode="diagnosing",
            )
            duplicate = emit_notification(
                project,
                category="stage_blocked",
                severity="warning",
                message="需要恢复",
                dedupe_key="job:stage:block",
                recovery_mode="diagnosing",
            )
            self.assertTrue(duplicate["deduplicated"])
            self.assertEqual(len(load_notifications(project)), 1)
            acknowledge_notification(project, first["notification_id"], acknowledged_by="tester")
            reissued = emit_notification(
                project,
                category="stage_blocked",
                severity="warning",
                message="再次阻塞",
                dedupe_key="job:stage:block",
                recovery_mode="diagnosing",
            )
            reduced = load_notifications(project)
            self.assertEqual(len(reduced), 2)
            self.assertEqual(reduced[0]["acknowledged_by"], "tester")
            self.assertNotEqual(reissued["notification_id"], first["notification_id"])

    def test_dashboard_binds_snapshot_handler_and_only_allows_scoped_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：看板"
            project.mkdir()
            artifact = project / "evidence.txt"
            artifact.write_text("evidence", encoding="utf-8")
            snapshot = {
                "project_dir": str(project),
                "agent_status": "blocked",
                "current_stage": "suno_generate",
                "supervisor": {"running": False},
                "budget": {},
                "estimated_remaining_minutes": {},
                "artifact_progress": {},
                "stage_rows": [],
                "notifications": [],
                "events": [],
                "evidence": {"fixture": str(artifact)},
            }
            sentinel = object()
            with patch("story_agent_dashboard.ThreadingHTTPServer", return_value=sentinel) as server:
                built = build_dashboard_server(
                    lambda: snapshot,
                    project_dir=project,
                    host="127.0.0.1",
                    port=8765,
                )
            self.assertIs(built, sentinel)
            self.assertEqual(server.call_args.args[0], ("127.0.0.1", 8765))
            self.assertTrue(callable(server.call_args.args[1]))
            self.assertTrue(_path_allowed(artifact, (project.resolve(),)))
            self.assertFalse(_path_allowed(Path("/etc/hosts"), (project.resolve(),)))
            html = render_dashboard_html()
            self.assertIn("/api/status", html)
            self.assertIn("DAG 与 attempt", html)
            self.assertIn("退避剩余", html)
            self.assertIn("<th>依赖</th>", html)
            self.assertIn("接受当前版本", html)
            self.assertIn("/api/accept-current", html)
            self.assertIn("为什么正在运行", html)
            self.assertIn("原因 / 重跑范围 / 产物", html)
            self.assertIn("正式编码", html)
            self.assertIn("ImageGen", html)
            self.assertIn('id="version"', html)

    def test_status_dashboard_and_real_pid_share_one_effective_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：PID"
            manifest = init_project(project, story_name="PID", slug="pid-status")
            manifest["agent"]["job_id"] = "pid-job"
            from story_project import write_manifest

            write_manifest(project_paths(project), manifest)
            save_json(
                supervisor_state_path(project),
                {
                    "kind": "story_agent_persistent_supervisor_state_v1",
                    "pid": os.getpid(),
                    "job_id": "pid-job",
                    "status": "waiting_for_user",
                    "heartbeat_at": timestamp(),
                    "heartbeat_timeout_seconds": 120,
                    "child": {},
                },
            )
            agent = StoryAgent(
                AgentContext(
                    project_dir=project,
                    inbox=None,
                    story_name="PID",
                    slug="pid-status",
                    execute=False,
                    update_latest_episode=False,
                    codex_mode="handoff",
                    codex_model="",
                    codex_sandbox="workspace-write",
                    codex_approval="never",
                    codex_path="codex",
                    codex_timeout=30,
                ),
                read_only=True,
            )
            payload = agent.status_payload()
            self.assertEqual(payload["story_agent_version"], "3.6.2-canary.4")
            self.assertTrue(payload["supervisor"]["running"])
            self.assertEqual(payload["supervisor"]["pid"], os.getpid())
            self.assertEqual(payload["supervisor"]["effective_status"], "waiting_for_user")
            self.assertEqual(
                payload["current_stage"],
                next(row["stage"] for row in payload["stage_rows"] if row["effective_status"] == "pending"),
            )

    def test_preflight_is_read_only_and_never_authorizes_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：预检"
            status = project / "99_项目状态"
            status.mkdir(parents=True)
            manifest = status / "project_manifest.json"
            manifest.write_text("{}\n", encoding="utf-8")
            code = Path(directory) / "module.py"
            code.write_text("VALUE = 1\n", encoding="utf-8")
            snapshot = {
                "job_id": "preflight-job",
                "project_dir": str(project),
                "agent_status": "blocked",
                "ready_stages": ["codex_story_images", "suno_generate"],
                "current_stage": "codex_story_images",
                "supervisor": {"running": False, "pid": 0, "effective_status": "stopped"},
                "locks": {
                    "job_lock": {"owner_alive": False, "path": "lock", "pid": 0},
                    "control": {"cancel_requested": False, "run_epoch": 4},
                },
                "budget": {"spent": 0, "reserved": 0, "hard_limit": 100},
                "timing": {
                    "runtime_deadline_enabled": True,
                    "deadline_policy": "deliver_best_valid",
                    "deadline_hours": 10,
                    "remaining_deadline_hours": 10,
                },
                "evidence": {"manifest": str(manifest)},
                "artifact_progress": {},
                "story_images": {},
                "stage_rows": [],
            }
            report = build_start_preflight_report(
                snapshot,
                code_paths=(code,),
                module_profile="production-default",
                module_execution_mode="production",
            )
            self.assertTrue(report["passed"], report)
            self.assertFalse(report["provider_calls_made"])
            self.assertFalse(report["start_authorized"])
            self.assertTrue(report["requires_user_confirmation"])

            snapshot["timing"] = {
                "runtime_deadline_enabled": False,
                "deadline_policy": "disabled",
                "deadline_hours": 0,
                "remaining_deadline_hours": None,
            }
            invalid = build_start_preflight_report(
                snapshot,
                code_paths=(code,),
                module_profile="production-default",
                module_execution_mode="production",
            )
            self.assertFalse(invalid["passed"])
            deadline_check = next(
                item for item in invalid["checks"] if item["name"] == "fixed_runtime_deadline_configured"
            )
            self.assertFalse(deadline_check["passed"])

    def test_status_counts_unbound_disk_images_as_stale_not_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：旧图口径"
            manifest = init_project(project, story_name="旧图口径", slug="old-count")
            paths = project_paths(project)
            storyboard_text = paths.inputs / "storyboard.txt"
            storyboard_text.write_text("第一镜。\n", encoding="utf-8")
            manifest["inputs"]["storyboard_text"] = str(storyboard_text)
            image = paths.images / "images" / "old-count_scene_01.png"
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(b"old-unbound-image")
            write_manifest(paths, manifest)
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="旧图口径",
                slug="old-count",
                execute=False,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            fake_root = root / "read-only-worktree"
            with patch("story_agent.ROOT", fake_root):
                payload = StoryAgent(context, read_only=True).status_payload()

            self.assertEqual(payload["story_images"]["physical_expected_named_count"], 1)
            self.assertEqual(payload["story_images"]["current_lineage_valid_count"], 0)
            self.assertEqual(payload["story_images"]["stale_or_unbound_count"], 1)
            self.assertEqual(
                payload["artifact_progress"]["故事图片（当前有效血缘）"]["actual"],
                0,
            )
            self.assertFalse(fake_root.exists(), "read-only status must not create staging directories")


if __name__ == "__main__":
    unittest.main()
