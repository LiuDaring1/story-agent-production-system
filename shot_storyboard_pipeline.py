#!/usr/bin/env python3
"""Compile one reviewed per-shot storyboard source into PPT and R2V consumers.

The pipeline deliberately keeps the director plan, reviewed runtime assets and
one ImageGen illustration per director shot on a single hash-bound lineage.
The sealed storyboard manifest is then consumed by both the static PPT and the
Reference-to-Video job compiler; neither consumer is allowed to count images or
reconstruct a second shot list on its own.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from r2v_retry_policy import POLICY_VERSION


ASSET_BUNDLE_SCHEMA = "story-runtime-asset-bundle/v1"
STORYBOARD_MANIFEST_SCHEMA = "story-shot-storyboards/v1"
COMPILE_RECEIPT_SCHEMA = "story-shot-storyboard-compile/v1"
STORYBOARD_PROMPT_MARKER = "[SEMANTIC_STORYBOARD_V1]"
STORYBOARD_PROMPT_CLAUSE = (
    f"{STORYBOARD_PROMPT_MARKER} 最后一张参考图只用于理解本镜头完整句意、人物关系、"
    "动作阶段和大致空间方向；它不是视频首帧，不锁定像素、静止姿势、构图或镜位。"
)
OFFSCREEN_REVEAL_GUARD_MARKER = "[OFFSCREEN_REVEAL_GUARD_V1]"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
# The live image_gen tool rejects more than five explicit image paths.
IMAGEGEN_REFERENCE_LIMIT = 5


class StoryboardPipelineError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest_value(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def digest_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def single_line_ppt_subtitle(value: str) -> str:
    """Normalize one director-shot caption to the required PPT display line.

    A director shot may cover several source TXT cues.  The static PPT keeps
    one page per director shot, so its display caption concatenates those
    already-approved cue lines without inventing punctuation or retaining
    paragraph breaks.
    """

    return "".join(str(value).splitlines()).strip()


def file_sha256(path: Path) -> str:
    from story_hash_cache import sha256_file
    return sha256_file(path)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise StoryboardPipelineError("R2V 任务不能为空")
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def load_object(path: Path, label: str) -> dict[str, Any]:
    target = path.expanduser().resolve()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StoryboardPipelineError(f"{label}不可读：{target}") from exc
    if not isinstance(value, dict):
        raise StoryboardPipelineError(f"{label}顶层必须是对象：{target}")
    return value


def _r2v_validator():
    path = (
        Path(__file__).resolve().parent
        / "skills/story-r2v-director/scripts/validate_r2v_plan.py"
    )
    spec = importlib.util.spec_from_file_location("story_r2v_pipeline_validator", path)
    if spec is None or spec.loader is None:
        raise StoryboardPipelineError(f"无法加载 R2V 计划校验器：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_director_plan(plan: dict[str, Any]) -> None:
    errors = _r2v_validator().validate_plan(plan)
    if errors:
        raise StoryboardPipelineError("导演计划校验失败：" + "；".join(errors))


def _asset_record(asset: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(asset.get("path") or "")).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise StoryboardPipelineError(
            f"资产不存在或不是支持的栅格图：{asset.get('asset_id')} -> {path}"
        )
    actual = file_sha256(path)
    declared = str(asset.get("sha256") or "")
    if actual != declared:
        raise StoryboardPipelineError(
            f"资产哈希与导演计划不一致：{asset.get('asset_id')}"
        )
    return {
        "asset_id": str(asset.get("asset_id") or ""),
        "kind": str(asset.get("kind") or ""),
        "path": str(path),
        "sha256": actual,
        "identity_id": str(asset.get("identity_id") or ""),
        "prop_id": str(asset.get("prop_id") or ""),
        "state_id": str(asset.get("state_id") or ""),
        "continuity_role": str(asset.get("design_source_kind") or ""),
        "contract": {
            key: value
            for key, value in asset.items()
            if key not in {"path", "sha256"}
        },
    }


def _asset_bundle_payload(director_plan_path: Path) -> dict[str, Any]:
    director_path = director_plan_path.expanduser().resolve()
    director = load_object(director_path, "导演计划")
    validate_director_plan(director)
    assets = director.get("assets")
    if not isinstance(assets, list) or not assets:
        raise StoryboardPipelineError("导演计划缺少资产")
    from story_asset_efficiency import validate_aliases
    generation_plan = validate_aliases(director)
    omitted = {r['asset_id'] for r in generation_plan['assets'] if r['operation'] == 'omit_unconsumed'}
    records = [_asset_record(asset) for asset in assets if isinstance(asset, dict) and asset.get('asset_id') not in omitted]
    if any(not isinstance(asset, dict) for asset in assets):
        raise StoryboardPipelineError("导演计划 assets 包含非对象")
    continuity_groups = director.get("continuity_groups")
    if not isinstance(continuity_groups, list) or not continuity_groups:
        raise StoryboardPipelineError("导演计划缺少 continuity_groups")
    bundle_hash = digest_value(
        {
            "story_id": director.get("story_id"),
            "assets": records,
            "continuity_groups": continuity_groups,
        }
    )
    payload = {
        "schema_version": ASSET_BUNDLE_SCHEMA,
        "story_id": str(director.get("story_id") or ""),
        "director_plan_path": str(director_path),
        "director_plan_sha256": file_sha256(director_path),
        "asset_count": len(records),
        "generation_plan": generation_plan,
        "assets": records,
        "continuity_groups": continuity_groups,
        "asset_bundle_sha256": bundle_hash,
    }
    return payload


def create_asset_bundle(director_plan_path: Path, output_path: Path) -> dict[str, Any]:
    payload = _asset_bundle_payload(director_plan_path)
    write_json(output_path, payload)
    return payload


def validate_review(review: dict[str, Any], expected_hash: str, label: str) -> None:
    if review.get("approved") is not True:
        raise StoryboardPipelineError(f"{label}未通过")
    score = review.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool) or score < 85:
        raise StoryboardPipelineError(f"{label}分数必须至少为 85")
    critical = review.get("critical_errors")
    if not isinstance(critical, list) or critical:
        raise StoryboardPipelineError(f"{label} critical_errors 必须为空数组")
    if review.get("artifact_sha256") != expected_hash:
        raise StoryboardPipelineError(f"{label}未绑定当前被审产物哈希")


def _asset_map(director: dict[str, Any]) -> dict[str, dict[str, Any]]:
    assets = director.get("assets")
    if not isinstance(assets, list):
        raise StoryboardPipelineError("导演计划缺少 assets")
    mapping: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise StoryboardPipelineError("导演计划 asset 不是对象")
        asset_id = str(asset.get("asset_id") or "")
        if not asset_id or asset_id in mapping:
            raise StoryboardPipelineError(f"导演计划 asset_id 缺失或重复：{asset_id}")
        mapping[asset_id] = asset
    return mapping


def _storyboard_references(
    shot: dict[str, Any], assets: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for raw_id in shot.get("reference_asset_ids") or []:
        asset_id = str(raw_id)
        asset = assets.get(asset_id)
        if asset is None:
            raise StoryboardPipelineError(
                f"{shot.get('shot_id')} 引用了不存在的资产：{asset_id}"
            )
        if asset.get("kind") == "storyboard":
            raise StoryboardPipelineError(
                "故事板规划必须使用尚未注入故事板的基础导演计划"
            )
        refs.append(asset)
    optional_kinds = {"style"}
    while len(refs) > IMAGEGEN_REFERENCE_LIMIT:
        optional_index = next(
            (index for index in range(len(refs) - 1, -1, -1) if refs[index].get("kind") in optional_kinds),
            None,
        )
        if optional_index is None:
            raise StoryboardPipelineError(
                f"{shot.get('shot_id')} 的故事板需要 {len(refs)} 张必要资产，"
                f"超过 ImageGen {IMAGEGEN_REFERENCE_LIMIT} 张限制；应在导演阶段拆镜或精简身份关键内容"
            )
        refs.pop(optional_index)
    return refs


def _frame(shot: dict[str, Any], key: str) -> dict[str, Any]:
    value = shot.get(key)
    return value if isinstance(value, dict) else {}


def build_storyboard_prompt(
    shot: dict[str, Any],
    refs: list[dict[str, Any]],
    continuity_group: dict[str, Any] | None = None,
) -> str:
    opening = _frame(shot, "opening_frame")
    closing = _frame(shot, "closing_frame")
    camera = _frame(shot, "camera_plan")
    narrative = _frame(shot, "narrative_visualization")
    focus = _frame(shot, "focus_contract")
    group = continuity_group if isinstance(continuity_group, dict) else {}
    setup_id = str(camera.get("camera_setup_id") or "")
    setup = next(
        (
            row
            for row in group.get("camera_setups") or []
            if isinstance(row, dict) and row.get("setup_id") == setup_id
        ),
        {},
    )
    axis_id = str(camera.get("axis_id") or "")
    axis = next(
        (
            row
            for row in group.get("axes") or []
            if isinstance(row, dict) and row.get("axis_id") == axis_id
        ),
        {},
    )
    scene_zones = [
        row for row in group.get("zones") or [] if isinstance(row, dict)
    ]
    scene_anchors = [
        row for row in group.get("anchors") or [] if isinstance(row, dict)
    ]
    color_contract = group.get("color_contract") if isinstance(group.get("color_contract"), dict) else {}
    environment_view_asset_id = str(setup.get("environment_view_asset_id") or "")
    environment_view = next(
        (
            asset
            for asset in refs
            if isinstance(asset, dict) and asset.get("asset_id") == environment_view_asset_id
        ),
        {},
    )
    primary_beats = [
        {
            "start_second": beat.get("start_second"),
            "end_second": beat.get("end_second"),
            "primary_beat": beat.get("primary_beat"),
            "supporting_actions": beat.get("supporting_actions", []),
        }
        for beat in shot.get("performance_beats") or []
        if isinstance(beat, dict) and isinstance(beat.get("primary_beat"), dict)
    ]
    narrative_mode = str(narrative.get("mode") or "literal_action")
    if narrative_mode == "speech_visual_bubble":
        composition_rule = (
            "Composition: one coherent present-time tableau with exactly one clearly bounded, soft-edged, non-text visual speech bubble whose tail points to the current speaker. "
            "The speaker and reality anchor remain visibly outside the bubble. Content inside the bubble is a framed representation, not a second real event; a repeated identity is allowed only inside that bubble. "
            "No comics grid, multiple bubbles, captions or written words."
        )
        continuity_rule = (
            "Continuity: preserve the reviewed current factual state outside the bubble. Do not transfer the bubble content's location, wetness, damage, props or action into present reality. "
            "Any repeated identity must remain wholly inside the clearly framed bubble and must not read as a clone in the real scene."
        )
    else:
        composition_rule = (
            "Composition: one coherent cinematic tableau that communicates the entire sentence through readable cause and effect, character placement, gaze, contact and prop state. "
            "Use a decisive representative moment. For a changing or repeated action, show the clearest mid-action contact instead of pretending that one still image contains the whole temporal sequence. "
            "No collage, split screen, comics or multiple panels."
        )
        continuity_rule = (
            "Continuity: preserve the reviewed identities, wardrobe, relative scale, environment anchors, current prop state, material behavior and intended screen direction. "
            "Do not invent extra named characters or duplicate a character or prop."
        )
    reference_lines: list[str] = []
    for asset in refs:
        summary = str(
            asset.get("appearance_summary")
            or asset.get("state_id")
            or asset.get("population_id")
            or ""
        )
        reference_lines.append(
            f"- {asset.get('asset_id')} ({asset.get('kind')}): {summary}".rstrip(": ")
        )
    return "\n".join(
        [
            "Use case: illustration-story",
            f"Asset type: 16:9 semantic director storyboard for shot {shot.get('shot_id')}",
            "Primary request: Create one brand-new standalone story illustration from the reviewed assets. It will serve both the customer PPT and as the final semantic reference in an R2V request; it is not a video screenshot and not an I2V first frame.",
            f"Whole sentence: {shot.get('story_text', '')}",
            f"Director focus: {shot.get('visual_focus', '')}",
            f"Narrative visualization mode: {narrative_mode}",
            f"Narrative layer: {narrative.get('narrative_layer', 'current_fact')}",
            f"Present-reality anchor: {narrative.get('reality_anchor', '')}",
            f"Dialogue content to visualize: {narrative.get('content_to_visualize', '')}",
            f"Visualization entry cue: {narrative.get('entry_cue', '')}",
            f"Visualization exit cue: {narrative.get('exit_cue', '')}",
            f"PPT no-subtitle readability strategy: {narrative.get('ppt_readability_strategy', '')}",
            f"Duplicate identity policy: {narrative.get('duplicate_identity_policy', 'forbid')}",
            f"Opening beat: {opening.get('visual_focus', '')}; action phase={opening.get('action_phase', '')}",
            f"Closing beat: {closing.get('visual_focus', '')}; action phase={closing.get('action_phase', '')}",
            f"Required entry state: {json.dumps(shot.get('entry_state', {}), ensure_ascii=False, sort_keys=True)}",
            f"Required exit state: {json.dumps(shot.get('exit_state', {}), ensure_ascii=False, sort_keys=True)}",
            f"Camera intent: {camera.get('start_size', '')} to {camera.get('end_size', '')}; screen direction={camera.get('screen_direction', '')}; movement={camera.get('movement', '')}",
            f"Structured camera setup: {json.dumps(setup, ensure_ascii=False, sort_keys=True)}",
            f"Authoritative camera-view plate: {environment_view_asset_id}; {environment_view.get('appearance_summary', '')}",
            "The camera-view plate is authoritative for this shot's viewpoint and crop. Preserve its forward direction and furniture orientation; do not reconstruct the scene from a different master angle. Empty means character-free, not infrastructure-free: keep every venue anchor that the plate and visible_anchor_ids require.",
            f"Structured axis: {json.dumps(axis, ensure_ascii=False, sort_keys=True)}",
            f"Authoritative scene-map zones: {json.dumps(scene_zones, ensure_ascii=False, sort_keys=True)}",
            f"Fixed scene anchors and occupancy: {json.dumps(scene_anchors, ensure_ascii=False, sort_keys=True)}",
            f"Group color contract: {json.dumps(color_contract, ensure_ascii=False, sort_keys=True)}",
            f"Camera angle: {setup.get('camera_angle') or camera.get('camera_angle') or opening.get('camera_angle', '')}",
            f"Subject layout: {camera.get('subject_layout') or opening.get('subject_layout', '')}",
            f"Visible anchors: {json.dumps(camera.get('visible_anchor_ids', []), ensure_ascii=False)}",
            f"Excluded anchors: {json.dumps(camera.get('excluded_anchor_ids', []), ensure_ascii=False)}",
            "View-direction geometry is authoritative: place the camera in camera_origin_zone_id and look through look_target_zone_id. Only the declared background_zone_ids, which lie geometrically beyond the target on the scene map, may appear behind the primary subject. Never move an origin-side zone or anchor behind the subject just to make the composition fuller.",
            f"Focus contract: {json.dumps(focus, ensure_ascii=False, sort_keys=True)}",
            f"Subject presence contract: {json.dumps(shot.get('subject_presence', []), ensure_ascii=False, sort_keys=True)}",
            "World position and frame visibility are separate: an off-screen subject remains at its declared entry_world_zone_id/exit_world_zone_id. Off-screen never means absent from the scene. No on-screen character, crowd or anchor may come from a zone behind the camera. If a visible anchor is occupied by that subject, show the occupant; otherwise exclude the entire occupied anchor from frame.",
            f"Prop contracts: {json.dumps(shot.get('prop_contracts', []), ensure_ascii=False, sort_keys=True)}",
            f"One primary action per performance beat: {json.dumps(primary_beats, ensure_ascii=False, sort_keys=True)}",
            "Child-audience wardrobe default: adult characters wear complete, securely closed everyday clothing with the torso covered. Use an open-front or revealing design only when an explicitly reviewed story requirement says so.",
            "Framing priority: obey the director focus, shot size and subject layout before trying to show every referenced character. A speaker, listener reaction, prop detail or environmental beat may be the sole dominant subject; supporting characters may be cropped in the foreground or kept offscreen when the plan allows it. Do not default to an equal-size two-character wide shot merely because two character references are attached.",
            f"Eyeline and axis: {axis.get('description') or camera.get('axis') or opening.get('eyeline', '')}",
            f"Decisive storyboard moment: {opening.get('decisive_storyboard_moment') or closing.get('decisive_storyboard_moment') or opening.get('action_phase', '')}",
            "Reviewed references and their roles:",
            *reference_lines,
            composition_rule,
            continuity_rule,
            "Output: landscape 16:9, full bleed, polished children's story illustration, main action readable at thumbnail size.",
            "Avoid: text, subtitles, captions, logos, watermarks, borders, UI, wrong prop state, extra limbs, cloned characters, cloned props or unrelated decoration.",
        ]
    )


def build_storyboard_manifest(
    director_plan_path: Path,
    asset_bundle_path: Path,
    asset_review_path: Path,
    output_dir: Path,
    output_manifest: Path,
) -> dict[str, Any]:
    director_path = director_plan_path.expanduser().resolve()
    asset_bundle_file = asset_bundle_path.expanduser().resolve()
    asset_review_file = asset_review_path.expanduser().resolve()
    director = load_object(director_path, "导演计划")
    validate_director_plan(director)
    bundle = load_object(asset_bundle_file, "资产清单")
    if bundle.get("schema_version") != ASSET_BUNDLE_SCHEMA:
        raise StoryboardPipelineError("资产清单版本不受支持")
    if bundle.get("director_plan_sha256") != file_sha256(director_path):
        raise StoryboardPipelineError("资产清单绑定的导演计划已过期")
    current_bundle = _asset_bundle_payload(director_path)
    if bundle != current_bundle:
        raise StoryboardPipelineError("资产清单内容与当前导演资产不一致")
    review = load_object(asset_review_file, "资产独立审核")
    validate_review(review, str(current_bundle["asset_bundle_sha256"]), "资产独立审核")

    assets = _asset_map(director)
    continuity_groups = {
        str(group.get("group_id") or ""): group
        for group in director.get("continuity_groups") or []
        if isinstance(group, dict)
    }
    shots = director.get("shots")
    if not isinstance(shots, list) or not shots:
        raise StoryboardPipelineError("导演计划缺少 shots")
    target_dir = output_dir.expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    shot_ids: list[str] = []
    for shot in shots:
        if not isinstance(shot, dict):
            raise StoryboardPipelineError("导演计划 shot 不是对象")
        shot_id = str(shot.get("shot_id") or "")
        if not shot_id or shot_id in shot_ids:
            raise StoryboardPipelineError(f"导演 shot_id 缺失或重复：{shot_id}")
        shot_ids.append(shot_id)
        refs = _storyboard_references(shot, assets)
        prompt = build_storyboard_prompt(
            shot,
            refs,
            continuity_groups.get(str(shot.get("continuity_group") or "")),
        )
        target = target_dir / f"{shot_id}.png"
        ref_records = [
            {
                "asset_id": str(asset.get("asset_id") or ""),
                "kind": str(asset.get("kind") or ""),
                "path": str(Path(str(asset.get("path") or "")).expanduser().resolve()),
                "sha256": str(asset.get("sha256") or ""),
            }
            for asset in refs
        ]
        contains = [
            str(asset.get("identity_id"))
            for asset in refs
            if asset.get("kind") == "character" and asset.get("identity_id")
        ]
        entry = {
            "shot_index": len(entries) + 1,
            "shot_id": shot_id,
            "story_text": str(shot.get("story_text") or ""),
            "story_text_sha256": digest_text(str(shot.get("story_text") or "")),
            "director_shot_sha256": digest_value(shot),
            "image_path": str(target),
            "image_sha256": "",
            "width": 0,
            "height": 0,
            "status": "planned",
            "contains_characters": contains,
            "reference_assets": ref_records,
            "imagegen_prompt": prompt,
            "imagegen_prompt_sha256": digest_text(prompt),
        }
        entries.append(entry)
        requests.append(
            {
                "shot_id": shot_id,
                "target_path": str(target),
                "prompt": prompt,
                "reference_image_paths": [row["path"] for row in ref_records],
            }
        )
    payload = {
        "schema_version": STORYBOARD_MANIFEST_SCHEMA,
        "status": "planned",
        "story_id": str(director.get("story_id") or ""),
        "director_plan_path": str(director_path),
        "director_plan_sha256": file_sha256(director_path),
        "asset_bundle_path": str(asset_bundle_file),
        "asset_bundle_sha256": str(current_bundle["asset_bundle_sha256"]),
        "asset_review_path": str(asset_review_file),
        "asset_review_sha256": file_sha256(asset_review_file),
        "shot_count": len(entries),
        "ordered_shot_ids": shot_ids,
        "entries": entries,
        "imagegen_requests": requests,
        "storyboard_bundle_sha256": "",
    }
    write_json(output_manifest, payload)
    return payload


def _manifest_bundle_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "story_id": manifest.get("story_id"),
        "director_plan_sha256": manifest.get("director_plan_sha256"),
        "asset_bundle_sha256": manifest.get("asset_bundle_sha256"),
        "ordered_shot_ids": manifest.get("ordered_shot_ids"),
        "entries": [
            {
                "shot_id": row.get("shot_id"),
                "director_shot_sha256": row.get("director_shot_sha256"),
                "image_path": row.get("image_path"),
                "image_sha256": row.get("image_sha256"),
                "width": row.get("width"),
                "height": row.get("height"),
                "reference_assets": row.get("reference_assets"),
                "imagegen_prompt_sha256": row.get("imagegen_prompt_sha256"),
            }
            for row in manifest.get("entries") or []
        ],
    }


def seal_storyboard_manifest(input_manifest: Path, output_manifest: Path) -> dict[str, Any]:
    source = load_object(input_manifest, "逐镜故事板计划")
    if source.get("schema_version") != STORYBOARD_MANIFEST_SCHEMA:
        raise StoryboardPipelineError("逐镜故事板计划版本不受支持")
    director_path = Path(str(source.get("director_plan_path") or ""))
    if not director_path.is_file() or file_sha256(director_path) != source.get("director_plan_sha256"):
        raise StoryboardPipelineError("逐镜故事板计划绑定的导演计划已过期")
    asset_bundle_path = Path(str(source.get("asset_bundle_path") or ""))
    bundle = load_object(asset_bundle_path, "资产清单")
    if bundle.get("asset_bundle_sha256") != source.get("asset_bundle_sha256"):
        raise StoryboardPipelineError("逐镜故事板计划绑定的资产清单已过期")
    current_bundle = _asset_bundle_payload(director_path)
    if bundle != current_bundle:
        raise StoryboardPipelineError("逐镜故事板计划绑定的正式资产已变化")
    asset_review_path = Path(str(source.get("asset_review_path") or ""))
    review = load_object(asset_review_path, "资产独立审核")
    validate_review(review, str(source.get("asset_bundle_sha256") or ""), "资产独立审核")

    entries = source.get("entries")
    if not isinstance(entries, list) or not entries:
        raise StoryboardPipelineError("逐镜故事板计划没有 entries")
    sealed = deepcopy(source)
    for entry in sealed["entries"]:
        image_path = Path(str(entry.get("image_path") or "")).expanduser().resolve()
        if not image_path.is_file() or image_path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise StoryboardPipelineError(f"故事板图片缺失：{entry.get('shot_id')} -> {image_path}")
        with Image.open(image_path) as image:
            width, height = image.size
            image.verify()
        if width < 1280 or height < 700:
            raise StoryboardPipelineError(
                f"{entry.get('shot_id')} 故事板分辨率过低：{width}x{height}"
            )
        if abs((width / height) - (16 / 9)) > 0.04:
            raise StoryboardPipelineError(
                f"{entry.get('shot_id')} 故事板不是 16:9：{width}x{height}"
            )
        entry.update(
            {
                "image_path": str(image_path),
                "image_sha256": file_sha256(image_path),
                "width": width,
                "height": height,
                "status": "sealed",
            }
        )
    sealed["status"] = "sealed"
    sealed["sealed_at"] = utc_now()
    sealed["storyboard_bundle_sha256"] = digest_value(_manifest_bundle_payload(sealed))
    write_json(output_manifest, sealed)
    return sealed


def _verify_sealed_manifest(manifest_path: Path) -> dict[str, Any]:
    manifest = load_object(manifest_path, "封存故事板清单")
    if manifest.get("schema_version") != STORYBOARD_MANIFEST_SCHEMA or manifest.get("status") != "sealed":
        raise StoryboardPipelineError("故事板清单尚未封存")
    director_path = Path(str(manifest.get("director_plan_path") or ""))
    if not director_path.is_file() or file_sha256(director_path) != manifest.get("director_plan_sha256"):
        raise StoryboardPipelineError("故事板清单绑定的导演计划已过期")
    asset_bundle_path = Path(str(manifest.get("asset_bundle_path") or ""))
    asset_bundle = load_object(asset_bundle_path, "资产清单")
    if asset_bundle != _asset_bundle_payload(director_path):
        raise StoryboardPipelineError("故事板清单绑定的正式资产已变化")
    for entry in manifest.get("entries") or []:
        image_path = Path(str(entry.get("image_path") or ""))
        if not image_path.is_file() or file_sha256(image_path) != entry.get("image_sha256"):
            raise StoryboardPipelineError(f"故事板图片已变化：{entry.get('shot_id')}")
    expected = digest_value(_manifest_bundle_payload(manifest))
    if expected != manifest.get("storyboard_bundle_sha256"):
        raise StoryboardPipelineError("故事板清单 bundle 哈希无效")
    return manifest


def _prompt_with_storyboard(prompt: str) -> str:
    base = str(prompt or "").split(STORYBOARD_PROMPT_MARKER, 1)[0].rstrip()
    return f"{base}\n{STORYBOARD_PROMPT_CLAUSE}"


def _prompt_with_offscreen_reveal_guard(shot: dict[str, Any]) -> str:
    """Keep actor eyelines from turning into an unplanned camera reveal."""

    prompt = str(shot.get("prompt") or "")
    base = prompt.split(OFFSCREEN_REVEAL_GUARD_MARKER, 1)[0].rstrip()
    offscreen_ids = sorted(
        {
            str(row.get("subject_id") or "")
            for row in shot.get("subject_presence") or []
            if isinstance(row, dict)
            and (
                row.get("entry_presence") == "off_screen"
                or row.get("exit_presence") == "off_screen"
            )
            and str(row.get("subject_id") or "")
        }
    )
    if not offscreen_ids:
        return base
    camera = _frame(shot, "camera_plan")
    excluded = [str(value) for value in camera.get("excluded_anchor_ids") or []]
    return (
        f"{base}\n{OFFSCREEN_REVEAL_GUARD_MARKER} "
        "Looking, speaking, apologizing, pointing or gesturing toward an off-screen subject "
        "is an actor-only performance cue. The camera must not follow that eyeline, widen, "
        "rotate, pull back or reveal the off-screen zone. Preserve the planned camera movement "
        "inside the current camera-view plate and keep every excluded anchor out of frame. "
        f"Off-screen subjects={json.dumps(offscreen_ids, ensure_ascii=False)}; "
        f"excluded anchors={json.dumps(excluded, ensure_ascii=False)}."
    )


def compile_provider_prompt(
    shot: dict[str, Any], ref_assets: list[dict[str, Any]]
) -> str:
    """Compile v4 director facts into the actual provider-facing instruction.

    Free prose remains useful for style and nuance, but it is appended after the
    locked projection and cannot replace camera, entry/exit, beat, prop, or
    reference-role facts.
    """

    camera = _frame(shot, "camera_plan")
    lines = [
        "[LOCKED_DIRECTOR_INTENT_V1] One continuous shot; do not cut inside the shot.",
        (
            "Audience meaning: "
            + str(shot.get("visual_focus") or shot.get("story_text") or "").strip()
        ),
        (
            "Camera: setup={setup}; axis={axis}; framing={start}->{end}; movement={movement}; "
            "screen_direction={direction}; visible_anchors={visible}; excluded_anchors={excluded}."
        ).format(
            setup=camera.get("camera_setup_id") or "declared setup",
            axis=camera.get("axis_id") or camera.get("axis") or "declared axis",
            start=camera.get("start_size") or "declared",
            end=camera.get("end_size") or "declared",
            movement=camera.get("movement") or "locked",
            direction=camera.get("screen_direction") or "declared",
            visible=json.dumps(camera.get("visible_anchor_ids") or [], ensure_ascii=False),
            excluded=json.dumps(camera.get("excluded_anchor_ids") or [], ensure_ascii=False),
        ),
        "Entry state: " + json.dumps(shot.get("entry_state") or {}, ensure_ascii=False, sort_keys=True),
        "Exit state: " + json.dumps(shot.get("exit_state") or {}, ensure_ascii=False, sort_keys=True),
    ]
    visualization = shot.get("narrative_visualization")
    if isinstance(visualization, dict):
        lines.append(
            "Narrative layer (current fact / memory / imagination / proposal): "
            + json.dumps(visualization, ensure_ascii=False, sort_keys=True)
        )
    presence_parts = []
    for item in shot.get("subject_presence") or []:
        if not isinstance(item, dict):
            continue
        presence_parts.append(
            "{subject}:{entry}@{entry_zone}->{exit}@{exit_zone};world={world_entry}@{world_zone}->{world_exit}".format(
                subject=item.get("subject_id"),
                entry=item.get("entry_presence"),
                entry_zone=item.get("entry_zone_id"),
                exit=item.get("exit_presence"),
                exit_zone=item.get("exit_zone_id"),
                world_entry=item.get("entry_world_presence"),
                world_zone=item.get("entry_world_zone_id"),
                world_exit=item.get("exit_world_presence"),
            )
        )
    if presence_parts:
        lines.append("Subject presence: " + " | ".join(presence_parts))
    beat_parts = []
    for beat in shot.get("performance_beats") or []:
        if not isinstance(beat, dict):
            continue
        primary = beat.get("primary_beat") if isinstance(beat.get("primary_beat"), dict) else {}
        support = beat.get("supporting_actions") if isinstance(beat.get("supporting_actions"), list) else []
        beat_parts.append(
            "{start}-{end}s PRIMARY {subject}: {action}; supporting={support}".format(
                start=beat.get("start_second"),
                end=beat.get("end_second"),
                subject=primary.get("subject_id"),
                action=primary.get("action") or beat.get("performance"),
                support=json.dumps(support, ensure_ascii=False, sort_keys=True),
            )
        )
    if beat_parts:
        lines.append("Ordered performance; only each interval's PRIMARY action leads: " + " | ".join(beat_parts))
    if shot.get("prop_contracts"):
        lines.append(
            "Prop physics and state: "
            + json.dumps(shot["prop_contracts"], ensure_ascii=False, sort_keys=True)
        )
    reference_roles = [
        {
            "asset_id": asset.get("asset_id"),
            "kind": asset.get("kind"),
            "identity_id": asset.get("identity_id"),
            "prop_id": asset.get("prop_id"),
            "state_id": asset.get("state_id"),
        }
        for asset in ref_assets
    ]
    lines.append("Reference image roles in upload order: " + json.dumps(reference_roles, ensure_ascii=False))
    free = str(shot.get("prompt") or "").split("[LOCKED_DIRECTOR_INTENT_V1]", 1)[0].strip()
    if free:
        lines.append("Style/performance nuance (cannot override locked facts): " + free)
    return _prompt_with_offscreen_reveal_guard({**shot, "prompt": "\n".join(lines)})


def _runtime_reference_assets(
    shot: dict[str, Any], ref_assets: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Avoid double-conditioning an exact recurring cohort at provider time.

    The population archetype remains a reviewed director/storyboard asset. Once a
    runtime semantic storyboard has fixed an exact recurring cohort and count,
    sending both images encourages providers to expand the cohort. Anonymous
    background crowds keep their population reference.
    """

    crowd = shot.get("crowd_plan") if isinstance(shot.get("crowd_plan"), dict) else {}
    mode = str(crowd.get("mode") or "")
    storyboard_mode = str(shot.get("storyboard_reference_mode") or "runtime")
    population_ids = {str(value) for value in crowd.get("population_asset_ids") or []}
    has_runtime_storyboard = (
        storyboard_mode == "runtime"
        and bool(ref_assets)
        and ref_assets[-1].get("kind") == "storyboard"
    )
    omitted: list[str] = []
    filtered = ref_assets
    if mode == "recurring_cohort" and has_runtime_storyboard and population_ids:
        filtered = []
        for asset in ref_assets:
            asset_id = str(asset.get("asset_id") or "")
            if asset.get("kind") == "population" and asset_id in population_ids:
                omitted.append(asset_id)
                continue
            filtered.append(asset)
    return filtered, {
        "policy_version": "story-r2v-runtime-reference-policy/v1",
        "crowd_mode": mode,
        "omitted_population_asset_ids": omitted,
        "reason": (
            "exact recurring cohort count and identities are bound by the reviewed runtime storyboard"
            if omitted
            else "no recurring-cohort population omission required"
        ),
    }


def compile_r2v_plan(
    director: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, Any]:
    compiled = deepcopy(director)
    shot_map = {str(row.get("shot_id") or ""): row for row in compiled.get("shots") or []}
    entry_map = {str(row.get("shot_id") or ""): row for row in manifest.get("entries") or []}
    if list(shot_map) != list(manifest.get("ordered_shot_ids") or []):
        raise StoryboardPipelineError("导演镜头顺序与故事板清单不一致")
    assets = compiled.get("assets")
    if not isinstance(assets, list):
        raise StoryboardPipelineError("导演计划 assets 无效")
    max_refs = int((compiled.get("policies") or {}).get("max_reference_images") or 7)
    for shot_id, shot in shot_map.items():
        entry = entry_map[shot_id]
        storyboard_id = f"storyboard__{shot_id}"
        refs = [
            str(value)
            for value in shot.get("reference_asset_ids") or []
            if not str(value).startswith("storyboard__")
        ]
        storyboard_reference_mode = str(
            shot.get("storyboard_reference_mode") or "runtime"
        ).strip()
        storyboard_reference_reason = str(
            shot.get("storyboard_reference_reason") or "default_runtime_reference"
        ).strip()
        include_storyboard = storyboard_reference_mode == "runtime"
        required_refs = len(refs) + (1 if include_storyboard else 0)
        if required_refs > max_refs:
            raise StoryboardPipelineError(
                f"{shot_id} 需要 {required_refs} 张运行时参考图，超过上限 {max_refs}；"
                "必须在导演阶段精简可选参考或拆镜，不能静默丢弃正式资产"
            )
        assets.append(
            {
                "asset_id": storyboard_id,
                "kind": "storyboard",
                "path": str(entry["image_path"]),
                "sha256": str(entry["image_sha256"]),
                "runtime_eligible": include_storyboard,
                "contains_characters": list(entry.get("contains_characters") or []),
                "design_source_kind": "semantic_storyboard",
                "appearance_summary": (
                    f"{shot_id} complete-sentence semantic guide; "
                    + (
                        "final runtime reference, not a first frame"
                        if include_storyboard
                        else "director and PPT evidence only"
                    )
                ),
            }
        )
        shot["storyboard_reference_mode"] = storyboard_reference_mode
        shot["storyboard_reference_reason"] = storyboard_reference_reason
        shot["reference_asset_ids"] = [*refs, storyboard_id] if include_storyboard else refs
        if include_storyboard:
            shot["prompt"] = _prompt_with_storyboard(str(shot.get("prompt") or ""))
    validate_director_plan(compiled)
    return compiled


def _performance_summary(shot: dict[str, Any]) -> str:
    focus = str(shot.get("visual_focus") or "").strip()
    if focus:
        return focus
    beats = shot.get("performance_beats")
    if isinstance(beats, list):
        for beat in beats:
            if isinstance(beat, dict) and str(beat.get("performance") or "").strip():
                return str(beat["performance"]).strip()
    return str(shot.get("story_text") or "").strip()


def build_r2v_jobs(
    compiled: dict[str, Any],
    manifest_path: Path,
    manifest: dict[str, Any],
    storyboard_review_path: Path,
) -> list[dict[str, str]]:
    assets = _asset_map(compiled)
    bundle_hash = str(manifest.get("storyboard_bundle_sha256") or "")
    manifest_hash = file_sha256(manifest_path.expanduser().resolve())
    review_path = storyboard_review_path.expanduser().resolve()
    review_hash = file_sha256(review_path)
    rows: list[dict[str, str]] = []
    for index, shot in enumerate(compiled.get("shots") or [], start=1):
        shot_id = str(shot.get("shot_id") or "")
        ref_assets = [assets[str(asset_id)] for asset_id in shot.get("reference_asset_ids") or []]
        storyboard_asset = assets.get(f"storyboard__{shot_id}")
        if storyboard_asset is None:
            raise StoryboardPipelineError(f"{shot_id} 缺少已封存语义故事板资产")
        storyboard_reference_mode = str(
            shot.get("storyboard_reference_mode") or "runtime"
        ).strip()
        if storyboard_reference_mode == "runtime":
            if not ref_assets or ref_assets[-1].get("kind") != "storyboard":
                raise StoryboardPipelineError(f"{shot_id} runtime 语义故事板不是最后一张参考图")
        elif any(asset.get("kind") == "storyboard" for asset in ref_assets):
            raise StoryboardPipelineError(f"{shot_id} director_only 语义故事板不得进入运行时参考图")
        ref_assets, runtime_reference_policy = _runtime_reference_assets(shot, ref_assets)
        reference_paths = [str(Path(str(asset["path"])).expanduser().resolve()) for asset in ref_assets]
        reference_hashes = [str(asset.get("sha256") or "") for asset in ref_assets]
        runtime_reference_ids = [str(asset.get("asset_id") or "") for asset in ref_assets]
        camera = _frame(shot, "camera_plan")
        source_start = float(shot.get("source_start") or 0)
        source_end = float(shot.get("source_end") or 0)
        rows.append(
            {
                "scene": f"{index:02d}",
                "shot_id": shot_id,
                "generation_mode": "reference_to_video",
                "image_filename": Path(str(storyboard_asset["path"])).name,
                "source_image": str(storyboard_asset["path"]),
                "storyboard_image_path": str(storyboard_asset["path"]),
                "storyboard_reference_mode": storyboard_reference_mode,
                "storyboard_reference_reason": str(
                    shot.get("storyboard_reference_reason") or "default_runtime_reference"
                ),
                "story_text": str(shot.get("story_text") or ""),
                "visual_description": str(shot.get("visual_focus") or ""),
                "prompt": compile_provider_prompt(shot, ref_assets),
                "provider_prompt_compiler": "story-r2v-provider-prompt/v1",
                "subject_action": _performance_summary(shot),
                "camera_motion": str(camera.get("movement") or ""),
                "camera_setup_id": str(camera.get("camera_setup_id") or ""),
                "axis_id": str(camera.get("axis_id") or camera.get("axis") or ""),
                "focus_contract_json": json.dumps(
                    shot.get("focus_contract") or {}, ensure_ascii=False, sort_keys=True
                ),
                "subject_presence_json": json.dumps(
                    shot.get("subject_presence") or [], ensure_ascii=False, sort_keys=True
                ),
                "prop_contracts_json": json.dumps(
                    shot.get("prop_contracts") or [], ensure_ascii=False, sort_keys=True
                ),
                "performance_beats_json": json.dumps(
                    shot.get("performance_beats") or [], ensure_ascii=False, sort_keys=True
                ),
                "environment_motion": "环境保持连续，仅保留自然的风、光和背景运动。",
                "reference_image_paths_json": json.dumps(reference_paths, ensure_ascii=False),
                "reference_image_sha256_json": json.dumps(reference_hashes, ensure_ascii=False),
                "reference_asset_ids_json": json.dumps(
                    runtime_reference_ids, ensure_ascii=False
                ),
                "runtime_reference_policy_json": json.dumps(
                    runtime_reference_policy, ensure_ascii=False, sort_keys=True
                ),
                "storyboard_manifest_path": str(manifest_path.expanduser().resolve()),
                "storyboard_manifest_sha256": manifest_hash,
                "storyboard_bundle_sha256": bundle_hash,
                "storyboard_review_path": str(review_path),
                "storyboard_review_sha256": review_hash,
                "duration": f"{max(0.0, source_end - source_start):.6f}",
                "target_duration": f"{max(0.0, source_end - source_start):.6f}",
                "narration_start": f"{source_start:.6f}",
                "narration_end": f"{source_end:.6f}",
                "generation_duration": str(int(shot.get("provider_seconds") or 10)),
                "target_video_filename": f"{shot_id}.mp4",
                "status": "todo",
                "provider_attempt": "0",
                "retry_policy_version": POLICY_VERSION,
                "quality_retry_count": "0",
                "quality_version": "1",
                "retry_defect_code": "",
                "retry_evidence": "",
                "retry_root_cause": "",
                "retry_strategy": "",
                "retry_defect_severity": "",
                "v3_escalation_approved": "",
                "batch_retry_calibration_status": "not_required",
                "batch_retry_calibration_notes": "",
                "task_id": "",
                "video_url": "",
                "error": "",
                "notes": (
                    "semantic storyboard is the final reference and not a first frame"
                    if storyboard_reference_mode == "runtime"
                    else "semantic storyboard is director/PPT evidence and is not sent to the provider"
                ),
            }
        )
    return rows


def build_ppt_plan(
    previous: dict[str, Any], manifest: dict[str, Any], director: dict[str, Any]
) -> dict[str, Any]:
    old_slides = previous.get("slides")
    if not isinstance(old_slides, list) or not old_slides:
        raise StoryboardPipelineError("旧 PPT 计划缺少 slides")
    old_map = {
        str(row.get("shot_id") or ""): row for row in old_slides if isinstance(row, dict)
    }
    title = old_map.get("TITLE")
    if title is None:
        raise StoryboardPipelineError("旧 PPT 计划缺少 TITLE")
    director_map = {
        str(row.get("shot_id") or ""): row for row in director.get("shots") or []
    }
    slides = [{**title, "slide_index": 1, "poster_origin": "approved_title_art"}]
    for index, entry in enumerate(manifest.get("entries") or [], start=2):
        shot_id = str(entry.get("shot_id") or "")
        if shot_id not in director_map:
            raise StoryboardPipelineError(f"PPT 时长计划缺少导演镜头：{shot_id}")
        shot = director_map[shot_id]
        previous_slide = old_map.get(shot_id)
        source_duration = float(shot.get("source_end") or 0) - float(
            shot.get("source_start") or 0
        )
        duration_seconds = (
            float(previous_slide.get("duration_seconds") or source_duration)
            if previous_slide is not None
            else source_duration
        )
        slides.append(
            {
                **({k: previous_slide[k] for k in ("word_start", "word_text") if k in previous_slide} if previous_slide else {}),
                "slide_index": index,
                "shot_id": shot_id,
                "poster_path": str(entry.get("image_path") or ""),
                "poster_sha256": str(entry.get("image_sha256") or ""),
                "poster_origin": "imagegen_shot_illustration",
                "duration_seconds": duration_seconds,
                "subtitle": single_line_ppt_subtitle(
                    str(shot.get("story_text") or "")
                ),
                "semantic_card": False,
                "story_text_sha256": str(entry.get("story_text_sha256") or ""),
                "director_shot_sha256": str(entry.get("director_shot_sha256") or ""),
                "reference_assets": list(entry.get("reference_assets") or []),
                "imagegen_prompt": str(entry.get("imagegen_prompt") or ""),
                "imagegen_prompt_sha256": str(entry.get("imagegen_prompt_sha256") or ""),
                "shot_storyboard_bundle_sha256": str(
                    manifest.get("storyboard_bundle_sha256") or ""
                ),
            }
        )
    moral = old_map.get("MORAL")
    if moral is not None:
        slides.append({**moral, "slide_index": len(slides) + 1})
    return {
        "schema_version": "story-static-ppt-plan/v4",
        "story_name": str(previous.get("story_name") or director.get("story_id") or ""),
        "media_mode": "imagegen_shot_illustrations",
        "director_plan_path": str(manifest.get("director_plan_path") or ""),
        "director_plan_sha256": str(manifest.get("director_plan_sha256") or ""),
        "shot_storyboard_manifest_path": "",
        "shot_storyboard_bundle_sha256": str(manifest.get("storyboard_bundle_sha256") or ""),
        "music_path": previous.get("music_path"),
        "music_sha256": previous.get("music_sha256"),
        "slides": slides,
        "imagegen_requests": [],
    }


def compile_consumers(
    sealed_manifest_path: Path,
    storyboard_review_path: Path,
    output_r2v_plan: Path,
    output_jobs_csv: Path,
    output_receipt: Path,
    previous_ppt_plan: Path | None = None,
    output_ppt_plan: Path | None = None,
) -> dict[str, Any]:
    manifest_path = sealed_manifest_path.expanduser().resolve()
    review_path = storyboard_review_path.expanduser().resolve()
    manifest = _verify_sealed_manifest(manifest_path)
    review = load_object(review_path, "故事板独立审核")
    validate_review(
        review, str(manifest.get("storyboard_bundle_sha256") or ""), "故事板独立审核"
    )
    # Versioned production evidence must carry canonical provenance.  The
    # final ledger validator still rejects unversioned fixtures as delivery
    # evidence, while keeping lightweight historical compiler fixtures usable.
    if review.get("schema_version"):
        from story_production_v2 import review_provenance
        try:
            review_provenance(review, allow_legacy_storyboard=True)
        except ValueError as exc:
            raise StoryboardPipelineError("故事板独立审核缺少独立上下文证据") from exc
    director_path = Path(str(manifest.get("director_plan_path") or ""))
    director = load_object(director_path, "导演计划")
    compiled = compile_r2v_plan(director, manifest)
    write_json(output_r2v_plan, compiled)
    rows = build_r2v_jobs(compiled, manifest_path, manifest, review_path)
    write_csv(output_jobs_csv, rows)

    ppt_path_value = ""
    ppt_hash_value = ""
    if (previous_ppt_plan is None) != (output_ppt_plan is None):
        raise StoryboardPipelineError(
            "--previous-ppt-plan 与 --output-ppt-plan 必须同时提供"
        )
    if previous_ppt_plan is not None and output_ppt_plan is not None:
        previous = load_object(previous_ppt_plan, "旧 PPT 计划")
        ppt = build_ppt_plan(previous, manifest, director)
        ppt["shot_storyboard_manifest_path"] = str(manifest_path)
        write_json(output_ppt_plan, ppt)
        ppt_path_value = str(output_ppt_plan.expanduser().resolve())
        ppt_hash_value = file_sha256(output_ppt_plan.expanduser().resolve())

    receipt = {
        "schema_version": COMPILE_RECEIPT_SCHEMA,
        "compiled_at": utc_now(),
        "story_id": str(manifest.get("story_id") or ""),
        "director_plan_path": str(director_path),
        "director_plan_sha256": str(manifest.get("director_plan_sha256") or ""),
        "asset_bundle_sha256": str(manifest.get("asset_bundle_sha256") or ""),
        "storyboard_manifest_path": str(manifest_path),
        "storyboard_manifest_sha256": file_sha256(manifest_path),
        "storyboard_bundle_sha256": str(manifest.get("storyboard_bundle_sha256") or ""),
        "storyboard_review_path": str(review_path),
        "storyboard_review_sha256": file_sha256(review_path),
        "storyboard_review_artifact_id": "storyboard_review",
        "shot_count": int(manifest.get("shot_count") or 0),
        "ordered_shot_ids": list(manifest.get("ordered_shot_ids") or []),
        "r2v_plan_path": str(output_r2v_plan.expanduser().resolve()),
        "r2v_plan_sha256": file_sha256(output_r2v_plan.expanduser().resolve()),
        "r2v_jobs_csv_path": str(output_jobs_csv.expanduser().resolve()),
        "r2v_jobs_csv_sha256": file_sha256(output_jobs_csv.expanduser().resolve()),
        "ppt_plan_path": ppt_path_value,
        "ppt_plan_sha256": ppt_hash_value,
    }
    if review.get('schema_version'):
        from story_review_schema import source_review_provenance
        receipt['storyboard_review_provenance'] = source_review_provenance(review_path, allow_legacy_storyboard=True)
    write_json(output_receipt, receipt)
    return receipt


def validate_compile_receipt(
    path: Path,
    *,
    require_current_r2v_jobs: bool = True,
) -> dict[str, Any]:
    receipt = load_object(path, "逐镜故事板编译回执")
    if receipt.get("schema_version") != COMPILE_RECEIPT_SCHEMA:
        raise StoryboardPipelineError("逐镜故事板编译回执版本不受支持")
    shot_ids = receipt.get("ordered_shot_ids")
    if (
        not isinstance(shot_ids, list)
        or len(shot_ids) != int(receipt.get("shot_count") or 0)
        or len(set(str(value) for value in shot_ids)) != len(shot_ids)
    ):
        raise StoryboardPipelineError("逐镜故事板编译回执镜头清单无效")
    bindings = (
        ("storyboard_manifest_path", "storyboard_manifest_sha256", True),
        ("storyboard_review_path", "storyboard_review_sha256", True),
        ("r2v_plan_path", "r2v_plan_sha256", True),
        ("r2v_jobs_csv_path", "r2v_jobs_csv_sha256", True),
        ("ppt_plan_path", "ppt_plan_sha256", False),
    )
    for path_key, hash_key, required in bindings:
        raw_path = str(receipt.get(path_key) or "").strip()
        raw_hash = str(receipt.get(hash_key) or "").strip()
        if not raw_path and not required:
            if raw_hash:
                raise StoryboardPipelineError(f"{path_key} 为空但 {hash_key} 非空")
            continue
        target = Path(raw_path)
        if path_key == "r2v_jobs_csv_path" and not require_current_r2v_jobs:
            # The R2V runner writes provider status/result metadata back into
            # this CSV after the static PPT plan has already been compiled.
            # Static-PPT consumers bind the immutable director/manifest/plan
            # fields above and must not be invalidated by those job receipts.
            if not target.is_file() or len(raw_hash) != 64:
                raise StoryboardPipelineError(f"逐镜故事板编译回执绑定失效：{path_key}")
            continue
        if not target.is_file() or file_sha256(target) != raw_hash:
            raise StoryboardPipelineError(f"逐镜故事板编译回执绑定失效：{path_key}")
    if 'storyboard_review_provenance' in receipt:
        from story_review_schema import source_review_provenance
        expected = source_review_provenance(receipt['storyboard_review_path'], allow_legacy_storyboard=True)
        if receipt['storyboard_review_provenance'] != expected or receipt.get('storyboard_review_artifact_id') != 'storyboard_review':
            raise StoryboardPipelineError('故事板审核来源与规范产物 ID 不一致')
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把导演镜头、审核资产和逐镜故事板编译成同源 PPT 与 R2V 任务"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    assets = subparsers.add_parser("assets", help="校验导演资产并生成待审核资产清单")
    assets.add_argument("--director-plan", required=True, type=Path)
    assets.add_argument("--output", required=True, type=Path)

    plan = subparsers.add_parser("plan", help="资产审核通过后生成逐镜故事板 ImageGen 请求")
    plan.add_argument("--director-plan", required=True, type=Path)
    plan.add_argument("--asset-bundle", required=True, type=Path)
    plan.add_argument("--asset-review", required=True, type=Path)
    plan.add_argument("--output-dir", required=True, type=Path)
    plan.add_argument("--output-manifest", required=True, type=Path)

    seal = subparsers.add_parser("seal", help="逐镜图片生成后做机器预检并封存哈希")
    seal.add_argument("--input-manifest", required=True, type=Path)
    seal.add_argument("--output-manifest", required=True, type=Path)

    compile_parser = subparsers.add_parser(
        "compile", help="故事板独立审核后同时编译 R2V 计划、任务 CSV 和 PPT 计划"
    )
    compile_parser.add_argument("--sealed-manifest", required=True, type=Path)
    compile_parser.add_argument("--storyboard-review", required=True, type=Path)
    compile_parser.add_argument("--output-r2v-plan", required=True, type=Path)
    compile_parser.add_argument("--output-jobs-csv", required=True, type=Path)
    compile_parser.add_argument("--output-receipt", required=True, type=Path)
    compile_parser.add_argument("--previous-ppt-plan", type=Path)
    compile_parser.add_argument("--output-ppt-plan", type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "assets":
        payload = create_asset_bundle(args.director_plan, args.output)
    elif args.command == "plan":
        payload = build_storyboard_manifest(
            args.director_plan,
            args.asset_bundle,
            args.asset_review,
            args.output_dir,
            args.output_manifest,
        )
    elif args.command == "seal":
        payload = seal_storyboard_manifest(args.input_manifest, args.output_manifest)
    elif args.command == "compile":
        payload = compile_consumers(
            args.sealed_manifest,
            args.storyboard_review,
            args.output_r2v_plan,
            args.output_jobs_csv,
            args.output_receipt,
            args.previous_ppt_plan,
            args.output_ppt_plan,
        )
    else:  # pragma: no cover
        raise StoryboardPipelineError(f"未知命令：{args.command}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
