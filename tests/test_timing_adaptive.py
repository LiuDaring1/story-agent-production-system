from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import story_workflow
import apply_narration_durations
from apply_narration_durations import generation_duration_for_target
from story_agent import AgentContext, StoryAgent
from story_project import init_project, project_paths, write_manifest
from story_video_synthesizer.align import LineTiming


class AdaptiveTimingTests(unittest.TestCase):
    def test_adaptive_seconds_rounds_up_and_clamps_to_provider_bounds(self) -> None:
        kwargs = {
            "duration_mode": "adaptive-seconds",
            "fixed_duration": 10,
            "min_generation_seconds": 1,
            "max_generation_seconds": 15,
        }
        self.assertEqual(generation_duration_for_target(9.83, **kwargs), 10)
        self.assertEqual(generation_duration_for_target(8.09, **kwargs), 9)
        self.assertEqual(generation_duration_for_target(4.17, **kwargs), 5)
        self.assertEqual(generation_duration_for_target(0.01, **kwargs), 1)
        self.assertEqual(generation_duration_for_target(99.0, **kwargs), 15)

    def test_fixed_mode_keeps_existing_generation_duration(self) -> None:
        self.assertEqual(
            generation_duration_for_target(
                4.17,
                duration_mode="fixed",
                fixed_duration=10,
                min_generation_seconds=1,
                max_generation_seconds=15,
            ),
            10.0,
        )

    def test_adaptive_rows_store_truthful_seconds_and_compatibility_frames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = root / "jobs.csv"
            jobs.write_text(
                "scene,image_filename,story_text,prompt,target_video_filename,status\n"
                "1,01.png,甲,甲,01.mp4,todo\n"
                "2,02.png,乙,乙,02.mp4,todo\n"
                "3,03.png,丙,丙,03.mp4,todo\n",
                encoding="utf-8-sig",
            )
            narration = root / "narration.m4a"
            narration.write_bytes(b"audio")
            timings = [
                LineTiming(1, "甲", 0.0, 9.68, 9.68, 0.0, 9.68),
                LineTiming(2, "乙", 9.68, 17.62, 7.94, 9.68, 17.62),
                LineTiming(3, "丙", 17.62, 21.64, 4.02, 17.62, 21.64),
            ]
            argv = [
                "apply_narration_durations.py",
                "--jobs-csv",
                str(jobs),
                "--narration",
                str(narration),
                "--alignment-mode",
                "even",
                "--duration-mode",
                "adaptive-seconds",
                "--min-generation-seconds",
                "1",
                "--max-generation-seconds",
                "15",
            ]
            with patch.object(sys, "argv", argv), patch.object(
                apply_narration_durations, "align_script_to_narration", return_value=timings
            ):
                apply_narration_durations.main()
            with jobs.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual([row["generation_duration"] for row in rows], ["10", "9", "5"])
            self.assertEqual([row["duration"] for row in rows], ["10", "9", "5"])
            self.assertEqual([row["effective_duration"] for row in rows], ["10.000", "9.000", "5.000"])
            self.assertEqual([row["frames"] for row in rows], ["240", "216", "120"])
            self.assertTrue(all(row["duration_mode"] == "adaptive-seconds" for row in rows))

    def test_workflow_forwards_adaptive_mode_and_bounds(self) -> None:
        argv = [
            "story_workflow.py",
            "timing",
            "--jobs-csv",
            "jobs.csv",
            "--narration",
            "narration.m4a",
            "--duration-mode",
            "adaptive-seconds",
            "--min-generation-seconds",
            "1",
            "--max-generation-seconds",
            "15",
        ]
        with patch.object(sys, "argv", argv), patch.object(story_workflow, "run_script") as runner:
            story_workflow.main()
        command = list(runner.call_args.args)
        self.assertEqual(command[0], "apply_narration_durations.py")
        self.assertIn("--duration-mode", command)
        self.assertEqual(command[command.index("--duration-mode") + 1], "adaptive-seconds")
        self.assertEqual(command[command.index("--min-generation-seconds") + 1], "1.0")
        self.assertEqual(command[command.index("--max-generation-seconds") + 1], "15.0")

    def test_has_timing_rejects_prepare_placeholder_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：时长闸门"
            manifest = init_project(project, story_name="时长闸门", slug="timing-gate")
            jobs = project_paths(project).video_jobs / "timing-gate_image_video_jobs.csv"
            jobs.parent.mkdir(parents=True, exist_ok=True)
            jobs.write_text(
                "scene,target_video_filename,duration,frames,status\n1,01.mp4,10,240,todo\n",
                encoding="utf-8-sig",
            )
            manifest["outputs"]["jobs_csv"] = str(jobs)
            write_manifest(project_paths(project), manifest)
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="时长闸门",
                slug="timing-gate",
                execute=False,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="read-only",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context, read_only=True)
            self.assertFalse(agent._has_timing(manifest))

    def test_has_timing_accepts_real_row_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：时长元数据"
            manifest = init_project(project, story_name="时长元数据", slug="timing-metadata")
            jobs = project_paths(project).video_jobs / "timing-metadata_image_video_jobs.csv"
            jobs.parent.mkdir(parents=True, exist_ok=True)
            jobs.write_text(
                "scene,target_video_filename,duration,frames,status,target_duration,narration_start,narration_end,generation_duration,duration_mode\n"
                "1,01.mp4,5,120,todo,4.17,0.000,4.020,5,adaptive-seconds\n",
                encoding="utf-8-sig",
            )
            manifest["outputs"]["jobs_csv"] = str(jobs)
            write_manifest(project_paths(project), manifest)
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="时长元数据",
                slug="timing-metadata",
                execute=False,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="read-only",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            self.assertTrue(StoryAgent(context, read_only=True)._has_timing(manifest))

    def test_stage_timing_selects_adaptive_for_current_grok_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：Grok 时长"
            manifest = init_project(project, story_name="Grok 时长", slug="grok-timing")
            paths = project_paths(project)
            narration = paths.inputs / "narration.m4a"
            narration.write_bytes(b"audio")
            jobs = paths.video_jobs / "grok-timing_image_video_jobs.csv"
            jobs.parent.mkdir(parents=True, exist_ok=True)
            jobs.write_text("scene,target_video_filename,status\n1,01.mp4,todo\n", encoding="utf-8-sig")
            manifest["inputs"]["narration"] = str(narration)
            manifest["outputs"]["jobs_csv"] = str(jobs)
            write_manifest(paths, manifest)
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="Grok 时长",
                slug="grok-timing",
                execute=False,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="read-only",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context, read_only=True)
            config = {
                "video_api": {
                    "provider": "configured_provider",
                    "adapters": {
                        "configured_provider": {
                            "runner": "run_image_video_jobs.py",
                            "model": "grok-video-1.5",
                            "min_seconds": 1,
                            "max_seconds": 15,
                        }
                    },
                }
            }
            with patch("story_agent.load_config", return_value=config), patch.object(
                agent, "_workflow", return_value=type("Result", (), {"status": "done", "message": "ok", "handoff": None})()
            ) as workflow:
                agent._stage_timing(manifest)
            command = list(workflow.call_args.args[0])
            self.assertIn("--duration-mode", command)
            self.assertEqual(command[command.index("--duration-mode") + 1], "adaptive-seconds")
            self.assertEqual(command[command.index("--min-generation-seconds") + 1], "1")
            self.assertEqual(command[command.index("--max-generation-seconds") + 1], "15")


if __name__ == "__main__":
    unittest.main()
