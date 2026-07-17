from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from story_agent_runtime import file_sha256
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

    def test_requires_current_master_edit_lineage_for_new_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：封面血缘"
            make_publish_fixture(project)
            paths = project_paths(project)
            (paths.publish / "publish_package_codex_handoff.md").write_text("必须写 cover_lineage.json\n", encoding="utf-8")
            qa_publish(project)
            missing = json.loads((paths.status / "qa_publish_report.json").read_text(encoding="utf-8"))
            self.assertFalse(missing["passed"])
            self.assertIn("缺少 cover_lineage.json", "\n".join(missing["issues"]))

            parents = {
                "main/covers/cover_4x3.png": None,
                "main/covers/cover_3x4.png": "main/covers/cover_4x3.png",
                "main/covers/cover_16x9.png": "main/covers/cover_4x3.png",
                "library/covers/cover_4x3.png": "main/covers/cover_4x3.png",
                "library/covers/cover_3x4.png": "library/covers/cover_4x3.png",
                "library/covers/cover_16x9.png": "library/covers/cover_4x3.png",
            }
            covers = []
            for relative, parent in parents.items():
                path = paths.publish / relative
                covers.append(
                    {
                        "path": relative,
                        "parent": parent,
                        "parent_sha256": file_sha256(paths.publish / parent) if parent else None,
                        "generation_mode": "edit-derived" if parent else "master",
                        "reference_files": ["current_story_reference.png"],
                        "sha256": file_sha256(path),
                    }
                )
            (paths.publish / "cover_lineage.json").write_text(json.dumps({"version": 1, "covers": covers}, ensure_ascii=False), encoding="utf-8")
            qa_publish(project)
            passed = json.loads((paths.status / "qa_publish_report.json").read_text(encoding="utf-8"))
            self.assertTrue(passed["passed"], passed["issues"])

            stale = json.loads((paths.publish / "cover_lineage.json").read_text(encoding="utf-8"))
            stale["covers"][0]["sha256"] = "0" * 64
            (paths.publish / "cover_lineage.json").write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")
            qa_publish(project)
            rejected = json.loads((paths.status / "qa_publish_report.json").read_text(encoding="utf-8"))
            self.assertFalse(rejected["passed"])
            self.assertIn("当前哈希失效", "\n".join(rejected["issues"]))


if __name__ == "__main__":
    unittest.main()
