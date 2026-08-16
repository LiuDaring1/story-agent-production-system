from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from story_agent import canonical_video_prompt_rows, video_prompt_review_matches_current
from story_agent_runtime import STORY_STAGE_SEQUENCE, file_sha256, write_review_bundle
from story_project import final_delivery, init_project, load_manifest, project_paths, sha256_file, write_manifest
from tests.test_release_qa import make_vertical_video


class FullAutoContractTests(unittest.TestCase):
    def test_final_demo_is_reviewed_before_release_and_publish_claims(self) -> None:
        self.assertLess(STORY_STAGE_SEQUENCE.index("product_package_review"), STORY_STAGE_SEQUENCE.index("publish_package_review"))
        self.assertLess(STORY_STAGE_SEQUENCE.index("product_package_review"), STORY_STAGE_SEQUENCE.index("release_preview"))
        self.assertLess(STORY_STAGE_SEQUENCE.index("release_video_review"), STORY_STAGE_SEQUENCE.index("publish_package"))

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
                copy_path.write_text(
                    "# 发布标题\n\n这是一段完整的发布正文，介绍故事内容、观看亮点和适龄信息。\n\n#儿童故事 #亲子阅读 #故事表演\n",
                    encoding="utf-8",
                )
                palette = {
                    "main": {"3x4": (80, 120, 180), "4x3": (130, 90, 170), "16x9": (70, 155, 120)},
                    "library": {"3x4": (175, 110, 70), "4x3": (70, 135, 175), "16x9": (155, 75, 115)},
                }
                for ratio, size in (("3x4", (900, 1200)), ("4x3", (1200, 900)), ("16x9", (1280, 720))):
                    cover = paths.publish / account / "covers" / f"cover_{ratio}.png"
                    cover.parent.mkdir(parents=True, exist_ok=True)
                    Image.new("RGB", size, palette[account][ratio]).save(cover)

            base = paths.product / "绵羊故事锦囊：模拟供应商闭环（基础版）"
            advanced = paths.product / "绵羊故事锦囊：模拟供应商闭环（进阶版）"
            for folder in (base, advanced):
                folder.mkdir(parents=True, exist_ok=True)
            for name in ("故事文稿：模拟.docx", "故事配乐：模拟.mp3", "朗读标注：模拟.docx", "示范表演：模拟.mp4", "背景图片：模拟.png"):
                (base / name).write_bytes(b"mock")
            for source in base.iterdir():
                shutil.copy2(source, advanced / source.name)
            for name in ("背景视频：模拟（含字幕）.mp4", "背景视频：模拟（无字幕）.mp4", "故事PPT：模拟（含字幕）.pptx", "故事PPT：模拟（无字幕）.pptx", "A镜无人物背景视频：模拟.mp4"):
                (advanced / name).write_bytes(b"mock")

            decisions = paths.status / "source_edit" / "edit_decisions.json"
            decisions.parent.mkdir(parents=True, exist_ok=True)
            decisions.write_text("{}", encoding="utf-8")
            source_qa = paths.status / "qa_source_report.json"
            source_qa.write_text(
                json.dumps(
                    {
                        "passed": True,
                        "errors": [],
                        "artifacts": {
                            "edit_decisions": {
                                "path": str(decisions),
                                "sha256": sha256_file(decisions),
                                "bytes": decisions.stat().st_size,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
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

            source_review = paths.status / "source_edit" / "source_edit_review.json"
            source_review.write_text(
                json.dumps({"approved": True, "score": 92, "critical_errors": [], "artifact_sha256": file_sha256(decisions)}),
                encoding="utf-8",
            )
            review_dir = paths.status / "reviews"
            review_dir.mkdir(parents=True, exist_ok=True)
            bundles = {
                "story_images_review": ("story_images_bundle.json", [paths.publish / "main" / "covers" / "cover_3x4.png"]),
                "video_prompt_review": ("video_prompt_bundle.json", [decisions]),
                "video_review": ("video_bundle.json", [paths.release / "主账号发布视频.mp4"]),
                "release_preview_review": ("release_preview_bundle.json", [paths.release / "主账号发布视频.mp4"]),
                "release_video_review": (
                    "release_video_bundle.json",
                    [paths.release / "主账号发布视频.mp4", paths.release / "宝库号发布视频.mp4"],
                ),
                "publish_package_review": ("publish_package_bundle.json", [paths.publish]),
                "product_annotation_review": ("product_annotation_bundle.json", [next(base.glob("朗读标注*"))]),
                "product_package_review": ("product_package_bundle.json", [base, advanced]),
            }
            review_filenames = {
                "story_images_review": "story_images_review_review.json",
                "video_prompt_review": "video_prompt_review.json",
                "video_review": "video_review_review.json",
                "release_preview_review": "release_preview_review.json",
                "release_video_review": "release_video_review_review.json",
                "publish_package_review": "publish_package_review_review.json",
                "product_annotation_review": "product_annotation_review_review.json",
                "product_package_review": "product_package_review_review.json",
            }
            for review_name, (bundle_name, artifacts) in bundles.items():
                bundle = write_review_bundle(review_dir / bundle_name, artifacts)
                (review_dir / review_filenames[review_name]).write_text(
                    json.dumps({"approved": True, "score": 92, "critical_errors": [], "artifact_sha256": file_sha256(bundle)}),
                    encoding="utf-8",
                )
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

            bound_qa_files = [
                Path(value)
                for value in (load_manifest(paths) or {}).get("qa", {}).values()
                if value and Path(value).is_file()
            ]
            qa_hashes_before_finalization = {path: sha256_file(path) for path in bound_qa_files}
            second_report = final_delivery(project)
            complete = load_manifest(paths)
            assert complete is not None
            self.assertTrue(
                complete.get("completed_at"),
                second_report.read_text(encoding="utf-8")
                + "\nPUBLISH QA:\n"
                + (paths.status / "qa_publish_report.json").read_text(encoding="utf-8"),
            )
            self.assertIn("必备交付物和独立审核均已满足", second_report.read_text(encoding="utf-8"))
            self.assertEqual(
                qa_hashes_before_finalization,
                {path: sha256_file(path) for path in bound_qa_files},
                "Agent 最终交付不得重写已经被独立审核绑定的 QA 报告",
            )
            for name in ("成本报告.md", "QA汇总.md", "异常说明.md"):
                self.assertTrue((paths.status / name).exists())
                self.assertFalse((base / name).exists())
                self.assertFalse((advanced / name).exists())

            changed_cover = paths.publish / "main" / "covers" / "cover_3x4.png"
            Image.new("RGB", (900, 1200), (180, 80, 80)).save(changed_cover)
            stale_report = final_delivery(project)
            stale_manifest = load_manifest(paths)
            assert stale_manifest is not None
            self.assertNotIn("completed_at", stale_manifest)
            self.assertIn("bundle 中的产物已经变化", stale_report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
