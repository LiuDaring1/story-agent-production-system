from __future__ import annotations

import unittest

from PIL import Image, ImageDraw

from presenter_layout import (
    PRESENTER_LAYOUT_POLICY,
    body_core_visibility_fraction,
    compile_fixed_anchor,
    severe_body_overflow_windows,
    source_native_fixed_anchor_issues,
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

    def test_local_repair_gate_blocks_scale_crop_y_and_anchor_drift(self) -> None:
        self.assertEqual(
            source_native_fixed_anchor_issues(
                source_width=1920,
                source_height=1080,
                rendered_height=918,
                person_x=800,
                person_y=20,
                person_crop=(10, 0, 1800, 1080),
                policy=PRESENTER_LAYOUT_POLICY,
                expected_x=849,
            ),
            [
                "presenter_source_native_person_crop_forbidden",
                "presenter_source_native_scale_forbidden",
                "presenter_source_native_y_shift_forbidden",
                "presenter_fixed_anchor_x_mismatch",
            ],
        )

    def test_local_repair_gate_accepts_source_native_x_only_layout(self) -> None:
        self.assertEqual(
            source_native_fixed_anchor_issues(
                source_width=1920,
                source_height=1080,
                rendered_height=1080,
                person_x=849,
                person_y=0,
                person_crop=None,
                policy=PRESENTER_LAYOUT_POLICY,
                expected_x=849,
            ),
            [],
        )

    def test_opening_anchor_stays_fixed_and_allows_natural_gesture_overflow(self) -> None:
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
        self.assertEqual(
            plan["gesture_overlap_policy"],
            "allow_source_frame_overflow",
        )

    def test_body_core_visibility_ignores_sparse_hand_outlier(self) -> None:
        alpha = Image.new("L", (200, 100), 0)
        draw = ImageDraw.Draw(alpha)
        draw.rectangle((70, 10, 130, 99), fill=255)  # head and torso mass
        draw.rectangle((20, 40, 70, 50), fill=255)   # sparse extended hand/arm
        visible = body_core_visibility_fraction(alpha, anchor_x=80, canvas_width=200)
        self.assertIsNotNone(visible)
        self.assertGreater(float(visible), 0.95)

    def test_persistent_one_third_body_loss_becomes_padded_b_window(self) -> None:
        samples = [
            (0.0, 1.0),
            (0.2, 0.68),
            (0.4, 0.62),
            (0.6, 0.55),
            (0.8, 0.92),
        ]
        self.assertEqual(
            severe_body_overflow_windows(samples, duration=1.0),
            [(0.0, 1.0)],
        )

    def test_single_bad_sample_does_not_trigger_scene_cut(self) -> None:
        samples = [(0.0, 1.0), (0.2, 0.4), (0.4, 1.0)]
        self.assertEqual(severe_body_overflow_windows(samples, duration=1.0), [])

    def test_borderline_arm_or_hand_band_does_not_trigger_scene_cut(self) -> None:
        samples = [
            (0.0, 1.0),
            (0.2, 0.68),
            (0.4, 0.64),
            (0.6, 0.66),
            (0.8, 0.69),
            (1.0, 1.0),
        ]
        self.assertEqual(severe_body_overflow_windows(samples, duration=1.2), [])

    def test_unmistakable_core_excursion_opens_full_warning_window(self) -> None:
        samples = [
            (0.0, 1.0),
            (0.2, 0.68),
            (0.4, 0.62),
            (0.6, 0.49),
            (0.8, 0.66),
            (1.0, 1.0),
        ]
        self.assertEqual(
            severe_body_overflow_windows(samples, duration=1.2),
            [(0.0, 1.2)],
        )


if __name__ == "__main__":
    unittest.main()
