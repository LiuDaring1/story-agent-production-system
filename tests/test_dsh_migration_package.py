from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.build_dsh_migration_package import (
    CODEX_AGENTS_BASELINE,
    MANIFEST_NAME,
    MANIFEST_SHA_NAME,
    REQUIRED_PATHS,
    PackageError,
    build_package,
    verify_package,
)


class DshMigrationPackageTests(unittest.TestCase):
    def _git(self, root: Path, *args: str) -> str:
        process = subprocess.run(
            ["git", *args],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        return process.stdout.strip()

    def _source_repo(self, root: Path) -> Path:
        source = root / "source"
        source.mkdir()
        for relative in REQUIRED_PATHS:
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture for {relative}\n", encoding="utf-8")
        (source / "AGENTS.md").write_text("codex baseline instructions\n", encoding="utf-8")
        (source / "pipeline_config.json").write_text(
            json.dumps({"brand_assets": {"logo": "/Volumes/private/logo"}}),
            encoding="utf-8",
        )
        (source / "docs/migration/DSH_AGENTS.md").write_text(
            "dsh isolated instructions\n", encoding="utf-8"
        )
        (source / "docs/migration/pipeline_config.dsh-template.json").write_text(
            json.dumps({"brand_assets": {"logo": ""}, "agent_defaults": {"deadline_hours": 0}}),
            encoding="utf-8",
        )
        self._git(source, "init")
        self._git(source, "config", "user.name", "Story Agent Test")
        self._git(source, "config", "user.email", "story-agent-test@example.invalid")
        self._git(source, "add", ".")
        self._git(source, "commit", "-m", "fixture")
        return source

    def test_builds_from_clean_head_with_portable_overlays_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_repo(root)
            destination = root / "dsh-package"

            result = build_package(source, destination)

            self.assertEqual(result["package"], str(destination.resolve()))
            self.assertFalse(result["provider_calls_made"])
            self.assertFalse(result["canary_started"])
            self.assertFalse((destination / ".git").exists())
            self.assertEqual(
                (destination / "AGENTS.md").read_text(encoding="utf-8"),
                "dsh isolated instructions\n",
            )
            self.assertEqual(
                (destination / CODEX_AGENTS_BASELINE).read_text(encoding="utf-8"),
                "codex baseline instructions\n",
            )
            packaged_config = json.loads(
                (destination / "pipeline_config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(packaged_config["brand_assets"]["logo"], "")
            manifest = json.loads((destination / MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual(manifest["source_git"]["commit"], self._git(source, "rev-parse", "HEAD"))
            self.assertFalse(manifest["source_git"]["dirty"])
            self.assertFalse(manifest["provider_calls_made"])
            self.assertFalse(manifest["canary_started"])
            self.assertTrue((destination / MANIFEST_SHA_NAME).is_file())
            verified = verify_package(destination)
            self.assertTrue(verified["verified"])

    def test_dirty_source_is_refused_without_touching_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_repo(root)
            destination = root / "dsh-package"
            (source / "story_agent.py").write_text("dirty\n", encoding="utf-8")

            with self.assertRaisesRegex(PackageError, "dirty"):
                build_package(source, destination)

            self.assertFalse(destination.exists())

    def test_existing_destination_and_tampering_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_repo(root)
            destination = root / "dsh-package"
            build_package(source, destination)

            with self.assertRaisesRegex(PackageError, "already exists"):
                build_package(source, destination)

            (destination / "AI_CONTEXT.md").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(PackageError, "(?:size|SHA-256) mismatch"):
                verify_package(destination)


if __name__ == "__main__":
    unittest.main()
