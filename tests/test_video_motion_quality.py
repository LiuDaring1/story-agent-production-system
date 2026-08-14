from __future__ import annotations

import json
import csv
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from video_motion import (
    adjacent_handoff_issues,
    canonical_json_bytes,
    compile_motion_plan,
    formal_source_issues,
    inject_motion_prompt,
    motion_evidence_issues,
    motion_metrics,
    review_semantic_issues,
    schema_python_parity,
    video_receipt_issues,
    write_video_receipt,
)
from story_project import init_project, project_paths, qa_videos


def expected(primary: str, subject: str = "low", environment: str = "low", camera: str = "low") -> dict:
    return {
        "primary": primary,
        "subject_level": subject,
        "environment_level": environment,
        "camera_level": camera,
        "rationale": "generic scene performance intent",
        "subject_region": [0.2, 0.15, 0.45, 0.7],
    }


def shot(scene: int, *, direction: str = "left_to_right", primary: str = "subject") -> dict:
    state = {"state_machine": "state_1"}
    handoff = f"handoff_{scene}"
    return {
        "scene": scene,
        "story_text": f"line {scene}",
        "visible_characters": ["character_a"],
        "scale_basis": {"applicable": False, "reason": "single character"},
        "current_story_state": state,
        "visual_state_evidence": {"state_machine": "visible"},
        "subject_action": "character_a performs a clear story action",
        "environment_motion": "background elements move gently",
        "camera_motion": "stable natural follow",
        "entry_state": {"story_state": state},
        "exit_state": {"story_state": state},
        "screen_direction": direction,
        "adjacent_handoff": {
            "from_previous": f"handoff_{scene - 1}" if scene > 1 else "",
            "to_next": handoff,
            "allows_direction_change": False,
            "allows_state_transition": False,
        },
        "expected_motion": expected(primary, "moderate" if primary == "subject" else "low", "moderate" if primary == "environment" else "low", "moderate" if primary == "camera" else "low"),
    }


def storyboard() -> dict:
    return {
        "contract_schema_version": "1.0.0",
        "story_contract_sha256": "a" * 64,
        "story_contract_dependency_sha256": "b" * 64,
        "contract_projection_sha256": "c" * 64,
        "storyboard_sha256": "d" * 64,
        "shots": [shot(1), shot(2)],
    }


class VideoMotionQualityTests(unittest.TestCase):
    def frame_set(self, directory: Path, painter) -> list[Path]:
        result = []
        for index in range(5):
            image = Image.new("RGB", (320, 180), "#505050")
            painter(image, index)
            path = directory / f"frame_{index}.png"
            image.save(path)
            result.append(path)
        return result

    def test_motion_plan_is_deterministic_and_schema_matches_python(self) -> None:
        first = compile_motion_plan(storyboard())
        second = compile_motion_plan(storyboard())
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))
        self.assertEqual(schema_python_parity(), [])
        self.assertEqual(first["shots"][0]["continuity"]["characters"], ["character_a"])

    def test_prompt_assigns_motion_without_using_stillness_as_preservation(self) -> None:
        motion_shot = compile_motion_plan(storyboard())["shots"][0]
        prompt = inject_motion_prompt("Preserve identity and scale.", motion_shot)
        self.assertIn("[VIDEO_MOTION_PLAN_V1]", prompt)
        self.assertIn("不得用完全静止或几乎不动逃避动作", prompt)
        self.assertIn("subject_action", prompt)

    def test_completely_static_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            frames = self.frame_set(Path(directory), lambda image, index: None)
            metrics = motion_metrics(frames, expected("subject", "moderate"))
        self.assertIn("completely_static", motion_evidence_issues(metrics, expected("subject", "moderate")))

    def test_near_static_fails_when_subject_action_is_moderate(self) -> None:
        def paint(image: Image.Image, index: int) -> None:
            ImageDraw.Draw(image).rectangle((80 + index % 2, 55, 115 + index % 2, 90), fill="#555555")
        with tempfile.TemporaryDirectory() as directory:
            metrics = motion_metrics(self.frame_set(Path(directory), paint), expected("subject", "moderate"))
        self.assertTrue(motion_evidence_issues(metrics, expected("subject", "moderate")))

    def test_quiet_low_motion_is_allowed(self) -> None:
        def paint(image: Image.Image, index: int) -> None:
            ImageDraw.Draw(image).rectangle((70 + index * 5, 45, 145 + index * 5, 120), fill="#d0d0d0")
        plan = expected("quiet", "low", "none", "none")
        with tempfile.TemporaryDirectory() as directory:
            metrics = motion_metrics(self.frame_set(Path(directory), paint), plan)
        self.assertEqual(motion_evidence_issues(metrics, plan), [])

    def test_environment_motion_can_satisfy_plan_with_still_subject(self) -> None:
        def paint(image: Image.Image, index: int) -> None:
            draw = ImageDraw.Draw(image)
            draw.rectangle((65, 35, 150, 145), fill="#777777")
            draw.rectangle((205 + index * 8, 15, 270 + index * 8, 75), fill="#eeeeee")
        plan = expected("environment", "none", "moderate", "none")
        with tempfile.TemporaryDirectory() as directory:
            metrics = motion_metrics(self.frame_set(Path(directory), paint), plan)
        self.assertEqual(motion_evidence_issues(metrics, plan), [])

    def test_camera_motion_can_satisfy_plan_with_still_subject_relationship(self) -> None:
        metrics = {
            "global_motion": 0.02,
            "subject_motion": 0.001,
            "environment_motion": 0.01,
            "camera_translation_evidence": 0.01,
        }
        plan = expected("camera", "none", "low", "moderate")
        self.assertEqual(motion_evidence_issues(metrics, plan), [])

    def test_normal_subject_motion_passes(self) -> None:
        def paint(image: Image.Image, index: int) -> None:
            ImageDraw.Draw(image).ellipse((70 + index * 10, 50, 125 + index * 10, 115), fill="white")
        plan = expected("subject", "moderate", "none", "none")
        with tempfile.TemporaryDirectory() as directory:
            metrics = motion_metrics(self.frame_set(Path(directory), paint), plan)
        self.assertEqual(motion_evidence_issues(metrics, plan), [])

    def test_large_motion_does_not_override_story_state_failure(self) -> None:
        payload = {"per_scene_reviews": [{"scene": 1, "story_state_consistent": False, "adjacent_handoff_consistent": True}]}
        self.assertEqual(review_semantic_issues(payload), ["scene_1:story_state_conflict"])

    def test_formal_review_requires_every_scene_and_boolean_handoff_findings(self) -> None:
        payload = {"per_scene_reviews": [{"scene": 1, "story_state_consistent": True}]}
        issues = review_semantic_issues(payload, expected_scenes=[1, 2])
        self.assertIn("scene_1:adjacent_handoff_consistent_missing", issues)
        self.assertIn("per_scene_reviews_incomplete:2", issues)

    def test_adjacent_direction_and_state_pass_or_fail_by_declared_handoff(self) -> None:
        plan = compile_motion_plan(storyboard())
        self.assertEqual(adjacent_handoff_issues(plan["shots"]), [])
        plan["shots"][1]["screen_direction"] = "right_to_left"
        self.assertIn("无解释方向反转", "；".join(adjacent_handoff_issues(plan["shots"])))
        plan["shots"][1]["adjacent_handoff"]["allows_direction_change"] = True
        plan["shots"][1]["entry_state"] = {"story_state": {"state_machine": "state_2"}}
        self.assertIn("story_state 跳变", "；".join(adjacent_handoff_issues(plan["shots"])))

    def test_mock_is_allowed_for_test_but_blocked_for_production(self) -> None:
        row = {"video_source_kind": "mock_provider", "production_eligible": "false"}
        self.assertEqual(formal_source_issues(row, production_mode=False), [])
        self.assertIn("non_production_video_source:mock_provider", formal_source_issues(row, production_mode=True))

    def test_receipt_binds_provider_output_and_detects_static_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = root / "jobs.csv"
            jobs.write_text("scene,target_video_filename\n1,01.mp4\n", encoding="utf-8")
            video = root / "01.mp4"
            video.write_bytes(b"provider-video")
            row = {
                "scene": "1", "target_video_filename": "01.mp4", "provider_attempt": "0",
                "story_contract_sha256": "a" * 64,
                "story_contract_dependency_sha256": "b" * 64,
                "motion_plan_sha256": "c" * 64,
            }
            receipt, receipt_sha = write_video_receipt(
                jobs, row, video, provider="provider", model="model",
                source_kind="provider_generated", execution_mode="production", production_eligible=True,
            )
            row.update({
                "video_source_kind": "provider_generated", "production_eligible": "true",
                "video_receipt_path": str(receipt), "video_receipt_sha256": receipt_sha,
            })
            self.assertEqual(video_receipt_issues(row, video, production_mode=True), [])
            row["video_source_kind"] = "static_fallback"
            self.assertTrue(video_receipt_issues(row, video, production_mode=True))

    def test_real_static_video_fixture_writes_only_its_scene_to_retry_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "generic-story"
            init_project(project, story_name="generic", slug="generic")
            paths = project_paths(project)
            videos = paths.video_jobs / "videos"
            videos.mkdir(parents=True)
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=2:r=10",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(videos / "01_generic.mp4"),
            ], check=True)
            jobs = paths.video_jobs / "generic_image_video_jobs.csv"
            with jobs.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["scene", "target_video_filename", "expected_motion"])
                writer.writeheader()
                writer.writerow({
                    "scene": "1", "target_video_filename": "01_generic.mp4",
                    "expected_motion": json.dumps(expected("subject", "moderate")),
                })
            qa_videos(project, videos, execution_mode="test")
            payload = json.loads((paths.status / "qa_videos_report.json").read_text(encoding="utf-8"))
            self.assertFalse(payload["passed"])
            self.assertEqual(payload["retry_indices"], [1])
            self.assertIn("completely_static", payload["clips"][0]["issues"])


if __name__ == "__main__":
    unittest.main()
