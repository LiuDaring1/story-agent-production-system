from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from story_evidence import file_sha256
from story_contract_runtime import (
    CONTRACT_CONSUMER_SECTIONS,
    CONTRACT_POLICY_LEGACY,
    CONTRACT_POLICY_REQUIRED,
    build_trusted_input_chain,
    contract_lock_is_current,
    contract_paths,
    contract_runtime_issues,
    contract_review_payload_issues,
    contract_consumer_completion_is_current,
    contract_consumer_context_is_current,
    contract_consumer_path,
    contract_diagnostics,
    enforce_targeted_contract_revision,
    legacy_passthrough_allowed,
    locked_contract_binding,
    mark_contract_consumer_completed,
    normalize_contract_policy_provenance,
    write_contract_lock,
    write_contract_consumer_context,
    write_trusted_input_chain,
)
from story_project import (
    detect_project_assets,
    ensure_story_frame_variants,
    init_project,
    load_config,
    project_paths,
    save_json,
    write_manifest,
)
from tests.test_story_contracts import valid_contract


STORY_TEXT = "通用测试故事\n主角出发去寻找答案。\n最后，主角学会了认真观察。\n"






def _runtime_valid_contract(project: Path, manifest: dict) -> dict:
    contract = valid_contract(with_characters=False)
    contract["story"].update(
        {
            "story_id": "generic-contract",
            "title": "通用测试故事",
            "source_sha256": hashlib.sha256(STORY_TEXT.encode("utf-8")).hexdigest(),
        }
    )
    chain = build_trusted_input_chain(project, manifest, load_config())
    task = next(item for item in chain["sources"] if item["source"] == "task_input")
    default = next(item for item in chain["sources"] if item["source"] == "brand_or_global_default")

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"provenance", "mode_provenance"} and isinstance(child, dict):
                    if child.get("source") == "task_input":
                        child.clear()
                        child.update(
                            {
                                "source": "task_input",
                                "source_ref": task["source_ref"],
                                "source_sha256": task["sha256"],
                                "evidence_quote": "通用测试故事",
                                "source_order": 0,
                            }
                        )
                    elif child.get("source") != "agent_inference":
                        child.clear()
                        child.update(
                            {
                                "source": "brand_or_global_default",
                                "source_ref": default["source_ref"],
                                "source_sha256": default["sha256"],
                                "evidence_pointer": "/default_image_style",
                                "source_order": 0,
                            }
                        )
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(contract)
    return contract




class StoryContractRuntimeTests(unittest.TestCase):
    def test_theme_qa_preflight_does_not_mutate_existing_receipted_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            theme = Path(directory)
            frame = theme / "story_frame_a.png"
            source = theme / "story_frame_source.png"
            Image.new("RGBA", (32, 18), (240, 90, 20, 255)).save(frame)
            Image.new("RGB", (32, 18), (255, 0, 255)).save(source)
            before = hashlib.sha256(frame.read_bytes()).hexdigest()

            ensure_story_frame_variants(theme, (2, 2, 20, 10))

            self.assertEqual(hashlib.sha256(frame.read_bytes()).hexdigest(), before)

    def test_trusted_input_chain_reads_docx_story_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            story_docx = project_paths(project).inputs / "story.docx"
            document_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>第一自然段。</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>第二自然段！</w:t></w:r></w:p></w:body></w:document>'
            )
            with zipfile.ZipFile(story_docx, "w") as archive:
                archive.writestr("word/document.xml", document_xml)
            manifest["inputs"]["story_text"] = str(story_docx)

            chain = build_trusted_input_chain(project, manifest, load_config())

            self.assertEqual(chain["sources"][0]["text"], "第一自然段。\n第二自然段！")
            self.assertEqual(chain["sources"][0]["project_relative_path"], "00_输入素材/story.docx")

    def test_asset_detection_preserves_explicit_story_text_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            bound = Path(manifest["inputs"]["story_text"])
            competing_docx = project / "文稿：自动发现但未绑定.docx"
            competing_docx.write_bytes(b"not-selected")
            write_manifest(project_paths(project), manifest)

            detected = detect_project_assets(project, extract_audio=False)

            self.assertEqual(detected["inputs"]["story_text"], str(bound))







    def test_generator_cannot_rewrite_runtime_trusted_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            paths = contract_paths(project)
            chain = build_trusted_input_chain(project, manifest, load_config())
            write_trusted_input_chain(paths["trusted_inputs"], chain)
            save_json(paths["contract"], _runtime_valid_contract(project, manifest))
            paths["summary"].write_text("# Contract\n", encoding="utf-8")
            forged = json.loads(paths["trusted_inputs"].read_text(encoding="utf-8"))
            forged["sources"][0]["text"] += "\n伪造的任务要求"
            save_json(paths["trusted_inputs"], forged)
            issues = contract_runtime_issues(project)
            self.assertTrue(any("source_drift" in issue for issue in issues), issues)

    def test_project_config_receipt_contains_value_not_override_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            manifest["story"]["image_style"] = "项目指定的通用绘本风格"
            manifest["story"]["manual_overrides"] = {"image_style": True}
            chain = build_trusted_input_chain(project, manifest, load_config())
            project_source = next(
                item for item in chain["sources"] if item["source"] == "project_config"
            )
            self.assertEqual(
                project_source["json"],
                {"image_style": "项目指定的通用绘本风格"},
            )

    def test_trusted_defaults_bind_the_resolved_style_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            chain = build_trusted_input_chain(project, manifest, load_config())
            default_source = next(
                item for item in chain["sources"] if item["source"] == "brand_or_global_default"
            )
            resolved = default_source["json"]["resolved_image_style"]
            self.assertEqual(resolved["key"], "3d_cartoon")
            self.assertEqual(resolved["label"], "3D卡通")
            self.assertIn("圆润可爱的角色比例", resolved["prompt"])
            self.assertIn("材质细腻但不过度真实", resolved["prompt"])

    def test_runtime_downgrades_story_routing_and_composite_brand_policy_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            paths = contract_paths(project)
            chain = build_trusted_input_chain(project, manifest, load_config())
            contract = _runtime_valid_contract(project, manifest)
            task = next(item for item in chain["sources"] if item["source"] == "task_input")
            contract["contracts"]["semantic_artifacts"]["mappings"][0]["provenance"] = {
                "source": "task_input",
                "source_ref": task["source_ref"],
                "source_sha256": task["sha256"],
                "evidence_quote": "通用测试故事",
            }
            save_json(paths["contract"], contract)

            changes = normalize_contract_policy_provenance(paths["contract"], chain)

            self.assertGreaterEqual(len(changes), 2)
            updated = json.loads(paths["contract"].read_text(encoding="utf-8"))
            mapping = updated["contracts"]["semantic_artifacts"]["mappings"][0]
            self.assertEqual(mapping["provenance"]["source"], "agent_inference")
            asset = updated["contracts"]["brand"]["assets"][0]
            self.assertEqual(asset["provenance"]["source"], "agent_inference")


    def test_contract_review_requires_evidence_for_all_seven_sections(self) -> None:
        payload = {"evidence_matrix": [{"section": "visual_style", "evidence": "contracts.visual_style"}]}
        issues = contract_review_payload_issues(payload)
        self.assertTrue(any("缺少合同节" in issue for issue in issues), issues)

    def test_contract_review_accepts_structured_evidence_and_auxiliary_scope_row(self) -> None:
        sections = (
            "semantic_artifacts",
            "visual_style",
            "characters",
            "world_scale",
            "story_state",
            "brand",
            "release_layout",
        )
        payload = {
            "approved": True,
            "score": 96,
            "critical_errors": [],
            "evidence_matrix": [
                {
                    "section": section,
                    "conclusion": "该节与可信输入一致。",
                    "contract_paths": [f"/contracts/{section}"],
                    "trusted_evidence": [{"file": "trusted.json", "path": "/sources/0"}],
                }
                for section in sections
            ]
            + [
                {
                    "section": "review_boundary",
                    "conclusion": "只审核文本合同。",
                    "contract_paths": ["/preview_assets"],
                    "trusted_evidence": [{"file": "contract.json", "path": "/preview_assets"}],
                }
            ]
        }
        self.assertEqual(contract_review_payload_issues(payload), [])

    def test_rejected_contract_review_requires_explicit_revision_sections(self) -> None:
        payload = {
            "approved": False,
            "evidence_matrix": [
                {"section": section, "evidence": f"contracts.{section}"}
                for section in (
                    "semantic_artifacts", "visual_style", "characters", "world_scale",
                    "story_state", "brand", "release_layout",
                )
            ],
        }
        issues = contract_review_payload_issues(payload)
        self.assertTrue(any("retry_contract_sections" in issue for issue in issues), issues)

    def test_runtime_restores_sections_outside_targeted_contract_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rejected_path = root / "rejected.json"
            revised_path = root / "revised.json"
            receipt_path = root / "scope.json"
            rejected = valid_contract(with_characters=True)
            revised = json.loads(json.dumps(rejected))
            revised["story"]["title"] = "worker drifted title"
            revised["contracts"]["characters"]["rules"].append(
                {
                    "rule_id": "characters.targeted_fix",
                    "value": "allowed change",
                    "provenance": {
                        "source": "agent_inference",
                        "source_ref": "agent_inference.targeted_fix",
                        "confidence": 0.8,
                    },
                }
            )
            revised["contracts"]["brand"]["rules"].append(
                {
                    "rule_id": "brand.unrelated_drift",
                    "value": "must be discarded",
                    "provenance": {
                        "source": "agent_inference",
                        "source_ref": "agent_inference.unrelated_drift",
                        "confidence": 0.8,
                    },
                }
            )
            save_json(rejected_path, rejected)
            save_json(revised_path, revised)
            enforce_targeted_contract_revision(
                rejected_path,
                revised_path,
                allowed_sections=("characters",),
                receipt_path=receipt_path,
            )
            guarded = json.loads(revised_path.read_text(encoding="utf-8"))
            self.assertEqual(guarded["story"], rejected["story"])
            self.assertEqual(guarded["contracts"]["brand"], rejected["contracts"]["brand"])
            self.assertNotEqual(
                guarded["contracts"]["characters"], rejected["contracts"]["characters"]
            )
            self.assertEqual(guarded["preview_assets"], [])
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["allowed_sections"], ["characters"])
            self.assertIn("brand", receipt["restored_sections"])







    def test_consumer_receipt_requires_complete_current_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            _lock_contract(project, manifest)
            context = write_contract_consumer_context(project, "cover")
            self.assertTrue(contract_consumer_context_is_current(project, "cover"))
            mark_contract_consumer_completed(project, "cover")
            self.assertTrue(contract_consumer_completion_is_current(project, "cover"))
            payload = json.loads(context.read_text(encoding="utf-8"))
            payload.pop("contract_projection")
            save_json(context, payload)
            self.assertFalse(contract_consumer_context_is_current(project, "cover"))
            self.assertFalse(contract_consumer_completion_is_current(project, "cover"))

    def test_damaged_or_incomplete_lock_never_unlocks_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            _agent, paths = _lock_contract(project, manifest)
            self.assertEqual(locked_contract_binding(project, "image_video")["consumer"], "image_video")
            valid = paths["lock"].read_bytes()
            for damaged in (b'{"version":1}\n', valid[: max(1, len(valid) // 2)], b"not-json\n"):
                paths["lock"].write_bytes(damaged)
                self.assertFalse(contract_lock_is_current(project, bundle=paths["bundle"], review=paths["review"]))
                with self.assertRaises(ValueError):
                    locked_contract_binding(project, "image_video")
                paths["lock"].write_bytes(valid)

    def test_failed_lock_replace_preserves_last_valid_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            _agent, paths = _lock_contract(project, manifest)
            previous = paths["lock"].read_bytes()
            with patch("story_contract_runtime.os.replace", side_effect=OSError("simulated crash")):
                with self.assertRaises(OSError):
                    write_contract_lock(project, bundle=paths["bundle"], review=paths["review"])
            self.assertEqual(paths["lock"].read_bytes(), previous)
            self.assertTrue(contract_lock_is_current(project, bundle=paths["bundle"], review=paths["review"]))
            self.assertEqual(list(paths["lock"].parent.glob(f".{paths['lock'].name}.*.tmp")), [])

    def test_unrelated_contract_section_does_not_invalidate_consumer_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            _agent, paths = _lock_contract(project, manifest)
            before = write_contract_consumer_context(project, "image_video")
            before_payload = json.loads(before.read_text(encoding="utf-8"))
            # image_video depends on characters + story_state, not brand.
            contract = json.loads(paths["contract"].read_text(encoding="utf-8"))
            contract["contracts"]["brand"]["rules"].append({
                "rule_id": "brand-extra",
                "value": "额外品牌规则",
                "provenance": {
                    "source": "agent_inference",
                    "source_ref": "agent_inference.brand-extra",
                    "confidence": 0.5,
                },
            })
            save_json(paths["contract"], contract)
            # Re-review/re-lock the changed contract.
            _lock_contract(project, manifest, contract_payload=contract)
            current = locked_contract_binding(project, "image_video")
            self.assertEqual(
                before_payload["story_contract_dependency_sha256"],
                current["story_contract_dependency_sha256"],
            )
            self.assertTrue(contract_consumer_context_is_current(project, "image_video"))
            self.assertNotEqual(before_payload["story_contract_sha256"], current["story_contract_sha256"])

    def test_section_changes_invalidate_only_declared_consumer_families(self) -> None:
        expected_stale = {
            "semantic_artifacts": {"storyboard_images", "music", "cover", "product_package"},
            "visual_style": {"storyboard_images", "cover"},
            "characters": {"storyboard_images", "image_video", "cover"},
            "world_scale": {"storyboard_images"},
            "story_state": {"storyboard_images", "image_video", "music"},
            "brand": {"cover", "release_video"},
            "release_layout": {"cover", "release_video"},
        }
        for changed_section, expected in expected_stale.items():
            with self.subTest(section=changed_section), tempfile.TemporaryDirectory() as directory:
                project, manifest = _new_project(Path(directory))
                _agent, paths = _lock_contract(project, manifest)
                for consumer in CONTRACT_CONSUMER_SECTIONS:
                    write_contract_consumer_context(project, consumer)
                    mark_contract_consumer_completed(project, consumer)
                contract = json.loads(paths["contract"].read_text(encoding="utf-8"))
                contract["contracts"][changed_section]["rules"].append({
                    "rule_id": f"diagnostic.{changed_section}",
                    "value": f"changed-{changed_section}",
                    "provenance": {
                        "source": "agent_inference",
                        "source_ref": f"agent_inference.diagnostic.{changed_section}",
                        "confidence": 0.5,
                    },
                })
                _lock_contract(project, manifest, contract_payload=contract)
                status = contract_diagnostics(project, manifest)
                stale = {
                    name
                    for name, item in status["consumers"].items()
                    if item["request_manifest"] == "stale"
                }
                self.assertEqual(stale, expected)
                for name, item in status["consumers"].items():
                    self.assertEqual(item["changed_sections"], [changed_section] if name in expected else [])
                    self.assertEqual(
                        item["completed_receipt"],
                        "stale" if name in expected else "current",
                    )









if __name__ == "__main__":
    unittest.main()


def _new_project(root: Path) -> tuple[Path, dict]:
    project = root / "故事剪辑：通用测试"
    manifest = init_project(project, story_name="通用测试故事", slug="generic-contract")
    story_text = project_paths(project).inputs / "story.txt"
    narration = project_paths(project).inputs / "narration.wav"
    story_text.write_text(STORY_TEXT, encoding="utf-8")
    narration.write_bytes(b"fixture")
    manifest["inputs"]["story_text"] = str(story_text)
    manifest["inputs"]["narration"] = str(narration)
    manifest.setdefault("agent", {}).setdefault("story_contract", {})
    manifest["agent"]["story_contract"]["policy"] = CONTRACT_POLICY_REQUIRED
    write_manifest(project_paths(project), manifest)
    return project, manifest


def _lock_contract(project, manifest, *, contract_payload=None):
    """Build a read-only compatibility fixture without loading the retired Agent."""
    from story_evidence import write_review_bundle
    paths = contract_paths(project)
    write_trusted_input_chain(paths["trusted_inputs"], build_trusted_input_chain(project, manifest, load_config()))
    save_json(paths["contract"], contract_payload or _runtime_valid_contract(project, manifest))
    paths["summary"].write_text("# 已验证合同\n", encoding="utf-8")
    write_review_bundle(paths["bundle"], [paths["contract"], paths["summary"], paths["trusted_inputs"]])
    save_json(paths["review"], {
        "approved": True, "score": 96, "critical_errors": [],
        "artifact_sha256": file_sha256(paths["bundle"]),
        "evidence_matrix": [{"section": section, "evidence": f"contracts.{section}"}
                            for section in ("semantic_artifacts", "visual_style", "characters", "world_scale", "story_state", "brand", "release_layout")],
    })
    write_contract_lock(project, bundle=paths["bundle"], review=paths["review"])
    return None, paths
