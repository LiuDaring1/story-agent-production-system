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


SCHEMA_VERSION = "story-r2v-plan-v3"
LEGACY_SCHEMA_VERSIONS = {"story-r2v-plan-v2"}
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


def validate_plan(plan: Any) -> list[str]:
    """Return human-readable validation errors; an empty list means valid."""
    errors: list[str] = []
    if not isinstance(plan, dict):
        return ["plan: must be a JSON object"]
    schema_version = plan.get("schema_version")
    if schema_version != SCHEMA_VERSION and schema_version not in LEGACY_SCHEMA_VERSIONS:
        errors.append(
            f"schema_version: must equal {SCHEMA_VERSION!r} or a supported legacy version"
        )
    strict_v3 = schema_version == SCHEMA_VERSION
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
    _validate_assets(assets, errors, strict_v3=strict_v3)
    _validate_prop_state_families(assets, errors, strict_v3=strict_v3)
    _validate_groups(groups, assets, errors)

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
                elif strict_v3 and start - previous_shot["source_end"] > MAX_TIMELINE_GAP_SECONDS:
                    errors.append(
                        f"{path}.source_start: leaves an unassigned timeline gap of "
                        f"{start - previous_shot['source_end']:.3f}s; absorb pauses into an adjacent shot"
                    )
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
            for field in ("start_size", "end_size", "movement", "axis", "screen_direction"):
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
            strict_v3,
            errors,
        )

        assembly_trim = shot.get("assembly_trim")
        if assembly_trim is not None:
            if not isinstance(assembly_trim, dict):
                errors.append(f"{path}.assembly_trim: must be an object")
            else:
                anchor = assembly_trim.get("anchor")
                if anchor not in {"start", "center", "end", "explicit"}:
                    errors.append(f"{path}.assembly_trim.anchor: unsupported trim anchor")
                start_second = assembly_trim.get("start_second")
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

    if strict_v3 and shot_items and _number(audio_duration):
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
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.plan.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 2
    errors = validate_plan(payload)
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
