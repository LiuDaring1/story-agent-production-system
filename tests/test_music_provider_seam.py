from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import assemble_suno_music
from story_evidence import file_sha256
from story_module_adapters import MockMusicProviderAdapter, SunoMusicProviderAdapter
from story_module_ports import (
    ModuleCapabilities,
    ModuleIdentity,
    MusicProviderPort,
    MusicProviderRequest,
    MusicProviderResult,
)
from story_module_registry import (
    MODULE_EXECUTION_MODE_ENV,
    MODULE_EXECUTION_MODE_REQUIRED_ENV,
    MODULE_PROFILE_ENV,
    MODULE_PROFILE_REQUIRED_ENV,
    ModuleRegistry,
    build_registry_for_profile,
)
from story_project import init_project, project_paths, write_manifest




def _mock_music_environment() -> dict[str, str]:
    return {
        MODULE_PROFILE_ENV: "mock-music",
        MODULE_PROFILE_REQUIRED_ENV: "mock-music",
        MODULE_EXECUTION_MODE_ENV: "test",
        MODULE_EXECUTION_MODE_REQUIRED_ENV: "test",
    }


class MusicProviderProductionSeamTests(unittest.TestCase):







    def test_assemble_find_audio_prefers_exact_name_then_segment_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clips = Path(directory)
            exact = clips / "required-name.mp3"
            fallback = clips / "01_downloaded-from-browser.wav"
            exact.write_bytes(b"exact")
            fallback.write_bytes(b"fallback")
            row = {"segment": "1", "target_audio_filename": exact.name}
            self.assertEqual(assemble_suno_music._find_audio(row, clips), exact)
            exact.unlink()
            self.assertEqual(assemble_suno_music._find_audio(row, clips), fallback)


if __name__ == "__main__":
    unittest.main()
