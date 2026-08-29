#!/usr/bin/env python3
"""Archived pre-storyboard ImageGen plan for historical project reproduction only."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_text(value: str) -> str:
    return digest_bytes(value.encode("utf-8"))


def digest_path(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是对象：{path}")
    return value


def build_prompt(shot: dict[str, Any], assets: list[dict[str, Any]]) -> str:
    refs: list[str] = []
    for asset in assets:
        label = str(asset.get("asset_id") or "")
        kind = str(asset.get("kind") or "reference")
        summary = str(asset.get("appearance_summary") or asset.get("state_id") or asset.get("population_id") or "")
        refs.append(f"- {label} ({kind})：{summary}".rstrip("："))
    camera = shot.get("camera_plan") if isinstance(shot.get("camera_plan"), dict) else {}
    opening = shot.get("opening_frame") if isinstance(shot.get("opening_frame"), dict) else {}
    closing = shot.get("closing_frame") if isinstance(shot.get("closing_frame"), dict) else {}
    crowd = shot.get("crowd_plan") if isinstance(shot.get("crowd_plan"), dict) else {}
    state = closing.get("state_summary") if isinstance(closing.get("state_summary"), dict) else {}
    return "\n".join([
        "Use case: illustration-story",
        f"Asset type: 16:9 full-bleed customer story-PPT illustration for director shot {shot.get('shot_id')}",
        "Primary request: Create a brand-new standalone story illustration, not a selected frame, not a video screenshot, and not an AutoViz/R2V still.",
        f"Whole sentence to communicate: {shot.get('story_text', '')}",
        f"Director narrative focus: {shot.get('visual_focus', '')}",
        f"Opening beat: {opening.get('visual_focus', '')}",
        f"Decisive resolved beat: {closing.get('visual_focus', '')}",
        f"Required final state: {stable_json(state)}",
        f"Camera/framing: begin from {camera.get('start_size', '')}, resolve as {camera.get('end_size', '')}; preserve the director's readable spatial relationship and screen direction {camera.get('screen_direction', '')}.",
        f"Crowd/count rule: mode={crowd.get('mode', 'none')}, target_count={crowd.get('target_count', 0)}; never invent extra named characters.",
        "Reference images and their roles:",
        *refs,
        "Composition: one coherent cinematic tableau that expresses the whole sentence through a clear cause-and-effect arrangement. Choose the decisive resolved moment while keeping the preceding action visually understandable through pose, gaze, object placement, and restrained motion cues. No collage, no split screen, no storyboard panels.",
        "Style/medium: polished warm 3D children's storybook illustration matching the supplied character, prop, population, and environment references; natural depth, expressive but believable faces, clean silhouettes, child-friendly.",
        "Output: landscape 16:9, full bleed, presentation-ready, main action readable at thumbnail size.",
        "Constraints: preserve referenced identities, clothing, relative scale, location, exact prop state, flower-petal state, and required population count. Do not reuse pixels from any prior generated story frame.",
        "Avoid: any written text, subtitles, captions, logos, watermarks, borders, UI, duplicated limbs, duplicated characters, extra petals, wrong prop state, or decorative elements that obscure the narrative action.",
    ])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--director-plan", required=True, type=Path)
    parser.add_argument("--previous-ppt-plan", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--output-plan", required=True, type=Path)
    parser.add_argument("--reference-overrides", type=Path)
    args = parser.parse_args()

    director = load_object(args.director_plan)
    previous = load_object(args.previous_ppt_plan)
    shots = director.get("shots")
    assets = director.get("assets")
    previous_slides = previous.get("slides")
    if not isinstance(shots, list) or not shots:
        raise ValueError("导演计划缺少 shots")
    if not isinstance(assets, list):
        raise ValueError("导演计划缺少 assets")
    if not isinstance(previous_slides, list) or not previous_slides:
        raise ValueError("旧静态 PPT 计划缺少 slides")
    asset_map = {str(row.get("asset_id") or ""): row for row in assets if isinstance(row, dict)}
    state_asset_map = {
        str(row.get("state_id") or ""): row
        for row in assets if isinstance(row, dict) and str(row.get("state_id") or "")
    }
    old_map = {str(row.get("shot_id") or ""): row for row in previous_slides if isinstance(row, dict)}
    overrides: dict[str, Any] = {}
    if args.reference_overrides:
        overrides = load_object(args.reference_overrides)
    title = old_map.get("TITLE")
    if not title:
        raise ValueError("旧静态 PPT 计划缺少 TITLE 页")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    slides: list[dict[str, Any]] = [{**title, "slide_index": 1, "poster_origin": "approved_title_art"}]
    requests: list[dict[str, Any]] = []
    for index, raw_shot in enumerate(shots, start=2):
        if not isinstance(raw_shot, dict):
            raise ValueError(f"导演镜头 {index - 1} 不是对象")
        shot_id = str(raw_shot.get("shot_id") or "")
        if not shot_id or shot_id not in old_map:
            raise ValueError(f"镜头缺失或没有旧时长映射：{shot_id}")
        refs: list[dict[str, Any]] = []
        for asset_id in raw_shot.get("reference_asset_ids") or []:
            asset = asset_map.get(str(asset_id))
            if not asset:
                raise ValueError(f"{shot_id} 引用了不存在的资产：{asset_id}")
            path = Path(str(asset.get("path") or ""))
            if not path.is_file():
                raise ValueError(f"{shot_id} 参考资产不存在：{path}")
            refs.append(asset)
        closing = raw_shot.get("closing_frame") if isinstance(raw_shot.get("closing_frame"), dict) else {}
        closing_state = closing.get("state_summary") if isinstance(closing.get("state_summary"), dict) else {}
        for state_value in closing_state.values():
            state_asset = state_asset_map.get(str(state_value))
            if state_asset and str(state_asset.get("asset_id") or "") not in {
                str(row.get("asset_id") or "") for row in refs
            }:
                refs.append(state_asset)
        override = overrides.get(shot_id) if isinstance(overrides.get(shot_id), dict) else {}
        replace_ids = {str(value) for value in override.get("replace_asset_ids") or []}
        if replace_ids:
            refs = [row for row in refs if str(row.get("asset_id") or "") not in replace_ids]
        for extra in override.get("add_references") or []:
            if not isinstance(extra, dict):
                raise ValueError(f"{shot_id} reference override 必须是对象")
            extra_path = Path(str(extra.get("path") or ""))
            if not extra_path.is_file():
                raise ValueError(f"{shot_id} 补充参考资产不存在：{extra_path}")
            refs.append({
                **extra,
                "sha256": str(extra.get("sha256") or digest_path(extra_path)),
                "kind": str(extra.get("kind") or "supporting_reference"),
            })
        # The built-in ImageGen reference contract accepts at most five files.
        # Director-declared references come first; resolved end-state assets and
        # reviewed per-shot overrides fill the remaining slots.
        refs = refs[:5]
        prompt = build_prompt(raw_shot, refs)
        prompt_append = str(override.get("prompt_append") or "").strip()
        if prompt_append:
            prompt = f"{prompt}\nTargeted visual clarification: {prompt_append}"
        target = args.output_dir / f"{shot_id}.png"
        old = old_map[shot_id]
        slides.append({
            "slide_index": index,
            "shot_id": shot_id,
            "poster_path": str(target),
            "poster_sha256": digest_path(target) if target.is_file() else "",
            "poster_origin": "imagegen_shot_illustration",
            "duration_seconds": float(old.get("duration_seconds") or 0),
            "subtitle": str(raw_shot.get("story_text") or ""),
            "semantic_card": False,
            "story_text_sha256": digest_text(str(raw_shot.get("story_text") or "")),
            "director_shot_sha256": digest_text(stable_json(raw_shot)),
            "reference_assets": [
                {
                    "asset_id": str(asset.get("asset_id") or ""),
                    "path": str(asset.get("path") or ""),
                    "sha256": str(asset.get("sha256") or digest_path(Path(str(asset.get("path") or "")))),
                    "role": str(asset.get("kind") or "reference"),
                }
                for asset in refs
            ],
            "imagegen_prompt": prompt,
            "imagegen_prompt_sha256": digest_text(prompt),
        })
        requests.append({
            "shot_id": shot_id,
            "target_path": str(target),
            "prompt": prompt,
            "reference_image_paths": [str(asset.get("path") or "") for asset in refs],
        })

    payload = {
        "schema_version": "story-static-ppt-plan/v4",
        "story_name": str(previous.get("story_name") or director.get("story_id") or ""),
        "media_mode": "imagegen_shot_illustrations",
        "director_plan_path": str(args.director_plan.resolve()),
        "director_plan_sha256": digest_path(args.director_plan),
        "music_path": previous.get("music_path"),
        "music_sha256": previous.get("music_sha256"),
        "slides": slides,
        "imagegen_requests": requests,
    }
    args.output_plan.parent.mkdir(parents=True, exist_ok=True)
    args.output_plan.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": "ok",
        "shot_count": len(requests),
        "generated_files_present": sum(Path(row["target_path"]).is_file() for row in requests),
        "output_plan": str(args.output_plan),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
