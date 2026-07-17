from __future__ import annotations

import json
import subprocess
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
    storyboard_plan,
)
from story_agent_runtime import file_sha256
from story_project import init_project, load_manifest, project_paths, write_manifest


class SourceVideoPipelineTests(unittest.TestCase):
    def test_rendered_source_edit_emits_clean_video_and_clean_audio(self) -> None:
        fixture = Path(__file__).resolve().parents[1] / "tools" / "video-subtitle-remover" / "test" / "test2.mp4"
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：媒体输出"
            manifest = init_project(project, story_name="媒体输出", slug="clean-media")
            manifest["agent"]["source"] = {"sha256": file_sha256(fixture), "project_copy": str(fixture)}
            manifest["agent"]["input_contract"] = {
                "version": 1,
                "mode": "single_greenscreen",
                "user_inputs": [
                    {
                        "role": "greenscreen_video",
                        "path": str(fixture),
                        "sha256": file_sha256(fixture),
                        "bytes": fixture.stat().st_size,
                    }
                ],
                "processing_assets": [],
                "derived_inputs": {},
            }
            write_manifest(project_paths(project), manifest)
            outputs = build_source_outputs(
                project,
                {"segments": [{"start": 0.2, "end": 2.2, "text": "从前有一只勇敢的小羊。"}]},
                source_video=fixture,
                render_clean_video=True,
            )
            self.assertGreater(outputs["clean_video"].stat().st_size, 0)
            self.assertGreater(outputs["clean_audio"].stat().st_size, 0)
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,pix_fmt", "-of", "json", str(outputs["clean_video"])],
                text=True,
                capture_output=True,
                check=True,
            )
            video_stream = next(item for item in json.loads(probe.stdout)["streams"] if item["codec_type"] == "video")
            self.assertLessEqual(video_stream["width"], 1920)
            self.assertEqual(video_stream["pix_fmt"], "yuv420p")
            source_qa = json.loads((project_paths(project).status / "qa_source_report.json").read_text(encoding="utf-8"))
            self.assertTrue(source_qa["passed"], source_qa["errors"])
            self.assertAlmostEqual(
                source_qa["durations"]["clean_video"],
                source_qa["durations"]["expected_from_keep_intervals"],
                delta=0.5,
            )
            self.assertIn("edit_decisions", source_qa["artifacts"])
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            self.assertEqual(manifest["inputs"]["greenscreen_video"], str(outputs["clean_video"]))
            self.assertEqual(manifest["inputs"]["extracted_narration"], str(outputs["clean_audio"]))
            derived = manifest["agent"]["input_contract"]["derived_inputs"]
            self.assertEqual(
                set(derived),
                {"story_text", "clean_greenscreen_video", "clean_narration"},
            )
            self.assertTrue(all(item["source_sha256"] == file_sha256(fixture) for item in derived.values()))
            self.assertTrue(all(item["decisions_sha256"] == file_sha256(outputs["edit_decisions"]) for item in derived.values()))

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

    def test_preserves_long_asr_gap_when_no_segment_explicitly_rejected(self) -> None:
        segments = [
            TranscriptSegment(1, 0.0, 2.0, "小青蛙上台了。"),
            TranscriptSegment(2, 8.0, 10.0, "大家都笑了。"),
        ]
        intervals = merge_keep_intervals(choose_clean_segments(segments))
        self.assertEqual(intervals, [{"start": 0.0, "end": 10.35}])

    def test_audio_grounded_insertion_restores_whole_asr_omission(self) -> None:
        fixture = Path(__file__).resolve().parents[1] / "tools" / "video-subtitle-remover" / "test" / "test2.mp4"
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：漏听补回"
            init_project(project, story_name="漏听补回", slug="asr-insertion")
            outputs = build_source_outputs(
                project,
                {
                    "segments": [
                        {"start": 0.2, "end": 0.7, "text": "第一句。"},
                        {"start": 1.4, "end": 2.0, "text": "第三句。"},
                    ]
                },
                source_video=fixture,
                render_clean_video=False,
                text_insertions=[{"start": 0.8, "end": 1.2, "text": "第二句。"}],
            )
            decisions = json.loads(outputs["edit_decisions"].read_text(encoding="utf-8"))
            self.assertEqual([item["text"] for item in decisions["segments"]], ["第一句。", "第二句。", "第三句。"])
            self.assertIn("第二句。", outputs["story_text"].read_text(encoding="utf-8"))

    def test_storyboard_uses_time_and_semantic_breaks(self) -> None:
        segments = [
            TranscriptSegment(1, 0, 6.5, "第一句话讲完了。"),
            TranscriptSegment(2, 6.7, 13.0, "第二句话也讲完了。"),
            TranscriptSegment(3, 13.2, 23.3, "这里进入了新的场景。"),
        ]
        lines = storyboard_lines(segments)
        self.assertEqual(len(lines), 3)

    def test_storyboard_plan_splits_long_segment_and_records_timing(self) -> None:
        plan = storyboard_plan(
            [TranscriptSegment(1, 0.0, 20.0, "小蚂蚁想到了一个好办法。它爬上象腿狠狠咬了一口。")]
        )
        self.assertEqual(len(plan), 2)
        self.assertTrue(all(8.0 <= item["duration"] <= 12.5 for item in plan))
        self.assertEqual(plan[0]["source_segments"], [1])

    def test_storyboard_plan_preserves_closing_quote_and_merges_short_tail(self) -> None:
        plan = storyboard_plan(
            [
                TranscriptSegment(1, 0.0, 6.0, "大象说：“我一定做到。”"),
                TranscriptSegment(2, 6.0, 10.0, "这才是真本领。"),
            ]
        )
        self.assertEqual(len(plan), 1)
        self.assertIn("”", plan[0]["text"])
        self.assertEqual(plan[0]["duration"], 10.0)

    def test_storyboard_plan_preserves_opening_quote_after_colon(self) -> None:
        plan = storyboard_plan(
            [
                TranscriptSegment(1, 0.0, 4.0, "虎妈妈说：“"),
                TranscriptSegment(2, 4.0, 10.0, "要尊重别人。”"),
            ]
        )
        self.assertEqual(len(plan), 1)
        self.assertIn("说：“要尊重别人。”", plan[0]["text"])

    def test_word_timestamp_can_extend_coarse_segment_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：逐词边界"
            init_project(project, story_name="逐词边界", slug="word-boundary")
            video = root / "source.mp4"
            video.write_bytes(b"fake")
            outputs = build_source_outputs(
                project,
                {
                    "segments": [
                        {
                            "start": 1.0,
                            "end": 4.0,
                            "text": "大声喊道。",
                            "words": [{"word": "道。", "start": 3.8, "end": 4.3}],
                        }
                    ]
                },
                source_video=video,
                render_clean_video=False,
                keep_overrides={1: {"keep": True, "trim_start": 1.0, "trim_end": 4.3, "text": "大声喊道。"}},
            )
            decisions = json.loads(outputs["edit_decisions"].read_text(encoding="utf-8"))
            self.assertEqual(decisions["segments"][0]["source_end"], 4.3)
            self.assertEqual(decisions["segments"][0]["end"], 4.3)

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

    def test_consumer_manuscript_keeps_closing_quote_with_dialogue(self) -> None:
        result = consumer_manuscript("小虎说：“请听我唱歌！”大家鼓起掌来。", "标题")
        self.assertIn("请听我唱歌！”\n\n大家鼓起掌来。", result)
        self.assertNotIn("\n\n”", result)

    def test_consumer_manuscript_keeps_multi_sentence_dialogue_together(self) -> None:
        result = consumer_manuscript("小虎说：“第一句！第二句。第三句？”大家鼓掌。", "标题")
        self.assertIn("小虎说：“第一句！第二句。第三句？”\n\n大家鼓掌。", result)
        self.assertNotIn("第一句！\n\n第二句", result)

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

    def test_reviewer_override_can_trim_inside_segment_with_traceability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：段内精剪"
            init_project(project, story_name="段内精剪", slug="precise-trim")
            video = root / "source.mp4"
            video.write_bytes(b"fake")
            transcript = {"segments": [{"start": 10.0, "end": 20.0, "text": "必要语义。嗯，最后一次完整表达。"}]}
            outputs = build_source_outputs(
                project,
                transcript,
                source_video=video,
                render_clean_video=False,
                keep_overrides={
                    1: {
                        "keep": True,
                        "trim_start": 12.0,
                        "trim_end": 19.0,
                        "text": "最后一次完整表达。",
                        "reason": "删除段首旧录与语气词",
                    }
                },
            )
            decisions = json.loads(outputs["edit_decisions"].read_text(encoding="utf-8"))
            segment = decisions["segments"][0]
            self.assertEqual((segment["start"], segment["end"]), (12.0, 19.0))
            self.assertEqual((segment["source_start"], segment["source_end"]), (10.0, 20.0))
            self.assertEqual(segment["source_text"], "必要语义。嗯，最后一次完整表达。")
            self.assertEqual(segment["text"], "最后一次完整表达。")
            self.assertEqual(decisions["keep_intervals"], [{"start": 11.65, "end": 19.35}])

    def test_expressive_interjections_are_not_removed_as_fillers(self) -> None:
        segments = [
            TranscriptSegment(1, 0.0, 0.8, "啊呜！"),
            TranscriptSegment(2, 1.0, 1.8, "哎哟！"),
            TranscriptSegment(3, 2.0, 2.5, "嗯啊"),
        ]
        decided = choose_clean_segments(segments)
        self.assertTrue(decided[0].keep)
        self.assertTrue(decided[1].keep)
        self.assertFalse(decided[2].keep)

    def test_reviewer_override_rejects_trim_outside_original_segment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：越界精剪"
            init_project(project, story_name="越界精剪", slug="invalid-trim")
            video = root / "source.mp4"
            video.write_bytes(b"fake")
            with self.assertRaisesRegex(ValueError, "超出原区间"):
                build_source_outputs(
                    project,
                    {"segments": [{"start": 10.0, "end": 20.0, "text": "正文。"}]},
                    source_video=video,
                    render_clean_video=False,
                    keep_overrides={1: {"keep": True, "trim_start": 9.0, "trim_end": 19.0, "text": "正文。"}},
                )

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

    def test_source_decisions_bind_selected_lut_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：LUT测试"
            init_project(project, story_name="LUT测试", slug="lut-test")
            video = root / "source.mp4"
            video.write_bytes(b"fake")
            lut = root / "sony.cube"
            lut.write_text("LUT_3D_SIZE 2\n0 0 0\n0 0 1\n0 1 0\n0 1 1\n1 0 0\n1 0 1\n1 1 0\n1 1 1\n", encoding="utf-8")
            outputs = build_source_outputs(
                project,
                {"segments": [{"start": 0, "end": 3, "text": "大象遇见了蚂蚁。"}]},
                source_video=video,
                color_lut=lut,
                render_clean_video=False,
            )
            decisions = json.loads(outputs["edit_decisions"].read_text(encoding="utf-8"))
            self.assertEqual(decisions["color_lut_sha256"], file_sha256(lut))
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            self.assertEqual(manifest["inputs"]["color_lut"], str(lut))


if __name__ == "__main__":
    unittest.main()
