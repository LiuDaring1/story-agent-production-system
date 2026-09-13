from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from story_workflow import (
    build_abc_scene_windows,
    load_release_person_layout,
    preview_times_with_b_coverage,
    preview_times_with_library_tail_coverage,
    require_preferred_keyer,
)


class StorySceneWindowTests(unittest.TestCase):
    def test_preview_times_cover_every_b_window_without_duplicate_filenames(self) -> None:
        result = preview_times_with_b_coverage(
            "1,2,37,92",
            "14.075-32.979,70.440-88.979,128.160-146.189",
            "0.000-14.075,50.000-70.440",
        )
        times = [float(item) for item in result.split(",")]
        self.assertEqual(times[:4], [1.0, 2.0, 37.0, 92.0])
        for start, end in ((14.075, 32.979), (70.440, 88.979), (128.160, 146.189)):
            self.assertTrue(any(start <= item <= end for item in times))
        for start, end in ((0.0, 14.075), (50.0, 70.44)):
            self.assertTrue(any(start <= item <= end for item in times))
        self.assertEqual(len({round(item) for item in times}), len(times))

    def test_starts_with_c_uses_b_and_subtitle_free_tail_uses_c(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            srt = Path(directory) / "story.srt"
            blocks = []
            for index, start in enumerate(range(0, 72, 6), start=1):
                end = start + 5
                blocks.append(f"{index}\n00:00:{start:02d},000 --> 00:00:{end:02d},000\n第{index}句")
            srt.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
            b_windows, c_windows = build_abc_scene_windows(80.0, srt)
            self.assertTrue(c_windows.startswith("0.000-"))
            self.assertTrue(b_windows)
            self.assertNotIn("-80.000", b_windows)
            self.assertIn("71.000-80.000", c_windows)

    def test_short_video_is_all_a_to_guarantee_a_ending(self) -> None:
        b_windows, c_windows = build_abc_scene_windows(12.0, None)
        self.assertEqual(b_windows, "")
        self.assertEqual(c_windows, "")

    def test_subtitle_free_moral_tail_uses_c(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            srt = Path(directory) / "story.srt"
            srt.write_text(
                "1\n00:00:00,000 --> 00:00:18,000\n正文\n",
                encoding="utf-8",
            )
            _b_windows, c_windows = build_abc_scene_windows(30.0, srt)
            self.assertIn("18.000-30.000", c_windows)

    def test_library_preview_always_samples_blurred_tail(self) -> None:
        result = preview_times_with_library_tail_coverage("12,72", 192.667, 0.0)
        times = [float(item) for item in result.split(",")]
        self.assertTrue(any(item > 190.0 for item in times))

    def test_preferred_rvm_refuses_colorkey_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preset = Path(directory) / "keying_preset.json"
            preset.write_text(json.dumps({"keyer": "colorkey"}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "拒绝静默降级"):
                require_preferred_keyer(preset, "rvm")

    def test_preferred_rvm_accepts_rvm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preset = Path(directory) / "keying_preset.json"
            preset.write_text(json.dumps({"keyer": "rvm"}), encoding="utf-8")
            require_preferred_keyer(preset, "rvm")

    def test_project_release_layout_binds_keying_without_cropping(self) -> None:
        import hashlib

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preset = root / "keying_preset.json"
            preset.write_text(
                json.dumps(
                    {
                        "keyer": "rvm",
                        "person_layout_policy": "source-native-fixed-anchor/v2",
                        "rvm_input_width": 1920,
                        "rvm_input_height": 1080,
                        "presenter_initial_subject_bbox": [390, 89, 914, 991],
                    }
                ),
                encoding="utf-8",
            )
            layout = root / "release_layout.json"
            layout.write_text(
                json.dumps(
                    {
                        "schema_version": "story-release-person-layout/v1",
                        "keying_preset_sha256": hashlib.sha256(preset.read_bytes()).hexdigest(),
                        "person_height": 1080,
                        "person_x": 673,
                        "person_y": 0,
                        "person_crop": None,
                        "dynamic_repositioning": False,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                load_release_person_layout(layout, preset),
                {"person_height": 1080, "person_x": 673, "person_y": 0},
            )

    def test_project_release_layout_rejects_scale_or_wrong_center_axis(self) -> None:
        import hashlib

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preset = root / "keying_preset.json"
            preset.write_text(
                json.dumps(
                    {
                        "keyer": "rvm",
                        "person_layout_policy": "source-native-fixed-anchor/v2",
                        "rvm_input_width": 1920,
                        "rvm_input_height": 1080,
                        "presenter_initial_subject_bbox": [390, 89, 914, 991],
                    }
                ),
                encoding="utf-8",
            )
            base = {
                "schema_version": "story-release-person-layout/v1",
                "keying_preset_sha256": hashlib.sha256(preset.read_bytes()).hexdigest(),
                "person_height": 980,
                "person_x": 673,
                "person_y": 0,
                "person_crop": None,
                "dynamic_repositioning": False,
            }
            layout = root / "release_layout.json"
            layout.write_text(json.dumps(base), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "禁止缩放人物"):
                load_release_person_layout(layout, preset)
            base["person_height"] = 1080
            base["person_x"] = 780
            layout.write_text(json.dumps(base), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "人物中轴未对齐"):
                load_release_person_layout(layout, preset)

    def test_project_release_layout_rejects_stale_keying_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preset = root / "keying_preset.json"
            preset.write_text(json.dumps({"keyer": "rvm"}), encoding="utf-8")
            layout = root / "release_layout.json"
            layout.write_text(
                json.dumps(
                    {
                        "schema_version": "story-release-person-layout/v1",
                        "keying_preset_sha256": "0" * 64,
                        "person_height": 1080,
                        "person_x": 580,
                        "person_y": 0,
                        "person_crop": None,
                        "dynamic_repositioning": False,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "未绑定"):
                load_release_person_layout(layout, preset)

if __name__ == "__main__":
    unittest.main()
