from __future__ import annotations

import unittest

from presenter_layout import (
    PRESENTER_LAYOUT_POLICY,
    compile_fixed_anchor,
    source_native_layout_issues,
)


class PresenterLayoutTests(unittest.TestCase):
    def test_source_native_policy_blocks_crop_and_scale(self) -> None:
        self.assertEqual(
            source_native_layout_issues(
                source_width=1920,
                source_height=1080,
                rendered_height=900,
                person_crop=(100, 0, 1200, 1080),
                policy=PRESENTER_LAYOUT_POLICY,
            ),
            [
                "presenter_source_native_person_crop_forbidden",
                "presenter_source_native_scale_forbidden",
            ],
        )

    def test_detection_bbox_is_not_an_input_to_source_native_gate(self) -> None:
        self.assertEqual(
            source_native_layout_issues(
                source_width=1920,
                source_height=1080,
                rendered_height=1080,
                person_crop=None,
                policy=PRESENTER_LAYOUT_POLICY,
            ),
            [],
        )

    def test_opening_anchor_stays_fixed_even_when_later_gesture_overflows(self) -> None:
        samples = [
            (0.0, 740, 1142),
            (1.0, 700, 1200),
            (2.0, 300, 1750),
            (3.0, 700, 1200),
            (4.0, 740, 1142),
        ]
        plan = compile_fixed_anchor(
            samples,
            active_windows=[(0.0, 4.0)],
            initial_subject_bbox=[740, 64, 402, 1016],
            right_blank_rect=[1200, 0, 720, 1080],
            canvas_width=1920,
            edge_margin=12,
            anticipation_seconds=0.5,
        )
        self.assertEqual(plan["anchor_x"], 619)
        self.assertEqual(plan["minimum_x"], 619)
        self.assertEqual(plan["maximum_x"], 619)
        self.assertFalse(plan["dynamic_repositioning"])
        self.assertEqual(plan["gesture_overlap_policy"], "allow_source_frame_overflow")


if __name__ == "__main__":
    unittest.main()
