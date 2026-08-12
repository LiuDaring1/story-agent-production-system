from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from story_contracts import (
    ContractSection,
    RuleConflictError,
    RuleSource,
    STORY_CONTRACT_SCHEMA_VERSION,
    StoryContractValidationError,
    StoryProductionContract,
    contract_sha256,
    load_contract_schema,
    load_story_contract,
    resolve_rule_candidates,
    validate_story_contract,
    validate_story_contract_or_raise,
)


def provenance(source: str = "agent_inference", ref: str = "automatic analysis", order: int = 0) -> dict:
    return {"source": source, "source_ref": ref, "source_order": order}


def valid_contract(*, with_characters: bool = True) -> dict:
    characters = (
        [
            {
                "character_id": "protagonist",
                "display_name": "主角",
                "role": "protagonist",
                "species_or_form": "story-defined creature",
                "identity_anchors": ["round eyes", "warm brown palette"],
                "required_features": ["same facial design in every scene"],
                "forbidden_features": ["invented accessories"],
                "appearance_variants": ["default"],
                "provenance": provenance("task_input", "confirmed story text"),
            },
            {
                "character_id": "guide",
                "display_name": "引导者",
                "role": "supporting",
                "species_or_form": "story-defined creature",
                "identity_anchors": ["taller silhouette"],
                "required_features": [],
                "forbidden_features": ["identity drift"],
                "appearance_variants": [],
                "provenance": provenance(),
            },
        ]
        if with_characters
        else []
    )
    relationships = (
        [
            {
                "relationship_id": "hero_vs_guide",
                "subject": "character:protagonist",
                "reference": "character:guide",
                "qualitative_relation": "much_smaller",
                "visual_guidance": "The protagonist reads clearly smaller without requiring an exact ratio.",
                "provenance": provenance("task_input", "story explicitly contrasts their size"),
            }
        ]
        if with_characters
        else []
    )
    machines = (
        [
            {
                "machine_id": "protagonist_condition",
                "entity_ref": "character:protagonist",
                "initial_state": "state_initial",
                "states": [
                    {
                        "state_id": "state_initial",
                        "description": "initial appearance",
                        "required": ["initial feature visible"],
                        "forbidden": [],
                        "provenance": provenance("task_input", "opening story line"),
                    },
                    {
                        "state_id": "state_changed",
                        "description": "appearance after story event",
                        "required": ["changed feature visible"],
                        "forbidden": ["initial feature restored too early"],
                        "provenance": provenance("task_input", "state-changing story line"),
                    },
                ],
                "transitions": [
                    {
                        "from": "state_initial",
                        "to": "state_changed",
                        "trigger": "the explicit story event occurs",
                        "boundary_ref": "source-line:3",
                        "provenance": provenance("task_input", "source line 3"),
                    }
                ],
                "provenance": provenance("task_input", "confirmed story text"),
            }
        ]
        if with_characters
        else []
    )
    return {
        "schema_version": STORY_CONTRACT_SCHEMA_VERSION,
        "contract_id": "fixture-contract-v1",
        "story": {
            "story_id": "generic-fixture",
            "title": "通用测试故事",
            "source_sha256": hashlib.sha256("通用测试故事".encode()).hexdigest(),
        },
        "contracts": {
            "semantic_artifacts": {
                "rules": [],
                "mappings": [
                    {
                        "semantic_kind": "story_body",
                        "artifact": "demo",
                        "action": "include",
                        "subtitle_policy": "show",
                        "provenance": provenance("brand_or_global_default", "default semantic output policy"),
                    }
                ],
            },
            "visual_style": {
                "rules": [
                    {
                        "rule_id": "visual.render_style",
                        "value": "warm_storybook",
                        "provenance": provenance("project_config", "project visual profile"),
                    }
                ],
                "style_profile": {
                    "style_id": "warm_storybook",
                    "description": "Warm, coherent children's story imagery.",
                    "required_traits": ["consistent rendering"],
                    "forbidden_traits": ["photorealistic identity drift"],
                    "provenance": provenance("project_config", "project visual profile"),
                },
            },
            "characters": {
                "rules": [],
                "mode": "present" if with_characters else "none",
                "mode_provenance": provenance("task_input", "confirmed story text"),
                **(
                    {"no_character_reason": "The story is an abstract visual poem."}
                    if not with_characters
                    else {}
                ),
                "characters": characters,
            },
            "world_scale": {"rules": [], "relationships": relationships},
            "story_state": {"rules": [], "machines": machines},
            "brand": {
                "rules": [
                    {
                        "rule_id": "brand.generated_logo_allowed",
                        "value": False,
                        "provenance": provenance("brand_or_global_default", "official brand policy"),
                    }
                ],
                "assets": [
                    {
                        "asset_id": "official_logo",
                        "sha256": "a" * 64,
                        "allowed_uses": ["cover", "release_video"],
                        "max_per_frame": 1,
                        "provenance": provenance("brand_or_global_default", "official asset registry"),
                    }
                ],
            },
            "release_layout": {
                "rules": [],
                "variants": [
                    {
                        "variant_id": "portrait_main",
                        "aspect_ratio": "3:4",
                        "regions": [
                            {
                                "region_id": "story_area",
                                "role": "story_media",
                                "x": 0.08,
                                "y": 0.20,
                                "width": 0.84,
                                "height": 0.56,
                                "provenance": provenance("project_config", "main account layout"),
                            }
                        ],
                        "provenance": provenance("project_config", "main account layout"),
                    }
                ],
            },
        },
        "preview_assets": [
            {
                "preview_id": "style_probe",
                "kind": "style_anchor",
                "need_reason": "The inferred style must be inspected before batch generation.",
                "content_refs": [],
                "provenance": provenance(),
            },
            *(
                [
                    {
                        "preview_id": "character_probe",
                        "kind": "character_sheet",
                        "need_reason": "The story has recurring characters whose identity must remain stable.",
                        "content_refs": ["protagonist", "guide"],
                        "provenance": provenance(),
                    },
                    {
                        "preview_id": "scale_probe",
                        "kind": "scale_anchor",
                        "need_reason": "The story explicitly contrasts the two characters' size.",
                        "content_refs": ["hero_vs_guide"],
                        "provenance": provenance("task_input", "explicit story contrast"),
                    },
                ]
                if with_characters
                else []
            ),
        ],
    }


class StoryContractTests(unittest.TestCase):
    def test_all_versioned_schema_documents_load(self) -> None:
        root = load_contract_schema()
        self.assertEqual(root["properties"]["schema_version"]["const"], "1.0")
        for section in ContractSection:
            schema = load_contract_schema(section)
            self.assertEqual(schema["type"], "object")

    def test_valid_contract_passes_and_hash_is_stable(self) -> None:
        contract = valid_contract()
        self.assertEqual(validate_story_contract(contract), [])
        self.assertEqual(contract_sha256(contract), contract_sha256(copy.deepcopy(contract)))

    def test_core_model_detaches_caller_data_and_returns_detached_sections(self) -> None:
        source = valid_contract()
        model = StoryProductionContract.from_mapping(source)
        original_hash = model.sha256
        source["contract_id"] = "mutated-outside"
        section = model.section(ContractSection.VISUAL_STYLE)
        section["style_profile"]["style_id"] = "mutated-copy"
        self.assertEqual(model.contract_id, "fixture-contract-v1")
        self.assertEqual(model.sha256, original_hash)
        self.assertEqual(model.section("visual_style")["style_profile"]["style_id"], "warm_storybook")

    def test_contract_loader_rejects_invalid_json_and_accepts_valid_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            path.write_text(json.dumps(valid_contract(), ensure_ascii=False), encoding="utf-8")
            self.assertEqual(load_story_contract(path)["contract_id"], "fixture-contract-v1")
            path.write_text("not-json", encoding="utf-8")
            with self.assertRaises(StoryContractValidationError):
                load_story_contract(path)

    def test_source_precedence_is_automatic_and_records_overridden_rules(self) -> None:
        resolved = resolve_rule_candidates(
            [
                {"rule_id": "style", "value": "agent", "provenance": provenance()},
                {
                    "rule_id": "style",
                    "value": "brand",
                    "provenance": provenance("brand_or_global_default", "global style"),
                },
                {
                    "rule_id": "style",
                    "value": "project",
                    "provenance": provenance("project_config", "project config"),
                },
                {
                    "rule_id": "style",
                    "value": "explicit",
                    "provenance": provenance("task_input", "current task"),
                },
            ]
        )["style"]
        self.assertEqual(resolved.value, "explicit")
        self.assertEqual(resolved.provenance["source"], RuleSource.TASK_INPUT.value)
        self.assertEqual(len(resolved.overridden), 3)

    def test_later_instruction_wins_within_same_source(self) -> None:
        resolved = resolve_rule_candidates(
            [
                {"rule_id": "layout", "value": "old", "provenance": provenance("task_input", "turn 1", 1)},
                {"rule_id": "layout", "value": "new", "provenance": provenance("task_input", "turn 2", 2)},
            ]
        )["layout"]
        self.assertEqual(resolved.value, "new")

    def test_equal_precedence_conflict_is_exceptional(self) -> None:
        with self.assertRaises(RuleConflictError):
            resolve_rule_candidates(
                [
                    {"rule_id": "style", "value": "a", "provenance": provenance("project_config", "a", 0)},
                    {"rule_id": "style", "value": "b", "provenance": provenance("project_config", "b", 0)},
                ]
            )

    def test_abstract_story_does_not_require_character_or_scale_previews(self) -> None:
        contract = valid_contract(with_characters=False)
        self.assertEqual(validate_story_contract(contract), [])
        kinds = {asset["kind"] for asset in contract["preview_assets"]}
        self.assertNotIn("character_sheet", kinds)
        self.assertNotIn("scale_anchor", kinds)

    def test_character_preview_is_rejected_for_story_without_characters(self) -> None:
        contract = valid_contract(with_characters=False)
        contract["preview_assets"].append(
            {
                "preview_id": "invented_character_probe",
                "kind": "character_sheet",
                "need_reason": "forced template",
                "content_refs": ["invented"],
                "provenance": provenance(),
            }
        )
        codes = {issue.code for issue in validate_story_contract(contract)}
        self.assertIn("conditional", codes)
        self.assertIn("unknown_reference", codes)

    def test_scale_is_qualitative_by_default(self) -> None:
        contract = valid_contract()
        relation = contract["contracts"]["world_scale"]["relationships"][0]
        self.assertNotIn("numeric_range", relation)
        validate_story_contract_or_raise(contract)

    def test_numeric_scale_requires_basis_and_wide_tolerance(self) -> None:
        contract = valid_contract()
        relation = contract["contracts"]["world_scale"]["relationships"][0]
        relation["numeric_range"] = {
            "min_ratio": 0.33,
            "target_ratio": 0.34,
            "max_ratio": 0.35,
        }
        issues = validate_story_contract(contract)
        self.assertTrue(any(issue.code == "false_precision" for issue in issues))
        self.assertTrue(any(issue.path.endswith(".basis") for issue in issues))

        relation["numeric_range"] = {
            "min_ratio": 0.25,
            "target_ratio": 0.35,
            "max_ratio": 0.45,
            "basis": {"kind": "story_explicit", "evidence": "The source explicitly calls one subject tiny."},
        }
        self.assertEqual(validate_story_contract(contract), [])

    def test_cross_references_and_layout_bounds_are_verified(self) -> None:
        contract = valid_contract()
        contract["contracts"]["world_scale"]["relationships"][0]["subject"] = "character:missing"
        contract["contracts"]["story_state"]["machines"][0]["transitions"][0]["to"] = "missing_state"
        contract["contracts"]["release_layout"]["variants"][0]["regions"][0]["width"] = 0.99
        issues = validate_story_contract(contract)
        self.assertGreaterEqual(sum(issue.code == "unknown_reference" for issue in issues), 2)
        self.assertTrue(any(issue.code == "bounds" for issue in issues))

    def test_preview_paths_are_portable_and_hash_bound(self) -> None:
        contract = valid_contract()
        preview = contract["preview_assets"][0]
        preview["path"] = "/private/tmp/style.png"
        issues = validate_story_contract(contract)
        self.assertTrue(any(issue.code == "paired_fields" for issue in issues))
        self.assertTrue(any(issue.code == "portable_path" for issue in issues))


if __name__ == "__main__":
    unittest.main()
