from __future__ import annotations

import tempfile
import subprocess
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from story_project import (
    auto_keying,
    init_project,
    preview_chromakey,
    render_keying_candidate_sheet,
    sample_green_across_frames,
    select_keying_representative_frames,
)


def make_green_frame(path: Path, *, gesture: bool) -> None:
    image = Image.new("RGB", (640, 360), (20, 220, 30))
    draw = ImageDraw.Draw(image)
    draw.ellipse((280, 55, 360, 135), fill=(210, 165, 130))
    draw.rectangle((270, 125, 370, 330), fill=(238, 235, 225))
    if gesture:
        draw.rectangle((100, 145, 540, 185), fill=(210, 165, 130))
    else:
        draw.rectangle((235, 145, 405, 185), fill=(210, 165, 130))
    image.save(path, quality=96)


class KeyingSearchTests(unittest.TestCase):
    def test_auto_keying_writes_search_evidence_and_selected_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：抠像搜索"
            init_project(project, story_name="抠像搜索", slug="keying-search")
            frame = root / "green.png"
            video = root / "green.mp4"
            make_green_frame(frame, gesture=True)
            process = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loop",
                    "1",
                    "-i",
                    str(frame),
                    "-t",
                    "2",
                    "-r",
                    "24",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:v",
                    "libx264",
                    str(video),
                ],
                text=True,
                capture_output=True,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            preset_path = auto_keying(project, video)
            search = project / "04_发布视频" / "keying" / "keying_search.json"
            sheet = project / "04_发布视频" / "keying" / "keying_candidates.jpg"
            self.assertTrue(search.exists())
            self.assertGreater(sheet.stat().st_size, 0)
            import json

            preset = json.loads(preset_path.read_text(encoding="utf-8"))
            payload = json.loads(search.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["candidates"]), 9)
            self.assertEqual(preset["keying_candidate"], payload["recommended_candidate"])
            candidate_ids = {item["id"] for item in payload["candidates"]}
            self.assertIn(preset["keying_candidate"], candidate_ids)
            self.assertIn("candidate_detail_sheet", payload)
            self.assertTrue(Path(payload["candidate_detail_sheet"]).exists())
            # With a stable green screen the conservative centre candidate is
            # selected; the recommendation must be evidence-backed by the
            # searched candidates rather than a hard-coded aggressive value.
            similarities = sorted({item["similarity"] for item in payload["candidates"]})
            centre_similarity = similarities[len(similarities) // 2]
            self.assertEqual(preset["chroma_similarity"], centre_similarity)
            self.assertEqual(preset["chroma_blend"], 0.04)

            with Image.open(payload["candidate_detail_sheet"]) as detail:
                # Detail evidence is intentionally larger than the old 300px
                # thumbnails so hair and hand edges can be inspected.
                self.assertGreaterEqual(detail.width, 1800)
                self.assertGreaterEqual(detail.height, 600)

    def test_selects_standing_and_wide_gesture_frames_and_renders_grid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            standing = root / "standing.jpg"
            gesture = root / "gesture.jpg"
            make_green_frame(standing, gesture=False)
            make_green_frame(gesture, gesture=True)
            color, variation = sample_green_across_frames([standing, gesture])
            selected_standing, selected_gesture = select_keying_representative_frames([gesture, standing], color)
            self.assertEqual(selected_standing, standing)
            self.assertEqual(selected_gesture, gesture)
            self.assertLess(variation, 10)

            output = root / "candidates.jpg"
            candidates = [
                {"id": "low", "similarity": 0.06, "blend": 0.02},
                {"id": "balanced", "similarity": 0.08, "blend": 0.04},
            ]
            render_keying_candidate_sheet(standing, gesture, color, candidates, output)
            self.assertGreater(output.stat().st_size, 0)
            with Image.open(output) as sheet:
                self.assertGreaterEqual(sheet.width, 1200)

    def test_preview_chromakey_replaces_green_background(self) -> None:
        image = Image.new("RGB", (80, 60), (20, 220, 30))
        ImageDraw.Draw(image).rectangle((30, 10, 50, 55), fill=(240, 230, 220))
        preview = preview_chromakey(image, (20, 220, 30), 0.08, 0.04)
        self.assertNotEqual(preview.getpixel((0, 0)), (20, 220, 30))
        self.assertEqual(preview.getpixel((40, 30)), (240, 230, 220))


if __name__ == "__main__":
    unittest.main()
