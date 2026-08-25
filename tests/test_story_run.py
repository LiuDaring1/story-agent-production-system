from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from story_run import PACKAGE_NAMES, finalize_run, init_run, load_run, record_run, status_summary


class StoryRunLedgerTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path, Path, Path, Path]:
        project = root / "故事项目"
        text = root / "confirmed.txt"
        video = root / "restored.mp4"
        audio = root / "narration.wav"
        run_file = project / "99_项目状态" / "story_run.json"
        text.write_text("确认正文，不允许系统改写。\n", encoding="utf-8")
        video.write_bytes(b"restored-video")
        audio.write_bytes(b"authoritative-audio")
        return project, text, video, audio, run_file

    def test_init_creates_only_six_coarse_packages_and_input_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, text, video, audio, run_file = self.fixture(Path(directory))
            payload = init_run(
                run_file=run_file,
                confirmed_text=text,
                greenscreen_video=video,
                audio=audio,
                project_dir=project,
            )
            self.assertEqual(set(payload["work_packages"]), set(PACKAGE_NAMES))
            self.assertTrue(all(item["status"] == "pending" for item in payload["work_packages"].values()))
            self.assertEqual(len(payload["inputs"]["confirmed_text"]["sha256"]), 64)
            self.assertNotIn("stages", payload)
            self.assertNotIn("attempts", payload)

    def test_record_is_idempotent_and_requires_replace_for_hash_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, text, video, audio, run_file = self.fixture(root)
            init_run(run_file=run_file, confirmed_text=text, greenscreen_video=video, audio=audio, project_dir=project)
            artifact = root / "story_r2v_plan.json"
            artifact.write_text('{"version": 1}\n', encoding="utf-8")
            first = record_run(
                run_file=run_file,
                package="director_plan",
                status="done",
                artifact_id="story_r2v_plan",
                artifact_path=artifact,
                input_hashes={"text": load_run(run_file)["inputs"]["confirmed_text"]["sha256"]},
                paid_amount=1.25,
            )
            second = record_run(
                run_file=run_file,
                package="director_plan",
                status="done",
                artifact_id="story_r2v_plan",
                artifact_path=artifact,
            )
            self.assertEqual(first["artifacts"]["story_r2v_plan"]["sha256"], second["artifacts"]["story_r2v_plan"]["sha256"])
            self.assertEqual(second["paid_total"], 1.25)
            artifact.write_text('{"version": 2}\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "--replace"):
                record_run(
                    run_file=run_file,
                    package="director_plan",
                    status="done",
                    artifact_id="story_r2v_plan",
                    artifact_path=artifact,
                )

    def test_hard_budget_blocks_record_and_status_warns_at_soft_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, text, video, audio, run_file = self.fixture(Path(directory))
            init_run(
                run_file=run_file,
                confirmed_text=text,
                greenscreen_video=video,
                audio=audio,
                project_dir=project,
                soft_budget=2,
                hard_budget=3,
            )
            record_run(run_file=run_file, package="music", status="running", paid_amount=2)
            summary = status_summary(load_run(run_file))
            self.assertTrue(summary["soft_budget_warning"])
            self.assertTrue(summary["can_start_paid_work"])
            with self.assertRaisesRegex(RuntimeError, "硬预算"):
                record_run(run_file=run_file, package="music", status="done", paid_amount=1.01)

    def test_finalize_rechecks_hashes_and_all_packages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, text, video, audio, run_file = self.fixture(root)
            init_run(run_file=run_file, confirmed_text=text, greenscreen_video=video, audio=audio, project_dir=project)
            artifact = root / "delivery.txt"
            artifact.write_text("ready\n", encoding="utf-8")
            for package in PACKAGE_NAMES:
                record_run(run_file=run_file, package=package, status="done")
            record_run(
                run_file=run_file,
                package="delivery",
                status="done",
                artifact_id="delivery_manifest",
                artifact_path=artifact,
            )
            payload = finalize_run(run_file=run_file, required_artifacts=["delivery_manifest"])
            self.assertTrue(payload["finalized_at"])
            artifact.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "哈希漂移"):
                finalize_run(run_file=run_file, required_artifacts=["delivery_manifest"])

    def test_blocked_state_requires_reason_and_stays_compact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, text, video, audio, run_file = self.fixture(Path(directory))
            init_run(run_file=run_file, confirmed_text=text, greenscreen_video=video, audio=audio, project_dir=project)
            with self.assertRaisesRegex(ValueError, "--blocker"):
                record_run(run_file=run_file, package="music", status="blocked")
            record_run(run_file=run_file, package="music", status="blocked", blocker="Suno 登录失效")
            serialized = run_file.read_text(encoding="utf-8")
            self.assertLess(len(serialized.encode("utf-8")), 8000)
            self.assertEqual(json.loads(serialized)["blocker"], "Suno 登录失效")


if __name__ == "__main__":
    unittest.main()
