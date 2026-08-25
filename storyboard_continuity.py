from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


ACTION_VISIBILITIES = {"in_frame", "implied", "not_applicable"}


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def storyboard_continuity_issues(
    shots: Sequence[Mapping[str, Any]],
    state_machines: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Validate the continuity facts that must survive image and video generation.

    The checks deliberately describe generic story mechanics: an uninterrupted
    location cannot silently change, a character cannot look at information it
    is declared not to know, and an irreversible state transition can require
    its causing action to be visible.  No story title, role name or prop is
    encoded here.
    """

    errors: list[str] = []
    machines = {
        str(machine.get("machine_id") or ""): machine
        for machine in state_machines
        if isinstance(machine, Mapping) and str(machine.get("machine_id") or "")
    }
    previous: Mapping[str, Any] | None = None
    for position, shot in enumerate(shots, start=1):
        prefix = f"scene_{position}"
        location = shot.get("location_state")
        if not isinstance(location, Mapping):
            errors.append(f"{prefix}:location_state_missing")
        else:
            for field in ("location_id", "time_of_day", "change_cue"):
                if not isinstance(location.get(field), str):
                    errors.append(f"{prefix}:location_state_{field}_invalid")
            if not isinstance(location.get("change_from_previous"), bool):
                errors.append(f"{prefix}:location_state_change_flag_invalid")

        visible = shot.get("visible_characters")
        knowledge = shot.get("character_knowledge")
        if not isinstance(visible, list) or not isinstance(knowledge, Mapping):
            errors.append(f"{prefix}:character_knowledge_missing")
        else:
            for character in (str(item) for item in visible):
                state = knowledge.get(character)
                if not isinstance(state, Mapping):
                    errors.append(f"{prefix}:character_knowledge_missing:{character}")
                    continue
                aware = state.get("aware_of")
                unaware = state.get("unaware_of")
                if not _string_list(aware) or not _string_list(unaware):
                    errors.append(f"{prefix}:character_knowledge_lists_invalid:{character}")
                    continue
                if set(aware) & set(unaware):
                    errors.append(f"{prefix}:character_knowledge_conflict:{character}")
                if not isinstance(state.get("gaze_target"), str):
                    errors.append(f"{prefix}:character_gaze_target_invalid:{character}")

        actions = shot.get("required_visible_actions")
        if not isinstance(actions, list):
            errors.append(f"{prefix}:required_visible_actions_missing")
            actions = []
        else:
            for action_index, action in enumerate(actions, start=1):
                if not isinstance(action, Mapping):
                    errors.append(f"{prefix}:visible_action_{action_index}_invalid")
                    continue
                for field in ("machine_id", "action", "subject", "object"):
                    if not isinstance(action.get(field), str) or not str(action.get(field)).strip():
                        errors.append(f"{prefix}:visible_action_{action_index}_{field}_invalid")
                if action.get("visibility") not in ACTION_VISIBILITIES:
                    errors.append(f"{prefix}:visible_action_{action_index}_visibility_invalid")

        transition_evidence = shot.get("state_transition_evidence")
        if not isinstance(transition_evidence, Mapping):
            errors.append(f"{prefix}:state_transition_evidence_missing")
            transition_evidence = {}

        if previous is not None:
            previous_location = previous.get("location_state")
            if (
                isinstance(location, Mapping)
                and isinstance(previous_location, Mapping)
                and shot.get("continuity_group") == previous.get("continuity_group")
            ):
                changed = (
                    location.get("location_id") != previous_location.get("location_id")
                    or location.get("time_of_day") != previous_location.get("time_of_day")
                )
                if changed and location.get("change_from_previous") is not True:
                    errors.append(f"{prefix}:silent_location_or_time_jump")
                if changed and not str(location.get("change_cue") or "").strip():
                    errors.append(f"{prefix}:location_change_cue_missing")

            before = previous.get("current_story_state")
            after = shot.get("current_story_state")
            if isinstance(before, Mapping) and isinstance(after, Mapping):
                for machine_id, to_state in after.items():
                    from_state = before.get(machine_id)
                    if from_state == to_state:
                        continue
                    machine = machines.get(str(machine_id))
                    transitions = machine.get("transitions", []) if isinstance(machine, Mapping) else []
                    transition = next(
                        (
                            item for item in transitions
                            if isinstance(item, Mapping)
                            and item.get("from") == from_state
                            and item.get("to") == to_state
                        ),
                        None,
                    )
                    if transition is None:
                        errors.append(f"{prefix}:undeclared_state_transition:{machine_id}:{from_state}->{to_state}")
                        continue
                    evidence = transition_evidence.get(machine_id)
                    if not isinstance(evidence, Mapping):
                        errors.append(f"{prefix}:state_transition_evidence_missing:{machine_id}")
                        continue
                    if evidence.get("from") != from_state or evidence.get("to") != to_state:
                        errors.append(f"{prefix}:state_transition_evidence_state_mismatch:{machine_id}")
                    if not str(evidence.get("evidence") or "").strip():
                        errors.append(f"{prefix}:state_transition_evidence_text_missing:{machine_id}")
                    if transition.get("must_show_action") is True:
                        if evidence.get("visibility") != "in_frame":
                            errors.append(f"{prefix}:required_transition_action_not_visible:{machine_id}")
                        matching_actions = [
                            item for item in actions
                            if isinstance(item, Mapping)
                            and item.get("machine_id") == machine_id
                            and item.get("visibility") == "in_frame"
                        ]
                        if not matching_actions:
                            errors.append(f"{prefix}:required_visible_action_missing:{machine_id}")
                        else:
                            expected_subject = str(transition.get("action_subject") or "").strip()
                            expected_object = str(transition.get("action_object") or "").strip()
                            if expected_subject and all(item.get("subject") != expected_subject for item in matching_actions):
                                errors.append(f"{prefix}:visible_action_subject_mismatch:{machine_id}")
                            if expected_object and all(item.get("object") != expected_object for item in matching_actions):
                                errors.append(f"{prefix}:visible_action_object_mismatch:{machine_id}")
        previous = shot
    return sorted(set(errors))


__all__ = ["ACTION_VISIBILITIES", "storyboard_continuity_issues"]
