from __future__ import annotations

import unittest

from story_video_synthesizer.align import LineTiming
from story_video_synthesizer.subtitles import build_subtitle_cues, split_subtitle_text


class SubtitleSegmentationTests(unittest.TestCase):
    def test_user_supplied_txt_line_is_not_rewritten_or_resplit(self) -> None:
        timing = LineTiming(1, "妈妈，这是太阳吗？", 0.0, 2.0, 2.0, 0.0, 2.0)
        cues = build_subtitle_cues([timing], preserve_input_lines=True)
        self.assertEqual([cue.text for cue in cues], ["妈妈，这是太阳吗？"])

    def test_preschool_cues_are_balanced_short_and_punctuation_free(self) -> None:
        source = "小兔子顺着妈妈手指的方向抬起头，大声喊：妈妈，妈妈，我找到了太阳！红红的、圆圆的、亮亮的，照在身上暖洋洋的。"
        cues = split_subtitle_text(source, max_chars=12)

        self.assertTrue(cues)
        self.assertTrue(all(1 <= len(cue) <= 12 for cue in cues))
        self.assertEqual("".join(cues), "".join(char for char in source if char not in "，：！、。"))
        self.assertFalse(any(len(cue) <= 2 for cue in cues))

    def test_long_clause_avoids_tiny_tail(self) -> None:
        cues = split_subtitle_text("一二三四五六七八九十甲乙丙", max_chars=6)
        self.assertEqual([5, 4, 4], [len(cue) for cue in cues])

    def test_numeric_grouping_comma_does_not_create_bad_fragment(self) -> None:
        cues = split_subtitle_text("18,000年前，天空很蓝", max_chars=12)
        self.assertEqual(["18000年前天空很蓝"], cues)


if __name__ == "__main__":
    unittest.main()
