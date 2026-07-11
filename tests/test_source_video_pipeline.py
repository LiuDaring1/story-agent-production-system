from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from source_video_pipeline import (
    TranscriptSegment,
    build_source_outputs,
    choose_clean_segments,
    consumer_manuscript,
    merge_keep_intervals,
    retime_kept_segments,
    storyboard_lines,
)
from story_agent_runtime import file_sha256
from story_project import init_project, load_manifest, project_paths


class SourceVideoPipelineTests(unittest.TestCase):
    def test_rendered_source_edit_emits_clean_video_and_clean_audio(self) -> None:
        fixture = Path(__file__).resolve().parents[1] / "tools" / "video-subtitle-remover" / "test" / "test2.mp4"
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：媒体输出"
            init_project(project, story_name="媒体输出", slug="clean-media")
            outputs = build_source_outputs(
                project,
                {"segments": [{"start": 0.2, "end": 2.2, "text": "从前有一只勇敢的小羊。"}]},
                source_video=fixture,
                render_clean_video=True,
            )
            self.assertGreater(outputs["clean_video"].stat().st_size, 0)
            self.assertGreater(outputs["clean_audio"].stat().st_size, 0)
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            self.assertEqual(manifest["inputs"]["greenscreen_video"], str(outputs["clean_video"]))
            self.assertEqual(manifest["inputs"]["extracted_narration"], str(outputs["clean_audio"]))

    def test_keeps_last_complete_take_and_removes_filler(self) -> None:
        segments = [
            TranscriptSegment(1, 0.0, 2.0, "从前有一只"),
            TranscriptSegment(2, 2.2, 5.0, "从前有一只聪明的小兔子。"),
            TranscriptSegment(3, 5.2, 5.8, "嗯啊"),
            TranscriptSegment(4, 6.0, 11.0, "它每天都去森林里采蘑菇。"),
        ]
        decided = choose_clean_segments(segments)
        self.assertFalse(decided[0].keep)
        self.assertIn("keep_last_complete_take", decided[0].reason)
        self.assertFalse(decided[2].keep)
        self.assertEqual([item.index for item in decided if item.keep], [2, 4])
        intervals = merge_keep_intervals(decided)
        self.assertEqual(len(intervals), 2)
        retimed = retime_kept_segments(decided, intervals)
        self.assertLess(retimed[-1].start, decided[-1].start)

    def test_storyboard_uses_time_and_semantic_breaks(self) -> None:
        segments = [
            TranscriptSegment(1, 0, 6.5, "第一句话讲完了。"),
            TranscriptSegment(2, 6.7, 13.0, "第二句话也讲完了。"),
            TranscriptSegment(3, 13.2, 23.3, "这里进入了新的场景。"),
        ]
        lines = storyboard_lines(segments)
        self.assertEqual(len(lines), 3)

    def test_build_outputs_without_rendering_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：测试故事"
            init_project(project, story_name="测试故事", slug="test-story")
            video = root / "source.mp4"
            video.write_bytes(b"fake")
            transcript = {
                "text": "大家好，我是绵羊姐姐。从前有一只小兔子。我的故事讲完了。",
                "segments": [
                    {"start": 0, "end": 3, "text": "大家好，我是绵羊姐姐。"},
                    {"start": 3.1, "end": 10, "text": "从前有一只小兔子。"},
                    {"start": 10.1, "end": 12, "text": "我的故事讲完了。"},
                ],
            }
            outputs = build_source_outputs(project, transcript, source_video=video, render_clean_video=False)
            self.assertTrue(outputs["story_text"].exists())
            self.assertTrue(outputs["edit_decisions"].exists())
            decisions = json.loads(outputs["edit_decisions"].read_text(encoding="utf-8"))
            self.assertEqual(decisions["source_sha256"], file_sha256(video))
            manuscript = outputs["consumer_manuscript"].read_text(encoding="utf-8")
            self.assertIn("故事文稿：测试故事", manuscript)
            self.assertNotIn("我是绵羊姐姐", manuscript)
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            self.assertEqual(manifest["inputs"]["story_text"], str(outputs["story_text"]))

    def test_consumer_manuscript_removes_host_bookends(self) -> None:
        result = consumer_manuscript("大家好，我是绵羊姐姐。故事内容。我的故事讲完了。", "标题")
        self.assertNotIn("绵羊姐姐", result)
        self.assertNotIn("我的故事讲完了", result)

    def test_reviewer_override_changes_edit_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：覆盖测试"
            init_project(project, story_name="覆盖测试", slug="override")
            video = root / "source.mp4"
            video.write_bytes(b"fake")
            transcript = {"segments": [{"start": 0, "end": 3, "text": "嗯啊"}, {"start": 3, "end": 9, "text": "正文。"}]}
            outputs = build_source_outputs(
                project,
                transcript,
                source_video=video,
                render_clean_video=False,
                keep_overrides={1: (True, "审核确认这是角色台词，不是语气词")},
            )
            decisions = json.loads(outputs["edit_decisions"].read_text(encoding="utf-8"))
            first = decisions["segments"][0]
            self.assertTrue(first["keep"])
            self.assertIn("review_override", first["reason"])

    def test_text_override_corrects_asr_without_changing_timing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：校对测试"
            init_project(project, story_name="校对测试", slug="correction")
            video = root / "source.mp4"
            video.write_bytes(b"fake")
            transcript = {"segments": [{"start": 1.0, "end": 5.0, "text": "小白图跑进森林。"}]}
            outputs = build_source_outputs(
                project,
                transcript,
                source_video=video,
                render_clean_video=False,
                text_overrides={1: "小白兔跑进森林。"},
                text_overrides_source_sha256="correction-sha",
            )
            decisions = json.loads(outputs["edit_decisions"].read_text(encoding="utf-8"))
            self.assertEqual(decisions["segments"][0]["text"], "小白兔跑进森林。")
            self.assertEqual(decisions["segments"][0]["start"], 1.0)
            self.assertEqual(decisions["text_overrides_source_sha256"], "correction-sha")


if __name__ == "__main__":
    unittest.main()
