from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from cover_quality import COVER_GRAPH, render_required_covers
from publish_package import extract_candidate_frames, render_contact_sheet
from story_module_adapters import LocalPublishAssetAdapter, MockPublishAssetAdapter
from story_module_ports import (
    ModuleCapabilities,
    ModuleFailureCode,
    ModuleIdentity,
    PublishAssetPort,
    PublishAssetRequest,
    PublishAssetResult,
    publish_asset_payload,
    validate_publish_asset_payload,
)
from story_module_registry import build_publish_asset_registry
from story_project import project_paths
from tests.test_publish_qa import make_required_cover_fixture


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "module_ports" / "v1" / "publish_asset_port.schema.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def request_fixture(root: Path) -> PublishAssetRequest:
    root.mkdir(parents=True, exist_ok=True)
    source = root / "source.png"
    source.write_bytes(b"publish-input")
    return PublishAssetRequest(
        artifact_id="publish:test",
        operation="final_cover_render",
        input_artifacts=({"role": "creative_base", "path": str(source), "sha256": sha256(source)},),
        execution_binding={"ratio": "4:3", "compiled_cover_spec_sha256": "a" * 64},
        output_targets=(root / "cover_4x3.png",),
        attempt_id="attempt-1",
    )


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
        item["then"] for item in schema["allOf"]
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
            for index, item in enumerate(value):
                if not isinstance(item, dict):
                    issues.append(f"items:{field}")
                    continue
                for name in item_rule.get("required", []):
                    if name not in item:
                        issues.append(f"missing:{field}[{index}].{name}")
    return issues


class ProtocolOnlyFake:
    identity = ModuleIdentity(
        "publish_asset", "story-publish-asset-port/v1", "protocol-only", "protocol-only/v1"
    )
    capabilities = ModuleCapabilities(
        provider="test", model_or_tool="protocol", runner_or_tool="in-process",
        deterministic=True,
    )

    def __init__(self) -> None:
        self.requests: list[PublishAssetRequest] = []

    def execute(self, request: PublishAssetRequest, *, executor):
        self.requests.append(request)
        return executor(request)


class PublishAssetPortTests(unittest.TestCase):
    def test_schema_python_validator_parity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = request_fixture(Path(directory))
            output = request.output_targets[0]
            output.write_bytes(b"cover")
            result = PublishAssetResult(
                True, request.operation,
                ({"path": str(output), "sha256": sha256(output), "production_eligible": True},),
                request.attempt_id, "local-publish-asset", "adapter/v1", True,
            )
            for kind, value in (
                ("publish_asset_request", request),
                ("publish_asset_result", result),
            ):
                payload = publish_asset_payload(kind, value)
                self.assertEqual(validate_publish_asset_payload(payload), [])
                self.assertEqual(schema_issues(payload), [])
                invalid = copy.deepcopy(payload)
                field = next(name for name in invalid if name not in {"kind", "schema_version"})
                invalid.pop(field)
                self.assertEqual(bool(validate_publish_asset_payload(invalid)), bool(schema_issues(invalid)))
            nested = publish_asset_payload("publish_asset_request", request)
            nested["input_artifacts"][0].pop("sha256")
            self.assertTrue(validate_publish_asset_payload(nested))
            self.assertTrue(schema_issues(nested))

    def test_production_adapter_exact_delegation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = request_fixture(Path(directory))
            calls: list[PublishAssetRequest] = []

            def executor(actual: PublishAssetRequest) -> PublishAssetResult:
                calls.append(actual)
                actual.output_targets[0].write_bytes(b"cover")
                return PublishAssetResult(
                    True, actual.operation,
                    ({"path": str(actual.output_targets[0]), "sha256": sha256(actual.output_targets[0]), "production_eligible": True},),
                    actual.attempt_id, "executor", "executor/v1", True,
                )

            result = LocalPublishAssetAdapter().execute(request, executor=executor)
            self.assertTrue(result.success)
            self.assertEqual(calls, [request])

    def test_mock_never_calls_executor_codex_or_imagegen(self) -> None:
        calls = {"executor": 0, "codex": 0, "imagegen": 0}
        with tempfile.TemporaryDirectory() as directory:
            request = request_fixture(Path(directory))

            def forbidden(kind: str):
                calls[kind] += 1
                self.fail(f"mock called {kind}")

            result = MockPublishAssetAdapter().execute(
                request, executor=lambda _request: forbidden("executor")
            )
            self.assertTrue(result.success)
            self.assertFalse(result.production_eligible)
            self.assertFalse(request.output_targets[0].exists())
            self.assertIn("_mock_publish_asset", result.output_artifacts[0]["path"])
            self.assertEqual(calls, {"executor": 0, "codex": 0, "imagegen": 0})
            forbidden_target = replace(request, output_targets=(ROOT / "cover_4x3.png",))
            rejected = MockPublishAssetAdapter().execute(
                forbidden_target, executor=lambda _request: forbidden("executor")
            )
            self.assertFalse(rejected.success)
            self.assertEqual(rejected.failure.code, ModuleFailureCode.CONFIGURATION_ERROR)

    def test_missing_or_stale_input_fails_before_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for mode in ("missing", "stale"):
                request = request_fixture(Path(directory) / mode)
                source = Path(request.input_artifacts[0]["path"])
                if mode == "missing":
                    source.unlink()
                else:
                    source.write_bytes(b"tampered")
                result = LocalPublishAssetAdapter().execute(
                    request, executor=lambda _request: self.fail("invalid request reached executor")
                )
                self.assertFalse(result.success)
                self.assertEqual(result.failure.code, ModuleFailureCode.INVALID_INPUT)
                self.assertFalse(request.output_targets[0].exists())

    def test_reference_frames_and_contact_sheet_use_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "release.mp4"
            video.write_bytes(b"offline-video")
            frames = root / "frames"
            contact = root / "候选帧索引.jpg"
            fake = ProtocolOnlyFake()
            ffmpeg_calls: list[list[str]] = []

            def fake_run(command, **_kwargs):
                ffmpeg_calls.append(command)
                Image.new("RGB", (40, 60), (len(ffmpeg_calls) * 20, 30, 40)).save(Path(command[-1]))

            with patch("publish_package.probe_duration", return_value=90.0), \
                 patch("publish_package.subprocess.run", side_effect=fake_run):
                extract_candidate_frames(video, frames, 3, publish_asset_port=fake)
            render_contact_sheet(frames, contact, "主账号", publish_asset_port=fake)

            self.assertIsInstance(fake, PublishAssetPort)
            self.assertEqual([request.operation for request in fake.requests], ["reference_frames", "reference_contact_sheet"])
            self.assertEqual([path.name for path in sorted(frames.glob("*.jpg"))], [
                "candidate_01.jpg", "candidate_02.jpg", "candidate_03.jpg",
            ])
            self.assertEqual(len(ffmpeg_calls), 3)
            self.assertTrue(contact.is_file())
            self.assertEqual(fake.requests[0].execution_binding["candidate_count"], 3)

    def test_final_cover_consumer_uses_protocol_and_preserves_lineage_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：PublishPort"
            logo, compiled_path = make_required_cover_fixture(project, root)
            paths = project_paths(project)
            creative_lineage = paths.publish / "cover_creative_lineage.json"
            original_creative_lineage = creative_lineage.read_bytes()
            compiled = json.loads(compiled_path.read_text(encoding="utf-8"))
            fake = ProtocolOnlyFake()

            receipt, render_manifest, lineage = render_required_covers(
                paths.publish,
                compiled_spec=compiled,
                logo_path=logo,
                story={
                    "name": "通用故事标题", "story_type": "童话故事",
                    "duration_text": "3分钟", "age_range": "6-8岁",
                },
                publish_asset_port=fake,
            )

            self.assertEqual(len(fake.requests), 6)
            self.assertTrue(all(request.operation == "final_cover_render" for request in fake.requests))
            self.assertEqual({request.artifact_id.removeprefix("publish-final-cover:") for request in fake.requests}, set(COVER_GRAPH))
            expected_names = {f"cover_{ratio}.png" for ratio in ("3x4", "4x3", "16x9")}
            self.assertEqual({request.output_targets[0].name for request in fake.requests}, expected_names)
            self.assertEqual(creative_lineage.read_bytes(), original_creative_lineage)
            self.assertTrue(receipt.is_file())
            self.assertTrue(render_manifest.is_file())
            self.assertTrue(lineage.is_file())
            self.assertFalse((paths.publish / "publish_asset_manifest.json").exists())
            render = json.loads(render_manifest.read_text(encoding="utf-8"))
            self.assertEqual(set(render["covers"]), set(COVER_GRAPH))
            self.assertTrue(all(item["official_logo_count"] == 1 for item in render["covers"].values()))
            self.assertTrue(all(item["title_text"] == "通用故事标题" for item in render["covers"].values()))

    def test_local_registry_reports_policy_and_provider_boundaries(self) -> None:
        registry = build_publish_asset_registry()
        port = registry.publish_asset()
        self.assertIsInstance(port, PublishAssetPort)
        self.assertFalse(port.capabilities.external)
        self.assertFalse(port.capabilities.paid)
        self.assertFalse(port.capabilities.supported["owns_image_provider"])
        self.assertFalse(port.capabilities.supported["owns_lineage"])
        self.assertFalse(port.capabilities.supported["owns_manifest"])
        self.assertFalse(port.capabilities.supported["owns_quality_policy"])


if __name__ == "__main__":
    unittest.main()
