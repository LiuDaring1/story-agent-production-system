from __future__ import annotations

import hashlib
import json
import os
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageChops, ImageStat


MOTION_PLAN_SCHEMA_VERSION = "1.1.0"
MOTION_PLAN_COMPILER_VERSION = "1.1.0"
MOTION_LEVELS = {"none", "low", "moderate", "high"}
MOTION_PRIMARIES = {"subject", "environment", "camera", "quiet"}
SCREEN_DIRECTIONS = {
    "left_to_right", "right_to_left", "toward_camera", "away_from_camera",
    "stationary", "mixed",
}
VIDEO_SOURCE_KINDS = {
    "provider_generated", "mock_provider", "ffmpeg_still_frame",
    "static_fallback", "test_fixture",
}


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compile_motion_plan(storyboard_payload: Mapping[str, Any]) -> dict[str, Any]:
    shots = storyboard_payload.get("shots")
    if not isinstance(shots, list):
        raise ValueError("storyboard plan 缺少 shots")
    binding = {
        key: storyboard_payload.get(key, "")
        for key in (
            "contract_schema_version", "story_contract_sha256",
            "story_contract_dependency_sha256", "contract_projection_sha256", "storyboard_sha256",
        )
    }
    result_shots: list[dict[str, Any]] = []
    for row in shots:
        if not isinstance(row, dict):
            raise ValueError("storyboard shot 必须是对象")
        continuity = {
            "characters": row.get("visible_characters", []),
            "story_state": row.get("current_story_state", {}),
            "visual_state_evidence": row.get("visual_state_evidence", {}),
            "scale_basis": row.get("scale_basis", {}),
            "required": row.get("continuity_required", []),
            "forbidden": row.get("continuity_forbidden", []),
        }
        result_shots.append({
            "scene": row.get("scene"),
            "subject_action": row.get("subject_action"),
            "environment_motion": row.get("environment_motion"),
            "camera_motion": row.get("camera_motion"),
            "entry_state": row.get("entry_state"),
            "exit_state": row.get("exit_state"),
            "screen_direction": row.get("screen_direction"),
            "adjacent_handoff": row.get("adjacent_handoff"),
            "expected_motion": row.get("expected_motion"),
            "continuity": continuity,
        })
    payload = {
        "schema_version": MOTION_PLAN_SCHEMA_VERSION,
        "compiler_version": MOTION_PLAN_COMPILER_VERSION,
        **binding,
        "shots": result_shots,
    }
    issues = validate_motion_plan(payload)
    if issues:
        raise ValueError("视频动作计划无效：" + "；".join(issues))
    return payload


def validate_motion_plan(payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("schema_version") != MOTION_PLAN_SCHEMA_VERSION:
        errors.append("schema_version 不匹配")
    if payload.get("compiler_version") != MOTION_PLAN_COMPILER_VERSION:
        errors.append("compiler_version 不匹配")
    for key in (
        "contract_schema_version", "story_contract_sha256",
        "story_contract_dependency_sha256", "contract_projection_sha256", "storyboard_sha256",
    ):
        if not isinstance(payload.get(key), str) or not payload.get(key):
            errors.append(f"缺少 {key}")
    shots = payload.get("shots")
    if not isinstance(shots, list) or not shots:
        return errors + ["shots 必须是非空数组"]
    for index, shot in enumerate(shots, start=1):
        prefix = f"scene {index}"
        if not isinstance(shot, dict) or shot.get("scene") != index:
            errors.append(f"{prefix} 编号错误")
            continue
        for key in ("subject_action", "environment_motion", "camera_motion"):
            if not isinstance(shot.get(key), str) or not shot.get(key).strip():
                errors.append(f"{prefix} 缺少 {key}")
        if shot.get("screen_direction") not in SCREEN_DIRECTIONS:
            errors.append(f"{prefix} screen_direction 非法")
        for key in ("entry_state", "exit_state", "adjacent_handoff", "expected_motion", "continuity"):
            if not isinstance(shot.get(key), dict):
                errors.append(f"{prefix} 缺少 {key}")
        expected = shot.get("expected_motion")
        if isinstance(expected, dict):
            if expected.get("primary") not in MOTION_PRIMARIES:
                errors.append(f"{prefix} expected_motion.primary 非法")
            for key in ("subject_level", "environment_level", "camera_level"):
                if expected.get(key) not in MOTION_LEVELS:
                    errors.append(f"{prefix} expected_motion.{key} 非法")
            if not isinstance(expected.get("rationale"), str) or not expected.get("rationale", "").strip():
                errors.append(f"{prefix} expected_motion.rationale 缺失")
        handoff = shot.get("adjacent_handoff")
        if isinstance(handoff, dict):
            if not isinstance(handoff.get("from_previous"), str) or not isinstance(handoff.get("to_next"), str):
                errors.append(f"{prefix} adjacent_handoff 缺少承接标识")
            if not isinstance(handoff.get("allows_direction_change"), bool):
                errors.append(f"{prefix} adjacent_handoff.allows_direction_change 非法")
            if not isinstance(handoff.get("allows_state_transition"), bool):
                errors.append(f"{prefix} adjacent_handoff.allows_state_transition 非法")
    errors.extend(adjacent_handoff_issues(shots))
    return errors


def adjacent_handoff_issues(shots: Sequence[Mapping[str, Any]]) -> list[str]:
    errors: list[str] = []
    for previous, current in zip(shots, shots[1:]):
        left = previous.get("adjacent_handoff", {})
        right = current.get("adjacent_handoff", {})
        if isinstance(left, dict) and isinstance(right, dict):
            outgoing = str(left.get("to_next") or "")
            incoming = str(right.get("from_previous") or "")
            if outgoing and incoming and outgoing != incoming:
                errors.append(f"scene {previous.get('scene')}→{current.get('scene')} adjacent_handoff 不匹配")
            allows = left.get("allows_direction_change") is True or right.get("allows_direction_change") is True
            opposite = {
                ("left_to_right", "right_to_left"),
                ("right_to_left", "left_to_right"),
                ("toward_camera", "away_from_camera"),
                ("away_from_camera", "toward_camera"),
            }
            directions = (previous.get("screen_direction"), current.get("screen_direction"))
            if directions in opposite and not allows:
                errors.append(f"scene {previous.get('scene')}→{current.get('scene')} 无解释方向反转")
        exit_state = previous.get("exit_state", {})
        entry_state = current.get("entry_state", {})
        if isinstance(exit_state, dict) and isinstance(entry_state, dict):
            previous_story = exit_state.get("story_state")
            current_story = entry_state.get("story_state")
            permits = right.get("allows_state_transition") is True if isinstance(right, dict) else False
            if previous_story is not None and current_story is not None and previous_story != current_story and not permits:
                errors.append(f"scene {previous.get('scene')}→{current.get('scene')} entry/exit story_state 跳变")
    return errors


def motion_prompt_clause(shot: Mapping[str, Any]) -> str:
    expected = shot.get("expected_motion", {})
    machine = json.dumps({
        key: shot.get(key)
        for key in (
            "subject_action", "environment_motion", "camera_motion", "entry_state",
            "exit_state", "screen_direction", "adjacent_handoff", "expected_motion", "continuity",
        )
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    primary = expected.get("primary") if isinstance(expected, dict) else ""
    quiet = primary == "quiet"
    movement_rule = (
        "本镜允许安静低运动，但必须保留自然呼吸、微表情或已声明的环境/相机运动，不能输出完全静止帧。"
        if quiet else
        "连续性用于稳定身份、状态、尺度和故事逻辑，不得用完全静止或几乎不动逃避动作；必须完成已声明的主体、环境和相机职责。"
    )
    return f"[VIDEO_MOTION_PLAN_V1]{machine}\n{movement_rule}不得为增加运动而破坏 required/forbidden 或故事状态。"


def inject_motion_prompt(prompt: str, shot: Mapping[str, Any]) -> str:
    marker = "[VIDEO_MOTION_PLAN_V1]"
    base = str(prompt or "").split(marker, 1)[0].rstrip()
    return f"{base}\n{motion_prompt_clause(shot)}".strip()


def motion_metrics(frame_paths: Sequence[Path], expected_motion: Mapping[str, Any]) -> dict[str, Any]:
    images = [Image.open(path).convert("L").resize((160, 90)) for path in frame_paths]
    if len(images) < 2:
        raise ValueError("运动 QA 至少需要两帧")
    region = expected_motion.get("subject_region", [0.25, 0.15, 0.5, 0.75])
    if not isinstance(region, list) or len(region) != 4:
        region = [0.25, 0.15, 0.5, 0.75]
    x, y, width, height = [float(value) for value in region]
    box = (int(x * 160), int(y * 90), int((x + width) * 160), int((y + height) * 90))
    global_scores: list[float] = []
    subject_scores: list[float] = []
    environment_scores: list[float] = []
    camera_scores: list[float] = []
    for first, second in zip(images, images[1:]):
        diff = ImageChops.difference(first, second)
        global_score = ImageStat.Stat(diff).mean[0] / 255.0
        subject_score = ImageStat.Stat(diff.crop(box)).mean[0] / 255.0
        area = 160 * 90
        subject_area = max(1, (box[2] - box[0]) * (box[3] - box[1]))
        environment_score = max(0.0, (global_score * area - subject_score * subject_area) / max(1, area - subject_area))
        best = global_score
        for dx, dy in ((-3, 0), (3, 0), (0, -2), (0, 2)):
            shifted = ImageChops.offset(first, dx, dy)
            candidate = ImageStat.Stat(ImageChops.difference(shifted, second)).mean[0] / 255.0
            best = min(best, candidate)
        global_scores.append(global_score)
        subject_scores.append(subject_score)
        environment_scores.append(environment_score)
        camera_scores.append(max(0.0, global_score - best))
    component_key = {
        "subject": "subject",
        "environment": "environment",
        "camera": "camera",
    }.get(str(expected_motion.get("primary") or ""), "global")
    component_scores = {
        "subject": subject_scores,
        "environment": environment_scores,
        "camera": camera_scores,
        "global": global_scores,
    }[component_key]
    level_key = f"{component_key}_level" if component_key != "global" else "subject_level"
    level = str(expected_motion.get(level_key) or "low")
    activation_thresholds = {"none": 0.0, "low": 0.0015, "moderate": 0.006, "high": 0.012}
    activation_threshold = activation_thresholds.get(level, 0.0015)
    pair_count = len(global_scores)
    active_pair_count = sum(score >= 0.0015 for score in global_scores)
    motion_pair_count = sum(score >= activation_threshold for score in component_scores)
    return {
        "global_motion": max(global_scores),
        "subject_motion": max(subject_scores),
        "environment_motion": max(environment_scores),
        "camera_translation_evidence": max(camera_scores),
        "pairwise_global_motion": global_scores,
        "pairwise_subject_motion": subject_scores,
        "pairwise_environment_motion": environment_scores,
        "pairwise_camera_translation_evidence": camera_scores,
        "active_pair_count": active_pair_count,
        "motion_pair_count": motion_pair_count,
        "motion_coverage": motion_pair_count / pair_count if pair_count else 0.0,
        "median_global_motion": statistics.median(global_scores),
        "median_primary_motion": statistics.median(component_scores),
        "frame_count": len(images),
    }


def motion_evidence_issues(metrics: Mapping[str, Any], expected: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    global_motion = float(metrics.get("global_motion", 0.0))
    if global_motion < 0.0015:
        return ["completely_static"]
    thresholds = {"none": 0.0, "low": 0.0015, "moderate": 0.006, "high": 0.012}
    primary = expected.get("primary")
    component = {
        "subject": ("subject_motion", "subject_level"),
        "environment": ("environment_motion", "environment_level"),
        "camera": ("camera_translation_evidence", "camera_level"),
    }.get(primary)
    if component:
        metric_key, level_key = component
        level = str(expected.get(level_key) or "none")
        if float(metrics.get(metric_key, 0.0)) < thresholds.get(level, 0.0):
            issues.append(f"expected_{primary}_motion_not_met")
        pairwise_key = {
            "subject": "pairwise_subject_motion",
            "environment": "pairwise_environment_motion",
            "camera": "pairwise_camera_translation_evidence",
        }[str(primary)]
        pairwise = metrics.get(pairwise_key)
        if level in {"moderate", "high"}:
            if not isinstance(pairwise, list) or not pairwise:
                issues.append(f"expected_{primary}_motion_time_evidence_missing")
            else:
                required_coverage = 0.5 if level == "moderate" else 0.75
                coverage = sum(float(value) >= thresholds[level] for value in pairwise) / len(pairwise)
                if coverage < required_coverage:
                    issues.append(f"expected_{primary}_motion_not_sustained")
    if primary != "quiet" and global_motion < 0.003:
        issues.append("near_static_conflicts_with_plan")
    return issues


def formal_source_issues(row: Mapping[str, Any], *, production_mode: bool) -> list[str]:
    kind = str(row.get("video_source_kind") or "").strip()
    eligible = str(row.get("production_eligible") or "").strip().lower() == "true"
    if not production_mode:
        return []
    if kind != "provider_generated" or not eligible:
        return [f"non_production_video_source:{kind or 'missing'}"]
    return []


def write_video_receipt(
    jobs_csv: Path,
    row: Mapping[str, Any],
    video_path: Path,
    *,
    provider: str,
    model: str,
    source_kind: str,
    execution_mode: str,
    production_eligible: bool,
) -> tuple[Path, str]:
    if source_kind not in VIDEO_SOURCE_KINDS:
        raise ValueError(f"未知视频来源类型：{source_kind}")
    receipt = {
        "schema_version": "1.0.0",
        "scene": int(str(row.get("scene") or "0")),
        "target_video_filename": str(row.get("target_video_filename") or ""),
        "video_sha256": file_sha256(video_path),
        "provider": provider,
        "model": model,
        "source_kind": source_kind,
        "execution_mode": execution_mode,
        "production_eligible": bool(production_eligible),
        "provider_attempt": int(str(row.get("provider_attempt") or "0")),
        "task_id": str(row.get("task_id") or ""),
        "client_business_id": str(row.get("client_business_id") or ""),
        "story_contract_sha256": str(row.get("story_contract_sha256") or ""),
        "story_contract_dependency_sha256": str(row.get("story_contract_dependency_sha256") or ""),
        "motion_plan_sha256": str(row.get("motion_plan_sha256") or ""),
    }
    directory = jobs_csv.expanduser().parent / "video_receipts"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"scene_{receipt['scene']:02d}_attempt_{receipt['provider_attempt']:02d}.json"
    temporary = target.with_name(target.name + f".{os.getpid()}.tmp")
    temporary.write_bytes(canonical_json_bytes(receipt))
    temporary.replace(target)
    return target, file_sha256(target)


def video_receipt_issues(row: Mapping[str, Any], video_path: Path, *, production_mode: bool) -> list[str]:
    issues = formal_source_issues(row, production_mode=production_mode)
    receipt_path = Path(str(row.get("video_receipt_path") or "")) if row.get("video_receipt_path") else None
    if receipt_path is None or not receipt_path.is_file():
        return issues + ["video_receipt_missing"]
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return issues + ["video_receipt_invalid"]
    if receipt.get("schema_version") != "1.0.0":
        issues.append("video_receipt_schema_version_mismatch")
    if file_sha256(receipt_path) != str(row.get("video_receipt_sha256") or ""):
        issues.append("video_receipt_hash_mismatch")
    if not video_path.is_file() or receipt.get("video_sha256") != file_sha256(video_path):
        issues.append("video_receipt_video_hash_mismatch")
    integer_bindings = {"scene": "scene", "provider_attempt": "provider_attempt"}
    for receipt_key, row_key in integer_bindings.items():
        try:
            current = int(str(row.get(row_key) or "0"))
        except (TypeError, ValueError):
            issues.append(f"video_receipt_{receipt_key}_mismatch")
            continue
        if type(receipt.get(receipt_key)) is not int or receipt.get(receipt_key) != current:
            issues.append(f"video_receipt_{receipt_key}_mismatch")
    string_bindings = {
        "target_video_filename": "target_video_filename",
        "provider": "video_provider",
        "model": "video_model",
        "execution_mode": "video_execution_mode",
        "source_kind": "video_source_kind",
        "task_id": "task_id",
        "client_business_id": "client_business_id",
    }
    for receipt_key, row_key in string_bindings.items():
        if receipt.get(receipt_key) != str(row.get(row_key) or ""):
            issues.append(f"video_receipt_{receipt_key}_mismatch")
    row_eligible = str(row.get("production_eligible") or "").strip().lower() == "true"
    if type(receipt.get("production_eligible")) is not bool or receipt.get("production_eligible") != row_eligible:
        issues.append("video_receipt_production_eligible_mismatch")
    for key in ("story_contract_sha256", "story_contract_dependency_sha256", "motion_plan_sha256"):
        if receipt.get(key) != str(row.get(key) or ""):
            issues.append(f"video_receipt_{key}_mismatch")
    if production_mode and (receipt.get("source_kind") != "provider_generated" or receipt.get("production_eligible") is not True):
        issues.append("video_receipt_not_production_eligible")
    return issues


def review_semantic_issues(
    payload: Mapping[str, Any], *, expected_scenes: Sequence[int] | None = None,
) -> list[str]:
    issues: list[str] = []
    reviews = payload.get("per_scene_reviews", [])
    if not isinstance(reviews, list):
        return ["per_scene_reviews_missing"] if expected_scenes is not None else []
    seen: set[int] = set()
    for row in reviews:
        if not isinstance(row, dict):
            continue
        try:
            scene = int(row.get("scene"))
        except (TypeError, ValueError):
            scene = 0
        if scene:
            seen.add(scene)
        if expected_scenes is not None:
            for key in ("story_state_consistent", "adjacent_handoff_consistent"):
                if not isinstance(row.get(key), bool):
                    issues.append(f"scene_{scene or 'unknown'}:{key}_missing")
        if row.get("story_state_consistent") is False:
            issues.append(f"scene_{row.get('scene')}:story_state_conflict")
        if row.get("adjacent_handoff_consistent") is False:
            issues.append(f"scene_{row.get('scene')}:adjacent_handoff_conflict")
    if expected_scenes is not None:
        missing = sorted(set(expected_scenes) - seen)
        if missing:
            issues.append("per_scene_reviews_incomplete:" + ",".join(map(str, missing)))
    return issues


def schema_python_parity() -> list[str]:
    schema = json.loads((Path(__file__).parent / "schemas" / "video_motion" / "v1" / "video_motion_plan.schema.json").read_text(encoding="utf-8"))
    properties = schema.get("properties", {})
    errors: list[str] = []
    if properties.get("schema_version", {}).get("const") != MOTION_PLAN_SCHEMA_VERSION:
        errors.append("schema_version")
    shot = schema.get("$defs", {}).get("shot", {})
    if set(shot.get("properties", {}).get("screen_direction", {}).get("enum", [])) != SCREEN_DIRECTIONS:
        errors.append("screen_direction")
    expected = schema.get("$defs", {}).get("expected_motion", {}).get("properties", {})
    if set(expected.get("primary", {}).get("enum", [])) != MOTION_PRIMARIES:
        errors.append("expected_motion.primary")
    handoff = shot.get("properties", {}).get("adjacent_handoff", {})
    if "allows_state_transition" not in handoff.get("required", []):
        errors.append("adjacent_handoff.allows_state_transition.required")
    if handoff.get("properties", {}).get("allows_state_transition", {}).get("type") != "boolean":
        errors.append("adjacent_handoff.allows_state_transition.type")
    return errors
