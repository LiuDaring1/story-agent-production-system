from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


DIRECTOR_BRIEF_VERSION = "story_image_director_brief_v1"
DIRECTOR_RESULT_VERSION = "story_image_director_result_v1"
DIRECTOR_DECISIONS_VERSION = "story_image_director_decisions_v1"
LEGACY_PLAN_VERSION = "storyboard_plan_v2"

SCREEN_DIRECTIONS = {
    "left_to_right",
    "right_to_left",
    "toward_camera",
    "away_from_camera",
    "stationary",
    "mixed",
}
MOTION_LEVELS = {"none", "low", "moderate", "high"}
MOTION_PRIMARIES = {"subject", "environment", "camera", "quiet"}
ACTION_VISIBILITIES = {"in_frame", "implied", "not_applicable"}


class DirectorProtocolError(ValueError):
    pass


def canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _nonempty(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise DirectorProtocolError("required text field is empty")
    return text


def _string_list(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise DirectorProtocolError(f"{field} must be a list of non-empty strings")
    return [item.strip() for item in value]


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DirectorProtocolError(f"{field} must be an object")
    return value


def _brief_catalogs(brief: Mapping[str, Any]) -> tuple[set[str], dict[str, set[str]], set[str], set[str]]:
    catalog = _mapping(brief.get("catalog"), field="catalog")
    characters = {
        _nonempty(item.get("character_id"))
        for item in catalog.get("characters", [])
        if isinstance(item, Mapping)
    }
    machines = {
        _nonempty(item.get("machine_id")): {
            _nonempty(state.get("state_id"))
            for state in item.get("states", [])
            if isinstance(state, Mapping)
        }
        for item in catalog.get("state_machines", [])
        if isinstance(item, Mapping)
    }
    scales = {
        _nonempty(item.get("relationship_id"))
        for item in catalog.get("scale_relationships", [])
        if isinstance(item, Mapping)
    }
    references = {
        _nonempty(item.get("reference_id"))
        for item in brief.get("approved_references", [])
        if isinstance(item, Mapping)
    }
    return characters, machines, scales, references


def validate_director_result(
    brief: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    expected_brief_sha256: str,
) -> list[dict[str, Any]]:
    if brief.get("schema_version") != DIRECTOR_BRIEF_VERSION:
        raise DirectorProtocolError("unsupported director brief version")
    if result.get("schema_version") != DIRECTOR_RESULT_VERSION:
        raise DirectorProtocolError("unsupported director result version")
    if result.get("brief_sha256") != expected_brief_sha256:
        raise DirectorProtocolError("director result is not bound to the current brief SHA-256")
    requested = [int(value) for value in brief.get("requested_scenes", [])]
    if not requested:
        raise DirectorProtocolError("director brief has no requested scenes")
    rows = result.get("decisions")
    if not isinstance(rows, list) or len(rows) != len(requested):
        raise DirectorProtocolError("director result must cover every requested scene exactly once")
    characters, machines, scales, references = _brief_catalogs(brief)
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in rows:
        row = _mapping(raw, field="decision")
        try:
            scene = int(row.get("scene"))
        except (TypeError, ValueError) as exc:
            raise DirectorProtocolError("decision.scene must be an integer") from exc
        if scene not in requested or scene in seen:
            raise DirectorProtocolError(f"unexpected or duplicate decision scene: {scene}")
        seen.add(scene)
        visible = _string_list(row.get("visible_characters"), field=f"scene {scene}.visible_characters")
        excluded = _string_list(row.get("excluded_characters"), field=f"scene {scene}.excluded_characters")
        if characters and any(value not in characters for value in visible + excluded):
            raise DirectorProtocolError(f"scene {scene} references an unknown character")
        if set(visible) & set(excluded):
            raise DirectorProtocolError(f"scene {scene} has a visible/excluded character conflict")
        subject = _nonempty(row.get("subject"))
        if characters and subject not in characters and subject not in {"environment", "prop", "narrator"}:
            raise DirectorProtocolError(f"scene {scene} has an unknown subject")
        knowledge = _mapping(row.get("character_knowledge"), field=f"scene {scene}.character_knowledge")
        if set(knowledge) != set(visible):
            raise DirectorProtocolError(f"scene {scene} character_knowledge must cover visible characters exactly")
        normalized_knowledge: dict[str, dict[str, Any]] = {}
        for character in visible:
            item = _mapping(knowledge.get(character), field=f"scene {scene}.character_knowledge.{character}")
            aware = _string_list(item.get("aware_of"), field=f"scene {scene}.{character}.aware_of")
            unaware = _string_list(item.get("unaware_of"), field=f"scene {scene}.{character}.unaware_of")
            if set(aware) & set(unaware):
                raise DirectorProtocolError(f"scene {scene} has conflicting knowledge for {character}")
            normalized_knowledge[character] = {
                "aware_of": aware,
                "unaware_of": unaware,
                "gaze_target": str(item.get("gaze_target") or "").strip(),
            }
        updates = dict(_mapping(row.get("state_updates", {}), field=f"scene {scene}.state_updates"))
        for machine_id, state_id in updates.items():
            if machine_id not in machines or str(state_id) not in machines[machine_id]:
                raise DirectorProtocolError(f"scene {scene} has an unknown state update: {machine_id}={state_id}")
        actions: list[dict[str, str]] = []
        raw_actions = row.get("required_visible_actions", [])
        if not isinstance(raw_actions, list):
            raise DirectorProtocolError(f"scene {scene}.required_visible_actions must be a list")
        for raw_action in raw_actions:
            action = _mapping(raw_action, field=f"scene {scene}.required_visible_actions")
            machine_id = _nonempty(action.get("machine_id"))
            if machine_id not in machines:
                raise DirectorProtocolError(f"scene {scene} action references unknown machine: {machine_id}")
            visibility = _nonempty(action.get("visibility"))
            if visibility not in ACTION_VISIBILITIES:
                raise DirectorProtocolError(f"scene {scene} action has invalid visibility")
            actions.append(
                {
                    "machine_id": machine_id,
                    "action": _nonempty(action.get("action")),
                    "subject": _nonempty(action.get("subject")),
                    "object": _nonempty(action.get("object")),
                    "visibility": visibility,
                }
            )
        scale_ids = _string_list(row.get("scale_relationship_ids"), field=f"scene {scene}.scale_relationship_ids")
        if any(value not in scales for value in scale_ids):
            raise DirectorProtocolError(f"scene {scene} references an unknown scale relationship")
        reference_ids = _string_list(row.get("reference_ids"), field=f"scene {scene}.reference_ids")
        if any(value not in references for value in reference_ids):
            raise DirectorProtocolError(f"scene {scene} references an unapproved image")
        location = _mapping(row.get("location"), field=f"scene {scene}.location")
        screen_direction = _nonempty(row.get("screen_direction"))
        if screen_direction not in SCREEN_DIRECTIONS:
            raise DirectorProtocolError(f"scene {scene} has invalid screen_direction")
        expected_motion = _mapping(row.get("expected_motion"), field=f"scene {scene}.expected_motion")
        if expected_motion.get("primary") not in MOTION_PRIMARIES:
            raise DirectorProtocolError(f"scene {scene} has invalid expected_motion.primary")
        if any(expected_motion.get(key) not in MOTION_LEVELS for key in ("subject_level", "environment_level", "camera_level")):
            raise DirectorProtocolError(f"scene {scene} has invalid expected_motion level")
        normalized.append(
            {
                "scene": scene,
                "continuity_group": _nonempty(row.get("continuity_group")),
                "location": {
                    "location_id": _nonempty(location.get("location_id")),
                    "time_of_day": _nonempty(location.get("time_of_day")),
                    "continuity_anchor": _nonempty(location.get("continuity_anchor")),
                    "change_cue": str(location.get("change_cue") or "").strip(),
                },
                "subject": subject,
                "shot_size": _nonempty(row.get("shot_size")),
                "visible_characters": visible,
                "excluded_characters": excluded,
                "character_knowledge": normalized_knowledge,
                "required_visible_actions": actions,
                "state_updates": {str(key): str(value) for key, value in updates.items()},
                "scale_relationship_ids": scale_ids,
                "reference_ids": reference_ids,
                "image_prompt": _nonempty(row.get("image_prompt")),
                "video_prompt": _nonempty(row.get("video_prompt")),
                "emotion": _nonempty(row.get("emotion")),
                "subject_action": _nonempty(row.get("subject_action")),
                "environment_motion": _nonempty(row.get("environment_motion")),
                "camera_motion": _nonempty(row.get("camera_motion")),
                "screen_direction": screen_direction,
                "expected_motion": {
                    "primary": str(expected_motion.get("primary")),
                    "subject_level": str(expected_motion.get("subject_level")),
                    "environment_level": str(expected_motion.get("environment_level")),
                    "camera_level": str(expected_motion.get("camera_level")),
                    "rationale": _nonempty(expected_motion.get("rationale")),
                },
            }
        )
    return sorted(normalized, key=lambda item: int(item["scene"]))


def merge_director_decisions(
    existing: Mapping[str, Any] | None,
    *,
    result: Mapping[str, Any],
    normalized_decisions: Sequence[Mapping[str, Any]],
    brief_path: Path,
    brief_sha256: str,
    result_path: Path,
    result_sha256: str,
) -> dict[str, Any]:
    by_scene: dict[str, dict[str, Any]] = {}
    receipts: list[dict[str, Any]] = []
    ledger: dict[str, Any] = {}
    if isinstance(existing, Mapping) and existing.get("schema_version") == DIRECTOR_DECISIONS_VERSION:
        raw = existing.get("decisions_by_scene", {})
        if isinstance(raw, Mapping):
            by_scene = {str(key): dict(value) for key, value in raw.items() if isinstance(value, Mapping)}
        if isinstance(existing.get("ingest_receipts"), list):
            receipts = [dict(item) for item in existing["ingest_receipts"] if isinstance(item, Mapping)]
        if isinstance(existing.get("continuity_ledger"), Mapping):
            ledger = dict(existing["continuity_ledger"])
    incoming_ledger = result.get("continuity_ledger")
    if isinstance(incoming_ledger, Mapping):
        ledger.update(dict(incoming_ledger))
    for row in normalized_decisions:
        by_scene[str(int(row["scene"]))] = dict(row)
    receipts.append(
        {
            "brief_path": str(brief_path),
            "brief_sha256": brief_sha256,
            "result_path": str(result_path),
            "result_sha256": result_sha256,
            "scenes": [int(row["scene"]) for row in normalized_decisions],
            "executor": str(result.get("executor") or "frontend_handoff"),
        }
    )
    coverage = sorted(int(value) for value in by_scene)
    return {
        "schema_version": DIRECTOR_DECISIONS_VERSION,
        "continuity_ledger": ledger,
        "coverage": coverage,
        "decisions_by_scene": by_scene,
        "ingest_receipts": receipts,
    }


@dataclass(frozen=True)
class CompiledDirectorPlan:
    payload: dict[str, Any]
    covered_scenes: tuple[int, ...]
    complete: bool


def compile_legacy_storyboard_plan(
    *,
    decisions: Mapping[str, Any],
    story_lines: Sequence[str],
    contract_projection: Mapping[str, Any],
    bindings: Mapping[str, Any],
    approved_references: Sequence[Mapping[str, Any]],
) -> CompiledDirectorPlan:
    raw_by_scene = decisions.get("decisions_by_scene", {})
    if not isinstance(raw_by_scene, Mapping):
        raise DirectorProtocolError("compiled director decisions are missing")
    coverage = sorted(int(value) for value in raw_by_scene)
    if coverage and coverage != list(range(1, max(coverage) + 1)):
        raise DirectorProtocolError("director decision coverage must be a contiguous prefix")
    machines = [item for item in contract_projection.get("story_state", {}).get("machines", []) if isinstance(item, Mapping)]
    machine_by_id = {str(item.get("machine_id")): item for item in machines}
    state = {machine_id: _nonempty(machine.get("initial_state")) for machine_id, machine in machine_by_id.items()}
    reference_by_id = {
        str(item.get("reference_id")): item
        for item in approved_references
        if isinstance(item, Mapping) and str(item.get("reference_id") or "")
    }
    rows: list[dict[str, Any]] = []
    previous_decision: Mapping[str, Any] | None = None
    previous_state = dict(state)
    for scene in coverage:
        if scene < 1 or scene > len(story_lines):
            raise DirectorProtocolError(f"director decision scene is out of range: {scene}")
        decision = _mapping(raw_by_scene.get(str(scene), raw_by_scene.get(scene)), field=f"decision {scene}")
        before = dict(state)
        transition_evidence: dict[str, dict[str, str]] = {}
        for machine_id, to_state in decision.get("state_updates", {}).items():
            machine = machine_by_id.get(str(machine_id))
            if machine is None:
                raise DirectorProtocolError(f"scene {scene} updates an unknown state machine")
            from_state = state[str(machine_id)]
            if to_state == from_state:
                continue
            transition = next(
                (
                    item
                    for item in machine.get("transitions", [])
                    if isinstance(item, Mapping) and item.get("from") == from_state and item.get("to") == to_state
                ),
                None,
            )
            if transition is None:
                raise DirectorProtocolError(
                    f"scene {scene} has undeclared state transition: {machine_id}:{from_state}->{to_state}"
                )
            matching_actions = [
                item
                for item in decision.get("required_visible_actions", [])
                if isinstance(item, Mapping) and item.get("machine_id") == machine_id and item.get("visibility") == "in_frame"
            ]
            if transition.get("must_show_action") is True:
                if not matching_actions:
                    raise DirectorProtocolError(f"scene {scene} omits visible transition action for {machine_id}")
                expected_subject = str(transition.get("action_subject") or "").strip()
                expected_object = str(transition.get("action_object") or "").strip()
                if expected_subject and all(item.get("subject") != expected_subject for item in matching_actions):
                    raise DirectorProtocolError(f"scene {scene} transition subject mismatch for {machine_id}")
                if expected_object and all(item.get("object") != expected_object for item in matching_actions):
                    raise DirectorProtocolError(f"scene {scene} transition object mismatch for {machine_id}")
            evidence_text = "；".join(str(item.get("action") or "") for item in matching_actions) or str(transition.get("trigger") or "")
            transition_evidence[str(machine_id)] = {
                "from": str(from_state),
                "to": str(to_state),
                "visibility": "in_frame" if matching_actions else "implied",
                "evidence": evidence_text,
            }
            state[str(machine_id)] = str(to_state)
        location = _mapping(decision.get("location"), field=f"scene {scene}.location")
        changed = previous_decision is not None and (
            location.get("location_id") != previous_decision.get("location", {}).get("location_id")
            or location.get("time_of_day") != previous_decision.get("location", {}).get("time_of_day")
        )
        same_group = previous_decision is not None and decision.get("continuity_group") == previous_decision.get("continuity_group")
        change_cue = str(location.get("change_cue") or "").strip()
        if changed and same_group and not change_cue:
            raise DirectorProtocolError(f"scene {scene} changes location/time inside one continuity group without evidence")
        reference_assets = [dict(reference_by_id[value]) for value in decision.get("reference_ids", []) if value in reference_by_id]
        scale_ids = list(decision.get("scale_relationship_ids", []))
        visual_state_evidence = {
            machine_id: (
                transition_evidence[machine_id]["evidence"]
                if machine_id in transition_evidence
                else f"Runtime 从上一镜继承未变化状态 {state_id}；本镜仅在与画面相关时呈现。"
            )
            for machine_id, state_id in state.items()
        }
        row = {
            "scene": scene,
            "story_text": story_lines[scene - 1],
            "narrative_function": "story_progression",
            "shot_size": decision["shot_size"],
            "focal_character": decision["subject"],
            "visible_characters": list(decision["visible_characters"]),
            "excluded_characters": list(decision["excluded_characters"]),
            "continuity_group": decision["continuity_group"],
            "appearance_ids": {
                character: f"appearance:{character}:approved"
                for character in decision["visible_characters"]
            },
            "visual_description": decision["image_prompt"],
            "image_prompt": decision["image_prompt"],
            "reference_assets": reference_assets,
            "scale_basis": {
                "applicable": bool(scale_ids),
                "relationship_ids": scale_ids,
                **(
                    {"evidence": "Runtime 绑定导演决策中与本镜叙事相关的定性比例关系。"}
                    if scale_ids
                    else {"reason": "本镜没有需要同框证明的合同尺度关系。"}
                ),
            },
            "current_story_state": dict(state),
            "visual_state_evidence": visual_state_evidence,
            "speaker": "旁白",
            "listener": "观众",
            "narrative_focus": decision["subject"],
            "emotion": decision["emotion"],
            "shot_intent": decision["image_prompt"],
            "transition_reason": change_cue or ("切换连续场景组" if changed else "延续上一镜连续性"),
            "location_state": {
                "location_id": location["location_id"],
                "time_of_day": location["time_of_day"],
                "change_from_previous": bool(changed),
                "change_cue": change_cue or ("连续场景组切换" if changed else ""),
                "continuity_anchor": location["continuity_anchor"],
            },
            "character_knowledge": dict(decision["character_knowledge"]),
            "required_visible_actions": list(decision["required_visible_actions"]),
            "state_transition_evidence": transition_evidence,
            "subject_action": decision["subject_action"],
            "environment_motion": decision["environment_motion"],
            "camera_motion": decision["camera_motion"],
            "entry_state": {
                "location_id": previous_decision.get("location", {}).get("location_id") if previous_decision else location["location_id"],
                "story_state": before,
            },
            "exit_state": {"location_id": location["location_id"], "story_state": dict(state)},
            "screen_direction": decision["screen_direction"],
            "adjacent_handoff": {
                "allows_direction_change": bool(changed),
                "allows_state_transition": bool(transition_evidence),
            },
            "expected_motion": dict(decision["expected_motion"]),
            "video_prompt": decision["video_prompt"],
        }
        rows.append(row)
        previous_decision = decision
        previous_state = dict(state)
    del previous_state
    payload = {
        "schema_version": LEGACY_PLAN_VERSION,
        "director_protocol_version": DIRECTOR_RESULT_VERSION,
        "status": "complete" if len(rows) == len(story_lines) else "partial",
        **dict(bindings),
        "shots": rows,
    }
    return CompiledDirectorPlan(payload, tuple(coverage), len(rows) == len(story_lines))


__all__ = [
    "DIRECTOR_BRIEF_VERSION",
    "DIRECTOR_DECISIONS_VERSION",
    "DIRECTOR_RESULT_VERSION",
    "CompiledDirectorPlan",
    "DirectorProtocolError",
    "canonical_json_sha256",
    "compile_legacy_storyboard_plan",
    "merge_director_decisions",
    "validate_director_result",
]
