from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from story_evidence import file_sha256, write_review_bundle
from story_contract_consumers import projection_sha256
from story_contract_runtime import (
    contract_consumer_path,
    contract_paths,
    locked_contract_binding,
    write_contract_consumer_context,
)
from story_module_adapters import ApprovedStoryContractVisualDesignAdapter, MockVisualDesignAdapter
from story_module_ports import (
    ModuleCapabilities,
    ModuleIdentity,
    VisualDesignPort,
    VisualDesignRequest,
    VisualDesignResult,
)
from story_module_registry import (
    MODULE_PROFILE_ENV,
    MODULE_PROFILE_REQUIRED_ENV,
    ModuleRegistry,
)
from story_project import project_paths, save_json, write_manifest
from tests.test_story_contract_runtime import _lock_contract, _new_project, _runtime_valid_contract
from tests.test_story_contracts import valid_contract
from visual_sample_gate import (
    P0_CATEGORIES,
    VISUAL_SAMPLE_SCHEMA_PATH,
    compile_visual_sample_plan,
    enrich_visual_sample_supplemental_request,
    load_current_visual_sample_plan,
    validate_visual_sample_plan,
    visual_sample_binding,
    visual_sample_generation_jobs,
    visual_sample_lock_is_current,
    visual_sample_paths,
    visual_sample_review_payload_issues,
    visual_sample_schema_parity_issues,
    write_visual_sample_lock,
    write_visual_sample_machine_qa,
    write_visual_sample_plan,
    write_visual_sample_supplemental_request,
)


def _image(path: Path, color=(220, 190, 120)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (640, 480), color).save(path)
    return path


def _agent_provenance(value) -> None:
    if isinstance(value, dict):
        for key, child in list(value.items()):
            if key in {"provenance", "mode_provenance"} and isinstance(child, dict):
                value[key] = {
                    "source": "agent_inference",
                    "source_ref": "generic visual fixture inference",
                    "source_order": 0,
                }
            else:
                _agent_provenance(child)
    elif isinstance(value, list):
        for child in value:
            _agent_provenance(child)


def _visual_contract(
    project: Path,
    manifest: dict,
    *,
    characters: bool = True,
    scale: bool = True,
    state: bool = True,
    preview_kinds=("style_anchor",),
    style_description: str | None = None,
    required_traits: list[str] | None = None,
    forbidden_traits: list[str] | None = None,
) -> dict:
    contract = _runtime_valid_contract(project, manifest)
    if characters:
        source = valid_contract(with_characters=True)["contracts"]
        for section in ("characters", "world_scale", "story_state"):
            contract["contracts"][section] = copy.deepcopy(source[section])
        _agent_provenance(contract["contracts"]["characters"])
        _agent_provenance(contract["contracts"]["world_scale"])
        _agent_provenance(contract["contracts"]["story_state"])
        if not scale:
            contract["contracts"]["world_scale"]["relationships"] = []
        if not state:
            contract["contracts"]["story_state"]["machines"] = []
    else:
        contract["contracts"]["characters"].update(
            mode="none", characters=[], no_character_reason="This generic story has no visual character."
        )
        contract["contracts"]["world_scale"]["relationships"] = []
        contract["contracts"]["story_state"]["machines"] = []
    style_profile = contract["contracts"]["visual_style"]["style_profile"]
    if style_description is not None:
        style_profile["description"] = style_description
    if required_traits is not None:
        style_profile["required_traits"] = required_traits
    if forbidden_traits is not None:
        style_profile["forbidden_traits"] = forbidden_traits
    refs = {
        "style_anchor": [],
        "character_sheet": ["protagonist", "guide"],
        "scale_anchor": ["hero_vs_guide"],
    }
    previews = []
    for kind in preview_kinds:
        path = _image(project / "99_项目状态" / "story_contract" / "previews" / f"{kind}.png")
        previews.append(
            {
                "preview_id": f"{kind}_fixture",
                "kind": kind,
                "need_reason": "Generic conditional visual preview fixture.",
                "content_refs": refs[kind],
                "path": str(path.relative_to(project)),
                "sha256": file_sha256(path),
                "provenance": {
                    "source": "agent_inference",
                    "source_ref": "generic visual preview fixture",
                    "source_order": 0,
                },
            }
        )
    contract["preview_assets"] = previews
    return contract


def _fixture(
    root: Path,
    *,
    characters: bool = True,
    scale: bool = True,
    state: bool = True,
    preview_kinds=("style_anchor",),
    style_description: str | None = None,
    required_traits: list[str] | None = None,
    forbidden_traits: list[str] | None = None,
):
    project, manifest = _new_project(root)
    contract = _visual_contract(
        project,
        manifest,
        characters=characters,
        scale=scale,
        state=state,
        preview_kinds=preview_kinds,
        style_description=style_description,
        required_traits=required_traits,
        forbidden_traits=forbidden_traits,
    )
    agent, _paths = _lock_contract(project, manifest, contract_payload=contract)
    context = write_contract_consumer_context(project, "storyboard_images")
    return project, manifest, agent, context


def _ready_plan(project: Path, context: Path) -> dict:
    plan = compile_visual_sample_plan(project, context)
    for item in plan["requirements"]:
        if item["fulfillment"] == "supplemental_sample":
            _image(project / item["expected_path"], color=(160, 200, 180))
    write_visual_sample_plan(project, context)
    return load_current_visual_sample_plan(project, context)


def _passing_review(plan: dict, bundle: Path) -> dict:
    return {
        "approved": True,
        "score": 96,
        "critical_errors": [],
        "issues": [],
        "retry_indices": [],
        "retry_files": [],
        "retry_sample_ids": [],
        "retry_instructions": [],
        "p0_errors": [],
        "machine_completeness": {"passed": True, "evidence": "machine QA bound in bundle"},
        "contract_adherence": {
            "passed": True,
            "checks": [
                {"dimension": dimension, "passed": True, "evidence": f"sample verifies {dimension}"}
                for dimension in plan["review_profile"]["contract_adherence"]
            ],
        },
        "product_quality": {
            "passed": True,
            "dimensions": [
                {"dimension": dimension, "passed": True, "evidence": f"sample verifies {dimension}"}
                for dimension in plan["review_profile"]["product_quality"]
            ],
            "style_contract": {
                "description": plan["review_profile"]["style_contract"]["description"],
                "description_fit": True,
                "description_evidence": "All samples fit the declared style description.",
                "required_traits": [
                    {"trait": trait, "passed": True, "evidence": f"sample demonstrates {trait}"}
                    for trait in plan["review_profile"]["style_contract"]["required_traits"]
                ],
                "forbidden_traits": [
                    {"trait": trait, "absent": True, "evidence": f"sample avoids {trait}"}
                    for trait in plan["review_profile"]["style_contract"]["forbidden_traits"]
                ],
            },
        },
        "evidence_matrix": [
            {"sample_id": item["sample_id"], "evidence": item["expected_path"]}
            for item in plan["requirements"]
        ],
        "artifact_sha256": file_sha256(bundle),
    }


def _lock_samples(project: Path, context: Path) -> dict:
    plan = _ready_plan(project, context)
    paths = visual_sample_paths(project)
    machine = write_visual_sample_machine_qa(project, plan)
    assets = [project / item["expected_path"] for item in plan["requirements"]]
    bundle = write_review_bundle(paths["bundle"], [paths["plan"], machine, context, *assets])
    save_json(paths["review"], _passing_review(plan, bundle))
    write_visual_sample_lock(project)
    if not visual_sample_lock_is_current(project, context):
        raise AssertionError("visual sample fixture lock is not current")
    return plan



























def agent_context_for(project: Path):
    from tests.test_story_contract_runtime import _context

    return _context(project)


if __name__ == "__main__":
    unittest.main()


class VisualSampleGateTests(unittest.TestCase):
    def test_visual_design_port_binding_and_protocol_only_substitution_are_byte_equivalent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _manifest, _agent, context_path = _fixture(Path(directory))
            context = json.loads(context_path.read_text(encoding="utf-8"))
            binding = locked_contract_binding(project, "storyboard_images")
            adapter = ApprovedStoryContractVisualDesignAdapter()
            contract = json.loads(contract_paths(project)["contract"].read_text(encoding="utf-8"))
            references = tuple(contract.get("preview_assets", []))
            captured: list[VisualDesignRequest] = []

            class ProtocolFake:
                identity = ModuleIdentity("visual_design", "fake/v1", "protocol-fake", "protocol-fake/v1")
                capabilities = ModuleCapabilities(provider="fake", deterministic=True)

                def resolve(self, request: VisualDesignRequest) -> VisualDesignResult:
                    captured.append(request)
                    return VisualDesignResult(
                        True,
                        request.operation,
                        context["contract_projection"],
                        references,
                        request.projection_sha256,
                        request.story_contract_sha256,
                        request.projection_sha256,
                        request.story_semantics_sha256,
                        request.attempt_id,
                        "protocol-fake/v1",
                    )

            fake = ProtocolFake()
            self.assertIsInstance(fake, VisualDesignPort)
            baseline = compile_visual_sample_plan(project, context_path)
            substituted = compile_visual_sample_plan(project, context_path, fake)
            self.assertEqual(substituted, baseline)
            plan_path = write_visual_sample_plan(project, context_path)
            baseline_bytes = plan_path.read_bytes()
            write_visual_sample_plan(project, context_path, fake)
            self.assertEqual(plan_path.read_bytes(), baseline_bytes)
            self.assertEqual(captured[0].story_contract_sha256, binding["story_contract_sha256"])
            self.assertEqual(captured[0].projection_sha256, projection_sha256(context))
            result = adapter.resolve(captured[0])
            self.assertTrue(result.success)
            self.assertEqual(result.projection_sha256, projection_sha256(context))
            self.assertEqual(result.approved_projection, binding["contract_projection"])

    def test_visual_design_consumer_fails_closed_for_stale_contract_review_or_lock(self) -> None:
        for damaged_name in ("contract", "review", "lock"):
            with self.subTest(damaged=damaged_name), tempfile.TemporaryDirectory() as directory:
                project, _manifest, _agent, context = _fixture(Path(directory))
                paths = contract_paths(project)
                paths[damaged_name].write_text('{"damaged":true}\n', encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "visual design port rejected"):
                    compile_visual_sample_plan(project, context)

    def test_missing_required_mock_visual_selection_fails_closed_without_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _manifest, _agent, context = _fixture(Path(directory))
            environment = os.environ.copy()
            environment.pop(MODULE_PROFILE_ENV, None)
            environment[MODULE_PROFILE_REQUIRED_ENV] = "mock-visual-design"
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(RuntimeError, "refusing production fallback"):
                    write_visual_sample_plan(project, context)
            self.assertFalse(visual_sample_paths(project)["plan"].exists())

    def test_schema_parity_determinism_and_conditional_sample_matrix(self) -> None:
        cases = [
            (False, False, False, {"style_anchor"}),
            (True, False, False, {"style_anchor"}),
            (True, True, False, {"style_anchor", "scale_anchor"}),
            (True, True, True, {"style_anchor", "scale_anchor", "state_anchor"}),
        ]
        self.assertEqual(visual_sample_schema_parity_issues(), [])
        for characters, scale, state, expected in cases:
            with self.subTest(characters=characters, scale=scale, state=state), tempfile.TemporaryDirectory() as directory:
                project, _manifest, _agent, context = _fixture(
                    Path(directory), characters=characters, scale=scale, state=state
                )
                first = compile_visual_sample_plan(project, context)
                second = compile_visual_sample_plan(project, context)
                self.assertEqual(first, second)
                self.assertEqual({item["kind"] for item in first["requirements"]}, expected)
                self.assertEqual(validate_visual_sample_plan(first), [])
                tampered = copy.deepcopy(first)
                tampered["unexpected"] = True
                self.assertIn("unexpected_fields:unexpected", validate_visual_sample_plan(tampered))
                try:
                    import jsonschema
                except ImportError:
                    jsonschema = None
                if jsonschema is not None:
                    schema = json.loads(VISUAL_SAMPLE_SCHEMA_PATH.read_text(encoding="utf-8"))
                    jsonschema.Draft202012Validator(schema).validate(first)

    def test_reuses_reviewed_contract_preview_and_supplements_only_missing_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _manifest, _agent, context = _fixture(
                Path(directory), preview_kinds=("style_anchor", "character_sheet", "scale_anchor")
            )
            plan = compile_visual_sample_plan(project, context)
            by_kind = {item["kind"]: item for item in plan["requirements"]}
            self.assertEqual(by_kind["style_anchor"]["fulfillment"], "contract_preview")
            self.assertIn("shared mother reference", by_kind["style_anchor"]["need_reason"])
            self.assertEqual(by_kind["character_sheet"]["fulfillment"], "contract_preview")
            self.assertEqual(by_kind["scale_anchor"]["fulfillment"], "contract_preview")
            self.assertEqual(by_kind["state_anchor"]["fulfillment"], "supplemental_sample")

    def test_aggregate_state_sheet_is_not_reused_for_a_standalone_state_sample(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, manifest = _new_project(root)
            contract = _visual_contract(
                project,
                manifest,
                preview_kinds=("style_anchor", "character_sheet", "scale_anchor"),
            )
            state_ids = [
                row["machine_id"] for row in contract["contracts"]["story_state"]["machines"]
            ]
            state_path = _image(project / "99_项目状态" / "story_contract" / "previews" / "state.png")
            contract["preview_assets"].append(
                {
                    "preview_id": "state_anchor_fixture",
                    "kind": "state_anchor",
                    "need_reason": "One approved state sheet reuses the same character identity.",
                    "content_refs": state_ids,
                    "path": str(state_path.relative_to(project)),
                    "sha256": file_sha256(state_path),
                    "provenance": {
                        "source": "agent_inference",
                        "source_ref": "generic state preview fixture",
                        "source_order": 0,
                    },
                }
            )
            _agent, _paths = _lock_contract(project, manifest, contract_payload=contract)
            context = write_contract_consumer_context(project, "storyboard_images")

            plan = compile_visual_sample_plan(project, context)
            by_kind = {item["kind"]: item for item in plan["requirements"]}
            self.assertEqual(by_kind["state_anchor"]["fulfillment"], "supplemental_sample")
            self.assertNotEqual(by_kind["state_anchor"]["expected_path"], str(state_path.relative_to(project)))
            self.assertEqual(len(by_kind["state_anchor"]["content_refs"]), 2)

    def test_long_state_sequence_compiles_three_standalone_representative_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, manifest = _new_project(root)
            contract = _visual_contract(project, manifest, preview_kinds=())
            machine = contract["contracts"]["story_state"]["machines"][0]
            provenance = copy.deepcopy(machine["provenance"])
            machine["initial_state"] = "state_0"
            machine["states"] = [
                {
                    "state_id": f"state_{index}",
                    "description": f"Generic finite-prop state {index}.",
                    "required": [f"remaining count is {7 - index}"],
                    "forbidden": [f"previous state {max(0, index - 1)}"],
                    "provenance": copy.deepcopy(provenance),
                }
                for index in range(8)
            ]
            machine["transitions"] = []
            _agent, _paths = _lock_contract(project, manifest, contract_payload=contract)
            context = write_contract_consumer_context(project, "storyboard_images")

            plan = compile_visual_sample_plan(project, context)
            states = [item for item in plan["requirements"] if item["kind"] == "state_anchor"]
            self.assertEqual(len(states), 3)
            self.assertEqual([item["content_refs"][1] for item in states], ["state_0", "state_3", "state_7"])
            self.assertEqual(len({item["sample_id"] for item in states}), 3)
            self.assertTrue(all("contact sheet" in item["need_reason"] for item in states))

    def test_rich_review_evidence_shape_is_accepted_without_protocol_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _manifest, _agent, context = _fixture(Path(directory), preview_kinds=())
            plan = _ready_plan(project, context)
            review = _passing_review(plan, visual_sample_paths(project)["plan"])
            contract_path_by_dimension = {
                "visual_style": "contracts.visual_style",
                "characters": "contracts.characters",
                "world_scale": "contracts.world_scale.relationships[0]",
                "story_state": "contracts.story_state.machines[0].states[0]",
            }
            review["contract_adherence"]["checks"] = [
                {
                    "sample_id": plan["requirements"][0]["sample_id"],
                    "relevant_contract": contract_path_by_dimension[dimension],
                    "passed": True,
                    "evidence": f"bound evidence for {dimension}",
                }
                for dimension in plan["review_profile"]["contract_adherence"]
            ]
            review["evidence_matrix"] = [
                {
                    "sample_id": item["sample_id"],
                    "conclusions": ["file opened and inspected", "sample passes its scoped contract"],
                }
                for item in plan["requirements"]
            ]
            scale_dimension = next(
                (
                    item
                    for item in review["product_quality"]["dimensions"]
                    if item["dimension"] == "scale_readability"
                ),
                None,
            )
            if scale_dimension is not None:
                scale_dimension.pop("evidence", None)
                scale_dimension["relationships"] = [
                    {"relationship_id": "fixture", "passed": True, "evidence": "ground plane and size are clear"}
                ]
            self.assertEqual(visual_sample_review_payload_issues(review, plan), [])

    def test_retry_uses_quarantined_image_and_approved_mother_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _manifest, _agent, context = _fixture(Path(directory), preview_kinds=())
            plan = _ready_plan(project, context)
            paths = visual_sample_paths(project)
            write_visual_sample_machine_qa(project, plan)
            write_review_bundle(
                paths["bundle"],
                [paths["plan"], paths["machine_qa"], context, *[project / item["expected_path"] for item in plan["requirements"]]],
            )
            style_id = "style_anchor"
            scale_id = "scale_anchor"
            states = [item for item in plan["requirements"] if item["kind"] == "state_anchor"]
            base_state_id = states[0]["sample_id"]
            retry_state_ids = [item["sample_id"] for item in states[1:]]
            retry_ids = [style_id, scale_id, *retry_state_ids]
            review = _passing_review(plan, paths["bundle"])
            review.update(
                approved=False,
                score=78,
                p0_errors=[{"sample_id": style_id, "category": "unsupported_identity_feature"}],
                retry_sample_ids=retry_ids,
                retry_instructions=[
                    {"sample_id": sample_id, "instruction": f"repair only {sample_id}"}
                    for sample_id in retry_ids
                ],
            )
            save_json(paths["review"], review)
            write_visual_sample_supplemental_request(project, plan, review)
            rejected = paths["directory"] / "rejected" / "fixture-attempt"
            rejected.mkdir(parents=True)
            by_id = {item["sample_id"]: item for item in plan["requirements"]}
            for sample_id in retry_ids:
                source = project / by_id[sample_id]["expected_path"]
                source.replace(rejected / source.name)
            enrich_visual_sample_supplemental_request(project)
            retry_plan = compile_visual_sample_plan(project, context)
            missing = [item for item in retry_plan["requirements"] if not isinstance(item.get("asset"), dict)]
            projection = json.loads(context.read_text(encoding="utf-8"))["contract_projection"]
            jobs = visual_sample_generation_jobs(retry_plan, projection, missing)
            jobs_by_id = {item["sample_id"]: item for item in jobs}
            self.assertEqual(set(jobs_by_id), set(retry_ids))
            self.assertEqual(jobs_by_id[style_id]["retry_instruction"], f"repair only {style_id}")
            self.assertIn(
                "rejected_sample_for_targeted_edit",
                {item["role"] for item in jobs_by_id[style_id]["reference_assets"]},
            )
            self.assertIn(style_id, jobs_by_id[scale_id]["dependency_sample_ids"])
            for sample_id in retry_state_ids:
                references = jobs_by_id[sample_id]["reference_assets"]
                self.assertIn(base_state_id, {item["sample_id"] for item in references})
                self.assertIn("approved_mother_sample", {item["role"] for item in references})

    def test_review_without_retry_scope_never_defaults_to_all_samples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _manifest, _agent, context = _fixture(Path(directory), preview_kinds=())
            plan = _ready_plan(project, context)
            paths = visual_sample_paths(project)
            write_visual_sample_machine_qa(project, plan)
            write_review_bundle(
                paths["bundle"],
                [paths["plan"], paths["machine_qa"], context, *[project / item["expected_path"] for item in plan["requirements"]]],
            )
            review = _passing_review(plan, paths["bundle"])
            review.update(
                approved=False,
                score=70,
                retry_sample_ids=[],
                retry_instructions=[],
                issues=["unspecified failure"],
            )
            save_json(paths["review"], review)
            with self.assertRaisesRegex(ValueError, "refusing an unbounded all-sample retry"):
                write_visual_sample_supplemental_request(project, plan, review)

    def test_environment_only_relationships_do_not_multiply_scale_reference_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, manifest = _new_project(root)
            contract = _visual_contract(project, manifest, preview_kinds=("style_anchor", "character_sheet"))
            for relationship in contract["contracts"]["world_scale"]["relationships"]:
                relationship["qualitative_relation"] = "environment_reference"
            _agent, _paths = _lock_contract(project, manifest, contract_payload=contract)
            context = write_contract_consumer_context(project, "storyboard_images")

            plan = compile_visual_sample_plan(project, context)
            self.assertNotIn("scale_anchor", {item["kind"] for item in plan["requirements"]})

    def test_failed_sample_request_replaces_only_named_sample(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _manifest, _agent, context = _fixture(
                Path(directory),
                state=False,
                preview_kinds=("style_anchor", "character_sheet", "scale_anchor"),
            )
            plan = _ready_plan(project, context)
            paths = visual_sample_paths(project)
            write_visual_sample_machine_qa(project, plan)
            write_review_bundle(
                paths["bundle"],
                [paths["plan"], paths["machine_qa"], context, *[project / item["expected_path"] for item in plan["requirements"]]],
            )
            review = _passing_review(plan, paths["bundle"])
            review.update(
                approved=False,
                score=82,
                retry_sample_ids=["scale_anchor"],
                p0_errors=["character_identity_mismatch"],
            )
            save_json(paths["review"], review)
            write_visual_sample_supplemental_request(project, plan, review)
            replacement = compile_visual_sample_plan(project, context)
            by_kind = {item["kind"]: item for item in replacement["requirements"]}
            self.assertEqual(by_kind["character_sheet"]["fulfillment"], "contract_preview")
            self.assertEqual(by_kind["style_anchor"]["fulfillment"], "contract_preview")
            self.assertEqual(by_kind["scale_anchor"]["fulfillment"], "supplemental_sample")

    def test_review_lock_is_hash_bound_and_sample_tamper_invalidates_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _manifest, _agent, context = _fixture(Path(directory))
            plan = _lock_samples(project, context)
            self.assertTrue(visual_sample_lock_is_current(project, context))
            target = project / plan["requirements"][-1]["expected_path"]
            _image(target, color=(10, 20, 30))
            self.assertFalse(visual_sample_lock_is_current(project, context))
            with self.assertRaisesRegex(ValueError, "stale or manually modified"):
                load_current_visual_sample_plan(project, context)

    def test_quality_profile_is_style_adaptive_for_cute_historical_and_abstract_stories(self) -> None:
        cases = [
            {
                "name": "cute_character",
                "characters": True,
                "description": "A cute and welcoming children's animation style.",
                "required": ["cute rounded character design", "warm playful expression"],
                "forbidden": ["frightening imagery"],
                "character_dimensions": True,
            },
            {
                "name": "solemn_historical_character",
                "characters": True,
                "description": "A restrained, solemn historical picture-book style.",
                "required": ["historical atmosphere", "dignified restraint", "realistic proportions"],
                "forbidden": ["cute chibi treatment"],
                "character_dimensions": True,
            },
            {
                "name": "abstract_no_character",
                "characters": False,
                "description": "An abstract visual poem using calm geometric forms.",
                "required": ["abstract clarity"],
                "forbidden": ["invented characters"],
                "character_dimensions": False,
            },
        ]
        for case in cases:
            with self.subTest(case=case["name"]), tempfile.TemporaryDirectory() as directory:
                project, _manifest, _agent, context = _fixture(
                    Path(directory),
                    characters=case["characters"],
                    scale=False,
                    state=False,
                    style_description=case["description"],
                    required_traits=case["required"],
                    forbidden_traits=case["forbidden"],
                )
                plan = compile_visual_sample_plan(project, context)
                dimensions = set(plan["review_profile"]["product_quality"])
                self.assertTrue(
                    {"audience_fit", "composition", "color", "lighting", "style_suitability"}.issubset(dimensions)
                )
                self.assertNotIn("cuteness", dimensions)
                self.assertNotIn("child_appeal", dimensions)
                self.assertNotIn("natural_identity", dimensions)
                character_dimensions = {"character_design_fit", "identity_coherence", "anatomical_coherence"}
                self.assertEqual(character_dimensions.issubset(dimensions), case["character_dimensions"])
                self.assertEqual(plan["review_profile"]["style_contract"]["description"], case["description"])
                self.assertEqual(plan["review_profile"]["style_contract"]["required_traits"], case["required"])
                self.assertEqual(plan["review_profile"]["style_contract"]["forbidden_traits"], case["forbidden"])

                plan_path = visual_sample_paths(project)["plan"]
                save_json(plan_path, plan)
                review = _passing_review(plan, plan_path)
                self.assertEqual(visual_sample_review_payload_issues(review, plan), [])
                self.assertIn("unsafe_or_unsuitable_for_children", plan["review_profile"]["p0_categories"])
                if case["name"] == "cute_character":
                    review["product_quality"]["style_contract"]["required_traits"] = []
                    issues = visual_sample_review_payload_issues(review, plan)
                    self.assertTrue(any("cute rounded character design" in issue for issue in issues))
                if case["name"] == "solemn_historical_character":
                    review["product_quality"]["style_contract"]["required_traits"] = []
                    issues = visual_sample_review_payload_issues(review, plan)
                    self.assertTrue(any("historical atmosphere" in issue for issue in issues))

    def test_identity_policy_allows_ordinary_contextual_detail_without_promoting_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _manifest, _agent, context = _fixture(
                Path(directory),
                characters=True,
                scale=False,
                state=False,
                style_description="A grounded period village story.",
                required_traits=["era-appropriate ordinary clothing"],
                forbidden_traits=["invented emblems"],
            )
            policy = compile_visual_sample_plan(project, context)["identity_expansion_policy"]
            self.assertEqual(policy["scope"], "identity_defining_features_only")
            self.assertIn("era_and_scene_appropriate_ordinary_clothing", policy["allowed_contextual_inferences"])
            self.assertIn("fixed_accessory", policy["blocked_inferences"])
            self.assertIn("emblem", policy["blocked_inferences"])
            self.assertEqual(policy["inferred_detail_persistence"], "scene_local_unless_contract_promotes")
            self.assertEqual(policy["contract_precedence"], "required_and_forbidden_features_are_authoritative")
