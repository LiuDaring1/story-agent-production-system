from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from assemble_r2v_story import (
    sha256_path,
    validate_formal_r2v_bindings,
    validate_semantic_card_video_bindings,
)


class R2VAssemblyGateTests(unittest.TestCase):
    def fixture(self, root: Path) -> list[str]:
        videos = root / "videos"; videos.mkdir()
        for path in (root / "title.mp4", videos / "S01.mp4"):
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "testsrc2=size=320x180:rate=30:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
            ], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=2", str(root / "voice.wav")], check=True)
        (root / "plan.json").write_text(json.dumps({"shots": [{"shot_id": "S01", "source_start": 1, "source_end": 2, "story_text": "测试"}]}))
        return [
            sys.executable, "assemble_r2v_story.py", "--plan", str(root / "plan.json"),
            "--videos-dir", str(videos), "--title-video", str(root / "title.mp4"),
            "--audio", str(root / "voice.wav"), "--output", str(root / "master.mp4"),
            "--decisions", str(root / "decisions.json"), "--clips-dir", str(root / "clips"),
            "--ppt-plan", str(root / "ppt.json"),
        ]

    def test_formal_assembly_without_current_ledger_evidence_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); command = self.fixture(root)
            result = subprocess.run(command, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--run-file", result.stderr)
            self.assertFalse((root / "master.mp4").exists())

    def test_explicit_diagnostic_preview_is_marked_not_deliverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); command = self.fixture(root) + ["--diagnostic-preview"]
            result = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            decisions = json.loads((root / "decisions.json").read_text())
            self.assertEqual(decisions["qualification"], "diagnostic_preview_not_deliverable")

    def test_formal_bindings_accept_exact_reviewed_inputs_and_reject_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.fixture(root)
            plan = root / "plan.json"; videos = root / "videos"; clip = videos / "S01.mp4"
            receipt = root / "provider-receipt.json"; receipt.write_text('{"shots": []}')
            qa = root / "qa.json"
            qa.write_text(json.dumps({
                "plan_path": str(plan.resolve()), "plan_sha256": sha256_path(plan),
                "videos_dir": str(videos.resolve()),
                "receipt_path": str(receipt.resolve()), "receipt_sha256": sha256_path(receipt),
                "clips": [{"shot_id": "S01", "path": str(clip.resolve()), "sha256": sha256_path(clip)}],
            }))
            kwargs = {
                "plan_path": plan, "plan": json.loads(plan.read_text()), "videos_dir": videos,
                "moral_video": None, "machine_qa_path": qa,
                "provider_receipt_record": {"path": str(receipt.resolve()), "sha256": sha256_path(receipt)},
            }
            validate_formal_r2v_bindings(**kwargs)
            replacement = root / "replacement.mp4"; replacement.write_bytes(clip.read_bytes())
            clip.write_bytes(b"unreviewed replacement")
            with self.assertRaisesRegex(ValueError, "不是整组机器 QA 审过"):
                validate_formal_r2v_bindings(**kwargs)

    def test_formal_bindings_reject_different_plan_or_provider_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.fixture(root)
            plan = root / "plan.json"; videos = root / "videos"; clip = videos / "S01.mp4"
            receipt = root / "provider-receipt.json"; receipt.write_text('{"shots": []}')
            qa = root / "qa.json"
            qa.write_text(json.dumps({
                "plan_path": str(plan.resolve()), "plan_sha256": sha256_path(plan),
                "videos_dir": str(videos.resolve()),
                "receipt_path": str(receipt.resolve()), "receipt_sha256": sha256_path(receipt),
                "clips": [{"shot_id": "S01", "path": str(clip.resolve()), "sha256": sha256_path(clip)}],
            }))
            plan.write_text(json.dumps({"shots": [{"shot_id": "S02"}]}))
            with self.assertRaisesRegex(ValueError, "--plan"):
                validate_formal_r2v_bindings(
                    plan_path=plan, plan=json.loads(plan.read_text()), videos_dir=videos,
                    moral_video=None, machine_qa_path=qa,
                    provider_receipt_record={"path": str(receipt), "sha256": sha256_path(receipt)},
                )

    def test_formal_semantic_card_cli_inputs_must_match_motion_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            title = root / "title.mp4"; title.write_bytes(b"reviewed title")
            moral = root / "moral.mp4"; moral.write_bytes(b"reviewed moral")
            receipt = root / "semantic_card_motion_receipt.json"
            receipt.write_text(json.dumps({"cards": [
                {"card_kind": "title_card", "output_video_path": str(title), "output_video_sha256": sha256_path(title)},
                {"card_kind": "moral_card", "output_video_path": str(moral), "output_video_sha256": sha256_path(moral)},
            ]}))
            validate_semantic_card_video_bindings(
                title_video=title, moral_video=moral,
                motion_receipt_path=receipt, require_moral=True,
            )
            replacement = root / "replacement-title.mp4"
            replacement.write_bytes(title.read_bytes())
            with self.assertRaisesRegex(ValueError, "--title-video"):
                validate_semantic_card_video_bindings(
                    title_video=replacement, moral_video=moral,
                    motion_receipt_path=receipt, require_moral=True,
                )
            with self.assertRaisesRegex(ValueError, "--moral-video"):
                validate_semantic_card_video_bindings(
                    title_video=title, moral_video=None,
                    motion_receipt_path=receipt, require_moral=True,
                )


if __name__ == "__main__":
    unittest.main()
