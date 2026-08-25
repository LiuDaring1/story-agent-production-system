#!/usr/bin/env python3
"""Validate semantic and production invariants for a story R2V plan."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "story-r2v-plan-v1"
BODY_MODE = "reference_to_video"
PROVIDER_DURATIONS = {6, 10}
RELATIONS = {
    "first",
    "same_scene_continuation",
    "same_scene_angle_change",
    "reaction_cut",
    "match_action",
    "new_scene",
}
RUNTIME_FORBIDDEN_SOURCES = {
    "three_view_sheet",
    "expression_sheet",
    "composite_scene",
}
EPSILON = 0.01
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _duplicates(values: list[Any]) -> set[Any]:
    seen: set[Any] = set()
    duplicate: set[Any] = set()
    for value in values:
        if value in seen:
            duplicate.add(value)
        seen.add(value)
    return duplicate


def _required_text(container: dict[str, Any], field: str, path: str, errors: list[str]) -> None:
    if not isinstance(container.get(field), str) or not container.get(field).strip():
        errors.append(f"{path}.{field}: must be a non-empty string")


def _required_sha256(container: dict[str, Any], field: str, path: str, errors: list[str]) -> None:
    value = container.get(field)
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        errors.append(f"{path}.{field}: must be a lowercase 64-character SHA-256")


def _index_by_id(items: Any, id_field: str, path: str, errors: list[str]) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list) or not items:
        errors.append(f"{path}: must be a non-empty array")
        return {}
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{item_path}: must be an object")
            continue
        item_id = item.get(id_field)
        if not isinstance(item_id, str) or not item_id.strip():
            errors.append(f"{item_path}.{id_field}: must be a non-empty string")
            continue
        if item_id in result:
            errors.append(f"{item_path}.{id_field}: duplicate id {item_id!r}")
            continue
        result[item_id] = item
    return result


def _validate_policies(plan: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    policies = plan.get("policies")
    if not isinstance(policies, dict):
        errors.append("policies: must be an object")
        return {}
    if policies.get("body_generation_mode") != BODY_MODE:
        errors.append(f"policies.body_generation_mode: must equal {BODY_MODE!r}")
    if policies.get("duration_choices") != [6, 10]:
        errors.append("policies.duration_choices: must equal [6, 10]")

    max_refs = policies.get("max_reference_images")
    if not isinstance(max_refs, int) or isinstance(max_refs, bool) or not 1 <= max_refs <= 7:
        errors.append("policies.max_reference_images: must be an integer from 1 to 7")

    min_ratio = policies.get("min_retime_ratio")
    max_ratio = policies.get("max_retime_ratio")
    if not _number(min_ratio) or not 0.5 <= min_ratio <= 1:
        errors.append("policies.min_retime_ratio: must be between 0.5 and 1")
    if not _number(max_ratio) or not 1 <= max_ratio <= 1.5:
        errors.append("policies.max_retime_ratio: must be between 1 and 1.5")
    if _number(min_ratio) and _number(max_ratio) and min_ratio > max_ratio:
        errors.append("policies: min_retime_ratio cannot exceed max_retime_ratio")

    attempts = policies.get("max_attempts_per_shot")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 2:
        errors.append("policies.max_attempts_per_shot: must be 1 or 2")
    return policies


def _validate_assets(assets: dict[str, dict[str, Any]], errors: list[str]) -> None:
    valid_kinds = {"character", "environment", "prop", "style"}
    for asset_id, asset in assets.items():
        path = f"assets[{asset_id!r}]"
        _required_text(asset, "path", path, errors)
        _required_sha256(asset, "sha256", path, errors)
        kind = asset.get("kind")
        if kind not in valid_kinds:
            errors.append(f"{path}.kind: must be one of {sorted(valid_kinds)}")
        if not isinstance(asset.get("runtime_eligible"), bool):
            errors.append(f"{path}.runtime_eligible: must be a boolean")
        contains = asset.get("contains_characters")
        if not isinstance(contains, list) or any(not isinstance(value, str) or not value for value in contains):
            errors.append(f"{path}.contains_characters: must be an array of non-empty identity ids")
            contains = []
        elif _duplicates(contains):
            errors.append(f"{path}.contains_characters: identities must be unique")

        if kind == "character":
            identity = asset.get("identity_id")
            if not isinstance(identity, str) or not identity:
                errors.append(f"{path}.identity_id: character assets require an identity id")
            elif contains != [identity]:
                errors.append(f"{path}.contains_characters: runtime character asset must contain only {identity!r}")
        elif kind == "environment" and contains:
            errors.append(f"{path}.contains_characters: environment assets must be empty scenes")
        elif kind == "prop":
            if not isinstance(asset.get("prop_id"), str) or not asset.get("prop_id"):
                errors.append(f"{path}.prop_id: prop assets require a prop id")
            if not isinstance(asset.get("state_id"), str) or not asset.get("state_id"):
                errors.append(f"{path}.state_id: prop assets require a state id")

        if asset.get("runtime_eligible") and asset.get("design_source_kind") in RUNTIME_FORBIDDEN_SOURCES:
            errors.append(
                f"{path}.design_source_kind: {asset.get('design_source_kind')!r} cannot be runtime eligible"
            )


def _validate_groups(
    groups: dict[str, dict[str, Any]], assets: dict[str, dict[str, Any]], errors: list[str]
) -> None:
    for group_id, group in groups.items():
        path = f"continuity_groups[{group_id!r}]"
        for field in ("location", "time_of_day", "lighting"):
            _required_text(group, field, path, errors)
        environment_id = group.get("environment_asset_id")
        environment = assets.get(environment_id)
        if environment is None:
            errors.append(f"{path}.environment_asset_id: unknown asset {environment_id!r}")
        elif environment.get("kind") != "environment":
            errors.append(f"{path}.environment_asset_id: must reference an environment asset")
        anchors = group.get("anchors")
        if not isinstance(anchors, list) or not anchors:
            errors.append(f"{path}.anchors: must contain at least one stable scene anchor")


def _validate_beats(
    shot: dict[str, Any], shot_path: str, selected_identities: set[str], errors: list[str]
) -> None:
    beats = shot.get("performance_beats")
    provider_seconds = shot.get("provider_seconds")
    if not isinstance(beats, list) or not beats:
        errors.append(f"{shot_path}.performance_beats: must be a non-empty array")
        return
    previous_end = 0.0
    for index, beat in enumerate(beats):
        path = f"{shot_path}.performance_beats[{index}]"
        if not isinstance(beat, dict):
            errors.append(f"{path}: must be an object")
            continue
        start = beat.get("start_second")
        end = beat.get("end_second")
        if not _number(start) or not _number(end) or start < 0 or end <= start:
            errors.append(f"{path}: start_second and end_second must form a positive interval")
            continue
        if abs(start - previous_end) > EPSILON:
            errors.append(f"{path}.start_second: beats must be contiguous; expected {previous_end:g}")
        previous_end = end
        visible = beat.get("visible_characters")
        if not isinstance(visible, list):
            errors.append(f"{path}.visible_characters: must be an array")
        else:
            unknown = set(visible) - selected_identities
            if unknown:
                errors.append(f"{path}.visible_characters: no selected character asset for {sorted(unknown)}")
        for field in ("source_text", "performance", "camera"):
            if not isinstance(beat.get(field), str) or not beat.get(field).strip():
                errors.append(f"{path}.{field}: must be a non-empty string")
    if _number(provider_seconds) and abs(previous_end - provider_seconds) > EPSILON:
        errors.append(
            f"{shot_path}.performance_beats: must cover the full {provider_seconds:g}s provider duration"
        )


def _validate_prop_transitions(
    shot: dict[str, Any],
    shot_path: str,
    selected_assets: list[dict[str, Any]],
    assets: dict[str, dict[str, Any]],
    errors: list[str],
) -> None:
    transitions = shot.get("prop_state_transitions")
    if not isinstance(transitions, list):
        errors.append(f"{shot_path}.prop_state_transitions: must be an array")
        return
    for index, transition in enumerate(transitions):
        path = f"{shot_path}.prop_state_transitions[{index}]"
        if not isinstance(transition, dict):
            errors.append(f"{path}: must be an object")
            continue
        prop_id = transition.get("prop_id")
        before_state = transition.get("before_state")
        after_state = transition.get("after_state")
        matching_entry = [
            asset
            for asset in selected_assets
            if asset.get("kind") == "prop" and asset.get("prop_id") == prop_id
        ]
        if len(matching_entry) != 1:
            errors.append(f"{path}: current shot must select exactly one entry asset for prop {prop_id!r}")
        elif matching_entry[0].get("state_id") != before_state:
            errors.append(f"{path}.before_state: does not match the selected entry prop asset")

        next_asset_id = transition.get("next_shot_asset_id")
        next_asset = assets.get(next_asset_id)
        if next_asset is None:
            errors.append(f"{path}.next_shot_asset_id: unknown asset {next_asset_id!r}")
        elif next_asset.get("kind") != "prop" or next_asset.get("prop_id") != prop_id:
            errors.append(f"{path}.next_shot_asset_id: must reference the same prop")
        elif next_asset.get("state_id") != after_state:
            errors.append(f"{path}.after_state: does not match next_shot_asset_id state")
        elif not next_asset.get("runtime_eligible"):
            errors.append(f"{path}.next_shot_asset_id: next-state asset must be runtime eligible")
        for field in ("before_state", "visible_trigger_action", "after_state", "exit_evidence"):
            if not isinstance(transition.get(field), str) or not transition.get(field).strip():
                errors.append(f"{path}.{field}: must be a non-empty string")


def validate_plan(plan: Any) -> list[str]:
    """Return human-readable validation errors; an empty list means valid."""
    errors: list[str] = []
    if not isinstance(plan, dict):
        return ["plan: must be a JSON object"]
    if plan.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version: must equal {SCHEMA_VERSION!r}")
    _required_text(plan, "story_id", "plan", errors)

    source_audio = plan.get("source_audio")
    audio_duration: Any = None
    if not isinstance(source_audio, dict):
        errors.append("source_audio: must be an object")
    else:
        _required_text(source_audio, "path", "source_audio", errors)
        _required_sha256(source_audio, "sha256", "source_audio", errors)
        audio_duration = source_audio.get("duration_seconds")
        if not _number(audio_duration) or audio_duration <= 0:
            errors.append("source_audio.duration_seconds: must be positive")

    policies = _validate_policies(plan, errors)
    assets = _index_by_id(plan.get("assets"), "asset_id", "assets", errors)
    groups = _index_by_id(plan.get("continuity_groups"), "group_id", "continuity_groups", errors)
    shots = _index_by_id(plan.get("shots"), "shot_id", "shots", errors)
    _validate_assets(assets, errors)
    _validate_groups(groups, assets, errors)

    previous_shot: dict[str, Any] | None = None
    for index, (shot_id, shot) in enumerate(shots.items()):
        path = f"shots[{shot_id!r}]"
        _required_text(shot, "story_text", path, errors)
        if shot.get("generation_mode") != BODY_MODE:
            errors.append(f"{path}.generation_mode: story body must use {BODY_MODE!r}")
        provider_seconds = shot.get("provider_seconds")
        if provider_seconds not in PROVIDER_DURATIONS:
            errors.append(f"{path}.provider_seconds: must be 6 or 10")

        start = shot.get("source_start")
        end = shot.get("source_end")
        valid_window = _number(start) and _number(end) and start >= 0 and end > start
        if not valid_window:
            errors.append(f"{path}: source_start and source_end must form a positive interval")
        else:
            if _number(audio_duration) and end > audio_duration + EPSILON:
                errors.append(f"{path}.source_end: exceeds source audio duration")
            if previous_shot is not None and _number(previous_shot.get("source_end")):
                if start < previous_shot["source_end"] - EPSILON:
                    errors.append(f"{path}.source_start: overlaps the previous shot")
            min_ratio = policies.get("min_retime_ratio")
            max_ratio = policies.get("max_retime_ratio")
            if provider_seconds in PROVIDER_DURATIONS and _number(min_ratio) and _number(max_ratio):
                ratio = (end - start) / provider_seconds
                if ratio < min_ratio - EPSILON or ratio > max_ratio + EPSILON:
                    errors.append(
                        f"{path}: retime ratio {ratio:.3f} is outside {min_ratio:.3f}–{max_ratio:.3f}"
                    )

        relation = shot.get("relation_to_previous")
        if relation not in RELATIONS:
            errors.append(f"{path}.relation_to_previous: unsupported relation {relation!r}")
        elif index == 0 and relation != "first":
            errors.append(f"{path}.relation_to_previous: the first shot must use 'first'")
        elif index > 0 and relation == "first":
            errors.append(f"{path}.relation_to_previous: only the first shot may use 'first'")

        group_id = shot.get("continuity_group")
        group = groups.get(group_id)
        if group is None:
            errors.append(f"{path}.continuity_group: unknown group {group_id!r}")
        if previous_shot is not None and relation in RELATIONS - {"first", "new_scene"}:
            if group_id != previous_shot.get("continuity_group"):
                errors.append(f"{path}.continuity_group: non-new-scene relation must preserve the previous group")
        if previous_shot is not None and relation == "new_scene":
            if group_id == previous_shot.get("continuity_group"):
                errors.append(f"{path}.continuity_group: new_scene must enter a new group")

        reference_ids = shot.get("reference_asset_ids")
        if not isinstance(reference_ids, list) or not reference_ids:
            errors.append(f"{path}.reference_asset_ids: must be a non-empty array")
            reference_ids = []
        elif _duplicates(reference_ids):
            errors.append(f"{path}.reference_asset_ids: asset ids must be unique")
        max_refs = policies.get("max_reference_images")
        if isinstance(max_refs, int) and len(reference_ids) > max_refs:
            errors.append(f"{path}.reference_asset_ids: exceeds policy limit of {max_refs}")

        selected_assets: list[dict[str, Any]] = []
        for asset_id in reference_ids:
            asset = assets.get(asset_id)
            if asset is None:
                errors.append(f"{path}.reference_asset_ids: unknown asset {asset_id!r}")
            else:
                selected_assets.append(asset)
                if not asset.get("runtime_eligible"):
                    errors.append(f"{path}.reference_asset_ids: asset {asset_id!r} is not runtime eligible")
                if asset.get("design_source_kind") in RUNTIME_FORBIDDEN_SOURCES:
                    errors.append(
                        f"{path}.reference_asset_ids: asset {asset_id!r} is a forbidden runtime sheet/composite"
                    )

        environment_assets = [asset for asset in selected_assets if asset.get("kind") == "environment"]
        if len(environment_assets) != 1:
            errors.append(f"{path}.reference_asset_ids: select exactly one empty environment")
        elif group is not None and environment_assets[0].get("asset_id") != group.get("environment_asset_id"):
            errors.append(f"{path}.reference_asset_ids: selected environment does not match continuity group")

        character_assets = [asset for asset in selected_assets if asset.get("kind") == "character"]
        selected_identities = {asset.get("identity_id") for asset in character_assets if asset.get("identity_id")}
        identity_list = [asset.get("identity_id") for asset in character_assets if asset.get("identity_id")]
        if _duplicates(identity_list):
            errors.append(f"{path}.reference_asset_ids: select only one runtime asset per character identity")
        prop_ids = [asset.get("prop_id") for asset in selected_assets if asset.get("kind") == "prop"]
        if _duplicates(prop_ids):
            errors.append(f"{path}.reference_asset_ids: select only one entry state per prop")

        initial = shot.get("initial_visible_characters")
        entering = shot.get("entering_characters")
        exiting = shot.get("exiting_characters")
        for field, values in (
            ("initial_visible_characters", initial),
            ("entering_characters", entering),
            ("exiting_characters", exiting),
        ):
            if not isinstance(values, list):
                errors.append(f"{path}.{field}: must be an array")
            else:
                unknown = set(values) - selected_identities
                if unknown:
                    errors.append(f"{path}.{field}: no selected character asset for {sorted(unknown)}")
        if isinstance(initial, list) and isinstance(entering, list) and set(initial) & set(entering):
            errors.append(f"{path}: a character cannot be both initially visible and entering")

        camera_plan = shot.get("camera_plan")
        if not isinstance(camera_plan, dict):
            errors.append(f"{path}.camera_plan: must be an object")
        else:
            for field in ("start_size", "end_size", "movement", "axis", "screen_direction"):
                _required_text(camera_plan, field, f"{path}.camera_plan", errors)
            if camera_plan.get("internal_cut") is not False:
                errors.append(f"{path}.camera_plan.internal_cut: one R2V task must be one continuous take")

        for field in ("entry_state", "exit_state"):
            if not isinstance(shot.get(field), dict):
                errors.append(f"{path}.{field}: must be an object")

        _validate_beats(shot, path, selected_identities, errors)
        _validate_prop_transitions(shot, path, selected_assets, assets, errors)
        if not isinstance(shot.get("prompt"), str) or not shot.get("prompt").strip():
            errors.append(f"{path}.prompt: must be a non-empty director prompt")
        previous_shot = shot

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="Path to story_r2v_plan.json")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.plan.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 2
    errors = validate_plan(payload)
    print(
        json.dumps(
            {"valid": not errors, "error_count": len(errors), "errors": errors},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
