import copy
import importlib.util
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = (
    PROJECT_ROOT / "skills" / "story-r2v-director" / "scripts" / "validate_r2v_plan.py"
)
SPEC = importlib.util.spec_from_file_location("story_r2v_plan_validator", VALIDATOR_PATH)
VALIDATOR = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(VALIDATOR)


def _asset(asset_id, kind, source_kind, **extra):
    return {
        "asset_id": asset_id,
        "kind": kind,
        "path": f"/assets/{asset_id}.png",
        "sha256": "a" * 64,
        "runtime_eligible": True,
        "contains_characters": [],
        "design_source_kind": source_kind,
        **extra,
    }


def valid_plan():
    return {
        "schema_version": "story-r2v-plan-v2",
        "story_id": "sample-story",
        "source_audio": {
            "path": "/source/audio.wav",
            "sha256": "b" * 64,
            "duration_seconds": 10.0,
        },
        "policies": {
            "body_generation_mode": "reference_to_video",
            "duration_choices": [6, 10],
            "max_reference_images": 7,
            "min_retime_ratio": 0.8,
            "max_retime_ratio": 1.3,
            "max_attempts_per_shot": 2,
        },
        "assets": [
            _asset(
                "character-a",
                "character",
                "single_view",
                identity_id="character-a",
                contains_characters=["character-a"],
                appearance_summary="A child in a simple coat",
                scale_class="child",
            ),
            _asset(
                "character-b",
                "character",
                "single_view",
                identity_id="character-b",
                contains_characters=["character-b"],
                appearance_summary="An older adult in a long coat",
                scale_class="adult",
            ),
            _asset("environment-a", "environment", "empty_environment"),
            _asset(
                "prop-before",
                "prop",
                "isolated_prop",
                prop_id="prop-a",
                state_id="complete",
            ),
            _asset(
                "prop-after",
                "prop",
                "isolated_prop",
                prop_id="prop-a",
                state_id="one-part-removed",
            ),
        ],
        "continuity_groups": [
            {
                "group_id": "scene-a",
                "environment_asset_id": "environment-a",
                "location": "garden path",
                "time_of_day": "day",
                "lighting": "soft daylight from camera left",
                "anchors": ["stone path", "large tree"],
            }
        ],
        "shots": [
            {
                "shot_id": "shot-001",
                "generation_mode": "reference_to_video",
                "source_start": 0.0,
                "source_end": 10.0,
                "story_text": "One character approaches and a visible prop change follows.",
                "provider_seconds": 10,
                "reference_asset_ids": [
                    "character-a",
                    "character-b",
                    "environment-a",
                    "prop-before",
                ],
                "continuity_group": "scene-a",
                "relation_to_previous": "first",
                "initial_visible_characters": ["character-a"],
                "entering_characters": ["character-b"],
                "exiting_characters": [],
                "crowd_plan": {
                    "mode": "none",
                    "target_count": 0,
                    "identity_critical_characters": [],
                    "population_asset_ids": [],
                    "placement": "no crowd",
                    "behavior": "no crowd",
                    "face_readability": "background_unreadable",
                },
                "audio_speaker": "narrator",
                "visual_focus": "the interaction and the prop transition",
                "camera_plan": {
                    "start_size": "wide",
                    "end_size": "medium",
                    "movement": "slow push in",
                    "axis": "keep both characters on the established axis",
                    "screen_direction": "left_to_right",
                    "internal_cut": False,
                },
                "opening_frame": {
                    "shot_size": "wide",
                    "camera_angle": "eye level from the established axis",
                    "visual_focus": "character-a before character-b enters",
                    "subject_layout": "character-a on frame right with open space on frame left",
                    "eyeline": "character-a looks toward frame left",
                    "action_phase": "before the approach",
                    "state_summary": {"prop-a": "complete"},
                    "acceptable_variation": "subject may shift within the right third",
                },
                "closing_frame": {
                    "shot_size": "medium",
                    "camera_angle": "eye level from the established axis",
                    "visual_focus": "both characters and the changed prop",
                    "subject_layout": "balanced two-shot on the same axis",
                    "eyeline": "the characters look toward each other",
                    "action_phase": "after the visible state change",
                    "state_summary": {"prop-a": "one-part-removed"},
                    "acceptable_variation": "both faces and the changed prop remain readable",
                },
                "cut_to_next": None,
                "entry_state": {"prop-a": "complete"},
                "exit_state": {"prop-a": "one-part-removed"},
                "performance_beats": [
                    {
                        "start_second": 0.0,
                        "end_second": 4.0,
                        "source_text": "The second character approaches.",
                        "performance": "Character B enters and establishes eye contact.",
                        "visible_characters": ["character-a", "character-b"],
                        "camera": "wide, slow push in",
                    },
                    {
                        "start_second": 4.0,
                        "end_second": 10.0,
                        "source_text": "A piece is visibly removed and released.",
                        "performance": "Contact, separation, release, then a readable changed prop.",
                        "visible_characters": ["character-a", "character-b"],
                        "prop_action": "remove one part and release it",
                        "camera": "continuous medium framing",
                    },
                ],
                "prop_state_transitions": [
                    {
                        "prop_id": "prop-a",
                        "before_state": "complete",
                        "visible_trigger_action": "hand maintains contact through visible separation",
                        "after_state": "one-part-removed",
                        "exit_evidence": "the main prop visibly lacks the removed part",
                        "next_shot_asset_id": "prop-after",
                    }
                ],
                "prompt": "One continuous take with ordered entrance, contact, separation, and reaction.",
            }
        ],
    }


def valid_two_shot_plan():
    plan = valid_plan()
    plan["source_audio"]["duration_seconds"] = 20.0
    second = copy.deepcopy(plan["shots"][0])
    second["shot_id"] = "shot-002"
    second["source_start"] = 10.0
    second["source_end"] = 20.0
    second["relation_to_previous"] = "same_scene_angle_change"
    second["opening_frame"]["shot_size"] = "close"
    second["opening_frame"]["visual_focus"] = "character-b reaction"
    second["opening_frame"]["subject_layout"] = "character-b fills the frame on the left third"
    second["opening_frame"]["action_phase"] = "reaction after the state change"
    second["cut_to_next"] = None
    plan["shots"][0]["cut_to_next"] = {
        "next_shot_id": "shot-002",
        "cut_type": "direct",
        "motivation": "move from the completed action to the other character's reaction",
        "continuity_bindings": [
            {
                "dimension": "axis",
                "outgoing_value": "established-axis-a",
                "incoming_value": "established-axis-a",
                "match_required": True,
            },
            {
                "dimension": "action_phase",
                "outgoing_value": "action complete",
                "incoming_value": "reaction begins",
                "match_required": False,
            },
        ],
        "deliberate_changes": ["shot_size", "visual_focus", "subject", "action_phase"],
    }
    plan["shots"].append(second)
    return plan


class StoryR2VPlanValidatorTests(unittest.TestCase):
    def assert_has_error(self, plan, fragment):
        errors = VALIDATOR.validate_plan(plan)
        self.assertTrue(
            any(fragment in error for error in errors),
            msg=f"Expected error containing {fragment!r}; got {errors}",
        )

    def test_accepts_valid_native_r2v_plan(self):
        self.assertEqual(VALIDATOR.validate_plan(valid_plan()), [])

    def test_rejects_i2v_story_body(self):
        plan = valid_plan()
        plan["shots"][0]["generation_mode"] = "image_to_video"
        self.assert_has_error(plan, "story body must use")

    def test_rejects_runtime_three_view_or_composite_reference(self):
        plan = valid_plan()
        plan["assets"][0]["design_source_kind"] = "three_view_sheet"
        self.assert_has_error(plan, "cannot be runtime eligible")
        self.assert_has_error(plan, "forbidden runtime sheet/composite")

    def test_rejects_character_bearing_environment(self):
        plan = valid_plan()
        plan["assets"][2]["contains_characters"] = ["character-a"]
        self.assert_has_error(plan, "environment assets must be empty scenes")

    def test_rejects_duplicate_identity_references(self):
        plan = valid_plan()
        duplicate = copy.deepcopy(plan["assets"][0])
        duplicate["asset_id"] = "character-a-side"
        plan["assets"].append(duplicate)
        plan["shots"][0]["reference_asset_ids"].append("character-a-side")
        self.assert_has_error(plan, "one runtime asset per character identity")

    def test_rejects_before_and_after_prop_assets_in_same_request(self):
        plan = valid_plan()
        plan["shots"][0]["reference_asset_ids"].append("prop-after")
        self.assert_has_error(plan, "one entry state per prop")
        self.assert_has_error(plan, "exactly one entry asset")

    def test_rejects_internal_cut_and_excessive_retime(self):
        plan = valid_plan()
        plan["shots"][0]["camera_plan"]["internal_cut"] = True
        plan["shots"][0]["source_end"] = 5.0
        self.assert_has_error(plan, "one continuous take")
        self.assert_has_error(plan, "retime ratio")

    def test_rejects_prop_exit_asset_with_wrong_state(self):
        plan = valid_plan()
        plan["assets"][4]["state_id"] = "wrong-state"
        self.assert_has_error(plan, "does not match next_shot_asset_id state")

    def test_accepts_anonymous_crowd_with_one_population_archetype(self):
        plan = valid_plan()
        plan["assets"].append(
            _asset(
                "population-a",
                "population",
                "population_archetype",
                population_id="background-visitors",
            )
        )
        shot = plan["shots"][0]
        shot["reference_asset_ids"].append("population-a")
        shot["crowd_plan"] = {
            "mode": "anonymous_background",
            "target_count": 12,
            "identity_critical_characters": [],
            "population_asset_ids": ["population-a"],
            "placement": "loosely distributed in the far background",
            "behavior": "quiet independent background activity",
            "face_readability": "background_unreadable",
        }
        self.assertEqual(VALIDATOR.validate_plan(plan), [])

    def test_identity_critical_ensemble_requires_each_character_asset(self):
        plan = valid_plan()
        shot = plan["shots"][0]
        shot["crowd_plan"] = {
            "mode": "identity_critical_ensemble",
            "target_count": 2,
            "identity_critical_characters": ["character-a", "character-b"],
            "population_asset_ids": [],
            "placement": "both characters readable in the foreground",
            "behavior": "distinct story actions",
            "face_readability": "identity_clear",
        }
        shot["reference_asset_ids"].remove("character-b")
        self.assert_has_error(plan, "each identity needs a selected character asset")

    def test_nonfinal_shot_requires_contract_to_immediate_next_shot(self):
        plan = valid_two_shot_plan()
        plan["shots"][0]["cut_to_next"] = None
        self.assert_has_error(plan, "every non-final shot needs a cut contract")

    def test_cut_contract_validates_frame_change_and_match_bindings(self):
        plan = valid_two_shot_plan()
        self.assertEqual(VALIDATOR.validate_plan(plan), [])
        binding = plan["shots"][0]["cut_to_next"]["continuity_bindings"][0]
        binding["incoming_value"] = "different-axis"
        self.assert_has_error(plan, "required continuity match must use equal")
        plan = valid_two_shot_plan()
        plan["shots"][1]["opening_frame"]["shot_size"] = "medium"
        self.assert_has_error(plan, "declared but both frame envelopes are identical")


if __name__ == "__main__":
    unittest.main()
