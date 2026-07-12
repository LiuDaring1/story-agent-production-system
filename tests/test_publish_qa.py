from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from story_project import init_project, project_paths, qa_publish


def make_publish_fixture(project: Path, *, mechanical: bool = False) -> None:
    paths = project_paths(project)
    init_project(project, story_name="发布QA", slug="publish-qa")
    sizes = {"3x4": (900, 1200), "4x3": (1200, 900), "16x9": (1280, 720)}
    colors = {
        "main": {"3x4": (60, 100, 170), "4x3": (150, 80, 120), "16x9": (70, 160, 105)},
        "library": {"3x4": (180, 120, 70), "4x3": (75, 130, 180), "16x9": (145, 75, 165)},
    }
    for account in ("main", "library"):
        copy = paths.publish / account / "copy.md"
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text("# 发布标题\n\n这里是完整正文内容，介绍故事亮点和适龄信息。\n\n#儿童故事 #亲子阅读\n", encoding="utf-8")
        for ratio, size in sizes.items():
            cover = paths.publish / account / "covers" / f"cover_{ratio}.png"
            cover.parent.mkdir(parents=True, exist_ok=True)
            color = (90, 120, 150) if mechanical else colors[account][ratio]
            image = Image.new("RGB", size, color)
            if not mechanical:
                draw = ImageDraw.Draw(image)
                draw.rectangle((size[0] // 5, size[1] // 5, size[0] // 2, size[1] // 2), fill=(240, 210, 120))
            image.save(cover)


class PublishQaTests(unittest.TestCase):
    def test_passes_six_distinct_correct_ratio_covers_and_two_copies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：发布QA"
            make_publish_fixture(project)
            qa_publish(project)
            payload = json.loads((project_paths(project).status / "qa_publish_report.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["passed"], payload["issues"])
            self.assertEqual(len(payload["artifacts"]), 8)

    def test_rejects_wrong_ratio_and_mechanically_resized_master(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：机械封面"
            make_publish_fixture(project, mechanical=True)
            wrong = project_paths(project).publish / "main" / "covers" / "cover_16x9.png"
            Image.new("RGB", (900, 1200), (90, 120, 150)).save(wrong)
            qa_publish(project)
            payload = json.loads((project_paths(project).status / "qa_publish_report.json").read_text(encoding="utf-8"))
            self.assertFalse(payload["passed"])
            combined = "\n".join(payload["issues"])
            self.assertIn("比例错误", combined)
            self.assertIn("机械缩放", combined)
            self.assertTrue(payload["retry_files"])


if __name__ == "__main__":
    unittest.main()
