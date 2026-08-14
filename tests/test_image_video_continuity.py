from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from story_agent import AgentContext, StoryAgent
from story_project import init_project, project_paths
from story_workflow import reset_redo_scenes
from story_video_synthesizer.image_video import (
    VisualContinuityContractError,
    build_jobs,
    continuity_context_from_row,
    inject_visual_continuity_prompt,
    validate_image_video_jobs,
    write_job_outputs,
)


class ImageVideoContinuityTests(unittest.TestCase):
    def _fixture(self, root: Path, *, required: str = "保持当前状态", state: str = "state_a") -> tuple[Path, Path, Path, Path]:
        image_dir = root / "images"
        image_dir.mkdir(parents=True)
        Image.new("RGB", (24, 24), "#d4b16a").save(image_dir / "demo_scene_01.png")
        storyboard = root / "storyboard.txt"
        storyboard.write_text("1.故事内容：主体发生一个动作。画面描述：主体位于场景中。\n", encoding="utf-8")
        contract = root / "visual_continuity_contract.json"
        contract.write_text(
            json.dumps(
                {
                    "allowed_states": [state],
                    "storyboard_requirements": {"required_field": "character_state"},
                    "state_rules": {state: {"required": [required], "forbidden": ["禁止状态"]}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        plan = root / "demo_storyboard_plan.json"
        plan.write_text(json.dumps({"shots": [{
            "scene": 1,
            "character_state": state,
            "visible_characters": ["character_a"],
            "scale_basis": {"applicable": False, "reason": "single character"},
            "current_story_state": {"state_machine": state},
            "visual_state_evidence": {"state_machine": "visible"},
            "subject_action": "character_a performs the story action",
            "environment_motion": "environment moves gently",
            "camera_motion": "stable natural follow",
            "entry_state": {"story_state": {"state_machine": state}},
            "exit_state": {"story_state": {"state_machine": state}},
            "screen_direction": "left_to_right",
            "adjacent_handoff": {"from_previous": "", "to_next": "", "allows_direction_change": False, "allows_state_transition": False},
            "expected_motion": {
                "primary": "subject", "subject_level": "moderate",
                "environment_level": "low", "camera_level": "low", "rationale": "story action",
            },
        }]}, ensure_ascii=False), encoding="utf-8")
        return image_dir, storyboard, contract, plan

    def test_contract_required_and_forbidden_are_in_final_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_dir, storyboard, contract, plan = self._fixture(Path(directory))
            jobs, _warnings = build_jobs(image_dir, storyboard, Path(directory) / "jobs", "demo", "demo", contract, plan)
            prompt = jobs[0].prompt
            self.assertIn('"current_state":"state_a"', prompt)
            self.assertIn("保持当前状态", prompt)
            self.assertIn("禁止状态", prompt)
            self.assertIn("整个视频片段必须始终保持 current_state=state_a", prompt)

    def test_story_production_contract_binding_is_written_to_every_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_dir, storyboard, continuity, plan = self._fixture(root)
            context = root / "image_video.json"
            context.write_text(
                json.dumps(
                    {
                        "consumer": "image_video",
                        "contract_schema_version": "1.0.0",
                        "story_contract_sha256": "a" * 64,
                        "story_contract_dependency_sha256": "b" * 64,
                        "contract_projection_sha256": "c" * 64,
                        "contract_projection": {
                            "characters": {"mode": "character_driven", "items": [{"character_id": "hero"}]},
                            "story_state": {"states": [{"state_id": "state_a"}]},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            jobs, _warnings = build_jobs(
                image_dir,
                storyboard,
                root / "jobs",
                "demo",
                "demo",
                continuity,
                plan,
                context,
            )
            outputs = write_job_outputs(jobs, root / "jobs", "demo")
            with outputs["manifest_csv"].open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["contract_schema_version"], "1.0.0")
            self.assertEqual(row["story_contract_sha256"], "a" * 64)
            self.assertEqual(row["story_contract_dependency_sha256"], "b" * 64)
            self.assertEqual(row["subject_action"], "character_a performs the story action")
            self.assertEqual(json.loads(row["expected_motion"])["primary"], "subject")
            self.assertEqual(json.loads(row["entry_state"])["story_state"]["state_machine"], "state_a")
            self.assertIn("STORY_CONTRACT_V1", row["prompt"])
            self.assertIn('"character_id":"hero"', row["prompt"])
            self.assertEqual(validate_image_video_jobs(outputs["manifest_csv"]), [])
            payload = json.loads(plan.read_text(encoding="utf-8"))
            payload["diagnostic_note"] = "storyboard changed after motion-plan compilation"
            plan.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self.assertIn("当前 storyboard plan 已改变", "；".join(validate_image_video_jobs(outputs["manifest_csv"])))

    def test_old_review_csv_cannot_erase_contract_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_dir, storyboard, contract, plan = self._fixture(root)
            output_dir = root / "jobs"
            jobs, _warnings = build_jobs(image_dir, storyboard, output_dir, "demo", "demo", contract, plan)
            write_job_outputs(jobs, output_dir, "demo")
            (output_dir / "prompt_review_decisions.csv").write_text(
                "scene,image_filename,story_text,review_status,prompt,notes\n"
                "01,demo_scene_01.png,主体发生一个动作。,approved,擦掉全部硬约束,旧审核\n",
                encoding="utf-8",
            )
            write_job_outputs(jobs, output_dir, "demo")
            with (output_dir / "demo_image_video_jobs.csv").open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertIn("保持当前状态", row["prompt"])
            self.assertIn("禁止状态", row["prompt"])

    def test_prepare_refresh_preserves_download_and_timing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_dir, storyboard, contract, plan = self._fixture(root)
            output_dir = root / "jobs"
            jobs, _warnings = build_jobs(image_dir, storyboard, output_dir, "demo", "demo", contract, plan)
            outputs = write_job_outputs(jobs, output_dir, "demo")
            with outputs["manifest_csv"].open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
                fieldnames = list(handle.seek(0) or []) if False else None
            rows[0].update(
                {
                    "status": "downloaded",
                    "task_id": "provider-task-keep",
                    "frames": "123",
                    "narration_duration": "7.5",
                    "generation_min_seconds": "1",
                }
            )
            with outputs["manifest_csv"].open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            jobs, _warnings = build_jobs(image_dir, storyboard, output_dir, "demo", "demo", contract, plan)
            write_job_outputs(jobs, output_dir, "demo")
            with outputs["manifest_csv"].open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["status"], "downloaded")
            self.assertEqual(row["task_id"], "provider-task-keep")
            self.assertEqual(row["frames"], "123")
            self.assertEqual(row["narration_duration"], "7.5")
            self.assertEqual(row["generation_min_seconds"], "1")

    def test_legacy_project_prompt_review_behavior_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_dir = root / "images"
            image_dir.mkdir()
            Image.new("RGB", (24, 24), "#d4b16a").save(image_dir / "demo_scene_01.png")
            storyboard = root / "storyboard.txt"
            storyboard.write_text("1.故事内容：旧项目。画面描述：旧画面。\n", encoding="utf-8")
            output_dir = root / "jobs"
            jobs, _warnings = build_jobs(image_dir, storyboard, output_dir, "demo", "demo")
            write_job_outputs(jobs, output_dir, "demo")
            (output_dir / "prompt_review_decisions.csv").write_text(
                "scene,image_filename,story_text,review_status,prompt,notes\n"
                "01,demo_scene_01.png,旧项目。,approved,审核后的旧提示词,\n",
                encoding="utf-8",
            )
            write_job_outputs(jobs, output_dir, "demo")
            with (output_dir / "demo_image_video_jobs.csv").open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["prompt"], "审核后的旧提示词")

    def test_invalid_state_blocks_job_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_dir, storyboard, contract, plan = self._fixture(root, state="not_allowed")
            contract.write_text(
                json.dumps(
                    {
                        "allowed_states": ["state_a"],
                        "storyboard_requirements": {"required_field": "character_state"},
                        "state_rules": {"state_a": {"required": [], "forbidden": []}},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaises(VisualContinuityContractError):
                build_jobs(image_dir, storyboard, root / "jobs", "demo", "demo", contract, plan)

    def test_staging_sync_copies_flow_prompt_control_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            init_project(project, story_name="同步测试", slug="sync-test")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="同步测试",
                slug="sync-test",
                execute=False,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context, read_only=True)
            staging = root / "staging"
            staging_images = staging / "images"
            staging_images.mkdir(parents=True)
            staging_storyboard = staging / "sync-test_storyboard_lines.txt"
            staging_storyboard.write_text("一行\n", encoding="utf-8")
            (staging / "sync-test_visual_bible.md").write_text("视觉圣经", encoding="utf-8")
            (staging / "sync-test_storyboard_plan.json").write_text(json.dumps({"shots": []}), encoding="utf-8")
            for name in ("sync-test_flow_video_prompts.csv", "sync-test_flow_video_prompts.md", "sync-test_flow_clip_names.csv"):
                (staging_images / name).write_text("control", encoding="utf-8")
            agent._sync_story_images_from_staging(staging, staging_storyboard, staging_images)
            for name in ("sync-test_flow_video_prompts.csv", "sync-test_flow_video_prompts.md", "sync-test_flow_clip_names.csv"):
                self.assertTrue((project_paths(project).images / name).is_file())

    def test_redo_reset_keeps_continuity_clause_at_end_of_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_dir, storyboard, contract, plan = self._fixture(root)
            output_dir = root / "jobs"
            jobs, _warnings = build_jobs(image_dir, storyboard, output_dir, "demo", "demo", contract, plan)
            outputs = write_job_outputs(jobs, output_dir, "demo")
            videos_dir = output_dir / "videos"
            videos_dir.mkdir(exist_ok=True)
            (videos_dir / "demo.mp4").write_bytes(b"old")
            decisions = root / "decisions.csv"
            decisions.write_text(
                "scene,review_status,notes\n"
                "01,redo,重新生成且保留连续性\n",
                encoding="utf-8-sig",
            )

            self.assertEqual(reset_redo_scenes(outputs["manifest_csv"], videos_dir, decisions), [1])
            with outputs["manifest_csv"].open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["status"], "todo")
            self.assertIn("重新生成且保留连续性", row["prompt"])
            self.assertIn('"current_state":"state_a"', row["prompt"])
            self.assertTrue(row["prompt"].endswith("禁止状态。"))

    def test_long_continuity_prompt_is_idempotent(self) -> None:
        row = {
            "scene": "08",
            "continuity_state": "tail_missing",
            "continuity_required": json.dumps(["无尾，或仅保留短小圆钝的断尾根"], ensure_ascii=False),
            "continuity_forbidden": json.dumps(["完整尾巴", "细长尾巴"], ensure_ascii=False),
        }
        base = (
            "参考当前图片，小壁虎沿老槐树树干向上爬两步后停住俯看，身体末端保持短小圆钝断尾根；"
            "雄性牛伯伯低头咀嚼青草，四条牛腿稳稳站住，再把从臀部连接的长牛尾向侧后方甩动。"
            "镜头从树干上方俯摇到牛和牛尾。保持牛尾连接牛身，小壁虎不出现完整尾巴、乳房或乳头。"
            "保持角色、服装、道具、场景和画风不变；不要新增字幕、文字、logo或水印。"
            " [审核备注] 视频生成时错误长出完整尾巴；必须保持短圆断尾根，禁止可见完整尾巴。"
        )
        first = inject_visual_continuity_prompt(base, continuity_context_from_row(row))
        second = inject_visual_continuity_prompt(first, continuity_context_from_row(row))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
