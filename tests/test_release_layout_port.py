from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from release_video import ReleaseConfig, package_release_videos, render_release_previews
from story_module_adapters import LocalReleaseLayoutAdapter, MockReleaseLayoutAdapter
from story_module_ports import (
    ModuleCapabilities,
    ModuleFailureCode,
    ModuleIdentity,
    ReleaseLayoutPort,
    ReleaseLayoutRequest,
    ReleaseLayoutResult,
    release_layout_payload,
    validate_release_layout_payload,
)
from story_module_registry import build_release_layout_registry


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "module_ports" / "v1" / "release_layout_port.schema.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def schema_issues(payload: dict) -> list[str]:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    issues: list[str] = []
    for field in schema["required"]:
        if field not in payload:
            issues.append(f"missing:{field}")
    if payload.get("schema_version") != schema["properties"]["schema_version"]["const"]:
        issues.append("schema_version")
    if payload.get("kind") not in schema["properties"]["kind"]["enum"]:
        issues.append("kind")
        return issues
    branch = next(
        item["then"]
        for item in schema["allOf"]
        if item["if"]["properties"]["kind"]["const"] == payload["kind"]
    )
    for field in branch["required"]:
        if field not in payload:
            issues.append(f"missing:{field}")
    for field, rule in branch["properties"].items():
        if field not in payload:
            continue
        value = payload[field]
        allowed = rule.get("type")
        allowed = allowed if isinstance(allowed, list) else [allowed]
        matches = (
            ("null" in allowed and value is None)
            or ("boolean" in allowed and type(value) is bool)
            or ("string" in allowed and isinstance(value, str))
            or ("object" in allowed and isinstance(value, dict))
            or ("array" in allowed and isinstance(value, list))
        )
        if not matches:
            issues.append(f"type:{field}")
            continue
        item_rule = rule.get("items", {}) if isinstance(value, list) else {}
        if item_rule.get("type") == "object":
            if any(not isinstance(item, dict) for item in value):
                issues.append(f"items:{field}")
            for index, item in enumerate(value):
                if not isinstance(item, dict):
                    continue
                for name in item_rule.get("required", []):
                    if name not in item:
                        issues.append(f"missing:{field}[{index}].{name}")
                for name, nested in item_rule.get("properties", {}).items():
                    if name in item and nested.get("type") == "string" and not isinstance(item[name], str):
                        issues.append(f"type:{field}[{index}].{name}")
        if field == "output_artifact" and isinstance(value, dict):
            for name in rule.get("required", []):
                if name not in value:
                    issues.append(f"missing:output_artifact.{name}")
            for name, nested in rule.get("properties", {}).items():
                if name not in value:
                    continue
                if nested.get("type") == "string" and not isinstance(value[name], str):
                    issues.append(f"type:output_artifact.{name}")
                if nested.get("type") == "boolean" and type(value[name]) is not bool:
                    issues.append(f"type:output_artifact.{name}")
    return issues


def request_fixture(root: Path, operation: str = "main_wide_render") -> ReleaseLayoutRequest:
    root.mkdir(parents=True, exist_ok=True)
    source = root / "source.bin"
    source.write_bytes(b"release input\n")
    return ReleaseLayoutRequest(
        "release-layout:fixture",
        operation,
        ({"role": "source", "path": str(source), "sha256": sha256(source)},),
        {"variant": "main", "geometry_sha256": "a" * 64},
        root / "output.mp4",
        "attempt-1",
    )


def release_config(root: Path) -> ReleaseConfig:
    paths = {
        name: root / name
        for name in (
            "bg.mp4", "background.png", "person.mov", "audio.wav", "logo.png", "antipiracy.png",
            "frame_a.png", "frame_b.png", "story_logo.png", "subtitles.srt", "semantic_plan.json",
            "keying_preset.json", "demo_render_manifest.json",
        )
    }
    for path in paths.values():
        path.write_bytes(path.name.encode())
    paths["keying_preset.json"].with_name("keying_preset.lock.json").write_bytes(b"keying lock")
    return ReleaseConfig(
        story_name="通用故事", duration_text="2分30秒", bg_video=paths["bg.mp4"], output_dir=root / "release",
        variant="both", bg_image=paths["background.png"], person_greenscreen=paths["person.mov"],
        audio_mix=paths["audio.wav"], watermark_logo=None, antipiracy_logo=paths["antipiracy.png"],
        plate_image=None, video_box=(0, 416, 1080, 608), watermark_width=120,
        watermark_opacity=0.62, watermark_speed=0.35, frame_image=paths["frame_a.png"],
        story_box=(170, 250, 990, 557), story_bleed=0, background_blur=14,
        frame_image_b=paths["frame_b.png"], b_story_box=(150, 88, 1620, 911),
        b_windows=((10.0, 20.0),), c_windows=((30.0, 40.0),), story_logo=paths["story_logo.png"],
        story_logo_width_a=180, story_logo_width_b=210, story_logo_x=42, story_logo_y=44,
        subtitle_srt=paths["subtitles.srt"], subtitle_font_size=52, subtitle_margin_v=72,
        mix_bg_audio=True, voice_volume=1.05, bg_audio_volume=0.28,
        person_height=940, person_x=1190, person_y=100, chroma_color="0x00FF00",
        chroma_similarity=0.16, chroma_blend=0.08, keyer="chromakey", person_crop=None,
        detected_person_bbox=None, person_grade="none", person_beauty="light",
        library_watermark_text="watermark", tail_seconds=3.0, tail_notice_text="notice",
        crf=15, preset="medium", output_scale=2,
        artifact_semantic_plan=paths["semantic_plan.json"], keying_preset_path=paths["keying_preset.json"],
        demo_render_manifest=paths["demo_render_manifest.json"],
    )


def asset_fixture(root: Path) -> dict[str, Path]:
    assets: dict[str, Path] = {}
    for name in ("frame", "main_top", "main_bottom", "library_top", "library_bottom", "library_watermark", "tail_notice"):
        path = root / "assets" / f"{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
        assets[name] = path
    return assets


class ProtocolOnlyFake:
    identity = ModuleIdentity(
        "release_layout", "story-release-layout-port/v1", "protocol-fake", "protocol-fake/v1"
    )
    capabilities = ModuleCapabilities(provider="test", deterministic=True)

    def __init__(self) -> None:
        self.requests: list[ReleaseLayoutRequest] = []

    def execute(self, request: ReleaseLayoutRequest, *, executor):
        self.requests.append(request)
        return executor(request)


class ReleaseLayoutPortTests(unittest.TestCase):
    def test_schema_and_python_validator_have_parity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = request_fixture(Path(directory))
            result = MockReleaseLayoutAdapter().execute(
                request, executor=lambda _request: self.fail("mock called executor")
            )
            for kind, value in (("release_layout_request", request), ("release_layout_result", result)):
                payload = release_layout_payload(kind, value)
                self.assertEqual(validate_release_layout_payload(payload), [])
                self.assertEqual(schema_issues(payload), [])
                invalid = copy.deepcopy(payload)
                field = next(name for name in invalid if name not in {"kind", "schema_version"})
                invalid.pop(field)
                self.assertEqual(bool(validate_release_layout_payload(invalid)), bool(schema_issues(invalid)))
            nested = release_layout_payload("release_layout_request", request)
            nested["input_artifacts"][0].pop("sha256")
            self.assertTrue(validate_release_layout_payload(nested))
            self.assertTrue(schema_issues(nested))

    def test_production_adapter_delegates_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = request_fixture(Path(directory))
            calls: list[ReleaseLayoutRequest] = []

            def executor(actual: ReleaseLayoutRequest) -> ReleaseLayoutResult:
                calls.append(actual)
                actual.output_target.write_bytes(b"rendered\n")
                return ReleaseLayoutResult(
                    True, actual.operation,
                    {"path": str(actual.output_target), "sha256": sha256(actual.output_target), "production_eligible": True},
                    actual.attempt_id, "executor", "executor/v1", True,
                )

            result = LocalReleaseLayoutAdapter().execute(request, executor=executor)
            self.assertTrue(result.success)
            self.assertEqual(calls, [request])

    def test_mock_never_calls_executor_or_formal_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = request_fixture(Path(directory))
            result = MockReleaseLayoutAdapter().execute(
                request, executor=lambda _request: self.fail("mock called production executor")
            )
            self.assertTrue(result.success)
            self.assertFalse(result.production_eligible)
            self.assertFalse(request.output_target.exists())
            self.assertIn("_mock_release_layout", result.output_artifact["path"])
            forbidden = replace(request, output_target=ROOT / "主账号发布视频.mp4")
            rejected = MockReleaseLayoutAdapter().execute(
                forbidden, executor=lambda _request: self.fail("mock called production executor")
            )
            self.assertFalse(rejected.success)
            self.assertEqual(rejected.failure.code, ModuleFailureCode.CONFIGURATION_ERROR)
            self.assertFalse(forbidden.output_target.exists())

    def test_missing_or_stale_input_fails_before_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for mode in ("missing", "stale"):
                request = request_fixture(Path(directory) / mode)
                source = Path(request.input_artifacts[0]["path"])
                if mode == "missing":
                    source.unlink()
                else:
                    source.write_bytes(b"tampered")
                result = LocalReleaseLayoutAdapter().execute(
                    request, executor=lambda _request: self.fail("invalid request reached executor")
                )
                self.assertFalse(result.success)
                self.assertEqual(result.failure.code, ModuleFailureCode.INVALID_INPUT)
                self.assertFalse(request.output_target.exists())

    def test_preview_render_uses_protocol_and_preserves_names_scenes_and_geometry_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = release_config(root)
            assets = asset_fixture(root)
            preview = root / "preview"
            geometry = {"geometry_sha256": "a" * 64, "bindings": {"keying_lock_sha256": "b" * 64}}
            fake = ProtocolOnlyFake()
            main_calls: list[tuple] = []
            library_calls: list[tuple] = []

            def main(*args, **kwargs):
                main_calls.append((*args, kwargs))
                Path(args[2]).write_bytes(f"main-{args[5]}".encode())

            def library(*args, **kwargs):
                library_calls.append((*args, kwargs))
                Path(args[2]).write_bytes(b"library")

            with patch("release_video.validate_config"), \
                 patch("release_video.compile_release_geometry", return_value=geometry), \
                 patch("release_video.render_static_assets", return_value=assets), \
                 patch("release_video.render_main_preview_frame", side_effect=main), \
                 patch("release_video.render_library_preview_frame", side_effect=library):
                render_release_previews(
                    config, preview, [1.0, 20.0, 40.0], contract_spec={"consumer": "release_video"},
                    release_layout_port=fake,
                )

            self.assertIsInstance(fake, ReleaseLayoutPort)
            self.assertEqual([call[5] for call in main_calls], ["a", "b", "c"])
            self.assertEqual(len(library_calls), 3)
            self.assertEqual(
                {path.name for path in preview.glob("*.png")},
                {
                    "main_001s_a.png", "main_020s_b.png", "main_040s_c.png",
                    "library_001s.png", "library_020s.png", "library_040s.png",
                },
            )
            self.assertEqual(
                [request.operation for request in fake.requests],
                ["preview_main", "preview_main", "preview_main", "preview_library", "preview_library", "preview_library"],
            )
            persisted = json.loads((preview / "release_geometry_manifest_both.json").read_text())
            self.assertEqual(persisted, geometry)
            self.assertEqual(fake.requests[0].layout_binding["release_geometry_sha256"], "a" * 64)

    def test_full_release_uses_protocol_and_preserves_canonical_names_and_manifest_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = release_config(root)
            assets = asset_fixture(root)
            geometry = {"geometry_sha256": "a" * 64, "bindings": {"keying_lock_sha256": "b" * 64}}
            fake = ProtocolOnlyFake()
            calls: list[tuple[str, Path]] = []

            def write(label):
                def renderer(*args, **kwargs):
                    if label == "main_wide":
                        target = args[2]
                    elif label == "library_window":
                        target = kwargs["output_path"]
                    else:
                        target = kwargs["output_path"]
                    Path(target).parent.mkdir(parents=True, exist_ok=True)
                    Path(target).write_bytes(label.encode())
                    calls.append((label, Path(target)))
                return renderer

            manifest = {"consumer": "release_video", "sentinel": "original-manifest-writer"}
            with patch("release_video.validate_config"), \
                 patch("release_video.compile_release_geometry", return_value=geometry), \
                 patch("release_video.geometry_manifest_issues", return_value=[]), \
                 patch("release_video.render_static_assets", return_value=assets), \
                 patch("release_video.render_main_wide", side_effect=write("main_wide")), \
                 patch("release_video.render_library_window_video", side_effect=write("library_window")), \
                 patch("release_video.render_vertical_package", side_effect=write("vertical")), \
                 patch("release_video.probe_duration", return_value=10.0), \
                 patch("release_video.build_release_render_manifest", return_value=manifest), \
                 patch("release_video.release_render_manifest_issues", return_value=[]):
                package_release_videos(
                    config, contract_spec={"consumer": "release_video"}, release_layout_port=fake,
                )

            main = config.output_dir / "主账号发布视频.mp4"
            library = config.output_dir / "宝库号发布视频.mp4"
            self.assertTrue(main.is_file())
            self.assertTrue(library.is_file())
            self.assertEqual(
                [request.operation for request in fake.requests],
                ["main_wide_render", "main_vertical_render", "library_window_render", "library_vertical_render"],
            )
            self.assertEqual(fake.requests[1].output_target, main)
            self.assertEqual(fake.requests[3].output_target, library)
            persisted = json.loads((config.output_dir / "release_render_manifest_both.json").read_text())
            self.assertEqual(persisted, manifest)
            self.assertEqual([label for label, _target in calls], ["main_wide", "vertical", "library_window", "vertical"])

    def test_keying_and_final_geometry_gate_remain_before_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = release_config(root)
            fake = ProtocolOnlyFake()
            with patch("release_video.validate_config"), \
                 patch("release_video.compile_release_geometry", side_effect=ValueError("keying preset lock invalid")):
                with self.assertRaisesRegex(ValueError, "keying preset lock invalid"):
                    render_release_previews(
                        config, root / "preview", [1.0], contract_spec={"consumer": "release_video"},
                        release_layout_port=fake,
                    )
            self.assertEqual(fake.requests, [])

    def test_local_registry_exposes_protocol_and_policy_boundaries(self) -> None:
        port = build_release_layout_registry().release_layout()
        self.assertIsInstance(port, ReleaseLayoutPort)
        self.assertEqual(port.identity.port_name, "release_layout")
        self.assertFalse(port.capabilities.external)
        self.assertFalse(port.capabilities.paid)
        self.assertFalse(port.capabilities.supported["owns_product_policy"])
        self.assertFalse(port.capabilities.supported["owns_geometry_policy"])
        self.assertFalse(port.capabilities.supported["owns_manifest"])
        self.assertFalse(port.capabilities.supported["owns_currentness"])


if __name__ == "__main__":
    unittest.main()
