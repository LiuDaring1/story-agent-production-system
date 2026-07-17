from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from story_agent import main as story_agent_main
from story_agent_runtime import JobRegistry, ensure_manifest_v2, file_sha256, write_review_bundle
from story_project import init_project, load_manifest, project_paths, save_json, write_manifest
from story_qualification import (
    REQUIRED_REVIEW_FILES,
    build_promotion_report,
    evaluate_project_for_promotion,
    record_human_signoff,
    record_unattended_launch,
)


class StoryQualificationTests(unittest.TestCase):
    def make_completed_project(self, root: Path) -> Path:
        project = root / "故事剪辑：资格测试"
        paths = project_paths(project)
        manifest = init_project(project, story_name="资格测试", slug="qualification-test")
        outputs = manifest["outputs"]
        paths.release.mkdir(parents=True, exist_ok=True)
        for name, key in (("主账号发布视频.mp4", "main_release_video"), ("宝库号发布视频.mp4", "library_release_video")):
            target = paths.release / name
            target.write_bytes(b"video")
            outputs[key] = str(target)
        for account in ("main", "library"):
            copy = paths.publish / account / "copy.md"
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_text("# 标题\n\n正文\n\n#话题\n", encoding="utf-8")
            for ratio in ("3x4", "4x3", "16x9"):
                cover = paths.publish / account / "covers" / f"cover_{ratio}.png"
                cover.parent.mkdir(parents=True, exist_ok=True)
                cover.write_bytes(f"{account}-{ratio}".encode())
        outputs["publish_package"] = str(paths.publish)
        for suffix, key in (("基础版", "product_base"), ("进阶版", "product_advanced")):
            package = paths.product / f"绵羊故事锦囊：资格测试（{suffix}）"
            package.mkdir(parents=True, exist_ok=True)
            (package / "fixture.txt").write_text(suffix, encoding="utf-8")
            outputs[key] = str(package)
        checklist = paths.root / "总交付清单.md"
        checklist.write_text("complete", encoding="utf-8")
        outputs["final_delivery_checklist"] = str(checklist)
        decisions = paths.status / "source_edit" / "edit_decisions.json"
        decisions.parent.mkdir(parents=True, exist_ok=True)
        decisions.write_text("{}", encoding="utf-8")
        outputs["source_edit_decisions"] = str(decisions)
        source_video = paths.inputs / "source.mp4"
        clean_video = paths.inputs / "clean.mp4"
        clean_audio = paths.inputs / "clean.m4a"
        story_text = paths.inputs / "story.txt"
        source_video.write_bytes(b"original-green-video")
        clean_video.write_bytes(b"derived-clean-video")
        clean_audio.write_bytes(b"derived-clean-audio")
        story_text.write_text("从前有一个资格测试故事。\n", encoding="utf-8")
        inputs = manifest["inputs"]
        inputs["greenscreen_video_original"] = str(source_video)
        inputs["greenscreen_video"] = str(clean_video)
        inputs["extracted_narration"] = str(clean_audio)
        inputs["narration"] = str(clean_audio)
        inputs["story_text"] = str(story_text)
        source_sha256 = file_sha256(source_video)
        decisions_sha256 = file_sha256(decisions)
        manifest["completed_at"] = "2026-07-13 22:00:00"
        manifest["agent"].update(
            {
                "status": "completed",
                "job_id": "qualification-job",
                "last_checkpoint": "doctor",
                "active_elapsed_seconds": 9 * 3600,
                "source": {"sha256": source_sha256, "project_copy": str(source_video)},
                "budget": {"spent": 8.4, "reserved": 0.0, "entries": []},
                "input_contract": {
                    "version": 1,
                    "mode": "single_greenscreen",
                    "created_at": "2026-07-13 11:59:00",
                    "user_inputs": [
                        {
                            "role": "greenscreen_video",
                            "path": str(source_video),
                            "sha256": source_sha256,
                            "bytes": source_video.stat().st_size,
                        }
                    ],
                    "processing_assets": [],
                    "derived_inputs": {
                        role: {
                            "path": str(path),
                            "sha256": file_sha256(path),
                            "bytes": path.stat().st_size,
                            "producer": "source_video_pipeline",
                            "source_sha256": source_sha256,
                            "decisions_sha256": decisions_sha256,
                        }
                        for role, path in {
                            "story_text": story_text,
                            "clean_greenscreen_video": clean_video,
                            "clean_narration": clean_audio,
                        }.items()
                    },
                },
            }
        )
        ensure_manifest_v2(manifest)
        for stage in manifest["agent"]["stages"].values():
            stage["status"] = "passed"
        write_manifest(paths, manifest)
        supervisor_log = paths.status / "story_agent_supervisor.log"
        supervisor_log.write_text("started\nDONE: 故事生产 Agent 已完成全部阶段。\n", encoding="utf-8")
        supervisor_record = paths.status / "story_agent_supervisor.json"
        save_json(
            supervisor_record,
            {
                "kind": "story_agent_start_v1",
                "job_id": "qualification-job",
                "project_dir": str(project.resolve()),
                "pid": 12345,
                "started_at": "2026-07-13 12:00:00",
                "log": str(supervisor_log.resolve()),
                "command": [
                    "/usr/bin/python3",
                    "/repo/story_agent.py",
                    "run",
                    "--job",
                    "qualification-job",
                    "--execute",
                    "--codex-mode",
                    "cli",
                ],
            },
        )
        record_unattended_launch(project, supervisor_record=supervisor_record)

        source_review = paths.status / "source_edit" / "source_edit_review.json"
        source_review.write_text(
            json.dumps({"approved": True, "score": 90, "critical_errors": [], "artifact_sha256": file_sha256(decisions)}),
            encoding="utf-8",
        )
        review_dir = paths.status / "reviews"
        review_dir.mkdir(parents=True, exist_ok=True)
        artifact = paths.release / "主账号发布视频.mp4"
        for name, (review_relative, bundle_relative) in REQUIRED_REVIEW_FILES.items():
            if bundle_relative is None:
                continue
            bundle = write_review_bundle(paths.status / bundle_relative, [artifact])
            review = paths.status / review_relative
            review.write_text(
                json.dumps({"approved": True, "score": 92, "critical_errors": [], "artifact_sha256": file_sha256(bundle)}),
                encoding="utf-8",
            )
        return project

    def test_promotion_requires_human_signoff_and_current_delivery_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self.make_completed_project(root)
            before = evaluate_project_for_promotion(project)
            self.assertFalse(before["qualified"])
            self.assertIn("缺少用户人工终审记录", before["reasons"])

            record_human_signoff(project, result="pass", minutes=8.5, notes="无需修改")
            after = evaluate_project_for_promotion(project)
            self.assertTrue(after["qualified"], after["reasons"])
            report = build_promotion_report(root)
            self.assertEqual(report["qualified_distinct_stories"], 1)
            self.assertFalse(report["ready_for_default_entry"])

            cover = project_paths(project).publish / "main" / "covers" / "cover_3x4.png"
            cover.write_bytes(b"changed")
            stale = evaluate_project_for_promotion(project)
            self.assertFalse(stale["qualified"])
            self.assertIn("用户人工终审绑定的交付物已变化", stale["reasons"])

    def test_prepared_entry_can_be_production_valid_but_never_counts_for_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_completed_project(Path(directory))
            paths = project_paths(project)
            manifest = load_manifest(paths)
            assert manifest is not None
            prepared_video = Path(manifest["inputs"]["greenscreen_video"])
            story_text = Path(manifest["inputs"]["story_text"])
            confirmed = paths.inputs / "confirmed.txt"
            confirmed.write_text(story_text.read_text(encoding="utf-8"), encoding="utf-8")
            manifest["inputs"]["greenscreen_video_original"] = str(prepared_video)
            manifest["agent"]["input_contract"] = {
                "version": 1,
                "mode": "prepared_greenscreen_confirmed_text",
                "track": "assisted_accelerated",
                "counts_toward_default_entry": False,
                "user_inputs": [
                    {
                        "role": "prepared_greenscreen_video",
                        "path": str(prepared_video),
                        "sha256": file_sha256(prepared_video),
                        "bytes": prepared_video.stat().st_size,
                    },
                    {
                        "role": "confirmed_story_text",
                        "path": str(confirmed),
                        "sha256": file_sha256(confirmed),
                        "bytes": confirmed.stat().st_size,
                    },
                ],
                "derived_inputs": {
                    "story_text": {
                        "path": str(story_text),
                        "sha256": file_sha256(story_text),
                        "bytes": story_text.stat().st_size,
                    }
                },
            }
            write_manifest(paths, manifest)
            record_human_signoff(project, result="pass", minutes=5)
            with patch("story_agent_runtime.probe_source_video", return_value={"width": 1920, "height": 1080, "has_audio": True}):
                result = evaluate_project_for_promotion(project)
            self.assertTrue(result["production_valid"], result["production_reasons"])
            self.assertFalse(result["default_entry_eligible"])
            self.assertFalse(result["qualified"])
            self.assertTrue(any("不计入" in reason for reason in result["reasons"]))

    def test_promotion_rejects_non_unattended_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_completed_project(Path(directory))
            paths = project_paths(project)
            manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
            manifest["agent"].pop("unattended_mode", None)
            manifest["agent"].pop("unattended_started_at", None)
            write_manifest(paths, manifest)
            record_human_signoff(project, result="pass", minutes=5)
            result = evaluate_project_for_promotion(project)
            self.assertFalse(result["qualified"])
            self.assertIn("缺少由 start 后台启动的无人值守运行证据", result["reasons"])

    def test_record_unattended_launch_persists_hashed_supervisor_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_completed_project(Path(directory))
            paths = project_paths(project)
            manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
            manifest["agent"].pop("unattended_mode", None)
            manifest["agent"].pop("unattended_started_at", None)
            write_manifest(paths, manifest)

            record = paths.status / "story_agent_supervisor.json"
            marked = record_unattended_launch(project, supervisor_record=record)

            self.assertTrue(marked["agent"]["unattended_mode"])
            self.assertEqual(marked["agent"]["unattended_started_at"], "2026-07-13 12:00:00")
            self.assertEqual(marked["agent"]["unattended_supervisor_record"], str(record.resolve()))
            self.assertEqual(marked["agent"]["unattended_supervisor_record_sha256"], file_sha256(record))

    def test_tampered_supervisor_record_revokes_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_completed_project(Path(directory))
            record_human_signoff(project, result="pass", minutes=4)
            record = project_paths(project).status / "story_agent_supervisor.json"
            payload = json.loads(record.read_text(encoding="utf-8"))
            payload["command"][payload["command"].index("--codex-mode") + 1] = "handoff"
            save_json(record, payload)

            result = evaluate_project_for_promotion(project)
            self.assertFalse(result["qualified"])
            self.assertTrue(any("启动证明无效" in reason for reason in result["reasons"]))

    def test_start_process_creation_failure_leaves_no_unattended_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：启动失败"
            manifest = init_project(project, story_name="启动失败", slug="start-failure")
            manifest["agent"]["job_id"] = "start-failure-job"
            write_manifest(project_paths(project), manifest)
            registry_path = root / "jobs.json"
            JobRegistry(registry_path).register("start-failure-job", project, "b" * 64)

            argv = [
                "story_agent.py",
                "start",
                "--job",
                "start-failure-job",
                "--registry",
                str(registry_path),
            ]
            with patch.object(sys, "argv", argv), patch("story_agent.subprocess.Popen", side_effect=OSError("spawn failed")):
                with self.assertRaisesRegex(OSError, "spawn failed"):
                    story_agent_main()

            reloaded = json.loads(project_paths(project).manifest.read_text(encoding="utf-8"))
            self.assertFalse(reloaded["agent"].get("unattended_mode", False))
            self.assertFalse((project_paths(project).status / "story_agent_supervisor.json").exists())

    def test_successful_start_writes_verifiable_supervisor_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：成功启动"
            manifest = init_project(project, story_name="成功启动", slug="start-success")
            manifest["agent"]["job_id"] = "start-success-job"
            write_manifest(project_paths(project), manifest)
            registry_path = root / "jobs.json"
            JobRegistry(registry_path).register("start-success-job", project, "c" * 64)
            argv = [
                "story_agent.py",
                "start",
                "--job",
                "start-success-job",
                "--registry",
                str(registry_path),
            ]
            with patch.object(sys, "argv", argv), patch(
                "story_agent.subprocess.Popen",
                return_value=SimpleNamespace(pid=4242),
            ):
                story_agent_main()

            paths = project_paths(project)
            record_path = paths.status / "story_agent_supervisor.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            reloaded = json.loads(paths.manifest.read_text(encoding="utf-8"))
            self.assertEqual(record["kind"], "story_agent_start_v1")
            self.assertEqual(record["pid"], 4242)
            self.assertTrue(reloaded["agent"]["unattended_mode"])
            self.assertEqual(reloaded["agent"]["unattended_supervisor_record_sha256"], file_sha256(record_path))

    def test_non_finite_review_score_and_signoff_minutes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_completed_project(Path(directory))
            paths = project_paths(project)
            review = paths.status / "source_edit" / "source_edit_review.json"
            payload = json.loads(review.read_text(encoding="utf-8"))
            payload["score"] = float("nan")
            review.write_text(json.dumps(payload), encoding="utf-8")

            result = evaluate_project_for_promotion(project)
            self.assertIn("独立审核未达标：source_edit_review", result["reasons"])
            with self.assertRaisesRegex(ValueError, "有限的非负数"):
                record_human_signoff(project, result="pass", minutes=float("nan"))

    def test_missing_supervisor_completion_marker_revokes_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_completed_project(Path(directory))
            record_human_signoff(project, result="pass", minutes=3)
            log = project_paths(project).status / "story_agent_supervisor.log"
            log.write_text("NEXT: final_delivery\n", encoding="utf-8")

            result = evaluate_project_for_promotion(project)
            self.assertFalse(result["qualified"])
            self.assertTrue(any("没有 Agent 完成标记" in reason for reason in result["reasons"]))

    def test_non_finite_cost_and_runtime_revoke_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_completed_project(Path(directory))
            paths = project_paths(project)
            record_human_signoff(project, result="pass", minutes=3)
            manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
            manifest["agent"]["budget"]["spent"] = float("nan")
            manifest["agent"]["active_elapsed_seconds"] = float("inf")
            write_manifest(paths, manifest)

            result = evaluate_project_for_promotion(project)
            self.assertFalse(result["qualified"])
            self.assertIn("单集成本记录无效", result["reasons"])
            self.assertIn("有效运行时间记录无效", result["reasons"])
            self.assertIsNone(result["cost_cny"])
            self.assertIsNone(result["active_hours"])

    def test_manual_content_inputs_revoke_single_greenscreen_qualification(self) -> None:
        mutations = {
            "story_text": ("manual_story.txt", b"manual story"),
            "narration": ("manual_narration.m4a", b"manual narration"),
            "music": ("manual_music.mp3", b"manual music"),
            "story_images": ("manual_images.zip", b"manual images"),
        }
        for field, (filename, content) in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                project = self.make_completed_project(Path(directory))
                paths = project_paths(project)
                record_human_signoff(project, result="pass", minutes=3)
                manual = paths.inputs / filename
                manual.write_bytes(content)
                manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
                manifest["inputs"][field] = str(manual)
                write_manifest(paths, manifest)

                result = evaluate_project_for_promotion(project)
                self.assertFalse(result["qualified"])
                self.assertTrue(any("单绿幕输入证明无效" in reason for reason in result["reasons"]))

    def test_tampered_derived_input_revokes_single_greenscreen_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_completed_project(Path(directory))
            paths = project_paths(project)
            record_human_signoff(project, result="pass", minutes=3)
            story_text = Path(json.loads(paths.manifest.read_text(encoding="utf-8"))["inputs"]["story_text"])
            story_text.write_text("手工替换后的故事。\n", encoding="utf-8")

            result = evaluate_project_for_promotion(project)
            self.assertFalse(result["qualified"])
            self.assertTrue(
                any("story_text" in reason and "已变化" in reason for reason in result["reasons"]),
                result["reasons"],
            )

    def test_pre_generated_review_artifact_before_start_revokes_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_completed_project(Path(directory))
            paths = project_paths(project)
            record_human_signoff(project, result="pass", minutes=3)
            manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
            start_epoch = time.mktime(
                time.strptime(manifest["agent"]["unattended_started_at"], "%Y-%m-%d %H:%M:%S")
            )
            generated_video = Path(manifest["outputs"]["main_release_video"])
            os.utime(generated_video, (start_epoch - 60, start_epoch - 60))

            result = evaluate_project_for_promotion(project)
            self.assertFalse(result["qualified"])
            self.assertTrue(
                any("早于无人值守启动" in reason or "启动前预置" in reason for reason in result["reasons"]),
                result["reasons"],
            )

    def test_incomplete_stage_or_checkpoint_revokes_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_completed_project(Path(directory))
            paths = project_paths(project)
            record_human_signoff(project, result="pass", minutes=3)
            manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
            manifest["agent"]["stages"]["video_qa"]["status"] = "pending"
            manifest["agent"]["last_checkpoint"] = "generate_videos"
            write_manifest(paths, manifest)

            result = evaluate_project_for_promotion(project)
            self.assertFalse(result["qualified"])
            self.assertTrue(any("video_qa" in reason for reason in result["reasons"]))
            self.assertIn("最后成功检查点不是 doctor", result["reasons"])


if __name__ == "__main__":
    unittest.main()
