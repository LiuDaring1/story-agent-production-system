from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import presenter_layout
import rvm_keying
from presenter_layout import scan_rvm_body_overflow, severe_body_overflow_windows
from story_release_policy import validate_presenter_scan_report


class RvmCacheIdentityRegressionTests(unittest.TestCase):
    def make_receipt(self, root: Path, **overrides: object) -> tuple[Path, Path, Path, Path]:
        source = root / "source.mov"
        output = root / "foreground.webm"
        model = root / "model.onnx"
        receipt = root / "receipt.json"
        source.write_bytes(b"source fixture")
        output.write_bytes(b"alpha fixture")
        model.write_bytes(b"official model fixture; validation is mocked")
        payload = {
            "schema_version": rvm_keying.RVM_RECEIPT_SCHEMA_VERSION,
            "backend_version": rvm_keying.RVM_BACKEND_VERSION,
            "status": "complete",
            "source_sha256": rvm_keying.file_sha256(source),
            "model_sha256": rvm_keying.OFFICIAL_RVM_MOBILENETV3_FP32_SHA256,
            "frame_count": 125,
            "output_video": str(output),
            "output_sha256": rvm_keying.file_sha256(output),
            "temporal_recurrence_used": True,
            "alpha_channel_verified": True,
            "width": 1920,
            "height": 1080,
            "fps": 25,
            "downsample_ratio": 0.4,
            "start_seconds": 0.0,
            "requested_duration_seconds": None,
        }
        payload.update(overrides)
        receipt.write_text(json.dumps(payload), encoding="utf-8")
        return source, output, model, receipt

    def assert_cache_miss(self, changed_options: dict[str, object]) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, model, receipt = self.make_receipt(Path(directory))
            options = {"model_path": model, **changed_options}
            with patch.object(
                rvm_keying,
                "validate_rvm_model",
                return_value=rvm_keying.OFFICIAL_RVM_MOBILENETV3_FP32_SHA256,
            ), patch.object(
                rvm_keying, "render_rvm_foreground_video", return_value=output
            ) as render:
                self.assertEqual(
                    rvm_keying.ensure_rvm_foreground_video(source, output, receipt, **options),
                    output,
                )
            render.assert_called_once_with(source, output, receipt, **options)

    def test_same_effective_defaults_reuse_existing_foreground(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, model, receipt = self.make_receipt(Path(directory))
            with patch.object(
                rvm_keying,
                "validate_rvm_model",
                return_value=rvm_keying.OFFICIAL_RVM_MOBILENETV3_FP32_SHA256,
            ), patch.object(rvm_keying, "render_rvm_foreground_video") as render:
                returned = rvm_keying.ensure_rvm_foreground_video(
                    source, output, receipt, model_path=model
                )
            self.assertEqual(returned, output)
            render.assert_not_called()

    def test_every_effective_render_parameter_change_invalidates_cache(self) -> None:
        for changed in (
            {"width": 1280},
            {"height": 720},
            {"fps": 30},
            {"downsample_ratio": 0.2},
            {"start_seconds": 2.0},
            {"duration_seconds": 10.0},
        ):
            with self.subTest(changed=changed):
                self.assert_cache_miss(changed)

    def test_missing_parameter_source_output_model_and_backend_do_not_reuse(self) -> None:
        for missing_field in rvm_keying.RVM_RENDER_RECEIPT_FIELDS.values():
            with self.subTest(missing_field=missing_field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, output, model, receipt = self.make_receipt(root)
                payload = json.loads(receipt.read_text(encoding="utf-8"))
                del payload[missing_field]
                receipt.write_text(json.dumps(payload), encoding="utf-8")
                with patch.object(
                    rvm_keying,
                    "validate_rvm_model",
                    return_value=rvm_keying.OFFICIAL_RVM_MOBILENETV3_FP32_SHA256,
                ):
                    issues = rvm_keying.rvm_receipt_issues(
                        receipt,
                        expected_source=source,
                        expected_output=output,
                        expected_render_options={"model_path": model},
                    )
                self.assertIn(f"rvm_render_parameter_missing:{missing_field}", issues)

        with tempfile.TemporaryDirectory() as directory:
            source, output, model, receipt = self.make_receipt(Path(directory))
            source.write_bytes(b"changed source")
            output.write_bytes(b"changed output")
            with patch.object(rvm_keying, "validate_rvm_model", return_value="f" * 64):
                issues = rvm_keying.rvm_receipt_issues(
                    receipt,
                    expected_source=source,
                    expected_output=output,
                    expected_render_options={"model_path": model},
                )
            self.assertIn("rvm_source_sha256_mismatch", issues)
            self.assertIn("rvm_output_sha256_mismatch", issues)
            self.assertIn("rvm_requested_model_sha256_mismatch", issues)

        with tempfile.TemporaryDirectory() as directory:
            source, output, model, receipt = self.make_receipt(
                Path(directory), backend_version="obsolete-rvm-implementation"
            )
            with patch.object(
                rvm_keying,
                "validate_rvm_model",
                return_value=rvm_keying.OFFICIAL_RVM_MOBILENETV3_FP32_SHA256,
            ):
                issues = rvm_keying.rvm_receipt_issues(
                    receipt,
                    expected_source=source,
                    expected_output=output,
                    expected_render_options={"model_path": model},
                )
            self.assertIn("rvm_backend_version_mismatch", issues)

    def test_unknown_render_option_is_not_silently_treated_as_a_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, output, model, receipt = self.make_receipt(Path(directory))
            with patch.object(
                rvm_keying,
                "validate_rvm_model",
                return_value=rvm_keying.OFFICIAL_RVM_MOBILENETV3_FP32_SHA256,
            ):
                issues = rvm_keying.rvm_receipt_issues(
                    receipt,
                    expected_source=source,
                    expected_output=output,
                    expected_render_options={"model_path": model, "typo_ratio": 0.2},
                )
            self.assertIn("rvm_render_option_unsupported:typo_ratio", issues)


class PresenterScanPrecisionRegressionTests(unittest.TestCase):
    def run_pixel_scan(self, root: Path, *, boundary_fixture: bool) -> tuple[dict, Path]:
        source = root / "foreground.webm"
        source.write_bytes(b"isolated foreground fixture")

        def fake_subprocess(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            if command[0] == "ffprobe":
                return subprocess.CompletedProcess(command, 0, "0.4\n", "")
            pattern = Path(command[-2])
            image = Image.new("L", (480, 10), 0)
            for y in range(10):
                if boundary_fixture:
                    image.putpixel((0, y), 255)
                    image.putpixel((84, y), 255)
                else:
                    for x in range(100, 200):
                        image.putpixel((x, y), 255)
            for index in (1, 2):
                image.save(str(pattern).replace("%06d", f"{index:06d}"))
            return subprocess.CompletedProcess(command, 0, "", "")

        report = root / "scan.json"
        geometry = {
            "fixed_anchor_x": 1737 if boundary_fixture else 0,
            "canvas_width": 1920,
            "source_width": 1922 if boundary_fixture else 1920,
            "source_height": 1080,
            "rendered_height": 1080,
        }
        with patch.object(presenter_layout.subprocess, "run", side_effect=fake_subprocess):
            payload = scan_rvm_body_overflow(source, report_path=report, **geometry)
        return payload, report

    def test_real_pixel_boundary_value_round_trips_without_changing_windows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload, report = self.run_pixel_scan(Path(directory), boundary_fixture=True)
            recorded = payload["samples"][0]["visible_core_fraction"]
            self.assertAlmostEqual(recorded, 0.5500222982012789)
            self.assertEqual(payload["severe_windows"], [])
            validated = validate_presenter_scan_report(report)
            self.assertEqual(validated["severe_windows"], payload["severe_windows"])

    def test_trigger_threshold_below_equal_and_above_are_unchanged(self) -> None:
        self.assertNotEqual(
            severe_body_overflow_windows([(0.0, 0.549999), (0.2, 0.69)], duration=0.4),
            [],
        )
        self.assertNotEqual(
            severe_body_overflow_windows([(0.0, 0.55), (0.2, 0.69)], duration=0.4),
            [],
        )
        self.assertEqual(
            severe_body_overflow_windows([(0.0, 0.550001), (0.2, 0.69)], duration=0.4),
            [],
        )

    def test_standard_canvas_normal_sample_persists_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload, report = self.run_pixel_scan(Path(directory), boundary_fixture=False)
            self.assertEqual(payload["severe_windows"], [])
            self.assertTrue(all(row["visible_core_fraction"] == 1.0 for row in payload["samples"]))
            validate_presenter_scan_report(report)


if __name__ == "__main__":
    unittest.main()
