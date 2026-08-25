from __future__ import annotations

import unittest
from pathlib import Path

from story_image_director import (
    DIRECTOR_BRIEF_VERSION,
    DIRECTOR_RESULT_VERSION,
    DirectorProtocolError,
    compile_legacy_storyboard_plan,
    merge_director_decisions,
    validate_director_result,
)


def brief() -> dict:
    return {
        "schema_version": DIRECTOR_BRIEF_VERSION,
        "requested_scenes": [1, 2],
        "catalog": {
            "characters": [{"character_id": "hero"}],
            "state_machines": [
                {
                    "machine_id": "prop_inventory",
                    "initial_state": "full",
                    "states": [{"state_id": "full"}, {"state_id": "empty"}],
                    "transitions": [
                        {
                            "from": "full",
                            "to": "empty",
                            "trigger": "主角拿走道具",
                            "must_show_action": True,
                            "action_subject": "主角",
                            "action_object": "道具",
                        }
                    ],
                }
            ],
            "scale_relationships": [],
        },
        "approved_references": [
            {"reference_id": "style", "path": "/tmp/style.png", "sha256": "a" * 64}
        ],
    }


def decision(scene: int, *, update: bool) -> dict:
    return {
        "scene": scene,
        "continuity_group": "room",
        "location": {
            "location_id": "room",
            "time_of_day": "day",
            "continuity_anchor": "blue window",
            "change_cue": "",
        },
        "subject": "hero",
        "shot_size": "medium",
        "visible_characters": ["hero"],
        "excluded_characters": [],
        "character_knowledge": {
            "hero": {"aware_of": ["道具"], "unaware_of": [], "gaze_target": "道具"}
        },
        "required_visible_actions": (
            [
                {
                    "machine_id": "prop_inventory",
                    "action": "主角拿走道具",
                    "subject": "主角",
                    "object": "道具",
                    "visibility": "in_frame",
                }
            ]
            if update
            else []
        ),
        "state_updates": {"prop_inventory": "empty"} if update else {},
        "scale_relationship_ids": [],
        "reference_ids": ["style"],
        "image_prompt": f"第{scene}镜画面",
        "video_prompt": f"第{scene}镜轻微动作",
        "emotion": "专注",
        "subject_action": "自然伸手",
        "environment_motion": "窗帘轻动",
        "camera_motion": "稳定",
        "screen_direction": "stationary",
        "expected_motion": {
            "primary": "subject",
            "subject_level": "moderate",
            "environment_level": "low",
            "camera_level": "none",
            "rationale": "动作由人物承担",
        },
    }


class StoryImageDirectorTests(unittest.TestCase):
    def test_slim_result_compiles_full_inherited_legacy_state(self) -> None:
        source_brief = brief()
        result = {
            "schema_version": DIRECTOR_RESULT_VERSION,
            "brief_sha256": "b" * 64,
            "executor": "frontend_handoff",
            "continuity_ledger": {"appearance": {"hero": "hero-v1"}},
            "decisions": [decision(1, update=True), decision(2, update=False)],
        }
        normalized = validate_director_result(source_brief, result, expected_brief_sha256="b" * 64)
        merged = merge_director_decisions(
            None,
            result=result,
            normalized_decisions=normalized,
            brief_path=Path("/tmp/brief.json"),
            brief_sha256="b" * 64,
            result_path=Path("/tmp/result.json"),
            result_sha256="c" * 64,
        )
        projection = {
            "story_state": {"machines": source_brief["catalog"]["state_machines"]},
            "world_scale": {"relationships": []},
        }
        compiled = compile_legacy_storyboard_plan(
            decisions=merged,
            story_lines=["第一镜。", "第二镜。"],
            contract_projection=projection,
            bindings={"story_contract_sha256": "d" * 64},
            approved_references=source_brief["approved_references"],
        )
        self.assertTrue(compiled.complete)
        self.assertEqual(compiled.payload["shots"][0]["current_story_state"], {"prop_inventory": "empty"})
        self.assertEqual(compiled.payload["shots"][1]["current_story_state"], {"prop_inventory": "empty"})
        self.assertEqual(compiled.payload["shots"][1]["state_transition_evidence"], {})
        self.assertEqual(compiled.payload["shots"][0]["story_text"], "第一镜。")

    def test_result_is_rejected_when_brief_hash_or_reference_is_untrusted(self) -> None:
        source_brief = brief()
        result = {
            "schema_version": DIRECTOR_RESULT_VERSION,
            "brief_sha256": "wrong",
            "decisions": [decision(1, update=True), decision(2, update=False)],
        }
        with self.assertRaises(DirectorProtocolError):
            validate_director_result(source_brief, result, expected_brief_sha256="b" * 64)
        result["brief_sha256"] = "b" * 64
        result["decisions"][0]["reference_ids"] = ["not-approved"]
        with self.assertRaises(DirectorProtocolError):
            validate_director_result(source_brief, result, expected_brief_sha256="b" * 64)

    def test_required_state_transition_cannot_hide_its_action(self) -> None:
        source_brief = brief()
        first = decision(1, update=True)
        first["required_visible_actions"] = []
        result = {
            "schema_version": DIRECTOR_RESULT_VERSION,
            "brief_sha256": "b" * 64,
            "decisions": [first, decision(2, update=False)],
        }
        normalized = validate_director_result(source_brief, result, expected_brief_sha256="b" * 64)
        merged = merge_director_decisions(
            None,
            result=result,
            normalized_decisions=normalized,
            brief_path=Path("/tmp/brief.json"),
            brief_sha256="b" * 64,
            result_path=Path("/tmp/result.json"),
            result_sha256="c" * 64,
        )
        projection = {
            "story_state": {"machines": source_brief["catalog"]["state_machines"]},
            "world_scale": {"relationships": []},
        }
        with self.assertRaises(DirectorProtocolError):
            compile_legacy_storyboard_plan(
                decisions=merged,
                story_lines=["第一镜。", "第二镜。"],
                contract_projection=projection,
                bindings={},
                approved_references=source_brief["approved_references"],
            )


if __name__ == "__main__":
    unittest.main()
