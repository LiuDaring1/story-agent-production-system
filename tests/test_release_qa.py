from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from story_project import init_project, project_paths, qa_release


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


if __name__ == "__main__":
    unittest.main()
