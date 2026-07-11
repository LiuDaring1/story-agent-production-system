from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from story_agent import canonical_video_prompt_rows, video_prompt_review_matches_current
from story_project import final_delivery, init_project, load_manifest, project_paths, sha256_file, write_manifest
from tests.test_release_qa import make_vertical_video


class FullAutoContractTests(unittest.TestCase):
    def test_video_prompt_review_snapshot_survives_status_writeback_but_rejects_content_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = root / "jobs.csv"
            snapshot = root / "snapshot.json"
            decisions = root / "decisions.csv"
            jobs.write_text(
                "scene,image_filename,story_text,prompt,status\n1,01.png,小羊出发,小羊向前走,pending\n",
                encoding="utf-8-sig",
            )
            snapshot.write_text(
                json.dumps({"version": 1, "rows": canonical_video_prompt_rows(jobs)}, ensure_ascii=False),
                encoding="utf-8",
            )
            decisions.write_text(
                "scene,image_filename,story_text,review_status,prompt,notes\n01,01.png,小羊出发,approved,小羊自然地向前走,动作明确\n",
                encoding="utf-8-sig",
            )
            self.assertTrue(video_prompt_review_matches_current(jobs, snapshot, decisions))

            jobs.write_text(
                "scene,image_filename,story_text,prompt,status,prompt_review_status\n1,01.png,小羊出发,小羊自然地向前走,downloaded,approved\n",
                encoding="utf-8-sig",
            )
            self.assertTrue(video_prompt_review_matches_current(jobs, snapshot, decisions))

            jobs.write_text(
                "scene,image_filename,story_text,prompt,status\n1,01.png,小羊回家,小羊自然地向前走,downloaded\n",
                encoding="utf-8-sig",
            )
            self.assertFalse(video_prompt_review_matches_current(jobs, snapshot, decisions))

    def test_outputs_without_independent_reviews_cannot_complete(self) -> None:
        fixture_video = Path(__file__).resolve().parents[1] / "tools" / "video-subtitle-remover" / "test" / "test2.mp4"
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：模拟供应商闭环"
            manifest = init_project(project, story_name="模拟供应商闭环", slug="mock-e2e")
            paths = project_paths(project)
            manifest["agent"]["job_id"] = "mock-e2e-job"

            for target in (
                paths.assembly / "story_no_subs_bgm.mp4",
                paths.assembly / "story_sales_subs_bgm.mp4",
            ):
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fixture_video, target)
            make_vertical_video(paths.release / "主账号发布视频.mp4")
            make_vertical_video(paths.release / "宝库号发布视频.mp4")

            for account in ("main", "library"):
                copy_path = paths.publish / account / "copy.md"
                copy_path.parent.mkdir(parents=True, exist_ok=True)
                copy_path.write_text("# 标题\n\n正文\n\n#故事 #儿童表演\n", encoding="utf-8")
                for ratio, size in (("3x4", (900, 1200)), ("4x3", (1200, 900)), ("16x9", (1280, 720))):
                    cover = paths.publish / account / "covers" / f"cover_{ratio}.png"
                    cover.parent.mkdir(parents=True, exist_ok=True)
                    Image.new("RGB", size, (80, 120, 180)).save(cover)

            base = paths.product / "绵羊故事锦囊：模拟供应商闭环（基础版）"
            advanced = paths.product / "绵羊故事锦囊：模拟供应商闭环（进阶版）"
            for folder in (base, advanced):
                folder.mkdir(parents=True, exist_ok=True)
            for name in ("故事文稿：模拟.docx", "故事配乐：模拟.mp3", "朗读标注：模拟.docx", "示范表演：模拟.mp4", "背景图片：模拟.png"):
                (base / name).write_bytes(b"mock")
            for source in base.iterdir():
                shutil.copy2(source, advanced / source.name)
            for name in ("背景视频：模拟（含字幕）.mp4", "背景视频：模拟（无字幕）.mp4", "故事PPT：模拟（含字幕）.pptx", "故事PPT：模拟（无字幕）.pptx"):
                (advanced / name).write_bytes(b"mock")

            decisions = paths.status / "source_edit" / "edit_decisions.json"
            decisions.parent.mkdir(parents=True, exist_ok=True)
            decisions.write_text("{}", encoding="utf-8")
            manifest["outputs"].update(
                {
                    "main_release_video": str(paths.release / "主账号发布视频.mp4"),
                    "library_release_video": str(paths.release / "宝库号发布视频.mp4"),
                    "background_video_no_sub": str(paths.assembly / "story_no_subs_bgm.mp4"),
                    "background_video_with_sub": str(paths.assembly / "story_sales_subs_bgm.mp4"),
                    "publish_package": str(paths.publish),
                    "product_base": str(base),
                    "product_advanced": str(advanced),
                    "source_edit_decisions": str(decisions),
                }
            )
            write_manifest(paths, manifest)

            first_report = final_delivery(project)
            incomplete = load_manifest(paths)
            assert incomplete is not None
            self.assertNotIn("completed_at", incomplete)
            self.assertIn("审核失败", first_report.read_text(encoding="utf-8"))

            review_paths = [
                paths.status / "source_edit" / "source_edit_review.json",
                *[
                    paths.status / "reviews" / f"{name}.json"
                    for name in (
                        "story_images_review",
                        "video_prompt_review",
                        "video_review",
                        "release_preview_review",
                        "release_video_review",
                        "publish_package_review",
                        "product_annotation_review",
                        "product_package_review",
                    )
                ],
            ]
            for review in review_paths:
                review.parent.mkdir(parents=True, exist_ok=True)
                review.write_text(json.dumps({"approved": True, "score": 92, "critical_errors": []}), encoding="utf-8")
            music_artifact = next(base.glob("故事配乐*"))
            music_qa = paths.status / "qa_music_report.json"
            music_qa.write_text(
                json.dumps(
                    {
                        "passed": True,
                        "artifacts": {"music": {"path": str(music_artifact), "sha256": sha256_file(music_artifact)}},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            release_qa = paths.status / "qa_release_report.json"
            release_qa.write_text(
                json.dumps(
                    {
                        "passed": True,
                        "artifacts": {
                            key: {"path": str(path), "sha256": sha256_file(path)}
                            for key, path in {
                                "main": paths.release / "主账号发布视频.mp4",
                                "library": paths.release / "宝库号发布视频.mp4",
                            }.items()
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            second_report = final_delivery(project)
            complete = load_manifest(paths)
            assert complete is not None
            self.assertTrue(complete.get("completed_at"), second_report.read_text(encoding="utf-8"))
            self.assertIn("必备交付物和独立审核均已满足", second_report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
