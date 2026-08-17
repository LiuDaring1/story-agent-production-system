from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from story_module_adapters import (
    CodexImageGeneratorAdapter,
    MockImageGeneratorAdapter,
    MockMusicProviderAdapter,
    SunoMusicProviderAdapter,
)
from story_module_ports import (
    ImageGeneratorPort,
    ImageGeneratorRequest,
    ImageGeneratorResult,
    ModuleCapabilities,
    ModuleFailureCode,
    ModuleIdentity,
    MusicProviderPort,
    MusicProviderRequest,
    MusicProviderResult,
    image_generator_payload,
    music_provider_payload,
    validate_image_generator_payload,
    validate_music_provider_payload,
)
from story_module_registry import (
    ALLOWED_MODULE_PROFILES,
    MODULE_EXECUTION_MODE_ENV,
    MODULE_EXECUTION_MODE_REQUIRED_ENV,
    MODULE_PROFILE_ENV,
    MODULE_PROFILE_REQUIRED_ENV,
    build_registry_for_profile,
)


ROOT = Path(__file__).resolve().parents[1]
IMAGE_SCHEMA = ROOT / "schemas" / "module_ports" / "v1" / "image_generator_port.schema.json"
MUSIC_SCHEMA = ROOT / "schemas" / "module_ports" / "v1" / "music_provider_port.schema.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def schema_issues(payload: dict, path: Path) -> list[str]:
    schema = json.loads(path.read_text(encoding="utf-8"))
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
    for field, rule in branch.get("properties", {}).items():
        if field not in payload:
            continue
        value = payload[field]
        expected = rule.get("type")
        allowed = expected if isinstance(expected, list) else [expected]
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
        if isinstance(value, list) and isinstance(rule.get("items"), dict):
            item_type = rule["items"].get("type")
            if item_type == "string" and any(not isinstance(item, str) for item in value):
                issues.append(f"items:{field}")
            if item_type == "object" and any(not isinstance(item, dict) for item in value):
                issues.append(f"items:{field}")
    return issues


def image_request(root: Path) -> ImageGeneratorRequest:
    execution_request = root / "image_request.md"
    execution_request.write_text("generate the already planned image batch\n", encoding="utf-8")
    return ImageGeneratorRequest(
        "story-images-batch-01",
        "generate_story_images",
        execution_request,
        sha256(execution_request),
        ({"role": "handoff", "path": str(root / "handoff.md"), "sha256": "a" * 64},),
        (root / "scene_01.png", root / "scene_02.png"),
        "attempt-1",
    )


def music_request(root: Path) -> MusicProviderRequest:
    execution_request = root / "music_request.md"
    execution_request.write_text("execute the already planned Suno handoff\n", encoding="utf-8")
    return MusicProviderRequest(
        "story-music-batch-01",
        "generate_music_segments",
        execution_request,
        sha256(execution_request),
        ({"role": "music_plan", "path": str(root / "music_plan.csv"), "sha256": "b" * 64},),
        (root / "01_story_music.mp3", root / "02_story_music.mp3"),
        "attempt-1",
    )


class MediaModulePortTests(unittest.TestCase):
    def test_independent_schema_and_python_validators_have_parity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_req = image_request(root)
            music_req = music_request(root)
            image_result = MockImageGeneratorAdapter().execute(
                image_req, executor=lambda _request: self.fail("mock called image executor")
            )
            music_result = MockMusicProviderAdapter().execute(
                music_req, executor=lambda _request: self.fail("mock called music executor")
            )
            examples = [
                (
                    image_generator_payload("image_generator_request", image_req),
                    IMAGE_SCHEMA,
                    validate_image_generator_payload,
                ),
                (
                    image_generator_payload("image_generator_result", image_result),
                    IMAGE_SCHEMA,
                    validate_image_generator_payload,
                ),
                (
                    music_provider_payload("music_provider_request", music_req),
                    MUSIC_SCHEMA,
                    validate_music_provider_payload,
                ),
                (
                    music_provider_payload("music_provider_result", music_result),
                    MUSIC_SCHEMA,
                    validate_music_provider_payload,
                ),
            ]
            for payload, schema, validator in examples:
                self.assertEqual(validator(payload), [], payload["kind"])
                self.assertEqual(schema_issues(payload, schema), [], payload["kind"])
                for mutation in ("missing", "wrong_type"):
                    invalid = copy.deepcopy(payload)
                    field = next(name for name in invalid if name not in {"kind", "schema_version"})
                    if mutation == "missing":
                        invalid.pop(field)
                    else:
                        invalid[field] = "false" if isinstance(invalid[field], bool) else False
                    self.assertEqual(bool(validator(invalid)), bool(schema_issues(invalid, schema)))

            invalid_targets = image_generator_payload("image_generator_request", image_req)
            invalid_targets["output_targets"] = [False]
            self.assertTrue(validate_image_generator_payload(invalid_targets))
            self.assertTrue(schema_issues(invalid_targets, IMAGE_SCHEMA))
            invalid_outputs = music_provider_payload("music_provider_result", music_result)
            invalid_outputs["output_artifacts"] = [False]
            self.assertTrue(validate_music_provider_payload(invalid_outputs))
            self.assertTrue(schema_issues(invalid_outputs, MUSIC_SCHEMA))

    def test_production_adapters_only_delegate_to_the_supplied_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_req = image_request(root)
            music_req = music_request(root)
            calls: list[object] = []

            def image_executor(request: ImageGeneratorRequest) -> ImageGeneratorResult:
                calls.append(request)
                return ImageGeneratorResult(
                    True, request.operation, (), "codex", "imagegen", "provider-image-1",
                    request.attempt_id, request.execution_request_sha256, "existing-executor/v1", True,
                )

            def music_executor(request: MusicProviderRequest) -> MusicProviderResult:
                calls.append(request)
                return MusicProviderResult(
                    True, request.operation, (), "suno", "current-browser-workflow", "provider-music-1",
                    request.attempt_id, request.execution_request_sha256, "existing-executor/v1", True,
                )

            image_result = CodexImageGeneratorAdapter().execute(image_req, executor=image_executor)
            music_result = SunoMusicProviderAdapter().execute(music_req, executor=music_executor)
            self.assertEqual(calls, [image_req, music_req])
            self.assertEqual(image_result.request_id, "provider-image-1")
            self.assertEqual(music_result.request_id, "provider-music-1")
            self.assertFalse(any(path.exists() for path in image_req.output_targets))
            self.assertFalse(any(path.exists() for path in music_req.output_targets))

    def test_mocks_are_deterministic_offline_and_never_call_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_req = image_request(root)
            music_req = music_request(root)

            def forbidden_executor(_request: object):
                raise AssertionError("offline mock must not invoke the external executor")

            image_adapter = MockImageGeneratorAdapter()
            music_adapter = MockMusicProviderAdapter()
            image_first = image_adapter.execute(image_req, executor=forbidden_executor)
            music_first = music_adapter.execute(music_req, executor=forbidden_executor)
            image_second = image_adapter.execute(image_req, executor=forbidden_executor)
            music_second = music_adapter.execute(music_req, executor=forbidden_executor)
            self.assertEqual(image_first, image_second)
            self.assertEqual(music_first, music_second)
            for path in image_req.output_targets:
                with Image.open(path) as image:
                    self.assertEqual(image.size, (256, 256))
            self.assertEqual(
                {path.read_bytes() for path in music_req.output_targets}, {b"STORY_MODULE_MOCK_MUSIC\n"}
            )
            self.assertTrue(all(item["production_eligible"] is False for item in image_first.output_artifacts))
            self.assertTrue(all(item["production_eligible"] is False for item in music_first.output_artifacts))

    def test_mock_failure_classification_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requests = (image_request(root), music_request(root))
            adapter_types = (MockImageGeneratorAdapter, MockMusicProviderAdapter)
            expected = {
                "unsupported": ModuleFailureCode.UNSUPPORTED_CAPABILITY,
                "failure": ModuleFailureCode.EXECUTION_FAILED,
                "invalid_output": ModuleFailureCode.INVALID_OUTPUT,
            }
            for request, adapter_type in zip(requests, adapter_types):
                for mode, code in expected.items():
                    with self.subTest(adapter=adapter_type.__name__, mode=mode):
                        result = adapter_type(mode).execute(
                            request, executor=lambda _request: self.fail("mock called executor")
                        )
                        self.assertFalse(result.success)
                        self.assertEqual(result.failure.code, code)

            stale = image_request(root)
            stale.execution_request_path.write_text("changed\n", encoding="utf-8")
            result = MockImageGeneratorAdapter().execute(
                stale, executor=lambda _request: self.fail("mock called executor")
            )
            self.assertEqual(result.failure.code, ModuleFailureCode.INVALID_INPUT)

    def test_protocol_only_fakes_require_no_concrete_adapter(self) -> None:
        class ImageFake:
            identity = ModuleIdentity("image_generator", "fake/v1", "fake", "fake/v1")
            capabilities = ModuleCapabilities(provider="fake", deterministic=True)

            def execute(self, request, *, executor):
                del executor
                return ImageGeneratorResult(
                    True, request.operation, (), "fake", "fixture", "fake-image-1",
                    request.attempt_id, request.execution_request_sha256, "fake/v1", True,
                )

        class MusicFake:
            identity = ModuleIdentity("music_provider", "fake/v1", "fake", "fake/v1")
            capabilities = ModuleCapabilities(provider="fake", deterministic=True)

            def execute(self, request, *, executor):
                del executor
                return MusicProviderResult(
                    True, request.operation, (), "fake", "fixture", "fake-music-1",
                    request.attempt_id, request.execution_request_sha256, "fake/v1", True,
                )

        self.assertIsInstance(ImageFake(), ImageGeneratorPort)
        self.assertIsInstance(MusicFake(), MusicProviderPort)

    def test_ports_do_not_absorb_product_quality_or_persistence_policy(self) -> None:
        forbidden = {
            "prompt_planning", "batching", "naming", "music_planning", "currentness",
            "manifest", "ledger", "qa_score", "review_verdict", "p0_errors", "assembly",
        }
        for value in (
            ImageGeneratorRequest,
            ImageGeneratorResult,
            MusicProviderRequest,
            MusicProviderResult,
            CodexImageGeneratorAdapter,
            SunoMusicProviderAdapter,
        ):
            names = set(getattr(value, "__dataclass_fields__", {})) | set(dir(value))
            self.assertFalse(forbidden & names, value)

    def test_registry_allowlists_media_mocks_and_keeps_production_default(self) -> None:
        self.assertTrue({"mock-image", "mock-music"}.issubset(ALLOWED_MODULE_PROFILES))
        with patch.dict(os.environ, {}, clear=True):
            production = build_registry_for_profile("production-default")
        self.assertIsInstance(production.image_generator(), CodexImageGeneratorAdapter)
        self.assertIsInstance(production.music_provider(), SunoMusicProviderAdapter)
        self.assertEqual(production.selection_execution_mode(), "production")

    def test_media_mock_dual_lock_selects_one_allowlisted_module_only(self) -> None:
        for profile, selected_type, other_type in (
            ("mock-image", MockImageGeneratorAdapter, SunoMusicProviderAdapter),
            ("mock-music", MockMusicProviderAdapter, CodexImageGeneratorAdapter),
        ):
            env = {
                MODULE_PROFILE_ENV: profile,
                MODULE_PROFILE_REQUIRED_ENV: profile,
                MODULE_EXECUTION_MODE_ENV: "test",
                MODULE_EXECUTION_MODE_REQUIRED_ENV: "test",
            }
            with self.subTest(profile=profile), patch.dict(os.environ, env, clear=True):
                registry = build_registry_for_profile(profile, execution_mode="test")
                selected = registry.image_generator() if profile == "mock-image" else registry.music_provider()
                other = registry.music_provider() if profile == "mock-image" else registry.image_generator()
                self.assertIsInstance(selected, selected_type)
                self.assertIsInstance(other, other_type)
                self.assertEqual(registry.selection_profile(), profile)
                self.assertEqual(registry.selection_execution_mode(), "test")

    def test_media_mock_selection_fails_closed_for_missing_mismatched_or_illegal_locks(self) -> None:
        cases = (
            (
                "selected profile missing",
                "",
                "test",
                {MODULE_PROFILE_REQUIRED_ENV: "mock-image"},
            ),
            (
                "required profile missing",
                "mock-image",
                "test",
                {
                    MODULE_EXECUTION_MODE_ENV: "test",
                    MODULE_EXECUTION_MODE_REQUIRED_ENV: "test",
                },
            ),
            (
                "profile mismatch",
                "mock-image",
                "test",
                {MODULE_PROFILE_REQUIRED_ENV: "mock-music"},
            ),
            (
                "selected test guard missing",
                "mock-image",
                "",
                {
                    MODULE_PROFILE_REQUIRED_ENV: "mock-image",
                    MODULE_EXECUTION_MODE_REQUIRED_ENV: "test",
                },
            ),
            (
                "required test guard missing",
                "mock-image",
                "test",
                {MODULE_PROFILE_REQUIRED_ENV: "mock-image"},
            ),
            (
                "test guard invalid",
                "mock-image",
                "unsafe",
                {
                    MODULE_PROFILE_REQUIRED_ENV: "mock-image",
                    MODULE_EXECUTION_MODE_REQUIRED_ENV: "test",
                },
            ),
            (
                "test guard mismatch",
                "mock-music",
                "test",
                {
                    MODULE_PROFILE_REQUIRED_ENV: "mock-music",
                    MODULE_EXECUTION_MODE_REQUIRED_ENV: "production",
                },
            ),
            (
                "production mode cannot activate external mock",
                "mock-image",
                "production",
                {
                    MODULE_PROFILE_REQUIRED_ENV: "mock-image",
                    MODULE_EXECUTION_MODE_REQUIRED_ENV: "production",
                },
            ),
        )
        for label, profile, mode, env in cases:
            with self.subTest(label=label), patch.dict(os.environ, env, clear=True):
                with self.assertRaises((RuntimeError, ValueError)):
                    build_registry_for_profile(profile, execution_mode=mode)

    def test_media_mock_registry_never_calls_executor_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = {"image": 0, "music": 0}

            def image_sentinel(_request: object):
                calls["image"] += 1
                self.fail("mock-image touched the real executor")

            def music_sentinel(_request: object):
                calls["music"] += 1
                self.fail("mock-music touched the real executor")

            image_env = {
                MODULE_PROFILE_ENV: "mock-image",
                MODULE_PROFILE_REQUIRED_ENV: "mock-image",
                MODULE_EXECUTION_MODE_ENV: "test",
                MODULE_EXECUTION_MODE_REQUIRED_ENV: "test",
            }
            with patch.dict(os.environ, image_env, clear=True):
                image_result = build_registry_for_profile(
                    "mock-image", execution_mode="test"
                ).image_generator().execute(image_request(root), executor=image_sentinel)
            music_env = {
                MODULE_PROFILE_ENV: "mock-music",
                MODULE_PROFILE_REQUIRED_ENV: "mock-music",
                MODULE_EXECUTION_MODE_ENV: "test",
                MODULE_EXECUTION_MODE_REQUIRED_ENV: "test",
            }
            with patch.dict(os.environ, music_env, clear=True):
                music_result = build_registry_for_profile(
                    "mock-music", execution_mode="test"
                ).music_provider().execute(music_request(root), executor=music_sentinel)
            self.assertEqual(calls, {"image": 0, "music": 0})
            self.assertEqual(image_result.provider, "mock")
            self.assertEqual(music_result.provider, "mock")
            self.assertFalse(image_result.production_eligible)
            self.assertFalse(music_result.production_eligible)
            self.assertTrue(all(item["production_eligible"] is False for item in image_result.output_artifacts))
            self.assertTrue(all(item["production_eligible"] is False for item in music_result.output_artifacts))

    def test_child_missing_either_required_lock_fails_before_diagnostics(self) -> None:
        complete = {
            MODULE_PROFILE_ENV: "mock-image",
            MODULE_PROFILE_REQUIRED_ENV: "mock-image",
            MODULE_EXECUTION_MODE_ENV: "test",
            MODULE_EXECUTION_MODE_REQUIRED_ENV: "test",
        }
        for missing, expected in (
            (MODULE_PROFILE_REQUIRED_ENV, "required module profile"),
            (MODULE_EXECUTION_MODE_REQUIRED_ENV, "required test execution mode"),
        ):
            env = os.environ.copy()
            env.update(complete)
            env.pop(missing, None)
            process = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "story_module_registry.py"),
                    "--profile",
                    "mock-image",
                    "--execution-mode",
                    "test",
                    "list",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(process.returncode, 0)
            self.assertIn(expected, process.stderr + process.stdout)


if __name__ == "__main__":
    unittest.main()
