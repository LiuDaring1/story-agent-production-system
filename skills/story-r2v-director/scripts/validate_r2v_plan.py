#!/usr/bin/env python3
"""Validate semantic and production invariants for a story R2V plan."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "story-r2v-plan-v4"
LEGACY_SCHEMA_VERSIONS = {"story-r2v-plan-v2", "story-r2v-plan-v3"}
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
    "ensemble_design_master",
}
ABSENT_PROP_STATES = {"absent", "none", "removed"}
SHOT_SIZES = {
    "extreme_wide",
    "wide",
    "medium_wide",
    "medium",
    "medium_close",
    "close",
    "extreme_close",
    "insert",
}
CROWD_MODES = {"none", "anonymous_background", "recurring_cohort", "identity_critical_ensemble"}
FACE_READABILITY = {"background_unreadable", "secondary", "identity_clear"}
CUT_TYPES = {
    "direct",
    "shot_reverse_shot",
    "reaction",
    "match_action",
    "eyeline_match",
    "graphic_match",
    "scene_transition",
}
CONTINUITY_DIMENSIONS = {
    "axis",
    "eyeline",
    "screen_direction",
    "spatial_relation",
    "character_state",
    "prop_state",
    "action_phase",
    "lighting",
}
DELIBERATE_CHANGES = {
    "shot_size",
    "camera_angle",
    "visual_focus",
    "subject",
    "action_phase",
    "location",
    "time",
}
EPSILON = 0.01
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_TIMELINE_GAP_SECONDS = 0.08
PHYSICAL_SCALE_BASES = {
    "handheld_small",
    "handheld_two_hands",
    "body_scale",
    "furniture_scale",
    "environment_scale",
}
PHYSICAL_SUPPORT_MODES = {"handheld", "freestanding", "grounded", "attached", "suspended", "loose"}
PHYSICAL_RIGIDITY = {"rigid", "flexible", "soft", "fragile", "fluid"}
SUBJECT_TYPES = {"character", "crowd", "prop", "environment"}
SCREEN_SIDES = {"left", "center", "right", "full_frame"}
EYELINE_DIRECTIONS = {"camera_left", "camera_right", "toward_camera", "away", "none", "mixed"}
CAMERA_SIDES = {"side_a", "side_b", "on_axis"}
FRAME_PRESENCE = {"on_screen", "off_screen"}
WORLD_PRESENCE = {"in_scene", "outside_scene"}
PROP_PRESENCE = {"present", "absent"}
ANCHOR_PERSISTENCE = {"fixed", "shot_local"}
ANCHOR_OCCUPANCY_RULES = {"none", "occupied_while_subject_in_scene"}
NARRATIVE_VISUALIZATION_MODES = {
    "literal_action",
    "speaker_performance",
    "listener_reaction",
    "speech_visual_bubble",
    "imagined_cutaway",
    "flashback",
    "metaphoric_insert",
}
NARRATIVE_LAYERS = {
    "current_fact",
    "proposed_action",
    "imagined_example",
    "memory",
    "explanation",
    "future_result",
}
DUPLICATE_IDENTITY_POLICIES = {
    "forbid",
    "framed_representation_only",
    "same_identity_memory_only",
}
NON_CHARACTER_SPEAKERS = {"", "narrator", "旁白", "none", "unknown"}
MODALITY_CUES = (
    "如果",
    "假如",
    "假设",
    "想象",
    "梦见",
    "回忆",
    "曾经",
    "打算",
    "计划",
    "将来",
    "你把我",
)


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


def _validate_physical_contract(asset: dict[str, Any], path: str, errors: list[str]) -> None:
    contract = asset.get("physical_contract")
    if not isinstance(contract, dict):
        errors.append(f"{path}.physical_contract: runtime prop assets require a physical contract")
        return
    if contract.get("scale_basis") not in PHYSICAL_SCALE_BASES:
        errors.append(f"{path}.physical_contract.scale_basis: unsupported physical scale basis")
    if contract.get("support_mode") not in PHYSICAL_SUPPORT_MODES:
        errors.append(f"{path}.physical_contract.support_mode: unsupported support mode")
    if contract.get("rigidity") not in PHYSICAL_RIGIDITY:
        errors.append(f"{path}.physical_contract.rigidity: unsupported rigidity")
    _required_text(contract, "grip_or_contact", f"{path}.physical_contract", errors)
    forbidden = contract.get("forbidden_inferences")
    if (
        not isinstance(forbidden, list)
        or not forbidden
        or any(not isinstance(value, str) or not value.strip() for value in forbidden)
    ):
        errors.append(
            f"{path}.physical_contract.forbidden_inferences: must list at least one forbidden inference"
        )
    elif _duplicates(forbidden):
        errors.append(f"{path}.physical_contract.forbidden_inferences: values must be unique")


def _validate_assets(
    assets: dict[str, dict[str, Any]], errors: list[str], *, strict_v3: bool
) -> None:
    valid_kinds = {"character", "population", "environment", "prop", "style", "storyboard"}
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
        elif kind == "population":
            if not isinstance(asset.get("population_id"), str) or not asset.get("population_id"):
                errors.append(f"{path}.population_id: population assets require a population id")
            if contains:
                errors.append(f"{path}.contains_characters: population assets cannot stand in for named identities")
            if asset.get("runtime_eligible") and asset.get("design_source_kind") != "population_archetype":
                errors.append(f"{path}.design_source_kind: runtime population assets must use 'population_archetype'")
        elif kind == "environment" and contains:
            errors.append(f"{path}.contains_characters: environment assets must be empty scenes")
        elif kind == "prop":
            if not isinstance(asset.get("prop_id"), str) or not asset.get("prop_id"):
                errors.append(f"{path}.prop_id: prop assets require a prop id")
            if not isinstance(asset.get("state_id"), str) or not asset.get("state_id"):
                errors.append(f"{path}.state_id: prop assets require a state id")
            if strict_v3 and asset.get("runtime_eligible"):
                _required_text(asset, "scale_class", path, errors)
                _validate_physical_contract(asset, path, errors)
        elif kind == "storyboard":
            if asset.get("design_source_kind") != "semantic_storyboard":
                errors.append(
                    f"{path}.design_source_kind: storyboard assets must use 'semantic_storyboard'"
                )
            _required_text(asset, "appearance_summary", path, errors)

        if asset.get("runtime_eligible") and asset.get("design_source_kind") in RUNTIME_FORBIDDEN_SOURCES:
            errors.append(
                f"{path}.design_source_kind: {asset.get('design_source_kind')!r} cannot be runtime eligible"
            )


def _validate_prop_state_families(
    assets: dict[str, dict[str, Any]], errors: list[str], *, strict_v3: bool
) -> None:
    """Keep multi-state prop variants traceable to one reviewed visual mother asset."""
    if not strict_v3:
        return
    by_prop: dict[str, list[dict[str, Any]]] = {}
    for asset in assets.values():
        if asset.get("kind") != "prop" or not asset.get("runtime_eligible"):
            continue
        prop_id = asset.get("prop_id")
        if isinstance(prop_id, str) and prop_id:
            by_prop.setdefault(prop_id, []).append(asset)

    for prop_id, family in by_prop.items():
        if len(family) < 2:
            continue
        family_path = f"prop_state_family[{prop_id!r}]"
        for asset in family:
            asset_path = f"assets[{asset.get('asset_id')!r}]"
            for field in ("state_family_id", "state_family_master_path", "state_delta"):
                _required_text(asset, field, asset_path, errors)
            _required_sha256(asset, "state_family_master_sha256", asset_path, errors)

        def values(field: str) -> set[Any]:
            return {asset.get(field) for asset in family}

        if len(values("state_family_id")) != 1:
            errors.append(f"{family_path}.state_family_id: all states must share one family id")
        if len(values("state_family_master_path")) != 1:
            errors.append(f"{family_path}.state_family_master_path: all states must share one mother asset")
        if len(values("state_family_master_sha256")) != 1:
            errors.append(f"{family_path}.state_family_master_sha256: all states must bind the same mother hash")
        state_ids = [asset.get("state_id") for asset in family]
        if _duplicates(state_ids):
            errors.append(f"{family_path}.state_id: each derived state must be unique")
        if len(values("scale_class")) != 1:
            errors.append(f"{family_path}.scale_class: derived states must preserve one scale class")
        scale_bases = {
            (asset.get("physical_contract") or {}).get("scale_basis")
            for asset in family
            if isinstance(asset.get("physical_contract"), dict)
        }
        if len(scale_bases) != 1:
            errors.append(f"{family_path}.physical_contract.scale_basis: derived states must preserve scale")
        rigidities = {
            (asset.get("physical_contract") or {}).get("rigidity")
            for asset in family
            if isinstance(asset.get("physical_contract"), dict)
        }
        if len(rigidities) != 1:
            errors.append(f"{family_path}.physical_contract.rigidity: derived states must preserve material behavior")


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


def _validate_v4_groups(
    groups: dict[str, dict[str, Any]],
    assets: dict[str, dict[str, Any]],
    errors: list[str],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Validate the structured stage geography used by current production plans."""

    identity_ids = {
        str(asset.get("identity_id"))
        for asset in assets.values()
        if asset.get("kind") == "character" and asset.get("identity_id")
    }
    prop_ids = {
        str(asset.get("prop_id"))
        for asset in assets.values()
        if asset.get("kind") == "prop" and asset.get("prop_id")
    }
    population_ids = {
        str(asset.get("population_id"))
        for asset in assets.values()
        if asset.get("kind") == "population" and asset.get("population_id")
    }
    structures: dict[str, dict[str, dict[str, Any]]] = {}
    for group_id, group in groups.items():
        path = f"continuity_groups[{group_id!r}]"
        master_environment = assets.get(group.get("environment_asset_id"))
        master_environment_family_id = (
            master_environment.get("environment_family_id")
            if isinstance(master_environment, dict)
            else None
        )
        if not isinstance(master_environment_family_id, str) or not master_environment_family_id.strip():
            errors.append(
                f"{path}.environment_asset_id: master environment must declare environment_family_id"
            )
        color = group.get("color_contract")
        if not isinstance(color, dict):
            errors.append(f"{path}.color_contract: must be an object")
        else:
            for field in ("white_balance", "palette", "rationale"):
                _required_text(color, field, f"{path}.color_contract", errors)
            prohibited_casts = color.get("prohibited_casts")
            if (
                not isinstance(prohibited_casts, list)
                or not prohibited_casts
                or any(not isinstance(value, str) or not value.strip() for value in prohibited_casts)
            ):
                errors.append(
                    f"{path}.color_contract.prohibited_casts: must list at least one unwanted global cast"
                )
        zones = _index_by_id(group.get("zones"), "zone_id", f"{path}.zones", errors)
        for zone_id, zone in zones.items():
            zone_path = f"{path}.zones[{zone_id!r}]"
            _required_text(zone, "description", zone_path, errors)
            for field in ("map_x", "map_y"):
                if not _number(zone.get(field)):
                    errors.append(f"{zone_path}.{field}: must be a finite scene-map coordinate")

        anchors = _index_by_id(group.get("anchors"), "anchor_id", f"{path}.anchors", errors)
        for anchor_id, anchor in anchors.items():
            anchor_path = f"{path}.anchors[{anchor_id!r}]"
            _required_text(anchor, "description", anchor_path, errors)
            if anchor.get("zone_id") not in zones:
                errors.append(f"{anchor_path}.zone_id: unknown zone {anchor.get('zone_id')!r}")
            if anchor.get("persistence") not in ANCHOR_PERSISTENCE:
                errors.append(f"{anchor_path}.persistence: unsupported anchor persistence")
            occupancy_rule = anchor.get("occupancy_rule")
            occupant_subject_id = anchor.get("occupant_subject_id")
            if occupancy_rule not in ANCHOR_OCCUPANCY_RULES:
                errors.append(f"{anchor_path}.occupancy_rule: unsupported occupancy rule")
            elif occupancy_rule == "none" and occupant_subject_id is not None:
                errors.append(f"{anchor_path}.occupant_subject_id: unoccupied anchors must use null")
            elif occupancy_rule == "occupied_while_subject_in_scene":
                if occupant_subject_id not in identity_ids:
                    errors.append(
                        f"{anchor_path}.occupant_subject_id: unknown character {occupant_subject_id!r}"
                    )

        axes = _index_by_id(group.get("axes"), "axis_id", f"{path}.axes", errors)
        for axis_id, axis in axes.items():
            axis_path = f"{path}.axes[{axis_id!r}]"
            endpoint_a = axis.get("endpoint_a_zone_id")
            endpoint_b = axis.get("endpoint_b_zone_id")
            if endpoint_a not in zones:
                errors.append(f"{axis_path}.endpoint_a_zone_id: unknown zone {endpoint_a!r}")
            if endpoint_b not in zones:
                errors.append(f"{axis_path}.endpoint_b_zone_id: unknown zone {endpoint_b!r}")
            if endpoint_a == endpoint_b and endpoint_a is not None:
                errors.append(f"{axis_path}: axis endpoints must use different zones")
            _required_text(axis, "description", axis_path, errors)

        setups = _index_by_id(
            group.get("camera_setups"),
            "setup_id",
            f"{path}.camera_setups",
            errors,
        )
        for setup_id, setup in setups.items():
            setup_path = f"{path}.camera_setups[{setup_id!r}]"
            if setup.get("axis_id") not in axes:
                errors.append(f"{setup_path}.axis_id: unknown axis {setup.get('axis_id')!r}")
            if setup.get("camera_side") not in CAMERA_SIDES:
                errors.append(f"{setup_path}.camera_side: unsupported camera side")
            if setup.get("shot_size") not in SHOT_SIZES:
                errors.append(f"{setup_path}.shot_size: unsupported shot size")
            _required_text(setup, "camera_angle", setup_path, errors)
            subject_type = setup.get("primary_subject_type")
            subject_id = setup.get("primary_subject_id")
            if subject_type not in SUBJECT_TYPES:
                errors.append(f"{setup_path}.primary_subject_type: unsupported subject type")
            _required_text(setup, "primary_subject_id", setup_path, errors)
            if subject_type == "character" and subject_id not in identity_ids:
                errors.append(f"{setup_path}.primary_subject_id: unknown character {subject_id!r}")
            elif subject_type == "prop" and subject_id not in prop_ids:
                errors.append(f"{setup_path}.primary_subject_id: unknown prop {subject_id!r}")
            elif subject_type == "crowd" and subject_id not in population_ids:
                errors.append(f"{setup_path}.primary_subject_id: unknown crowd {subject_id!r}")
            elif subject_type == "environment" and subject_id != group.get("environment_asset_id"):
                errors.append(f"{setup_path}.primary_subject_id: must name this group's environment asset")
            if setup.get("subject_zone_id") not in zones:
                errors.append(f"{setup_path}.subject_zone_id: unknown zone {setup.get('subject_zone_id')!r}")
            camera_origin = setup.get("camera_origin_zone_id")
            look_target = setup.get("look_target_zone_id")
            if camera_origin not in zones:
                errors.append(f"{setup_path}.camera_origin_zone_id: unknown zone {camera_origin!r}")
            if look_target not in zones:
                errors.append(f"{setup_path}.look_target_zone_id: unknown zone {look_target!r}")
            if (
                setup.get("subject_zone_id") in zones
                and look_target in zones
                and setup.get("subject_zone_id") != look_target
            ):
                errors.append(
                    f"{setup_path}.look_target_zone_id: must match subject_zone_id so the camera ray "
                    "actually passes through the declared primary subject"
                )
            if camera_origin == look_target and camera_origin is not None:
                errors.append(f"{setup_path}: camera origin and look target must use different zones")
            origin_zone = zones.get(camera_origin)
            target_zone = zones.get(look_target)
            if isinstance(origin_zone, dict) and isinstance(target_zone, dict):
                origin_x = origin_zone.get("map_x")
                origin_y = origin_zone.get("map_y")
                target_x = target_zone.get("map_x")
                target_y = target_zone.get("map_y")
                if all(_number(value) for value in (origin_x, origin_y, target_x, target_y)):
                    if math.hypot(target_x - origin_x, target_y - origin_y) <= EPSILON:
                        errors.append(
                            f"{setup_path}: camera origin and look target must have different scene-map coordinates"
                        )
            background_zone_ids = setup.get("background_zone_ids")
            if (
                not isinstance(background_zone_ids, list)
                or not background_zone_ids
                or any(value not in zones for value in background_zone_ids)
            ):
                errors.append(
                    f"{setup_path}.background_zone_ids: must list one or more known zones behind the subject"
                )
            elif _duplicates(background_zone_ids):
                errors.append(f"{setup_path}.background_zone_ids: zones must be unique")
            elif isinstance(origin_zone, dict) and isinstance(target_zone, dict):
                origin_x = origin_zone.get("map_x")
                origin_y = origin_zone.get("map_y")
                target_x = target_zone.get("map_x")
                target_y = target_zone.get("map_y")
                if all(_number(value) for value in (origin_x, origin_y, target_x, target_y)):
                    view_x = target_x - origin_x
                    view_y = target_y - origin_y
                    view_length = math.hypot(view_x, view_y)
                    if view_length > EPSILON:
                        for background_zone_id in background_zone_ids:
                            background_zone = zones.get(background_zone_id)
                            if not isinstance(background_zone, dict):
                                continue
                            background_x = background_zone.get("map_x")
                            background_y = background_zone.get("map_y")
                            if not all(_number(value) for value in (background_x, background_y)):
                                continue
                            beyond_target = (
                                (background_x - target_x) * view_x
                                + (background_y - target_y) * view_y
                            ) / view_length
                            if beyond_target <= EPSILON:
                                errors.append(
                                    f"{setup_path}.background_zone_ids: {background_zone_id!r} is not "
                                    "geometrically behind the look target; it lies at the target or on "
                                    "the camera-facing side of the scene map"
                                )
            _required_text(setup, "camera_position_description", setup_path, errors)
            environment_view_id = setup.get("environment_view_asset_id")
            environment_view = assets.get(environment_view_id)
            if environment_view is None:
                errors.append(
                    f"{setup_path}.environment_view_asset_id: unknown asset {environment_view_id!r}"
                )
            elif environment_view.get("kind") != "environment":
                errors.append(f"{setup_path}.environment_view_asset_id: must reference an environment")
            else:
                if environment_view.get("design_source_kind") != "empty_environment_camera_view":
                    errors.append(
                        f"{setup_path}.environment_view_asset_id: must use an empty_environment_camera_view"
                    )
                if environment_view.get("contains_characters"):
                    errors.append(
                        f"{setup_path}.environment_view_asset_id: camera view must contain no characters"
                    )
                if environment_view.get("runtime_eligible") is not True:
                    errors.append(
                        f"{setup_path}.environment_view_asset_id: camera view must be runtime eligible"
                    )
                if environment_view.get("derived_from_asset_id") != group.get("environment_asset_id"):
                    errors.append(
                        f"{setup_path}.environment_view_asset_id: camera view must derive from the group master environment"
                    )
                if environment_view.get("environment_family_id") != master_environment_family_id:
                    errors.append(
                        f"{setup_path}.environment_view_asset_id: camera view must remain in the group environment family"
                    )
                if environment_view.get("view_from_zone_id") != camera_origin:
                    errors.append(
                        f"{setup_path}.environment_view_asset_id: view_from_zone_id must match camera_origin_zone_id"
                    )
                if environment_view.get("view_target_zone_id") != look_target:
                    errors.append(
                        f"{setup_path}.environment_view_asset_id: view_target_zone_id must match look_target_zone_id"
                    )
                if environment_view.get("view_background_zone_ids") != background_zone_ids:
                    errors.append(
                        f"{setup_path}.environment_view_asset_id: view background zones must exactly match the camera setup"
                    )
            if setup.get("screen_side") not in SCREEN_SIDES:
                errors.append(f"{setup_path}.screen_side: unsupported screen side")
            if setup.get("eyeline_direction") not in EYELINE_DIRECTIONS:
                errors.append(f"{setup_path}.eyeline_direction: unsupported eyeline direction")
        structures[group_id] = {
            "zones": zones,
            "anchors": anchors,
            "axes": axes,
            "camera_setups": setups,
        }
    return structures


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


def _validate_v4_shot_contracts(
    shot: dict[str, Any],
    shot_path: str,
    selected_assets: list[dict[str, Any]],
    assets: dict[str, dict[str, Any]],
    structure: dict[str, dict[str, Any]],
    errors: list[str],
) -> None:
    """Bind free-form direction to stage geography, focus, presence and props."""

    zones = structure.get("zones", {})
    axes = structure.get("axes", {})
    setups = structure.get("camera_setups", {})
    camera = shot.get("camera_plan") if isinstance(shot.get("camera_plan"), dict) else {}
    axis_id = camera.get("axis_id")
    setup_id = camera.get("camera_setup_id")
    if axis_id not in axes:
        errors.append(f"{shot_path}.camera_plan.axis_id: unknown axis {axis_id!r}")
    setup = setups.get(setup_id)
    if setup is None:
        errors.append(f"{shot_path}.camera_plan.camera_setup_id: unknown camera setup {setup_id!r}")
    else:
        if setup.get("axis_id") != axis_id:
            errors.append(f"{shot_path}.camera_plan: camera setup and axis_id do not match")
        opening = shot.get("opening_frame")
        if isinstance(opening, dict) and opening.get("shot_size") != setup.get("shot_size"):
            errors.append(
                f"{shot_path}.opening_frame.shot_size: must match the structured camera setup"
            )
    visible_anchor_ids = camera.get("visible_anchor_ids")
    excluded_anchor_ids = camera.get("excluded_anchor_ids")
    for field, values in (
        ("visible_anchor_ids", visible_anchor_ids),
        ("excluded_anchor_ids", excluded_anchor_ids),
    ):
        if not isinstance(values, list):
            errors.append(f"{shot_path}.camera_plan.{field}: must be an array")
        else:
            if any(value not in structure.get("anchors", {}) for value in values):
                errors.append(f"{shot_path}.camera_plan.{field}: contains an unknown anchor")
            if _duplicates(values):
                errors.append(f"{shot_path}.camera_plan.{field}: anchors must be unique")
    if isinstance(visible_anchor_ids, list) and isinstance(excluded_anchor_ids, list):
        overlap = set(visible_anchor_ids) & set(excluded_anchor_ids)
        if overlap:
            errors.append(
                f"{shot_path}.camera_plan: anchors cannot be both visible and excluded {sorted(overlap)}"
            )
        fixed_anchor_ids = {
            anchor_id
            for anchor_id, anchor in structure.get("anchors", {}).items()
            if isinstance(anchor, dict) and anchor.get("persistence") == "fixed"
        }
        unclassified = fixed_anchor_ids - set(visible_anchor_ids) - set(excluded_anchor_ids)
        if unclassified:
            errors.append(
                f"{shot_path}.camera_plan: every fixed anchor must be classified as visible or "
                f"excluded; missing {sorted(unclassified)}"
            )

    identity_ids = {
        str(asset.get("identity_id"))
        for asset in assets.values()
        if asset.get("kind") == "character" and asset.get("identity_id")
    }
    selected_identity_ids = {
        str(asset.get("identity_id"))
        for asset in selected_assets
        if asset.get("kind") == "character" and asset.get("identity_id")
    }
    selected_population_ids = {
        str(asset.get("population_id"))
        for asset in selected_assets
        if asset.get("kind") == "population" and asset.get("population_id")
    }
    presence_items = _index_by_id(
        shot.get("subject_presence"),
        "subject_id",
        f"{shot_path}.subject_presence",
        errors,
    )
    initial: set[str] = set()
    entering: set[str] = set()
    exiting: set[str] = set()
    fully_off_screen: set[str] = set()
    visible_at_some_point: set[str] = set()
    for subject_id, presence in presence_items.items():
        path = f"{shot_path}.subject_presence[{subject_id!r}]"
        subject_type = presence.get("subject_type")
        if subject_type not in SUBJECT_TYPES:
            errors.append(f"{path}.subject_type: unsupported subject type")
        if subject_type == "character" and subject_id not in identity_ids:
            errors.append(f"{path}.subject_id: unknown character {subject_id!r}")
        entry = presence.get("entry_presence")
        exit_value = presence.get("exit_presence")
        if entry not in FRAME_PRESENCE:
            errors.append(f"{path}.entry_presence: must be on_screen or off_screen")
        if exit_value not in FRAME_PRESENCE:
            errors.append(f"{path}.exit_presence: must be on_screen or off_screen")
        for phase, value in (("entry", entry), ("exit", exit_value)):
            zone_id = presence.get(f"{phase}_zone_id")
            if value == "on_screen" and zone_id not in zones:
                errors.append(f"{path}.{phase}_zone_id: unknown zone {zone_id!r}")
            if value == "off_screen" and zone_id is not None:
                errors.append(f"{path}.{phase}_zone_id: off-screen subjects must use null")
            world_field = f"{phase}_world_zone_id"
            world_presence_field = f"{phase}_world_presence"
            world_presence = presence.get(world_presence_field)
            if world_presence not in WORLD_PRESENCE:
                errors.append(
                    f"{path}.{world_presence_field}: must be in_scene or outside_scene"
                )
            if world_field not in presence:
                errors.append(f"{path}.{world_field}: must be declared even when off-screen")
                world_zone_id = None
            else:
                world_zone_id = presence.get(world_field)
                if world_zone_id is not None and world_zone_id not in zones:
                    errors.append(f"{path}.{world_field}: unknown zone {world_zone_id!r}")
            if world_presence == "in_scene" and world_zone_id not in zones:
                errors.append(
                    f"{path}.{world_field}: an in-scene subject must occupy one known world zone"
                )
            if world_presence == "outside_scene" and world_zone_id is not None:
                errors.append(
                    f"{path}.{world_field}: an outside-scene subject must use null"
                )
            if value == "on_screen" and world_presence != "in_scene":
                errors.append(
                    f"{path}.{world_presence_field}: an on-screen subject must be in_scene"
                )
            if value == "on_screen" and world_zone_id != zone_id:
                errors.append(
                    f"{path}.{world_field}: on-screen frame zone and world zone must match"
                )
            if value == "on_screen" and setup is not None and zone_id in zones:
                origin = zones.get(setup.get("camera_origin_zone_id"))
                target = zones.get(setup.get("look_target_zone_id"))
                visible_zone = zones.get(zone_id)
                if all(isinstance(item, dict) for item in (origin, target, visible_zone)):
                    origin_x = origin.get("map_x")
                    origin_y = origin.get("map_y")
                    target_x = target.get("map_x")
                    target_y = target.get("map_y")
                    visible_x = visible_zone.get("map_x")
                    visible_y = visible_zone.get("map_y")
                    if all(
                        _number(number)
                        for number in (
                            origin_x,
                            origin_y,
                            target_x,
                            target_y,
                            visible_x,
                            visible_y,
                        )
                    ):
                        view_x = target_x - origin_x
                        view_y = target_y - origin_y
                        forward_projection = (
                            (visible_x - origin_x) * view_x
                            + (visible_y - origin_y) * view_y
                        )
                        if forward_projection < -EPSILON:
                            errors.append(
                                f"{path}.{phase}_zone_id: on-screen subject {subject_id!r} lies "
                                "behind the camera on the scene map"
                            )
        _required_text(presence, "visibility_reason", path, errors)
        if entry == "off_screen" and exit_value == "off_screen":
            fully_off_screen.add(subject_id)
        if subject_type == "character":
            if entry == "on_screen":
                initial.add(subject_id)
            if entry == "off_screen" and exit_value == "on_screen":
                entering.add(subject_id)
            if entry == "on_screen" and exit_value == "off_screen":
                exiting.add(subject_id)
            if entry != "off_screen" or exit_value != "off_screen":
                visible_at_some_point.add(subject_id)
                if subject_id not in selected_identity_ids:
                    errors.append(f"{path}: on-screen character needs one selected runtime asset")
        elif subject_type == "crowd" and (
            entry == "on_screen" or exit_value == "on_screen"
        ) and subject_id not in selected_population_ids:
            errors.append(f"{path}: on-screen crowd needs its selected population asset")

    for subject_id in selected_identity_ids - set(presence_items):
        errors.append(
            f"{shot_path}.subject_presence: selected character {subject_id!r} lacks a presence contract"
        )
    for subject_id in selected_population_ids - set(presence_items):
        errors.append(
            f"{shot_path}.subject_presence: selected population {subject_id!r} lacks an on-screen crowd presence contract"
        )
    for subject_id in selected_population_ids & set(presence_items):
        presence = presence_items[subject_id]
        if (
            presence.get("subject_type") != "crowd"
            or (
                presence.get("entry_presence") != "on_screen"
                and presence.get("exit_presence") != "on_screen"
            )
        ):
            errors.append(
                f"{shot_path}.subject_presence: selected population {subject_id!r} must be an on-screen crowd"
            )
    declared_sets = (
        ("initial_visible_characters", initial),
        ("entering_characters", entering),
        ("exiting_characters", exiting),
    )
    for field, derived in declared_sets:
        declared = shot.get(field)
        if isinstance(declared, list) and set(declared) != derived:
            errors.append(f"{shot_path}.{field}: must match structured subject_presence")

    focus = shot.get("focus_contract")
    if not isinstance(focus, dict):
        errors.append(f"{shot_path}.focus_contract: must be an object")
        focus = {}
    primary_id = focus.get("primary_subject_id")
    primary_type = focus.get("primary_subject_type")
    _required_text(focus, "primary_subject_id", f"{shot_path}.focus_contract", errors)
    if primary_type not in SUBJECT_TYPES:
        errors.append(f"{shot_path}.focus_contract.primary_subject_type: unsupported subject type")
    for field in (
        "secondary_subject_ids",
        "background_subject_ids",
        "off_screen_subject_ids",
    ):
        values = focus.get(field)
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
            errors.append(f"{shot_path}.focus_contract.{field}: must be an array of subject ids")
        elif _duplicates(values):
            errors.append(f"{shot_path}.focus_contract.{field}: subject ids must be unique")
    _required_text(focus, "visual_priority", f"{shot_path}.focus_contract", errors)
    _required_text(focus, "composition_rule", f"{shot_path}.focus_contract", errors)
    if setup is not None and (
        primary_id != setup.get("primary_subject_id")
        or primary_type != setup.get("primary_subject_type")
    ):
        errors.append(f"{shot_path}.focus_contract: focus primary subject must match the camera setup")
    if primary_type == "character" and primary_id not in visible_at_some_point:
        errors.append(f"{shot_path}.focus_contract: primary character must be on-screen in this shot")
    offscreen_declared = focus.get("off_screen_subject_ids")
    if isinstance(offscreen_declared, list) and set(offscreen_declared) != fully_off_screen:
        errors.append(
            f"{shot_path}.focus_contract.off_screen_subject_ids: must match subjects off-screen for the whole shot"
        )

    if isinstance(visible_anchor_ids, list):
        anchors = structure.get("anchors", {})
        if setup is not None:
            origin_zone = zones.get(setup.get("camera_origin_zone_id"))
            target_zone = zones.get(setup.get("look_target_zone_id"))
            if isinstance(origin_zone, dict) and isinstance(target_zone, dict):
                origin_x = origin_zone.get("map_x")
                origin_y = origin_zone.get("map_y")
                target_x = target_zone.get("map_x")
                target_y = target_zone.get("map_y")
                if all(_number(value) for value in (origin_x, origin_y, target_x, target_y)):
                    view_x = target_x - origin_x
                    view_y = target_y - origin_y
                    for anchor_id in visible_anchor_ids:
                        anchor = anchors.get(anchor_id)
                        anchor_zone = zones.get(anchor.get("zone_id")) if isinstance(anchor, dict) else None
                        if not isinstance(anchor_zone, dict):
                            continue
                        anchor_x = anchor_zone.get("map_x")
                        anchor_y = anchor_zone.get("map_y")
                        if not all(_number(value) for value in (anchor_x, anchor_y)):
                            continue
                        forward_projection = (
                            (anchor_x - origin_x) * view_x
                            + (anchor_y - origin_y) * view_y
                        )
                        if forward_projection < -EPSILON:
                            errors.append(
                                f"{shot_path}.camera_plan.visible_anchor_ids: {anchor_id!r} lies "
                                "behind the camera on the scene map and cannot be visible"
                            )
        for anchor_id in visible_anchor_ids:
            anchor = anchors.get(anchor_id)
            if not isinstance(anchor, dict):
                continue
            if anchor.get("occupancy_rule") != "occupied_while_subject_in_scene":
                continue
            occupant_id = anchor.get("occupant_subject_id")
            occupant = presence_items.get(occupant_id)
            if occupant is None:
                errors.append(
                    f"{shot_path}.camera_plan.visible_anchor_ids: occupied anchor {anchor_id!r} "
                    f"requires a presence contract for {occupant_id!r}"
                )
                continue
            anchor_zone = anchor.get("zone_id")
            for phase in ("entry", "exit"):
                if (
                    occupant.get(f"{phase}_world_presence") == "in_scene"
                    and
                    occupant.get(f"{phase}_world_zone_id") == anchor_zone
                    and occupant.get(f"{phase}_presence") != "on_screen"
                ):
                    errors.append(
                        f"{shot_path}.camera_plan.visible_anchor_ids: {anchor_id!r} cannot appear "
                        f"empty while {occupant_id!r} occupies it at {phase}"
                    )

    beats = shot.get("performance_beats")
    if isinstance(beats, list):
        for index, beat in enumerate(beats):
            if not isinstance(beat, dict):
                continue
            path = f"{shot_path}.performance_beats[{index}]"
            primary = beat.get("primary_beat")
            if not isinstance(primary, dict):
                errors.append(f"{path}.primary_beat must be one object")
                primary = {}
            _required_text(primary, "subject_id", f"{path}.primary_beat", errors)
            _required_text(primary, "action", f"{path}.primary_beat", errors)
            visible = set(beat.get("visible_characters") or [])
            beat_primary_id = primary.get("subject_id")
            if beat_primary_id in fully_off_screen:
                errors.append(f"{path}: an off-screen primary subject cannot drive a visible beat")
            if beat_primary_id in identity_ids and beat_primary_id not in visible:
                errors.append(f"{path}.primary_beat: primary character must be visible in the beat")
            forbidden_visible = visible & fully_off_screen
            if forbidden_visible:
                errors.append(
                    f"{path}.visible_characters: a subject off-screen for the whole shot cannot be visible {sorted(forbidden_visible)}"
                )
            supporting = beat.get("supporting_actions")
            if not isinstance(supporting, list):
                errors.append(f"{path}.supporting_actions: must be an array")
                continue
            supporting_ids: list[str] = []
            for support_index, action in enumerate(supporting):
                action_path = f"{path}.supporting_actions[{support_index}]"
                if not isinstance(action, dict):
                    errors.append(f"{action_path}: must be an object")
                    continue
                _required_text(action, "subject_id", action_path, errors)
                _required_text(action, "action", action_path, errors)
                support_id = action.get("subject_id")
                if isinstance(support_id, str):
                    supporting_ids.append(support_id)
                if support_id == beat_primary_id:
                    errors.append(f"{action_path}: primary subject cannot also be a supporting action")
                if support_id in identity_ids and support_id not in visible:
                    errors.append(f"{action_path}: supporting character must be visible in the beat")
            if _duplicates(supporting_ids):
                errors.append(f"{path}.supporting_actions: each supporting subject may appear once")

    selected_props = {
        str(asset.get("prop_id")): asset
        for asset in selected_assets
        if asset.get("kind") == "prop" and asset.get("prop_id")
    }
    prop_contracts = _index_by_id(
        shot.get("prop_contracts"),
        "prop_id",
        f"{shot_path}.prop_contracts",
        errors,
    ) if shot.get("prop_contracts") else {}
    if not isinstance(shot.get("prop_contracts"), list):
        errors.append(f"{shot_path}.prop_contracts: must be an array")
    for prop_id in selected_props.keys() - prop_contracts.keys():
        errors.append(f"{shot_path}.prop_contracts: selected prop {prop_id!r} lacks a shot contract")
    for prop_id, contract in prop_contracts.items():
        path = f"{shot_path}.prop_contracts[{prop_id!r}]"
        entry_presence = contract.get("entry_presence")
        exit_presence = contract.get("exit_presence")
        if entry_presence not in PROP_PRESENCE:
            errors.append(f"{path}.entry_presence: must be present or absent")
        if exit_presence not in PROP_PRESENCE:
            errors.append(f"{path}.exit_presence: must be present or absent")
        for field in ("entry_state", "exit_state", "owner_id", "grip_or_contact"):
            _required_text(contract, field, path, errors)
        zone_id = contract.get("zone_id")
        if (entry_presence == "present" or exit_presence == "present") and zone_id not in zones:
            errors.append(f"{path}.zone_id: unknown zone {zone_id!r}")
        asset_id = contract.get("asset_id")
        selected = selected_props.get(prop_id)
        if entry_presence == "absent":
            if asset_id is not None or selected is not None:
                errors.append(f"{path}: absent entry prop must not bind or select an asset")
        else:
            if selected is None or selected.get("asset_id") != asset_id:
                errors.append(f"{path}.asset_id: must bind the selected entry prop asset")
            elif selected.get("state_id") != contract.get("entry_state"):
                errors.append(f"{path}.entry_state: must match the selected prop asset state")
            if selected is not None:
                physical = selected.get("physical_contract") or {}
                matches_physics = (
                    contract.get("scale_basis") == physical.get("scale_basis")
                    and contract.get("support_mode") == physical.get("support_mode")
                    and contract.get("rigidity") == physical.get("rigidity")
                    and contract.get("grip_or_contact") == physical.get("grip_or_contact")
                    and contract.get("forbidden_inferences") == physical.get("forbidden_inferences")
                )
                if not matches_physics:
                    errors.append(
                        f"{path}: must match the selected prop asset physical contract"
                    )
        entry_state = shot.get("entry_state")
        exit_state = shot.get("exit_state")
        if isinstance(entry_state, dict) and entry_state.get(prop_id) != contract.get("entry_state"):
            errors.append(f"{path}.entry_state: must match shot.entry_state")
        if isinstance(exit_state, dict) and exit_state.get(prop_id) != contract.get("exit_state"):
            errors.append(f"{path}.exit_state: must match shot.exit_state")


def _validate_v4_reverse_cut(
    shot: dict[str, Any],
    next_shot: dict[str, Any] | None,
    shot_path: str,
    structures: dict[str, dict[str, dict[str, Any]]],
    errors: list[str],
) -> None:
    contract = shot.get("cut_to_next")
    if not isinstance(contract, dict) or contract.get("cut_type") != "shot_reverse_shot":
        return
    if not isinstance(next_shot, dict):
        return
    current_group_id = str(shot.get("continuity_group") or "")
    next_group_id = str(next_shot.get("continuity_group") or "")
    if current_group_id != next_group_id:
        errors.append(f"{shot_path}.cut_to_next: shot_reverse_shot must remain in one continuity group")
        return
    setups = structures.get(current_group_id, {}).get("camera_setups", {})
    current_camera = shot.get("camera_plan") if isinstance(shot.get("camera_plan"), dict) else {}
    next_camera = next_shot.get("camera_plan") if isinstance(next_shot.get("camera_plan"), dict) else {}
    current_id = current_camera.get("camera_setup_id")
    next_id = next_camera.get("camera_setup_id")
    if current_id == next_id:
        errors.append(
            f"{shot_path}.cut_to_next: shot_reverse_shot requires different camera setups"
        )
    current_setup = setups.get(current_id)
    next_setup = setups.get(next_id)
    if current_setup is None or next_setup is None:
        return
    if current_setup.get("axis_id") != next_setup.get("axis_id"):
        errors.append(f"{shot_path}.cut_to_next: reverse setups must share one axis")
    if (
        current_setup.get("camera_side") != next_setup.get("camera_side")
        or current_setup.get("camera_side") == "on_axis"
    ):
        errors.append(
            f"{shot_path}.cut_to_next: reverse setups must stay on the same non-axis camera side"
        )
    if current_setup.get("primary_subject_id") == next_setup.get("primary_subject_id"):
        errors.append(f"{shot_path}.cut_to_next: reverse setups must change primary subject")
    screen_sides = {current_setup.get("screen_side"), next_setup.get("screen_side")}
    if screen_sides != {"left", "right"}:
        errors.append(f"{shot_path}.cut_to_next: reverse subjects must occupy complementary screen sides")
    eyelines = {current_setup.get("eyeline_direction"), next_setup.get("eyeline_direction")}
    if eyelines != {"camera_left", "camera_right"}:
        errors.append(f"{shot_path}.cut_to_next: reverse eyelines must be complementary")


def _validate_v4_cross_shot_continuity(
    shot: dict[str, Any],
    next_shot: dict[str, Any] | None,
    shot_path: str,
    errors: list[str],
) -> None:
    """Reject visible same-scene subjects that change zones at the cut.

    A camera change may reveal or hide a subject, but a subject that is visible
    on both sides of a contiguous cut must occupy the same declared zone. A
    planned walk belongs inside one of the adjacent shots, so its exit and
    entry zones still meet at the cut boundary.
    """
    if not isinstance(next_shot, dict):
        return
    if next_shot.get("relation_to_previous") == "new_scene":
        return
    if shot.get("continuity_group") != next_shot.get("continuity_group"):
        return

    def character_presence(item: dict[str, Any], side: str) -> dict[str, dict[str, Any]]:
        rows = item.get("subject_presence")
        if not isinstance(rows, list):
            return {}
        presence_field = f"{side}_presence"
        return {
            str(row.get("subject_id")): row
            for row in rows
            if isinstance(row, dict)
            and row.get("subject_type") == "character"
            and row.get(presence_field) == "on_screen"
            and isinstance(row.get("subject_id"), str)
            and row.get("subject_id")
        }

    outgoing = character_presence(shot, "exit")
    incoming = character_presence(next_shot, "entry")
    for subject_id in sorted(outgoing.keys() & incoming.keys()):
        outgoing_zone = outgoing[subject_id].get("exit_zone_id")
        incoming_zone = incoming[subject_id].get("entry_zone_id")
        if outgoing_zone != incoming_zone:
            errors.append(
                f"{shot_path}.cut_to_next: character {subject_id} teleports from zone "
                f"{outgoing_zone!r} to {incoming_zone!r} across a same-scene cut"
            )

    def all_subject_presence(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
        rows = item.get("subject_presence")
        if not isinstance(rows, list):
            return {}
        return {
            str(row.get("subject_id")): row
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("subject_id"), str)
            and row.get("subject_id")
        }

    all_outgoing = all_subject_presence(shot)
    all_incoming = all_subject_presence(next_shot)
    for subject_id in sorted(all_outgoing.keys() & all_incoming.keys()):
        outgoing_row = all_outgoing[subject_id]
        incoming_row = all_incoming[subject_id]
        outgoing_presence = outgoing_row.get("exit_world_presence")
        incoming_presence = incoming_row.get("entry_world_presence")
        subject_type = outgoing_row.get("subject_type") or "subject"
        if outgoing_presence != incoming_presence:
            errors.append(
                f"{shot_path}.cut_to_next: {subject_type} {subject_id} changes world presence from "
                f"{outgoing_presence!r} to {incoming_presence!r} across a same-scene camera cut"
            )
            continue
        outgoing_zone = outgoing_row.get("exit_world_zone_id")
        incoming_zone = incoming_row.get("entry_world_zone_id")
        if (
            outgoing_zone is not None
            and incoming_zone is not None
            and outgoing_zone != incoming_zone
        ):
            errors.append(
                f"{shot_path}.cut_to_next: {subject_type} {subject_id} changes world zone from "
                f"{outgoing_zone!r} to {incoming_zone!r} without an in-shot move"
            )


def _validate_crowd_plan(
    shot: dict[str, Any],
    shot_path: str,
    selected_assets: list[dict[str, Any]],
    selected_identities: set[str],
    errors: list[str],
) -> None:
    crowd = shot.get("crowd_plan")
    if not isinstance(crowd, dict):
        errors.append(f"{shot_path}.crowd_plan: must be an object")
        return
    mode = crowd.get("mode")
    if mode not in CROWD_MODES:
        errors.append(f"{shot_path}.crowd_plan.mode: unsupported mode {mode!r}")
    target_count = crowd.get("target_count")
    if target_count is not None and (
        not isinstance(target_count, int) or isinstance(target_count, bool) or target_count < 0
    ):
        errors.append(f"{shot_path}.crowd_plan.target_count: must be null or a non-negative integer")
    critical = crowd.get("identity_critical_characters")
    if not isinstance(critical, list):
        errors.append(f"{shot_path}.crowd_plan.identity_critical_characters: must be an array")
        critical = []
    else:
        unknown = set(critical) - selected_identities
        if unknown:
            errors.append(
                f"{shot_path}.crowd_plan.identity_critical_characters: each identity needs a selected character asset; missing {sorted(unknown)}"
            )

    declared_population_ids = crowd.get("population_asset_ids")
    if not isinstance(declared_population_ids, list):
        errors.append(f"{shot_path}.crowd_plan.population_asset_ids: must be an array")
        declared_population_ids = []
    selected_population_ids = [
        asset.get("asset_id") for asset in selected_assets if asset.get("kind") == "population"
    ]
    if len(selected_population_ids) > 1:
        errors.append(f"{shot_path}.reference_asset_ids: select at most one population archetype")
    if set(declared_population_ids) != set(selected_population_ids):
        errors.append(
            f"{shot_path}.crowd_plan.population_asset_ids: must exactly match selected population assets"
        )

    for field in ("placement", "behavior", "face_readability"):
        _required_text(crowd, field, f"{shot_path}.crowd_plan", errors)
    if crowd.get("face_readability") not in FACE_READABILITY:
        errors.append(
            f"{shot_path}.crowd_plan.face_readability: unsupported value {crowd.get('face_readability')!r}"
        )

    if mode == "none":
        if target_count != 0 or critical or declared_population_ids:
            errors.append(f"{shot_path}.crowd_plan: mode 'none' requires count 0 and no crowd identities/assets")
    elif mode == "anonymous_background":
        if target_count is not None and target_count < 2:
            errors.append(f"{shot_path}.crowd_plan.target_count: an anonymous crowd requires at least two people")
        if critical:
            errors.append(f"{shot_path}.crowd_plan: anonymous background cannot contain identity-critical characters")
        if crowd.get("face_readability") == "identity_clear":
            errors.append(f"{shot_path}.crowd_plan.face_readability: anonymous faces cannot be identity-clear")
    elif mode == "recurring_cohort":
        if target_count is not None and target_count < 2:
            errors.append(f"{shot_path}.crowd_plan.target_count: a cohort requires at least two people")
        if len(declared_population_ids) != 1:
            errors.append(f"{shot_path}.crowd_plan: a recurring non-identity cohort needs one population archetype")
    elif mode == "identity_critical_ensemble":
        if len(critical) < 2:
            errors.append(f"{shot_path}.crowd_plan: an identity-critical ensemble needs at least two named identities")
        if target_count is not None and target_count != len(critical):
            errors.append(f"{shot_path}.crowd_plan.target_count: must equal the number of critical identities")
        if declared_population_ids:
            errors.append(f"{shot_path}.crowd_plan: a population archetype cannot replace critical identities")
        if crowd.get("face_readability") != "identity_clear":
            errors.append(f"{shot_path}.crowd_plan.face_readability: critical ensemble faces must be identity-clear")


def _validate_frame_envelope(frame: Any, path: str, errors: list[str]) -> None:
    if not isinstance(frame, dict):
        errors.append(f"{path}: must be an object")
        return
    if frame.get("shot_size") not in SHOT_SIZES:
        errors.append(f"{path}.shot_size: unsupported shot size {frame.get('shot_size')!r}")
    for field in (
        "camera_angle",
        "visual_focus",
        "subject_layout",
        "eyeline",
        "action_phase",
        "acceptable_variation",
    ):
        _required_text(frame, field, path, errors)
    if not isinstance(frame.get("state_summary"), dict):
        errors.append(f"{path}.state_summary: must be an object")


def _validate_narrative_visualization(shot: dict[str, Any], path: str, errors: list[str]) -> None:
    visualization = shot.get("narrative_visualization")
    if visualization is None:
        return
    if not isinstance(visualization, dict):
        errors.append(f"{path}.narrative_visualization: must be an object")
        return
    mode = visualization.get("mode")
    layer = visualization.get("narrative_layer")
    duplicate_policy = visualization.get("duplicate_identity_policy")
    if mode not in NARRATIVE_VISUALIZATION_MODES:
        errors.append(f"{path}.narrative_visualization.mode: unsupported mode {mode!r}")
    if layer not in NARRATIVE_LAYERS:
        errors.append(f"{path}.narrative_visualization.narrative_layer: unsupported layer {layer!r}")
    if duplicate_policy not in DUPLICATE_IDENTITY_POLICIES:
        errors.append(
            f"{path}.narrative_visualization.duplicate_identity_policy: unsupported policy {duplicate_policy!r}"
        )
    for field in (
        "reality_anchor",
        "content_to_visualize",
        "entry_cue",
        "exit_cue",
        "ppt_readability_strategy",
    ):
        _required_text(visualization, field, f"{path}.narrative_visualization", errors)
    if mode == "speech_visual_bubble" and duplicate_policy != "framed_representation_only":
        errors.append(
            f"{path}.narrative_visualization: speech_visual_bubble requires framed_representation_only"
        )
    if mode == "flashback" and layer != "memory":
        errors.append(f"{path}.narrative_visualization: flashback requires narrative_layer='memory'")
    if mode in {"literal_action", "speaker_performance", "listener_reaction"} and layer != "current_fact":
        errors.append(
            f"{path}.narrative_visualization: {mode} must preserve narrative_layer='current_fact'"
        )


def _validate_cut_contract(
    shot: dict[str, Any],
    next_shot: dict[str, Any] | None,
    shot_path: str,
    groups: dict[str, dict[str, Any]],
    current_assets: list[dict[str, Any]],
    next_assets: list[dict[str, Any]],
    strict_v3: bool,
    errors: list[str],
) -> None:
    contract = shot.get("cut_to_next")
    if next_shot is None:
        if contract is not None:
            errors.append(f"{shot_path}.cut_to_next: the last shot must use null")
        return
    if not isinstance(contract, dict):
        errors.append(f"{shot_path}.cut_to_next: every non-final shot needs a cut contract")
        return
    if contract.get("next_shot_id") != next_shot.get("shot_id"):
        errors.append(f"{shot_path}.cut_to_next.next_shot_id: must point to the immediate next shot")
    _required_text(contract, "motivation", f"{shot_path}.cut_to_next", errors)
    cut_type = contract.get("cut_type")
    if cut_type not in CUT_TYPES:
        errors.append(f"{shot_path}.cut_to_next.cut_type: unsupported cut type {cut_type!r}")
    next_relation = next_shot.get("relation_to_previous")
    expected_relations = {
        "shot_reverse_shot": "same_scene_angle_change",
        "reaction": "reaction_cut",
        "match_action": "match_action",
        "scene_transition": "new_scene",
    }
    if cut_type in expected_relations and next_relation != expected_relations[cut_type]:
        errors.append(
            f"{shot_path}.cut_to_next.cut_type: {cut_type!r} requires next relation {expected_relations[cut_type]!r}"
        )
    if next_relation == "new_scene" and cut_type != "scene_transition":
        errors.append(f"{shot_path}.cut_to_next.cut_type: a new scene requires 'scene_transition'")

    bindings = contract.get("continuity_bindings")
    if not isinstance(bindings, list) or not bindings:
        errors.append(f"{shot_path}.cut_to_next.continuity_bindings: must be a non-empty array")
    else:
        dimensions: list[Any] = []
        for index, binding in enumerate(bindings):
            path = f"{shot_path}.cut_to_next.continuity_bindings[{index}]"
            if not isinstance(binding, dict):
                errors.append(f"{path}: must be an object")
                continue
            dimensions.append(binding.get("dimension"))
            if binding.get("dimension") not in CONTINUITY_DIMENSIONS:
                errors.append(f"{path}.dimension: unsupported continuity dimension {binding.get('dimension')!r}")
            outgoing = binding.get("outgoing_value")
            incoming = binding.get("incoming_value")
            _required_text(binding, "outgoing_value", path, errors)
            _required_text(binding, "incoming_value", path, errors)
            required = binding.get("match_required")
            if not isinstance(required, bool):
                errors.append(f"{path}.match_required: must be a boolean")
            elif required and outgoing != incoming:
                errors.append(f"{path}: a required continuity match must use equal outgoing and incoming values")
            elif not required and outgoing == incoming:
                errors.append(f"{path}: an intentional change must use different outgoing and incoming values")
        if _duplicates(dimensions):
            errors.append(f"{shot_path}.cut_to_next.continuity_bindings: dimensions must be unique")
        if strict_v3 and next_shot.get("relation_to_previous") != "new_scene":
            dimension_set = set(dimensions)
            required_dimensions = {"axis", "spatial_relation"}
            current_identities = {
                asset.get("identity_id")
                for asset in current_assets
                if asset.get("kind") == "character" and asset.get("identity_id")
            }
            next_identities = {
                asset.get("identity_id")
                for asset in next_assets
                if asset.get("kind") == "character" and asset.get("identity_id")
            }
            current_props = {
                asset.get("prop_id")
                for asset in current_assets
                if asset.get("kind") == "prop" and asset.get("prop_id")
            }
            next_props = {
                asset.get("prop_id")
                for asset in next_assets
                if asset.get("kind") == "prop" and asset.get("prop_id")
            }
            if current_identities & next_identities:
                required_dimensions.add("character_state")
            if current_props & next_props:
                required_dimensions.add("prop_state")
            if cut_type == "match_action":
                required_dimensions.add("action_phase")
            missing = required_dimensions - dimension_set
            if missing:
                errors.append(
                    f"{shot_path}.cut_to_next.continuity_bindings: missing required dimensions {sorted(missing)}"
                )

    changes = contract.get("deliberate_changes")
    if not isinstance(changes, list) or not changes:
        errors.append(f"{shot_path}.cut_to_next.deliberate_changes: must name at least one intentional change")
        return
    unknown_changes = set(changes) - DELIBERATE_CHANGES
    if unknown_changes:
        errors.append(
            f"{shot_path}.cut_to_next.deliberate_changes: unsupported changes {sorted(unknown_changes)}"
        )
    current_frame = shot.get("closing_frame")
    next_frame = next_shot.get("opening_frame")
    if not isinstance(current_frame, dict) or not isinstance(next_frame, dict):
        return
    field_map = {
        "shot_size": "shot_size",
        "camera_angle": "camera_angle",
        "visual_focus": "visual_focus",
        "subject": "subject_layout",
        "action_phase": "action_phase",
    }
    for change in changes:
        field = field_map.get(change)
        if field and current_frame.get(field) == next_frame.get(field):
            errors.append(
                f"{shot_path}.cut_to_next.deliberate_changes: {change!r} is declared but both frame envelopes are identical"
            )
    if "location" in changes or "time" in changes:
        current_group = groups.get(shot.get("continuity_group"), {})
        next_group = groups.get(next_shot.get("continuity_group"), {})
        if "location" in changes and current_group.get("location") == next_group.get("location"):
            errors.append(f"{shot_path}.cut_to_next.deliberate_changes: location did not change")
        if "time" in changes and current_group.get("time_of_day") == next_group.get("time_of_day"):
            errors.append(f"{shot_path}.cut_to_next.deliberate_changes: time did not change")


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
        if before_state in ABSENT_PROP_STATES:
            if matching_entry:
                errors.append(
                    f"{path}: an absent entry state must not select a prop image for {prop_id!r}"
                )
        elif len(matching_entry) != 1:
            errors.append(f"{path}: current shot must select exactly one entry asset for prop {prop_id!r}")
        elif matching_entry[0].get("state_id") != before_state:
            errors.append(f"{path}.before_state: does not match the selected entry prop asset")

        next_asset_id = transition.get("next_shot_asset_id")
        next_asset = assets.get(next_asset_id) if isinstance(next_asset_id, str) else None
        if after_state in ABSENT_PROP_STATES:
            if next_asset_id is not None:
                errors.append(
                    f"{path}.next_shot_asset_id: an absent exit state must use null instead of a placeholder image"
                )
        elif next_asset is None:
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



def _validate_body_audio_window(plan: dict[str, Any], errors: list[str]) -> tuple[float, float] | None:
    """Validate an explicit body slice on the complete authoritative audio clock."""
    if "body_audio_window" not in plan:
        return None
    window = plan["body_audio_window"]
    label = "body_audio_window"
    if not isinstance(window, dict):
        errors.append(f"{label}: must be an object")
        return None
    start, end = window.get("source_start"), window.get("source_end")
    if not _number(start) or not _number(end) or start < 0 or end <= start:
        errors.append(f"{label}: must form a positive absolute audio interval")
        return None
    binding = window.get("authoritative_timeline_receipt")
    if not isinstance(binding, dict):
        errors.append(f"{label}: requires authoritative_timeline_receipt path and sha256")
        return None
    try:
        # The validator is also executed directly from its nested skill directory.
        root = str(Path(__file__).resolve().parents[3])
        if root not in sys.path:
            sys.path.insert(0, root)
        from story_timeline import validate_authoritative_timeline_receipt, file_sha256, _audio_duration
        receipt_path = Path(binding["path"]).expanduser().resolve()
        if file_sha256(receipt_path) != binding.get("sha256"):
            raise ValueError("authoritative receipt hash drift")
        receipt = validate_authoritative_timeline_receipt(receipt_path)
        audio = plan.get("source_audio", {})
        if not isinstance(audio, dict):
            raise ValueError("source_audio must be an object")
        bound_audio = receipt["authoritative_audio"]
        if (Path(audio.get("path", "")).expanduser().resolve() != Path(bound_audio["path"]).resolve()
                or audio.get("sha256") != bound_audio["sha256"]):
            raise ValueError("source_audio must bind the receipt's complete authoritative audio")
        duration = _audio_duration(Path(bound_audio["path"]))
        if not _number(audio.get("duration_seconds")) or abs(audio["duration_seconds"] - duration) > 0.001:
            raise ValueError("source_audio duration must equal the complete authoritative audio duration")
        if end > duration:
            raise ValueError("body window exceeds complete authoritative audio")
        rows = json.loads(Path(receipt["timings"]["path"]).read_text(encoding="utf-8"))
        if not any(row["source_start"] >= start and row["source_end"] <= end for row in rows):
            raise ValueError("body window contains no complete authoritative cue")
        for row in rows:
            for boundary in (start, end):
                if row["source_start"] < boundary < row["source_end"]:
                    raise ValueError("body window cuts an authoritative subtitle cue")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        errors.append(f"{label}: {exc}")
        return None
    return float(start), float(end)


def validate_plan(plan: Any, *, require_current_schema: bool = False) -> list[str]:
    """Return human-readable validation errors; an empty list means valid."""
    errors: list[str] = []
    if not isinstance(plan, dict):
        return ["plan: must be a JSON object"]
    schema_version = plan.get("schema_version")
    if require_current_schema and schema_version != SCHEMA_VERSION:
        errors.append(
            f"schema_version: new production must use current {SCHEMA_VERSION!r}"
        )
    if schema_version != SCHEMA_VERSION and schema_version not in LEGACY_SCHEMA_VERSIONS:
        errors.append(
            f"schema_version: must equal {SCHEMA_VERSION!r} or a supported legacy version"
        )
    strict_v4 = schema_version == SCHEMA_VERSION
    strict_v3_or_newer = schema_version in {"story-r2v-plan-v3", SCHEMA_VERSION}
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

    body_window = _validate_body_audio_window(plan, errors)
    policies = _validate_policies(plan, errors)
    assets = _index_by_id(plan.get("assets"), "asset_id", "assets", errors)
    groups = _index_by_id(plan.get("continuity_groups"), "group_id", "continuity_groups", errors)
    shots = _index_by_id(plan.get("shots"), "shot_id", "shots", errors)
    _validate_assets(assets, errors, strict_v3=strict_v3_or_newer)
    _validate_prop_state_families(assets, errors, strict_v3=strict_v3_or_newer)
    _validate_groups(groups, assets, errors)
    v4_structures = _validate_v4_groups(groups, assets, errors) if strict_v4 else {}

    previous_shot: dict[str, Any] | None = None
    shot_items = list(shots.items())
    for index, (shot_id, shot) in enumerate(shot_items):
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
                elif body_window is not None and abs(start - previous_shot["source_end"]) > 1e-6:
                    errors.append(f"{path}.source_start: body_audio_window requires continuous shot coverage")
                elif strict_v3_or_newer and start - previous_shot["source_end"] > MAX_TIMELINE_GAP_SECONDS:
                    errors.append(
                        f"{path}.source_start: leaves an unassigned timeline gap of "
                        f"{start - previous_shot['source_end']:.3f}s; absorb pauses into an adjacent shot"
                    )
            min_ratio = policies.get("min_retime_ratio")
            max_ratio = policies.get("max_retime_ratio")
            if provider_seconds in PROVIDER_DURATIONS and _number(min_ratio) and _number(max_ratio):
                ratio = (end - start) / provider_seconds
                trim = shot.get("assembly_trim")
                continuous_trim = False
                if isinstance(trim, dict) and set(trim) <= {"anchor", "start_second"} and trim.get("anchor") in {"start", "center", "end", "explicit"}:
                    trim_start = trim.get("start_second")
                    valid_anchor = (
                        _number(trim_start) and trim_start >= 0
                        if trim.get("anchor") == "explicit" else trim_start is None
                    )
                    offset = trim_start if trim.get("anchor") == "explicit" and _number(trim_start) else 0
                    continuous_trim = (valid_anchor and end - start < provider_seconds
                                       and offset + end - start <= provider_seconds + EPSILON)
                # A declared continuous source trim does not accelerate playback.
                # Keep the original retiming bounds for all undeclared/invalid trims.
                if (ratio < min_ratio - EPSILON and not continuous_trim) or ratio > max_ratio + EPSILON:
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

        storyboard_mode = str(shot.get("storyboard_reference_mode") or "runtime").strip()
        if storyboard_mode not in {"runtime", "director_only"}:
            errors.append(f"{path}.storyboard_reference_mode: unsupported mode {storyboard_mode!r}")
        if storyboard_mode == "director_only":
            _required_text(shot, "storyboard_reference_reason", path, errors)
        _validate_narrative_visualization(shot, path, errors)

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
        elif group is not None:
            if strict_v4:
                structure = v4_structures.get(str(group_id), {})
                setup_id = (shot.get("camera_plan") or {}).get("camera_setup_id")
                setup = structure.get("camera_setups", {}).get(setup_id)
                expected_view_id = (
                    setup.get("environment_view_asset_id") if isinstance(setup, dict) else None
                )
                if environment_assets[0].get("asset_id") != expected_view_id:
                    errors.append(
                        f"{path}.reference_asset_ids: selected environment must match the camera setup view asset"
                    )
            elif environment_assets[0].get("asset_id") != group.get("environment_asset_id"):
                errors.append(f"{path}.reference_asset_ids: selected environment does not match continuity group")

        character_assets = [asset for asset in selected_assets if asset.get("kind") == "character"]
        selected_identities = {asset.get("identity_id") for asset in character_assets if asset.get("identity_id")}
        identity_list = [asset.get("identity_id") for asset in character_assets if asset.get("identity_id")]
        if _duplicates(identity_list):
            errors.append(f"{path}.reference_asset_ids: select only one runtime asset per character identity")
        prop_ids = [asset.get("prop_id") for asset in selected_assets if asset.get("kind") == "prop"]
        if _duplicates(prop_ids):
            errors.append(f"{path}.reference_asset_ids: select only one entry state per prop")
        storyboard_assets = [asset for asset in selected_assets if asset.get("kind") == "storyboard"]
        if len(storyboard_assets) > 1:
            errors.append(f"{path}.reference_asset_ids: select at most one semantic storyboard")
        elif storyboard_assets:
            storyboard = storyboard_assets[0]
            if reference_ids[-1] != storyboard.get("asset_id"):
                errors.append(f"{path}.reference_asset_ids: semantic storyboard must be the final reference")
            unknown_storyboard_identities = set(storyboard.get("contains_characters") or []) - selected_identities
            if unknown_storyboard_identities:
                errors.append(
                    f"{path}.reference_asset_ids: storyboard identities need independent character assets; missing {sorted(unknown_storyboard_identities)}"
                )

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

        _validate_crowd_plan(shot, path, selected_assets, selected_identities, errors)

        camera_plan = shot.get("camera_plan")
        if not isinstance(camera_plan, dict):
            errors.append(f"{path}.camera_plan: must be an object")
        else:
            camera_fields = ["start_size", "end_size", "movement", "screen_direction"]
            camera_fields.append("axis_id" if strict_v4 else "axis")
            if strict_v4:
                camera_fields.append("camera_setup_id")
            for field in camera_fields:
                _required_text(camera_plan, field, f"{path}.camera_plan", errors)
            if camera_plan.get("internal_cut") is not False:
                errors.append(f"{path}.camera_plan.internal_cut: one R2V task must be one continuous take")

        for field in ("entry_state", "exit_state"):
            if not isinstance(shot.get(field), dict):
                errors.append(f"{path}.{field}: must be an object")
        _validate_frame_envelope(shot.get("opening_frame"), f"{path}.opening_frame", errors)
        _validate_frame_envelope(shot.get("closing_frame"), f"{path}.closing_frame", errors)
        next_shot = shot_items[index + 1][1] if index + 1 < len(shot_items) else None
        next_assets: list[dict[str, Any]] = []
        if isinstance(next_shot, dict):
            for asset_id in next_shot.get("reference_asset_ids") or []:
                asset = assets.get(asset_id)
                if asset is not None:
                    next_assets.append(asset)
        _validate_cut_contract(
            shot,
            next_shot,
            path,
            groups,
            selected_assets,
            next_assets,
            strict_v3_or_newer,
            errors,
        )
        if strict_v4:
            structure = v4_structures.get(str(group_id), {})
            _validate_v4_shot_contracts(
                shot,
                path,
                selected_assets,
                assets,
                structure,
                errors,
            )
            _validate_v4_reverse_cut(shot, next_shot, path, v4_structures, errors)
            _validate_v4_cross_shot_continuity(shot, next_shot, path, errors)

        assembly_trim = shot.get("assembly_trim")
        if assembly_trim is not None:
            if not isinstance(assembly_trim, dict):
                errors.append(f"{path}.assembly_trim: must be an object")
            else:
                anchor = assembly_trim.get("anchor")
                if anchor not in {"start", "center", "end", "explicit"}:
                    errors.append(f"{path}.assembly_trim.anchor: unsupported trim anchor")
                start_second = assembly_trim.get("start_second")
                if valid_window and provider_seconds in PROVIDER_DURATIONS:
                    length = end - start
                    offset = start_second if anchor == "explicit" and _number(start_second) else 0
                    if length >= provider_seconds or offset + length > provider_seconds + EPSILON:
                        errors.append(f"{path}.assembly_trim: continuous trim must stay within source and be shorter than it")
                if anchor == "explicit":
                    if not _number(start_second) or start_second < 0:
                        errors.append(
                            f"{path}.assembly_trim.start_second: explicit trim requires a non-negative number"
                        )
                elif start_second is not None:
                    errors.append(
                        f"{path}.assembly_trim.start_second: only explicit trim may set start_second"
                    )

        _validate_beats(shot, path, selected_identities, errors)
        _validate_prop_transitions(shot, path, selected_assets, assets, errors)
        if not isinstance(shot.get("prompt"), str) or not shot.get("prompt").strip():
            errors.append(f"{path}.prompt: must be a non-empty director prompt")
        previous_shot = shot

    if body_window is not None and shot_items:
        first_start = shot_items[0][1].get("source_start")
        final_end = shot_items[-1][1].get("source_end")
        if not _number(first_start) or abs(first_start - body_window[0]) > 1e-6:
            errors.append("shots: first source_start must equal body_audio_window.source_start")
        if not _number(final_end) or abs(final_end - body_window[1]) > 1e-6:
            errors.append("shots: final source_end must equal body_audio_window.source_end")
    elif strict_v3_or_newer and shot_items and _number(audio_duration):
        final_end = shot_items[-1][1].get("source_end")
        if _number(final_end) and abs(audio_duration - final_end) > MAX_TIMELINE_GAP_SECONDS:
            errors.append(
                f"shots: final source_end must reach the authoritative audio duration within "
                f"{MAX_TIMELINE_GAP_SECONDS:.2f}s"
            )

    return errors


def creative_advisories(plan: Any) -> list[str]:
    """Return non-blocking prompts for human directorial judgment.

    These observations deliberately do not participate in ``valid``. They
    expose places worth discussing without turning shot variety or a chosen
    visualization device into a numerical delivery rule.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("shots"), list):
        return []
    shots = [shot for shot in plan["shots"] if isinstance(shot, dict)]
    advisories: list[str] = []

    for shot in shots:
        shot_id = str(shot.get("shot_id") or "unknown-shot")
        speaker = str(shot.get("audio_speaker") or "").strip()
        start = shot.get("source_start")
        end = shot.get("source_end")
        duration = end - start if _number(start) and _number(end) and end > start else None
        text = str(shot.get("story_text") or "")
        visualization = shot.get("narrative_visualization")
        if (
            speaker.lower() not in NON_CHARACTER_SPEAKERS
            and "narrator" not in speaker.lower()
            and "旁白" not in speaker
            and duration is not None
            and duration >= 8.0
            and not isinstance(visualization, dict)
        ):
            advisories.append(
                f"{shot_id}: 较长角色台词未使用 narrative_visualization；"
                "请审核现有 visual_focus/表演节拍是否已明确说明画面承担。"
                "保持说话者持续表演可以是正确选择，此项不阻断。"
            )
        if any(cue in text for cue in MODALITY_CUES) and not isinstance(visualization, dict):
            advisories.append(
                f"{shot_id}: 文本含设想/回忆/计划线索但未显式声明叙事层；"
                "请人工确认当前物理事实没有被台词偷偷改写。"
            )

    for previous, current in zip(shots, shots[1:]):
        previous_frame = previous.get("opening_frame")
        current_frame = current.get("opening_frame")
        if not isinstance(previous_frame, dict) or not isinstance(current_frame, dict):
            continue
        fields = ("shot_size", "camera_angle", "subject_layout")
        if all(
            str(previous_frame.get(field) or "").strip()
            and str(previous_frame.get(field) or "").strip()
            == str(current_frame.get(field) or "").strip()
            for field in fields
        ):
            advisories.append(
                f"{previous.get('shot_id')} → {current.get('shot_id')}: 入口景别、机位和主体布局高度相似；"
                "请审核是否有情绪、动作、信息或空间关系上的保留理由。"
                "相似本身不是错误，不按次数或比例退回。"
            )
    return advisories


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="Path to story_r2v_plan.json")
    parser.add_argument(
        "--require-current-schema",
        action="store_true",
        help="Reject legacy v2/v3 plans at a new-production or paid-submission gate",
    )
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.plan.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 2
    errors = validate_plan(payload, require_current_schema=args.require_current_schema)
    advisories = creative_advisories(payload)
    print(
        json.dumps(
            {
                "valid": not errors,
                "error_count": len(errors),
                "errors": errors,
                "advisory_count": len(advisories),
                "creative_advisories": advisories,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
