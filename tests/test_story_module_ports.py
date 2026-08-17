from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops

from production_keying import (
    production_keying_contract,
    production_keying_filter_chain,
    production_keying_fingerprint,
    render_production_keyed_foreground,
)
from story_module_adapters import (
    ExistingVideoGeneratorAdapter,
    MockKeyerAdapter,
    MockVideoGeneratorAdapter,
    ProductionKeyerAdapter,
)
from story_module_ports import (
    KeyerRequest,
    ModuleCapabilities,
    ModuleFailure,
    ModuleFailureCode,
    ModuleIdentity,
    ModuleUsageEvent,
    VideoGeneratorRequest,
    VideoGeneratorResult,
    module_payload,
    validate_module_payload,
)
from story_module_registry import ModuleRegistry, build_default_registry, load_pipeline_config
from story_agent import StoryAgent
from video_provider_adapter import VideoProviderAdapter, resolve_row_generation_seconds, resolve_video_provider


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "module_ports" / "v1" / "module_ports.schema.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def schema_issues(payload: dict) -> list[str]:
    """Evaluate the deliberately small JSON-Schema subset used by the Port schema."""

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
    kind = payload["kind"]
    branch = next(
        item["then"] for item in schema["allOf"]
        if item["if"]["properties"]["kind"]["const"] == kind
    )
    for field in branch["required"]:
        if field not in payload:
            issues.append(f"missing:{field}")
    for field, rule in branch.get("properties", {}).items():
        if field not in payload:
            continue
        value = payload[field]
        if "enum" in rule and value not in rule["enum"]:
            issues.append(field)
        expected = rule.get("type")
        if expected is None:
            continue
        allowed = expected if isinstance(expected, list) else [expected]
        matches = (
            ("null" in allowed and value is None)
            or ("boolean" in allowed and type(value) is bool)
            or ("string" in allowed and isinstance(value, str))
            or ("object" in allowed and isinstance(value, dict))
            or ("number" in allowed and not isinstance(value, bool) and isinstance(value, (int, float)))
        )
        if not matches:
            issues.append(f"type:{field}")
    return issues


def video_request(root: Path) -> VideoGeneratorRequest:
    source = root / "source.png"
    Image.new("RGB", (32, 18), (20, 120, 80)).save(source)
    prompt = "character_a walks calmly"
    return VideoGeneratorRequest(
        artifact_id="scene-01", scene_id="01", source_image_path=source,
        source_image_sha256=sha256(source), prompt=prompt,
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(), requested_duration=8,
        requested_ratio="16:9", requested_resolution="720p", output_target=root / "01.mp4",
        attempt_id="attempt-1", story_contract_sha256="a" * 64,
        story_contract_dependency_sha256="b" * 64, motion_plan_sha256="c" * 64,
    )


class StoryModulePortTests(unittest.TestCase):
    def test_schema_and_python_validator_accept_and_reject_the_same_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = video_request(root)
            examples = [
                module_payload("identity", ModuleIdentity("keyer", "v1", "adapter", "a1")),
                module_payload("capabilities", ModuleCapabilities(provider="local", deterministic=True)),
                module_payload("failure", ModuleFailure(ModuleFailureCode.INVALID_INPUT, "bad")),
                module_payload("usage_event", ModuleUsageEvent("local", "ffmpeg", "render")),
                module_payload("video_request", request),
                module_payload(
                    "video_result",
                    VideoGeneratorResult(True, request.output_target, "d" * 64, "mock", "fixture", "r1", 8, 8, "a1", request.source_image_sha256, request.prompt_sha256, "v1"),
                ),
                module_payload(
                    "keyer_request",
                    KeyerRequest("key-1", request.source_image_path, request.source_image_sha256, {}, root / "fg.png", "a1"),
                ),
            ]
            key_result = MockKeyerAdapter().render(
                KeyerRequest("key-1", request.source_image_path, request.source_image_sha256, {}, root / "fg.png", "a1")
            )
            examples.append(module_payload("keyer_result", key_result))
            for payload in examples:
                self.assertEqual(validate_module_payload(payload), [], payload["kind"])
                self.assertEqual(schema_issues(payload), [], payload["kind"])
                for mutation in ("missing", "wrong_type"):
                    invalid = copy.deepcopy(payload)
                    field = next(name for name in invalid if name not in {"kind", "schema_version"})
                    if mutation == "missing":
                        invalid.pop(field)
                    else:
                        invalid[field] = False if not isinstance(invalid[field], bool) else "false"
                    self.assertEqual(bool(validate_module_payload(invalid)), bool(schema_issues(invalid)))

    def test_video_adapter_is_behaviorally_equal_to_existing_provider_config(self) -> None:
        config = load_pipeline_config(ROOT)
        legacy = resolve_video_provider(config, ROOT)
        port = build_default_registry(config, ROOT).video_generator()
        self.assertEqual(port.identity.adapter_name, legacy.name)
        self.assertEqual(port.provider_config, legacy)
        self.assertEqual(port.model, legacy.model)
        self.assertEqual(port.runner, legacy.runner)
        self.assertEqual(port.provider_config.base_url, legacy.base_url)
        self.assertEqual(port.provider_config.api_key_env, legacy.api_key_env)
        self.assertEqual(port.default_seconds, legacy.default_seconds)
        self.assertEqual(port.min_seconds, legacy.min_seconds)
        self.assertEqual(port.max_seconds, legacy.max_seconds)
        self.assertEqual(port.capabilities.supported["resolution"], legacy.default_resolution)
        self.assertEqual(port.capabilities.supported["ratio"], legacy.default_ratio)
        self.assertEqual(port.estimate_cost(9), legacy.estimate_cost(9))
        self.assertEqual(port.runner_args(), legacy.runner_args())
        row = {"generation_duration": "8.2", "duration": "5"}
        self.assertEqual(
            port.resolve_request_seconds(row, 8),
            resolve_row_generation_seconds(row, model=legacy.model, fallback_seconds=8, min_seconds=legacy.min_seconds, max_seconds=legacy.max_seconds),
        )

    def test_legacy_video_duration_behavior_is_unchanged(self) -> None:
        provider = VideoProviderAdapter("legacy", ROOT / "run_image_video_jobs.py", "grok-video-3", "", "", 1.2)
        port = ExistingVideoGeneratorAdapter(provider)
        self.assertEqual(port.resolve_request_seconds({"generation_duration": "5"}, "provider-default"), "provider-default")

    def test_video_mock_swaps_without_changing_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = video_request(root)

            def consume(registry: ModuleRegistry) -> VideoGeneratorResult:
                return registry.video_generator().generate(request)

            registry = ModuleRegistry()
            registry.register("video_generator", MockVideoGeneratorAdapter())
            self.assertTrue(consume(registry).success)
            registry.register("video_generator", MockVideoGeneratorAdapter("unsupported"))
            result = consume(registry)
            self.assertEqual(result.failure.code, ModuleFailureCode.UNSUPPORTED_CAPABILITY)
            registry.register("video_generator", MockVideoGeneratorAdapter("failure"))
            self.assertEqual(consume(registry).failure.code, ModuleFailureCode.EXECUTION_FAILED)
            registry.register("video_generator", MockVideoGeneratorAdapter("invalid_output"))
            self.assertEqual(consume(registry).failure.code, ModuleFailureCode.INVALID_OUTPUT)

    def test_story_agent_accepts_registry_injection_without_global_configuration(self) -> None:
        registry = ModuleRegistry()
        registry.register("video_generator", MockVideoGeneratorAdapter())
        registry.register("keyer", MockKeyerAdapter())
        agent = StoryAgent.__new__(StoryAgent)
        agent._module_registry = registry
        self.assertIs(agent._modules(), registry)
        self.assertEqual(agent._provider_for_stage("generate_videos"), "mock-video")
        self.assertIs(agent._modules().keyer(), registry.keyer())

    def test_keyer_adapter_delegates_every_production_fact(self) -> None:
        settings = {
            "keyer": "colorkey", "chroma_color": "0x14DC1E", "chroma_similarity": 0.08,
            "chroma_blend": 0.04, "person_grade": "natural", "person_beauty": "light",
            "person_crop": [2, 2, 24, 14],
        }
        port = ProductionKeyerAdapter()
        self.assertEqual(port.compile_contract(settings), production_keying_contract(settings))
        self.assertEqual(port.filter_chain("[source]", settings, "crop=24:14:2:2,"), production_keying_filter_chain("[source]", settings, "crop=24:14:2:2,"))
        self.assertEqual(port.fingerprint(settings), production_keying_fingerprint(settings))
        chroma = dict(settings, keyer="chromakey", person_crop=None)
        self.assertEqual(port.filter_chain("[source]", chroma), production_keying_filter_chain("[source]", chroma))

    def test_keyer_render_is_pixel_equivalent_and_source_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "green.png"
            image = Image.new("RGB", (40, 30), (0, 255, 0))
            for x in range(10, 30):
                for y in range(5, 28):
                    image.putpixel((x, y), (190, 130, 90))
            image.save(source)
            before = sha256(source)
            settings = {"keyer": "colorkey", "chroma_color": "0x00FF00", "chroma_similarity": 0.1, "chroma_blend": 0.0}
            old_output, port_output = root / "old.png", root / "port.png"
            render_production_keyed_foreground(source, old_output, settings)
            request = KeyerRequest("fg", source, before, settings, port_output, "attempt-1")
            result = ProductionKeyerAdapter().render(request)
            self.assertTrue(result.success)
            with Image.open(old_output) as expected, Image.open(port_output) as actual:
                self.assertIsNone(ImageChops.difference(expected.convert("RGBA"), actual.convert("RGBA")).getbbox())
            self.assertEqual(sha256(source), before)

    def test_keyer_mock_swaps_without_changing_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            Image.new("RGBA", (8, 8), (1, 2, 3, 255)).save(source)
            request = KeyerRequest("fg", source, sha256(source), {}, root / "out.png", "a1")

            def consume(registry: ModuleRegistry):
                return registry.keyer().render(request)

            registry = ModuleRegistry()
            registry.register("keyer", MockKeyerAdapter())
            self.assertTrue(consume(registry).success)
            registry.register("keyer", MockKeyerAdapter("failure"))
            self.assertEqual(consume(registry).failure.code, ModuleFailureCode.EXECUTION_FAILED)

    def test_port_boundaries_do_not_contain_quality_policy(self) -> None:
        video_names = set(dir(ExistingVideoGeneratorAdapter))
        keyer_names = set(dir(ProductionKeyerAdapter))
        self.assertFalse({"motion_score", "static_threshold", "handoff_naturalness"} & video_names)
        self.assertFalse({"hair_quality", "spill_severity", "halo_p0", "review_verdict"} & keyer_names)

    def test_diagnostics_do_not_include_secret_values(self) -> None:
        payload = json.dumps(build_default_registry().list_descriptions(), ensure_ascii=False)
        self.assertNotIn("api_key", payload.lower())
        self.assertNotIn("secret", payload.lower())


if __name__ == "__main__":
    unittest.main()
