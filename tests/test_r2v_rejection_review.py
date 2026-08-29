from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from r2v_rejection_review import collect_attempts, write_report


class R2VRejectionReviewTests(unittest.TestCase):
    def test_reports_playable_rejections_and_missing_attempt_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            rejected = project / "99_项目状态" / "rejected_r2v" / "S03"
            rejected.mkdir(parents=True)
            (rejected / "S03_attempt_00.mp4").write_bytes(b"rejected")
            accepted = project / "02_背景动画" / "正文R2V"
            accepted.mkdir(parents=True)
            (accepted / "S03.mp4").write_bytes(b"accepted")
            jobs = project / "99_项目状态" / "r2v" / "r2v_jobs.csv"
            jobs.parent.mkdir(parents=True)
            with jobs.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=["scene", "shot_id", "provider_attempt", "story_text"])
                writer.writeheader()
                writer.writerow({
                    "scene": "3", "shot_id": "S03", "provider_attempt": "2",
                    "story_text": "盘古醒来了",
                })
            attempts, missing = collect_attempts(
                project_dir=project,
                jobs_csv=jobs,
                rejected_dir=project / "99_项目状态" / "rejected_r2v",
                reasons={"S03:0": {"reason": "状态错误", "evidence_source": "原始审核"}},
            )
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["reason"], "状态错误")
            self.assertEqual(attempts[0]["story_text"], "盘古醒来了")
            self.assertEqual(missing, [{
                "shot_id": "S03", "attempt": 1,
                "status": "attempt counter exists, but no rejected video was preserved",
            }])
            outputs = write_report(
                project_dir=project,
                output_dir=project / "99_项目状态" / "review",
                attempts=attempts,
                missing=missing,
            )
            self.assertTrue(all(path.is_file() for path in outputs))
            payload = json.loads(outputs[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["playable_rejected_video_count"], 1)
            self.assertEqual(payload["missing_attempt_evidence_count"], 1)


if __name__ == "__main__":
    unittest.main()
