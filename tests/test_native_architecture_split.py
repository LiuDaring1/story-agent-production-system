from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from story_evidence import review_bundle_is_current, review_passes, write_review_bundle


ROOT = Path(__file__).resolve().parents[1]


class NativeArchitectureSplitTests(unittest.TestCase):
    def test_supported_entry_describes_foreground_codex_and_legacy_boundary(self) -> None:
        process = subprocess.run(
            [sys.executable, str(ROOT / "story_pipeline.py"), "describe"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        payload = json.loads(process.stdout)
        self.assertEqual(payload["human_entry"], "Codex task + skills/story-full-auto")
        self.assertEqual(payload["supported_control_entry"], "story_pipeline.py")
        self.assertEqual(payload["state"], "99_项目状态/story_run.json")
        self.assertFalse(payload["legacy_is_production_entry"])



    def test_release_has_no_dynamic_presenter_x_implementation(self) -> None:
        presenter = (ROOT / "presenter_layout.py").read_text(encoding="utf-8")
        release = (ROOT / "release_video.py").read_text(encoding="utf-8")
        for retired_name in ("normalize_x_keyframes", "x_at_time", "ffmpeg_x_expression"):
            self.assertNotIn(retired_name, presenter)
            self.assertNotIn(retired_name, release)
        self.assertNotIn("person_x_keyframes:", release)


    def test_shared_review_evidence_is_current_without_legacy_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "artifact.bin"
            artifact.write_bytes(b"current-artifact")
            bundle = write_review_bundle(root / "review_bundle.json", [artifact])
            self.assertTrue(review_bundle_is_current(bundle))
            review = {
                "approved": True,
                "score": 90,
                "critical_errors": [],
                "artifact_sha256": __import__("hashlib").sha256(artifact.read_bytes()).hexdigest(),
            }
            self.assertTrue(review_passes(review, artifact=artifact))
            artifact.write_bytes(b"changed")
            self.assertFalse(review_bundle_is_current(bundle))
            self.assertFalse(review_passes(review, artifact=artifact))





if __name__ == "__main__":
    unittest.main()

class ProductionImportBoundaryTests(unittest.TestCase):
    def test_real_production_imports_never_load_retired_agents(self):
        code = """
import json, sys
import story_pipeline, run_image_video_jobs, story_workflow, release_video, product_package, assemble_r2v_story
print(json.dumps(sorted(n for n in sys.modules if n.startswith(('legacy.', 'story_agent', 'story_codex_tasks', 'workbench')))))
"""
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [])
        for name in ('story_agent.py', 'story_agent_runtime.py', 'story_codex_tasks.py', 'workbench_app.py'):
            self.assertFalse((ROOT/name).exists())

    def test_archived_source_bytes_are_verifiable_without_executing_them(self):
        subprocess.run([sys.executable, str(ROOT/'historical_archive/verify.py')], check=True, capture_output=True)
