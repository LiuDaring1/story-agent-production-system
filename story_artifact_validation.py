"""Fail-closed semantic validation for Codex-native story ledger artifacts.

The run ledger is intentionally a compact registry rather than a scheduler.
That does not mean arbitrary JSON may stand in for QA or review evidence.  This
module validates the small cross-project success contract shared by those
artifacts and rechecks every hash binding at finalization time.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from story_timeline import validate_authoritative_timeline_receipt
from story_customer_media import validate_customer_media_receipt
from release_geometry import release_package_receipt_issues

PASS_SCORE = 85.0

INDEPENDENT_REVIEW_TARGETS: dict[str, str | None] = {
    "director_plan_review": "master_director_plan",
    "storyboard_review": "storyboard_manifest_sealed",
    "r2v_group_visual_review": "r2v_provider_group_receipt",
    "keying_visual_review": None,
    "customer_media_independent_review": "customer_media_receipt",
    "final_delivery_review": None,
}

MACHINE_QA_ARTIFACTS = {
    "r2v_group_machine_qa",
    "qa_product_report",
    "qa_publish_report",
    "qa_release_report",
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}不是有效 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label}顶层必须是 JSON 对象：{path}")
    return payload


def _require_current_binding(item: Mapping[str, Any], label: str) -> Path:
    path_text = str(item.get("path") or "").strip()
    expected = str(item.get("sha256") or "").strip().lower()
    if not path_text or len(expected) != 64:
        raise ValueError(f"{label}缺少 path/sha256 绑定")
    path = Path(path_text).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"{label}绑定文件不存在：{path}")
    if file_sha256(path) != expected:
        raise ValueError(f"{label}绑定文件哈希漂移：{path}")
    return path


def _require_empty_list(payload: Mapping[str, Any], key: str, label: str) -> None:
    value = payload.get(key)
    if not isinstance(value, list) or value:
        raise ValueError(f"{label}要求 {key}=[]")


def _validate_bundle_members_if_present(path: Path, label: str) -> None:
    if path.suffix.lower() != ".json":
        return
    payload = _load_json_object(path, label)
    members = payload.get("artifacts")
    if members is None:
        return
    if isinstance(members, list):
        if not members:
            raise ValueError(f"{label}的 artifacts 必须是非空列表或对象")
        iterator = ((str(index), item) for index, item in enumerate(members))
    elif isinstance(members, dict):
        if not members:
            raise ValueError(f"{label}的 artifacts 必须是非空列表或对象")
        iterator = members.items()
    else:
        raise ValueError(f"{label}的 artifacts 必须是非空列表或对象")
    for name, item in iterator:
        if not isinstance(item, dict):
            raise ValueError(f"{label}的 artifacts[{name}] 格式无效")
        _require_current_binding(item, f"{label} artifacts[{name}]")


def _validate_customer_media_independent_evidence(payload: Mapping[str, Any]) -> None:
    label = "独立审核 customer_media_independent_review"
    required_checks = {
        "three_customer_videos_music_only_and_not_silent",
        "customer_videos_do_not_contain_authoritative_narration",
        "demo_contains_narration",
        "demo_contains_music",
        "demo_logo_visible_in_all_formal_samples",
        "demo_presenter_scaled_to_output_canvas",
        "demo_wide_gesture_not_cropped",
        "demo_subtitles_match_authoritative_timeline",
        "customer_subtitles_bottom_centered",
    }
    checks = payload.get("checks")
    if not isinstance(checks, dict) or any(checks.get(name) is not True for name in required_checks):
        raise ValueError(f"{label}缺少逐项媒体/Logo/几何/字幕通过证据")
    bindings = payload.get("bindings")
    if not isinstance(bindings, dict):
        raise ValueError(f"{label}缺少 Demo、完整 SRT 与官方 Logo 绑定")
    for name in ("demo_video", "authoritative_full_srt", "official_logo"):
        item = bindings.get(name)
        if not isinstance(item, dict):
            raise ValueError(f"{label}缺少 bindings.{name}")
        _require_current_binding(item, f"{label} bindings.{name}")
    geometry = payload.get("presenter_geometry")
    if not isinstance(geometry, dict):
        raise ValueError(f"{label}缺少人物显示几何")
    source = geometry.get("source_canvas")
    output = geometry.get("output_canvas")
    rendered = geometry.get("rendered_size")
    scale = geometry.get("scale")
    if (
        not isinstance(source, list) or len(source) != 2
        or not isinstance(output, list) or len(output) != 2
        or not isinstance(rendered, list) or len(rendered) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in [*source, *output, *rendered])
        or isinstance(scale, bool) or not isinstance(scale, (int, float)) or not 0 < float(scale) <= 1
        or rendered != output
        or any(abs(round(source[index] * float(scale)) - rendered[index]) > 1 for index in (0, 1))
    ):
        raise ValueError(f"{label}人物显示几何无效或仍为源尺寸直贴")
    frames = payload.get("formal_frame_evidence")
    if not isinstance(frames, list) or len(frames) < 5:
        raise ValueError(f"{label}至少需要五个正式 Demo 首中末证据帧")
    timestamps: set[float] = set()
    for index, item in enumerate(frames):
        if not isinstance(item, dict):
            raise ValueError(f"{label} formal_frame_evidence[{index}] 格式无效")
        _require_current_binding(item, f"{label} formal_frame_evidence[{index}]")
        try:
            timestamp = float(item.get("time_seconds"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}证据帧时间无效") from exc
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError(f"{label}证据帧时间无效")
        timestamps.add(timestamp)
    if len(timestamps) != len(frames):
        raise ValueError(f"{label}证据帧时间重复")


def validate_independent_review(
    artifact_id: str,
    path: Path,
    *,
    registered_artifacts: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    label = f"独立审核 {artifact_id}"
    payload = _load_json_object(path, label)
    if payload.get("approved") is not True:
        raise ValueError(f"{label}未 approved=true")
    try:
        score = float(payload.get("score"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}缺少有效 score") from exc
    if not math.isfinite(score) or score < PASS_SCORE:
        raise ValueError(f"{label}分数低于 {PASS_SCORE:g}")
    _require_empty_list(payload, "critical_errors", label)

    schema = str(payload.get("schema_version") or "")
    independence = payload.get("reviewer_independence")
    schema_declares_independence = "independent" in schema.lower()
    object_declares_independence = (
        isinstance(independence, dict)
        and independence.get("producer_claims_trusted") is False
    )
    if not schema_declares_independence and not object_declares_independence:
        raise ValueError(f"{label}没有独立上下文证据")

    reviewed_path_text = str(payload.get("artifact_path") or "").strip()
    reviewed_sha = str(payload.get("artifact_sha256") or "").strip().lower()
    if not reviewed_path_text or len(reviewed_sha) != 64:
        raise ValueError(f"{label}缺少被审 artifact_path/artifact_sha256")
    reviewed_path = Path(reviewed_path_text).expanduser().resolve()
    if not reviewed_path.is_file():
        raise ValueError(f"{label}的被审产物缺失或哈希漂移")
    reviewed_file_sha = file_sha256(reviewed_path)
    if artifact_id == "storyboard_review":
        storyboard_manifest = _load_json_object(reviewed_path, f"{label}被审故事板清单")
        bundle_sha = str(storyboard_manifest.get("storyboard_bundle_sha256") or "").lower()
        # New sealed manifests bind their independent review to the semantic
        # storyboard bundle, while the ledger separately binds the current
        # manifest file bytes.  Legacy manifests predate that semantic field
        # and therefore keep the historical file-SHA review contract.
        expected_review_sha = bundle_sha or reviewed_file_sha
        if expected_review_sha != reviewed_sha:
            if not bundle_sha:
                raise ValueError(f"{label}的被审产物缺失或哈希漂移")
            raise ValueError(f"{label}没有绑定故事板清单中的 storyboard_bundle_sha256")
    elif reviewed_file_sha != reviewed_sha:
        raise ValueError(f"{label}的被审产物缺失或哈希漂移")
    _validate_bundle_members_if_present(reviewed_path, f"{label}被审 bundle")

    target_id = INDEPENDENT_REVIEW_TARGETS[artifact_id]
    if target_id and registered_artifacts is not None:
        target = registered_artifacts.get(target_id)
        if not isinstance(target, Mapping):
            raise ValueError(f"{label}缺少账本目标 {target_id}")
        target_path = Path(str(target.get("path") or "")).expanduser().resolve()
        target_sha = str(target.get("sha256") or "").lower()
        target_binding_sha = reviewed_file_sha if artifact_id == "storyboard_review" else reviewed_sha
        if reviewed_path != target_path or target_binding_sha != target_sha:
            raise ValueError(f"{label}没有绑定账本中的当前 {target_id}")
    if artifact_id == "customer_media_independent_review":
        validate_customer_media_receipt(reviewed_path)
        _validate_customer_media_independent_evidence(payload)
    return payload


def validate_machine_qa(artifact_id: str, path: Path) -> dict[str, Any]:
    label = f"机器 QA {artifact_id}"
    payload = _load_json_object(path, label)
    if not str(payload.get("schema_version") or "").strip():
        raise ValueError(f"{label}缺少 schema_version")
    if payload.get("passed") is not True:
        raise ValueError(f"{label}未 passed=true")
    _require_empty_list(payload, "critical_errors", label)
    errors = payload.get("errors")
    if errors not in (None, []) and (not isinstance(errors, list) or errors):
        raise ValueError(f"{label}仍有 errors")
    return payload


def validate_music_qa(path: Path) -> dict[str, Any]:
    label = "配乐 QA qa_music_report"
    payload = _load_json_object(path, label)
    if payload.get("approved") is not True:
        raise ValueError(f"{label}未 approved=true")
    try:
        score = float(payload.get("score"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}缺少有效 score") from exc
    if not math.isfinite(score) or score < PASS_SCORE:
        raise ValueError(f"{label}分数低于 {PASS_SCORE:g}")
    _require_empty_list(payload, "critical_errors", label)

    final_audio = payload.get("reviewed_final_audio")
    if not isinstance(final_audio, dict):
        raise ValueError(f"{label}缺少 reviewed_final_audio")
    _require_current_binding(final_audio, f"{label} reviewed_final_audio")
    bound_inputs = payload.get("bound_inputs")
    if not isinstance(bound_inputs, dict) or not bound_inputs:
        raise ValueError(f"{label}缺少 bound_inputs")
    for name, item in bound_inputs.items():
        if not isinstance(item, dict):
            raise ValueError(f"{label} bound_inputs.{name} 格式无效")
        _require_current_binding(item, f"{label} bound_inputs.{name}")
    return payload


def validate_final_delivery_checklist(path: Path) -> dict[str, Any]:
    label = "最终交付清单 final_delivery_checklist"
    payload = _load_json_object(path, label)
    if payload.get("status") not in {
        "complete_pending_independent_final_review",
        "complete",
    }:
        raise ValueError(f"{label}状态未完成")
    _require_empty_list(payload, "missing", label)
    expected_matrix = {
        "release_videos": 2,
        "account_copy_files": 2,
        "covers": 6,
        "basic_customer_files": 5,
        "advanced_customer_files": 10,
        "static_ppt_variants": 2,
    }
    matrix = payload.get("matrix")
    if not isinstance(matrix, dict) or any(matrix.get(key) != value for key, value in expected_matrix.items()):
        raise ValueError(f"{label}交付矩阵不完整")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError(f"{label}缺少逐文件哈希清单")
    for index, item in enumerate(artifacts):
        if not isinstance(item, dict):
            raise ValueError(f"{label} artifacts[{index}] 格式无效")
        _require_current_binding(item, f"{label} artifacts[{index}]")
    return payload


def validate_release_package_receipt(path: Path) -> dict[str, Any]:
    label = "正式发布包装回执 release_package_receipt"
    payload = _load_json_object(path, label)
    issues = release_package_receipt_issues(payload, verify_outputs=True)
    if issues:
        raise ValueError(f"{label}未通过：" + "；".join(issues))
    return payload


def validate_artifact_semantics(
    artifact_id: str,
    path: Path,
    *,
    registered_artifacts: Mapping[str, Mapping[str, Any]] | None = None,
    registered_inputs: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Validate a ledger artifact when it has a cross-project success contract."""

    if artifact_id in INDEPENDENT_REVIEW_TARGETS:
        return validate_independent_review(
            artifact_id,
            path,
            registered_artifacts=registered_artifacts,
        )
    if artifact_id in MACHINE_QA_ARTIFACTS:
        return validate_machine_qa(artifact_id, path)
    if artifact_id == "qa_music_report":
        return validate_music_qa(path)
    if artifact_id == "final_delivery_checklist":
        return validate_final_delivery_checklist(path)
    if artifact_id == "authoritative_timeline_receipt":
        return validate_authoritative_timeline_receipt(
            path,
            expected_inputs=registered_inputs,
        )
    if artifact_id == "customer_media_receipt":
        return validate_customer_media_receipt(path)
    if artifact_id == "release_package_receipt":
        return validate_release_package_receipt(path)
    return None


__all__ = [
    "INDEPENDENT_REVIEW_TARGETS",
    "MACHINE_QA_ARTIFACTS",
    "validate_artifact_semantics",
    "validate_final_delivery_checklist",
    "validate_independent_review",
    "validate_machine_qa",
    "validate_music_qa",
    "validate_release_package_receipt",
]
