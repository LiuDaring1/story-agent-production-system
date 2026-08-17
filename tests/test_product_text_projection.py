from __future__ import annotations

import unittest

from product_text_projection import (
    PUBLIC_TEXT_TRANSFORM_VERSION,
    clean_public_story_text,
    compile_public_story_lines,
)


class ProductTextProjectionTests(unittest.TestCase):
    def test_host_intro_is_cleared_without_shifting_source_indices(self) -> None:
        raw = [
            "大家好，我是故事老师。",
            "第一句正文。",
            "第二句正文。",
        ]

        projected = compile_public_story_lines(raw)

        self.assertEqual(PUBLIC_TEXT_TRANSFORM_VERSION, "story-public-text/v2")
        self.assertEqual(projected, ["", raw[1], raw[2]])
        self.assertEqual(len(projected), len(raw))
        self.assertEqual(projected[1], raw[1])

    def test_story_body_dialogue_with_presenter_like_words_is_verbatim(self) -> None:
        body = [
            "小兔说：“我是森林里的兔子姐姐！”",
            "老师说：“我是你们的新老师。”",
            "小熊说：“我叫松林哥哥。”",
        ]

        self.assertEqual(compile_public_story_lines(body), body)

    def test_moral_with_first_person_identity_words_is_verbatim(self) -> None:
        raw = [
            "小鸟飞过了山谷。",
            "这个故事告诉我们：我是老师，也要先认真倾听。",
        ]

        self.assertEqual(compile_public_story_lines(raw), raw)

    def test_inline_host_intro_keeps_following_announcement(self) -> None:
        text = "大家好，我是故事老师。今天要给大家讲故事。"

        self.assertEqual(clean_public_story_text(text), "今天要给大家讲故事。")


if __name__ == "__main__":
    unittest.main()
