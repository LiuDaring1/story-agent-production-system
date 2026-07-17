from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from product_package import AnnotationBlock, validate_annotation_blocks, validate_annotation_coverage
from story_project import init_project, project_paths, qa_product, write_manifest


def make_package_fixture(project: Path) -> tuple[Path, Path]:
    paths = project_paths(project)
    manifest = init_project(project, story_name="资料包QA", slug="product-qa")
    base = paths.product / "绵羊故事锦囊：资料包QA（基础版）"
    advanced = paths.product / "绵羊故事锦囊：资料包QA（进阶版）"
    base.mkdir(parents=True)
    advanced.mkdir(parents=True)
    base_names = (
        "故事文稿：资料包QA.docx",
        "故事配乐：资料包QA.mp3",
        "朗读标注：资料包QA.docx",
        "示范表演：资料包QA.mp4",
        "背景图片：资料包QA.png",
    )
    advanced_names = (
        *base_names,
        "背景视频：资料包QA（含字幕）.mp4",
        "背景视频：资料包QA（无字幕）.mp4",
        "故事PPT：资料包QA（含字幕）.pptx",
        "故事PPT：资料包QA（无字幕）.pptx",
        "A镜无人物背景视频：资料包QA.mp4",
    )
    for name in base_names:
        (base / name).write_bytes(b"fixture")
    for name in advanced_names:
        (advanced / name).write_bytes(b"fixture")
    manifest["outputs"]["product_base"] = str(base)
    manifest["outputs"]["product_advanced"] = str(advanced)
    write_manifest(paths, manifest)
    return base, advanced


class ProductQaTests(unittest.TestCase):
    def test_annotation_requires_verbatim_text_and_natural_teacher_notes(self) -> None:
        good = [
            AnnotationBlock(
                title="决定",
                marked_text="小老虎说：“这才是**合格的评委**。”",
                emotion="认真地想明白了",
                notes=["说到“合格的评委”时放慢一点，像终于想明白了，认真地点一下头。"],
            )
        ]
        validate_annotation_blocks(good)
        validate_annotation_coverage(good, ["小老虎说：“这才是合格的评委。”"])
        with self.assertRaises(ValueError):
            validate_annotation_coverage(good, ["小老虎说：“这才是合格的小评委。”"])
        stiff = [
            AnnotationBlock(
                title="决定",
                marked_text="小老虎说：“这才是**合格的评委**。”",
                emotion="认真",
                notes=["第二个节奏落点要突出判断感，完成收束。"],
            )
        ]
        with self.assertRaises(ValueError):
            validate_annotation_blocks(stiff)

    def test_requires_both_subtitle_variants_for_video_and_ppt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：资料包QA"
            _base, advanced = make_package_fixture(project)
            qa_product(project)
            report = project_paths(project).status / "qa_product_report.json"
            self.assertTrue(json.loads(report.read_text(encoding="utf-8"))["passed"])

            (advanced / "背景视频：资料包QA（无字幕）.mp4").unlink()
            qa_product(project)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse(payload["passed"])
            self.assertTrue(any("背景视频+无字幕" in issue for issue in payload["issues"]))

    def test_rejects_internal_reports_inside_customer_packages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：报告泄漏"
            base, _advanced = make_package_fixture(project)
            (base / "成本报告.md").write_text("internal", encoding="utf-8")
            qa_product(project)
            payload = json.loads((project_paths(project).status / "qa_product_report.json").read_text(encoding="utf-8"))
            self.assertFalse(payload["passed"])
            self.assertTrue(any("混入内部报告" in issue for issue in payload["issues"]))


if __name__ == "__main__":
    unittest.main()
