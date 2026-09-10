from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from artifact_semantic_plan import (
    ARTIFACTS,
    artifact_semantic_plan_is_current,
    compile_artifact_semantic_plan,
    complete_missing_semantic_mappings,
    load_current_artifact_semantic_plan,
    plan_binding,
    presentation_windows,
    reconcile_semantic_mappings,
    schema_python_parity,
    selected_line_indices,
    semantic_plan_path,
    write_artifact_semantic_plan,
)
from story_video_synthesizer.align import LineTiming
from story_video_synthesizer.pipeline import SynthesisConfig, synthesize_story
from product_package import artifact_semantic_product_selections
from story_module_adapters import MockStorySemanticsAdapter
from story_module_ports import (
    STORY_SEMANTICS_COMPILER_VERSION,
    ModuleCapabilities,
    ModuleIdentity,
    StorySemanticsPort,
    StorySemanticsRequest,
    StorySemanticsResult,
)
from story_module_registry import MODULE_PROFILE_ENV, MODULE_PROFILE_REQUIRED_ENV, ModuleRegistry
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
            if artifact in {"background_subtitles", "sales_subtitles"} and kind != "story_body":
                mapping.update(action="exclude", subtitle_policy="hide")
                if kind in {"title", "moral"}:
                    mapping["mutual_exclusion_group"] = f"{kind}_presentation"
            mappings.append(mapping)
    contract["contracts"]["semantic_artifacts"]["mappings"] = mappings
    return contract


def _protocol_result(request: StorySemanticsRequest, kinds: tuple[str, ...], adapter: str) -> StorySemanticsResult:
    lines = tuple(
        {"line_number": index, "text": text, "semantic_kind": kind}
        for index, (text, kind) in enumerate(zip(request.normalized_lines, kinds), start=1)
    )
    segments = []
    for index, kind in enumerate(kinds, start=1):
        if segments and segments[-1]["kind"] == kind:
            segments[-1]["end_line"] = index
            segments[-1]["line_numbers"].append(index)
        else:
            segments.append(
                {"kind": kind, "start_line": index, "end_line": index, "line_numbers": [index]}
            )
    return StorySemanticsResult(
        True,
        request.source_path,
        request.source_sha256,
        next((row["text"] for row in lines if row["semantic_kind"] == "title"), ""),
        lines,
        tuple(segments),
        {},
        adapter,
        STORY_SEMANTICS_COMPILER_VERSION,
    )


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











    def test_runtime_removes_only_stale_runtime_default_kinds(self) -> None:
        from story_contract_runtime import contract_paths
        from story_project import save_json

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        project, manifest = _new_project(Path(temporary.name))
        source = Path(manifest["inputs"]["story_text"])
        source.write_text("主角走进森林。\n不要只注重外表。\n", encoding="utf-8")
        contract = _contract(project, manifest, ("story_body",))
        paths = contract_paths(project)
        save_json(paths["contract"], contract)
        with_moral = MockStorySemanticsAdapter(kinds=("story_body", "moral"))
        first = reconcile_semantic_mappings(paths["contract"], source, with_moral)
        self.assertEqual(len(first["added_mappings"]), 7)

        without_moral = MockStorySemanticsAdapter(kinds=("story_body", "story_body"))
        second = reconcile_semantic_mappings(paths["contract"], source, without_moral)
        self.assertEqual(len(second["removed_mappings"]), 7)
        updated = json.loads(paths["contract"].read_text(encoding="utf-8"))
        self.assertEqual(
            {item["semantic_kind"] for item in updated["contracts"]["semantic_artifacts"]["mappings"]},
            {"story_body"},
        )

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

    def test_compiler_consumes_minimal_protocol_fake_without_product_policy_changes(self) -> None:
        temporary, project, _manifest, source, _agent = self._fixture(
            "通用测试故事\n主角说：“我是森林里的姐姐。”\n这个故事告诉我们，要认真观察。\n"
        )
        self.addCleanup(temporary.cleanup)

        class ProtocolFake:
            identity = ModuleIdentity("story_semantics", "fake/v1", "protocol-fake", "protocol-fake/v1")
            capabilities = ModuleCapabilities(provider="fake", deterministic=True)

            def __init__(self) -> None:
                self.calls = 0

            def analyze(self, request: StorySemanticsRequest) -> StorySemanticsResult:
                self.calls += 1
                return _protocol_result(request, ("title", "story_body", "moral"), "protocol-fake/v1")

        fake = ProtocolFake()
        self.assertIsInstance(fake, StorySemanticsPort)
        baseline = compile_artifact_semantic_plan(project, source)
        substituted = compile_artifact_semantic_plan(project, source, fake)
        self.assertEqual(substituted, baseline)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(
            substituted["artifacts"]["background_subtitles"]["decisions"][1]["source_line_numbers"],
            [2],
        )

    def test_missing_required_mock_selection_fails_closed_without_writing_plan(self) -> None:
        temporary, project, _manifest, source, _agent = self._fixture()
        self.addCleanup(temporary.cleanup)
        environment = os.environ.copy()
        environment.pop(MODULE_PROFILE_ENV, None)
        environment[MODULE_PROFILE_REQUIRED_ENV] = "mock-semantics"
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "refusing production fallback"):
                write_artifact_semantic_plan(project, source)
        self.assertFalse(semantic_plan_path(project).exists())

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
        from story_contracts import StoryContractValidationError
        with self.assertRaises(StoryContractValidationError):
            _lock_contract(project, manifest, contract_payload=contract)
        contract = _contract(project, manifest, ("story_body",))
        _agent, _ = _lock_contract(project, manifest, contract_payload=contract)
        # Normal production now reconciles mappings before review.  A changed
        # classifier result presented directly to the compiler must still fail
        # closed instead of inventing an unreviewed policy downstream.
        unexpected_kind = MockStorySemanticsAdapter(kinds=("moral",))
        with self.assertRaisesRegex(ValueError, "missing required semantic mapping"):
            compile_artifact_semantic_plan(project, source, unexpected_kind)

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

    def test_host_opening_synthesizes_contract_title_and_declarative_moral_cards(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        project, manifest = _new_project(Path(temporary.name))
        source = Path(manifest["inputs"]["story_text"])
        source.write_text(
            "大家好，我是绵羊姐姐。\n"
            "今天给大家讲《爱比美的公鸡》。\n"
            "公鸡走进了森林。\n"
            "小朋友们，光长得好看是不够的，能帮助大家才是真正的美。\n",
            encoding="utf-8",
        )
        from story_project import project_paths, write_manifest
        write_manifest(project_paths(project), manifest)
        contract = _contract(
            project,
            manifest,
            ("host_intro", "story_announcement", "story_body", "moral"),
        )
        contract["story"]["title"] = "爱比美的公鸡"
        _agent, _paths = _lock_contract(project, manifest, contract_payload=contract)
        plan = compile_artifact_semantic_plan(project, source)
        cards = {item["card_kind"]: item for item in plan["visual_cards"]}
        self.assertEqual(cards["title_card"]["text"], "爱比美的公鸡")
        self.assertEqual(cards["title_card"]["source_line_numbers"], [1, 2])
        self.assertEqual(
            cards["moral_card"]["text"],
            "小朋友们，光长得好看是不够的，能帮助大家才是真正的美。",
        )
        self.assertFalse(plan["pre_roll_diagnostic"]["suspected"])

    def test_runtime_completes_only_missing_mapping_pairs_before_review(self) -> None:
        from story_contract_runtime import contract_paths
        from story_project import save_json

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        project, manifest = _new_project(Path(temporary.name))
        source = Path(manifest["inputs"]["story_text"])
        source.write_text(
            "大家好，我是绵羊姐姐。\n"
            "今天给大家讲《通用测试故事》。\n"
            "主角走进森林。\n"
            "小朋友们，要认真观察。\n",
            encoding="utf-8",
        )
        from story_project import project_paths, write_manifest
        write_manifest(project_paths(project), manifest)
        contract = _contract(project, manifest, ("story_body",))
        paths = contract_paths(project)
        save_json(paths["contract"], contract)
        fake = MockStorySemanticsAdapter(
            kinds=("host_intro", "story_announcement", "story_body", "moral")
        )

        added = complete_missing_semantic_mappings(paths["contract"], source, fake)
        self.assertEqual(len(added), 21)
        updated = json.loads(paths["contract"].read_text(encoding="utf-8"))
        mappings = updated["contracts"]["semantic_artifacts"]["mappings"]
        self.assertEqual(len(mappings), 28)
        body = [item for item in mappings if item["semantic_kind"] == "story_body"]
        self.assertTrue(all(item["provenance"] == _provenance() for item in body))
        moral_visual = next(
            item
            for item in mappings
            if item["semantic_kind"] == "moral" and item["artifact"] == "background_visual"
        )
        self.assertEqual(moral_visual["action"], "visual_substitute")
        self.assertEqual(moral_visual["mutual_exclusion_group"], "moral_presentation")

        _agent, _paths = _lock_contract(project, manifest, contract_payload=updated)
        plan = compile_artifact_semantic_plan(project, source, fake)
        self.assertEqual(
            {item["card_kind"] for item in plan["visual_cards"]},
            {"title_card", "moral_card"},
        )

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
        self.assertEqual(normalize(subtitle_calls[1]), normalize([lines[1]]))  # customer background: body only
        self.assertEqual(normalize(subtitle_calls[2]), normalize(lines))  # demo obeys its own mapping
        self.assertEqual(
            [(row["card_kind"], row["start"], row["end"]) for row in card_calls[0]],
            [("title_card", 0.0, 2.0), ("moral_card", 12.0, 15.0)],
        )






if __name__ == "__main__":
    unittest.main()
