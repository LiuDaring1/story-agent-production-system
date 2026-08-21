from __future__ import annotations

import unittest

from story_codex_tasks import infer_image_style_key, resolve_image_style


class StoryCodexTaskStyleTests(unittest.TestCase):
    def test_generic_once_upon_a_time_animal_story_defaults_to_3d(self) -> None:
        key = infer_image_style_key(
            story_type="童话故事",
            story_title="动物童话",
            story="从前有一只小动物，它住在村边。",
        )
        self.assertEqual(key, "3d_cartoon")
        self.assertEqual(
            resolve_image_style(
                "自动",
                story_type="童话故事",
                story_title="动物童话",
                story="从前有一只小动物。",
            )["label"],
            "3D卡通",
        )

    def test_story_type_does_not_override_unspecified_default(self) -> None:
        self.assertEqual(
            infer_image_style_key(story_type="民间故事", story_title="通用标题", story=""),
            "3d_cartoon",
        )

    def test_story_keywords_do_not_override_unspecified_default(self) -> None:
        self.assertEqual(
            infer_image_style_key(story_type="童话故事", story_title="通用标题", story="天帝派仙人下凡。"),
            "3d_cartoon",
        )

    def test_explicit_non_default_style_is_preserved(self) -> None:
        self.assertEqual(
            resolve_image_style(
                "中国风2D绘本",
                story_type="童话故事",
                story_title="通用标题",
                story="普通儿童故事。",
            )["key"],
            "chinese_2d_storybook",
        )


if __name__ == "__main__":
    unittest.main()
