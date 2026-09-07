#!/usr/bin/env python3
"""Minimal artifact ledger for Codex-native story production.

This module deliberately does not schedule work, invoke models, retry tasks, or
mirror the legacy Story Agent stage graph.  It records the current inputs,
coarse work-package state, current artifacts, and lightweight request facts in one
atomic JSON file so a foreground Codex task can resume without rediscovering or
repeating completed work.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from shot_storyboard_pipeline import validate_compile_receipt
from semantic_card_motion import (
    semantic_card_generation_receipt_issues,
    semantic_card_motion_receipt_issues,
)
from static_ppt_contract import validate_delivery_receipt
from story_artifact_validation import validate_artifact_semantics


SCHEMA_VERSION = "story-run-v1"
THEME_ASSETS_SCHEMA_VERSION = "story-theme-assets-lightweight/v3"
LEGACY_THEME_ASSETS_SCHEMA_VERSION = "story-theme-assets-lightweight/v2"
PACKAGE_NAMES = (
    "director_plan",
    "r2v_visuals",
    "music",
    "presenter_keying",
    "product_assets",
    "delivery",
)
PACKAGE_STATES = {"pending", "running", "done", "blocked"}
REQUEST_STATES = {"submitted", "running", "completed", "failed", "cancelled"}
TOKEN_STATES = {"reported", "not_reported", "not_applicable"}
PERFORMANCE_OBSERVATION_FIELDS = (
    "plan_duration_seconds",
    "machine_check_duration_seconds",
    "independent_review_rounds",
    "invalid_rejection_count",
    "duplicate_encode_count",
)
_RUN_THREAD_LOCKS: dict[str, threading.RLock] = {}
_RUN_THREAD_LOCKS_GUARD = threading.Lock()
CODEX_NATIVE_REQUIRED_ARTIFACTS = (
    "master_director_plan",
    "director_plan_review",
    "authoritative_timeline_receipt",
    "storyboard_manifest_sealed",
    "storyboard_review",
    "shot_storyboard_compile_receipt",
    "semantic_card_generation_receipt",
    "semantic_card_motion_receipt",
    "r2v_provider_group_receipt",
    "r2v_group_machine_qa",
    "r2v_group_visual_review",
    "qa_music_report",
    "keying_preset_lock",
    "keying_visual_review",
    "theme_assets_manifest",
    "static_ppt_delivery_receipt",
    "customer_media_receipt",
    "qa_product_report",
    "qa_publish_report",
    "release_package_receipt",
    "main_release_video",
    "library_release_video",
    "qa_release_report",
    "final_delivery_checklist",
    "final_delivery_review",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def run_file_lock(path: Path) -> Iterator[None]:
    """Serialize every read-modify-write transaction for one run ledger."""

    target = path.expanduser().resolve()
    lock_path = target.with_name(f".{target.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = str(lock_path)
    with _RUN_THREAD_LOCKS_GUARD:
        thread_lock = _RUN_THREAD_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_nonnegative_number(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}必须是非负数或 null")
    number = float(value)
    if number < 0:
        raise ValueError(f"{label}必须是非负数或 null")
    return number


def _new_package_observation(*, history_complete: bool) -> dict[str, Any]:
    return {
        "started_at": None,
        "finished_at": None,
        "active_seconds": None,
        "wait_seconds": None,
        "retry_count": 0 if history_complete else None,
        "history_complete": history_complete,
        "first_observed_at": None,
        "last_observed_at": None,
    }


def _new_performance_observation() -> dict[str, Any]:
    return {
        "schema_version": "story-performance-observation/v1",
        **{name: None for name in PERFORMANCE_OBSERVATION_FIELDS},
        "duplicate_provider_request_count": 0,
        "longest_dependency_chain": 0,
        "missing_measurements": list(PERFORMANCE_OBSERVATION_FIELDS),
    }


def _longest_dependency_chain(payload: dict[str, Any]) -> int:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        return 0
    memo: dict[str, int] = {}

    def depth(artifact_id: str, visiting: set[str]) -> int:
        if artifact_id in memo:
            return memo[artifact_id]
        if artifact_id in visiting:
            return 0
        record = artifacts.get(artifact_id)
        bindings = record.get("input_sha256s") if isinstance(record, dict) else None
        upstream = [
            str(name) for name in bindings or {}
            if str(name) in artifacts
        ] if isinstance(bindings, dict) else []
        value = 1 + max((depth(name, visiting | {artifact_id}) for name in upstream), default=0)
        memo[artifact_id] = value
        return value

    return max(depth(str(name), set()) for name in artifacts)


def ensure_observability(payload: dict[str, Any], *, new_run: bool = False) -> dict[str, Any]:
    """Add the native request/event ledger without inventing historical values."""

    existing = payload.get("observability")
    migrated = not isinstance(existing, dict)
    observability = payload.setdefault("observability", {})
    observability.setdefault("schema_version", "story-run-observability/v1")
    observability.setdefault("history_complete", bool(new_run) if migrated else False)
    observability.setdefault("events", [])
    observability.setdefault("requests", {})
    performance = observability.setdefault("performance", _new_performance_observation())
    if not isinstance(performance, dict):
        observability["performance"] = _new_performance_observation()
    else:
        defaults = _new_performance_observation()
        for name, value in defaults.items():
            performance.setdefault(name, value)
    package_rows = observability.setdefault("packages", {})
    history_complete = bool(observability.get("history_complete"))
    for package in PACKAGE_NAMES:
        package_rows.setdefault(
            package,
            _new_package_observation(history_complete=history_complete),
        )
    refresh_observability_summary(payload)
    return observability


def refresh_observability_summary(payload: dict[str, Any]) -> None:
    observability = payload.get("observability")
    if not isinstance(observability, dict):
        return
    requests = observability.get("requests")
    if not isinstance(requests, dict):
        requests = {}
        observability["requests"] = requests

    token_applicable = 0
    unknown_input = 0
    unknown_output = 0
    unknown_total = 0
    known_input = 0
    known_output = 0
    known_total = 0
    for item in requests.values():
        if not isinstance(item, dict):
            continue
        token_state = str(item.get("token_status") or "not_reported")
        if token_state == "not_applicable":
            continue
        token_applicable += 1
        if token_state != "reported":
            unknown_input += 1
            unknown_output += 1
            unknown_total += 1
            continue
        if item.get("input_tokens") is None:
            unknown_input += 1
        else:
            known_input += int(item["input_tokens"])
        if item.get("output_tokens") is None:
            unknown_output += 1
        else:
            known_output += int(item["output_tokens"])
        if item.get("total_tokens") is None:
            unknown_total += 1
        else:
            known_total += int(item["total_tokens"])
    if not token_applicable:
        token_summary = {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "known_input_tokens": 0,
            "known_output_tokens": 0,
            "known_total_tokens": 0,
            "unreported_request_count": 0,
            "status": "not_applicable",
        }
    else:
        token_summary = {
            "input_tokens": None if unknown_input else known_input,
            "output_tokens": None if unknown_output else known_output,
            "total_tokens": None if unknown_total else known_total,
            "known_input_tokens": known_input,
            "known_output_tokens": known_output,
            "known_total_tokens": known_total,
            "unreported_request_count": max(unknown_input, unknown_output, unknown_total),
            "status": "partial_unreported" if any((unknown_input, unknown_output, unknown_total)) else "reported",
        }
    observability["token_summary"] = token_summary

    performance = observability["performance"]
    fingerprints: dict[tuple[str, str], set[str]] = {}
    for item in requests.values():
        if not isinstance(item, dict) or not item.get("request_sha256"):
            continue
        key = (str(item.get("provider") or ""), str(item["request_sha256"]))
        fingerprints.setdefault(key, set()).add(str(item.get("request_id") or ""))
    performance["duplicate_provider_request_count"] = sum(
        max(0, len(request_ids) - 1) for request_ids in fingerprints.values()
    )
    performance["longest_dependency_chain"] = _longest_dependency_chain(payload)
    performance["missing_measurements"] = [
        name for name in PERFORMANCE_OBSERVATION_FIELDS
        if performance.get(name) is None
    ]

    package_rows = observability.get("packages")
    if not isinstance(package_rows, dict):
        return
    for package in PACKAGE_NAMES:
        metrics = package_rows.setdefault(
            package,
            _new_package_observation(
                history_complete=bool(observability.get("history_complete"))
            ),
        )
        package_requests = [
            item
            for item in requests.values()
            if isinstance(item, dict) and item.get("package") == package
        ]
        if not package_requests:
            continue
        starts = sorted(
            str(item["started_at"])
            for item in package_requests
            if item.get("started_at")
        )
        ends = sorted(
            str(item["ended_at"])
            for item in package_requests
            if item.get("ended_at")
        )
        if starts:
            metrics["started_at"] = starts[0]
        if ends:
            metrics["finished_at"] = ends[-1]
        known_active = sum(
            float(item["duration_seconds"])
            for item in package_requests
            if item.get("duration_seconds") is not None
        )
        unknown_active = sum(
            1 for item in package_requests if item.get("duration_seconds") is None
        )
        known_wait = sum(
            float(item["wait_seconds"])
            for item in package_requests
            if item.get("wait_seconds") is not None
        )
        unknown_wait = sum(
            1 for item in package_requests if item.get("wait_seconds") is None
        )
        known_retries = sum(
            int(item["retry_index"])
            for item in package_requests
            if item.get("retry_index") is not None
        )
        unknown_retries = sum(
            1 for item in package_requests if item.get("retry_index") is None
        )
        metrics.update(
            {
                "active_seconds": None if unknown_active else round(known_active, 3),
                "known_active_seconds": round(known_active, 3),
                "active_unreported_request_count": unknown_active,
                "wait_seconds": None if unknown_wait else round(known_wait, 3),
                "known_wait_seconds": round(known_wait, 3),
                "wait_unreported_request_count": unknown_wait,
                "retry_count": None if unknown_retries else known_retries,
                "known_retry_count": known_retries,
                "retry_unreported_request_count": unknown_retries,
            }
        )


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


def discover_subtitle_txt(project_dir: Path) -> Path:
    """Find exactly one user-authored subtitle TXT without inventing a stream."""

    project = project_dir.expanduser().resolve()
    roots = [project / "00_输入素材", project]
    candidates: list[Path] = []
    for root in roots:
        if root.is_dir():
            candidates.extend(path.resolve() for path in root.glob("*.txt") if path.is_file())
    candidates = sorted(
        path
        for path in set(candidates)
        if path.stem.lower() not in {"story_source", "confirmed_text", "script_lines"}
        and not any(token in path.stem for token in ("故事原文", "确认文本", "逐行台词"))
    )
    named = [
        path
        for path in candidates
        if any(token in path.stem.lower() for token in ("字幕", "subtitle"))
    ]
    selected = named if named else candidates
    if len(selected) != 1:
        if not selected:
            raise FileNotFoundError("项目缺少用户确认、已换好行的字幕 TXT；禁止系统另做一套字幕")
        raise ValueError(
            "项目内字幕 TXT 不唯一，请用 --subtitle-txt 明确指定："
            + "、".join(str(path) for path in selected)
        )
    return selected[0]


def _validate_hashed_raster(item: Any, label: str, *, method_key: str) -> Path:
    if not isinstance(item, dict):
        raise ValueError(f"发布美术清单缺少 {label}")
    source = require_file(Path(str(item.get("path") or "")), f"发布美术 {label}")
    if source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError(f"发布美术必须是栅格图：{label}")
    if item.get("sha256") != file_sha256(source):
        raise ValueError(f"发布美术哈希不匹配：{label}")
    if not str(item.get(method_key) or "").strip():
        raise ValueError(f"发布美术缺少生成/派生方法：{label}")
    return source


def _validate_theme_assets_v2(payload: dict[str, Any]) -> None:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("发布美术清单缺少 artifacts")
    forbidden_vector = [
        name
        for name, item in artifacts.items()
        if "svg" in str(name).lower()
        or (isinstance(item, dict) and Path(str(item.get("path") or "")).suffix.lower() == ".svg")
        or (
            isinstance(item, dict)
            and any("svg" in str(parent).lower() for parent in item.get("derived_from", []))
        )
    ]
    if forbidden_vector:
        raise ValueError("发布美术禁止 SVG/矢量派生：" + "、".join(sorted(forbidden_vector)))
    required = {
        "main_background_16x9": {"imagegen_raster", "imagegen_reference_edit"},
        "story_frame_source": {"imagegen_raster", "imagegen_reference_edit"},
    }
    for name, methods in required.items():
        item = artifacts.get(name)
        if not isinstance(item, dict) or item.get("generation_method") not in methods:
            raise ValueError(f"发布美术生成方法无效：{name}")
        _validate_hashed_raster(item, name, method_key="generation_method")
    frame = artifacts.get("story_frame_png")
    if not isinstance(frame, dict):
        raise ValueError("发布美术清单缺少 story_frame_png")
    if frame.get("transparency_method") not in {"imagegen_native_alpha", "raster_alpha_postprocess"}:
        raise ValueError("故事框透明方式无效")
    if frame.get("has_true_alpha") is not True or frame.get("derived_from") != ["story_frame_source"]:
        raise ValueError("故事框必须由 ImageGen 栅格源图派生并具有真实 Alpha")
    frame_path = require_file(Path(str(frame.get("path") or "")), "透明故事框")
    if frame_path.suffix.lower() != ".png" or frame.get("sha256") != file_sha256(frame_path):
        raise ValueError("透明故事框路径或哈希无效")


def _validate_theme_assets_v3(payload: dict[str, Any]) -> None:
    if payload.get("svg_used") is not False:
        raise ValueError("v3 发布美术必须明确 svg_used=false")
    reference = payload.get("frame_reference")
    if not isinstance(reference, dict):
        raise ValueError("v3 发布美术缺少正式故事框几何参考")
    reference_path = require_file(Path(str(reference.get("path") or "")), "故事框几何参考")
    if reference_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError("故事框几何参考必须是栅格图")
    if reference.get("sha256") != file_sha256(reference_path):
        raise ValueError("故事框几何参考哈希不匹配")
    if reference.get("role") != "geometry_only_not_theme_or_ornament":
        raise ValueError("故事框参考只能约束几何，不能继承主题或装饰")
    required_locks = {
        "screen_placement",
        "large_16_9_aperture",
        "continuous_practical_border_thickness",
    }
    if not required_locks.issubset(set(reference.get("locked_properties") or [])):
        raise ValueError("故事框几何参考未锁定位置、16:9 开口和实用厚度")

    sources = payload.get("sources")
    artifacts = payload.get("artifacts")
    if not isinstance(sources, dict) or not isinstance(artifacts, dict):
        raise ValueError("v3 发布美术缺少 sources 或 artifacts")
    vector_lineage = []
    for collection_name, collection in (("sources", sources), ("artifacts", artifacts)):
        for name, item in collection.items():
            if not isinstance(item, dict):
                continue
            values = [name, item.get("path"), *(item.get("derived_from") or [])]
            if any("svg" in str(value).lower() for value in values):
                vector_lineage.append(f"{collection_name}.{name}")
    if vector_lineage:
        raise ValueError("v3 发布美术禁止 SVG/矢量派生：" + "、".join(vector_lineage))
    environment = sources.get("environment")
    if not isinstance(environment, dict):
        raise ValueError("v3 发布美术缺少环境源图")
    environment_path = require_file(Path(str(environment.get("path") or "")), "环境源图")
    if environment.get("sha256") != file_sha256(environment_path):
        raise ValueError("环境源图哈希不匹配")

    frame_source = sources.get("story_frame_magenta")
    _validate_hashed_raster(frame_source, "story_frame_magenta", method_key="method")
    if frame_source.get("method") not in {"imagegen_raster", "imagegen_reference_edit"}:
        raise ValueError("洋红故事框源图必须由 ImageGen 栅格生成或参考编辑")
    if str(frame_source.get("background") or "").upper() != "#FF00FF":
        raise ValueError("故事框源图内外必须使用纯 #FF00FF 洋红")
    if frame_source.get("checkerboard") is not False:
        raise ValueError("故事框源图禁止棋盘格")

    background = artifacts.get("main_background_16x9")
    _validate_hashed_raster(background, "main_background_16x9", method_key="method")
    if background.get("method") not in {"imagegen_raster", "imagegen_reference_edit"}:
        raise ValueError("发布背景必须由 ImageGen 栅格生成或参考编辑")
    frame = artifacts.get("story_frame_png")
    frame_path = _validate_hashed_raster(frame, "story_frame_png", method_key="method")
    if frame_path.suffix.lower() != ".png":
        raise ValueError("透明故事框必须是 PNG")
    if frame.get("method") != "connected_magenta_raster_alpha_postprocess":
        raise ValueError("透明故事框必须由洋红栅格源图做固定 Alpha 后处理")
    if frame.get("derived_from") != ["story_frame_magenta"] or frame.get("has_true_alpha") is not True:
        raise ValueError("透明故事框必须绑定同一洋红源图并具有真实 Alpha")

    frame_review = payload.get("frame_design_review")
    required_frame_checks = (
        "passed",
        "current_story_redesign",
        "no_reference_theme_leak",
        "solid_magenta_source",
        "continuous_opaque_four_sides",
        "inner_masking_lip",
    )
    if not isinstance(frame_review, dict) or any(frame_review.get(key) is not True for key in required_frame_checks):
        raise ValueError("故事框设计审核未证明当前故事重设计、无主题泄漏且四边连续可遮挡")
    require_file(Path(str(frame_review.get("evidence") or "")), "故事框设计审核证据")

    clean_review = payload.get("background_clean_review")
    required_background_checks = (
        "passed",
        "not_preblurred",
        "controlled_high_frequency_detail",
        "no_text_logo_or_vignette",
    )
    if not isinstance(clean_review, dict) or any(clean_review.get(key) is not True for key in required_background_checks):
        raise ValueError("背景清洁审核未证明清晰、控噪且无文字/Logo/暗角")

    a_only = payload.get("a_only_video_qa")
    if a_only is not None:
        if not isinstance(a_only, dict):
            raise ValueError("A 镜无人物视频 QA 格式无效")
        video = require_file(Path(str(a_only.get("path") or "")), "A 镜无人物视频")
        if a_only.get("sha256") != file_sha256(video):
            raise ValueError("A 镜无人物视频 QA 哈希不匹配")
        require_file(Path(str(a_only.get("sample_sheet") or "")), "A 镜无人物视频抽样证据")


def validate_theme_assets_manifest(path: Path, *, require_v3: bool = False) -> dict[str, Any]:
    """Validate raster ImageGen lineage for Codex-native release art."""

    manifest = require_file(path, "发布美术清单")
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"发布美术清单不是有效 JSON：{manifest}") from exc
    schema = payload.get("schema_version")
    supported = {LEGACY_THEME_ASSETS_SCHEMA_VERSION, THEME_ASSETS_SCHEMA_VERSION}
    if schema not in supported or (require_v3 and schema != THEME_ASSETS_SCHEMA_VERSION):
        expected = THEME_ASSETS_SCHEMA_VERSION if require_v3 else "v2 或 v3"
        raise ValueError(f"发布美术清单版本无效，当前要求 {expected}")
    if not str(payload.get("story_name") or "").strip():
        raise ValueError("发布美术清单缺少 story_name")
    if schema == THEME_ASSETS_SCHEMA_VERSION:
        _validate_theme_assets_v3(payload)
    else:
        _validate_theme_assets_v2(payload)
    return payload


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
    ensure_observability(payload)
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
    observability = payload.get("observability")
    if not isinstance(observability, dict):
        raise ValueError("story_run.json 缺少 observability")
    if observability.get("schema_version") != "story-run-observability/v1":
        raise ValueError("story_run.json 的 observability 版本无效")
    if not isinstance(observability.get("events"), list):
        raise ValueError("story_run.json 的 observability.events 无效")
    requests = observability.get("requests")
    if not isinstance(requests, dict):
        raise ValueError("story_run.json 的 observability.requests 无效")
    for request_key, request in requests.items():
        if not isinstance(request, dict):
            raise ValueError(f"请求观测记录无效：{request_key}")
        if request.get("status") not in REQUEST_STATES:
            raise ValueError(f"请求状态无效：{request_key}")
        if request.get("token_status") not in TOKEN_STATES:
            raise ValueError(f"请求 Token 状态无效：{request_key}")
    performance = observability.get("performance")
    if not isinstance(performance, dict) or performance.get("schema_version") != "story-performance-observation/v1":
        raise ValueError("story_run.json 的 performance 观测无效")


def init_run(
    *,
    run_file: Path,
    confirmed_text: Path,
    greenscreen_video: Path,
    audio: Path,
    project_dir: Path,
    subtitle_txt: Path | None = None,
    soft_budget: float | None = None,
    hard_budget: float | None = None,
) -> dict[str, Any]:
    target = run_file.expanduser().resolve()
    project = project_dir.expanduser().resolve()
    project.mkdir(parents=True, exist_ok=True)
    authoritative_subtitle = (
        require_file(subtitle_txt, "确认字幕 TXT")
        if subtitle_txt is not None
        else discover_subtitle_txt(project)
    )
    if authoritative_subtitle.suffix.lower() != ".txt":
        raise ValueError("确认字幕必须是 TXT 文件")
    now = utc_now()
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": target.parent.parent.name or project.name,
        "project_dir": str(project),
        "created_at": now,
        "updated_at": now,
        "finalized_at": "",
        "requirements_policy": "story-applicable-requirements/v1",
        "inputs": {
            "confirmed_text": input_record(confirmed_text, "确认文本"),
            "greenscreen_video": input_record(greenscreen_video, "绿幕视频"),
            "audio": input_record(audio, "权威音频"),
            "subtitle_txt": input_record(authoritative_subtitle, "确认字幕 TXT"),
        },
        "work_packages": {
            name: {"status": "pending", "blocker": ""} for name in PACKAGE_NAMES
        },
        "artifacts": {},
        "blocker": "",
    }
    ensure_observability(payload, new_run=True)
    with run_file_lock(target):
        if target.exists():
            raise FileExistsError(f"运行账本已存在，禁止覆盖：{target}")
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


def _record_run_unlocked(
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
    if status == "blocked" and not blocker.strip():
        raise ValueError("blocked 状态必须提供 --blocker")
    if artifact_id and artifact_path is None:
        raise ValueError("提供 --artifact-id 时必须同时提供 --path")
    if artifact_path is not None and not artifact_id.strip():
        raise ValueError("提供 --path 时必须同时提供 --artifact-id")

    target = run_file.expanduser().resolve()
    payload = load_run(target)
    observability = ensure_observability(payload)

    if artifact_id:
        artifact = require_file(artifact_path or Path(), "产物")
        if artifact_id == "theme_assets_manifest":
            validate_theme_assets_manifest(artifact)
        if artifact_id == "shot_storyboard_compile_receipt":
            # Provider execution is allowed to append status/result metadata to
            # the jobs CSV after compilation.  Recording the sealed compiler
            # receipt therefore revalidates only its immutable bindings; the
            # live job results are proven by the downstream provider receipt.
            validate_compile_receipt(
                artifact,
                require_current_r2v_jobs=False,
            )
        if artifact_id == "static_ppt_delivery_receipt":
            validate_delivery_receipt(artifact)
        if artifact_id == "semantic_card_generation_receipt":
            issues = semantic_card_generation_receipt_issues(artifact)
            if issues:
                raise ValueError("ImageGen 片头/寓意卡回执未通过：" + "；".join(issues))
        if artifact_id == "semantic_card_motion_receipt":
            request_path = artifact.parent / "semantic_card_motion_request.json"
            issues = semantic_card_motion_receipt_issues(request_path, artifact)
            if issues:
                raise ValueError("片头/寓意卡 API 微动回执未通过：" + "；".join(issues))
        validate_artifact_semantics(
            artifact_id,
            artifact,
            registered_artifacts=payload["artifacts"],
            registered_inputs=payload["inputs"],
        )
        if artifact_id == "requirements_projection":
            # The projection itself declares the minimal applicable dependency
            # set. Persist it so status invalidates only these input roles.
            projection_inputs = json.loads(artifact.read_text(encoding="utf-8"))["inputs"]
            input_hashes = {**(input_hashes or {}), **{
                role: item["sha256"] for role, item in projection_inputs.items()
            }}
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

    previous_status = str(payload["work_packages"][package].get("status") or "")
    observed_at = utc_now()
    payload["work_packages"][package] = {
        "status": status,
        "blocker": blocker.strip() if status == "blocked" else "",
    }
    package_observation = observability["packages"][package]
    package_observation["first_observed_at"] = (
        package_observation.get("first_observed_at") or observed_at
    )
    package_observation["last_observed_at"] = observed_at
    if status == "running" and not package_observation.get("started_at"):
        if package_observation.get("history_complete"):
            package_observation["started_at"] = observed_at
    if status == "done":
        package_observation["finished_at"] = observed_at
    observability["events"].append(
        {
            "sequence": len(observability["events"]) + 1,
            "observed_at": observed_at,
            "event": "package_status",
            "package": package,
            "previous_status": previous_status,
            "status": status,
            "artifact_id": artifact_id.strip() or None,
            "legacy_financial_evidence": (
                {"paid_amount": float(paid_amount), "currency": "CNY", "deprecated": True}
                if paid_amount
                else None
            ),
        }
    )
    refresh_observability_summary(payload)
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
    target = run_file.expanduser().resolve()
    with run_file_lock(target):
        return _record_run_unlocked(
            run_file=target,
            package=package,
            status=status,
            artifact_id=artifact_id,
            artifact_path=artifact_path,
            input_hashes=input_hashes,
            provider_task_id=provider_task_id,
            paid_amount=paid_amount,
            blocker=blocker,
            replace=replace,
        )


def _record_request_observation_unlocked(
    *,
    run_file: Path,
    package: str,
    provider: str,
    request_id: str,
    operation: str,
    status: str,
    model: str = "",
    started_at: str = "",
    ended_at: str = "",
    duration_seconds: float | None = None,
    wait_seconds: float | None = None,
    retry_index: int | None = None,
    token_status: str = "not_reported",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    request_sha256: str = "",
    error_type: str = "",
    estimated_cost: float | None = None,
    actual_cost: float | None = None,
    currency: str = "CNY",
    cost_status: str = "provider_not_exposed",
    replace: bool = False,
) -> dict[str, Any]:
    if package not in PACKAGE_NAMES:
        raise ValueError(f"未知工作包：{package}")
    provider = provider.strip()
    request_id = request_id.strip()
    operation = operation.strip()
    if not provider or not request_id or not operation:
        raise ValueError("请求观测必须提供 provider、request_id 和 operation")
    if status not in REQUEST_STATES:
        raise ValueError(f"请求状态无效：{status}")
    if token_status not in TOKEN_STATES:
        raise ValueError(f"Token 状态无效：{token_status}")
    duration_seconds = _optional_nonnegative_number(duration_seconds, "duration_seconds")
    wait_seconds = _optional_nonnegative_number(wait_seconds, "wait_seconds")
    estimated_cost = _optional_nonnegative_number(estimated_cost, "estimated_cost")
    actual_cost = _optional_nonnegative_number(actual_cost, "actual_cost")
    if retry_index is not None and (isinstance(retry_index, bool) or retry_index < 0):
        raise ValueError("retry_index 必须是非负整数或 null")
    token_values = (input_tokens, output_tokens, total_tokens)
    if any(
        value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
        for value in token_values
    ):
        raise ValueError("Token 用量必须是非负整数或 null")
    if token_status == "reported":
        if total_tokens is None:
            if input_tokens is None or output_tokens is None:
                raise ValueError("token_status=reported 时必须提供 total_tokens 或完整输入/输出 Token")
            total_tokens = input_tokens + output_tokens
    elif any(value is not None for value in token_values):
        raise ValueError("未报告/不适用 Token 不得写成 0 或其他数字")
    request_sha256 = request_sha256.strip().lower()
    if request_sha256 and (
        len(request_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in request_sha256)
    ):
        raise ValueError("request_sha256 无效")

    target = run_file.expanduser().resolve()
    payload = load_run(target)
    observability = ensure_observability(payload)
    key = f"{provider}:{request_id}"
    requests = observability["requests"]
    if key in requests and not replace:
        raise RuntimeError(f"请求 {key!r} 已登记；更新状态必须显式使用 --replace")

    record = {
        "package": package,
        "provider": provider,
        "request_id": request_id,
        "operation": operation,
        "model": model.strip() or None,
        "status": status,
        "started_at": started_at.strip() or None,
        "ended_at": ended_at.strip() or None,
        "duration_seconds": duration_seconds,
        "wait_seconds": wait_seconds,
        "retry_index": retry_index,
        "token_status": token_status,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "request_sha256": request_sha256 or None,
        "error_type": error_type.strip() or None,
        "observed_at": utc_now(),
    }
    if estimated_cost is not None or actual_cost is not None:
        record["legacy_financial_evidence"] = {
            "estimated_cost": estimated_cost,
            "actual_cost": actual_cost,
            "currency": currency.strip().upper() or None,
            "cost_status": cost_status or None,
            "deprecated": True,
        }
    old = requests.get(key)
    requests[key] = record
    refresh_observability_summary(payload)

    package_observation = observability["packages"][package]
    package_observation["first_observed_at"] = (
        package_observation.get("first_observed_at") or record["observed_at"]
    )
    package_observation["last_observed_at"] = record["observed_at"]
    if retry_index is not None:
        known_retry_count = max(0, retry_index)
        previous_retry_count = package_observation.get("retry_count")
        package_observation["retry_count"] = max(
            int(previous_retry_count or 0),
            known_retry_count,
        )
    observability["events"].append(
        {
            "sequence": len(observability["events"]) + 1,
            "observed_at": record["observed_at"],
            "event": "request_observation",
            "package": package,
            "provider": provider,
            "request_id": request_id,
            "status": status,
            "token_status": token_status,
            "replaced_previous_observation": old is not None,
        }
    )
    payload["updated_at"] = record["observed_at"]
    payload["finalized_at"] = ""
    atomic_write_json(target, payload)
    return payload


def record_request_observation(
    *,
    run_file: Path,
    package: str,
    provider: str,
    request_id: str,
    operation: str,
    status: str,
    model: str = "",
    started_at: str = "",
    ended_at: str = "",
    duration_seconds: float | None = None,
    wait_seconds: float | None = None,
    retry_index: int | None = None,
    token_status: str = "not_reported",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    request_sha256: str = "",
    error_type: str = "",
    estimated_cost: float | None = None,
    actual_cost: float | None = None,
    currency: str = "CNY",
    cost_status: str = "provider_not_exposed",
    replace: bool = False,
) -> dict[str, Any]:
    target = run_file.expanduser().resolve()
    with run_file_lock(target):
        return _record_request_observation_unlocked(
            run_file=target,
            package=package,
            provider=provider,
            request_id=request_id,
            operation=operation,
            status=status,
            model=model,
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=duration_seconds,
            wait_seconds=wait_seconds,
            retry_index=retry_index,
            token_status=token_status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            request_sha256=request_sha256,
            error_type=error_type,
            estimated_cost=estimated_cost,
            actual_cost=actual_cost,
            currency=currency,
            cost_status=cost_status,
            replace=replace,
        )


def record_performance_observation(
    *, run_file: Path, plan_duration_seconds: float | None = None,
    machine_check_duration_seconds: float | None = None,
    independent_review_rounds: int | None = None,
    invalid_rejection_count: int | None = None,
    duplicate_encode_count: int | None = None,
) -> dict[str, Any]:
    """Record only measured workflow facts; omitted/unknown values stay null."""

    supplied = {
        "plan_duration_seconds": plan_duration_seconds,
        "machine_check_duration_seconds": machine_check_duration_seconds,
        "independent_review_rounds": independent_review_rounds,
        "invalid_rejection_count": invalid_rejection_count,
        "duplicate_encode_count": duplicate_encode_count,
    }
    if all(value is None for value in supplied.values()):
        raise ValueError("性能观测至少提供一个实测值")
    for name in ("plan_duration_seconds", "machine_check_duration_seconds"):
        value = supplied[name]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or float(value) < 0
        ):
            raise ValueError(f"{name} 必须是有限非负数")
    for name in ("independent_review_rounds", "invalid_rejection_count", "duplicate_encode_count"):
        value = supplied[name]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError(f"{name} 必须是非负整数")

    target = run_file.expanduser().resolve()
    with run_file_lock(target):
        payload = load_run(target)
        observability = ensure_observability(payload)
        performance = observability["performance"]
        for name, value in supplied.items():
            if value is not None:
                performance[name] = float(value) if name.endswith("_seconds") else value
        observed_at = utc_now()
        observability["events"].append({
            "sequence": len(observability["events"]) + 1,
            "observed_at": observed_at,
            "event": "performance_observation",
            "measurements": {name: value for name, value in supplied.items() if value is not None},
        })
        refresh_observability_summary(payload)
        payload["updated_at"] = observed_at
        payload["finalized_at"] = ""
        atomic_write_json(target, payload)
        return payload


def status_summary(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_observability(payload)
    stale_dependencies = dependency_staleness(payload)
    return {
        "schema_version": payload["schema_version"],
        "run_id": payload["run_id"],
        "project_dir": payload["project_dir"],
        "work_packages": payload["work_packages"],
        "artifact_ids": sorted(payload["artifacts"]),
        "token_usage": payload["observability"]["token_summary"],
        "request_count": len(payload["observability"]["requests"]),
        "performance": payload["observability"]["performance"],
        "stale_artifacts": stale_dependencies,
        "blocker": payload.get("blocker", ""),
        "finalized": bool(payload.get("finalized_at")),
    }


def dependency_staleness(payload: dict[str, Any]) -> dict[str, list[str]]:
    """Return only artifacts whose explicitly declared upstream hashes are stale.

    A dependency key names either a ledger input or another artifact.  Historical
    records without ``input_sha256s`` remain readable; they are not retroactively
    guessed.  Unknown declared keys are evidence gaps and therefore stale.
    """

    current: dict[str, str] = {}
    for collection_name in ("inputs", "artifacts"):
        collection = payload.get(collection_name)
        if not isinstance(collection, dict):
            continue
        for key, record in collection.items():
            if isinstance(record, dict) and isinstance(record.get("sha256"), str):
                if collection_name == "inputs":
                    path = Path(str(record.get("path") or "")).expanduser().resolve()
                    current[str(key)] = file_sha256(path) if path.is_file() else "missing"
                else:
                    current[str(key)] = str(record["sha256"])
    stale: dict[str, list[str]] = {}
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        return stale
    for artifact_id, record in artifacts.items():
        if not isinstance(record, dict):
            continue
        bindings = record.get("input_sha256s")
        if not isinstance(bindings, dict):
            continue
        reasons = [
            f"{key}:expected={expected},current={current.get(str(key), 'missing')}"
            for key, expected in sorted(bindings.items())
            if current.get(str(key)) != expected
        ]
        if reasons:
            stale[str(artifact_id)] = reasons
    return stale


def _finalize_run_unlocked(*, run_file: Path, required_artifacts: Iterable[str]) -> dict[str, Any]:
    target = run_file.expanduser().resolve()
    payload = load_run(target)
    incomplete = [
        name for name, value in payload["work_packages"].items() if value["status"] != "done"
    ]
    if incomplete:
        raise RuntimeError(f"仍有未完成工作包：{', '.join(incomplete)}")
    required = list(
        dict.fromkeys([*CODEX_NATIVE_REQUIRED_ARTIFACTS, *required_artifacts])
    )
    # Historical ledgers predate the merged final review. Keep their real
    # customer-media review instead of inventing a v2 compatibility shell or
    # silently dropping its audio/subtitle/logo/geometry checks.
    if payload.get("requirements_policy") == "story-applicable-requirements/v1":
        required.append("requirements_projection")
    else:
        required.append("customer_media_independent_review")
    missing = [name for name in required if name not in payload["artifacts"]]
    if missing:
        raise RuntimeError(f"缺少必需产物：{', '.join(missing)}")
    stale: list[str] = []
    for artifact_id, record in payload["artifacts"].items():
        path = Path(record["path"])
        if not path.is_file() or file_sha256(path) != record["sha256"]:
            stale.append(artifact_id)
    if stale:
        raise RuntimeError(f"产物缺失或哈希漂移：{', '.join(stale)}")
    dependency_stale = dependency_staleness(payload)
    if dependency_stale:
        detail = "；".join(
            f"{artifact_id}[{', '.join(reasons)}]"
            for artifact_id, reasons in sorted(dependency_stale.items())
        )
        raise RuntimeError(f"产物依赖已过期：{detail}")
    for artifact_id in required:
        record = payload["artifacts"][artifact_id]
        try:
            validate_artifact_semantics(
                artifact_id,
                Path(record["path"]),
                registered_artifacts=payload["artifacts"],
                registered_inputs=payload["inputs"],
            )
        except ValueError as exc:
            raise RuntimeError(f"产物语义门禁失败：{exc}") from exc
    if payload.get("requirements_policy") == "story-applicable-requirements/v1":
        final_review = json.loads(
            Path(payload["artifacts"]["final_delivery_review"]["path"]).read_text(encoding="utf-8")
        )
        if final_review.get("schema_version") != "final-delivery-independent-review/v2":
            raise RuntimeError(
                "新项目 final_delivery_review 必须合并音频、字幕、Logo、人物几何和证据检查；"
                "不再新建重复客户媒体独立审核"
            )
    storyboard_receipt = payload["artifacts"].get("shot_storyboard_compile_receipt")
    theme_manifest = payload["artifacts"].get("theme_assets_manifest")
    if storyboard_receipt is not None and not isinstance(theme_manifest, dict):
        raise RuntimeError(
            "Codex 原生逐镜流水线缺少 theme_assets_manifest；"
            "背景、故事框及 A 镜合成规范尚未进入最终账本"
        )
    if isinstance(theme_manifest, dict):
        validate_theme_assets_manifest(
            Path(theme_manifest["path"]),
            require_v3=isinstance(storyboard_receipt, dict),
        )
    if isinstance(storyboard_receipt, dict):
        # The provider mutates status/result columns in the jobs CSV after the
        # storyboard compile receipt is sealed.  Finalization binds the current
        # receipt file and all immutable dependencies, while the delivery
        # receipt separately binds the delivered PPT pair.
        validate_compile_receipt(
            Path(storyboard_receipt["path"]),
            require_current_r2v_jobs=False,
        )
        delivery_receipt = payload["artifacts"].get("static_ppt_delivery_receipt")
        if not isinstance(delivery_receipt, dict):
            raise RuntimeError(
                "已登记逐镜故事板编译回执，但缺少 static_ppt_delivery_receipt；"
                "不能证明最终双版 PPT 仍消费同一份封存故事板"
            )
        delivery = validate_delivery_receipt(Path(delivery_receipt["path"]))
        if delivery.get("shot_storyboard_compile_receipt_sha256") != storyboard_receipt.get("sha256"):
            raise RuntimeError("静态 PPT 交付回执没有绑定账本中的当前故事板编译回执")
    generation_receipt = payload["artifacts"].get("semantic_card_generation_receipt")
    if isinstance(generation_receipt, dict):
        issues = semantic_card_generation_receipt_issues(Path(generation_receipt["path"]))
        if issues:
            raise RuntimeError("ImageGen 片头/寓意卡回执失效：" + "；".join(issues))
    motion_receipt = payload["artifacts"].get("semantic_card_motion_receipt")
    if isinstance(motion_receipt, dict):
        motion_path = Path(motion_receipt["path"])
        issues = semantic_card_motion_receipt_issues(
            motion_path.parent / "semantic_card_motion_request.json",
            motion_path,
        )
        if issues:
            raise RuntimeError("片头/寓意卡 API 微动回执失效：" + "；".join(issues))
    payload["finalized_at"] = utc_now()
    payload["updated_at"] = payload["finalized_at"]
    payload["blocker"] = ""
    atomic_write_json(target, payload)
    return payload


def finalize_run(*, run_file: Path, required_artifacts: Iterable[str]) -> dict[str, Any]:
    target = run_file.expanduser().resolve()
    with run_file_lock(target):
        return _finalize_run_unlocked(
            run_file=target,
            required_artifacts=required_artifacts,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex 原生故事生产的极简产物账本")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="用确认文本、调色绿幕视频和权威音频创建账本")
    init.add_argument("--run-file", required=True, type=Path)
    init.add_argument("--project-dir", required=True, type=Path)
    init.add_argument("--text", required=True, type=Path)
    init.add_argument("--video", required=True, type=Path)
    init.add_argument("--audio", required=True, type=Path)
    init.add_argument(
        "--subtitle-txt",
        type=Path,
        help="用户确认且已换好行的字幕 TXT；省略时仅在项目内唯一自动识别",
    )
    init.add_argument("--soft-budget", type=float, help=argparse.SUPPRESS)
    init.add_argument("--hard-budget", type=float, help=argparse.SUPPRESS)

    record = subparsers.add_parser("record", help="登记一个工作包状态和可选的当前有效产物")
    record.add_argument("--run-file", required=True, type=Path)
    record.add_argument("--package", required=True, choices=PACKAGE_NAMES)
    record.add_argument("--status", required=True, choices=sorted(PACKAGE_STATES))
    record.add_argument("--artifact-id", default="")
    record.add_argument("--path", type=Path)
    record.add_argument("--input-hash", action="append", default=[])
    record.add_argument("--provider-task-id", default="")
    record.add_argument("--paid-amount", type=float, default=0.0, help=argparse.SUPPRESS)
    record.add_argument("--blocker", default="")
    record.add_argument("--replace", action="store_true")

    observe = subparsers.add_parser(
        "observe-request",
        help="登记一次请求的 provider/model/ID、时间、等待、重试和 Token",
    )
    observe.add_argument("--run-file", required=True, type=Path)
    observe.add_argument("--package", required=True, choices=PACKAGE_NAMES)
    observe.add_argument("--provider", required=True)
    observe.add_argument("--request-id", required=True)
    observe.add_argument("--operation", required=True)
    observe.add_argument("--status", required=True, choices=sorted(REQUEST_STATES))
    observe.add_argument("--model", default="")
    observe.add_argument("--started-at", default="")
    observe.add_argument("--ended-at", default="")
    observe.add_argument("--duration-seconds", type=float)
    observe.add_argument("--wait-seconds", type=float)
    observe.add_argument("--retry-index", type=int)
    observe.add_argument("--token-status", choices=sorted(TOKEN_STATES), default="not_reported")
    observe.add_argument("--input-tokens", type=int)
    observe.add_argument("--output-tokens", type=int)
    observe.add_argument("--total-tokens", type=int)
    observe.add_argument("--request-sha256", default="")
    observe.add_argument("--error-type", default="")
    observe.add_argument("--estimated-cost", type=float, help=argparse.SUPPRESS)
    observe.add_argument("--actual-cost", type=float, help=argparse.SUPPRESS)
    observe.add_argument("--currency", default="CNY", help=argparse.SUPPRESS)
    observe.add_argument("--cost-status", default="provider_not_exposed", help=argparse.SUPPRESS)
    observe.add_argument("--replace", action="store_true")

    performance = subparsers.add_parser(
        "observe-performance",
        help="登记计划/机器检查耗时、审核轮次与重复工作实测值",
    )
    performance.add_argument("--run-file", required=True, type=Path)
    performance.add_argument("--plan-duration-seconds", type=float)
    performance.add_argument("--machine-check-duration-seconds", type=float)
    performance.add_argument("--independent-review-rounds", type=int)
    performance.add_argument("--invalid-rejection-count", type=int)
    performance.add_argument("--duplicate-encode-count", type=int)

    status = subparsers.add_parser("status", help="输出六个工作包、当前产物、请求和过期依赖")
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
            subtitle_txt=args.subtitle_txt,
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
    elif args.command == "observe-request":
        payload = record_request_observation(
            run_file=args.run_file,
            package=args.package,
            provider=args.provider,
            request_id=args.request_id,
            operation=args.operation,
            status=args.status,
            model=args.model,
            started_at=args.started_at,
            ended_at=args.ended_at,
            duration_seconds=args.duration_seconds,
            wait_seconds=args.wait_seconds,
            retry_index=args.retry_index,
            token_status=args.token_status,
            input_tokens=args.input_tokens,
            output_tokens=args.output_tokens,
            total_tokens=args.total_tokens,
            request_sha256=args.request_sha256,
            error_type=args.error_type,
            estimated_cost=args.estimated_cost,
            actual_cost=args.actual_cost,
            currency=args.currency,
            cost_status=args.cost_status,
            replace=args.replace,
        )
    elif args.command == "observe-performance":
        payload = record_performance_observation(
            run_file=args.run_file,
            plan_duration_seconds=args.plan_duration_seconds,
            machine_check_duration_seconds=args.machine_check_duration_seconds,
            independent_review_rounds=args.independent_review_rounds,
            invalid_rejection_count=args.invalid_rejection_count,
            duplicate_encode_count=args.duplicate_encode_count,
        )
    elif args.command == "status":
        payload = status_summary(load_run(args.run_file))
    else:
        payload = finalize_run(run_file=args.run_file, required_artifacts=args.require)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
