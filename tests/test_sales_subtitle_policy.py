from __future__ import annotations

import unittest
from pathlib import Path

from story_video_synthesizer.align import LineTiming
from story_video_synthesizer.pipeline import SynthesisConfig, _sales_subtitle_timings


def timing(index: int, line: str) -> LineTiming:
    return LineTiming(index, line, index - 1, index, 1.0, index - 1, index)


class SalesSubtitlePolicyTests(unittest.TestCase):
    def config(self, *, head: int = -1, tail: int = -1) -> SynthesisConfig:
        root = Path("/tmp")
        return SynthesisConfig(
            video_dir=root,
            script_path=root / "story.txt",
            narration_path=root / "voice.mp3",
            music_path=root / "music.mp3",
            output_dir=root,
            sales_skip_head_lines=head,
            sales_skip_tail_lines=tail,
        )

    def test_semantic_policy_starts_at_first_narrative_line_and_stops_before_moral(self) -> None:
        timings = [
            timing(1, "大家好，我是绵羊姐姐。"),
            timing(2, "今天给大家讲《大象和蚂蚁》。"),
            timing(3, "森林里住着一头大象。"),
            timing(4, "小蚂蚁勇敢地站了出来。"),
            timing(5, "小朋友们，这个故事告诉我们不要欺负弱小。"),
        ]
        result = _sales_subtitle_timings(timings, self.config())
        self.assertEqual([item.index for item in result], [3, 4])

    def test_explicit_line_counts_remain_supported(self) -> None:
        timings = [timing(1, "甲"), timing(2, "乙"), timing(3, "丙"), timing(4, "丁")]
        result = _sales_subtitle_timings(timings, self.config(head=1, tail=1))
        self.assertEqual([item.index for item in result], [2, 3])


if __name__ == "__main__":
    unittest.main()
