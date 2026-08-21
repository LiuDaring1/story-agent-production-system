from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from story_agent_observability import (
    append_ndjson,
    load_notifications,
    recovery_log_path,
    supervisor_state_path,
)
from story_agent_runtime import mark_stage
from story_agent_supervisor import PersistentSupervisor, SupervisorConfig
from story_project import init_project, load_manifest, project_paths, save_json, write_manifest


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += max(0.1, seconds)


class ImmediateChild:
    def __init__(self, returncode: int = 2, pid: int = 99_999_991) -> None:
        self.pid = pid
        self.returncode = returncode

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class HangingChild(ImmediateChild):
    def __init__(self) -> None:
        super().__init__(returncode=0)
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = -15
        return self.returncode


class NaturallyCompletingChild(HangingChild):
    def __init__(self, *, complete_after_polls: int = 4) -> None:
        super().__init__()
        self.complete_after_polls = complete_after_polls
        self.poll_count = 0

    def poll(self):
        self.poll_count += 1
        if self.poll_count >= self.complete_after_polls:
            self.returncode = 0
        return self.returncode

def _snapshot(project: Path, stage: str, reason: str, *, heartbeat_age: float = 0) -> dict:
    return {
        "job_id": "supervisor-job",
        "project_dir": str(project),
        "agent_status": "blocked",
        "completion_valid": False,
        "current_stage": stage,
        "next_stage": stage,
        "blocked_reason": reason,
        "stage_rows": [
            {
                "stage": stage,
                "effective_status": "blocked",
                "attempt_id": "attempt-1",
                "message": reason,
                "input_hashes": {},
                "output_hashes": {},
            }
        ],
        "scheduler": {"running": {}},
        "supervisor": {},
        "locks": {},
        "budget": {"spent": 0, "hard_limit": 100},
        "timing": {"runtime_deadline_enabled": False, "deadline_hours": 0, "remaining_deadline_hours": None},
        "artifact_progress": {},
        "provider_receipts": [],
        "events": [],
        "recent_logs": [],
        "agent_heartbeat_age_seconds": heartbeat_age,
    }


def _config(project: Path, *, max_cycles: int = 2) -> SupervisorConfig:
    return SupervisorConfig(
        project_dir=project,
        job_id="supervisor-job",
        run_command=("python3", "story_agent.py", "run"),
        log_path=project_paths(project).status / "story_agent_supervisor.log",
        heartbeat_interval_seconds=1,
        heartbeat_timeout_seconds=10,
        initial_backoff_seconds=5,
        max_backoff_seconds=10,
        notification_sink_names="project",
        max_cycles=max_cycles,
    )


class StoryAgentSupervisorTests(unittest.TestCase):
    def test_stale_prelaunch_heartbeat_gets_launch_grace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：启动宽限"
            manifest = init_project(project, story_name="启动宽限", slug="heartbeat-grace")
            manifest["agent"]["job_id"] = "supervisor-job"
            write_manifest(project_paths(project), manifest)
            clock = FakeClock()
            snapshot = _snapshot(
                project,
                "generate_videos",
                "old heartbeat before launch",
                heartbeat_age=999,
            )
            child = NaturallyCompletingChild()
            supervisor = PersistentSupervisor(
                _config(project),
                snapshot_provider=lambda: snapshot,
                sleep=clock.sleep,
                clock=clock,
            )
            supervisor.child = child
            supervisor._child_started_clock = clock()
            supervisor.state["child"] = {"pid": child.pid, "running": True}
            exit_code, watchdog = supervisor._monitor_child(child)
            self.assertEqual(exit_code, 0)
            self.assertFalse(watchdog)

    def test_provider_502_requeues_with_backoff_and_notification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：502"
            manifest = init_project(project, story_name="502", slug="provider-502")
            manifest["agent"]["job_id"] = "supervisor-job"
            mark_stage(manifest, "generate_videos", "blocked", message="provider HTTP 502")
            write_manifest(project_paths(project), manifest)
            clock = FakeClock()
            snapshot = _snapshot(project, "generate_videos", "provider HTTP 502")
            supervisor = PersistentSupervisor(
                _config(project),
                snapshot_provider=lambda: snapshot,
                popen_factory=lambda *args, **kwargs: ImmediateChild(2),
                sleep=clock.sleep,
                clock=clock,
            )
            self.assertEqual(supervisor.run(), 0)
            reloaded = load_manifest(project_paths(project))
            assert reloaded is not None
            record = reloaded["agent"]["stages"]["generate_videos"]
            self.assertEqual(record["status"], "pending")
            self.assertEqual(record["infrastructure_attempts"], 1)
            categories = [item["category"] for item in load_notifications(project)]
            self.assertIn("automatic_recovery", categories)

    def test_heartbeat_timeout_stops_worker_and_repairs_interrupted_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：心跳"
            manifest = init_project(project, story_name="心跳", slug="heartbeat")
            manifest["agent"]["job_id"] = "supervisor-job"
            mark_stage(manifest, "generate_videos", "running", message="worker running")
            write_manifest(project_paths(project), manifest)
            clock = FakeClock()
            snapshot = _snapshot(
                project,
                "generate_videos",
                "worker heartbeat timeout",
                heartbeat_age=999,
            )
            child = HangingChild()
            supervisor = PersistentSupervisor(
                _config(project),
                snapshot_provider=lambda: snapshot,
                popen_factory=lambda *args, **kwargs: child,
                sleep=clock.sleep,
                clock=clock,
            )
            self.assertEqual(supervisor.run(), 0)
            self.assertIsNotNone(child.returncode)
            reloaded = load_manifest(project_paths(project))
            assert reloaded is not None
            self.assertEqual(reloaded["agent"]["stages"]["generate_videos"]["status"], "pending")
            categories = [item["category"] for item in load_notifications(project)]
            self.assertIn("heartbeat_timeout", categories)
            self.assertIn("automatic_recovery", categories)

    def test_login_blocker_waits_for_user_and_never_requeues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：等待用户"
            manifest = init_project(project, story_name="等待用户", slug="wait-user")
            manifest["agent"]["job_id"] = "supervisor-job"
            mark_stage(manifest, "suno_generate", "blocked", message="Suno 登录失效，需要验证码")
            write_manifest(project_paths(project), manifest)
            clock = FakeClock()
            snapshot = _snapshot(project, "suno_generate", "Suno 登录失效，需要验证码")
            spawn_count = 0

            def one_child_only(*args, **kwargs):
                nonlocal spawn_count
                del args, kwargs
                spawn_count += 1
                return ImmediateChild(2)

            supervisor = PersistentSupervisor(
                _config(project, max_cycles=3),
                snapshot_provider=lambda: snapshot,
                popen_factory=one_child_only,
                sleep=clock.sleep,
                clock=clock,
            )
            self.assertEqual(supervisor.run(), 0)
            self.assertEqual(spawn_count, 1)
            reloaded = load_manifest(project_paths(project))
            assert reloaded is not None
            self.assertEqual(reloaded["agent"]["status"], "wait_for_user")
            self.assertEqual(reloaded["agent"]["stages"]["suno_generate"]["status"], "blocked")
            waiting = [
                item for item in load_notifications(project) if item["category"] == "waiting_for_user"
            ]
            self.assertEqual(len(waiting), 1)
            self.assertTrue(waiting[0]["required_action"])

    def test_restarted_supervisor_restores_waiting_gate_without_spawning_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：恢复等待"
            manifest = init_project(project, story_name="恢复等待", slug="restore-wait")
            manifest["agent"]["job_id"] = "supervisor-job"
            manifest["agent"]["status"] = "wait_for_user"
            write_manifest(project_paths(project), manifest)
            save_json(
                supervisor_state_path(project),
                {
                    "kind": "story_agent_persistent_supervisor_state_v1",
                    "supervisor_id": "previous",
                    "pid": 99_999_999,
                    "job_id": "supervisor-job",
                    "project_dir": str(project),
                    "started_at": "2026-08-21 10:00:00",
                    "status": "waiting_for_user",
                    "waiting_control_epoch": 0,
                    "restart_count": 3,
                    "backoff_seconds": 10,
                    "recovery": {"action": "wait_for_user"},
                    "child": {},
                },
            )
            clock = FakeClock()
            snapshot = _snapshot(project, "suno_generate", "等待登录")
            snapshot["agent_status"] = "wait_for_user"

            def forbidden_spawn(*args, **kwargs):
                raise AssertionError("waiting supervisor must not spawn a child")

            supervisor = PersistentSupervisor(
                _config(project, max_cycles=1),
                snapshot_provider=lambda: snapshot,
                popen_factory=forbidden_spawn,
                sleep=clock.sleep,
                clock=clock,
            )
            self.assertEqual(supervisor.run(), 0)
            self.assertEqual(supervisor.state["restart_count"], 3)

    def test_missing_supervisor_state_restores_waiting_from_manifest_log_and_control(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：事件恢复"
            manifest = init_project(project, story_name="事件恢复", slug="event-restore")
            manifest["agent"]["job_id"] = "supervisor-job"
            manifest["agent"]["status"] = "wait_for_user"
            write_manifest(project_paths(project), manifest)
            append_ndjson(
                recovery_log_path(project),
                {
                    "kind": "recovery_decision_v1",
                    "job_id": "supervisor-job",
                    "stage": "suno_generate",
                    "action": "wait_for_user",
                    "required_action": "恢复登录后 resume",
                },
            )
            clock = FakeClock()
            snapshot = _snapshot(project, "suno_generate", "等待登录")
            snapshot["agent_status"] = "wait_for_user"

            def forbidden_spawn(*args, **kwargs):
                raise AssertionError("restored waiting supervisor must not spawn a child")

            supervisor = PersistentSupervisor(
                _config(project, max_cycles=1),
                snapshot_provider=lambda: snapshot,
                popen_factory=forbidden_spawn,
                sleep=clock.sleep,
                clock=clock,
            )
            self.assertEqual(supervisor.state["status"], "waiting_for_user")
            self.assertEqual(
                supervisor.state["restored_from"],
                "manifest+recovery_log+control",
            )
            self.assertEqual(supervisor.run(), 0)


if __name__ == "__main__":
    unittest.main()
