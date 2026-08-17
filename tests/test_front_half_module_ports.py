from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from story_contract_runtime import contract_paths, locked_contract_binding
from story_module_adapters import (
    ApprovedStoryContractVisualDesignAdapter,
    ExistingStorySemanticsAdapter,
    MockStorySemanticsAdapter,
    MockVisualDesignAdapter,
    json_sha256,
)
from story_module_ports import (
    STORY_SEMANTICS_COMPILER_VERSION,
    ModuleCapabilities,
    ModuleFailureCode,
    ModuleIdentity,
    StorySemanticsPort,
    StorySemanticsRequest,
    StorySemanticsResult,
    VisualDesignPort,
    VisualDesignRequest,
    VisualDesignResult,
    story_semantics_payload,
    validate_story_semantics_payload,
    validate_visual_design_payload,
    visual_design_payload,
)
from story_module_registry import (
    ALLOWED_MODULE_PROFILES,
    ModuleRegistry,
    build_registry_for_profile,
    normalize_module_profile,
)
from story_semantics import StoryOutput, classify_story, lines_for_output
from tests.test_story_contract_runtime import _lock_contract, _new_project


ROOT = Path(__file__).resolve().parents[1]
SEMANTICS_SCHEMA = ROOT / "schemas" / "module_ports" / "v1" / "story_semantics_port.schema.json"
VISUAL_SCHEMA = ROOT / "schemas" / "module_ports" / "v1" / "visual_design_port.schema.json"


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
        if expected is None:
            continue
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
        additional = rule.get("additionalProperties")
        if isinstance(value, dict) and isinstance(additional, dict):
            if additional.get("type") == "array" and any(not isinstance(items, list) for items in value.values()):
                issues.append(f"values:{field}")
            elif additional.get("type") == "array" and additional.get("items", {}).get("type") == "integer":
                if any(any(isinstance(item, bool) or not isinstance(item, int) for item in items) for items in value.values()):
                    issues.append(f"values:{field}")
    return issues


def semantics_request(root: Path, lines: tuple[str, ...] | None = None) -> StorySemanticsRequest:
    material = lines or (
        "《通用故事》",
        "大家好，我是故事老师。",
        "今天我们来讲一个故事。",
        "主角说：“我是森林里的姐姐。”",
        "这个故事告诉我们要认真观察。",
        "我的故事讲完了。",
    )
    source = root / "story.txt"
    source.write_text("\n".join(material) + "\n", encoding="utf-8")
    return StorySemanticsRequest(
        source,
        hashlib.sha256(source.read_bytes()).hexdigest(),
        material,
        "generic-story",
        STORY_SEMANTICS_COMPILER_VERSION,
        "attempt-1",
    )


def visual_request(project: Path, semantics_sha: str) -> VisualDesignRequest:
    binding = locked_contract_binding(project, "storyboard_images")
    projection_sha = json_sha256(binding["contract_projection"])
    return VisualDesignRequest(
        project,
        "storyboard_images",
        "approved_projection",
        str(binding["story_contract_sha256"]),
        projection_sha,
        semantics_sha,
        (),
        "attempt-1",
    )


class FrontHalfModulePortTests(unittest.TestCase):
    def test_independent_schema_and_python_validators_have_parity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            semantic_request = semantics_request(root)
            semantic_result = ExistingStorySemanticsAdapter().analyze(semantic_request)
            project, manifest = _new_project(root)
            _lock_contract(project, manifest)
            visual_req = visual_request(project, semantic_request.source_sha256)
            visual_result = ApprovedStoryContractVisualDesignAdapter().resolve(visual_req)
            examples = [
                (
                    story_semantics_payload("semantics_request", semantic_request),
                    SEMANTICS_SCHEMA,
                    validate_story_semantics_payload,
                ),
                (
                    story_semantics_payload("semantics_result", semantic_result),
                    SEMANTICS_SCHEMA,
                    validate_story_semantics_payload,
                ),
                (
                    visual_design_payload("visual_design_request", visual_req),
                    VISUAL_SCHEMA,
                    validate_visual_design_payload,
                ),
                (
                    visual_design_payload("visual_design_result", visual_result),
                    VISUAL_SCHEMA,
                    validate_visual_design_payload,
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
            invalid_lines = story_semantics_payload("semantics_request", semantic_request)
            invalid_lines["normalized_lines"] = [False]
            self.assertTrue(validate_story_semantics_payload(invalid_lines))
            self.assertTrue(schema_issues(invalid_lines, SEMANTICS_SCHEMA))
            invalid_outputs = story_semantics_payload("semantics_result", semantic_result)
            invalid_outputs["output_line_numbers"] = {"ppt": [False]}
            self.assertTrue(validate_story_semantics_payload(invalid_outputs))
            self.assertTrue(schema_issues(invalid_outputs, SEMANTICS_SCHEMA))

    def test_existing_semantics_adapter_delegates_with_exact_parity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = semantics_request(Path(directory))
            result = ExistingStorySemanticsAdapter().analyze(request)
            expected = classify_story(request.normalized_lines)
            self.assertTrue(result.success)
            self.assertEqual([item["semantic_kind"] for item in result.lines], [
                expected.kind_at(index).value for index in range(1, len(request.normalized_lines) + 1)
            ])
            self.assertEqual(result.title, expected.title)
            self.assertEqual(
                result.output_line_numbers,
                {
                    output.value: tuple(line.line_number for line in lines_for_output(expected, output))
                    for output in StoryOutput
                },
            )
            self.assertEqual(result.lines[3]["semantic_kind"], "story_body")

    def test_production_identity_and_capabilities_are_truthful(self) -> None:
        registry = build_registry_for_profile("production-default")
        semantics = registry.story_semantics()
        visual = registry.visual_design()
        self.assertIsInstance(semantics, ExistingStorySemanticsAdapter)
        self.assertEqual(semantics.identity.port_name, "story_semantics")
        self.assertTrue(semantics.capabilities.deterministic)
        self.assertFalse(semantics.capabilities.external)
        self.assertFalse(semantics.capabilities.paid)
        self.assertIsInstance(visual, ApprovedStoryContractVisualDesignAdapter)
        self.assertEqual(visual.identity.port_name, "visual_design")
        self.assertTrue(visual.capabilities.supported["review_lock_required"])
        self.assertFalse(visual.capabilities.supported["writes_visual_contract"])

    def test_malformed_requests_fail_closed_as_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = semantics_request(root)
            malformed = StorySemanticsRequest(
                request.source_path,
                "0" * 64,
                request.normalized_lines,
                request.story_id,
                request.compiler_version,
                request.attempt_id,
            )
            semantic_result = ExistingStorySemanticsAdapter().analyze(malformed)
            self.assertFalse(semantic_result.success)
            self.assertEqual(semantic_result.failure.code, ModuleFailureCode.INVALID_INPUT)

            visual_result = ApprovedStoryContractVisualDesignAdapter().resolve(
                VisualDesignRequest(root / "missing", "storyboard_images", "approved_projection", "a" * 64, "b" * 64, "c" * 64, (), "a1")
            )
            self.assertFalse(visual_result.success)
            self.assertEqual(visual_result.failure.code, ModuleFailureCode.INVALID_INPUT)

    def test_semantics_mock_is_a_deterministic_known_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = semantics_request(Path(directory), ("《标题》", "正文", "道理"))
            mock = MockStorySemanticsAdapter(
                kinds=("title", "story_body", "moral"),
                output_line_numbers={"ppt": (2, 3)},
            )
            first = mock.analyze(request)
            second = mock.analyze(request)
            self.assertEqual(first, second)
            self.assertEqual(first.output_line_numbers, {"ppt": (2, 3)})
            self.assertEqual([item["semantic_kind"] for item in first.lines], ["title", "story_body", "moral"])
            failed = MockStorySemanticsAdapter(kinds=("title",), mode="failure").analyze(request)
            self.assertEqual(failed.failure.code, ModuleFailureCode.EXECUTION_FAILED)

    def test_visual_design_rejects_stale_contract_review_and_lock(self) -> None:
        mutations = ("contract", "review", "lock")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                project, manifest = _new_project(Path(directory))
                _lock_contract(project, manifest)
                request = visual_request(project, "d" * 64)
                self.assertTrue(ApprovedStoryContractVisualDesignAdapter().resolve(request).success)
                paths = contract_paths(project)
                if mutation == "contract":
                    paths["contract"].write_text(paths["contract"].read_text(encoding="utf-8") + "\n", encoding="utf-8")
                elif mutation == "review":
                    review = json.loads(paths["review"].read_text(encoding="utf-8"))
                    review["score"] = 95
                    paths["review"].write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                else:
                    paths["lock"].write_text('{"stale": true}\n', encoding="utf-8")
                result = ApprovedStoryContractVisualDesignAdapter().resolve(request)
                self.assertFalse(result.success)
                self.assertEqual(result.failure.code, ModuleFailureCode.INVALID_INPUT)

    def test_visual_mock_uses_approved_projection_without_writing_second_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            _lock_contract(project, manifest)
            request = visual_request(project, "e" * 64)
            before = {path.relative_to(project) for path in project.rglob("*") if path.is_file()}
            result = MockVisualDesignAdapter().resolve(request)
            after = {path.relative_to(project) for path in project.rglob("*") if path.is_file()}
            self.assertTrue(result.success)
            self.assertEqual(result.projection_sha256, request.projection_sha256)
            self.assertEqual(before, after)
            self.assertFalse(any("visual_design_contract" in str(path) for path in after))

    def test_registry_profiles_are_allowlisted_and_preserve_3a_profiles(self) -> None:
        self.assertTrue({"production-default", "mock-video", "mock-keyer"}.issubset(ALLOWED_MODULE_PROFILES))
        self.assertTrue({"mock-semantics", "mock-visual-design"}.issubset(ALLOWED_MODULE_PROFILES))
        semantics_registry = build_registry_for_profile("mock-semantics")
        self.assertIsInstance(semantics_registry.story_semantics(), MockStorySemanticsAdapter)
        self.assertIsInstance(build_registry_for_profile("mock-visual-design").visual_design(), MockVisualDesignAdapter)
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(semantics_registry.story_semantics().analyze(semantics_request(Path(directory))).success)
        with self.assertRaises(ValueError):
            normalize_module_profile("package.module:ArbitraryAdapter")
        with self.assertRaises(ValueError):
            normalize_module_profile("python -c arbitrary")

    def test_minimal_protocol_only_fakes_register_without_concrete_dependencies(self) -> None:
        class SemanticFake:
            identity = ModuleIdentity("story_semantics", "fake/v1", "fake", "fake/v1")
            capabilities = ModuleCapabilities(provider="fake", deterministic=True)

            def analyze(self, request: StorySemanticsRequest) -> StorySemanticsResult:
                return StorySemanticsResult(True, request.source_path, request.source_sha256, "", (), (), {}, "fake/v1", request.compiler_version)

        class VisualFake:
            identity = ModuleIdentity("visual_design", "fake/v1", "fake", "fake/v1")
            capabilities = ModuleCapabilities(provider="fake", deterministic=True)

            def resolve(self, request: VisualDesignRequest) -> VisualDesignResult:
                return VisualDesignResult(True, request.operation, {}, (), request.projection_sha256, request.story_contract_sha256, request.projection_sha256, request.story_semantics_sha256, request.attempt_id, "fake/v1")

        semantic_fake, visual_fake = SemanticFake(), VisualFake()
        self.assertIsInstance(semantic_fake, StorySemanticsPort)
        self.assertIsInstance(visual_fake, VisualDesignPort)
        registry = ModuleRegistry()
        registry.register("story_semantics", semantic_fake)
        registry.register("visual_design", visual_fake)
        self.assertIs(registry.story_semantics(), semantic_fake)
        self.assertIs(registry.visual_design(), visual_fake)

    def test_front_half_ports_do_not_absorb_quality_policy(self) -> None:
        forbidden = {
            "audience_fit", "composition_score", "color_score", "lighting_score",
            "style_suitability", "anatomical_coherence", "p0_errors", "review_verdict",
        }
        self.assertFalse(forbidden & set(dir(ExistingStorySemanticsAdapter)))
        self.assertFalse(forbidden & set(dir(ApprovedStoryContractVisualDesignAdapter)))
        self.assertFalse(forbidden & set(StorySemanticsResult.__dataclass_fields__))
        self.assertFalse(forbidden & set(VisualDesignResult.__dataclass_fields__))

    def test_existing_3a_schema_is_unchanged_and_profiles_still_build(self) -> None:
        schema = json.loads((ROOT / "schemas/module_ports/v1/module_ports.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(
            schema["properties"]["kind"]["enum"],
            ["identity", "capabilities", "failure", "usage_event", "video_request", "video_result", "keyer_request", "keyer_result"],
        )
        for profile in ("production-default", "mock-video", "mock-keyer"):
            registry = build_registry_for_profile(profile)
            self.assertIsNotNone(registry.video_generator())
            self.assertIsNotNone(registry.keyer())


if __name__ == "__main__":
    unittest.main()
