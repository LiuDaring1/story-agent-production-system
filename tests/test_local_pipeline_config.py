import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from story_project import load_config, save_config


class LocalPipelineConfigTests(unittest.TestCase):
    def test_local_paths_override_without_changing_shared_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "pipeline_config.json"
            config.write_text(json.dumps({"brand_assets": {"assets_dir": "assets/brand"}}))
            before = config.read_bytes()
            config.with_name("pipeline_config.local.json").write_text(json.dumps({
                "brand_assets": {"assets_dir": str(Path(directory) / "private-assets")}
            }))
            with patch("story_project.CONFIG_PATH", config):
                result = load_config()
                result["latest_episode"] = 124
                save_config(result)
                self.assertEqual(load_config()["latest_episode"], 124)
            self.assertEqual(result["brand_assets"]["assets_dir"], str(Path(directory) / "private-assets"))
            self.assertEqual(config.read_bytes(), before)
            self.assertIn("logo", result["brand_assets"])

    def test_absent_shared_config_still_uses_local_override(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "pipeline_config.json"
            config.with_name("pipeline_config.local.json").write_text('{"latest_episode": 123}')
            with patch("story_project.CONFIG_PATH", config):
                self.assertEqual(load_config()["latest_episode"], 123)
            self.assertNotEqual(json.loads(config.read_text())["latest_episode"], 123)
