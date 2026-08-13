from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import prepare_suno_music_request


class MusicContractBindingTests(unittest.TestCase):
    def test_music_request_records_reviewed_contract_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "story.txt"
            story.write_text("主角出发。\n主角遇到困难。\n主角找到答案。\n", encoding="utf-8")
            output = root / "output"
            context = root / "music.json"
            payload = {
                "version": 1,
                "consumer": "music",
                "contract_schema_version": "1.0.0",
                "story_contract_sha256": "a" * 64,
                "story_contract_dependency_sha256": "b" * 64,
                "contract_projection": {
                    "semantic_artifacts": {"rules": [{"value": "故事正文边界"}]},
                    "story_state": {"states": [{"state_id": "opening", "emotion": "好奇"}]},
                },
            }
            context.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            argv = [
                "prepare_suno_music_request.py",
                "--story-file", str(story),
                "--output-dir", str(output),
                "--slug", "generic",
                "--story-contract-context", str(context),
            ]
            with patch.object(sys, "argv", argv):
                prepare_suno_music_request.main()

            manifest = json.loads(
                (output / "music" / "generic_music_request_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest, payload)
            request = (output / "music" / "generic_suno_music_request.md").read_text(encoding="utf-8")
            self.assertIn("story_contract_dependency_sha256", request)
            self.assertIn("semantic_artifacts", request)
            self.assertIn("story_state", request)

    def test_music_context_rejects_wrong_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = Path(directory) / "wrong.json"
            context.write_text(
                json.dumps(
                    {
                        "consumer": "cover",
                        "contract_schema_version": "1.0.0",
                        "story_contract_sha256": "a" * 64,
                        "story_contract_dependency_sha256": "b" * 64,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                prepare_suno_music_request._load_music_contract_context(context)


if __name__ == "__main__":
    unittest.main()
