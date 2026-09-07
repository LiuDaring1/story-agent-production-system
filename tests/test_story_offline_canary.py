from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from story_artifact_validation import validate_artifact_semantics
from story_run import file_sha256


class OfflineRealMediaCanaryTests(unittest.TestCase):
    def test_real_short_video_flows_from_qa_producer_to_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); videos = root / "videos"; videos.mkdir()
            clip = videos / "S01.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "testsrc2=size=320x180:rate=30:duration=1.2", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", str(clip),
            ], check=True)
            plan = root / "plan.json"
            plan.write_text(json.dumps({"shots": [{"shot_id": "S01", "provider_seconds": 1}]}))
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps({"shots": [{
                "filename": "S01.mp4", "status": "downloaded", "task_id": "offline-canary-1",
                "output_sha256": file_sha256(clip),
            }]}))
            report = root / "qa.json"
            completed = subprocess.run([
                sys.executable, "r2v_group_qa.py", "--plan", str(plan), "--receipt", str(receipt),
                "--videos-dir", str(videos), "--output", str(report),
            ], text=True, capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            payload = validate_artifact_semantics("r2v_group_machine_qa", report)
            self.assertTrue(payload["passed"])
            self.assertEqual(payload["clips"][0]["sha256"], file_sha256(clip))
            clip.write_bytes(clip.read_bytes() + b"changed")
            with self.assertRaisesRegex(ValueError, "哈希漂移"):
                validate_artifact_semantics("r2v_group_machine_qa", report)


if __name__ == "__main__":
    unittest.main()
