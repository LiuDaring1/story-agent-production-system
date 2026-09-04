from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from story_customer_media import (
    file_sha256,
    music_only_fit,
    narration_music_fit,
    subtitle_geometry_from_frames,
    validate_customer_media_receipt,
)


class CustomerMediaPolicyTests(unittest.TestCase):
    def test_release_mix_requires_both_narration_and_music(self) -> None:
        rng = np.random.default_rng(23)
        narration = rng.normal(0.0, 0.15, 16_000)
        music = rng.normal(0.0, 0.08, 16_000)
        mixed = 0.9 * narration + 0.7 * music + rng.normal(0.0, 0.0001, 16_000)

        self.assertTrue(narration_music_fit(mixed, narration, music)["passed"])
        narration_only = narration + rng.normal(0.0, 0.0001, 16_000)
        self.assertFalse(narration_music_fit(narration_only, narration, music)["passed"])

    def test_music_only_signal_passes_but_narration_mix_fails(self) -> None:
        rng = np.random.default_rng(20260902)
        music = rng.normal(0.0, 0.08, 80_000)
        music_only = 0.22 * music + rng.normal(0.0, 0.0001, music.size)
        narration_mix = music_only + rng.normal(0.0, 0.12, music.size)

        self.assertTrue(music_only_fit(music_only, music)["passed"])
        self.assertFalse(music_only_fit(narration_mix, music)["passed"])

    def test_subtitle_geometry_requires_bottom_center_of_full_canvas(self) -> None:
        without = np.zeros((1080, 1920, 3), dtype=np.uint8)
        correct = without.copy()
        correct[930:970, 700:1220] = 255
        wrong = without.copy()
        wrong[610:650, 380:900] = 255

        correct_result = subtitle_geometry_from_frames(correct, without)
        wrong_result = subtitle_geometry_from_frames(wrong, without)

        self.assertTrue(correct_result["passed"])
        self.assertFalse(wrong_result["passed"])
        self.assertLess(correct_result["horizontal_center_error_ratio"], 0.01)
        self.assertLess(wrong_result["vertical_center_ratio"], 0.72)

    def test_subtitle_geometry_ignores_sparse_codec_difference_rows(self) -> None:
        without = np.zeros((1080, 1920, 3), dtype=np.uint8)
        with_subtitles = without.copy()
        with_subtitles[930:970, 700:1220] = 255
        # Paired encodes can leave a few bright, dense-enough difference rows
        # far from the subtitle. They must not stretch the subtitle bbox.
        with_subtitles[460:461, 100:110] = 255
        with_subtitles[525:528, 1300:1314] = 255

        result = subtitle_geometry_from_frames(with_subtitles, without)

        self.assertTrue(result["passed"])
        self.assertEqual(result["bbox"], [700, 930, 1220, 970])

    def test_receipt_rejects_self_reported_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "customer_media_receipt.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "story-customer-media-receipt/v1",
                        "passed": False,
                        "critical_errors": ["audio_not_music_only"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "未通过"):
                validate_customer_media_receipt(path)


if __name__ == "__main__":
    unittest.main()
