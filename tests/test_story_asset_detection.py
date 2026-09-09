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
    def setUp(self):
        from PIL import Image
        from story_project import load_config
        self.fixture_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.fixture_dir.cleanup)
        frame = Path(self.fixture_dir.name) / "frame.png"
        Image.new("RGBA", (160, 90), (255, 0, 255, 255)).save(frame)
        config = load_config()
        config["brand_assets"]["frame_reference"] = str(frame)
        patcher = patch("story_project.load_config", return_value=config)
        patcher.start()
        self.addCleanup(patcher.stop)

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
            # Age is deliberately no longer inferred.  Declare it explicitly
            # so this test continues to isolate narration-duration detection.
            manifest["story"]["age_range"] = "3-6岁"
            manifest["story"].setdefault("manual_overrides", {})["age_range"] = True
            write_manifest(paths, manifest)
            with patch("story_project.safe_duration", return_value=179.7):
                outputs = create_theme_asset_request(root)
            request = outputs["request"].read_text(encoding="utf-8")
            self.assertIn("时长文案：3分钟", request)
            self.assertIn("禁止用 SVG", request)
            self.assertEqual(load_manifest(paths)["story"]["duration_text"], "3分钟")

    def test_stale_theme_outputs_are_archived_before_a_new_imagegen_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "故事剪辑：测试故事"
            paths = project_paths(root)
            manifest = init_project(root, story_name="测试故事", slug="test-story")
            manifest["story"]["age_range"] = "3-6岁"
            manifest["story"].setdefault("manual_overrides", {})["age_range"] = True
            write_manifest(paths, manifest)
            theme = paths.release / "theme_assets"
            theme.mkdir(parents=True, exist_ok=True)
            stale = theme / "main_release_plate_top.png"
            stale.write_bytes(b"stale-generated-panel")

            outputs = create_theme_asset_request(root)

            self.assertFalse(stale.exists())
            archived = list((paths.status / "rejected" / "theme_assets").glob("*/main_release_plate_top.png"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_bytes(), b"stale-generated-panel")
            self.assertTrue(outputs["request"].is_file())


if __name__ == "__main__":
    unittest.main()
