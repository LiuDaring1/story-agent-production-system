from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import story_agent
import story_agent_runtime
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

    def test_legacy_imports_resolve_to_archived_implementation(self) -> None:
        self.assertIn("legacy/story_agent_v3/story_agent.py", str(Path(story_agent.__file__)))
        self.assertIn(
            "legacy/story_agent_v3/story_agent_runtime.py",
            str(Path(story_agent_runtime.__file__)),
        )

    def test_all_nonlegacy_python_modules_do_not_import_legacy_agent(self) -> None:
        compatibility_shims = {
            "story_agent.py",
            "story_agent_dashboard.py",
            "story_agent_observability.py",
            "story_agent_preflight.py",
            "story_agent_recovery.py",
            "story_agent_runtime.py",
            "story_agent_supervisor.py",
            "story_codex_tasks.py",
        }
        candidates = sorted(
            {
                *ROOT.glob("*.py"),
                *(ROOT / "scripts").rglob("*.py"),
                *(ROOT / "story_video_synthesizer").rglob("*.py"),
                *(ROOT / "skills").glob("*/scripts/*.py"),
            }
        )
        scanned = 0
        for path in candidates:
            relative = path.relative_to(ROOT)
            if len(relative.parts) == 1 and relative.name in compatibility_shims:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
            imported: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.append(node.module)
            forbidden = [
                name
                for name in imported
                if name == "story_agent_runtime"
                or name.startswith("legacy.story_agent_v3")
            ]
            self.assertEqual(forbidden, [], msg=f"{relative} 反向导入 Legacy：{forbidden}")
            scanned += 1
        self.assertGreater(scanned, 60)

    def test_release_has_no_dynamic_presenter_x_implementation(self) -> None:
        presenter = (ROOT / "presenter_layout.py").read_text(encoding="utf-8")
        release = (ROOT / "release_video.py").read_text(encoding="utf-8")
        for retired_name in ("normalize_x_keyframes", "x_at_time", "ffmpeg_x_expression"):
            self.assertNotIn(retired_name, presenter)
            self.assertNotIn(retired_name, release)
        self.assertNotIn("person_x_keyframes:", release)

    def test_legacy_qualification_is_not_exposed_as_a_native_root_module(self) -> None:
        self.assertFalse((ROOT / "story_qualification.py").exists())
        self.assertTrue((ROOT / "legacy/story_agent_v3/story_qualification.py").is_file())

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
