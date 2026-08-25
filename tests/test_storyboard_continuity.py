from __future__ import annotations

import unittest

from storyboard_continuity import storyboard_continuity_issues


def shot(scene: int, *, state: str, location: str = "street") -> dict:
    return {
        "scene": scene,
        "continuity_group": "same_event",
        "visible_characters": ["child", "dog"],
        "current_story_state": {"prop": state},
        "location_state": {
            "location_id": location,
            "time_of_day": "day",
            "change_from_previous": False,
            "change_cue": "",
        },
        "character_knowledge": {
            "child": {"aware_of": ["destination"], "unaware_of": ["dog stealing"], "gaze_target": "destination"},
            "dog": {"aware_of": ["dog stealing"], "unaware_of": [], "gaze_target": "prop"},
        },
        "required_visible_actions": [],
        "state_transition_evidence": {},
    }


MACHINES = [{
    "machine_id": "prop",
    "transitions": [{
        "from": "intact", "to": "one_removed", "trigger": "remove one part",
        "must_show_action": True, "action_subject": "child", "action_object": "part",
    }],
}]


class StoryboardContinuityTests(unittest.TestCase):
    def test_uninterrupted_event_cannot_silently_change_place_or_time(self) -> None:
        rows = [shot(1, state="intact"), shot(2, state="intact", location="room")]
        self.assertIn("scene_2:silent_location_or_time_jump", storyboard_continuity_issues(rows, MACHINES))

    def test_character_awareness_conflict_is_rejected(self) -> None:
        row = shot(1, state="intact")
        row["character_knowledge"]["child"]["aware_of"].append("dog stealing")
        self.assertIn("scene_1:character_knowledge_conflict:child", storyboard_continuity_issues([row], MACHINES))

    def test_irreversible_transition_requires_visible_cause_action(self) -> None:
        rows = [shot(1, state="intact"), shot(2, state="one_removed")]
        issues = storyboard_continuity_issues(rows, MACHINES)
        self.assertIn("scene_2:state_transition_evidence_missing:prop", issues)

        rows[1]["state_transition_evidence"] = {
            "prop": {"from": "intact", "to": "one_removed", "visibility": "in_frame", "evidence": "hand removes part"}
        }
        rows[1]["required_visible_actions"] = [{
            "machine_id": "prop", "action": "remove one part", "subject": "child",
            "object": "part", "visibility": "in_frame",
        }]
        self.assertEqual(storyboard_continuity_issues(rows, MACHINES), [])


if __name__ == "__main__":
    unittest.main()
