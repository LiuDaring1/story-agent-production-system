from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from story_agent_observability import (
    append_agent_event,
    emit_notification,
    notification_sinks,
    read_ndjson,
    recovery_log_path,
    status_dir,
    supervisor_state_path,
    timestamp,
)
from story_agent_recovery import (
    CodexDecisionRunner,
    CodexRecoveryController,
    RecoveryDecision,
    apply_recovery_decision,
)
from story_agent_runtime import AgentRuntimeError, load_control, process_is_alive
from story_project import load_manifest, project_paths, save_json


SnapshotProvider = Callable[[], dict[str, Any]]
PopenFactory = Callable[..., subprocess.Popen[Any]]


@dataclass(frozen=True)
class SupervisorConfig:
    project_dir: Path
    job_id: str
    run_command: tuple[str, ...]
    log_path: Path
    heartbeat_interval_seconds: float = 5.0
    heartbeat_timeout_seconds: int = 120
    initial_backoff_seconds: int = 10
    max_backoff_seconds: int = 600
    notification_sink_names: str = "project,codex,desktop"
    max_cycles: int = 0


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


@contextmanager
def supervisor_lifetime_lock(project_dir: Path) -> Iterator[Path]:
    lock = status_dir(project_dir) / "story_agent_supervisor.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    if lock.exists():
        payload = _read_json(lock)
        try:
            owner_pid = int(payload.get("pid") or 0)
        except (TypeError, ValueError):
            owner_pid = 0
        if process_is_alive(owner_pid):
            raise AgentRuntimeError(f"persistent supervisor already running with PID {owner_pid}")
        archive = status_dir(project_dir) / "recovery" / "stale_locks"
        archive.mkdir(parents=True, exist_ok=True)
        lock.replace(
            archive / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}-{lock.name}"
        )
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise AgentRuntimeError("another persistent supervisor is starting") from exc
    try:
        try:
            os.write(
                descriptor,
                json.dumps(
                    {"pid": os.getpid(), "token": token, "started_at": timestamp()},
                    ensure_ascii=False,
                ).encode("utf-8"),
            )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        yield lock
    finally:
        current = _read_json(lock)
        if current.get("token") == token:
            lock.unlink(missing_ok=True)


class PersistentSupervisor:
    def __init__(
        self,
        config: SupervisorConfig,
        *,
        snapshot_provider: SnapshotProvider,
        recovery_runner: CodexDecisionRunner | None = None,
        popen_factory: PopenFactory = subprocess.Popen,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.snapshot_provider = snapshot_provider
        self.recovery_controller = CodexRecoveryController(
            config.project_dir,
            runner=recovery_runner,
        )
        self.popen_factory = popen_factory
        self.sleep = sleep
        self.clock = clock
        self.sinks = notification_sinks(config.notification_sink_names)
        self.supervisor_id = uuid.uuid4().hex
        self.state: dict[str, Any] = {
            "kind": "story_agent_persistent_supervisor_state_v1",
            "version": 1,
            "supervisor_id": self.supervisor_id,
            "pid": os.getpid(),
            "job_id": config.job_id,
            "project_dir": str(config.project_dir.expanduser().resolve()),
            "started_at": timestamp(),
            "heartbeat_at": timestamp(),
            "heartbeat_timeout_seconds": config.heartbeat_timeout_seconds,
            "status": "starting",
            "restart_count": 0,
            "backoff_seconds": config.initial_backoff_seconds,
            "child": {},
            "recovery": {},
            "last_exit_code": None,
            "waiting_control_epoch": None,
        }
        previous = _read_json(supervisor_state_path(config.project_dir))
        if (
            previous.get("kind") == "story_agent_persistent_supervisor_state_v1"
            and str(previous.get("job_id") or "") == config.job_id
        ):
            self.state["previous_supervisor_id"] = str(previous.get("supervisor_id") or "")
            self.state["first_started_at"] = str(
                previous.get("first_started_at") or previous.get("started_at") or ""
            )
            self.state["restart_count"] = int(previous.get("restart_count") or 0)
            self.state["backoff_seconds"] = int(
                previous.get("backoff_seconds") or config.initial_backoff_seconds
            )
            self.state["recovery"] = (
                dict(previous.get("recovery"))
                if isinstance(previous.get("recovery"), Mapping)
                else {}
            )
            previous_status = str(previous.get("status") or "")
            if previous_status in {"wait_for_user", "waiting_for_user", "terminal_bug"}:
                self.state["status"] = (
                    "waiting_for_user" if previous_status == "wait_for_user" else previous_status
                )
                self.state["waiting_control_epoch"] = previous.get("waiting_control_epoch")
            previous_child = (
                previous.get("child") if isinstance(previous.get("child"), Mapping) else {}
            )
            try:
                orphaned_pid = int(previous_child.get("pid") or 0)
            except (TypeError, ValueError):
                orphaned_pid = 0
            if process_is_alive(orphaned_pid):
                self.state["orphaned_child_pid"] = orphaned_pid
                self.state["child"] = dict(previous_child)
                self.state["child"]["running"] = True
        if self.state.get("status") not in {"waiting_for_user", "terminal_bug"}:
            manifest = load_manifest(project_paths(config.project_dir)) or {}
            agent = manifest.get("agent", {}) if isinstance(manifest.get("agent"), Mapping) else {}
            manifest_status = str(agent.get("status") or "")
            if manifest_status in {"wait_for_user", "waiting_for_user", "terminal_bug"}:
                decisions = [
                    item
                    for item in read_ndjson(recovery_log_path(config.project_dir), limit=200)
                    if item.get("kind") == "recovery_decision_v1"
                    and str(item.get("job_id") or "") == config.job_id
                ]
                self.state["status"] = (
                    "waiting_for_user" if manifest_status == "wait_for_user" else manifest_status
                )
                self.state["waiting_control_epoch"] = int(
                    load_control(config.project_dir).get("run_epoch") or 0
                )
                if decisions:
                    self.state["recovery"] = dict(decisions[-1])
                self.state["restored_from"] = "manifest+recovery_log+control"
        self.child: subprocess.Popen[Any] | None = None
        self._child_started_clock = 0.0
        self._last_log_mtime = 0.0
        self._last_log_progress_clock = 0.0
        self.cycles = 0

    def _write_state(self, *, status: str | None = None, **updates: Any) -> None:
        if status is not None:
            self.state["status"] = status
        self.state.update(updates)
        self.state["heartbeat_at"] = timestamp()
        self.state["pid"] = os.getpid()
        save_json(supervisor_state_path(self.config.project_dir), self.state)

    def _event(self, event_type: str, *, status: str, summary: str, **metadata: Any) -> None:
        append_agent_event(
            self.config.project_dir,
            event_type=event_type,
            status=status,
            summary=summary,
            job_id=self.config.job_id,
            stage=str(metadata.pop("stage", "") or ""),
            recovery_decision=str(metadata.get("recovery_decision") or ""),
            blocker_category=str(metadata.get("blocker_category") or ""),
            metadata=metadata,
        )

    def _notify(
        self,
        *,
        category: str,
        severity: str,
        message: str,
        dedupe_key: str,
        required_action: str = "",
        recovery_mode: str = "",
        stage: str = "",
    ) -> None:
        emit_notification(
            self.config.project_dir,
            category=category,
            severity=severity,
            message=message,
            dedupe_key=dedupe_key,
            required_action=required_action,
            recovery_mode=recovery_mode,
            stage=stage,
            job_id=self.config.job_id,
            sinks=self.sinks,
        )

    def _cancel_requested(self) -> bool:
        return bool(load_control(self.config.project_dir).get("cancel_requested"))

    def _stop_child(self, *, reason: str) -> None:
        child = self.child
        if child is None or child.poll() is not None:
            return
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except OSError:
            try:
                child.terminate()
            except OSError:
                return
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                child.kill()
            child.wait(timeout=10)
        self._event(
            "supervisor_child_stopped",
            status="cancelled",
            summary=reason,
            child_pid=child.pid,
        )

    def _launch_child(self) -> subprocess.Popen[Any]:
        self.config.log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = self.config.log_path.open("a", encoding="utf-8")
        try:
            child = self.popen_factory(
                list(self.config.run_command),
                cwd=str(Path(__file__).resolve().parent),
                env=os.environ.copy(),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            log_file.close()
        self.child = child
        self._child_started_clock = self.clock()
        try:
            self._last_log_mtime = self.config.log_path.stat().st_mtime
        except OSError:
            self._last_log_mtime = 0.0
        self._last_log_progress_clock = self._child_started_clock
        self.state["child"] = {
            "pid": child.pid,
            "started_at": timestamp(),
            "command": list(self.config.run_command),
            "running": True,
        }
        self._write_state(status="running")
        self._event(
            "supervisor_child_started",
            status="running",
            summary=f"Agent child PID {child.pid} started",
            child_pid=child.pid,
            restart_count=self.state["restart_count"],
        )
        return child

    def _monitor_child(self, child: subprocess.Popen[Any]) -> tuple[int, bool]:
        watchdog_triggered = False
        notified_timeout = False
        hard_timeout = self._hard_liveness_timeout_seconds()
        while child.poll() is None:
            if self._cancel_requested():
                self._stop_child(reason="cancel requested")
                return int(child.returncode or 3), watchdog_triggered
            try:
                snapshot = self.snapshot_provider()
            except Exception as exc:
                snapshot = {"agent_heartbeat_age_seconds": None}
                self._event(
                    "snapshot_failed",
                    status="warning",
                    summary=f"{type(exc).__name__}: {exc}",
                )
            raw_heartbeat_age = snapshot.get("agent_heartbeat_age_seconds")
            child_runtime = max(0.0, self.clock() - self._child_started_clock)
            heartbeat_is_current = (
                isinstance(raw_heartbeat_age, (int, float))
                and raw_heartbeat_age
                <= child_runtime + max(2.0, self.config.heartbeat_interval_seconds * 2)
            )
            watchdog_age = (
                float(raw_heartbeat_age) if heartbeat_is_current else child_runtime
            )
            liveness = self._joint_liveness_evidence(snapshot)
            if watchdog_age > self.config.heartbeat_timeout_seconds:
                if not notified_timeout:
                    self._notify(
                        category="heartbeat_timeout",
                        severity="warning",
                        message=(
                            f"Agent child PID {child.pid} 已 {round(watchdog_age)} 秒无本次运行的 heartbeat；"
                            "进程仍存活，supervisor 将结合进程、日志和外部任务状态继续观察。"
                        ),
                        dedupe_key=f"{self.config.job_id}:heartbeat:{child.pid}",
                        recovery_mode="automatic",
                    )
                    self._event(
                        "heartbeat_timeout",
                        status="warning",
                        summary=f"heartbeat stale but child process alive: {watchdog_age:.1f}s",
                        child_pid=child.pid,
                        raw_agent_heartbeat_age_seconds=raw_heartbeat_age,
                    )
                    notified_timeout = True
            # A stale 120-second manifest heartbeat is never sufficient to
            # terminate a live Luna/Sol, provider, or FFmpeg process.  The hard
            # watchdog is derived from the configured Codex timeout and is only
            # reached after a much longer absence of every normal completion.
            if child_runtime > hard_timeout and not any(
                bool(liveness.get(key))
                for key in ("recent_log_activity", "nested_worker_alive", "provider_task_active")
            ):
                watchdog_triggered = True
                self._stop_child(reason="joint liveness hard timeout")
                break
            self.state["child"]["running"] = child.poll() is None
            self.state["child"]["heartbeat_age_seconds"] = raw_heartbeat_age
            self.state["child"]["watchdog_age_seconds"] = round(watchdog_age, 1)
            self.state["child"]["heartbeat_seen_after_launch"] = heartbeat_is_current
            self.state["child"]["process_alive"] = child.poll() is None
            self.state["child"]["hard_liveness_timeout_seconds"] = hard_timeout
            self.state["child"]["liveness_policy"] = "process+manifest+log+provider; heartbeat_alone_nonfatal"
            self.state["child"]["joint_liveness_evidence"] = liveness
            self._write_state(status="running")
            self.sleep(max(0.1, self.config.heartbeat_interval_seconds))
        return int(child.returncode or 0), watchdog_triggered

    def _hard_liveness_timeout_seconds(self) -> float:
        codex_timeout = 0.0
        command = list(self.config.run_command)
        if "--codex-timeout" in command:
            try:
                codex_timeout = float(command[command.index("--codex-timeout") + 1])
            except (IndexError, TypeError, ValueError):
                codex_timeout = 0.0
        return max(
            float(self.config.heartbeat_timeout_seconds) * 6,
            codex_timeout * 3 + 600 if codex_timeout > 0 else 0.0,
        )

    def _joint_liveness_evidence(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        log_age: float | None = None
        log_progress_age: float | None = None
        try:
            current_mtime = self.config.log_path.stat().st_mtime
            log_age = max(0.0, time.time() - current_mtime)
            if current_mtime > self._last_log_mtime:
                self._last_log_mtime = current_mtime
                self._last_log_progress_clock = self.clock()
            if self._last_log_progress_clock:
                log_progress_age = max(0.0, self.clock() - self._last_log_progress_clock)
        except OSError:
            pass
        scheduler = snapshot.get("scheduler") if isinstance(snapshot.get("scheduler"), Mapping) else {}
        running = scheduler.get("running") if isinstance(scheduler.get("running"), Mapping) else {}
        nested_worker_alive = False
        for item in running.values():
            if not isinstance(item, Mapping):
                continue
            try:
                pid = int(item.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            if pid and process_is_alive(pid):
                nested_worker_alive = True
                break
        receipts = snapshot.get("provider_receipts") if isinstance(snapshot.get("provider_receipts"), list) else []
        active_statuses = {"queued", "submitted", "running", "processing", "in_progress", "pending"}
        provider_task_active = any(
            isinstance(item, Mapping)
            and str(item.get("status") or item.get("state") or "").lower() in active_statuses
            for item in receipts
        )
        # A log file merely existing is not proof of life. Only a changed
        # mtime refreshes this clock, which also keeps fake-clock watchdog
        # tests deterministic.
        recent_window = max(30.0, float(self.config.heartbeat_timeout_seconds) * 2)
        return {
            "process_alive": True,
            "manifest_heartbeat_current": isinstance(snapshot.get("agent_heartbeat_age_seconds"), (int, float))
            and float(snapshot["agent_heartbeat_age_seconds"]) <= self.config.heartbeat_timeout_seconds,
            "log_age_seconds": round(log_age, 1) if log_age is not None else None,
            "log_progress_age_seconds": round(log_progress_age, 1) if log_progress_age is not None else None,
            "recent_log_activity": log_progress_age is not None and log_progress_age <= recent_window,
            "nested_worker_alive": nested_worker_alive,
            "provider_task_active": provider_task_active,
        }

    def _wait_backoff(self, seconds: int, *, decision: RecoveryDecision) -> str:
        deadline = self.clock() + max(0, seconds)
        while self.clock() < deadline:
            if self._cancel_requested():
                return "cancelled"
            self._write_state(
                status="backoff",
                retry_at=decision.retry_at,
                backoff_remaining_seconds=max(0, round(deadline - self.clock())),
            )
            self.sleep(
                max(
                    0.1,
                    min(self.config.heartbeat_interval_seconds, deadline - self.clock()),
                )
            )
        return "ready"

    def _waiting_gate_open(self, snapshot: Mapping[str, Any]) -> bool:
        waiting_epoch = self.state.get("waiting_control_epoch")
        control = load_control(self.config.project_dir)
        if self._cancel_requested():
            return False
        if waiting_epoch is None:
            return False
        return (
            int(control.get("run_epoch") or 0) > int(waiting_epoch)
            and str(snapshot.get("agent_status") or "") == "pending"
        )

    def _handle_waiting(self, snapshot: Mapping[str, Any]) -> str:
        if self._cancel_requested():
            return "cancelled"
        if self._waiting_gate_open(snapshot):
            self.state["waiting_control_epoch"] = None
            self.state["recovery"] = {}
            self._write_state(status="resuming")
            self._event(
                "supervisor_user_resume_observed",
                status="pending",
                summary="control epoch advanced and manifest returned to pending",
            )
            return "resume"
        self._write_state(status=str(self.state.get("status") or "waiting_for_user"))
        self.sleep(max(0.1, self.config.heartbeat_interval_seconds))
        return "waiting"

    def _completion(self, snapshot: Mapping[str, Any]) -> bool:
        return bool(snapshot.get("completion_valid")) and str(
            snapshot.get("agent_status") or ""
        ) == "completed"

    def _monitor_orphaned_child_once(self, snapshot: Mapping[str, Any]) -> str:
        try:
            pid = int(self.state.get("orphaned_child_pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid <= 0:
            return "none"
        if self._cancel_requested():
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            return "cancelled"
        if process_is_alive(pid):
            heartbeat_age = snapshot.get("agent_heartbeat_age_seconds")
            self.state["child"]["heartbeat_age_seconds"] = heartbeat_age
            self._write_state(status="monitoring_orphaned_worker")
            self.state["child"]["liveness_policy"] = "live orphan process is never killed by heartbeat age alone"
            self.sleep(max(0.1, self.config.heartbeat_interval_seconds))
            return "monitoring"
        self.state.pop("orphaned_child_pid", None)
        self.state["child"]["running"] = False
        self._write_state(status="diagnosing_orphaned_worker")
        recovery_snapshot = {
            **snapshot,
            "blocked_reason": (
                str(snapshot.get("blocked_reason") or "")
                + "；orphaned worker crash detected after supervisor restart; stale lock possible"
            ).strip("；"),
        }
        self._diagnose_and_recover(
            recovery_snapshot,
            exit_code=1,
            watchdog_triggered=True,
        )
        return "recovered"

    def _cycle_limit_reached(self) -> bool:
        return self.config.max_cycles > 0 and self.cycles >= self.config.max_cycles

    def _diagnose_and_recover(
        self,
        snapshot: Mapping[str, Any],
        *,
        exit_code: int,
        watchdog_triggered: bool = False,
    ) -> str:
        scheduler = snapshot.get("scheduler", {}) if isinstance(snapshot.get("scheduler"), Mapping) else {}
        running = scheduler.get("running", {}) if isinstance(scheduler.get("running"), Mapping) else {}
        for stage, item in running.items():
            if not isinstance(item, Mapping):
                continue
            try:
                pid = int(item.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            if process_is_alive(pid):
                self.state["orphaned_child_pid"] = pid
                self.state["child"] = {
                    **dict(item),
                    "pid": pid,
                    "stage": stage,
                    "running": True,
                    "adopted_after_supervisor_restart": True,
                }
                self._write_state(status="monitoring_orphaned_worker")
                self._notify(
                    category="supervisor",
                    severity="warning",
                    message=f"检测到仍存活的 orphan worker PID {pid}（{stage}），正在接管监控。",
                    dedupe_key=f"{self.config.job_id}:orphan-worker:{pid}",
                    recovery_mode="monitoring",
                    stage=str(stage),
                )
                return "monitoring"
        if watchdog_triggered:
            snapshot = {
                **snapshot,
                "blocked_reason": (
                    str(snapshot.get("blocked_reason") or "")
                    + "；heartbeat timeout watchdog detected worker crash or stale lock"
                ).strip("；"),
            }
        decision = self.recovery_controller.decide(
            snapshot,
            exit_code=exit_code,
            log_paths=(self.config.log_path,),
        )
        self.state["recovery"] = asdict(decision)
        if decision.action in {"wait_for_user", "terminal_bug"}:
            apply_recovery_decision(self.config.project_dir, snapshot, decision)
            control = load_control(self.config.project_dir)
            self.state["waiting_control_epoch"] = int(control.get("run_epoch") or 0)
            self._write_state(
                status=(
                    "waiting_for_user"
                    if decision.action == "wait_for_user"
                    else "terminal_bug"
                )
            )
            self._notify(
                category=(
                    "waiting_for_user"
                    if decision.action == "wait_for_user"
                    else "terminal_bug"
                ),
                severity="warning" if decision.action == "wait_for_user" else "critical",
                message=decision.reason,
                dedupe_key=(
                    f"{self.config.job_id}:{decision.stage}:{decision.action}:"
                    f"{decision.evidence_sha256[:16]}"
                ),
                required_action=decision.required_action,
                recovery_mode=decision.action,
                stage=decision.stage,
            )
            return "waiting"
        repair = apply_recovery_decision(self.config.project_dir, snapshot, decision)
        if not repair.success:
            self.state["waiting_control_epoch"] = int(
                load_control(self.config.project_dir).get("run_epoch") or 0
            )
            self._write_state(
                status="terminal_bug",
                repair_failure=repair.message,
            )
            self._notify(
                category="terminal_bug",
                severity="critical",
                message=f"自动恢复被安全门禁拒绝：{repair.message}",
                dedupe_key=(
                    f"{self.config.job_id}:{decision.stage}:repair-refused:"
                    f"{decision.evidence_sha256[:16]}"
                ),
                required_action="请检查 recovery 诊断包并修复后执行 resume。",
                recovery_mode="terminal_bug",
                stage=decision.stage,
            )
            return "waiting"
        self.state["restart_count"] = int(self.state.get("restart_count") or 0) + 1
        configured_backoff = int(self.state.get("backoff_seconds") or 0)
        delay = max(decision.retry_after_seconds, configured_backoff)
        delay = min(self.config.max_backoff_seconds, max(5, delay))
        self.state["backoff_seconds"] = min(
            self.config.max_backoff_seconds,
            max(self.config.initial_backoff_seconds, delay * 2),
        )
        self._write_state(status="backoff")
        self._notify(
            category="automatic_recovery",
            severity="warning",
            message=(
                f"{decision.stage} 将自动恢复：{decision.reason}；"
                f"{delay} 秒后启动新 attempt。"
            ),
            dedupe_key=(
                f"{self.config.job_id}:{decision.stage}:auto-recovery:"
                f"{decision.evidence_sha256[:16]}"
            ),
            recovery_mode=decision.action,
            stage=decision.stage,
        )
        return self._wait_backoff(delay, decision=decision)

    def run(self) -> int:
        with supervisor_lifetime_lock(self.config.project_dir):
            restored_status = str(self.state.get("status") or "")
            self._write_state(
                status=(
                    restored_status
                    if restored_status in {"waiting_for_user", "terminal_bug"}
                    else "starting"
                )
            )
            self._event(
                "persistent_supervisor_started",
                status="running",
                summary=f"Persistent supervisor PID {os.getpid()} started",
            )
            self._notify(
                category="supervisor",
                severity="info",
                message=f"Story Agent persistent supervisor 已启动，PID {os.getpid()}。",
                dedupe_key=f"{self.config.job_id}:supervisor:{self.supervisor_id}:started",
                recovery_mode="monitoring",
            )
            while True:
                self.cycles += 1
                if self._cancel_requested():
                    self._stop_child(reason="cancel requested")
                    self._write_state(status="cancelled")
                    self._notify(
                        category="supervisor",
                        severity="warning",
                        message="Story Agent supervisor 已按取消请求停止。",
                        dedupe_key=f"{self.config.job_id}:supervisor:cancelled",
                        recovery_mode="none",
                    )
                    return 3
                try:
                    snapshot = self.snapshot_provider()
                except Exception as exc:
                    snapshot = {
                        "job_id": self.config.job_id,
                        "agent_status": "failed",
                        "current_stage": "",
                        "next_stage": "",
                        "blocked_reason": f"snapshot failure: {type(exc).__name__}: {exc}",
                        "stage_rows": [],
                        "events": [],
                        "recent_logs": [],
                    }
                if self._completion(snapshot):
                    self._write_state(status="completed", finished_at=timestamp(), child={})
                    self._event(
                        "persistent_supervisor_completed",
                        status="completed",
                        summary="All effective stage gates passed",
                    )
                    self._notify(
                        category="supervisor",
                        severity="info",
                        message="Story Agent 已完成全部阶段并通过当前有效门禁。",
                        dedupe_key=f"{self.config.job_id}:supervisor:completed",
                        recovery_mode="none",
                    )
                    return 0
                orphan_result = self._monitor_orphaned_child_once(snapshot)
                if orphan_result in {"monitoring", "recovered", "cancelled"}:
                    if self._cycle_limit_reached():
                        self._write_state(status="paused_for_test")
                        return 0
                    continue
                if self.state.get("status") in {"waiting_for_user", "terminal_bug"}:
                    waiting_result = self._handle_waiting(snapshot)
                    if waiting_result == "cancelled":
                        continue
                    if self._cycle_limit_reached():
                        self._write_state(status="paused_for_test")
                        return 0
                    if waiting_result == "waiting":
                        continue
                if self._cycle_limit_reached():
                    self._write_state(status="paused_for_test")
                    return 0

                watchdog_triggered = False
                try:
                    child = self._launch_child()
                    exit_code, watchdog_triggered = self._monitor_child(child)
                except OSError as exc:
                    exit_code = 1
                    self.state["child"] = {
                        "pid": 0,
                        "started_at": timestamp(),
                        "running": False,
                        "spawn_error": f"{type(exc).__name__}: {exc}",
                    }
                self.state["last_exit_code"] = exit_code
                self.state["child"]["running"] = False
                self.state["child"]["finished_at"] = timestamp()
                self._write_state(status="diagnosing")
                try:
                    snapshot = self.snapshot_provider()
                except Exception as exc:
                    snapshot = {
                        "job_id": self.config.job_id,
                        "agent_status": "failed",
                        "completion_valid": False,
                        "current_stage": "",
                        "next_stage": "",
                        "blocked_reason": (
                            f"post-child snapshot failure: {type(exc).__name__}: {exc}"
                        ),
                        "stage_rows": [],
                        "events": [],
                        "recent_logs": [str(self.config.log_path)],
                    }
                    self._event(
                        "snapshot_failed",
                        status="failed",
                        summary=str(snapshot["blocked_reason"]),
                    )
                if self._completion(snapshot):
                    continue
                try:
                    self._diagnose_and_recover(
                        snapshot,
                        exit_code=exit_code,
                        watchdog_triggered=watchdog_triggered,
                    )
                except Exception as exc:
                    stage = str(
                        snapshot.get("current_stage") or snapshot.get("next_stage") or ""
                    )
                    message = f"recovery controller failure: {type(exc).__name__}: {exc}"
                    self.state["waiting_control_epoch"] = int(
                        load_control(self.config.project_dir).get("run_epoch") or 0
                    )
                    self._write_state(status="terminal_bug", recovery_controller_error=message)
                    self._event(
                        "recovery_controller_failed",
                        status="terminal_bug",
                        summary=message,
                        stage=stage,
                    )
                    self._notify(
                        category="terminal_bug",
                        severity="critical",
                        message=message,
                        dedupe_key=f"{self.config.job_id}:{stage}:recovery-controller-failed",
                        required_action="请检查 supervisor 日志与 recovery 证据后修复，再执行 resume。",
                        recovery_mode="terminal_bug",
                        stage=stage,
                    )
