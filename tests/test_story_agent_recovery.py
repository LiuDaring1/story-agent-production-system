from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from story_agent_observability import read_ndjson, recovery_log_path, timestamp
from story_agent_recovery import (
    CodexRecoveryController,
    RecoveryDecision,
    apply_recovery_decision,
    classify_recovery,
    validate_codex_decision,
)
from story_agent_runtime import file_sha256, mark_stage
from story_project import init_project, load_manifest, project_paths, write_manifest


def _decision(
    *,
    stage: str,
    action: str,
    repairs: tuple[str, ...] = (),
    reason: str = "fixture",
) -> RecoveryDecision:
    return RecoveryDecision(
        decision_id=uuid.uuid4().hex,
        created_at=timestamp(),
        job_id="recovery-job",
        stage=stage,
        attempt_id="attempt-1",
        action=action,
        category="fixture",
        reason=reason,
        evidence_sha256="a" * 64,
        evidence_path="/tmp/evidence.json",
        source="deterministic",
        retry_after_seconds=5 if action in {"retry_after", "repair_and_retry"} else 0,
        retry_at=timestamp() if action in {"retry_after", "repair_and_retry"} else "",
        safe_repairs=repairs,
        required_action="用户操作" if action in {"wait_for_user", "terminal_bug"} else "",
    )


def _snapshot(stage: str, reason: str) -> dict:
    return {
        "job_id": "recovery-job",
        "project_dir": "",
        "agent_status": "blocked",
        "current_stage": stage,
        "next_stage": stage,
        "blocked_reason": reason,
        "stage_rows": [
            {
                "stage": stage,
                "effective_status": "blocked",
                "attempt_id": "attempt-1",
                "message": reason,
                "input_hashes": {"input": "b" * 64},
                "output_hashes": {},
            }
        ],
        "supervisor": {},
        "locks": {},
        "budget": {"spent": 0, "hard_limit": 100},
        "timing": {"deadline_hours": 10, "remaining_deadline_hours": 9},
        "artifact_progress": {},
        "provider_receipts": [],
        "events": [],
        "recent_logs": [],
    }


class StoryAgentRecoveryTests(unittest.TestCase):
    def test_provider_502_retries_but_login_payment_and_permission_wait(self) -> None:
        transient = classify_recovery(
            "provider returned HTTP 502",
            stage="generate_videos",
            job_id="job",
            attempt_id="a1",
            evidence_sha256="a" * 64,
            evidence_path=Path("/tmp/evidence"),
        )
        self.assertEqual(transient.action, "retry_after")
        self.assertGreaterEqual(transient.retry_after_seconds, 5)
        for message in ("Suno 登录失效", "需要验证码 captcha", "积分不足需要付费", "permission denied"):
            decision = classify_recovery(
                message,
                stage="suno_generate",
                job_id="job",
                attempt_id="a1",
                evidence_sha256="b" * 64,
                evidence_path=Path("/tmp/evidence"),
            )
            self.assertEqual(decision.action, "wait_for_user", message)
            self.assertEqual(decision.safe_repairs, ())

    def test_codex_decision_is_hash_bound_and_cannot_request_arbitrary_repairs(self) -> None:
        with self.assertRaisesRegex(ValueError, "evidence SHA-256"):
            validate_codex_decision(
                '{"evidence_sha256":"bad","action":"retry_after","retry_after_seconds":30}',
                evidence_sha256="a" * 64,
                evidence_path=Path("/tmp/evidence"),
                job_id="job",
                stage="generate_videos",
                attempt_id="a1",
                model="fixture",
            )
        with self.assertRaisesRegex(ValueError, "unsafe recovery repair"):
            validate_codex_decision(
                json.dumps(
                    {
                        "evidence_sha256": "a" * 64,
                        "action": "repair_and_retry",
                        "category": "fixture",
                        "reason": "fix",
                        "retry_after_seconds": 5,
                        "safe_repairs": ["run_arbitrary_shell"],
                    }
                ),
                evidence_sha256="a" * 64,
                evidence_path=Path("/tmp/evidence"),
                job_id="job",
                stage="generate_videos",
                attempt_id="a1",
                model="fixture",
            )

    def test_retry_requeues_only_target_and_preserves_artifacts_and_other_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：恢复"
            manifest = init_project(project, story_name="恢复", slug="recovery")
            artifact = project / "00_输入素材" / "original.mov"
            artifact.write_bytes(b"immutable-original")
            original_sha = file_sha256(artifact)
            mark_stage(manifest, "generate_videos", "blocked", message="provider 502")
            mark_stage(manifest, "suno_generate", "blocked", message="等待登录")
            write_manifest(project_paths(project), manifest)
            result = apply_recovery_decision(
                project,
                _snapshot("generate_videos", "provider 502"),
                _decision(stage="generate_videos", action="retry_after", reason="provider 502"),
            )
            self.assertTrue(result.success, result.message)
            reloaded = load_manifest(project_paths(project))
            assert reloaded is not None
            self.assertEqual(reloaded["agent"]["stages"]["generate_videos"]["status"], "pending")
            self.assertEqual(
                reloaded["agent"]["stages"]["generate_videos"]["infrastructure_attempts"],
                1,
            )
            self.assertEqual(reloaded["agent"]["stages"]["suno_generate"]["status"], "blocked")
            self.assertEqual(file_sha256(artifact), original_sha)

    def test_stale_lock_is_archived_but_live_lock_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：锁恢复"
            manifest = init_project(project, story_name="锁恢复", slug="lock-recovery")
            mark_stage(manifest, "generate_videos", "failed", message="worker crash stale lock")
            write_manifest(project_paths(project), manifest)
            lock = project_paths(project).status / "story_agent.lock"
            lock.write_text(json.dumps({"pid": 99_999_999, "token": "old"}), encoding="utf-8")
            repairs = ("archive_stale_job_lock", "requeue_stage")
            result = apply_recovery_decision(
                project,
                _snapshot("generate_videos", "worker crash stale lock"),
                _decision(stage="generate_videos", action="repair_and_retry", repairs=repairs),
            )
            self.assertTrue(result.success, result.message)
            self.assertFalse(lock.exists())
            self.assertTrue(list((project_paths(project).status / "recovery" / "stale_locks").iterdir()))

            mark_stage(manifest, "generate_videos", "failed", message="worker crash stale lock")
            write_manifest(project_paths(project), manifest)
            lock.write_text(json.dumps({"pid": os.getpid(), "token": "live"}), encoding="utf-8")
            refused = apply_recovery_decision(
                project,
                _snapshot("generate_videos", "worker crash stale lock"),
                _decision(stage="generate_videos", action="repair_and_retry", repairs=repairs),
            )
            self.assertFalse(refused.success)
            self.assertTrue(lock.exists())
            self.assertIn("still alive", refused.message)

    def test_codex_controller_persists_valid_decision_and_safety_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：Codex恢复"
            init_project(project, story_name="Codex恢复", slug="codex-recovery")

            def retry_runner(prompt: Path, evidence: Path) -> tuple[bool, str, str]:
                self.assertTrue(prompt.is_file())
                digest = file_sha256(evidence)
                return (
                    True,
                    json.dumps(
                        {
                            "evidence_sha256": digest,
                            "action": "retry_after",
                            "category": "provider_502",
                            "reason": "temporary",
                            "retry_after_seconds": 45,
                            "safe_repairs": [],
                            "required_action": "",
                        }
                    ),
                    "gpt-fixture",
                )

            decision = CodexRecoveryController(project, runner=retry_runner).decide(
                _snapshot("generate_videos", "provider 502"),
                exit_code=1,
            )
            self.assertEqual(decision.source, "codex")
            self.assertEqual(decision.action, "retry_after")
            records = read_ndjson(recovery_log_path(project), limit=20)
            self.assertEqual(records[-1]["evidence_sha256"], decision.evidence_sha256)
            self.assertTrue(records[-1]["decision_sha256"])

            overridden = CodexRecoveryController(project, runner=retry_runner).decide(
                _snapshot("suno_generate", "Suno 登录失效，需要验证码"),
                exit_code=2,
            )
            self.assertEqual(overridden.action, "wait_for_user")
            self.assertEqual(overridden.source, "deterministic")
            self.assertIn("cannot override", read_ndjson(recovery_log_path(project), limit=20)[-1]["model_error"])

    def test_codex_runner_exception_uses_deterministic_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：恢复器异常"
            init_project(project, story_name="恢复器异常", slug="recovery-runner-error")

            def exploding_runner(prompt: Path, evidence: Path) -> tuple[bool, str, str]:
                del prompt, evidence
                raise RuntimeError("isolated Codex runner crashed")

            decision = CodexRecoveryController(project, runner=exploding_runner).decide(
                _snapshot("generate_videos", "provider 502"),
                exit_code=1,
            )
            self.assertEqual(decision.source, "deterministic")
            self.assertEqual(decision.action, "retry_after")
            latest = read_ndjson(recovery_log_path(project), limit=20)[-1]
            self.assertIn("RuntimeError", latest["model_error"])


if __name__ == "__main__":
    unittest.main()
