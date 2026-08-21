from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.request
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


EVENT_LOG_NAME = "agent_events.ndjson"
NOTIFICATION_LOG_NAME = "notifications.ndjson"
CODEX_NOTIFICATION_LOG_NAME = "codex_notifications.ndjson"
RECOVERY_LOG_NAME = "recovery_decisions.ndjson"
SUPERVISOR_STATE_NAME = "story_agent_supervisor_state.json"

NOTIFICATION_SEVERITIES = frozenset({"info", "warning", "error", "critical"})
NOTIFICATION_CATEGORIES = frozenset(
    {
        "stage_completed",
        "stage_blocked",
        "stage_failed",
        "automatic_recovery",
        "waiting_for_user",
        "heartbeat_timeout",
        "budget_risk",
        "state_reconciled",
        "supervisor",
        "terminal_bug",
    }
)

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|bearer|token|secret|password|passwd|webhook)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_SECRET_TOKEN = re.compile(
    r"(?i)\b(?:sk|rk|pk|key|token)[-_][A-Za-z0-9_-]{12,}\b"
)
_WEBHOOK_PATH = re.compile(r"(?i)(https?://[^\s]*?/hook/)[^?\s]+")
_SECRET_FIELD_NAMES = frozenset(
    {
        "apikey",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "refreshtoken",
        "secret",
        "sessioncookie",
        "token",
        "webhook",
        "webhookurl",
    }
)


def timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def status_dir(project_dir: Path | str) -> Path:
    return Path(project_dir).expanduser() / "99_项目状态"


def event_log_path(project_dir: Path | str) -> Path:
    return status_dir(project_dir) / EVENT_LOG_NAME


def notification_log_path(project_dir: Path | str) -> Path:
    return status_dir(project_dir) / NOTIFICATION_LOG_NAME


def codex_notification_log_path(project_dir: Path | str) -> Path:
    return status_dir(project_dir) / CODEX_NOTIFICATION_LOG_NAME


def recovery_log_path(project_dir: Path | str) -> Path:
    return status_dir(project_dir) / RECOVERY_LOG_NAME


def supervisor_state_path(project_dir: Path | str) -> Path:
    return status_dir(project_dir) / SUPERVISOR_STATE_NAME


def redact_text(value: str, *, max_length: int = 12_000) -> str:
    """Remove common credential shapes before evidence reaches logs or models."""

    redacted = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)
    redacted = _SECRET_TOKEN.sub("[REDACTED]", redacted)
    redacted = _WEBHOOK_PATH.sub(r"\1[REDACTED]", redacted)
    if len(redacted) > max_length:
        return redacted[:max_length] + "…[truncated]"
    return redacted


def safe_payload(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            normalized_key = re.sub(r"[^a-z0-9]", "", text_key.lower())
            is_secret_field = normalized_key in _SECRET_FIELD_NAMES or normalized_key.endswith(
                ("apikey", "accesstoken", "refreshtoken", "webhookurl")
            )
            result[text_key] = "[REDACTED]" if is_secret_field else safe_payload(item)
        return result
    if isinstance(value, (list, tuple, set)):
        return [safe_payload(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        safe_payload(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def append_ndjson(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Append one complete record with one O_APPEND write.

    Stage workers can finish concurrently.  A single encoded line avoids
    read/modify/write races while preserving every event as immutable evidence.
    """

    record = dict(safe_payload(payload))
    encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > 512 * 1024:
        raise ValueError("NDJSON record exceeds 512 KiB safety limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        written = os.write(descriptor, encoded)
        if written != len(encoded):
            raise OSError(f"short append: {written}/{len(encoded)}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return record


def read_ndjson(path: Path, *, limit: int = 500) -> list[dict[str, Any]]:
    if limit <= 0 or not path.is_file():
        return []
    records: deque[dict[str, Any]] = deque(maxlen=limit)
    try:
        with path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    records.append(
                        {
                            "kind": "corrupt_ndjson_record_v1",
                            "line_number": line_number,
                            "sha256": hashlib.sha256(line.encode("utf-8")).hexdigest(),
                        }
                    )
                    continue
                if isinstance(payload, dict):
                    records.append(payload)
    except OSError:
        return []
    return list(records)


def append_agent_event(
    project_dir: Path | str,
    *,
    event_type: str,
    status: str = "",
    summary: str = "",
    job_id: str = "",
    run_id: str = "",
    stage: str = "",
    attempt_id: str = "",
    input_evidence_hashes: Mapping[str, str] | None = None,
    output_evidence_hashes: Mapping[str, str] | None = None,
    blocker_category: str = "",
    recovery_decision: str = "",
    provider: str = "",
    request_id: str = "",
    receipt_id: str = "",
    cost_cny: float | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    record = {
        "kind": "agent_event_v1",
        "event_id": uuid.uuid4().hex,
        "timestamp": timestamp(),
        "job_id": job_id,
        "run_id": run_id,
        "stage": stage,
        "attempt_id": attempt_id,
        "event_type": event_type,
        "status": status,
        "summary": summary,
        "input_evidence_hashes": dict(input_evidence_hashes or {}),
        "output_evidence_hashes": dict(output_evidence_hashes or {}),
        "blocker_category": blocker_category,
        "recovery_decision": recovery_decision,
        "provider": {
            "name": provider,
            "request_id": request_id,
            "receipt_id": receipt_id,
        },
        "cost_cny": round(max(0.0, float(cost_cny)), 4) if cost_cny is not None else None,
        "metadata": dict(metadata or {}),
    }
    return append_ndjson(event_log_path(project_dir), record)


def load_agent_events(project_dir: Path | str, *, limit: int = 500) -> list[dict[str, Any]]:
    return read_ndjson(event_log_path(project_dir), limit=limit)


def _parse_timestamp(value: str) -> float | None:
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return time.mktime(time.strptime(value, pattern))
        except (ValueError, TypeError):
            continue
    return None


def age_seconds(value: str, *, current_time: float | None = None) -> float | None:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    return max(0.0, (time.time() if current_time is None else current_time) - parsed)


def reduce_notifications(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    notifications: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in records:
        kind = str(item.get("kind") or "")
        if kind == "notification_v1":
            notification_id = str(item.get("notification_id") or "")
            if not notification_id:
                continue
            notifications[notification_id] = dict(item)
            order.append(notification_id)
        elif kind == "notification_ack_v1":
            notification_id = str(item.get("notification_id") or "")
            if notification_id in notifications:
                notifications[notification_id]["acknowledged_at"] = str(item.get("acknowledged_at") or "")
                notifications[notification_id]["acknowledged_by"] = str(item.get("acknowledged_by") or "")
    return [notifications[item] for item in order if item in notifications]


class NotificationSink(Protocol):
    name: str

    def send(self, project_dir: Path, notification: Mapping[str, Any]) -> None:
        ...


class CodexInboxSink:
    name = "codex"

    def send(self, project_dir: Path, notification: Mapping[str, Any]) -> None:
        append_ndjson(codex_notification_log_path(project_dir), notification)


class DesktopNotificationSink:
    name = "desktop"

    def send(self, project_dir: Path, notification: Mapping[str, Any]) -> None:
        del project_dir
        if sys_platform() != "darwin":
            return
        title = f"Story Agent · {notification.get('category', '通知')}"
        body = str(notification.get("message") or "")[:220]
        script = "on run argv\n display notification (item 2 of argv) with title (item 1 of argv)\nend run"
        subprocess.run(
            ["/usr/bin/osascript", "-e", script, title, body],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )


class LarkWebhookNotificationSink:
    name = "lark"

    def __init__(self, *, env_name: str = "STORY_AGENT_LARK_WEBHOOK_URL") -> None:
        self.env_name = env_name

    def send(self, project_dir: Path, notification: Mapping[str, Any]) -> None:
        del project_dir
        webhook = os.environ.get(self.env_name, "").strip()
        if not webhook:
            return
        message = (
            f"[{notification.get('severity', 'info')}] {notification.get('message', '')}\n"
            f"需要操作：{notification.get('required_action') or '无'}"
        )
        request = urllib.request.Request(
            webhook,
            data=json.dumps(
                {"msg_type": "text", "content": {"text": redact_text(message)}},
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read(1024)


def sys_platform() -> str:
    import sys

    return sys.platform


def notification_sinks(names: str | Sequence[str]) -> list[NotificationSink]:
    values = names.split(",") if isinstance(names, str) else list(names)
    selected: list[NotificationSink] = []
    for raw in values:
        name = str(raw).strip().lower()
        if not name or name == "project":
            continue
        if name == "codex":
            selected.append(CodexInboxSink())
        elif name == "desktop":
            selected.append(DesktopNotificationSink())
        elif name == "lark":
            selected.append(LarkWebhookNotificationSink())
        else:
            raise ValueError(f"unknown notification sink: {name}")
    return selected


def emit_notification(
    project_dir: Path | str,
    *,
    category: str,
    severity: str,
    message: str,
    dedupe_key: str,
    required_action: str = "",
    recovery_mode: str = "",
    stage: str = "",
    job_id: str = "",
    sinks: Sequence[NotificationSink] = (),
    repeat_after_seconds: int = 3600,
) -> dict[str, Any]:
    if category not in NOTIFICATION_CATEGORIES:
        raise ValueError(f"unknown notification category: {category}")
    if severity not in NOTIFICATION_SEVERITIES:
        raise ValueError(f"unknown notification severity: {severity}")
    path = notification_log_path(project_dir)
    reduced = reduce_notifications(read_ndjson(path, limit=4000))
    latest = next((item for item in reversed(reduced) if item.get("dedupe_key") == dedupe_key), None)
    if latest and not latest.get("acknowledged_at"):
        created = _parse_timestamp(str(latest.get("created_at") or ""))
        if created is not None and time.time() - created < max(0, repeat_after_seconds):
            return {**latest, "deduplicated": True}
    record = {
        "kind": "notification_v1",
        "notification_id": uuid.uuid4().hex,
        "created_at": timestamp(),
        "acknowledged_at": "",
        "acknowledged_by": "",
        "job_id": job_id,
        "stage": stage,
        "category": category,
        "severity": severity,
        "dedupe_key": dedupe_key,
        "message": message,
        "required_action": required_action,
        "recovery_mode": recovery_mode,
    }
    stored = append_ndjson(path, record)
    root = Path(project_dir).expanduser()
    sink_errors: list[str] = []
    for sink in sinks:
        try:
            sink.send(root, stored)
        except Exception as exc:
            sink_errors.append(f"{sink.name}:{type(exc).__name__}")
    if sink_errors:
        append_agent_event(
            root,
            event_type="notification_sink_failed",
            status="warning",
            summary="、".join(sink_errors),
            stage=stage,
            job_id=job_id,
        )
    return stored


def acknowledge_notification(
    project_dir: Path | str,
    notification_id: str,
    *,
    acknowledged_by: str = "user",
) -> dict[str, Any]:
    active = {
        str(item.get("notification_id")): item
        for item in reduce_notifications(read_ndjson(notification_log_path(project_dir), limit=4000))
    }
    if notification_id not in active:
        raise ValueError(f"unknown notification_id: {notification_id}")
    record = {
        "kind": "notification_ack_v1",
        "notification_id": notification_id,
        "acknowledged_at": timestamp(),
        "acknowledged_by": acknowledged_by,
    }
    return append_ndjson(notification_log_path(project_dir), record)


def load_notifications(project_dir: Path | str, *, limit: int = 200) -> list[dict[str, Any]]:
    records = read_ndjson(notification_log_path(project_dir), limit=max(limit * 4, 500))
    return reduce_notifications(records)[-limit:]
