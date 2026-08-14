from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from story_agent import StageResult, StoryAgent
from story_agent_runtime import file_sha256, write_review_bundle
from story_contract_runtime import contract_consumer_path, write_contract_consumer_context
from story_project import project_paths, save_json, write_manifest
from tests.test_story_agent_runtime import as_frozen_v3_legacy
from tests.test_story_contract_runtime import _lock_contract, _new_project, _runtime_valid_contract
from tests.test_story_contracts import valid_contract
from visual_sample_gate import (
    P0_CATEGORIES,
    VISUAL_SAMPLE_SCHEMA_PATH,
    compile_visual_sample_plan,
    load_current_visual_sample_plan,
    validate_visual_sample_plan,
    visual_sample_binding,
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


class VisualSampleGateTests(unittest.TestCase):
    def test_schema_parity_determinism_and_conditional_sample_matrix(self) -> None:
        cases = [
            (False, False, False, {"style_anchor"}),
            (True, False, False, {"style_anchor", "character_sheet"}),
            (True, True, False, {"style_anchor", "character_sheet", "scale_anchor"}),
            (True, True, True, {"style_anchor", "character_sheet", "scale_anchor", "state_anchor"}),
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
            self.assertEqual(by_kind["character_sheet"]["fulfillment"], "contract_preview")
            self.assertEqual(by_kind["scale_anchor"]["fulfillment"], "contract_preview")
            self.assertEqual(by_kind["state_anchor"]["fulfillment"], "supplemental_sample")

    def test_worker_handoff_contains_full_projection_and_identity_expansion_ban(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest, agent, context = _fixture(Path(directory))

            def fake_task(**_kwargs):
                handoff = visual_sample_paths(project)["handoff"].read_text(encoding="utf-8")
                for name in ("visual_style", "characters", "world_scale", "story_state"):
                    self.assertIn(f'"{name}"', handoff)
                self.assertIn("不得擅自新增会成为跨镜头身份锚点的特殊标记", handoff)
                self.assertIn("时代和场景合理的普通服饰", handoff)
                self.assertIn("不得升级为永久身份锚点", handoff)
                plan = compile_visual_sample_plan(project, context)
                for item in plan["requirements"]:
                    if not isinstance(item.get("asset"), dict):
                        _image(project / item["expected_path"])
                return StageResult("done", "fixture samples created")

            with patch.object(agent, "_codex_task", side_effect=fake_task):
                result = agent._stage_visual_samples(manifest)
            self.assertEqual(result.status, "done", result.message)
            self.assertTrue(agent._has_visual_samples(manifest))

    def test_three_layer_review_p0_is_a_hard_gate_and_batch_never_starts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest, agent, context = _fixture(Path(directory))
            plan = _ready_plan(project, context)
            paths = visual_sample_paths(project)
            bad = _passing_review(plan, paths["plan"])
            bad.update(score=100, approved=True, p0_errors=["unsupported_identity_feature"])
            self.assertTrue(any("P0 hard gate" in issue for issue in visual_sample_review_payload_issues(bad, plan)))

            def fake_review(**kwargs):
                bad["artifact_sha256"] = file_sha256(kwargs["bundle"])
                save_json(paths["review"], bad)
                return StageResult("done", "generic score passed", paths["review"]), bad

            with patch.object(agent, "_structured_review", side_effect=fake_review):
                result = agent._stage_visual_sample_review(manifest)
            self.assertIn(result.status, {"retrying", "blocked"})
            self.assertFalse(paths["lock"].exists())
            with patch.object(agent, "_codex_task") as producer:
                batch = agent._stage_codex_story_images(manifest)
            self.assertEqual(batch.status, "blocked")
            producer.assert_not_called()

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
                retry_sample_ids=["character_sheet"],
                p0_errors=["character_identity_mismatch"],
            )
            save_json(paths["review"], review)
            write_visual_sample_supplemental_request(project, plan, review)
            replacement = compile_visual_sample_plan(project, context)
            by_kind = {item["kind"]: item for item in replacement["requirements"]}
            self.assertEqual(by_kind["character_sheet"]["fulfillment"], "supplemental_sample")
            self.assertEqual(by_kind["style_anchor"]["fulfillment"], "contract_preview")
            self.assertEqual(by_kind["scale_anchor"]["fulfillment"], "contract_preview")

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

    def test_storyboard_requires_current_sample_binding_scale_state_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest, agent, context = _fixture(Path(directory))
            _lock_samples(project, context)
            expected = json.loads(context.read_text(encoding="utf-8"))
            storyboard = Path(directory) / "storyboard.txt"
            storyboard.write_text("主角出发。\n", encoding="utf-8")
            semantic_binding = {
                "artifact_semantic_plan_sha256": "a" * 64,
                "artifact_semantic_plan_schema_version": "1.0",
                "artifact_semantic_plan_dependency_sha256": "b" * 64,
            }
            sample_binding = visual_sample_binding(project)
            shot = {
                "scene": 1, "story_text": "主角出发。", "narrative_function": "setup",
                "shot_size": "wide", "focal_character": "protagonist",
                "visible_characters": ["protagonist"], "excluded_characters": [],
                "continuity_group": "opening", "appearance_ids": ["protagonist_default"],
                "visual_description": "The protagonist starts the journey.",
                "scale_basis": {
                    "applicable": True, "relationship_ids": ["hero_vs_guide"],
                    "evidence": "The protagonist is visibly much smaller than the guide.",
                },
                "current_story_state": {"protagonist_condition": "state_initial"},
                "visual_state_evidence": {"protagonist_condition": "Initial feature is visible."},
            }
            payload = {
                **{key: expected[key] for key in ("contract_schema_version", "story_contract_sha256", "story_contract_dependency_sha256")},
                "contract_projection": expected["contract_projection"], **semantic_binding, **sample_binding,
                "shots": [shot],
            }
            plan_path = Path(directory) / "storyboard_plan.json"
            plan_path.write_text(json.dumps(payload), encoding="utf-8")
            with patch("story_agent.load_current_artifact_semantic_plan", return_value={}), patch(
                "story_agent.artifact_semantic_plan_binding", return_value=semantic_binding
            ):
                self.assertTrue(agent._storyboard_plan_valid(plan_path, storyboard))
                payload["shots"][0].pop("visual_state_evidence")
                plan_path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertFalse(agent._storyboard_plan_valid(plan_path, storyboard))
                payload["shots"][0]["visual_state_evidence"] = {
                    "protagonist_condition": "Initial feature is visible."
                }
                payload["visual_sample_plan_sha256"] = "0" * 64
                plan_path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertFalse(agent._storyboard_plan_valid(plan_path, storyboard))

    def test_full_story_image_review_uses_product_dimensions_and_p0_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _manifest, agent, context = _fixture(Path(directory))
            plan = _ready_plan(project, context)
            payload = _passing_review(plan, visual_sample_paths(project)["plan"])
            payload.update({
                "p0_errors": [],
                "contract_adherence": {"passed": True, "evidence": "per-shot contract evidence"},
            })
            self.assertEqual(agent._story_image_quality_review_issues(payload), [])
            payload["p0_errors"] = ["anatomy_or_organ_error"]
            payload["score"] = 100
            self.assertTrue(any("P0 hard gate" in issue for issue in agent._story_image_quality_review_issues(payload)))

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

    def test_anatomical_coherence_is_contract_and_style_relative(self) -> None:
        cases = [
            {
                "name": "strongly_stylized_character",
                "description": "A strongly stylized cartoon with deliberately oversized heads and tiny bodies.",
                "required": ["intentional exaggerated proportions"],
                "forbidden": ["unintended extra limbs"],
            },
            {
                "name": "anthropomorphic_fantasy_character",
                "description": "An anthropomorphic fantasy character whose contract-defined wings function as arms.",
                "required": ["contract-consistent fantastical anatomy"],
                "forbidden": ["unintended duplicate organs"],
            },
        ]
        for case in cases:
            with self.subTest(case=case["name"]), tempfile.TemporaryDirectory() as directory:
                project, _manifest, agent, context = _fixture(
                    Path(directory),
                    characters=True,
                    scale=False,
                    state=False,
                    style_description=case["description"],
                    required_traits=case["required"],
                    forbidden_traits=case["forbidden"],
                )
                plan = _ready_plan(project, context)
                dimensions = set(plan["review_profile"]["product_quality"])
                self.assertIn("anatomical_coherence", dimensions)
                self.assertNotIn("natural_anatomy", dimensions)
                review = _passing_review(plan, visual_sample_paths(project)["plan"])
                review["contract_adherence"]["evidence"] = "contract-relative structure verified"
                self.assertEqual(visual_sample_review_payload_issues(review, plan), [])
                self.assertEqual(agent._story_image_quality_review_issues(review), [])

    def test_anatomy_p0_rejects_unintended_extra_limbs_and_forbidden_anatomy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, _manifest, agent, context = _fixture(
                Path(directory),
                characters=True,
                scale=False,
                state=False,
                style_description="A coherent illustrated character design.",
                required_traits=["stable contract-defined body plan"],
                forbidden_traits=["unintended extra limbs", "forbidden horn anatomy"],
            )
            plan = _ready_plan(project, context)
            for defect in ("unintended extra limb", "contract-forbidden horn anatomy"):
                with self.subTest(defect=defect):
                    review = _passing_review(plan, visual_sample_paths(project)["plan"])
                    review.update(
                        score=100,
                        approved=True,
                        p0_errors=["anatomy_or_organ_error"],
                    )
                    review["contract_adherence"]["evidence"] = defect
                    sample_issues = visual_sample_review_payload_issues(review, plan)
                    story_issues = agent._story_image_quality_review_issues(review)
                    self.assertTrue(any("P0 hard gate" in issue for issue in sample_issues))
                    self.assertTrue(any("P0 hard gate" in issue for issue in story_issues))

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

    def test_legacy_project_needs_no_visual_samples_or_new_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            manifest["agent"]["story_contract"].pop("policy", None)
            manifest = as_frozen_v3_legacy(manifest)
            write_manifest(project_paths(project), manifest)
            agent = StoryAgent(agent_context_for(project))
            self.assertTrue(agent._has_visual_samples(manifest))
            self.assertTrue(agent._has_visual_sample_review(manifest))
            self.assertEqual(agent._stage_visual_samples(manifest).status, "done")
            self.assertEqual(agent._stage_visual_sample_review(manifest).status, "done")
            self.assertFalse(visual_sample_paths(project)["directory"].exists())


def agent_context_for(project: Path):
    from tests.test_story_contract_runtime import _context

    return _context(project)


if __name__ == "__main__":
    unittest.main()
