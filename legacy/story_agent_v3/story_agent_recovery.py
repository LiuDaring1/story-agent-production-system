from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from story_agent_observability import (
    append_agent_event,
    append_ndjson,
    canonical_sha256,
    recovery_log_path,
    safe_payload,
    status_dir,
    timestamp,
)
from story_agent_runtime import (
    STORY_STAGE_SEQUENCE,
    ensure_manifest_v2,
    file_sha256,
    freeze_runtime,
    process_is_alive,
)
from story_project import load_manifest, project_paths, save_json, write_manifest


RECOVERY_ACTIONS = frozenset(
    {"retry_after", "repair_and_retry", "wait_for_user", "terminal_bug"}
)
SAFE_REPAIRS: dict[str, str] = {
    "requeue_stage": "只把当前失败阶段改回 pending，保留 attempt、日志与产物证据。",
    "reset_interrupted_stage": "仅当 running worker 已死亡时，把该阶段改回 pending。",
    "clear_orphaned_scheduler_entries": "仅移除 PID 已死亡的 scheduler.running 条目。",
    "archive_stale_job_lock": "仅当锁记录 PID 已死亡时，把锁移动到 recovery/stale_locks 归档。",
}
MAX_INFRASTRUCTURE_RECOVERIES = 8
MAX_RETRY_AFTER_SECONDS = 6 * 3600

CodexDecisionRunner = Callable[[Path, Path], tuple[bool, str, str]]


@dataclass(frozen=True)
class RecoveryDecision:
    decision_id: str
    created_at: str
    job_id: str
    stage: str
    attempt_id: str
    action: str
    category: str
    reason: str
    evidence_sha256: str
    evidence_path: str
    source: str
    retry_after_seconds: int = 0
    retry_at: str = ""
    safe_repairs: tuple[str, ...] = ()
    required_action: str = ""
    model: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RepairResult:
    success: bool
    stage: str
    actions: tuple[str, ...]
    message: str
    report_path: str


def _retry_at(seconds: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + seconds))


def classify_recovery(
    message: str,
    *,
    stage: str,
    job_id: str,
    attempt_id: str,
    evidence_sha256: str,
    evidence_path: Path,
) -> RecoveryDecision:
    """Choose the safest deterministic fallback when Codex is unavailable."""

    text = message.lower()
    base = {
        "decision_id": uuid.uuid4().hex,
        "created_at": timestamp(),
        "job_id": job_id,
        "stage": stage,
        "attempt_id": attempt_id,
        "evidence_sha256": evidence_sha256,
        "evidence_path": str(evidence_path),
        "source": "deterministic",
    }
    if any(
        marker in text
        for marker in (
            "验证码",
            "captcha",
            "登录",
            "login",
            "sign in",
            "付费",
            "payment",
            "订阅",
            "积分不足",
            "预算",
            "hard limit",
            "额度",
            "api key",
            "credential",
            "鉴权",
            "authorization",
            "permission",
            "权限",
            "用户确认",
            "人工确认",
            "审美确认",
            "外部发布",
        )
    ):
        return RecoveryDecision(
            **base,
            action="wait_for_user",
            category="authorization_or_user_action",
            reason=message or "需要用户授权或交互。",
            required_action="请完成登录/验证码/付费或明确授权后运行 resume；系统不会自动绕过。",
        )
    if any(
        marker in text
        for marker in (
            "502",
            "503",
            "504",
            "429",
            "rate limit",
            "too many requests",
            "timed out",
            "timeout",
            "connection reset",
            "temporary",
            "temporarily",
            "网络",
            "限流",
            "服务暂时",
        )
    ):
        delay = 60 if any(marker in text for marker in ("429", "rate limit", "限流")) else 30
        return RecoveryDecision(
            **base,
            action="retry_after",
            category="transient_infrastructure",
            reason=message or "临时基础设施错误。",
            retry_after_seconds=delay,
            retry_at=_retry_at(delay),
        )
    if any(
        marker in text
        for marker in (
            "worker 异常退出",
            "worker crash",
            "heartbeat",
            "心跳",
            "orphan",
            "残留锁",
            "stale lock",
            "结果信封无效",
            "interrupted",
        )
    ):
        repairs = [
            "archive_stale_job_lock",
            "clear_orphaned_scheduler_entries",
            "reset_interrupted_stage",
            "requeue_stage",
        ]
        return RecoveryDecision(
            **base,
            action="repair_and_retry",
            category="interrupted_worker",
            reason=message or "worker 或锁状态中断。",
            safe_repairs=tuple(repairs),
            retry_after_seconds=5,
            retry_at=_retry_at(5),
        )
    if any(
        marker in text
        for marker in (
            "nameerror",
            "attributeerror",
            "assertionerror",
            "syntaxerror",
            "manifest 合并冲突",
            "不变量",
            "invariant",
            "unknown stage",
            "未知阶段",
        )
    ):
        return RecoveryDecision(
            **base,
            action="terminal_bug",
            category="code_or_invariant_bug",
            reason=message or "检测到代码或不变量错误。",
            required_action="请让 Codex/开发者检查诊断包和代码；未经修复不要继续付费阶段。",
        )
    return RecoveryDecision(
        **base,
        action="terminal_bug",
        category="unclassified_failure",
        reason=message or "无法安全分类的失败。",
        required_action="需要 Codex/开发者检查诊断证据；系统不会盲目重试。",
    )


def _safe_log_snippet(path: Path, *, max_lines: int = 120, max_chars: int = 12_000) -> dict[str, Any]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        snippet = "\n".join(lines[-max_lines:])
        return {
            "path": str(path),
            "sha256": file_sha256(path),
            "tail": safe_payload(snippet[-max_chars:]),
        }
    except OSError as exc:
        return {"path": str(path), "error": f"{type(exc).__name__}: {exc}"}


def build_recovery_evidence(
    project_dir: Path,
    snapshot: Mapping[str, Any],
    *,
    exit_code: int,
    log_paths: Sequence[Path] = (),
) -> tuple[Path, str]:
    project_root = project_dir.expanduser().resolve()
    recovery_dir = status_dir(project_root) / "recovery"
    recovery_dir.mkdir(parents=True, exist_ok=True)
    selected_logs: list[dict[str, Any]] = []
    candidate_paths = [*log_paths]
    for raw in snapshot.get("recent_logs", []) if isinstance(snapshot.get("recent_logs"), list) else []:
        candidate_paths.append(Path(str(raw)))
    seen: set[str] = set()
    for candidate in candidate_paths:
        try:
            resolved = candidate.expanduser().resolve(strict=True)
            if os.path.commonpath((str(resolved), str(project_root))) != str(project_root):
                continue
        except (OSError, ValueError):
            continue
        key = str(resolved)
        if key in seen or len(selected_logs) >= 6:
            continue
        seen.add(key)
        selected_logs.append(_safe_log_snippet(resolved))
    stage = str(snapshot.get("current_stage") or snapshot.get("next_stage") or "")
    stage_rows = snapshot.get("stage_rows", []) if isinstance(snapshot.get("stage_rows"), list) else []
    stage_record = next(
        (item for item in stage_rows if isinstance(item, Mapping) and item.get("stage") == stage),
        {},
    )
    payload = {
        "kind": "recovery_evidence_v1",
        "created_at": timestamp(),
        "project_dir": str(project_root),
        "job_id": str(snapshot.get("job_id") or ""),
        "exit_code": int(exit_code),
        "agent_status": str(snapshot.get("agent_status") or ""),
        "current_stage": stage,
        "blocked_reason": str(snapshot.get("blocked_reason") or ""),
        "stage_record": stage_record,
        "supervisor": snapshot.get("supervisor", {}),
        "locks": snapshot.get("locks", {}),
        "budget": snapshot.get("budget", {}),
        "timing": snapshot.get("timing", {}),
        "artifact_progress": snapshot.get("artifact_progress", {}),
        "provider_receipts": snapshot.get("provider_receipts", [])[-40:],
        "recent_events": snapshot.get("events", [])[-80:],
        "log_evidence": selected_logs,
        "allowed_repairs": SAFE_REPAIRS,
        "prohibited_actions": [
            "不得登录、绕过验证码、付费、提高预算或扩大权限",
            "不得调用 ImageGen、Grok、Suno 或其他生产 provider 作为诊断动作",
            "不得删除或覆盖用户原片、审核证据或生产产物",
            "不得修改密钥、把密钥写入 prompt/manifest/log",
            "不得直接改仓库源代码；代码缺陷必须 terminal_bug 并等待开发修复",
            "修复后仍必须重新执行机器门禁和独立审核",
        ],
    }
    path = recovery_dir / f"evidence_{time.strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:8]}.json"
    save_json(path, safe_payload(payload))
    return path, file_sha256(path)


def write_recovery_prompt(
    project_dir: Path,
    *,
    evidence_path: Path,
    evidence_sha256: str,
) -> Path:
    target = status_dir(project_dir) / "recovery" / f"codex_recovery_{uuid.uuid4().hex[:8]}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "\n".join(
            [
                "# Story Agent 恢复控制器",
                "",
                f"只读证据包：{evidence_path}",
                f"证据 SHA-256：{evidence_sha256}",
                "",
                "请读取证据并判断唯一恢复动作。不要执行修复、不要访问 provider、不要登录或付费。",
                "最终回复只能是一个 JSON 对象，不要 Markdown 围栏：",
                "{",
                f'  "evidence_sha256": "{evidence_sha256}",',
                '  "action": "retry_after|repair_and_retry|wait_for_user|terminal_bug",',
                '  "category": "简短机器分类",',
                '  "reason": "基于证据的简洁原因",',
                '  "retry_after_seconds": 0,',
                '  "safe_repairs": ["仅可使用证据包 allowed_repairs 中的 ID"],',
                '  "required_action": "仅 wait_for_user/terminal_bug 时说明用户动作"',
                "}",
                "",
                "约束：",
                "- 临时网络/限流选 retry_after，5 到 21600 秒。",
                "- 只有白名单本地修复可选 repair_and_retry；不得提议任意 shell、删文件或改代码。",
                "- 登录、验证码、付费、审美确认、外部授权一律 wait_for_user。",
                "- 代码缺陷或不变量破坏一律 terminal_bug。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def _json_object_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fence = chr(96) * 3
    if stripped.startswith(fence):
        stripped = re.sub("^" + re.escape(fence) + r"(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*" + re.escape(fence) + "$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Codex recovery response does not contain a JSON object")
    payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Codex recovery response must be an object")
    return payload


def validate_codex_decision(
    text: str,
    *,
    evidence_sha256: str,
    evidence_path: Path,
    job_id: str,
    stage: str,
    attempt_id: str,
    model: str,
) -> RecoveryDecision:
    payload = _json_object_from_text(text)
    if payload.get("evidence_sha256") != evidence_sha256:
        raise ValueError("Codex recovery decision is not bound to the reviewed evidence SHA-256")
    action = str(payload.get("action") or "")
    if action not in RECOVERY_ACTIONS:
        raise ValueError(f"unknown recovery action: {action}")
    try:
        retry_seconds = int(payload.get("retry_after_seconds") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("retry_after_seconds must be an integer") from exc
    raw_repairs = payload.get("safe_repairs", [])
    if not isinstance(raw_repairs, list):
        raise ValueError("safe_repairs must be an array")
    repairs = tuple(str(item) for item in raw_repairs if str(item))
    unknown_repairs = sorted(set(repairs) - set(SAFE_REPAIRS))
    if unknown_repairs:
        raise ValueError("unsafe recovery repair requested: " + ", ".join(unknown_repairs))
    required_action = str(payload.get("required_action") or "")
    if action == "retry_after":
        if retry_seconds < 5 or retry_seconds > MAX_RETRY_AFTER_SECONDS:
            raise ValueError("retry_after_seconds outside 5..21600")
        repairs = ()
    elif action == "repair_and_retry":
        if not repairs:
            raise ValueError("repair_and_retry requires at least one safe repair")
        retry_seconds = max(5, min(MAX_RETRY_AFTER_SECONDS, retry_seconds or 5))
    else:
        retry_seconds = 0
        repairs = ()
        if not required_action:
            raise ValueError(f"{action} requires required_action")
    return RecoveryDecision(
        decision_id=uuid.uuid4().hex,
        created_at=timestamp(),
        job_id=job_id,
        stage=stage,
        attempt_id=attempt_id,
        action=action,
        category=str(payload.get("category") or "codex_classified"),
        reason=str(payload.get("reason") or ""),
        evidence_sha256=evidence_sha256,
        evidence_path=str(evidence_path),
        source="codex",
        retry_after_seconds=retry_seconds,
        retry_at=_retry_at(retry_seconds) if retry_seconds else "",
        safe_repairs=repairs,
        required_action=required_action,
        model=model,
    )


class CodexRecoveryController:
    def __init__(
        self,
        project_dir: Path,
        *,
        runner: CodexDecisionRunner | None = None,
    ) -> None:
        self.project_dir = project_dir.expanduser()
        self.runner = runner

    def decide(
        self,
        snapshot: Mapping[str, Any],
        *,
        exit_code: int,
        log_paths: Sequence[Path] = (),
    ) -> RecoveryDecision:
        evidence_path, evidence_sha256 = build_recovery_evidence(
            self.project_dir,
            snapshot,
            exit_code=exit_code,
            log_paths=log_paths,
        )
        job_id = str(snapshot.get("job_id") or "")
        stage = str(snapshot.get("current_stage") or snapshot.get("next_stage") or "")
        stage_rows = snapshot.get("stage_rows", []) if isinstance(snapshot.get("stage_rows"), list) else []
        row = next(
            (item for item in stage_rows if isinstance(item, Mapping) and item.get("stage") == stage),
            {},
        )
        attempt_id = str(row.get("attempt_id") or "")
        fallback = classify_recovery(
            str(snapshot.get("blocked_reason") or row.get("message") or ""),
            stage=stage,
            job_id=job_id,
            attempt_id=attempt_id,
            evidence_sha256=evidence_sha256,
            evidence_path=evidence_path,
        )
        decision = fallback
        model_error = ""
        if self.runner is not None:
            prompt_path = write_recovery_prompt(
                self.project_dir,
                evidence_path=evidence_path,
                evidence_sha256=evidence_sha256,
            )
            try:
                succeeded, output, model = self.runner(prompt_path, evidence_path)
                if succeeded:
                    candidate = validate_codex_decision(
                        output,
                        evidence_sha256=evidence_sha256,
                        evidence_path=evidence_path,
                        job_id=job_id,
                        stage=stage,
                        attempt_id=attempt_id,
                        model=model,
                    )
                    if (
                        fallback.action in {"wait_for_user", "terminal_bug"}
                        and candidate.action != fallback.action
                    ):
                        model_error = (
                            f"Codex decision {candidate.action} cannot override mandatory "
                            f"{fallback.action} safety classification"
                        )
                    else:
                        decision = candidate
                else:
                    model_error = "Codex recovery runner failed; deterministic fallback used"
            except Exception as exc:
                model_error = f"{type(exc).__name__}: {exc}"
        decision_payload = {
            "kind": "recovery_decision_v1",
            **asdict(decision),
            "model_error": model_error,
        }
        decision_payload["decision_sha256"] = canonical_sha256(decision_payload)
        append_ndjson(recovery_log_path(self.project_dir), decision_payload)
        decision_path = (
            status_dir(self.project_dir) / "recovery" / f"decision_{decision.decision_id}.json"
        )
        save_json(decision_path, decision_payload)
        append_agent_event(
            self.project_dir,
            event_type="recovery_decided",
            status=decision.action,
            summary=decision.reason,
            job_id=decision.job_id,
            stage=decision.stage,
            attempt_id=decision.attempt_id,
            blocker_category=decision.category,
            recovery_decision=decision.action,
            input_evidence_hashes={"recovery_evidence": decision.evidence_sha256},
            output_evidence_hashes={"decision": file_sha256(decision_path)},
            metadata={
                "source": decision.source,
                "safe_repairs": list(decision.safe_repairs),
                "retry_at": decision.retry_at,
                "required_action": decision.required_action,
                "model": decision.model,
                "model_error": model_error,
            },
        )
        return decision


def _archive_stale_job_lock(project_dir: Path) -> str:
    lock = project_paths(project_dir).status / "story_agent.lock"
    if not lock.exists():
        return "job lock absent"
    try:
        payload = json.loads(lock.read_text(encoding="utf-8"))
        pid = int(payload.get("pid") or 0)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot prove job lock is stale: {exc}") from exc
    if process_is_alive(pid):
        raise RuntimeError(f"job lock owner PID {pid} is still alive")
    archive = status_dir(project_dir) / "recovery" / "stale_locks"
    archive.mkdir(parents=True, exist_ok=True)
    target = archive / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}-{lock.name}"
    shutil.move(str(lock), str(target))
    return str(target)


def apply_recovery_decision(
    project_dir: Path,
    snapshot: Mapping[str, Any],
    decision: RecoveryDecision,
) -> RepairResult:
    paths = project_paths(project_dir)
    manifest = ensure_manifest_v2(load_manifest(paths) or {})
    agent = manifest["agent"]
    stage = decision.stage or str(snapshot.get("current_stage") or snapshot.get("next_stage") or "")
    if stage not in STORY_STAGE_SEQUENCE:
        return RepairResult(False, stage, (), f"unknown recovery stage: {stage}", "")
    record = agent["stages"][stage]
    performed: list[str] = []
    notes: list[str] = []

    if decision.action in {"wait_for_user", "terminal_bug"}:
        freeze_runtime(agent)
        agent["status"] = decision.action
        agent["blocked_reason"] = decision.reason
        write_manifest(paths, manifest)
        report_path = status_dir(project_dir) / "recovery" / f"repair_{decision.decision_id}.json"
        save_json(
            report_path,
            {
                "kind": "recovery_application_v1",
                "decision_id": decision.decision_id,
                "action": decision.action,
                "stage": stage,
                "success": True,
                "performed": [],
                "message": decision.required_action,
                "created_at": timestamp(),
            },
        )
        return RepairResult(True, stage, (), decision.required_action, str(report_path))

    infrastructure_attempts = int(record.get("infrastructure_attempts") or 0)
    if infrastructure_attempts >= MAX_INFRASTRUCTURE_RECOVERIES:
        return RepairResult(
            False,
            stage,
            (),
            f"{stage} infrastructure recovery limit reached ({MAX_INFRASTRUCTURE_RECOVERIES})",
            "",
        )

    requested = list(decision.safe_repairs)
    if decision.action == "retry_after":
        requested = ["requeue_stage"]
    for repair in requested:
        if repair not in SAFE_REPAIRS:
            return RepairResult(False, stage, tuple(performed), f"unsafe repair: {repair}", "")
        if repair == "archive_stale_job_lock":
            try:
                notes.append(_archive_stale_job_lock(project_dir))
            except (OSError, RuntimeError) as exc:
                return RepairResult(
                    False,
                    stage,
                    tuple(performed),
                    f"archive_stale_job_lock refused: {exc}",
                    "",
                )
        elif repair == "clear_orphaned_scheduler_entries":
            running = agent.get("scheduler", {}).get("running", {})
            if not isinstance(running, dict):
                running = {}
            for running_stage, item in list(running.items()):
                pid = int(item.get("pid") or 0) if isinstance(item, Mapping) else 0
                if not process_is_alive(pid):
                    running.pop(running_stage, None)
                    notes.append(f"removed orphan scheduler entry {running_stage}:{pid}")
        elif repair == "reset_interrupted_stage":
            running = agent.get("scheduler", {}).get("running", {})
            item = running.get(stage, {}) if isinstance(running, dict) else {}
            pid = int(item.get("pid") or 0) if isinstance(item, Mapping) else 0
            if process_is_alive(pid):
                return RepairResult(
                    False,
                    stage,
                    tuple(performed),
                    f"refusing to reset live worker PID {pid}",
                    "",
                )
            if record.get("status") == "running":
                record["status"] = "pending"
                notes.append("interrupted stage reset to pending")
        elif repair == "requeue_stage":
            record["status"] = "pending"
            record["finished_at"] = ""
            record["retry_reason"] = decision.reason
            notes.append("stage requeued without deleting artifacts")
        performed.append(repair)

    record["infrastructure_attempts"] = infrastructure_attempts + 1
    record["last_recovery_decision_id"] = decision.decision_id
    record["last_recovery_evidence_sha256"] = decision.evidence_sha256
    branch_blockers = agent.get("branch_blockers", {})
    if isinstance(branch_blockers, dict):
        branch_blockers.pop(stage, None)
    agent["status"] = "pending"
    agent["blocked_reason"] = ""
    agent["heartbeat_at"] = timestamp()
    agent["events"] = [
        *agent.get("events", []),
        {
            "time": timestamp(),
            "event": "recovery_applied",
            "stage": stage,
            "decision_id": decision.decision_id,
            "actions": performed,
        },
    ][-500:]
    write_manifest(paths, manifest)
    report_path = status_dir(project_dir) / "recovery" / f"repair_{decision.decision_id}.json"
    report = {
        "kind": "recovery_application_v1",
        "decision_id": decision.decision_id,
        "evidence_sha256": decision.evidence_sha256,
        "action": decision.action,
        "stage": stage,
        "success": True,
        "performed": performed,
        "notes": notes,
        "created_at": timestamp(),
    }
    save_json(report_path, report)
    append_agent_event(
        project_dir,
        event_type="recovery_applied",
        status="passed",
        summary="；".join(notes),
        job_id=decision.job_id,
        stage=stage,
        attempt_id=decision.attempt_id,
        blocker_category=decision.category,
        recovery_decision=decision.action,
        input_evidence_hashes={"recovery_evidence": decision.evidence_sha256},
        output_evidence_hashes={"repair_report": file_sha256(report_path)},
        metadata={"actions": performed},
    )
    return RepairResult(True, stage, tuple(performed), "；".join(notes), str(report_path))
