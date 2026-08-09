from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from release_video import (
    build_tail_frame_probe_commands,
    person_tail_pad_seconds,
    probe_video_size,
    release_plate_integrity_issues,
    safe_watermark_motion_expressions,
    story_frame_integrity_issues,
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
        self.assertTrue(expressions[0].startswith("20+"))
        self.assertTrue(expressions[1].startswith("20+"))
        self.assertTrue(expressions[2].startswith("W-w-20-"))
        self.assertTrue(expressions[3].startswith("H-h-20-"))

    def test_person_tail_padding_is_deterministic_and_clones_last_frame(self) -> None:
        self.assertEqual(person_tail_pad_seconds(12.03, 12.0), 0.0)
        # A stream that is a few frames short receives a small deterministic
        # clone window instead of allowing ffmpeg to synthesize an EOF frame.
        self.assertAlmostEqual(person_tail_pad_seconds(11.96, 12.0), 0.08, places=6)
        self.assertAlmostEqual(person_tail_pad_seconds(11.96, 12.0, frame_duration=1 / 25), 0.08, places=6)

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
