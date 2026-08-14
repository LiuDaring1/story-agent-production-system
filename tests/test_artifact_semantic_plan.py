from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from artifact_semantic_plan import (
    ARTIFACTS,
    artifact_semantic_plan_is_current,
    compile_artifact_semantic_plan,
    load_current_artifact_semantic_plan,
    plan_binding,
    presentation_windows,
    schema_python_parity,
    selected_line_indices,
    semantic_plan_path,
    write_artifact_semantic_plan,
)
from story_video_synthesizer.align import LineTiming
from story_video_synthesizer.pipeline import SynthesisConfig, synthesize_story
from product_package import artifact_semantic_product_selections
from tests.test_story_contract_runtime import _lock_contract, _new_project, _runtime_valid_contract


def _provenance() -> dict:
    return {"source": "agent_inference", "source_ref": "fixture semantic compiler mapping", "source_order": 0}


def _contract(project: Path, manifest: dict, kinds=("title", "story_body", "moral")) -> dict:
    contract = _runtime_valid_contract(project, manifest)
    source_text = Path(manifest["inputs"]["story_text"]).read_text(encoding="utf-8")
    quote = next(line for line in source_text.splitlines() if line.strip())
    def fix_quotes(value):
        if isinstance(value, dict):
            if value.get("source") == "task_input" and "evidence_quote" in value:
                value["evidence_quote"] = quote
            for child in value.values():
                fix_quotes(child)
        elif isinstance(value, list):
            for child in value:
                fix_quotes(child)
    fix_quotes(contract)
    mappings = []
    for kind in kinds:
        for artifact in ARTIFACTS:
            action, policy = "include", "show" if artifact.endswith("subtitles") else "inherit"
            mapping = {
                "semantic_kind": kind,
                "artifact": artifact,
                "action": action,
                "subtitle_policy": policy,
                "provenance": _provenance(),
            }
            if artifact == "background_visual" and kind in {"title", "moral"}:
                mapping.update(action="visual_substitute", subtitle_policy="inherit", visual_substitute=f"{kind}_card", mutual_exclusion_group=f"{kind}_presentation")
            if artifact == "background_subtitles" and kind in {"title", "moral"}:
                mapping.update(action="exclude", subtitle_policy="hide", mutual_exclusion_group=f"{kind}_presentation")
            if artifact == "sales_subtitles" and kind == "title":
                mapping.update(action="exclude", subtitle_policy="hide")
            mappings.append(mapping)
    contract["contracts"]["semantic_artifacts"]["mappings"] = mappings
    return contract


class ArtifactSemanticPlanTests(unittest.TestCase):
    def _fixture(self, text="通用测试故事\n一天，主角出发。\n这个故事告诉我们，要认真观察。\n"):
        temporary = tempfile.TemporaryDirectory()
        project, manifest = _new_project(Path(temporary.name))
        source = Path(manifest["inputs"]["story_text"])
        source.write_text(text, encoding="utf-8")
        from story_project import project_paths, write_manifest
        write_manifest(project_paths(project), manifest)
        kinds = tuple(dict.fromkeys({"通用测试故事": "title"}.get(line, "moral" if "告诉我们" in line else "story_body") for line in text.splitlines() if line.strip()))
        agent, _ = _lock_contract(project, manifest, contract_payload=_contract(project, manifest, kinds))
        return temporary, project, manifest, source, agent

    def test_schema_python_parity_and_byte_determinism(self) -> None:
        temporary, project, _manifest, source, _agent = self._fixture()
        self.addCleanup(temporary.cleanup)
        self.assertEqual(schema_python_parity(), [])
        first = compile_artifact_semantic_plan(project, source)
        second = compile_artifact_semantic_plan(project, source)
        self.assertEqual(first, second)
        path = write_artifact_semantic_plan(project, source)
        before = path.read_bytes()
        write_artifact_semantic_plan(project, source)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(artifact_semantic_plan_is_current(project, source))
        try:
            import jsonschema
        except ImportError:  # production deliberately has no new dependency
            jsonschema = None
        if jsonschema is not None:
            schema = json.loads(Path("schemas/artifact_semantic_plan/v1/artifact_semantic_plan.schema.json").read_text())
            jsonschema.Draft202012Validator(schema).validate(first)

    def test_stale_or_modified_plan_is_rejected(self) -> None:
        temporary, project, _manifest, source, _agent = self._fixture()
        self.addCleanup(temporary.cleanup)
        path = write_artifact_semantic_plan(project, source)
        payload = json.loads(path.read_text())
        payload["artifacts"]["ppt"]["decisions"][0]["action"] = "exclude"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stale|modified"):
            load_current_artifact_semantic_plan(project, source)
        path.write_text('{"schema_version":', encoding="utf-8")
        self.assertFalse(artifact_semantic_plan_is_current(project, source))
        write_artifact_semantic_plan(project, source)
        source.write_text(source.read_text() + "新增正文。\n", encoding="utf-8")
        self.assertFalse(artifact_semantic_plan_is_current(project, source))

    def test_provenance_and_missing_mapping_are_not_fabricated(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        project, manifest = _new_project(Path(temporary.name))
        source = Path(manifest["inputs"]["story_text"])
        source.write_text("一天，主角出发。\n", encoding="utf-8")
        from story_project import project_paths, write_manifest
        write_manifest(project_paths(project), manifest)
        contract = _contract(project, manifest, ("story_body",))
        contract["contracts"]["semantic_artifacts"]["mappings"][0]["provenance"] = {"source": "task_input", "source_ref": "invented"}
        with self.assertRaises(AssertionError):
            _lock_contract(project, manifest, contract_payload=contract)
        contract = _contract(project, manifest, ("story_body",))
        contract["contracts"]["semantic_artifacts"]["mappings"].pop()
        _agent, _ = _lock_contract(project, manifest, contract_payload=contract)
        with self.assertRaisesRegex(ValueError, "missing required semantic mapping"):
            compile_artifact_semantic_plan(project, source)

    def test_mutual_exclusion_and_first_last_windows(self) -> None:
        temporary, project, _manifest, source, _agent = self._fixture()
        self.addCleanup(temporary.cleanup)
        plan = compile_artifact_semantic_plan(project, source)
        lines = [line for line in source.read_text().splitlines() if line]
        self.assertEqual(selected_line_indices(lines, plan, "background_subtitles"), [1])
        self.assertEqual(selected_line_indices(lines, plan, "demo_subtitles"), [0, 1, 2])
        timings = [
            LineTiming(1, lines[0], 0, 2, 2, 0, 2),
            LineTiming(2, lines[1], 2, 12, 10, 2, 12),
            LineTiming(3, lines[2], 12, 15, 3, 12, 15),
        ]
        windows = presentation_windows(timings, plan)
        self.assertEqual([(row["card_kind"], row["start"], row["end"]) for row in windows], [("title_card", 0.0, 2.0), ("moral_card", 12.0, 15.0)])
        conflict = _contract(project, _manifest)
        for mapping in conflict["contracts"]["semantic_artifacts"]["mappings"]:
            if mapping["semantic_kind"] == "title" and mapping["artifact"] == "background_subtitles":
                mapping["subtitle_policy"] = "show"
        _agent, _ = _lock_contract(project, _manifest, contract_payload=conflict)
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            compile_artifact_semantic_plan(project, source)

    def test_body_only_story_has_no_empty_cards(self) -> None:
        temporary, project, _manifest, source, _agent = self._fixture("一天，主角出发。\n主角安全回家。\n")
        self.addCleanup(temporary.cleanup)
        plan = compile_artifact_semantic_plan(project, source)
        self.assertEqual(plan["visual_cards"], [])
        self.assertFalse(plan["pre_roll_diagnostic"]["suspected"])

    def test_storyboard_plan_requires_semantic_plan_binding(self) -> None:
        temporary, project, manifest, source, agent = self._fixture()
        self.addCleanup(temporary.cleanup)
        plan_path = write_artifact_semantic_plan(project, source)
        semantic = load_current_artifact_semantic_plan(project, source)
        staging = Path(temporary.name) / "staging"
        staging.mkdir()
        storyboard = staging / "storyboard.txt"
        storyboard.write_text("主角出发。\n", encoding="utf-8")
        from story_contract_runtime import contract_consumer_path, write_contract_consumer_context
        expected_path = write_contract_consumer_context(project, "storyboard_images")
        expected = json.loads(expected_path.read_text())
        sample_binding = {
            "visual_sample_schema_version": "1.0",
            "visual_sample_plan_sha256": "1" * 64,
            "visual_sample_review_bundle_sha256": "2" * 64,
            "visual_sample_lock_sha256": "3" * 64,
        }
        shot = {
            "scene": 1, "story_text": "主角出发。", "narrative_function": "setup",
            "shot_size": "wide", "focal_character": "主角", "visible_characters": ["主角"],
            "excluded_characters": [], "continuity_group": "opening", "appearance_ids": [],
            "visual_description": "主角出发",
            "scale_basis": {"applicable": False, "relationship_ids": [], "reason": "合同没有尺度关系"},
            "current_story_state": {}, "visual_state_evidence": {},
            "subject_action": "主角自然出发",
            "environment_motion": "环境轻微自然变化",
            "camera_motion": "稳定跟随",
            "entry_state": {"story_state": "opening"},
            "exit_state": {"story_state": "opening"},
            "screen_direction": "left_to_right",
            "adjacent_handoff": {"from_previous": "", "to_next": "", "allows_direction_change": False, "allows_state_transition": False},
            "expected_motion": {
                "primary": "subject", "subject_level": "moderate",
                "environment_level": "low", "camera_level": "low",
                "rationale": "主角正在出发",
            },
        }
        payload = {
            **{key: expected[key] for key in ("contract_schema_version", "story_contract_sha256", "story_contract_dependency_sha256")},
            "contract_projection": expected["contract_projection"], **plan_binding(plan_path, semantic),
            **sample_binding, "shots": [shot],
        }
        storyboard_plan = staging / "plan.json"
        storyboard_plan.write_text(json.dumps(payload), encoding="utf-8")
        with patch("story_agent.visual_sample_lock_is_current", return_value=True), patch(
            "story_agent.visual_sample_binding", return_value=sample_binding
        ):
            self.assertTrue(agent._storyboard_plan_valid(storyboard_plan, storyboard))
            payload["artifact_semantic_plan_sha256"] = "0" * 64
            storyboard_plan.write_text(json.dumps(payload), encoding="utf-8")
            self.assertFalse(agent._storyboard_plan_valid(storyboard_plan, storyboard))

    def test_runtime_stage_compiles_and_stale_source_blocks_resume(self) -> None:
        temporary, project, manifest, source, agent = self._fixture()
        self.addCleanup(temporary.cleanup)
        result = agent._stage_artifact_semantic_plan(manifest)
        self.assertEqual(result.status, "done")
        self.assertTrue(agent._has_artifact_semantic_plan(manifest))
        source.write_text(source.read_text() + "又一天，主角继续观察。\n", encoding="utf-8")
        self.assertFalse(agent._has_artifact_semantic_plan(manifest))

    def test_assembly_and_product_selectors_consume_current_plan(self) -> None:
        temporary, project, _manifest, source, _agent = self._fixture()
        self.addCleanup(temporary.cleanup)
        semantic_path = write_artifact_semantic_plan(project, source)
        plan = load_current_artifact_semantic_plan(project, source)
        lines = [line for line in source.read_text().splitlines() if line]
        selections = artifact_semantic_product_selections(lines, plan)
        self.assertEqual(selections["ppt"], [0, 1, 2])
        self.assertEqual(selections["customer_manuscript"], [0, 1, 2])
        self.assertEqual(selections["reading_annotation"], [0, 1, 2])
        self.assertEqual(selections["demo_subtitles"], [0, 1, 2])

        video_dir = project / "videos"
        video_dir.mkdir()
        videos = []
        for index in range(1, 4):
            video = video_dir / f"{index:02d}.mp4"
            video.write_bytes(b"fixture")
            videos.append(video)
        narration = project / "narration.wav"
        music = project / "music.mp3"
        narration.write_bytes(b"fixture")
        music.write_bytes(b"fixture")
        output = project / "assembly"
        timings = [
            LineTiming(1, lines[0], 0, 2, 2, 0, 2),
            LineTiming(2, lines[1], 2, 12, 10, 2, 12),
            LineTiming(3, lines[2], 12, 15, 3, 12, 15),
        ]
        subtitle_calls: list[list[str]] = []
        card_calls: list[list[dict]] = []

        def touch_output(*args, **kwargs):
            target = kwargs.get("output_path")
            if target is None and len(args) > 2:
                target = args[2]
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).touch()

        def render_segments(*_args, **_kwargs):
            paths = []
            for index in range(3):
                path = output / "_work" / f"segment-{index}.mp4"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                paths.append(path)
            return paths

        def burn(*_args, **kwargs):
            subtitle_calls.append([cue.text for cue in kwargs["cues"]])
            Path(kwargs["output_path"]).parent.mkdir(parents=True, exist_ok=True)
            Path(kwargs["output_path"]).touch()

        def overlay(*_args, **kwargs):
            card_calls.append(list(_args[1]))
            Path(_args[2]).parent.mkdir(parents=True, exist_ok=True)
            Path(_args[2]).touch()

        config = SynthesisConfig(
            video_dir=video_dir, script_path=source, subtitle_script_path=source,
            narration_path=narration, music_path=music, output_dir=output,
            project_dir=project, artifact_semantic_plan_path=semantic_path,
            keep_workdir=True,
        )
        with ExitStack() as stack:
            stack.enter_context(patch("story_video_synthesizer.pipeline._validate_tools"))
            stack.enter_context(patch("story_video_synthesizer.pipeline.align_script_to_narration", return_value=timings))
            stack.enter_context(patch("story_video_synthesizer.pipeline.probe_duration", return_value=15.0))
            stack.enter_context(patch("story_video_synthesizer.pipeline._render_video_segments", side_effect=render_segments))
            stack.enter_context(patch("story_video_synthesizer.pipeline._concat_videos", side_effect=lambda _segments, target, _list: Path(target).touch()))
            stack.enter_context(patch("story_video_synthesizer.pipeline._overlay_semantic_cards", side_effect=overlay))
            stack.enter_context(patch("story_video_synthesizer.pipeline._burn_subtitles_only", side_effect=burn))
            stack.enter_context(patch("story_video_synthesizer.pipeline._mux_with_music", side_effect=touch_output))
            stack.enter_context(patch("story_video_synthesizer.pipeline._mux_with_voice_music", side_effect=touch_output))
            result = synthesize_story(config)

        self.assertTrue(result.semantic_plan_manifest.is_file())
        receipt = json.loads(result.semantic_plan_manifest.read_text())
        self.assertEqual(
            {key: receipt[key] for key in plan_binding(semantic_path, plan)},
            plan_binding(semantic_path, plan),
        )
        normalize = lambda values: "".join(values).replace("，", "").replace("。", "")
        self.assertEqual(normalize(subtitle_calls[0]), normalize([lines[1]]))  # background: no duplicate title/moral
        self.assertEqual(normalize(subtitle_calls[1]), normalize([lines[1], lines[2]]))  # sales: title hidden, moral shown
        self.assertEqual(normalize(subtitle_calls[2]), normalize(lines))  # demo obeys its own mapping
        self.assertEqual(
            [(row["card_kind"], row["start"], row["end"]) for row in card_calls[0]],
            [("title_card", 0.0, 2.0), ("moral_card", 12.0, 15.0)],
        )


if __name__ == "__main__":
    unittest.main()
