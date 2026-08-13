from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from story_agent import AgentContext, StageResult, StoryAgent
from story_agent_runtime import (
    STORY_STAGE_DEPENDENCIES,
    STORY_STAGE_SEQUENCE,
    JobRegistry,
    ensure_manifest_v2,
    file_sha256,
    render_job_report,
    resume_job,
    submit_video_job,
)
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
    legacy_passthrough_allowed,
    locked_contract_binding,
    mark_contract_consumer_completed,
    write_contract_lock,
    write_contract_consumer_context,
    write_trusted_input_chain,
)
from story_project import init_project, load_config, project_paths, save_json, write_manifest
from tests.test_story_contracts import valid_contract


STORY_TEXT = "通用测试故事\n主角出发去寻找答案。\n最后，主角学会了认真观察。\n"


def _context(project: Path) -> AgentContext:
    return AgentContext(
        project_dir=project,
        inbox=None,
        story_name="通用测试故事",
        slug="generic-contract",
        execute=True,
        update_latest_episode=False,
        codex_mode="cli",
        codex_model="gpt-5.6-sol",
        codex_sandbox="workspace-write",
        codex_approval="never",
        codex_path="codex",
        codex_timeout=30,
        codex_worker_model="gpt-5.6-luna",
        codex_reasoning_effort="xhigh",
        codex_worker_reasoning_effort="max",
    )


def _new_project(root: Path) -> tuple[Path, dict]:
    project = root / "故事剪辑：通用测试"
    manifest = init_project(project, story_name="通用测试故事", slug="generic-contract")
    story_text = project_paths(project).inputs / "story.txt"
    narration = project_paths(project).inputs / "narration.wav"
    story_text.write_text(STORY_TEXT, encoding="utf-8")
    narration.write_bytes(b"fixture")
    manifest["inputs"]["story_text"] = str(story_text)
    manifest["inputs"]["narration"] = str(narration)
    manifest = ensure_manifest_v2(manifest)
    manifest["agent"]["story_contract"]["policy"] = CONTRACT_POLICY_REQUIRED
    write_manifest(project_paths(project), manifest)
    return project, manifest


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


def _lock_contract(project: Path, manifest: dict, *, contract_payload: dict | None = None) -> tuple[StoryAgent, dict[str, Path]]:
    agent = StoryAgent(_context(project))
    paths = contract_paths(project)
    write_trusted_input_chain(paths["trusted_inputs"], build_trusted_input_chain(project, manifest, load_config()))
    save_json(paths["contract"], contract_payload or _runtime_valid_contract(project, manifest))
    paths["summary"].write_text("# 已验证合同\n", encoding="utf-8")

    def fake_review(**kwargs):
        payload = {
            "approved": True,
            "score": 96,
            "critical_errors": [],
            "issues": [],
            "retry_indices": [],
            "retry_files": [],
            "retry_instructions": [],
            "evidence_matrix": [
                {"section": section, "evidence": f"contracts.{section}"}
                for section in (
                    "semantic_artifacts", "visual_style", "characters", "world_scale",
                    "story_state", "brand", "release_layout",
                )
            ],
            "artifact_sha256": file_sha256(kwargs["bundle"]),
        }
        save_json(paths["review"], payload)
        return StageResult("done", "reviewed", paths["review"]), payload

    with patch.object(agent, "_structured_review", side_effect=fake_review):
        result = agent._stage_story_contract_review(manifest)
    if result.status != "done":
        raise AssertionError(result.message)
    return agent, paths


class StoryContractRuntimeTests(unittest.TestCase):
    def test_manifest_without_historical_evidence_requires_contract(self) -> None:
        migrated = ensure_manifest_v2({"story": {"name": "possibly-v3"}})
        self.assertEqual(migrated["agent"]["story_contract"]["policy"], CONTRACT_POLICY_REQUIRED)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "new.mp4"
            source.write_bytes(b"new-story-fixture")
            with patch(
                "story_agent_runtime.probe_source_video",
                return_value={"duration": 1.0, "video": {"width": 1920, "height": 1080}, "audio": {}},
            ):
                _job, project, created = submit_video_job(
                    source,
                    projects_root=root / "runs",
                    registry=JobRegistry(root / "registry.json"),
                )
            self.assertTrue(created)
            submitted = json.loads(project_paths(project).manifest.read_text(encoding="utf-8"))
            self.assertEqual(submitted["agent"]["story_contract"]["policy"], CONTRACT_POLICY_REQUIRED)

    def test_contract_generation_dry_run_does_not_require_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            context = replace(_context(project), execute=False)
            result = StoryAgent(context)._stage_story_contract(manifest)
            self.assertEqual(result.status, "done")
            self.assertIn("dry-run", result.message)

    def test_contract_stages_are_before_all_expensive_branches(self) -> None:
        self.assertLess(STORY_STAGE_SEQUENCE.index("story_contract_review"), STORY_STAGE_SEQUENCE.index("codex_story_images"))
        self.assertEqual(STORY_STAGE_DEPENDENCIES["story_contract"], ("setup_project",))
        self.assertEqual(STORY_STAGE_DEPENDENCIES["artifact_semantic_plan"], ("story_contract_review",))
        self.assertEqual(STORY_STAGE_DEPENDENCIES["codex_story_images"], ("artifact_semantic_plan",))
        self.assertEqual(STORY_STAGE_DEPENDENCIES["music_request"], ("story_contract_review",))
        self.assertEqual(STORY_STAGE_DEPENDENCIES["release_assets"], ("artifact_semantic_plan",))

    def test_contract_generation_uses_luna_and_rejects_spoofed_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            agent = StoryAgent(_context(project))
            paths = contract_paths(project)
            paths["directory"].mkdir(parents=True, exist_ok=True)
            paths["lock"].write_text('{"stale": true}\n', encoding="utf-8")

            def fake_task(**kwargs):
                self.assertEqual(agent.context.codex_route(kwargs["stage"])[0], "worker")
                chain = build_trusted_input_chain(project, manifest, load_config())
                contract = _runtime_valid_contract(project, manifest)
                # A generator cannot mint a task_input receipt or reuse a false hash.
                contract["contracts"]["characters"]["mode_provenance"] = {
                    "source": "task_input",
                    "source_ref": "task_input.story_text",
                    "source_sha256": "f" * 64,
                    "evidence_quote": "通用测试故事",
                }
                save_json(paths["contract"], contract)
                paths["summary"].write_text("# Contract\n", encoding="utf-8")
                return StageResult("done", "generated")

            with patch.object(agent, "_codex_task", side_effect=fake_task):
                result = agent._stage_story_contract(manifest)
            self.assertEqual(result.status, "blocked")
            self.assertIn("可信来源", result.message)
            self.assertFalse(paths["lock"].exists())

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

    def test_independent_review_hash_binds_and_runtime_lock_invalidates_on_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            agent = StoryAgent(_context(project))
            paths = contract_paths(project)
            chain = build_trusted_input_chain(project, manifest, load_config())
            write_trusted_input_chain(paths["trusted_inputs"], chain)
            save_json(paths["contract"], _runtime_valid_contract(project, manifest))
            paths["summary"].write_text("# 已验证合同\n", encoding="utf-8")
            self.assertEqual(contract_runtime_issues(project), [])

            def fake_review(**kwargs):
                self.assertEqual(agent.context.codex_route(kwargs["stage"])[0], "commander")
                payload = {
                    "approved": True,
                    "score": 96,
                    "critical_errors": [],
                    "issues": [],
                    "retry_indices": [],
                    "retry_files": [],
                    "retry_instructions": [],
                    "evidence_matrix": [
                        {"section": section, "evidence": f"contracts.{section}"}
                        for section in (
                            "semantic_artifacts",
                            "visual_style",
                            "characters",
                            "world_scale",
                            "story_state",
                            "brand",
                            "release_layout",
                        )
                    ],
                    "artifact_sha256": file_sha256(kwargs["bundle"]),
                }
                save_json(paths["review"], payload)
                return StageResult("done", "reviewed", paths["review"]), payload

            with patch.object(agent, "_structured_review", side_effect=fake_review):
                result = agent._stage_story_contract_review(manifest)
            self.assertEqual(result.status, "done")
            self.assertTrue(agent._has_story_contract_review(manifest))
            self.assertTrue(contract_lock_is_current(project, bundle=paths["bundle"], review=paths["review"]))
            paths["summary"].write_text("# 被篡改的说明\n", encoding="utf-8")
            self.assertFalse(agent._has_story_contract_review(manifest))

    def test_contract_review_requires_evidence_for_all_seven_sections(self) -> None:
        payload = {"evidence_matrix": [{"section": "visual_style", "evidence": "contracts.visual_style"}]}
        issues = contract_review_payload_issues(payload)
        self.assertTrue(any("缺少合同节" in issue for issue in issues), issues)

    def test_legacy_project_passes_both_new_predicates_without_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "legacy"
            manifest = init_project(project, story_name="legacy", slug="legacy")
            manifest["created_at"] = "2026-08-11 12:00:00"
            manifest["agent"]["stages"]["setup_project"] = {"status": "passed"}
            manifest = ensure_manifest_v2(manifest)
            agent = StoryAgent(_context(project), read_only=True)
            self.assertTrue(agent._has_story_contract(manifest))
            self.assertTrue(agent._has_story_contract_review(manifest))
            self.assertEqual(agent._stage_story_contract(manifest).status, "done")
            self.assertEqual(agent._stage_story_contract_review(manifest).status, "done")

    def test_fresh_manifest_cannot_self_declare_legacy_passthrough(self) -> None:
        manifest = {
            "created_at": "2026-08-13 12:00:00",
            "story": {"name": "new", "slug": "new"},
            "agent": {
                "story_contract": {"policy": CONTRACT_POLICY_LEGACY},
                "stages": {"setup_project": {"status": "passed"}},
            },
        }
        migrated = ensure_manifest_v2(manifest)
        self.assertEqual(migrated["agent"]["story_contract"]["policy"], CONTRACT_POLICY_REQUIRED)
        self.assertFalse(legacy_passthrough_allowed(migrated))

    def test_legacy_receipt_is_bound_and_policy_edit_cannot_forge_it(self) -> None:
        manifest = {
            "created_at": "2026-08-11 12:00:00",
            "story": {"name": "old", "slug": "old"},
            "agent": {"stages": {"setup_project": {"status": "passed"}}},
        }
        migrated = ensure_manifest_v2(manifest)
        self.assertTrue(legacy_passthrough_allowed(migrated))
        migrated["story"]["slug"] = "forged"
        self.assertFalse(legacy_passthrough_allowed(migrated))

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

    def test_diagnostics_report_contract_review_lock_and_consumer_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            _agent, paths = _lock_contract(project, manifest)
            for consumer in CONTRACT_CONSUMER_SECTIONS:
                write_contract_consumer_context(project, consumer)
                mark_contract_consumer_completed(project, consumer)
            status = contract_diagnostics(project, manifest)
            self.assertEqual(status["policy"], CONTRACT_POLICY_REQUIRED)
            self.assertTrue(status["contract"]["valid"])
            self.assertTrue(status["review"]["current_and_approved"])
            self.assertTrue(status["lock"]["valid"])
            self.assertTrue(all(item["request_manifest"] == "current" for item in status["consumers"].values()))
            self.assertTrue(all(item["completed_receipt"] == "current" for item in status["consumers"].values()))

            contract_consumer_path(project, "cover").write_text("not-json\n", encoding="utf-8")
            paths["lock"].write_text('{"status":"locked"}\n', encoding="utf-8")
            resumed = resume_job(project)
            status = contract_diagnostics(project, resumed)
            self.assertFalse(status["lock"]["valid"])
            self.assertEqual(status["consumers"]["cover"]["request_manifest"], "damaged")
            self.assertNotEqual(status["consumers"]["cover"]["completed_receipt"], "current")
            report = render_job_report(project).read_text(encoding="utf-8")
            self.assertIn("## Story Production Contract", report)
            self.assertIn("| cover | brand, characters, release_layout | damaged |", report)

    def test_existing_status_cli_exposes_read_only_contract_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            _lock_contract(project, manifest)
            output = io.StringIO()
            with redirect_stdout(output):
                StoryAgent(_context(project), read_only=True).status()
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["story_contract"]["policy"], CONTRACT_POLICY_REQUIRED)
            self.assertTrue(payload["story_contract"]["lock"]["valid"])
            self.assertEqual(
                set(payload["story_contract"]["consumers"]),
                set(CONTRACT_CONSUMER_SECTIONS),
            )

    def test_diagnostics_never_forges_contract_state_for_eligible_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "legacy"
            manifest = init_project(project, story_name="legacy", slug="legacy")
            manifest["created_at"] = "2026-08-11 12:00:00"
            manifest["agent"]["stages"]["setup_project"] = {"status": "passed"}
            manifest = ensure_manifest_v2(manifest)
            write_manifest(project_paths(project), manifest)
            status = contract_diagnostics(project, manifest)
            self.assertEqual(status["policy"], CONTRACT_POLICY_LEGACY)
            self.assertTrue(status["legacy_eligible"])
            self.assertFalse(status["contract"]["exists"])
            self.assertFalse(status["review"]["review_exists"])
            self.assertFalse(status["lock"]["exists"])
            self.assertTrue(all(item["request_manifest"] == "legacy_passthrough" for item in status["consumers"].values()))

    def test_section_changes_invalidate_only_declared_consumer_families(self) -> None:
        expected_stale = {
            "semantic_artifacts": {"storyboard_images", "music", "product_package"},
            "visual_style": {"storyboard_images"},
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

    def test_interrupted_generation_or_failed_review_cannot_leave_a_valid_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            agent, paths = _lock_contract(project, manifest)
            with patch.object(agent, "_codex_task", return_value=StageResult("blocked", "worker interrupted")):
                result = agent._stage_story_contract(manifest)
            self.assertEqual(result.status, "blocked")
            self.assertFalse(paths["lock"].exists())

            save_json(paths["contract"], _runtime_valid_contract(project, manifest))
            paths["summary"].write_text("# regenerated\n", encoding="utf-8")
            write_trusted_input_chain(
                paths["trusted_inputs"], build_trusted_input_chain(project, manifest, load_config())
            )
            with patch.object(
                agent,
                "_structured_review",
                return_value=(StageResult("blocked", "review rejected"), None),
            ):
                result = agent._stage_story_contract_review(manifest)
            self.assertEqual(result.status, "blocked")
            self.assertFalse(paths["lock"].exists())


if __name__ == "__main__":
    unittest.main()
