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
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

from story_timeline import validate_authoritative_timeline_receipt
from story_customer_media import (
    validate_customer_media_receipt,
    RELEASE_AUDIO_DURATION_TOLERANCE_SECONDS,
    NARRATION_MUSIC_RESIDUAL_MAX,
    NARRATION_COMPONENT_RMS_RATIO_MIN,
    MUSIC_COMPONENT_RMS_RATIO_MIN,
)
from release_geometry import release_package_receipt_issues
from keying_quality import keying_preset_lock_issues
from story_requirements import validate_projection
from story_video_synthesizer.media import probe_duration

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
    if item.get("kind") == "directory":
        from story_production_v2 import current
        return current(item)
    if not path.is_file():
        raise ValueError(f"{label}绑定文件不存在：{path}")
    if file_sha256(path) != expected:
        raise ValueError(f"{label}绑定文件哈希漂移：{path}")
    return path


def _require_empty_list(payload: Mapping[str, Any], key: str, label: str) -> None:
    value = payload.get(key)
    if not isinstance(value, list) or value:
        raise ValueError(f"{label}要求 {key}=[]")


def _member_bindings(payload: Mapping[str, Any], label: str) -> dict[Path, str]:
    members = payload.get("artifacts")
    if isinstance(members, dict):
        members = list(members.values())
    if not isinstance(members, list) or not members:
        raise ValueError(f"{label}缺少逐文件哈希清单")
    result: dict[Path, str] = {}
    for item in members:
        if not isinstance(item, dict):
            raise ValueError(f"{label}成员格式无效")
        path = _require_current_binding(item, label)
        if path in result:
            raise ValueError(f"{label}重复列出文件：{path}")
        if item.get("kind") == "directory":
            for member in item['members']:
                child = path / member['relative_path']
                if child in result:
                    raise ValueError(f"{label}重复列出文件：{child}")
                result[child] = member['sha256']
        else:
            result[path] = str(item["sha256"]).lower()
    return result


def _validate_final_review_coverage(
    reviewed_path: Path, registered: Mapping[str, Mapping[str, Any]] | None,
) -> None:
    if registered is None:
        raise ValueError("最终独立审核需要当前账本产物，不能脱离当前交付集合验收")
    required: dict[Path, str] = {}
    checklist_members: dict[Path, str] = {}
    release_names = {"main_release_video": "主账号发布视频.mp4", "library_release_video": "宝库号发布视频.mp4"}
    for name in ("final_delivery_checklist", "main_release_video", "library_release_video", "qa_release_report"):
        item = registered.get(name)
        if not isinstance(item, Mapping):
            raise ValueError(f"最终独立审核缺少账本目标 {name}；先登记交付清单和发布 QA")
        path = _require_current_binding(item, f"最终审核目标 {name}")
        required[path] = str(item["sha256"]).lower()
        if name == "final_delivery_checklist":
            checklist = validate_final_delivery_checklist(path)
            checklist_members = _member_bindings(checklist, "当前交付清单")
            required.update(checklist_members)
        elif name in release_names:
            if (path not in checklist_members or path.name != release_names[name]
                or path.parent.name != "04_发布视频"):
                raise ValueError("最终独立审核：账本当前发布视频与交付清单中的正式文件不一致")
    bundle = _load_json_object(reviewed_path, "最终审核 bundle")
    reviewed = _member_bindings(bundle, "最终审核 bundle")
    if any(reviewed.get(path) != digest for path, digest in required.items()):
        raise ValueError("最终独立审核未覆盖当前交付清单、成员或发布 QA；旧版本审核不能用于新交付")


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


def _registered_checklist_members(
    registered: Mapping[str, Mapping[str, Any]] | None,
) -> dict[Path, str] | None:
    if registered is None:
        return None
    record = registered.get("final_delivery_checklist")
    if not isinstance(record, Mapping):
        return None
    path = _require_current_binding(record, "当前最终交付清单")
    checklist = validate_final_delivery_checklist(path)
    return _member_bindings(checklist, "当前最终交付清单")


def _validate_customer_media_independent_evidence(
    payload: Mapping[str, Any],
    registered: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
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
    if registered is not None:
        receipt_record = registered.get("customer_media_receipt")
        if not isinstance(receipt_record, Mapping):
            raise ValueError(f"{label}缺少账本当前 customer_media_receipt")
        receipt_path = _require_current_binding(receipt_record, f"{label} 当前客户媒体回执")
        receipt = validate_customer_media_receipt(receipt_path)
        expected_demo = receipt["artifacts"]["product_demo"]
        demo = bindings["demo_video"]
        if (
            Path(str(demo.get("path") or "")).expanduser().resolve()
            != Path(str(expected_demo.get("path") or "")).expanduser().resolve()
            or demo.get("sha256") != expected_demo.get("sha256")
        ):
            raise ValueError(f"{label}的 Demo 不是账本当前客户媒体回执中的正式示范视频")
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
    reviewer_context = payload.get("reviewer_context")
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
        _validate_customer_media_independent_evidence(payload, registered_artifacts)
    if artifact_id == "final_delivery_review":
        if (
            not isinstance(reviewer_context, str)
            or not reviewer_context.strip()
            or payload.get("independent_context") is not True
            or not isinstance(independence, dict)
            or independence.get("producer_claims_trusted") is not False
        ):
            raise ValueError(f"{label}没有独立上下文证据")
        if registered_artifacts is None:
            raise ValueError(f"{label}缺少账本正式发布上下文")
        release_record = registered_artifacts.get("release_package_receipt")
        if not isinstance(release_record, Mapping):
            raise ValueError(f"{label}缺少账本当前 release_package_receipt")
        release_path = _require_current_binding(release_record, f"{label}正式发布回执")
        release_payload = _load_json_object(release_path, f"{label}正式发布回执")
        producer_context = str(
            (release_payload.get("actual_geometry") or {}).get("producer_context") or ""
        ).strip()
        declared_producer = str(independence.get("producer_context") or "").strip()
        if (
            not producer_context
            or declared_producer != producer_context
            or reviewer_context.strip() == producer_context
        ):
            raise ValueError(f"{label}审核者与账本正式发布生产上下文未独立绑定")
        _validate_final_review_coverage(reviewed_path, registered_artifacts)
        # The final review now owns the former customer-media review's unique
        # audio/subtitle/logo/presenter evidence.  New runs do not create a
        # second independent review for the same delivered files.
        if payload.get("schema_version") == "final-delivery-independent-review/v2":
            _validate_customer_media_independent_evidence(payload, registered_artifacts)
    if artifact_id == "keying_visual_review" and registered_artifacts is not None:
        lock_record = registered_artifacts.get("keying_preset_lock")
        if not isinstance(lock_record, Mapping):
            raise ValueError("抠像审核缺少账本当前 keying_preset_lock")
        lock_path = _require_current_binding(lock_record, "抠像审核当前锁")
        lock = _load_json_object(lock_path, "抠像审核当前锁")
        if (
            Path(str(lock.get("review_path") or "")).expanduser().resolve() != path.resolve()
            or str(lock.get("review_sha256") or "").lower() != file_sha256(path)
        ):
            raise ValueError("抠像审核与账本当前锁不匹配")
    return payload


def validate_machine_qa(
    artifact_id: str, path: Path, *,
    registered_artifacts: Mapping[str, Mapping[str, Any]] | None = None,
    registered_inputs: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    label = f"机器 QA {artifact_id}"
    payload = _load_json_object(path, label)
    if artifact_id == "qa_product_report" and payload.get("schema_version") == "story-managed-package/v2":
        from story_production_v2 import validate_managed_receipt
        result = validate_managed_receipt(path, registered_inputs)
        _validate_product_qa_against_checklist(result, registered_artifacts)
        return result
    expected_schemas = {
        "r2v_group_machine_qa": "story-r2v-group-machine-qa-v1",
        "qa_product_report": "story-product-machine-qa/v2",
        "qa_publish_report": "story-publish-machine-qa/v2",
        "qa_release_report": "story-release-machine-qa/v3",
    }
    if payload.get("schema_version") != expected_schemas[artifact_id]:
        raise ValueError(f"{label}类型错误；必须使用 {expected_schemas[artifact_id]}")
    if payload.get("passed") is not True:
        raise ValueError(f"{label}未 passed=true")
    _require_empty_list(payload, "critical_errors", label)
    errors = payload.get("errors")
    if errors not in (None, []) and (not isinstance(errors, list) or errors):
        raise ValueError(f"{label}仍有 errors")
    if artifact_id == "qa_release_report":
        _validate_release_audio_qa(payload, registered_artifacts, registered_inputs)
    elif artifact_id == "r2v_group_machine_qa":
        _validate_r2v_group_qa(payload)
    elif artifact_id == "qa_product_report":
        _validate_named_machine_members(payload, label, minimum=15)
        _validate_product_qa_against_checklist(payload, registered_artifacts)
    elif artifact_id == "qa_publish_report":
        _validate_named_machine_members(payload, label, minimum=8)
        required = {
            *(f"{account}:cover_{ratio}" for account in ("main", "library") for ratio in ("3x4", "4x3", "16x9")),
            "main:copy", "library:copy",
        }
        if not required <= set(payload["artifacts"]):
            raise ValueError(f"{label}缺少六封面或双账号文案的实际绑定")
        _validate_publish_qa_against_checklist(payload, registered_artifacts)
    return payload


def _validate_named_machine_members(
    payload: Mapping[str, Any], label: str, *, minimum: int, exact: int | None = None,
) -> None:
    members = payload.get("artifacts")
    if not isinstance(members, dict) or len(members) < minimum:
        raise ValueError(f"{label}缺少完整成员集合")
    if exact is not None and len(members) != exact:
        raise ValueError(f"{label}成员数应为 {exact}，实际 {len(members)}")
    for name, binding in members.items():
        if not isinstance(binding, Mapping):
            raise ValueError(f"{label} artifacts.{name} 格式无效")
        _require_current_binding(binding, f"{label} artifacts.{name}")


def _validate_product_qa_against_checklist(
    payload: Mapping[str, Any],
    registered: Mapping[str, Mapping[str, Any]] | None,
) -> None:
    checklist = _registered_checklist_members(registered)
    if checklist is None:
        return
    expected = {
        path: digest for path, digest in checklist.items()
        if any(re.search(r"[（(](?:基础版|进阶版)[）)]$", parent.name) for parent in path.parents)
    }
    actual = _member_bindings(payload, "机器 QA qa_product_report")
    if actual != expected:
        raise ValueError("资料包机器 QA 未绑定最终交付清单中当前实际成员")


def _validate_publish_qa_against_checklist(
    payload: Mapping[str, Any],
    registered: Mapping[str, Mapping[str, Any]] | None,
) -> None:
    checklist = _registered_checklist_members(registered)
    if checklist is None:
        return
    for account in ("main", "library"):
        for role in ("copy", "cover_3x4", "cover_4x3", "cover_16x9"):
            key = f"{account}:{role}"
            binding = payload["artifacts"][key]
            suffix = (
                ("publish_package", account, "copy.md") if role == "copy"
                else ("publish_package", account, "covers", f"{role}.png")
            )
            matches = [
                (path, digest) for path, digest in checklist.items()
                if path.parts[-len(suffix):] == suffix
            ]
            if len(matches) != 1:
                raise ValueError(f"发布机器 QA 在最终清单中无法唯一定位 {key}")
            expected_path, expected_sha = matches[0]
            if (
                Path(str(binding.get("path") or "")).expanduser().resolve() != expected_path
                or binding.get("sha256") != expected_sha
            ):
                raise ValueError(f"发布机器 QA {key} 不是最终交付清单中的当前成员")


def _validate_customer_receipt_against_checklist(
    payload: Mapping[str, Any],
    registered: Mapping[str, Mapping[str, Any]] | None,
) -> None:
    """Bind customer-media sources to the files that are actually delivered.

    The receipt is normally created before the product package is copied, so
    source and delivery paths may differ.  Content hashes, however, must match
    the exact product roles in the final checklist.
    """
    from product_quality import _role_for_product_file

    checklist = _registered_checklist_members(registered)
    if checklist is None:
        return
    advanced = {
        _role_for_product_file(path.name): digest
        for path, digest in checklist.items()
        if re.search(r"[（(]进阶版[）)]$", path.parent.name)
    }
    base = {
        _role_for_product_file(path.name): digest
        for path, digest in checklist.items()
        if re.search(r"[（(]基础版[）)]$", path.parent.name)
    }
    expected_roles = {
        "product_demo": "demo",
        "product_background_with_subtitles": "background_video_with_subtitles",
        "product_background_without_subtitles": "background_video_without_subtitles",
        "product_a_only_background": "a_only_video",
    }
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ValueError("客户媒体回执缺少 artifacts")
    for receipt_role, product_role in expected_roles.items():
        binding = artifacts.get(receipt_role)
        if not isinstance(binding, Mapping):
            raise ValueError(f"客户媒体回执缺少 {receipt_role}")
        digest = str(binding.get("sha256") or "").lower()
        expected = advanced.get(product_role)
        if not expected or digest != expected:
            raise ValueError(f"客户媒体回执 {receipt_role} 未绑定最终进阶版对应文件")
        if receipt_role == "product_demo" and base.get("demo") != digest:
            raise ValueError("客户媒体回执 product_demo 未同时绑定基础版与进阶版示范视频")


def _validate_r2v_group_qa(payload: Mapping[str, Any]) -> None:
    label = "机器 QA r2v_group_machine_qa"
    for name in ("plan", "receipt"):
        binding = {"path": payload.get(f"{name}_path"), "sha256": payload.get(f"{name}_sha256")}
        _require_current_binding(binding, f"{label} {name}")
    expected = payload.get("expected_shots")
    checked = payload.get("checked_shots")
    clips = payload.get("clips")
    if (
        isinstance(expected, bool) or not isinstance(expected, int) or expected <= 0
        or checked != expected or not isinstance(clips, list) or len(clips) != expected
    ):
        raise ValueError(f"{label}镜头成员数不完整")
    shot_ids: set[str] = set()
    for index, clip in enumerate(clips):
        if not isinstance(clip, Mapping):
            raise ValueError(f"{label} clips[{index}] 格式无效")
        shot_id = str(clip.get("shot_id") or "").strip()
        if not shot_id or shot_id in shot_ids:
            raise ValueError(f"{label}镜头 ID 缺失或重复")
        shot_ids.add(shot_id)
        _require_empty_list(clip, "issues", f"{label} {shot_id}")
        output = clip.get("output") or clip.get("video")
        if not isinstance(output, Mapping):
            output = {"path": clip.get("path"), "sha256": clip.get("sha256")}
        _require_current_binding(output, f"{label} {shot_id} 输出")


def _positive_duration(path: Path) -> float:
    try:
        duration = float(probe_duration(path))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise ValueError(f"无法核验完整节目时长：{path}") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"完整节目时长无效：{path}")
    return duration


def _validate_release_audio_qa(
    payload: Mapping[str, Any], registered: Mapping[str, Mapping[str, Any]] | None,
    inputs: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    if payload.get("schema_version") != "story-release-machine-qa/v3":
        raise ValueError("发布 QA 必须使用带音轨内容证据的 story-release-machine-qa/v3；请重新运行机器 QA")
    contract = payload.get("audio_contract")
    if not isinstance(contract, dict) or contract.get("required_audio_role") != "narration_plus_music":
        raise ValueError("发布 QA 缺少旁白+配乐音频合同")
    sources = {}
    for name in ("narration", "music_bed"):
        binding = contract.get(name)
        if not isinstance(binding, dict):
            raise ValueError(f"发布 QA 缺少 {name} 绑定")
        sources[name] = _require_current_binding(binding, f"发布 QA {name}")
    if inputs is not None:
        current_audio = inputs.get("audio")
        if not isinstance(current_audio, Mapping):
            raise ValueError("发布 QA 缺少账本权威完整音频")
        if (sources["narration"] != _require_current_binding(current_audio, "发布 QA 权威完整音频")
            or str(contract["narration"]["sha256"]).lower() != str(current_audio["sha256"]).lower()):
            raise ValueError("发布 QA 旁白没有绑定账本权威完整音频")
    if inputs is not None and "finished_music" in inputs:
        music = inputs["finished_music"]
        if sources["music_bed"] != _require_current_binding(music, "用户成品音乐") or contract["music_bed"]["sha256"] != music["sha256"]:
            raise ValueError("发布 QA 配乐没有绑定用户成品音乐")
    duration = _positive_duration(sources["narration"])
    tolerance = RELEASE_AUDIO_DURATION_TOLERANCE_SECONDS
    if _positive_duration(sources["music_bed"]) + tolerance < duration:
        raise ValueError("发布 QA 配乐参考未覆盖完整口播时长")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {"main_release_video", "library_release_video"}:
        raise ValueError("发布 QA 必须绑定双账号当前视频")
    bound = _member_bindings(payload, "发布 QA")
    for name, binding in artifacts.items():
        if registered is not None:
            current = registered.get(name)
            if not isinstance(current, Mapping):
                raise ValueError(f"发布 QA 缺少账本目标 {name}；先登记视频")
            current_path = _require_current_binding(current, f"发布 QA 当前 {name}")
            if current_path != Path(binding["path"]).expanduser().resolve() or bound.get(current_path) != str(current["sha256"]).lower():
                raise ValueError(f"发布 QA 未绑定当前 {name}")
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 2:
        raise ValueError("发布 QA 缺少双账号音轨检测结果")
    seen: set[Path] = set()
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("发布 QA 音轨检测结果格式无效")
        path = Path(str(result.get("path") or "")).expanduser().resolve()
        if path not in bound or path in seen:
            raise ValueError("发布 QA 音轨结果重复或没有对应当前视频")
        seen.add(path)
        actual_duration = _positive_duration(path)
        try:
            recorded_duration = float(result["duration_sec"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("发布 QA 缺少有效时长证据") from exc
        if (not math.isfinite(recorded_duration)
            or abs(actual_duration - duration) > tolerance
            or abs(actual_duration - recorded_duration) > tolerance):
            raise ValueError("发布 QA 成片未覆盖完整口播时长或时长证据失效")
        _require_empty_list(result, "issues", "发布 QA 音轨结果")
        fit = result.get("audio_role_fit")
        if result.get("audio_role") != "narration_plus_music" or not isinstance(fit, dict) or fit.get("passed") is not True:
            raise ValueError("发布 QA 缺少旁白+配乐通过证据")
        try:
            values = {key: float(fit[key]) for key in (
                "voice_gain", "music_gain", "voice_component_rms_ratio",
                "music_component_rms_ratio", "residual_energy_ratio", "rms",
            )}
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("发布 QA 音轨指标不完整") from exc
        if (not all(math.isfinite(value) for value in values.values())
            or values["voice_gain"] <= 0 or values["music_gain"] <= 0
            or values["rms"] <= 1e-5
            or values["voice_component_rms_ratio"] < NARRATION_COMPONENT_RMS_RATIO_MIN
            or values["music_component_rms_ratio"] < MUSIC_COMPONENT_RMS_RATIO_MIN
            or not 0 <= values["residual_energy_ratio"] <= NARRATION_MUSIC_RESIDUAL_MAX):
            raise ValueError("发布 QA 音轨指标未通过旁白+配乐合同")


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
    if json.loads(path.read_text()).get("production_contract") == "story-production/v2":
        from story_production_v2 import validate_checklist
        return validate_checklist(path)
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
    members = _member_bindings(payload, label)
    _validate_delivery_members(set(members))
    return payload


def _validate_delivery_members(members: set[Path]) -> None:
    """Count actual canonical deliverables, including PPTs inside the package.

    Existing v1 checklists have no role fields. Derive roles from the same
    public filenames the packager already writes, so intact old deliveries
    need no rewritten checklist or review. Internal evidence is not counted.
    """
    from product_quality import _role_for_product_file

    release_names = {"主账号发布视频.mp4", "宝库号发布视频.mp4"}
    releases = {p for p in members if p.name in release_names and p.parent.name == "04_发布视频"}
    release_dirs = {p.parent for p in releases}
    if len(releases) != 2 or len(release_dirs) != 1:
        raise ValueError("交付矩阵：实际双账号发布视频不完整或来自不同项目")
    release_dir = next(iter(release_dirs))
    project = release_dir.parent
    publish = release_dir / "publish_package"
    required_publish = {
        publish / account / "covers" / f"cover_{ratio}.png"
        for account in ("main", "library") for ratio in ("3x4", "4x3", "16x9")
    } | {publish / account / "copy.md" for account in ("main", "library")}
    if not required_publish <= members:
        raise ValueError("交付矩阵：实际六张封面或双账号文案不完整")
    base_roles = {
        "customer_manuscript": ".docx", "music": ".mp3",
        "reading_annotation": ".docx", "demo": ".mp4", "background_image": ".png",
    }
    advanced_roles = {
        **base_roles, "background_video_with_subtitles": ".mp4",
        "background_video_without_subtitles": ".mp4", "a_only_video": ".mp4",
        "ppt_with_subtitles": ".pptx", "ppt_without_subtitles": ".pptx",
    }
    package_roots = set()
    for variant, roles in (("基础版", base_roles), ("进阶版", advanced_roles)):
        files = {p for p in members if re.search(rf"[（(]{variant}[）)]$", p.parent.name)}
        directories = {p.parent for p in files}
        if len(files) != len(roles) or len(directories) != 1:
            raise ValueError(f"交付矩阵：{variant}实际文件数量或目录不完整")
        directory = next(iter(directories))
        if project not in directory.parents:
            raise ValueError(f"交付矩阵：{variant}不属于当前发布项目")
        package_roots.add(directory.parent)
        actual = {p.resolve() for p in directory.iterdir() if p.is_file() and p.name != ".DS_Store"}
        if actual != files:
            raise ValueError(f"交付矩阵：{variant}实际目录与交付清单不一致")
        found = [_role_for_product_file(p.name) for p in files]
        if len(set(found)) != len(found) or set(found) != set(roles):
            raise ValueError(f"交付矩阵：{variant}有重复角色或缺失必需文件")
        if any(p.suffix.lower() != roles[_role_for_product_file(p.name)] for p in files):
            raise ValueError(f"交付矩阵：{variant}文件格式不符合交付合同")
    if len(package_roots) != 1:
        raise ValueError("交付矩阵：基础版与进阶版必须来自同一交付目录")


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
        return validate_machine_qa(
            artifact_id, path, registered_artifacts=registered_artifacts,
            registered_inputs=registered_inputs,
        )
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
        payload = validate_customer_media_receipt(path)
        if registered_inputs and "finished_music" in registered_inputs:
            source = payload["music_source"]
            expected = registered_inputs["finished_music"]
            if _require_current_binding(source, "客户媒体音乐") != _require_current_binding(expected, "成品音乐") or source["sha256"] != expected["sha256"]:
                raise ValueError("客户媒体没有使用用户成品音乐")
            validate_authoritative_timeline_receipt(Path(payload["authoritative_timeline_receipt"]["path"]), expected_inputs=registered_inputs)
        _validate_customer_receipt_against_checklist(payload, registered_artifacts)
        return payload
    if artifact_id == "release_package_receipt":
        return validate_release_package_receipt(path)
    if artifact_id == "keying_preset_lock":
        payload = _load_json_object(path, "抠像预设锁")
        preset = Path(str(payload.get("preset_path") or "")).expanduser()
        issues = keying_preset_lock_issues(preset, path)
        if issues:
            raise ValueError("抠像预设锁失效：" + "；".join(issues))
        return payload
    if artifact_id == "requirements_projection":
        return validate_projection(path, registered_inputs=registered_inputs, registered_artifacts=registered_artifacts)
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
