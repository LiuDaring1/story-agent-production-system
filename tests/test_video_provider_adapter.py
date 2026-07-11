from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from video_provider_adapter import VideoProviderConfigError, resolve_video_provider


class VideoProviderAdapterTests(unittest.TestCase):
    def test_mock_provider_runs_through_real_workflow_without_paid_api(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            images = work / "images"
            videos = work / "videos"
            images.mkdir()
            Image.new("RGB", (320, 180), (60, 120, 180)).save(images / "01.png")
            jobs = work / "demo_image_video_jobs.csv"
            decisions = work / "prompt_review_decisions.csv"
            jobs.write_text(
                "scene,image_filename,story_text,prompt,target_video_filename,duration,frames,status\n"
                "1,01.png,小羊出发,小羊向前走,01_demo.mp4,1.0,24,todo\n",
                encoding="utf-8-sig",
            )
            decisions.write_text(
                "scene,image_filename,story_text,review_status,prompt,notes\n"
                "01,01.png,小羊出发,approved,小羊自然地向前走,通过\n",
                encoding="utf-8-sig",
            )
            process = subprocess.run(
                [
                    sys.executable,
                    str(root / "story_workflow.py"),
                    "generate",
                    "--jobs-csv",
                    str(jobs),
                    "--images-dir",
                    str(images),
                    "--videos-dir",
                    str(videos),
                    "--provider",
                    "mock_local",
                ],
                cwd=root,
                text=True,
                capture_output=True,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertGreater((videos / "01_demo.mp4").stat().st_size, 0)
            with jobs.open(encoding="utf-8-sig", newline="") as file:
                row = next(csv.DictReader(file))
            self.assertEqual(row["status"], "downloaded")
            self.assertEqual(row["prompt"], "小羊自然地向前走")

    def test_selects_named_adapter_without_business_logic_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("pass\n", encoding="utf-8")
            second.write_text("pass\n", encoding="utf-8")
            config = {
                "video_api": {
                    "provider": "first",
                    "adapters": {
                        "first": {"runner": "first.py", "model": "m1", "estimated_cost_cny_per_clip": 1.0},
                        "second": {"runner": "second.py", "model": "m2", "estimated_cost_cny_per_clip": 2.5},
                    },
                }
            }
            selected = resolve_video_provider(config, root, "second")
            self.assertEqual(selected.name, "second")
            self.assertEqual(selected.runner, second)
            self.assertEqual(selected.model, "m2")
            self.assertEqual(selected.estimated_cost_cny_per_clip, 2.5)

    def test_unknown_provider_fails_before_paid_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(VideoProviderConfigError, "未找到 provider"):
                resolve_video_provider({"video_api": {"provider": "missing", "adapters": {}}}, Path(directory))


if __name__ == "__main__":
    unittest.main()
