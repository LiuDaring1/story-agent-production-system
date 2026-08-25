#!/usr/bin/env python3
"""Minimal artifact ledger for Codex-native story production.

This module deliberately does not schedule work, invoke models, retry tasks, or
mirror the legacy Story Agent stage graph.  It records the current inputs,
coarse work-package state, current artifacts, and aggregate paid cost in one
atomic JSON file so a foreground Codex task can resume without rediscovering or
repeating completed work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "story-run-v1"
DEFAULT_SOFT_BUDGET = 50.0
DEFAULT_HARD_BUDGET = 100.0
PACKAGE_NAMES = (
    "director_plan",
    "r2v_visuals",
    "music",
    "presenter_keying",
    "product_assets",
    "delivery",
)
PACKAGE_STATES = {"pending", "running", "done", "blocked"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"{label}不存在或不是文件：{resolved}")
    if resolved.stat().st_size <= 0:
        raise ValueError(f"{label}为空：{resolved}")
    return resolved


def input_record(path: Path, label: str) -> dict[str, Any]:
    resolved = require_file(path, label)
    return {
        "path": str(resolved),
        "sha256": file_sha256(resolved),
        "bytes": resolved.stat().st_size,
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_run(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"运行账本不存在：{resolved}")
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"不支持的运行账本版本：{payload.get('schema_version')!r}")
    validate_run(payload)
    return payload


def validate_run(payload: dict[str, Any]) -> None:
    if not isinstance(payload.get("inputs"), dict):
        raise ValueError("story_run.json 缺少 inputs")
    packages = payload.get("work_packages")
    if not isinstance(packages, dict) or set(packages) != set(PACKAGE_NAMES):
        raise ValueError("story_run.json 的六个工作包不完整")
    for name, record in packages.items():
        if not isinstance(record, dict) or record.get("status") not in PACKAGE_STATES:
            raise ValueError(f"工作包状态无效：{name}")
    if not isinstance(payload.get("artifacts"), dict):
        raise ValueError("story_run.json 缺少 artifacts")
    paid_total = payload.get("paid_total")
    if not isinstance(paid_total, (int, float)) or paid_total < 0:
        raise ValueError("story_run.json 的 paid_total 无效")


def init_run(
    *,
    run_file: Path,
    confirmed_text: Path,
    greenscreen_video: Path,
    audio: Path,
    project_dir: Path,
    soft_budget: float = DEFAULT_SOFT_BUDGET,
    hard_budget: float = DEFAULT_HARD_BUDGET,
) -> dict[str, Any]:
    target = run_file.expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"运行账本已存在，禁止覆盖：{target}")
    if soft_budget < 0 or hard_budget <= 0 or soft_budget > hard_budget:
        raise ValueError("预算必须满足 0 <= soft_budget <= hard_budget")
    project = project_dir.expanduser().resolve()
    project.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": target.parent.parent.name or project.name,
        "project_dir": str(project),
        "created_at": now,
        "updated_at": now,
        "finalized_at": "",
        "inputs": {
            "confirmed_text": input_record(confirmed_text, "确认文本"),
            "greenscreen_video": input_record(greenscreen_video, "绿幕视频"),
            "audio": input_record(audio, "权威音频"),
        },
        "work_packages": {
            name: {"status": "pending", "blocker": ""} for name in PACKAGE_NAMES
        },
        "artifacts": {},
        "paid_total": 0.0,
        "budget": {"soft": float(soft_budget), "hard": float(hard_budget)},
        "blocker": "",
    }
    atomic_write_json(target, payload)
    return payload


def parse_input_hashes(values: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"--input-hash 必须是 name=sha256：{value!r}")
        name, digest = value.split("=", 1)
        name, digest = name.strip(), digest.strip().lower()
        if not name or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError(f"--input-hash 无效：{value!r}")
        result[name] = digest
    return result


def record_run(
    *,
    run_file: Path,
    package: str,
    status: str,
    artifact_id: str = "",
    artifact_path: Path | None = None,
    input_hashes: dict[str, str] | None = None,
    provider_task_id: str = "",
    paid_amount: float = 0.0,
    blocker: str = "",
    replace: bool = False,
) -> dict[str, Any]:
    if package not in PACKAGE_NAMES:
        raise ValueError(f"未知工作包：{package}")
    if status not in PACKAGE_STATES:
        raise ValueError(f"未知工作包状态：{status}")
    if paid_amount < 0:
        raise ValueError("--paid-amount 不能为负数")
    if status == "blocked" and not blocker.strip():
        raise ValueError("blocked 状态必须提供 --blocker")
    if artifact_id and artifact_path is None:
        raise ValueError("提供 --artifact-id 时必须同时提供 --path")
    if artifact_path is not None and not artifact_id.strip():
        raise ValueError("提供 --path 时必须同时提供 --artifact-id")

    target = run_file.expanduser().resolve()
    payload = load_run(target)
    new_paid_total = round(float(payload["paid_total"]) + float(paid_amount), 4)
    hard = float(payload["budget"]["hard"])
    if new_paid_total > hard:
        raise RuntimeError(
            f"登记后将超过硬预算：{new_paid_total:.2f} > {hard:.2f}；禁止开始新的付费工作"
        )

    if artifact_id:
        artifact = require_file(artifact_path or Path(), "产物")
        digest = file_sha256(artifact)
        existing = payload["artifacts"].get(artifact_id)
        if existing and existing.get("sha256") != digest and not replace:
            raise RuntimeError(
                f"产物 {artifact_id!r} 已登记为不同哈希；显式使用 --replace 才能更新当前有效版本"
            )
        payload["artifacts"][artifact_id] = {
            "package": package,
            "path": str(artifact),
            "sha256": digest,
            "bytes": artifact.stat().st_size,
            "input_sha256s": dict(sorted((input_hashes or {}).items())),
            "provider_task_id": provider_task_id.strip(),
            "recorded_at": utc_now(),
        }

    payload["work_packages"][package] = {
        "status": status,
        "blocker": blocker.strip() if status == "blocked" else "",
    }
    payload["paid_total"] = new_paid_total
    blocked = [
        value["blocker"]
        for value in payload["work_packages"].values()
        if value["status"] == "blocked" and value["blocker"]
    ]
    payload["blocker"] = "；".join(blocked)
    payload["updated_at"] = utc_now()
    payload["finalized_at"] = ""
    atomic_write_json(target, payload)
    return payload


def status_summary(payload: dict[str, Any]) -> dict[str, Any]:
    hard = float(payload["budget"]["hard"])
    soft = float(payload["budget"]["soft"])
    paid = float(payload["paid_total"])
    return {
        "schema_version": payload["schema_version"],
        "run_id": payload["run_id"],
        "project_dir": payload["project_dir"],
        "work_packages": payload["work_packages"],
        "artifact_ids": sorted(payload["artifacts"]),
        "paid_total": paid,
        "soft_budget_warning": paid >= soft,
        "remaining_hard_budget": round(max(0.0, hard - paid), 4),
        "can_start_paid_work": paid < hard,
        "blocker": payload.get("blocker", ""),
        "finalized": bool(payload.get("finalized_at")),
    }


def finalize_run(*, run_file: Path, required_artifacts: Iterable[str]) -> dict[str, Any]:
    target = run_file.expanduser().resolve()
    payload = load_run(target)
    incomplete = [
        name for name, value in payload["work_packages"].items() if value["status"] != "done"
    ]
    if incomplete:
        raise RuntimeError(f"仍有未完成工作包：{', '.join(incomplete)}")
    missing = [name for name in required_artifacts if name not in payload["artifacts"]]
    if missing:
        raise RuntimeError(f"缺少必需产物：{', '.join(missing)}")
    stale: list[str] = []
    for artifact_id, record in payload["artifacts"].items():
        path = Path(record["path"])
        if not path.is_file() or file_sha256(path) != record["sha256"]:
            stale.append(artifact_id)
    if stale:
        raise RuntimeError(f"产物缺失或哈希漂移：{', '.join(stale)}")
    payload["finalized_at"] = utc_now()
    payload["updated_at"] = payload["finalized_at"]
    payload["blocker"] = ""
    atomic_write_json(target, payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex 原生故事生产的极简产物账本")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="用确认文本、调色绿幕视频和权威音频创建账本")
    init.add_argument("--run-file", required=True, type=Path)
    init.add_argument("--project-dir", required=True, type=Path)
    init.add_argument("--text", required=True, type=Path)
    init.add_argument("--video", required=True, type=Path)
    init.add_argument("--audio", required=True, type=Path)
    init.add_argument("--soft-budget", type=float, default=DEFAULT_SOFT_BUDGET)
    init.add_argument("--hard-budget", type=float, default=DEFAULT_HARD_BUDGET)

    record = subparsers.add_parser("record", help="登记一个工作包状态和可选的当前有效产物")
    record.add_argument("--run-file", required=True, type=Path)
    record.add_argument("--package", required=True, choices=PACKAGE_NAMES)
    record.add_argument("--status", required=True, choices=sorted(PACKAGE_STATES))
    record.add_argument("--artifact-id", default="")
    record.add_argument("--path", type=Path)
    record.add_argument("--input-hash", action="append", default=[])
    record.add_argument("--provider-task-id", default="")
    record.add_argument("--paid-amount", type=float, default=0.0)
    record.add_argument("--blocker", default="")
    record.add_argument("--replace", action="store_true")

    status = subparsers.add_parser("status", help="输出六个工作包和预算的简洁状态")
    status.add_argument("--run-file", required=True, type=Path)

    finalize = subparsers.add_parser("finalize", help="校验所有工作包、产物文件和哈希后锁定交付")
    finalize.add_argument("--run-file", required=True, type=Path)
    finalize.add_argument("--require", action="append", default=[])
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "init":
        payload = init_run(
            run_file=args.run_file,
            confirmed_text=args.text,
            greenscreen_video=args.video,
            audio=args.audio,
            project_dir=args.project_dir,
            soft_budget=args.soft_budget,
            hard_budget=args.hard_budget,
        )
    elif args.command == "record":
        payload = record_run(
            run_file=args.run_file,
            package=args.package,
            status=args.status,
            artifact_id=args.artifact_id,
            artifact_path=args.path,
            input_hashes=parse_input_hashes(args.input_hash),
            provider_task_id=args.provider_task_id,
            paid_amount=args.paid_amount,
            blocker=args.blocker,
            replace=args.replace,
        )
    elif args.command == "status":
        payload = status_summary(load_run(args.run_file))
    else:
        payload = finalize_run(run_file=args.run_file, required_artifacts=args.require)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
