from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from story_workflow import build_product_text_sources_from_story_source

try:
    from product_package import reject_full_subtitle_background
except ModuleNotFoundError:
    # The media-package test suite has an optional python-docx dependency in
    # the lightweight runtime used by alignment-only CI.
    reject_full_subtitle_background = None  # type: ignore[assignment]


def write_srt(path: Path, texts: list[str]) -> None:
    blocks = []
    for index, text in enumerate(texts, start=1):
        start = index - 1
        end = index
        blocks.append(
            f"{index}\n00:00:{start:02d},000 --> 00:00:{end:02d},000\n{text}"
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


class ProductTextSourceAlignmentTests(unittest.TestCase):
    def run_alignment(self, story_lines: list[str], subtitle_lines: list[str]):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            story_path = root / "story.txt"
            subtitles_path = root / "story_subtitles.srt"
            output_dir = root / "work"
            story_path.write_text("\n".join(story_lines) + "\n", encoding="utf-8")
            write_srt(subtitles_path, subtitle_lines)
            script_path, timings_path = build_product_text_sources_from_story_source(
                story_text=story_path,
                subtitles_srt=subtitles_path,
                output_dir=output_dir,
            )
            return (
                script_path.read_text(encoding="utf-8"),
                json.loads(timings_path.read_text(encoding="utf-8")),
            )

    def test_skips_semantic_title_and_host_intro_prefix(self) -> None:
        story_lines = [
            "一天，小壁虎爬呀爬，爬到小河边。",
            "小壁虎看见了妈妈。",
        ]
        script, timings = self.run_alignment(
            story_lines,
            [
                "小壁虎找妈妈",
                "大家好，我是小鱼姐姐。",
                *story_lines,
            ],
        )

        self.assertEqual(script, "\n".join(story_lines) + "\n")
        self.assertEqual([item["source_cue_start"] for item in timings], [3, 4])
        self.assertEqual([item["source_cue_end"] for item in timings], [3, 4])

    def test_skips_semantic_story_announcement_prefix(self) -> None:
        story_lines = ["一天，小壁虎爬呀爬，爬到小河边。"]
        script, timings = self.run_alignment(
            story_lines,
            ["小壁虎找妈妈", "今天要给大家讲小壁虎找妈妈。", story_lines[0]],
        )

        self.assertEqual(script, story_lines[0] + "\n")
        self.assertEqual(timings[0]["source_cue_start"], 3)
        self.assertEqual(timings[0]["source_cue_end"], 3)

    def test_missing_story_body_still_fails_after_prefix(self) -> None:
        story_lines = [
            "一天，小壁虎爬呀爬，爬到小河边。",
            "小壁虎看见了妈妈。",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            story_path = root / "story.txt"
            subtitles_path = root / "story_subtitles.srt"
            story_path.write_text("\n".join(story_lines) + "\n", encoding="utf-8")
            write_srt(
                subtitles_path,
                ["小壁虎找妈妈", "大家好，我是小鱼姐姐。", story_lines[0]],
            )

            with self.assertRaisesRegex(ValueError, r"故事原文第 2 行无法与 story_subtitles\.srt 顺序对齐"):
                build_product_text_sources_from_story_source(
                    story_text=story_path,
                    subtitles_srt=subtitles_path,
                    output_dir=root / "work",
                )

    def test_direct_alignment_without_prefix_remains_supported(self) -> None:
        story_lines = [
            "森林里住着一只小狐狸。",
            "小狐狸每天都练习唱歌。",
        ]
        script, timings = self.run_alignment(story_lines, story_lines)

        self.assertEqual(script, "\n".join(story_lines) + "\n")
        self.assertEqual([item["source_cue_start"] for item in timings], [1, 2])
        self.assertEqual([item["source_cue_end"] for item in timings], [1, 2])

    def test_missing_middle_story_line_is_not_skipped(self) -> None:
        story_lines = [
            "一天，小壁虎爬呀爬，爬到小河边。",
            "小壁虎看见了妈妈。",
            "小壁虎开心地笑了。",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            story_path = root / "story.txt"
            subtitles_path = root / "story_subtitles.srt"
            story_path.write_text("\n".join(story_lines) + "\n", encoding="utf-8")
            write_srt(subtitles_path, ["小壁虎找妈妈", "大家好，我是小鱼姐姐。", story_lines[0], story_lines[2]])

            with self.assertRaisesRegex(ValueError, r"故事原文第 2 行无法与 story_subtitles\.srt 顺序对齐"):
                build_product_text_sources_from_story_source(
                    story_text=story_path,
                    subtitles_srt=subtitles_path,
                    output_dir=root / "work",
                )


@unittest.skipIf(
    reject_full_subtitle_background is None,
    "product_package optional document dependencies are unavailable",
)
class ProductBackgroundSubtitleValidationTests(unittest.TestCase):
    def write_background_srt(self, root: Path, lines: list[str]) -> Path:
        srt = root / "story_sales_subtitles.srt"
        write_srt(srt, lines)
        return srt

    def test_legal_body_and_moral_small_friends_lines_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "story_sales_subs_bgm.mp4"
            video.touch()
            self.write_background_srt(
                root,
                [
                    "森林里住着一只小兔子。",
                    "小朋友们，你们觉得它会怎么做？",
                    "小朋友们，这个故事告诉我们要勇敢。",
                ],
            )

            reject_full_subtitle_background(video)

    def test_host_intro_still_fails_semantic_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "story_sales_subs_bgm.mp4"
            video.touch()
            self.write_background_srt(root, ["大家好，我是小鱼姐姐。", "森林里住着一只小兔子。"])

            with self.assertRaisesRegex(ValueError, "host_intro"):
                reject_full_subtitle_background(video)

    def test_legacy_full_subtitle_filename_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "story_subs_bgm.mp4"
            video.touch()

            with self.assertRaisesRegex(ValueError, "完整字幕版"):
                reject_full_subtitle_background(video)


if __name__ == "__main__":
    unittest.main()
