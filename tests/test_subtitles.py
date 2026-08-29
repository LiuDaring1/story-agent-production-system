from __future__ import annotations

import unittest

from story_video_synthesizer.subtitles import _split_line


class SubtitleSplitTests(unittest.TestCase):
    def test_numeric_grouping_comma_does_not_split_cue(self) -> None:
        self.assertEqual(
            _split_line("在混沌之中睡了18,000年", max_chars=18),
            ["在混沌之中睡了18000年"],
        )
        self.assertEqual(
            _split_line("这样过了18,000年", max_chars=18),
            ["这样过了18000年"],
        )

    def test_sentence_comma_still_splits_cue(self) -> None:
        self.assertEqual(
            _split_line("盘古醒来,他睁开眼睛", max_chars=18),
            ["盘古醒来", "他睁开眼睛"],
        )


if __name__ == "__main__":
    unittest.main()
