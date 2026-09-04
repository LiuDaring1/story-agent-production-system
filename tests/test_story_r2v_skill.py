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


def _physical_contract(**overrides):
    contract = {
        "scale_basis": "handheld_small",
        "support_mode": "handheld",
        "rigidity": "rigid",
        "grip_or_contact": "held securely at the intended handle",
        "forbidden_inferences": ["must not become grounded or oversized"],
    }
    contract.update(overrides)
    return contract


def valid_plan():
    return {
        "schema_version": "story-r2v-plan-v3",
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
                scale_class="small handheld prop",
                state_family_id="prop-a-family",
                state_family_master_path="/assets/prop-a-family-master.png",
                state_family_master_sha256="c" * 64,
                state_delta="canonical complete state",
                physical_contract=_physical_contract(),
            ),
            _asset(
                "prop-after",
                "prop",
                "isolated_prop",
                prop_id="prop-a",
                state_id="one-part-removed",
                scale_class="small handheld prop",
                state_family_id="prop-a-family",
                state_family_master_path="/assets/prop-a-family-master.png",
                state_family_master_sha256="c" * 64,
                state_delta="exactly one intended part removed; all other geometry unchanged",
                physical_contract=_physical_contract(),
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
                "dimension": "spatial_relation",
                "outgoing_value": "character-a right of character-b",
                "incoming_value": "character-a right of character-b",
                "match_required": True,
            },
            {
                "dimension": "character_state",
                "outgoing_value": "both identities and wardrobes unchanged",
                "incoming_value": "both identities and wardrobes unchanged",
                "match_required": True,
            },
            {
                "dimension": "prop_state",
                "outgoing_value": "prop-a=one-part-removed",
                "incoming_value": "prop-a=one-part-removed",
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


def valid_v4_plan():
    plan = valid_plan()
    plan["schema_version"] = "story-r2v-plan-v4"
    master_environment = next(
        asset for asset in plan["assets"] if asset["asset_id"] == "environment-a"
    )
    master_environment["environment_family_id"] = "garden-environment-family"
    plan["assets"].extend(
        [
            _asset(
                "environment-view-a",
                "environment",
                "empty_environment_camera_view",
                environment_family_id="garden-environment-family",
                derived_from_asset_id="environment-a",
                view_from_zone_id="garden-left",
                view_target_zone_id="garden-right",
                view_background_zone_ids=["garden-far-right"],
            ),
            _asset(
                "environment-view-b",
                "environment",
                "empty_environment_camera_view",
                environment_family_id="garden-environment-family",
                derived_from_asset_id="environment-a",
                view_from_zone_id="garden-right",
                view_target_zone_id="garden-left",
                view_background_zone_ids=["garden-far-left"],
            ),
        ]
    )
    group = plan["continuity_groups"][0]
    group["zones"] = [
        {
            "zone_id": "garden-left",
            "description": "the left side of the path used by character-b",
            "map_x": 0,
            "map_y": 0,
        },
        {
            "zone_id": "garden-right",
            "description": "the right side of the path used by character-a",
            "map_x": 10,
            "map_y": 0,
        },
        {
            "zone_id": "garden-far-left",
            "description": "the garden backdrop beyond character-b from the reverse camera",
            "map_x": -10,
            "map_y": 0,
        },
        {
            "zone_id": "garden-far-right",
            "description": "the garden backdrop beyond character-a from the first camera",
            "map_x": 20,
            "map_y": 0,
        },
    ]
    group["anchors"] = [
        {
            "anchor_id": "stone-path",
            "zone_id": "garden-left",
            "description": "the fixed stone path",
            "persistence": "fixed",
            "occupancy_rule": "none",
            "occupant_subject_id": None,
        },
        {
            "anchor_id": "large-tree",
            "zone_id": "garden-right",
            "description": "the fixed large tree",
            "persistence": "fixed",
            "occupancy_rule": "none",
            "occupant_subject_id": None,
        },
    ]
    group["color_contract"] = {
        "white_balance": "neutral daylight",
        "palette": "fresh greens, natural browns and clean neutral highlights",
        "prohibited_casts": ["unmotivated full-frame yellow cast"],
        "rationale": "daytime garden scene",
    }
    group["axes"] = [
        {
            "axis_id": "garden-axis",
            "endpoint_a_zone_id": "garden-left",
            "endpoint_b_zone_id": "garden-right",
            "description": "the stable 180-degree line between both path zones",
        }
    ]
    group["camera_setups"] = [
        {
            "setup_id": "setup-character-a",
            "axis_id": "garden-axis",
            "camera_side": "side_a",
            "shot_size": "wide",
            "camera_angle": "eye level toward garden-right",
            "primary_subject_id": "character-a",
            "primary_subject_type": "character",
            "subject_zone_id": "garden-right",
            "screen_side": "right",
            "eyeline_direction": "camera_left",
            "camera_origin_zone_id": "garden-left",
            "look_target_zone_id": "garden-right",
            "background_zone_ids": ["garden-far-right"],
            "camera_position_description": "camera stands on the garden-left side and looks right",
            "environment_view_asset_id": "environment-view-a",
        },
        {
            "setup_id": "setup-character-b",
            "axis_id": "garden-axis",
            "camera_side": "side_a",
            "shot_size": "close",
            "camera_angle": "reverse close view toward garden-left",
            "primary_subject_id": "character-b",
            "primary_subject_type": "character",
            "subject_zone_id": "garden-left",
            "screen_side": "left",
            "eyeline_direction": "camera_right",
            "camera_origin_zone_id": "garden-right",
            "look_target_zone_id": "garden-left",
            "background_zone_ids": ["garden-far-left"],
            "camera_position_description": "camera stands on the garden-right side and looks left",
            "environment_view_asset_id": "environment-view-b",
        },
    ]

    shot = plan["shots"][0]
    shot["reference_asset_ids"] = [
        "environment-view-a" if value == "environment-a" else value
        for value in shot["reference_asset_ids"]
    ]
    shot["camera_plan"].pop("axis")
    shot["camera_plan"].update(
        {
            "axis_id": "garden-axis",
            "camera_setup_id": "setup-character-a",
            "visible_anchor_ids": ["large-tree"],
            "excluded_anchor_ids": ["stone-path"],
        }
    )
    shot["focus_contract"] = {
        "primary_subject_id": "character-a",
        "primary_subject_type": "character",
        "secondary_subject_ids": ["character-b"],
        "background_subject_ids": [],
        "off_screen_subject_ids": [],
        "visual_priority": "character-a and the visible prop transition",
        "composition_rule": "character-a remains dominant while character-b supports the action",
    }
    shot["subject_presence"] = [
        {
            "subject_id": "character-a",
            "subject_type": "character",
            "entry_presence": "on_screen",
            "exit_presence": "on_screen",
            "entry_zone_id": "garden-right",
            "exit_zone_id": "garden-right",
            "entry_world_presence": "in_scene",
            "exit_world_presence": "in_scene",
            "entry_world_zone_id": "garden-right",
            "exit_world_zone_id": "garden-right",
            "visibility_reason": "character-a performs the primary prop action",
        },
        {
            "subject_id": "character-b",
            "subject_type": "character",
            "entry_presence": "off_screen",
            "exit_presence": "on_screen",
            "entry_zone_id": None,
            "exit_zone_id": "garden-left",
            "entry_world_presence": "in_scene",
            "exit_world_presence": "in_scene",
            "entry_world_zone_id": "garden-left",
            "exit_world_zone_id": "garden-left",
            "visibility_reason": "character-b enters to support the interaction",
        },
    ]
    shot["prop_contracts"] = [
        {
            "prop_id": "prop-a",
            "asset_id": "prop-before",
            "entry_presence": "present",
            "exit_presence": "present",
            "entry_state": "complete",
            "exit_state": "one-part-removed",
            "owner_id": "character-a",
            "zone_id": "garden-right",
            "scale_basis": "handheld_small",
            "support_mode": "handheld",
            "rigidity": "rigid",
            "grip_or_contact": "held securely at the intended handle",
            "forbidden_inferences": ["must not become grounded or oversized"],
        }
    ]
    for index, beat in enumerate(shot["performance_beats"], start=1):
        primary_subject = "character-b" if index == 1 else "character-a"
        beat["primary_beat"] = {
            "subject_id": primary_subject,
            "action": beat["performance"],
        }
        beat["supporting_actions"] = [
            {
                "subject_id": "character-a" if primary_subject == "character-b" else "character-b",
                "action": "supports the primary action without taking over the frame",
            }
        ]
    return plan


def valid_v4_reverse_shot_plan():
    plan = valid_v4_plan()
    plan["source_audio"]["duration_seconds"] = 20.0
    first = plan["shots"][0]
    second = copy.deepcopy(first)
    second["shot_id"] = "shot-002"
    second["source_start"] = 10.0
    second["source_end"] = 20.0
    second["relation_to_previous"] = "same_scene_angle_change"
    second["reference_asset_ids"] = [
        "character-a",
        "character-b",
        "environment-view-b",
        "prop-after",
    ]
    second["initial_visible_characters"] = ["character-a", "character-b"]
    second["entering_characters"] = []
    second["entry_state"] = {"prop-a": "one-part-removed"}
    second["exit_state"] = {"prop-a": "one-part-removed"}
    second["camera_plan"].update(
        {
            "start_size": "close",
            "end_size": "close",
            "movement": "locked reverse close view",
            "camera_setup_id": "setup-character-b",
            "visible_anchor_ids": ["stone-path"],
            "excluded_anchor_ids": ["large-tree"],
        }
    )
    second["opening_frame"].update(
        {
            "shot_size": "close",
            "camera_angle": "reverse close view toward garden-left",
            "visual_focus": "character-b reaction",
            "subject_layout": "character-b fills the left third",
            "action_phase": "reaction after the state change",
        }
    )
    second["closing_frame"].update(
        {
            "shot_size": "close",
            "camera_angle": "reverse close view toward garden-left",
            "visual_focus": "character-b completes the reaction",
            "subject_layout": "character-b remains on the left third",
            "action_phase": "reaction settles",
        }
    )
    second["focus_contract"] = {
        "primary_subject_id": "character-b",
        "primary_subject_type": "character",
        "secondary_subject_ids": ["character-a"],
        "background_subject_ids": [],
        "off_screen_subject_ids": [],
        "visual_priority": "character-b reaction",
        "composition_rule": "character-b dominates the reverse close view",
    }
    second["subject_presence"] = [
        {
            "subject_id": "character-a",
            "subject_type": "character",
            "entry_presence": "on_screen",
            "exit_presence": "on_screen",
            "entry_zone_id": "garden-right",
            "exit_zone_id": "garden-right",
            "entry_world_presence": "in_scene",
            "exit_world_presence": "in_scene",
            "entry_world_zone_id": "garden-right",
            "exit_world_zone_id": "garden-right",
            "visibility_reason": "supporting eyeline partner",
        },
        {
            "subject_id": "character-b",
            "subject_type": "character",
            "entry_presence": "on_screen",
            "exit_presence": "on_screen",
            "entry_zone_id": "garden-left",
            "exit_zone_id": "garden-left",
            "entry_world_presence": "in_scene",
            "exit_world_presence": "in_scene",
            "entry_world_zone_id": "garden-left",
            "exit_world_zone_id": "garden-left",
            "visibility_reason": "primary reaction subject",
        },
    ]
    second["prop_contracts"] = [
        {
            "prop_id": "prop-a",
            "asset_id": "prop-after",
            "entry_presence": "present",
            "exit_presence": "present",
            "entry_state": "one-part-removed",
            "exit_state": "one-part-removed",
            "owner_id": "character-a",
            "zone_id": "garden-right",
            "scale_basis": "handheld_small",
            "support_mode": "handheld",
            "rigidity": "rigid",
            "grip_or_contact": "held securely at the intended handle",
            "forbidden_inferences": ["must not become grounded or oversized"],
        }
    ]
    second["prop_state_transitions"] = []
    for beat in second["performance_beats"]:
        beat["primary_beat"] = {
            "subject_id": "character-b",
            "action": "reacts to the completed prop change",
        }
        beat["supporting_actions"] = [
            {
                "subject_id": "character-a",
                "action": "holds the changed prop without taking over the frame",
            }
        ]
    first["cut_to_next"] = {
        "next_shot_id": "shot-002",
        "cut_type": "shot_reverse_shot",
        "motivation": "reverse from character-a's action to character-b's reaction",
        "continuity_bindings": [
            {
                "dimension": "axis",
                "outgoing_value": "garden-axis",
                "incoming_value": "garden-axis",
                "match_required": True,
            },
            {
                "dimension": "spatial_relation",
                "outgoing_value": "character-a garden-right; character-b garden-left",
                "incoming_value": "character-a garden-right; character-b garden-left",
                "match_required": True,
            },
            {
                "dimension": "character_state",
                "outgoing_value": "both identities and wardrobes unchanged",
                "incoming_value": "both identities and wardrobes unchanged",
                "match_required": True,
            },
            {
                "dimension": "prop_state",
                "outgoing_value": "prop-a=one-part-removed",
                "incoming_value": "prop-a=one-part-removed",
                "match_required": True,
            },
        ],
        "deliberate_changes": ["shot_size", "camera_angle", "visual_focus", "subject"],
    }
    first["closing_frame"]["shot_size"] = "wide"
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

    def test_current_production_gate_rejects_legacy_v3(self):
        errors = VALIDATOR.validate_plan(valid_plan(), require_current_schema=True)
        self.assertTrue(any("new production must use current" in error for error in errors))

    def test_v4_accepts_structured_zones_setups_presence_focus_props_and_primary_beats(self):
        self.assertEqual(VALIDATOR.validate_plan(valid_v4_plan()), [])

    def test_v4_requires_camera_origin_target_background_and_anchor_visibility(self):
        for field in (
            "camera_origin_zone_id",
            "look_target_zone_id",
            "background_zone_ids",
            "camera_position_description",
        ):
            with self.subTest(field=field):
                plan = valid_v4_plan()
                plan["continuity_groups"][0]["camera_setups"][0].pop(field)
                self.assert_has_error(plan, field)

    def test_v4_requires_a_camera_specific_empty_environment_view(self):
        plan = valid_v4_plan()
        plan["continuity_groups"][0]["camera_setups"][0]["environment_view_asset_id"] = "environment-a"
        self.assert_has_error(plan, "must use an empty_environment_camera_view")

    def test_v4_shot_must_select_its_camera_setup_environment_view(self):
        plan = valid_v4_plan()
        plan["shots"][0]["reference_asset_ids"] = [
            "environment-view-b" if value == "environment-view-a" else value
            for value in plan["shots"][0]["reference_asset_ids"]
        ]
        self.assert_has_error(plan, "must match the camera setup view asset")

    def test_v4_scene_map_rejects_a_background_on_the_camera_side_of_the_subject(self):
        plan = valid_v4_plan()
        setup = plan["continuity_groups"][0]["camera_setups"][0]
        setup["background_zone_ids"] = ["garden-left"]
        self.assert_has_error(plan, "is not geometrically behind the look target")

    def test_v4_rejects_an_on_screen_subject_behind_the_camera(self):
        plan = valid_v4_plan()
        plan["continuity_groups"][0]["zones"].append(
            {
                "zone_id": "behind-camera",
                "description": "physically behind the current camera position",
                "map_x": -10,
                "map_y": 0,
            }
        )
        supporting = plan["shots"][0]["subject_presence"][1]
        supporting["entry_presence"] = "on_screen"
        supporting["entry_zone_id"] = "behind-camera"
        supporting["entry_world_zone_id"] = "behind-camera"
        supporting["exit_zone_id"] = "behind-camera"
        supporting["exit_world_zone_id"] = "behind-camera"
        plan["shots"][0]["initial_visible_characters"].append("character-b")
        plan["shots"][0]["entering_characters"] = []
        self.assert_has_error(plan, "lies behind the camera on the scene map")

    def test_v4_selected_population_requires_spatial_presence_contract(self):
        plan = valid_v4_plan()
        plan["assets"].append(
            _asset(
                "audience-population",
                "population",
                "population_archetype",
                population_id="audience",
            )
        )
        plan["shots"][0]["reference_asset_ids"].append("audience-population")
        plan["shots"][0]["crowd_plan"] = {
            "mode": "recurring_cohort",
            "target_count": 5,
            "identity_critical_characters": [],
            "population_asset_ids": ["audience-population"],
            "placement": "audience seating behind the judge",
            "behavior": "watching the stage",
            "face_readability": "secondary",
        }
        self.assert_has_error(plan, "lacks an on-screen crowd presence contract")

    def test_v4_rejects_visible_audience_behind_a_front_camera(self):
        plan = valid_v4_plan()
        plan["assets"].append(
            _asset(
                "audience-population",
                "population",
                "population_archetype",
                population_id="audience",
            )
        )
        plan["shots"][0]["reference_asset_ids"].append("audience-population")
        plan["shots"][0]["crowd_plan"] = {
            "mode": "recurring_cohort",
            "target_count": 5,
            "identity_critical_characters": [],
            "population_asset_ids": ["audience-population"],
            "placement": "behind the camera",
            "behavior": "watching the stage",
            "face_readability": "secondary",
        }
        plan["shots"][0]["subject_presence"].append(
            {
                "subject_id": "audience",
                "subject_type": "crowd",
                "entry_presence": "on_screen",
                "exit_presence": "on_screen",
                "entry_zone_id": "garden-far-left",
                "exit_zone_id": "garden-far-left",
                "entry_world_presence": "in_scene",
                "exit_world_presence": "in_scene",
                "entry_world_zone_id": "garden-far-left",
                "exit_world_zone_id": "garden-far-left",
                "visibility_reason": "invalidly shown behind the camera",
            }
        )
        self.assert_has_error(plan, "lies behind the camera on the scene map")

    def test_v4_rejects_crowd_disappearing_across_same_scene_camera_cut(self):
        plan = valid_v4_reverse_shot_plan()
        plan["assets"].append(
            _asset(
                "audience-population",
                "population",
                "population_archetype",
                population_id="audience",
            )
        )
        first, second = plan["shots"]
        first["reference_asset_ids"].append("audience-population")
        first["crowd_plan"] = {
            "mode": "recurring_cohort",
            "target_count": 5,
            "identity_critical_characters": [],
            "population_asset_ids": ["audience-population"],
            "placement": "stable audience zone",
            "behavior": "watching",
            "face_readability": "secondary",
        }
        first["subject_presence"].append(
            {
                "subject_id": "audience",
                "subject_type": "crowd",
                "entry_presence": "on_screen",
                "exit_presence": "on_screen",
                "entry_zone_id": "garden-right",
                "exit_zone_id": "garden-right",
                "entry_world_presence": "in_scene",
                "exit_world_presence": "in_scene",
                "entry_world_zone_id": "garden-right",
                "exit_world_zone_id": "garden-right",
                "visibility_reason": "audience is visible in the first camera",
            }
        )
        second["subject_presence"].append(
            {
                "subject_id": "audience",
                "subject_type": "crowd",
                "entry_presence": "off_screen",
                "exit_presence": "off_screen",
                "entry_zone_id": None,
                "exit_zone_id": None,
                "entry_world_presence": "outside_scene",
                "exit_world_presence": "outside_scene",
                "entry_world_zone_id": None,
                "exit_world_zone_id": None,
                "visibility_reason": "invalidly disappears when the camera changes",
            }
        )
        self.assert_has_error(plan, "changes world presence")

    def test_v4_scene_map_coordinates_are_required(self):
        plan = valid_v4_plan()
        plan["continuity_groups"][0]["zones"][0].pop("map_x")
        self.assert_has_error(plan, "map_x")

    def test_v4_camera_must_look_through_the_primary_subject_zone(self):
        plan = valid_v4_plan()
        plan["continuity_groups"][0]["camera_setups"][0]["look_target_zone_id"] = "garden-far-right"
        self.assert_has_error(plan, "must match subject_zone_id")
        for field in ("visible_anchor_ids", "excluded_anchor_ids"):
            with self.subTest(field=field):
                plan = valid_v4_plan()
                plan["shots"][0]["camera_plan"].pop(field)
                self.assert_has_error(plan, field)

    def test_v4_rejects_visible_and_excluded_anchor_overlap(self):
        plan = valid_v4_plan()
        plan["shots"][0]["camera_plan"]["excluded_anchor_ids"] = ["large-tree"]
        self.assert_has_error(plan, "cannot be both visible and excluded")

    def test_v4_requires_every_fixed_anchor_to_be_classified_per_shot(self):
        plan = valid_v4_plan()
        plan["shots"][0]["camera_plan"]["excluded_anchor_ids"] = []
        self.assert_has_error(plan, "every fixed anchor must be classified")

    def test_v4_rejects_a_visible_fixed_anchor_behind_the_camera(self):
        plan = valid_v4_plan()
        plan["continuity_groups"][0]["anchors"][0]["zone_id"] = "garden-far-left"
        plan["shots"][0]["camera_plan"]["visible_anchor_ids"] = ["large-tree", "stone-path"]
        plan["shots"][0]["camera_plan"]["excluded_anchor_ids"] = []
        self.assert_has_error(plan, "lies behind the camera")

    def test_v4_rejects_an_empty_visible_anchor_while_its_occupant_remains_there(self):
        plan = valid_v4_plan()
        anchor = plan["continuity_groups"][0]["anchors"][1]
        anchor["occupancy_rule"] = "occupied_while_subject_in_scene"
        anchor["occupant_subject_id"] = "character-a"
        occupant = plan["shots"][0]["subject_presence"][0]
        occupant["exit_presence"] = "off_screen"
        occupant["exit_zone_id"] = None
        plan["shots"][0]["exiting_characters"] = ["character-a"]
        self.assert_has_error(plan, "cannot appear empty")

    def test_v4_requires_world_location_separate_from_frame_visibility(self):
        plan = valid_v4_plan()
        offscreen = plan["shots"][0]["subject_presence"][1]
        offscreen.pop("entry_world_zone_id")
        self.assert_has_error(plan, "entry_world_zone_id")

    def test_v4_requires_world_presence_separate_from_frame_visibility(self):
        plan = valid_v4_plan()
        offscreen = plan["shots"][0]["subject_presence"][1]
        offscreen.pop("entry_world_presence")
        self.assert_has_error(plan, "entry_world_presence")

    def test_v4_requires_explicit_group_color_contract(self):
        plan = valid_v4_plan()
        plan["continuity_groups"][0].pop("color_contract")
        self.assert_has_error(plan, "color_contract")

    def test_v4_rejects_fake_shot_reverse_shot_using_the_same_camera_setup(self):
        plan = valid_v4_reverse_shot_plan()
        plan["shots"][1]["camera_plan"]["camera_setup_id"] = "setup-character-a"
        self.assert_has_error(plan, "shot_reverse_shot requires different camera setups")

    def test_v4_accepts_complementary_shot_reverse_shot_on_one_axis(self):
        self.assertEqual(VALIDATOR.validate_plan(valid_v4_reverse_shot_plan()), [])

    def test_v4_rejects_on_screen_character_teleport_between_same_scene_shots(self):
        plan = valid_v4_reverse_shot_plan()
        plan["shots"][1]["subject_presence"][0]["entry_zone_id"] = "garden-left"
        self.assert_has_error(plan, "character-a teleports from zone 'garden-right' to 'garden-left'")

    def test_v4_requires_exactly_one_structured_primary_beat_per_performance_beat(self):
        plan = valid_v4_plan()
        del plan["shots"][0]["performance_beats"][0]["primary_beat"]
        self.assert_has_error(plan, "primary_beat must be one object")

        plan = valid_v4_plan()
        plan["shots"][0]["performance_beats"][0]["primary_beat"] = [
            {"subject_id": "character-a", "action": "first"},
            {"subject_id": "character-b", "action": "second"},
        ]
        self.assert_has_error(plan, "primary_beat must be one object")

    def test_v4_rejects_offscreen_subject_claimed_visible_in_a_beat(self):
        plan = valid_v4_plan()
        presence = plan["shots"][0]["subject_presence"][1]
        presence["entry_presence"] = "off_screen"
        presence["exit_presence"] = "off_screen"
        presence["exit_zone_id"] = None
        self.assert_has_error(plan, "off-screen for the whole shot cannot be visible")

    def test_v4_focus_primary_must_be_present_and_match_camera_setup(self):
        plan = valid_v4_plan()
        plan["shots"][0]["focus_contract"]["primary_subject_id"] = "character-b"
        self.assert_has_error(plan, "focus primary subject must match the camera setup")

    def test_v4_prop_contract_binds_selected_asset_state_and_physics(self):
        plan = valid_v4_plan()
        plan["shots"][0]["prop_contracts"][0]["support_mode"] = "grounded"
        self.assert_has_error(plan, "must match the selected prop asset physical contract")

    def test_v4_rejects_unknown_structured_zone_axis_and_camera_setup(self):
        plan = valid_v4_plan()
        plan["shots"][0]["subject_presence"][0]["entry_zone_id"] = "unknown-zone"
        plan["shots"][0]["camera_plan"]["axis_id"] = "unknown-axis"
        plan["shots"][0]["camera_plan"]["camera_setup_id"] = "unknown-setup"
        self.assert_has_error(plan, "unknown zone")
        self.assert_has_error(plan, "unknown axis")
        self.assert_has_error(plan, "unknown camera setup")

    def test_long_dialogue_without_visualization_is_advisory_not_schema_failure(self):
        plan = valid_plan()
        plan["shots"][0]["audio_speaker"] = "character-a"
        self.assertEqual(VALIDATOR.validate_plan(plan), [])
        advisories = VALIDATOR.creative_advisories(plan)
        self.assertTrue(any("较长角色台词" in item for item in advisories))
        self.assertTrue(any("不阻断" in item for item in advisories))

    def test_repeated_composition_requests_rationale_without_count_gate(self):
        plan = valid_two_shot_plan()
        first = plan["shots"][0]["opening_frame"]
        second = plan["shots"][1]["opening_frame"]
        for field in ("shot_size", "camera_angle", "subject_layout"):
            second[field] = first[field]
        advisories = VALIDATOR.creative_advisories(plan)
        self.assertTrue(any("相似本身不是错误" in item for item in advisories))

    def test_modality_cue_requests_fact_check_without_forcing_a_bubble(self):
        plan = valid_plan()
        plan["shots"][0]["story_text"] = "如果你把我放进水里，会发生什么？"
        self.assertEqual(VALIDATOR.validate_plan(plan), [])
        advisories = VALIDATOR.creative_advisories(plan)
        self.assertTrue(any("当前物理事实" in item for item in advisories))

    def test_accepts_explicit_non_text_dialogue_visual_bubble(self):
        plan = valid_plan()
        plan["shots"][0]["narrative_visualization"] = {
            "mode": "speech_visual_bubble",
            "narrative_layer": "proposed_action",
            "reality_anchor": "speaker remains dry on the bank",
            "content_to_visualize": "speaker imagines washing in the pond",
            "entry_cue": "one bubble tail points to the speaker",
            "exit_cue": "return to the dry speaker and listener",
            "ppt_readability_strategy": "soft boundary separates proposal from reality",
            "duplicate_identity_policy": "framed_representation_only",
        }
        self.assertEqual(VALIDATOR.validate_plan(plan), [])

    def test_dialogue_visual_bubble_rejects_unframed_identity_duplication(self):
        plan = valid_plan()
        plan["shots"][0]["narrative_visualization"] = {
            "mode": "speech_visual_bubble",
            "narrative_layer": "proposed_action",
            "reality_anchor": "speaker remains dry on the bank",
            "content_to_visualize": "speaker imagines washing in the pond",
            "entry_cue": "one bubble tail points to the speaker",
            "exit_cue": "return to the dry speaker and listener",
            "ppt_readability_strategy": "soft boundary separates proposal from reality",
            "duplicate_identity_policy": "forbid",
        }
        self.assert_has_error(plan, "requires framed_representation_only")

    def test_director_only_storyboard_requires_a_recorded_reason(self):
        plan = valid_plan()
        plan["shots"][0]["storyboard_reference_mode"] = "director_only"
        self.assert_has_error(plan, "storyboard_reference_reason")
        plan["shots"][0]["storyboard_reference_reason"] = "结果态构图与入口过程冲突"
        self.assertEqual(VALIDATOR.validate_plan(plan), [])

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

    def test_accepts_one_semantic_storyboard_as_final_reference(self):
        plan = valid_plan()
        plan["assets"].append(
            _asset(
                "storyboard-a",
                "storyboard",
                "semantic_storyboard",
                contains_characters=["character-a", "character-b"],
                appearance_summary="mid-action spatial guide; semantic only and not a first frame",
            )
        )
        plan["shots"][0]["reference_asset_ids"].append("storyboard-a")
        self.assertEqual(VALIDATOR.validate_plan(plan), [])

    def test_rejects_multiple_or_nonfinal_semantic_storyboards(self):
        plan = valid_plan()
        for suffix in ("a", "b"):
            plan["assets"].append(
                _asset(
                    f"storyboard-{suffix}",
                    "storyboard",
                    "semantic_storyboard",
                    contains_characters=["character-a"],
                    appearance_summary="semantic action guide",
                )
            )
        plan["shots"][0]["reference_asset_ids"].extend(["storyboard-a", "storyboard-b"])
        self.assert_has_error(plan, "at most one semantic storyboard")

        plan = valid_plan()
        plan["assets"].append(
            _asset(
                "storyboard-a",
                "storyboard",
                "semantic_storyboard",
                contains_characters=["character-a"],
                appearance_summary="semantic action guide",
            )
        )
        plan["shots"][0]["reference_asset_ids"].insert(0, "storyboard-a")
        self.assert_has_error(plan, "semantic storyboard must be the final reference")

    def test_rejects_storyboard_without_independent_character_asset(self):
        plan = valid_plan()
        plan["assets"].append(
            _asset(
                "storyboard-a",
                "storyboard",
                "semantic_storyboard",
                contains_characters=["character-c"],
                appearance_summary="semantic action guide",
            )
        )
        plan["shots"][0]["reference_asset_ids"].append("storyboard-a")
        self.assert_has_error(plan, "storyboard identities need independent character assets")

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

    def test_v3_requires_runtime_prop_physics_and_scale(self):
        plan = valid_plan()
        del plan["assets"][3]["physical_contract"]
        del plan["assets"][3]["scale_class"]
        self.assert_has_error(plan, "runtime prop assets require a physical contract")
        self.assert_has_error(plan, "scale_class")

    def test_v3_requires_one_reviewed_mother_for_multi_state_prop(self):
        plan = valid_plan()
        for field in (
            "state_family_id",
            "state_family_master_path",
            "state_family_master_sha256",
            "state_delta",
        ):
            plan["assets"][3].pop(field)
        self.assert_has_error(plan, "state_family_id")
        self.assert_has_error(plan, "state_family_master_sha256")

    def test_v3_rejects_independently_generated_prop_states(self):
        plan = valid_plan()
        plan["assets"][4]["state_family_master_sha256"] = "d" * 64
        self.assert_has_error(plan, "all states must bind the same mother hash")

    def test_v3_allows_state_specific_support_while_preserving_object_identity(self):
        plan = valid_plan()
        plan["assets"][4]["physical_contract"] = _physical_contract(
            support_mode="grounded",
            grip_or_contact="rests on the reviewed surface after separation",
        )
        self.assertEqual(VALIDATOR.validate_plan(plan), [])

    def test_v3_rejects_unassigned_timeline_gap(self):
        plan = valid_two_shot_plan()
        plan["shots"][1]["source_start"] = 10.5
        self.assert_has_error(plan, "unassigned timeline gap")

    def test_v3_requires_spatial_character_and_prop_continuity_bindings(self):
        plan = valid_two_shot_plan()
        bindings = plan["shots"][0]["cut_to_next"]["continuity_bindings"]
        plan["shots"][0]["cut_to_next"]["continuity_bindings"] = [
            binding for binding in bindings if binding["dimension"] == "axis"
        ]
        self.assert_has_error(plan, "missing required dimensions")

    def test_accepts_legacy_v2_plan_without_v3_physical_contract(self):
        plan = valid_plan()
        plan["schema_version"] = "story-r2v-plan-v2"
        for asset in plan["assets"]:
            asset.pop("physical_contract", None)
            asset.pop("state_family_id", None)
            asset.pop("state_family_master_path", None)
            asset.pop("state_family_master_sha256", None)
            asset.pop("state_delta", None)
        self.assertEqual(VALIDATOR.validate_plan(plan), [])

    def test_accepts_absent_prop_state_without_placeholder_image(self):
        plan = valid_plan()
        shot = plan["shots"][0]
        shot["reference_asset_ids"].remove("prop-before")
        shot["entry_state"]["prop-a"] = "absent"
        shot["exit_state"]["prop-a"] = "complete"
        shot["prop_state_transitions"] = [
            {
                "prop_id": "prop-a",
                "before_state": "absent",
                "visible_trigger_action": "the prop appears only after the wish completes",
                "after_state": "complete",
                "exit_evidence": "the complete prop is visible at the end",
                "next_shot_asset_id": "prop-before",
            }
        ]
        self.assertEqual(VALIDATOR.validate_plan(plan), [])

    def test_accepts_absent_exit_state_without_placeholder_image(self):
        plan = valid_plan()
        shot = plan["shots"][0]
        shot["exit_state"]["prop-a"] = "absent"
        shot["prop_state_transitions"][0]["after_state"] = "absent"
        shot["prop_state_transitions"][0]["next_shot_asset_id"] = None
        self.assertEqual(VALIDATOR.validate_plan(plan), [])

    def test_rejects_placeholder_image_for_absent_exit_state(self):
        plan = valid_plan()
        shot = plan["shots"][0]
        shot["prop_state_transitions"][0]["after_state"] = "absent"
        self.assert_has_error(plan, "absent exit state must use null")

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
