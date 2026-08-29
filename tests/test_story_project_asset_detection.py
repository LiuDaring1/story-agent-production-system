import tempfile
import unittest
from pathlib import Path

from story_project import is_user_input_asset, project_paths


class StoryProjectAssetDetectionTests(unittest.TestCase):
    def test_story_specific_numbered_output_folders_are_not_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = project_paths(root)
            self.assertFalse(is_user_input_asset(paths, root / "02_背景动画" / "正文R2V" / "S01.mp4"))
            self.assertFalse(is_user_input_asset(paths, root / "04_配乐与音频" / "正式分段" / "01_music.mp3"))

    def test_root_and_authoritative_input_folder_remain_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = project_paths(root)
            self.assertTrue(is_user_input_asset(paths, root / "视频：盘古开天辟地.mp4"))
            self.assertTrue(is_user_input_asset(paths, root / "00_输入素材" / "权威音频.wav"))


if __name__ == "__main__":
    unittest.main()
