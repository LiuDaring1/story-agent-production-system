from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from lxml import etree

from dynamic_story_ppt import (
    SCHEMA_VERSION,
    build_dynamic_story_ppts,
    dynamic_ppt_plan_issues,
    load_source_plan,
)


ROOT = Path(__file__).resolve().parents[1]


def command(args: list[str]) -> None:
    completed = subprocess.run(args, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr)


class DynamicStoryPptValidationTests(unittest.TestCase):
    def test_plan_rejects_missing_video_and_duplicate_shot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing.mp4"
            plan = root / "plan.json"
            plan.write_text(json.dumps({
                "schema_version": SCHEMA_VERSION,
                "slides": [
                    {"shot_id": "S01", "video_path": str(missing), "subtitle": "第一句"},
                    {"shot_id": "S01", "video_path": str(missing), "subtitle": "第二句"},
                ],
            }), encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                load_source_plan(plan)


@unittest.skipUnless(
    os.environ.get("RUNTIME_NODE") and os.environ.get("RUNTIME_NODE_MODULES") and shutil.which("ffmpeg"),
    "dynamic PPT integration needs bundled Node paths and ffmpeg",
)
class DynamicStoryPptIntegrationTests(unittest.TestCase):
    def test_builds_four_full_motion_variants_with_one_shared_media_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clips: list[Path] = []
            for index, color in enumerate(("red", "green", "blue"), start=1):
                clip = root / f"shot-{index}.mp4"
                command([
                    "ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=640x360:d=0.45",
                    "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip),
                ])
                clips.append(clip)
            music = root / "music.mp3"
            command([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-q:a", "8", str(music),
            ])
            plan = root / "input-plan.json"
            plan.write_text(json.dumps({
                "schema_version": SCHEMA_VERSION,
                "story_name": "三镜短测",
                "slides": [
                    {"shot_id": f"S{index:02d}", "video_path": str(clip), "subtitle": f"这是第{index}个完整镜头。"}
                    for index, clip in enumerate(clips, start=1)
                ],
            }, ensure_ascii=False), encoding="utf-8")

            outputs = build_dynamic_story_ppts(
                source_plan_path=plan,
                music_path=music,
                output_dir=root / "output",
                work_dir=root / "work",
                node=Path(os.environ["RUNTIME_NODE"]),
                node_modules=Path(os.environ["RUNTIME_NODE_MODULES"]),
                builder_source=ROOT / "skills" / "story-full-auto" / "scripts" / "build_dynamic_story_ppt.mjs",
            )
            variant_keys = {
                "with_subtitles_auto", "without_subtitles_auto",
                "with_subtitles_control", "without_subtitles_control",
            }
            self.assertTrue(variant_keys.issubset(outputs))
            resolved = json.loads(outputs["plan"].read_text(encoding="utf-8"))
            self.assertEqual(len(resolved["slides"]), 3)
            self.assertEqual(set(resolved["outputs"]), variant_keys)
            self.assertEqual(resolved["media_mode"], "linked_shared_directory")
            self.assertEqual(dynamic_ppt_plan_issues(outputs["plan"]), [])
            self.assertTrue(json.loads(outputs["qa_report"].read_text(encoding="utf-8"))["passed"])
            self.assertEqual(len(list(outputs["media_dir"].glob("*.mp4"))), 3)
            self.assertTrue((outputs["media_dir"] / "故事背景音乐.mp3").is_file())

            for key in variant_keys:
                with zipfile.ZipFile(outputs[key]) as archive:
                    names = archive.namelist()
                    self.assertEqual(len([name for name in names if name.startswith("ppt/media/shot-")]), 0)
                    self.assertNotIn("ppt/media/background-music.mp3", names)
                    slide = etree.fromstring(archive.read("ppt/slides/slide1.xml"))
                    self.assertTrue(slide.xpath("//*[local-name()='videoFile']"))
                    transition = slide.xpath("//*[local-name()='transition']")[0]
                    if key.endswith("_auto"):
                        self.assertIsNotNone(transition.get("advTm"))
                        self.assertFalse(slide.xpath("//*[local-name()='audio']//*[local-name()='cTn'][@repeatCount='indefinite']"))
                    else:
                        self.assertIsNone(transition.get("advTm"))
                        self.assertTrue(slide.xpath("//*[local-name()='audio']//*[local-name()='cTn'][@repeatCount='indefinite']"))
                    rels = archive.read("ppt/slides/_rels/slide1.xml.rels")
                    self.assertIn(b"relationships/video", rels)
                    self.assertIn(b"relationships/audio", rels)
                    self.assertIn(b'TargetMode="External"', rels)
                    self.assertIn("PPT动态素材".encode(), rels)


if __name__ == "__main__":
    unittest.main()
