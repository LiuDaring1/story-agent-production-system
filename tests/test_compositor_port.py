from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from product_package import (
    KeyingPreset,
    _render_a_only_background_video_core,
    _render_demo_video_core,
    render_a_only_background_video,
    render_demo_video,
)
from story_module_adapters import LocalCompositorAdapter, MockCompositorAdapter
from story_module_ports import (
    CompositorPort,
    CompositorRequest,
    CompositorResult,
    ModuleCapabilities,
    ModuleFailureCode,
    ModuleIdentity,
    compositor_payload,
    validate_compositor_payload,
)
from story_module_registry import build_compositor_registry
from story_video_synthesizer.align import LineTiming
from story_video_synthesizer.pipeline import SynthesisConfig, synthesize_story


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "module_ports" / "v1" / "compositor_port.schema.json"


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
        item_type = item_rule.get("type")
        if item_type == "string" and any(not isinstance(item, str) for item in value):
            issues.append(f"items:{field}")
        if item_type == "object":
            if any(not isinstance(item, dict) for item in value):
                issues.append(f"items:{field}")
            for index, item in enumerate(value):
                if not isinstance(item, dict):
                    continue
                for name in item_rule.get("required", []):
                    if name not in item:
                        issues.append(f"missing:{field}[{index}].{name}")
                for name, nested in item_rule.get("properties", {}).items():
                    if name not in item:
                        continue
                    if nested.get("type") == "string" and not isinstance(item[name], str):
                        issues.append(f"type:{field}[{index}].{name}")
                    if nested.get("type") == "boolean" and type(item[name]) is not bool:
                        issues.append(f"type:{field}[{index}].{name}")
    return issues


def compositor_request(root: Path, operation: str = "presenter_demo") -> CompositorRequest:
    root.mkdir(parents=True, exist_ok=True)
    source = root / "input.bin"
    source.write_bytes(b"compositor input\n")
    return CompositorRequest(
        "compositor:fixture",
        operation,
        ({"role": "fixture", "path": str(source), "sha256": sha256(source)},),
        (root / "output.mp4",),
        {"width": 1920, "height": 1080},
        "attempt-1",
    )


class ProtocolOnlyFake:
    identity = ModuleIdentity(
        "compositor", "story-compositor-port/v1", "protocol-fake", "protocol-fake/v1"
    )
    capabilities = ModuleCapabilities(provider="test", deterministic=True)

    def __init__(self) -> None:
        self.requests: list[CompositorRequest] = []

    def execute(self, request: CompositorRequest, *, executor):
        self.requests.append(request)
        return executor(request)


class CompositorPortTests(unittest.TestCase):
    def test_schema_and_python_validator_have_parity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = compositor_request(Path(directory))
            result = MockCompositorAdapter().execute(
                request, executor=lambda _request: self.fail("mock called executor")
            )
            for kind, value in (("compositor_request", request), ("compositor_result", result)):
                payload = compositor_payload(kind, value)
                self.assertEqual(validate_compositor_payload(payload), [])
                self.assertEqual(schema_issues(payload), [])
                invalid = copy.deepcopy(payload)
                field = next(name for name in invalid if name not in {"kind", "schema_version"})
                invalid.pop(field)
                self.assertEqual(bool(validate_compositor_payload(invalid)), bool(schema_issues(invalid)))
            invalid_binding = compositor_payload("compositor_request", request)
            invalid_binding["input_artifacts"][0].pop("sha256")
            self.assertTrue(validate_compositor_payload(invalid_binding))
            self.assertTrue(schema_issues(invalid_binding))

    def test_production_adapter_delegates_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = compositor_request(Path(directory))
            calls: list[CompositorRequest] = []

            def executor(actual: CompositorRequest) -> CompositorResult:
                calls.append(actual)
                target = actual.output_targets[0]
                target.write_bytes(b"rendered\n")
                return CompositorResult(
                    True, actual.operation,
                    ({"path": str(target), "sha256": sha256(target), "production_eligible": True},),
                    actual.attempt_id, "executor/v1", True,
                )

            result = LocalCompositorAdapter().execute(request, executor=executor)
            self.assertTrue(result.success)
            self.assertEqual(calls, [request])
            self.assertEqual(result.output_artifacts[0]["path"], str(request.output_targets[0]))

    def test_mock_never_calls_executor_or_requested_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = compositor_request(Path(directory))
            result = MockCompositorAdapter().execute(
                request, executor=lambda _request: self.fail("mock called production executor")
            )
            self.assertTrue(result.success)
            self.assertFalse(result.production_eligible)
            self.assertFalse(request.output_targets[0].exists())
            self.assertIn("_mock_compositor", result.output_artifacts[0]["path"])

            forbidden = replace(request, output_targets=(ROOT / "forbidden-compositor.mp4",))
            rejected = MockCompositorAdapter().execute(
                forbidden, executor=lambda _request: self.fail("mock called production executor")
            )
            self.assertFalse(rejected.success)
            self.assertEqual(rejected.failure.code, ModuleFailureCode.CONFIGURATION_ERROR)
            self.assertFalse(forbidden.output_targets[0].exists())

    def test_missing_or_stale_input_fails_before_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for mode in ("missing", "stale"):
                request = compositor_request(Path(directory) / mode)
                source = Path(str(request.input_artifacts[0]["path"]))
                if mode == "missing":
                    source.unlink()
                else:
                    source.write_bytes(b"tampered\n")
                result = LocalCompositorAdapter().execute(
                    request, executor=lambda _request: self.fail("invalid input reached executor")
                )
                self.assertFalse(result.success)
                self.assertEqual(result.failure.code, ModuleFailureCode.INVALID_INPUT)
                self.assertFalse(request.output_targets[0].exists())

    def test_background_story_uses_protocol_and_preserves_four_outputs_and_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video_dir = root / "videos"
            video_dir.mkdir()
            (video_dir / "01.mp4").write_bytes(b"video")
            script = root / "script.txt"
            script.write_text("故事正文。\n", encoding="utf-8")
            narration = root / "narration.wav"
            music = root / "music.mp3"
            narration.write_bytes(b"voice")
            music.write_bytes(b"music")
            output = root / "assembly"
            config = SynthesisConfig(
                video_dir=video_dir, script_path=script, narration_path=narration,
                music_path=music, output_dir=output, keep_workdir=True,
            )
            timing = LineTiming(1, "故事正文。", 0, 2, 2, 0, 2)
            fake = ProtocolOnlyFake()

            def touch_target(*args, **kwargs):
                target = kwargs.get("output_path")
                if target is None:
                    target = args[2] if len(args) > 2 else args[1]
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                Path(target).touch()

            with patch("story_video_synthesizer.pipeline._validate_tools"), \
                 patch("story_video_synthesizer.pipeline.align_script_to_narration", return_value=[timing]), \
                 patch("story_video_synthesizer.pipeline.probe_duration", return_value=2.0), \
                 patch("story_video_synthesizer.pipeline._render_video_segments", return_value=[output / "_work" / "segment.mp4"]), \
                 patch("story_video_synthesizer.pipeline._concat_videos", side_effect=touch_target), \
                 patch("story_video_synthesizer.pipeline._burn_subtitles_only", side_effect=touch_target), \
                 patch("story_video_synthesizer.pipeline._mux_with_music", side_effect=touch_target), \
                 patch("story_video_synthesizer.pipeline._mux_with_voice_music", side_effect=touch_target):
                result = synthesize_story(config, compositor_port=fake)

            self.assertIsInstance(fake, CompositorPort)
            self.assertEqual([request.operation for request in fake.requests], ["background_story"])
            self.assertEqual(
                [result.no_subs_bgm.name, result.subs_bgm.name, result.sales_subs_bgm.name, result.demo_voice_bgm.name],
                ["story_no_subs_bgm.mp4", "story_subs_bgm.mp4", "story_sales_subs_bgm.mp4", "story_demo_voice_bgm.mp4"],
            )
            for path in (
                result.no_subs_bgm, result.subs_bgm, result.sales_subs_bgm, result.demo_voice_bgm,
                result.timings_json, result.subtitles_srt, result.sales_subtitles_srt,
                output / "story_semantic_timeline.srt",
            ):
                self.assertTrue(path.is_file(), path)
            self.assertEqual(fake.requests[0].execution_binding["music_volume"], config.music_volume)

    def test_presenter_demo_protocol_wrapper_preserves_ffmpeg_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {name: root / name for name in ("person.mov", "background.png", "voice.wav", "music.mp3", "subs.srt", "logo.png")}
            for path in paths.values():
                path.write_bytes(path.name.encode())
            output = root / "示范表演：通用故事.mp4"
            preset = KeyingPreset(person_height_ratio=0.9)
            commands: list[list[str]] = []

            def background(_source, target, *_args, **_kwargs):
                Path(target).write_bytes(b"background")

            def overlay(_source, target, *_args, **_kwargs):
                Path(target).write_bytes(b"overlay")

            def run(command):
                commands.append(list(command))
                output.write_bytes(b"demo")

            patches = (
                patch("product_package.probe_duration", return_value=10.0),
                patch("product_package.make_blurred_background", side_effect=background),
                patch("product_package.validate_demo_blurred_background"),
                patch("product_package.render_subtitle_overlay", side_effect=overlay),
                patch("product_package.demo_crop_filter", return_value=""),
                patch("product_package.keying_filter_chain", return_value="[1:v]null[person_keyed]"),
                patch("product_package.preserve_native_composition", return_value=False),
                patch("product_package.run_command", side_effect=run),
            )
            for item in patches:
                item.start()
            try:
                _render_demo_video_core(
                    paths["person.mov"], paths["background.png"], paths["voice.wav"], paths["music.mp3"],
                    paths["subs.srt"], output, preset, 1920, 1080, 20, "veryfast", 0.22, 1.0, 0.02,
                    logo_path=paths["logo.png"],
                )
                direct_command = commands.pop()
                output.unlink()
                fake = ProtocolOnlyFake()
                render_demo_video(
                    paths["person.mov"], paths["background.png"], paths["voice.wav"], paths["music.mp3"],
                    paths["subs.srt"], output, preset, 1920, 1080, 20, "veryfast", 0.22, 1.0, 0.02,
                    logo_path=paths["logo.png"], compositor_port=fake,
                )
                self.assertEqual(commands.pop(), direct_command)
                self.assertEqual(fake.requests[0].operation, "presenter_demo")
                self.assertEqual(fake.requests[0].output_targets, (output,))
            finally:
                for item in reversed(patches):
                    item.stop()

    def test_a_only_protocol_wrapper_preserves_ffmpeg_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bg_video = root / "story_no_subs_bgm.mp4"
            background = root / "background.png"
            frame = root / "frame.png"
            preset = root / "keying.json"
            for path in (bg_video, background, frame):
                path.write_bytes(path.name.encode())
            preset.write_text("{}\n", encoding="utf-8")
            output = root / "A镜无人物背景视频：通用故事.mp4"
            commands: list[list[str]] = []

            def prepare(_frame, _box, prepared, mask, _bleed):
                Path(prepared).write_bytes(b"prepared")
                Path(mask).write_bytes(b"mask")
                return Path(prepared), Path(mask), (10, 20, 300, 200)

            def run(command):
                commands.append(list(command))
                output.write_bytes(b"a-only")

            with patch("product_package.probe_duration", return_value=10.0), \
                 patch("release_video.prepare_story_frame_assets", side_effect=prepare), \
                 patch("product_package.run_command", side_effect=run):
                _render_a_only_background_video_core(
                    bg_video, background, frame, output, preset, 1920, 1080, 20, "veryfast"
                )
                direct_command = commands.pop()
                output.unlink()
                fake = ProtocolOnlyFake()
                render_a_only_background_video(
                    bg_video, background, frame, output, preset, 1920, 1080, 20, "veryfast",
                    compositor_port=fake,
                )
                self.assertEqual(commands.pop(), direct_command)
                self.assertEqual(fake.requests[0].operation, "a_only_background")
                self.assertEqual(fake.requests[0].output_targets, (output,))

    def test_ffmpeg_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = compositor_request(Path(directory), "a_only_background")
            result = LocalCompositorAdapter().execute(
                request, executor=lambda _request: (_ for _ in ()).throw(RuntimeError("ffmpeg failed"))
            )
            self.assertFalse(result.success)
            self.assertEqual(result.failure.code, ModuleFailureCode.EXECUTION_FAILED)
            self.assertIn("ffmpeg failed", result.failure.message)
            self.assertFalse(request.output_targets[0].exists())

    def test_local_registry_exposes_protocol_and_policy_boundaries(self) -> None:
        port = build_compositor_registry().compositor()
        self.assertIsInstance(port, CompositorPort)
        self.assertEqual(port.identity.port_name, "compositor")
        self.assertFalse(port.capabilities.external)
        self.assertFalse(port.capabilities.paid)
        self.assertFalse(port.capabilities.supported["owns_timeline_policy"])
        self.assertFalse(port.capabilities.supported["owns_manifest"])
        self.assertFalse(port.capabilities.supported["owns_currentness"])


if __name__ == "__main__":
    unittest.main()
