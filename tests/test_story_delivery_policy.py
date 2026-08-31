from __future__ import annotations

import unittest
from PIL import Image
from story_video_synthesizer.pipeline import _subtitle_palette_has_magenta_or_purple

from story_delivery_policy import (
    SUBTITLE_SCOPE_BODY,
    SUBTITLE_SCOPE_FULL,
    SUBTITLE_SCOPE_NONE,
    high_quality_background_blur,
    normalize_duration_label,
    release_semantic_subtitle_artifact,
    subtitle_scope,
)
from story_project import DEFAULT_CONFIG


class StoryDeliveryPolicyTests(unittest.TestCase):
    def test_subtitle_matrix_is_explicit_for_every_customer_and_release_artifact(self) -> None:
        self.assertEqual(subtitle_scope("release_main"), SUBTITLE_SCOPE_FULL)
        self.assertEqual(subtitle_scope("release_library"), SUBTITLE_SCOPE_FULL)
        self.assertEqual(subtitle_scope("product_demo"), SUBTITLE_SCOPE_FULL)
        self.assertEqual(subtitle_scope("product_background_with_subtitles"), SUBTITLE_SCOPE_BODY)
        self.assertEqual(subtitle_scope("product_background_without_subtitles"), SUBTITLE_SCOPE_NONE)
        self.assertEqual(subtitle_scope("ppt_with_subtitles"), SUBTITLE_SCOPE_BODY)
        self.assertEqual(subtitle_scope("ppt_without_subtitles"), SUBTITLE_SCOPE_NONE)

    def test_both_public_release_variants_use_full_spoken_semantics(self) -> None:
        self.assertEqual(release_semantic_subtitle_artifact("main"), "demo_subtitles")
        self.assertEqual(release_semantic_subtitle_artifact("library"), "demo_subtitles")

    def test_duration_label_never_contains_approximation_word(self) -> None:
        self.assertEqual(normalize_duration_label("约 3分钟"), "3分钟")
        self.assertEqual(normalize_duration_label("3分10秒"), "3分10秒")
        with self.assertRaisesRegex(ValueError, "不得使用"):
            normalize_duration_label("3分钟约版")

    def test_background_blur_uses_gaussian_not_box_blur(self) -> None:
        graph = high_quality_background_blur("src", 14, "out")
        self.assertIn("gblur=sigma=14:steps=4", graph)
        self.assertNotIn("boxblur", graph)

    def test_subtitle_palette_rejects_purple_key_fringe_but_accepts_white(self) -> None:
        purple = Image.new("RGBA", (1, 1), (180, 40, 180, 255))
        white = Image.new("RGBA", (1, 1), (255, 255, 255, 255))
        self.assertTrue(_subtitle_palette_has_magenta_or_purple(purple))
        self.assertFalse(_subtitle_palette_has_magenta_or_purple(white))

    def test_new_project_default_and_preference_both_require_rvm(self) -> None:
        release = DEFAULT_CONFIG["release_defaults"]
        self.assertEqual(release["keyer"], "rvm")
        self.assertEqual(release["preferred_keyer"], "rvm")


if __name__ == "__main__":
    unittest.main()
