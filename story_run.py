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

from shot_storyboard_pipeline import validate_compile_receipt
from semantic_card_motion import (
    semantic_card_generation_receipt_issues,
    semantic_card_motion_receipt_issues,
)
from static_ppt_contract import validate_delivery_receipt


SCHEMA_VERSION = "story-run-v1"
THEME_ASSETS_SCHEMA_VERSION = "story-theme-assets-lightweight/v3"
LEGACY_THEME_ASSETS_SCHEMA_VERSION = "story-theme-assets-lightweight/v2"
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
CODEX_NATIVE_REQUIRED_ARTIFACTS = (
    "master_director_plan",
    "director_plan_review",
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
    "qa_product_report",
    "qa_publish_report",
    "main_release_video",
    "library_release_video",
    "qa_release_report",
    "final_delivery_review",
    "final_delivery_checklist",
)


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
    subtitle_txt: Path | None = None,
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
        if artifact_id == "theme_assets_manifest":
            validate_theme_assets_manifest(artifact)
        if artifact_id == "shot_storyboard_compile_receipt":
            validate_compile_receipt(artifact)
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
    required = tuple(
        dict.fromkeys([*CODEX_NATIVE_REQUIRED_ARTIFACTS, *required_artifacts])
    )
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
    elif args.command == "status":
        payload = status_summary(load_run(args.run_file))
    else:
        payload = finalize_run(run_file=args.run_file, required_artifacts=args.require)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
