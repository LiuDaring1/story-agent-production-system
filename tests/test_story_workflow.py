from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import story_workflow
from story_workflow import (
    build_product_text_sources_from_story_source,
    confirmed_subtitle_from_story_run,
    confirmed_text_from_story_run,
    ensure_confirmed_spoken_timeline_srt,
    sealed_storyboard_images_dir,
    validate_static_ppt_full_timeline,
)

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
    def test_confirmed_audio_anchors_generate_full_spoken_srt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "99_项目状态"
            assembly = root / "03_背景成片"
            timing_dir = status / "preflight_alignment"
            timing_dir.mkdir(parents=True)
            assembly.mkdir(parents=True)
            (timing_dir / "confirmed_line_timings.json").write_text(
                json.dumps(
                    [
                        {"line": "大家好", "source_start": 0, "source_end": 1},
                        {"line": "正文", "source_start": 1, "source_end": 2},
                        {"line": "道理", "source_start": 2, "source_end": 3},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = ensure_confirmed_spoken_timeline_srt(status, assembly)

            self.assertIsNotNone(result)
            content = result.read_text(encoding="utf-8")
            self.assertIn("大家好", content)
            self.assertIn("正文", content)
            self.assertIn("道理", content)

    def test_static_ppt_timeline_preserves_opening_and_moral_audio_slots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            srt = root / "body.srt"
            srt.write_text(
                "1\n00:00:09,000 --> 00:02:59,000\n正文\n",
                encoding="utf-8",
            )
            plan = root / "plan.json"
            valid = {
                "slides": [
                    {"shot_id": "TITLE", "duration_seconds": 9.0},
                    {"shot_id": "S01", "duration_seconds": 170.0},
                    {"shot_id": "MORAL", "duration_seconds": 21.0},
                ]
            }
            plan.write_text(json.dumps(valid), encoding="utf-8")
            validate_static_ppt_full_timeline(plan, srt, 200.0)
            valid["slides"][0]["duration_seconds"] = 0.0
            valid["slides"][1]["duration_seconds"] = 179.0
            plan.write_text(json.dumps(valid), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "TITLE 时长"):
                validate_static_ppt_full_timeline(plan, srt, 200.0)

    def test_sealed_storyboard_directory_requires_every_image_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "99_项目状态"
            storyboards = status / "storyboards"
            images = storyboards / "requests"
            images.mkdir(parents=True)
            image = images / "S01.png"
            image.write_bytes(b"image")
            import hashlib

            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            (storyboards / "storyboard_manifest_sealed.json").write_text(
                json.dumps({
                    "status": "sealed",
                    "entries": [{"image_path": str(image), "image_sha256": digest}],
                }),
                encoding="utf-8",
            )
            self.assertEqual(sealed_storyboard_images_dir(status), images.resolve())
            image.write_bytes(b"tampered")
            self.assertIsNone(sealed_storyboard_images_dir(status))

    def test_native_ledger_confirmed_text_is_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "99_项目状态"
            status.mkdir()
            confirmed = root / "字幕：故事.txt"
            confirmed.write_text("标题\n正文\n", encoding="utf-8")
            import hashlib

            digest = hashlib.sha256(confirmed.read_bytes()).hexdigest()
            (status / "story_run.json").write_text(
                json.dumps({"inputs": {"confirmed_text": {"path": str(confirmed), "sha256": digest}}}),
                encoding="utf-8",
            )
            self.assertEqual(confirmed_text_from_story_run(status), confirmed)
            confirmed.write_text("被篡改", encoding="utf-8")
            self.assertIsNone(confirmed_text_from_story_run(status))

    def test_native_ledger_subtitle_txt_is_exact_and_fails_on_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "99_项目状态"
            status.mkdir()
            subtitle = root / "确认字幕.txt"
            subtitle.write_text("妈妈，这是太阳吗？\n不是，这是气球。\n", encoding="utf-8")
            import hashlib

            digest = hashlib.sha256(subtitle.read_bytes()).hexdigest()
            (status / "story_run.json").write_text(
                json.dumps({"inputs": {"subtitle_txt": {"path": str(subtitle), "sha256": digest}}}),
                encoding="utf-8",
            )
            self.assertEqual(confirmed_subtitle_from_story_run(status), subtitle)
            subtitle.write_text("被改过的字幕\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "哈希漂移"):
                confirmed_subtitle_from_story_run(status)

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

    def test_confirmed_text_title_can_be_absent_from_body_subtitles(self) -> None:
        body = ["很久很久以前", "天地还没有分开"]
        script, timings = self.run_alignment(
            ["盘古开天辟地", *body],
            body,
        )
        self.assertEqual(script, "\n".join(body) + "\n")
        self.assertEqual([item["line"] for item in timings], body)

    def test_presenter_intro_absent_from_sales_subtitles_is_trimmed_before_alignment(self) -> None:
        body = ["一只狼饿了好几天", "这时走来一只小鸭子"]
        script, timings = self.run_alignment(
            ["大家好我是绵羊姐姐", *body],
            body,
        )
        self.assertEqual(script, "\n".join(body) + "\n")
        self.assertEqual([item["line"] for item in timings], body)

    def test_independent_moral_card_suffix_is_trimmed_after_exact_body_subtitles(self) -> None:
        body = ["狼气疯了", "没了声"]
        script, timings = self.run_alignment(
            [
                *body,
                "小朋友们",
                "这个故事告诉我们",
                "遇到危险不要慌",
            ],
            body,
        )
        self.assertEqual(script, "\n".join(body) + "\n")
        self.assertEqual([item["line"] for item in timings], body)

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


class ProductPackageWorkflowForwardingTests(unittest.TestCase):
    def run_alignment(self, story_lines: list[str], subtitle_lines: list[str]):
        return ProductTextSourceAlignmentTests.run_alignment(self, story_lines, subtitle_lines)

    def test_forwards_complete_sealed_static_ppt_contract(self) -> None:
        argv = [
            "story_workflow.py",
            "product-package",
            "--story-name", "通用故事",
            "--story-text", "story.txt",
            "--script-lines", "lines.txt",
            "--narration", "narration.wav",
            "--music", "music.mp3",
            "--images-dir", "images",
            "--bg-video-with-sub", "with.mp4",
            "--bg-video-no-sub", "without.mp4",
            "--person-greenscreen", "person.mp4",
            "--annotation-skill-path", "skill.md",
            "--director-plan", "director.json",
            "--shot-storyboard-compile-receipt", "compile.json",
            "--static-ppt-plan", "ppt-plan.json",
            "--static-ppt-with-subtitles", "with.pptx",
            "--static-ppt-without-subtitles", "without.pptx",
        ]
        with patch.object(sys, "argv", argv), patch.object(story_workflow, "run_script") as runner:
            story_workflow.main()
        command = list(runner.call_args.args)
        self.assertEqual(command[0], "product_package.py")
        expected = {
            "--director-plan": "director.json",
            "--shot-storyboard-compile-receipt": "compile.json",
            "--static-ppt-plan": "ppt-plan.json",
            "--static-ppt-with-subtitles": "with.pptx",
            "--static-ppt-without-subtitles": "without.pptx",
        }
        for option, value in expected.items():
            self.assertIn(option, command)
            self.assertEqual(str(command[command.index(option) + 1]), value)

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

    def test_title_like_body_opening_is_allowed_when_not_the_reviewed_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "story_sales_subs_bgm.mp4"
            video.touch()
            self.write_background_srt(root, ["很久很久以前", "天地还没有分开"])
            reject_full_subtitle_background(video, story_title="盘古开天辟地")

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
