from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from story_workflow import build_abc_scene_windows


class StorySceneWindowTests(unittest.TestCase):
    def test_starts_with_c_uses_b_and_finishes_with_a(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            srt = Path(directory) / "story.srt"
            blocks = []
            for index, start in enumerate(range(0, 72, 6), start=1):
                end = start + 5
                blocks.append(f"{index}\n00:00:{start:02d},000 --> 00:00:{end:02d},000\n第{index}句")
            srt.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
            b_windows, c_windows = build_abc_scene_windows(80.0, srt)
            self.assertTrue(c_windows.startswith("0.000-"))
            self.assertTrue(b_windows)
            self.assertNotIn("-80.000", b_windows)
            self.assertNotIn("-80.000", c_windows)

    def test_short_video_is_all_a_to_guarantee_a_ending(self) -> None:
        b_windows, c_windows = build_abc_scene_windows(12.0, None)
        self.assertEqual(b_windows, "")
        self.assertEqual(c_windows, "")


if __name__ == "__main__":
    unittest.main()
