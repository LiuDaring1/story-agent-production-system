from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from release_geometry import CANONICAL_B_STORY_BOX
from release_video import (
    build_tail_frame_probe_commands,
    person_tail_pad_seconds,
    probe_video_stream_duration,
    release_preview_sample_window,
    probe_video_size,
    release_plate_integrity_issues,
    render_static_assets,
    safe_watermark_motion_expressions,
    story_frame_integrity_issues,
    validate_release_assets,
    video_window_black_edge_issues,
)
from story_project import init_project, project_paths, qa_release, write_manifest


def make_vertical_video(path: Path, *, with_audio: bool = True) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=0x335577:s=300x400:d=1.2:r=24",
    ]
    if with_audio:
        command.extend(["-f", "lavfi", "-i", "sine=frequency=330:duration=1.2", "-shortest"])
    command.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p"])
    if with_audio:
        command.extend(["-c:a", "aac"])
    command.append(str(path))
    process = subprocess.run(command, text=True, capture_output=True)
    if process.returncode != 0:
        raise RuntimeError(process.stderr)


class ReleaseQaTests(unittest.TestCase):
    def test_terminal_preview_shifts_full_sample_window_before_eof(self) -> None:
        start, duration, frame_time = release_preview_sample_window(179.883, 179.7)
        self.assertAlmostEqual(start, 179.483, places=3)
        self.assertAlmostEqual(duration, 0.4, places=6)
        self.assertAlmostEqual(start + frame_time, 179.7, places=3)

    def test_regular_preview_keeps_existing_forward_sample_behavior(self) -> None:
        start, duration, frame_time = release_preview_sample_window(179.883, 52.0)
        self.assertEqual(start, 52.0)
        self.assertEqual(duration, 0.4)
        self.assertEqual(frame_time, 0.2)

    def test_probe_video_size_uses_ffprobe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "probe.mp4"
            make_vertical_video(video, with_audio=False)
            self.assertEqual(probe_video_size(video), (300, 400))

    def test_moving_watermarks_stay_inside_safe_margin(self) -> None:
        expressions = safe_watermark_motion_expressions(70.0, 42.0, margin=20)
        self.assertEqual(len(expressions), 4)
        for expression in expressions:
            self.assertIn("max(1\\,", expression)
            self.assertNotIn(")-w", expression)
            self.assertIn("cos(PI*", expression)
            self.assertIn("floor(", expression)
            self.assertNotIn("mod(", expression)
        self.assertTrue(expressions[0].startswith("20+"))
        self.assertTrue(expressions[1].startswith("20+"))
        self.assertTrue(expressions[2].startswith("W-w-20-"))
        self.assertTrue(expressions[3].startswith("H-h-20-"))

    def test_person_tail_padding_is_deterministic_and_clones_last_frame(self) -> None:
        # A nominally equal stream can still have its final decoded PTS one
        # frame before the container duration, so keep one safety frame.
        self.assertAlmostEqual(person_tail_pad_seconds(12.0, 12.0), 0.04, places=6)
        self.assertAlmostEqual(person_tail_pad_seconds(12.03, 12.0), 0.04, places=6)
        self.assertEqual(person_tail_pad_seconds(12.05, 12.0), 0.0)
        # A stream that is a few frames short receives a small deterministic
        # clone window instead of allowing ffmpeg to synthesize an EOF frame.
        self.assertAlmostEqual(person_tail_pad_seconds(11.96, 12.0), 0.08, places=6)
        self.assertAlmostEqual(person_tail_pad_seconds(11.96, 12.0, frame_duration=1 / 25), 0.08, places=6)
        self.assertAlmostEqual(person_tail_pad_seconds(None, 12.0, frame_duration=1 / 30), 1 / 30, places=6)

    def test_video_only_webm_uses_format_duration_when_stream_duration_is_missing(self) -> None:
        responses = [
            SimpleNamespace(returncode=0, stdout="N/A\n"),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "streams": [{"codec_type": "video"}],
                    "format": {"duration": "179.866"},
                }),
            ),
        ]
        with patch("release_video.subprocess.run", side_effect=responses):
            self.assertEqual(probe_video_stream_duration(Path("presenter.webm")), 179.866)

    def test_muxed_file_does_not_use_format_duration_as_video_duration(self) -> None:
        responses = [
            SimpleNamespace(returncode=0, stdout="N/A\n"),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
                    "format": {"duration": "180.0"},
                }),
            ),
        ]
        with patch("release_video.subprocess.run", side_effect=responses):
            self.assertIsNone(probe_video_stream_duration(Path("muxed.mp4")))

    def test_tail_probe_samples_the_whole_last_two_seconds_and_final_frame(self) -> None:
        commands = build_tail_frame_probe_commands(Path("release.mp4"), Path("frames"), 10.0, fps=8)
        self.assertEqual(len(commands), 2)
        tail_command, final_command = commands
        self.assertIn("-t", tail_command)
        self.assertIn("2.000", tail_command)
        self.assertIn("fps=8", tail_command)
        self.assertIn("9.875", final_command)
        self.assertNotIn("9.500", tail_command)

    def test_release_plate_integrity_rejects_black_partition_and_large_black_rectangle(self) -> None:
        image = Image.new("RGB", (1080, 1440), (245, 240, 220))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 416, 1079, 425), fill=(0, 0, 0))
        draw.rectangle((160, 1040, 920, 1310), fill=(0, 0, 0))
        issues = release_plate_integrity_issues(image, (0, 416, 1080, 608))
        self.assertTrue(any("black_seam" in issue for issue in issues))
        self.assertTrue(any("black_rectangle" in issue for issue in issues))

    def test_video_window_edge_gate_catches_center_band_rim_missed_by_whole_canvas(self) -> None:
        image = Image.new("RGB", (1080, 1440), (245, 240, 220))
        draw = ImageDraw.Draw(image)
        video_box = (0, 416, 1080, 608)
        draw.rectangle((1068, 416, 1079, 1023), fill=(4, 4, 4))
        issues = video_window_black_edge_issues(image, video_box)
        self.assertTrue(any("black_edge:right" in issue for issue in issues), issues)

    def test_video_window_edge_gate_ignores_local_dark_object(self) -> None:
        image = Image.new("RGB", (1080, 1440), (220, 190, 120))
        draw = ImageDraw.Draw(image)
        video_box = (0, 416, 1080, 608)
        draw.rectangle((0, 416, 28, 560), fill=(0, 0, 0))
        self.assertFalse(video_window_black_edge_issues(image, video_box))

    def test_story_frame_integrity_rejects_missing_corner(self) -> None:
        image = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        window = (170, 250, 990, 557)
        x, y, width, height = window
        draw.rectangle((x - 40, y - 40, x + width + 40, y + height + 40), outline=(255, 180, 80, 255), width=18)
        draw.rectangle((x - 50, y - 50, x + 15, y + 15), fill=(0, 0, 0, 0))
        issues = story_frame_integrity_issues(image, window)
        self.assertTrue(any("corner_missing" in issue for issue in issues))

    def test_story_frame_integrity_rejects_open_side_gap(self) -> None:
        image = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        window = (170, 250, 990, 557)
        x, y, width, height = window
        draw.rectangle((x, y, x + width, y + height), outline=(255, 180, 80, 255), width=18)
        draw.rectangle((x + width // 2 - 60, y - 30, x + width // 2 + 60, y + 30), fill=(0, 0, 0, 0))
        issues = story_frame_integrity_issues(image, window)
        self.assertTrue(any("contour_gap" in issue for issue in issues))

    def test_story_frame_integrity_accepts_decorative_curved_corners(self) -> None:
        image = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        window = (210, 270, 910, 512)
        x, y, width, height = window
        # A rounded decorative frame is closed, but its corner transition is
        # not a straight side and must not be reported as a contour gap.
        draw.rounded_rectangle(
            (x - 80, y - 80, x + width + 80, y + height + 80),
            radius=70,
            outline=(255, 180, 80, 255),
            width=18,
        )
        draw.ellipse(
            (x + width - 80, y - 80, x + width + 80, y + 80),
            fill=(255, 180, 80, 255),
        )
        # Production frames also need a continuous inner masking lip that
        # overlaps the video rectangle; outer decoration alone is insufficient.
        draw.rounded_rectangle(
            (x - 8, y - 8, x + width + 8, y + height + 8),
            radius=28,
            outline=(255, 220, 150, 255),
            width=22,
        )
        issues = story_frame_integrity_issues(image, window)
        self.assertFalse(issues)

    def test_story_frame_integrity_rejects_closed_but_hairline_mask(self) -> None:
        image = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        window = (210, 270, 910, 512)
        x, y, width, height = window
        draw.rounded_rectangle(
            (x, y, x + width, y + height),
            radius=24,
            outline=(255, 180, 80, 255),
            width=3,
        )
        issues = story_frame_integrity_issues(image, window)
        self.assertTrue(any("masking_lip_too_thin" in issue for issue in issues), issues)

    def test_release_asset_validation_prepares_reused_frame_for_b_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            frame_path = Path(directory) / "story_frame_a.png"
            image = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            a_window = (170, 250, 990, 557)
            x, y, width, height = a_window
            draw.rounded_rectangle(
                (x - 58, y - 58, x + width + 58, y + height + 58),
                radius=60,
                fill=(236, 190, 120, 255),
                outline=(105, 70, 36, 255),
                width=8,
            )
            draw.rounded_rectangle(
                (x - 6, y - 6, x + width + 6, y + height + 6),
                radius=28,
                outline=(255, 244, 210, 255),
                width=16,
            )
            image.save(frame_path)
            config = SimpleNamespace(
                plate_image=None,
                frame_image=frame_path,
                story_box=a_window,
                frame_image_b=frame_path,
                b_story_box=CANONICAL_B_STORY_BOX,
                b_windows=((0.0, 1.0),),
            )
            # The raw A frame is not positioned around B, but preview/render
            # first fits the reused asset to B and should pass this gate.
            validate_release_assets(config)

            broken_b = Path(directory) / "broken_frame_b.png"
            Image.new("RGBA", (1920, 1080), (0, 0, 0, 0)).save(broken_b)
            broken_config = SimpleNamespace(**{**vars(config), "frame_image_b": broken_b})
            with self.assertRaisesRegex(ValueError, "frame_empty"):
                validate_release_assets(broken_config)

    def test_release_asset_validation_without_b_windows_has_no_phantom_b_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            frame_path = Path(directory) / "story_frame_a.png"
            image = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            story_box = (170, 250, 990, 557)
            x, y, width, height = story_box
            draw.rounded_rectangle(
                (x - 58, y - 58, x + width + 58, y + height + 58),
                radius=60,
                outline=(105, 70, 36, 255),
                width=18,
            )
            draw.rounded_rectangle(
                (x - 8, y - 8, x + width + 8, y + height + 8),
                radius=28,
                outline=(255, 220, 150, 255),
                width=22,
            )
            image.save(frame_path)
            config = SimpleNamespace(
                plate_image=None,
                frame_image=frame_path,
                story_box=story_box,
                frame_image_b=None,
                b_story_box=CANONICAL_B_STORY_BOX,
                b_windows=(),
            )

            validate_release_assets(config)

    def test_library_static_assets_have_no_phantom_story_frame_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            panel = root / "panel.png"
            Image.new("RGBA", (16, 16), (255, 255, 255, 255)).save(panel)
            config = SimpleNamespace(
                variant="library",
                frame_image=None,
                plate_image=None,
                story_box=(210, 270, 910, 512),
                frame_image_b=None,
                b_story_box=(356, 180, 1209, 680),
                b_windows=(),
                main_top_panel=panel,
                main_bottom_panel=panel,
                library_top_panel=panel,
                library_bottom_panel=panel,
                antipiracy_logo=panel,
                tail_notice_text="",
            )
            with patch("release_video.render_default_frame", side_effect=AssertionError("unused")):
                assets = render_static_assets(config, root)
            self.assertIsNone(assets["frame"])

    def test_new_project_has_no_implicit_wall_clock_completion_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：八小时默认策略"
            manifest = init_project(project, story_name="十小时默认策略", slug="ten-hour-default")

            self.assertFalse(manifest["agent"]["runtime_deadline_enabled"])
            self.assertEqual(manifest["agent"]["deadline_hours"], 0.0)
            self.assertIsNone(manifest["agent"]["target_delivery_seconds"])
            self.assertEqual(
                manifest["agent"]["deadline_behavior"],
                "no_implicit_wall_clock_deadline",
            )

    def test_release_qa_requires_vertical_video_with_aligned_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：终片QA"
            init_project(project, story_name="终片QA", slug="release-qa")
            paths = project_paths(project)
            make_vertical_video(paths.release / "主账号发布视频.mp4")
            make_vertical_video(paths.release / "宝库号发布视频.mp4")
            qa_release(project)
            payload = json.loads((paths.status / "qa_release_report.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["passed"])
            self.assertEqual(payload["schema_version"], "story-release-machine-qa/v2")
            self.assertEqual(payload["critical_errors"], [])
            self.assertEqual(len(payload["artifacts"]), 2)

    def test_release_qa_rejects_missing_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：缺音轨"
            init_project(project, story_name="缺音轨", slug="missing-audio")
            paths = project_paths(project)
            make_vertical_video(paths.release / "主账号发布视频.mp4", with_audio=False)
            make_vertical_video(paths.release / "宝库号发布视频.mp4")
            qa_release(project)
            payload = json.loads((paths.status / "qa_release_report.json").read_text(encoding="utf-8"))
            self.assertFalse(payload["passed"])
            self.assertTrue(payload["critical_errors"])
            self.assertTrue(any("缺少音轨" in issue for issue in payload["issues"]))

    def test_release_qa_never_uses_rejected_video_when_canonical_output_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：拒绝区隔离"
            manifest = init_project(project, story_name="拒绝区隔离", slug="release-rejected")
            paths = project_paths(project)
            current_main = paths.release / "主账号发布视频.mp4"
            current_library = paths.release / "宝库号发布视频.mp4"
            make_vertical_video(current_main)
            make_vertical_video(current_library)
            rejected = paths.status / "rejected" / "release_videos" / "old"
            rejected.mkdir(parents=True, exist_ok=True)
            rejected_main = rejected / "主账号发布视频.mp4"
            rejected_library = rejected / "宝库号发布视频.mp4"
            make_vertical_video(rejected_main)
            make_vertical_video(rejected_library)
            manifest["outputs"]["main_release_video"] = str(rejected_main)
            manifest["outputs"]["library_release_video"] = str(rejected_library)
            write_manifest(paths, manifest)

            qa_release(project)
            payload = json.loads((paths.status / "qa_release_report.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["artifacts"]["main_release_video"]["path"], str(current_main))
            self.assertEqual(payload["artifacts"]["library_release_video"]["path"], str(current_library))


if __name__ == "__main__":
    unittest.main()
