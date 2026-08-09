from __future__ import annotations

import unittest

from story_semantics import (
    SemanticKind,
    classify_story,
    lines_for_output,
    select_lines,
)


class StorySemanticsTests(unittest.TestCase):
    def test_split_title_and_separate_host_opening(self) -> None:
        source = [
            "小兔子找太阳",
            "大家好",
            "我是绵羊姐姐",
            "今天要给大家讲的故事是小兔子找太阳",
            "有一只可爱的小兔子。",
            "小朋友们，这个故事告诉我们要认真观察。",
            "今天的故事就到这里，再见。",
        ]

        semantics = classify_story(source)

        self.assertEqual(
            [(segment.kind, segment.start_line, segment.end_line) for segment in semantics.segments],
            [
                (SemanticKind.TITLE, 1, 1),
                (SemanticKind.HOST_INTRO, 2, 3),
                (SemanticKind.STORY_ANNOUNCEMENT, 4, 4),
                (SemanticKind.STORY_BODY, 5, 5),
                (SemanticKind.MORAL, 6, 6),
                (SemanticKind.OUTRO, 7, 7),
            ],
        )

        self.assertEqual([line.line_number for line in lines_for_output(semantics, "sales_subtitles")], [5])
        self.assertEqual(
            [line.line_number for line in lines_for_output(semantics, "ppt")],
            [5, 6],
        )
        self.assertEqual(
            [line.line_number for line in lines_for_output(semantics, "demo")],
            list(range(1, 8)),
        )

    def test_body_dialogue_mama_says_is_not_host_intro_or_outro(self) -> None:
        source = [
            "大家好，我是绵羊姐姐。",
            "今天要讲《小熊过河》。",
            "小熊走到河边，妈妈说你要小心。",
            "小熊点点头，继续向前走。",
        ]

        semantics = classify_story(source)

        self.assertEqual(semantics.kind_at(3), SemanticKind.STORY_BODY)
        self.assertEqual(semantics.kind_at(4), SemanticKind.STORY_BODY)
        self.assertEqual([line.text for line in select_lines(semantics, "sales_subtitles")], source[2:])

    def test_greeting_or_announcement_without_title_is_not_promoted_to_title(self) -> None:
        source = ["大家好", "我是绵羊姐姐", "今天要讲《小熊过河》", "小熊走到河边。"]

        semantics = classify_story(source)

        self.assertIsNone(semantics.segment(SemanticKind.TITLE))
        self.assertEqual(semantics.kind_at(1), SemanticKind.HOST_INTRO)
        self.assertEqual(semantics.kind_at(2), SemanticKind.HOST_INTRO)
        self.assertEqual(semantics.kind_at(3), SemanticKind.STORY_ANNOUNCEMENT)
        self.assertEqual(semantics.kind_at(4), SemanticKind.STORY_BODY)

    def test_original_text_and_line_order_are_unchanged(self) -> None:
        source = [
            "  《有趣的故事》  ",
            "大家好！",
            "我是绵羊姐姐。",
            "今天要讲这个故事。",
            "妈妈说：‘慢一点。’",
        ]

        semantics = classify_story(source)

        self.assertEqual(semantics.original_lines, tuple(source))
        self.assertEqual([line.text for line in semantics.lines], source)
        self.assertEqual([line.line_number for line in semantics.lines], list(range(1, len(source) + 1)))
        self.assertEqual([line.text for line in lines_for_output(semantics, "sales_subtitles")], [source[-1]])

    def test_moral_and_outro_are_not_overeager(self) -> None:
        source = [
            "狐狸来到小河边。",
            "狐狸说：这个故事告诉我们吗？其实他只是在问问题。",
            "小兔子回答说不用担心。",
        ]

        semantics = classify_story(source)

        self.assertTrue(all(semantics.kind_at(i) == SemanticKind.STORY_BODY for i in range(1, 4)))


if __name__ == "__main__":
    unittest.main()
