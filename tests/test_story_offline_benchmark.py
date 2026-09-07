from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class StoryOfflineBenchmarkTests(unittest.TestCase):
    def test_fixture_candidate_gets_executable_structural_scores_without_provider(self) -> None:
        completed = subprocess.run(
            [sys.executable, "offline_story_benchmark.py"],
            text=True, capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["provider_requests"], 0)
        self.assertTrue(result["candidate_results"][0]["structural_passed"])
        self.assertIsNone(result["candidate_results"][0]["creative_review"])

    def test_missing_candidate_evidence_fails_with_per_case_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.json"
            candidate.write_text(json.dumps({
                "schema_version": "story-offline-directing-candidate/v1",
                "candidate_id": "incomplete",
                "source_kind": "test",
                "cases": [{
                    "case_id": "dialogue-reaction",
                    "evidence": {"ordered_beats": "present"},
                    "critical_errors": [],
                }],
            }))
            completed = subprocess.run(
                [sys.executable, "offline_story_benchmark.py", "--candidate", str(candidate)],
                text=True, capture_output=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            result = json.loads(completed.stdout)
            first = result["candidate_results"][0]["per_case"][0]
            self.assertIn("reaction_after_information", first["missing_checks"])


if __name__ == "__main__":
    unittest.main()
