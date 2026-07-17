from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from story_project import (
    choose_preferred_music,
    create_theme_asset_request,
    init_project,
    load_manifest,
    project_paths,
    write_manifest,
)


class StoryAssetDetectionTests(unittest.TestCase):
    def test_extracted_source_audio_is_not_misclassified_as_music(self) -> None:
        narration = Path("elephant-ant_clean_narration.m4a")
        audios = [narration, Path("source_extracted_audio.m4a")]
        self.assertIsNone(choose_preferred_music(audios, narration))

    def test_explicitly_named_bgm_is_selected(self) -> None:
        narration = Path("story_narration.m4a")
        bgm = Path("forest_bgm.mp3")
        self.assertEqual(choose_preferred_music([narration, bgm], narration), bgm)

    def test_ambiguous_single_audio_is_not_assumed_to_be_music(self) -> None:
        narration = Path("story_narration.m4a")
        self.assertIsNone(choose_preferred_music([narration, Path("audio_001.mp3")], narration))

    def test_theme_request_infers_duration_from_narration_before_assembly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "故事剪辑：测试故事"
            paths = project_paths(root)
            manifest = init_project(root, story_name="测试故事", slug="test-story")
            narration = paths.inputs / "test-story_clean_narration.m4a"
            narration.parent.mkdir(parents=True, exist_ok=True)
            narration.write_bytes(b"audio-placeholder")
            manifest["inputs"]["extracted_narration"] = str(narration)
            manifest["story"]["duration_text"] = ""
            write_manifest(paths, manifest)
            with patch("story_project.safe_duration", return_value=179.7):
                outputs = create_theme_asset_request(root)
            request = outputs["request"].read_text(encoding="utf-8")
            self.assertIn("时长文案：3分钟", request)
            self.assertEqual(load_manifest(paths)["story"]["duration_text"], "3分钟")


if __name__ == "__main__":
    unittest.main()
