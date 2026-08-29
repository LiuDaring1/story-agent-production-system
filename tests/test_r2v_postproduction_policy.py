import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


ASSEMBLY = _load_module("assemble_r2v_story", PROJECT_ROOT / "assemble_r2v_story.py")
POSTFIX = _load_module("r2v_local_postfix", PROJECT_ROOT / "r2v_local_postfix.py")
GROUP_QA = _load_module("r2v_group_qa", PROJECT_ROOT / "r2v_group_qa.py")


class R2VPostproductionPolicyTests(unittest.TestCase):
    def test_contiguous_timeline_is_accepted(self):
        shots = [
            {"shot_id": "S01", "source_start": 1.0, "source_end": 6.0},
            {"shot_id": "S02", "source_start": 6.0, "source_end": 12.0},
        ]
        ASSEMBLY.validate_contiguous_timeline(shots, 12.0)

    def test_timeline_gap_is_rejected_instead_of_frozen(self):
        shots = [
            {"shot_id": "S01", "source_start": 1.0, "source_end": 6.0},
            {"shot_id": "S02", "source_start": 6.5, "source_end": 12.0},
        ]
        with self.assertRaisesRegex(ValueError, "禁止用定帧补齐"):
            ASSEMBLY.validate_contiguous_timeline(shots, 12.0)

    def test_same_source_trim_crop_is_allowed(self):
        operations = [
            {
                "shot_id": "S01",
                "strategy": "trim_crop",
                "start": 0.25,
                "seconds": 5.5,
                "crop": "crop=1000:700:100:10",
            }
        ]
        self.assertEqual(
            POSTFIX.validate_operations(operations, allow_legacy=False), operations
        )

    def test_cross_shot_bridge_is_rejected_by_default(self):
        operations = [
            {
                "shot_id": "S01",
                "strategy": "video_bridge",
                "bridge_source": "S02.mp4",
                "action_seconds": 4.0,
                "bridge_seconds": 2.0,
            }
        ]
        with self.assertRaisesRegex(ValueError, "正式生产只允许同一源镜头"):
            POSTFIX.validate_operations(operations, allow_legacy=False)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_exact_freeze_detector_catches_static_video(self):
        with tempfile.TemporaryDirectory(prefix="r2v-freeze-test-") as temporary:
            path = Path(temporary) / "static.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=24:d=0.75",
                    "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
                ],
                check=True,
            )
            intervals = GROUP_QA.exact_freeze_intervals(path, "ffmpeg", "24/1")
            self.assertTrue(intervals)
            self.assertGreaterEqual(intervals[0]["duration"], 0.45)


if __name__ == "__main__":
    unittest.main()
