from __future__ import annotations

import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from story_agent import AgentContext, StageResult, StoryAgent, WorkerAttempt, classify_command_failure
from story_agent_runtime import (
    AgentRuntimeError,
    BudgetExceeded,
    BudgetLedger,
    JobRegistry,
    existing_artifact_hashes,
    ensure_manifest_v2,
    file_sha256,
    render_job_report,
    request_cancel,
    resume_job,
    review_passes,
    manifest_context_sha256,
    mark_stage,
    job_lock,
    prepared_input_contract_errors,
    refresh_prepared_inputs,
    segment_storyboard_text,
    submit_video_job,
    assert_runnable,
    update_control,
    load_control,
)
from story_project import load_manifest, project_paths, save_json, write_manifest
from story_project import detect_project_assets, final_delivery, init_project, refresh_project_outputs, write_internal_agent_reports


def as_frozen_v3_legacy(manifest: dict) -> dict:
    """Turn a fixture into an evidence-bearing pre-V3.5 project.

    Tests for behavior that predates the production-contract gate must opt in
    explicitly; merely changing ``policy`` is intentionally insufficient.
    """

    manifest["created_at"] = "2026-08-11 12:00:00"
    stages = manifest.setdefault("agent", {}).setdefault("stages", {})
    stages["setup_project"] = {"status": "passed"}
    return ensure_manifest_v2(manifest)


class StoryAgentRuntimeTests(unittest.TestCase):
    def test_two_role_model_routing_uses_commander_for_judgment_and_worker_for_execution(self) -> None:
        context = AgentContext(
            project_dir=Path("/private/tmp/two-role-routing"),
            inbox=None,
            story_name="双档路由",
            slug="two-role-routing",
            execute=False,
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
        self.assertEqual(context.codex_route("video_review"), ("commander", "gpt-5.6-sol", "xhigh"))
        self.assertEqual(context.codex_route("codex_story_images"), ("worker", "gpt-5.6-luna", "max"))
        self.assertEqual(context.codex_route("generate_videos"), ("worker", "gpt-5.6-luna", "max"))

    def test_prepared_entry_binds_clean_video_and_confirmed_text_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = Path(__file__).resolve().parents[1] / "tools" / "video-subtitle-remover" / "test" / "test2.mp4"
            video = root / "prepared.mp4"
            shutil.copy2(fixture, video)
            confirmed = root / "confirmed.txt"
            confirmed.write_text("小老虎认真听大家唱歌。\n最后，它学会了公平。\n", encoding="utf-8")
            _job, project, created = submit_video_job(
                video,
                input_mode="prepared",
                confirmed_text=confirmed,
                projects_root=root / "projects",
                story_name="加速入口",
                slug="prepared-entry",
                registry=JobRegistry(root / "registry.json"),
            )
            self.assertTrue(created)
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            self.assertEqual(manifest["agent"]["input_contract"]["mode"], "prepared_greenscreen_confirmed_text")
            self.assertEqual(manifest["agent"]["input_contract"]["version"], 2)
            self.assertFalse(manifest["agent"]["input_contract"]["counts_toward_default_entry"])
            self.assertEqual(manifest["inputs"]["greenscreen_video"], manifest["inputs"]["greenscreen_video_original"])
            storyboard_text = Path(manifest["inputs"]["storyboard_text"])
            self.assertTrue(storyboard_text.is_file())
            derived_storyboard = manifest["agent"]["input_contract"]["derived_inputs"]["storyboard_text"]
            self.assertEqual(derived_storyboard["path"], str(storyboard_text))
            self.assertEqual(derived_storyboard["sha256"], file_sha256(storyboard_text))
            self.assertEqual(prepared_input_contract_errors(project, manifest), [])
            detected = detect_project_assets(project, extract_audio=False)
            self.assertEqual(detected["inputs"]["story_text"], manifest["inputs"]["story_text"])
            self.assertEqual(detected["inputs"]["greenscreen_video"], manifest["inputs"]["greenscreen_video"])
            self.assertEqual(prepared_input_contract_errors(project, detected), [])
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="加速入口",
                slug="prepared-entry",
                execute=True,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            self.assertTrue(StoryAgent(context)._has_source_edit(manifest))
            Path(manifest["inputs"]["story_text"]).write_text("被篡改", encoding="utf-8")
            self.assertFalse(StoryAgent(context)._has_source_edit(manifest))

    def test_storyboard_text_splits_long_prepared_story_without_changing_characters(self) -> None:
        story = (
            "小壁虎有一条尾巴。一天，小壁虎在墙上爬，突然一条蛇咬住了它的尾巴。"
            "小壁虎用力一挣，尾巴断了。它来到小河边，对鱼姐姐说：“鱼姐姐，请把尾巴借给我吧。”"
            "鱼姐姐说：“不行，我要用尾巴拨水呢。”小壁虎又来到大树下，对牛伯伯说：“牛伯伯，请把尾巴借给我吧。”"
            "牛伯伯说：“不行，我要用尾巴赶蝇子呢。”小壁虎再来到屋檐下，对燕子说：“燕子姐姐，请把尾巴借给我吧。”"
            "燕子说：“不行，我要用尾巴掌握方向呢。”小壁虎只好继续往前爬。"
        )
        lines = segment_storyboard_text(story)
        compact = lambda value: "".join(value.split())
        self.assertEqual(compact("".join(lines)), compact(story))
        self.assertGreater(len(lines), 6)
        self.assertLessEqual(max(map(len, lines)), 60)
        self.assertTrue(any("小壁虎有一条尾巴。" in line for line in lines))
        self.assertTrue(any("突然一条蛇咬住了它的尾巴。" in line for line in lines))
        self.assertTrue(any("尾巴断了。" in line for line in lines))
        self.assertFalse(
            any("蛇咬住了它的尾巴" in line and "尾巴断了" in line for line in lines),
            "完整尾巴/被咬与断尾转折必须落在不同镜头",
        )
        self.assertFalse(
            any("尾巴断了" in line and "没有尾巴多难看" in line for line in lines),
            "断尾瞬间与断尾后的情绪反应必须落在不同镜头",
        )
        self.assertTrue(any("鱼姐姐说" in line for line in lines))
        self.assertTrue(any("牛伯伯说" in line for line in lines))
        self.assertTrue(any("燕子说" in line for line in lines))

    def test_story_agent_story_lines_falls_back_for_legacy_manifest_without_storyboard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            story_path = root / "legacy_story.txt"
            story_path.write_text("旧项目第一行。\n旧项目第二行。\n", encoding="utf-8")
            context = AgentContext(
                project_dir=root / "故事剪辑：旧项目",
                inbox=None,
                story_name="旧项目",
                slug="legacy-project",
                execute=False,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context, read_only=True)
            self.assertEqual(
                agent._story_lines({"inputs": {"story_text": str(story_path)}}),
                ["旧项目第一行。", "旧项目第二行。"],
            )

    def test_visual_continuity_contract_requires_valid_storyboard_state_enum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：连续性合同"
            init_project(project, story_name="连续性合同", slug="continuity-contract")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="连续性合同",
                slug="continuity-contract",
                execute=False,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context, read_only=True)
            contract = project_paths(project).status / "visual_continuity_contract.json"
            plan = project_paths(project).images / "continuity-contract_storyboard_plan.json"
            contract.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "allowed_states": ["intact", "missing"],
                        "storyboard_requirements": {"required_field": "character_state"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            plan.write_text(
                json.dumps(
                    [
                        {"scene": 1, "character_state": "intact"},
                        {"scene": 2, "character_state": "missing"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.assertEqual(agent._visual_continuity_storyboard_errors(contract, plan), [])
            plan.write_text(
                json.dumps([{"scene": 1, "character_state": "unknown"}], ensure_ascii=False),
                encoding="utf-8",
            )
            errors = agent._visual_continuity_storyboard_errors(contract, plan)
            self.assertTrue(any("不属于允许枚举" in error for error in errors))

    def test_assemble_final_passes_confirmed_subtitles_as_independent_subtitle_script(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：字幕接入"
            # This test exercises the pre-V3.5 confirmed-subtitle compatibility
            # path.  New required_v1 projects are intentionally covered by the
            # artifact-semantic-plan tests and may not assemble without a
            # current locked plan.
            manifest = as_frozen_v3_legacy(
                init_project(project, story_name="字幕接入", slug="subtitle-assembly")
            )
            fixture = Path(__file__).resolve().parents[1] / "tools" / "video-subtitle-remover" / "test" / "test2.mp4"
            subtitles = project_paths(project).inputs / "subtitle-assembly_confirmed_subtitles.txt"
            subtitles.write_text("逐行字幕一。\n逐行字幕二。\n", encoding="utf-8")
            manifest["inputs"]["narration"] = str(fixture)
            manifest["inputs"]["confirmed_subtitles"] = str(subtitles)
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="字幕接入",
                slug="subtitle-assembly",
                execute=False,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context, read_only=True)
            with patch.object(agent, "_workflow", return_value=StageResult("done", "ok")) as workflow:
                result = agent._stage_assemble_final(manifest)
            self.assertEqual(result.status, "done")
            command = workflow.call_args.args[0]
            self.assertEqual(command[0], "assemble")
            subtitle_index = command.index("--subtitle-script")
            self.assertEqual(Path(command[subtitle_index + 1]), subtitles)

    def test_prepared_entry_accepts_reviewed_docx_and_derives_plain_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = Path(__file__).resolve().parents[1] / "tools" / "video-subtitle-remover" / "test" / "test2.mp4"
            video = root / "prepared.mp4"
            shutil.copy2(fixture, video)
            confirmed = root / "confirmed.docx"
            document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>小兔子找太阳</w:t></w:r></w:p>
    <w:p><w:r><w:t>它终于找到了温暖的太阳。</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
            with zipfile.ZipFile(confirmed, "w") as archive:
                archive.writestr("word/document.xml", document_xml)
            _job, project, created = submit_video_job(
                video,
                input_mode="prepared",
                confirmed_text=confirmed,
                projects_root=root / "projects",
                story_name="DOCX 加速入口",
                slug="prepared-docx",
                registry=JobRegistry(root / "registry.json"),
            )
            self.assertTrue(created)
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            confirmed_record = next(
                item
                for item in manifest["agent"]["input_contract"]["user_inputs"]
                if item["role"] == "confirmed_story_text"
            )
            self.assertEqual(Path(confirmed_record["path"]).suffix, ".docx")
            derived = Path(manifest["inputs"]["story_text"])
            self.assertEqual(derived.read_text(encoding="utf-8"), "它终于找到了温暖的太阳。\n")
            consumer = Path(manifest["outputs"]["consumer_manuscript"])
            self.assertEqual(consumer.read_text(encoding="utf-8"), "它终于找到了温暖的太阳。\n")
            full = Path(manifest["inputs"]["story_transcript_full"])
            self.assertEqual(full.read_text(encoding="utf-8"), "小兔子找太阳\n它终于找到了温暖的太阳。\n")
            self.assertEqual(prepared_input_contract_errors(project, manifest), [])

    def test_prepared_entry_accepts_optional_confirmed_subtitles_as_audited_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = Path(__file__).resolve().parents[1] / "tools" / "video-subtitle-remover" / "test" / "test2.mp4"
            video = root / "prepared.mp4"
            shutil.copy2(fixture, video)
            confirmed = root / "confirmed.txt"
            confirmed.write_text(
                "小壁虎借尾巴\n大家好，我是绵羊姐姐。\n小壁虎爬呀爬，爬到小河边。\n",
                encoding="utf-8",
            )
            subtitles = root / "confirmed-subtitles.txt"
            subtitle_text = "大家好，我是绵羊姐姐。\n小壁虎 爬呀爬，爬到小河边。\n"
            subtitles.write_text(subtitle_text, encoding="utf-8")
            _job, project, created = submit_video_job(
                video,
                input_mode="prepared",
                confirmed_text=confirmed,
                confirmed_subtitles=subtitles,
                projects_root=root / "projects",
                story_name="字幕审计",
                slug="subtitle-audit",
                registry=JobRegistry(root / "registry.json"),
            )
            self.assertTrue(created)
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            subtitle_copy = Path(manifest["inputs"]["confirmed_subtitles"])
            self.assertTrue(subtitle_copy.is_file())
            self.assertEqual(subtitle_copy.read_text(encoding="utf-8"), subtitle_text)
            self.assertEqual(subtitle_copy.parent.resolve(), project_paths(project).inputs.resolve())
            source_record = manifest["agent"]["source"]["confirmed_subtitles"]
            self.assertEqual(source_record["original_path"], str(subtitles.resolve()))
            self.assertEqual(source_record["project_copy"], str(subtitle_copy))
            self.assertEqual(source_record["sha256"], file_sha256(subtitle_copy))
            self.assertEqual(source_record["bytes"], subtitle_copy.stat().st_size)
            contract = manifest["agent"]["input_contract"]
            subtitle_record = next(item for item in contract["user_inputs"] if item["role"] == "confirmed_subtitles")
            self.assertEqual(subtitle_record["original_path"], str(subtitles.resolve()))
            self.assertEqual(subtitle_record["sha256"], file_sha256(subtitle_copy))
            self.assertEqual(subtitle_record["bytes"], subtitle_copy.stat().st_size)
            derived_record = contract["derived_inputs"]["confirmed_subtitles"]
            self.assertEqual(derived_record["path"], str(subtitle_copy))
            self.assertEqual(derived_record["source_sha256"], file_sha256(subtitle_copy))
            self.assertEqual(derived_record["producer"], "user_confirmed_subtitles")
            self.assertEqual(contract["integration_points"]["confirmed_subtitles"]["manifest_key"], "inputs.confirmed_subtitles")
            self.assertEqual(prepared_input_contract_errors(project, manifest), [])
            # The optional subtitle stream is an independent future timing/demo
            # input; it must not replace semantic story derivatives.
            self.assertNotEqual(Path(manifest["inputs"]["story_text"]).read_text(encoding="utf-8"), subtitle_text)
            self.assertNotIn("大家好，我是绵羊姐姐。", Path(manifest["inputs"]["sales_subtitle_text"]).read_text(encoding="utf-8"))

    def test_confirmed_subtitles_are_prepared_only_and_must_be_utf8_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = Path(__file__).resolve().parents[1] / "tools" / "video-subtitle-remover" / "test" / "test2.mp4"
            video = root / "prepared.mp4"
            shutil.copy2(fixture, video)
            confirmed = root / "confirmed.txt"
            confirmed.write_text("小故事\n故事正文。\n", encoding="utf-8")
            subtitles = root / "subtitles.srt"
            subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\n故事正文。\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "prepared 加速入口字幕只接受 UTF-8 .txt/.md"):
                submit_video_job(
                    video,
                    input_mode="prepared",
                    confirmed_text=confirmed,
                    confirmed_subtitles=subtitles,
                    projects_root=root / "projects",
                    registry=JobRegistry(root / "registry.json"),
                )
            with self.assertRaisesRegex(ValueError, "single-greenscreen 模式不能提供 --confirmed-subtitles"):
                submit_video_job(
                    video,
                    confirmed_subtitles=root / "subtitles.txt",
                    projects_root=root / "projects",
                    registry=JobRegistry(root / "registry-single.json"),
                )

    def test_prepared_semantic_contract_separates_customer_and_full_transcript_views(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = Path(__file__).resolve().parents[1] / "tools" / "video-subtitle-remover" / "test" / "test2.mp4"
            video = root / "prepared.mp4"
            shutil.copy2(fixture, video)
            confirmed = root / "confirmed.txt"
            confirmed.write_text(
                "大家好\n我是故事老师\n今天给大家讲的故事是《小兔子找太阳》。\n"
                "小兔子出门找太阳。\n我们要学会仔细观察。\n故事讲完了，再见。\n",
                encoding="utf-8",
            )
            _job, project, _created = submit_video_job(
                video,
                input_mode="prepared",
                confirmed_text=confirmed,
                projects_root=root / "projects",
                story_name="语义合同",
                slug="semantic-contract",
                registry=JobRegistry(root / "registry.json"),
            )
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            self.assertEqual(
                Path(manifest["outputs"]["consumer_manuscript"]).read_text(encoding="utf-8"),
                "小兔子出门找太阳。\n我们要学会仔细观察。\n",
            )
            self.assertIn("我是故事老师", Path(manifest["inputs"]["story_transcript_full"]).read_text(encoding="utf-8"))
            self.assertEqual(Path(manifest["inputs"]["sales_subtitle_text"]).read_text(encoding="utf-8"), "小兔子出门找太阳。\n")
            self.assertEqual(prepared_input_contract_errors(project, manifest), [])
            Path(manifest["inputs"]["story_semantics"]).write_text("{}", encoding="utf-8")
            self.assertTrue(prepared_input_contract_errors(project, manifest))

    def test_dag_batch_respects_resource_and_write_set_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：DAG"
            init_project(project, story_name="DAG", slug="dag")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="DAG",
                slug="dag",
                execute=True,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
                scheduler="dag",
                max_parallel=3,
            )
            selected = StoryAgent(context)._select_parallel_batch(
                ["codex_story_images", "music_request", "release_assets"],
                max_count=3,
            )
            self.assertEqual(selected, ["codex_story_images", "music_request"])

    def test_run_stage_worker_writes_envelope_without_touching_canonical_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：worker"
            manifest = ensure_manifest_v2(init_project(project, story_name="worker", slug="worker"))
            paths = project_paths(project)
            write_manifest(paths, manifest)
            canonical_before = file_sha256(paths.manifest)
            work = Path(directory) / "attempt"
            shadow = work / "shadow_manifest.json"
            result_file = work / "result.json"
            work.mkdir()
            shadow.write_text(paths.manifest.read_text(encoding="utf-8"), encoding="utf-8")
            epoch = int(update_control(project, cancel_requested=False, increment_epoch=True)["run_epoch"])
            env = os.environ.copy()
            env.update(
                {
                    "STORY_AGENT_MANIFEST_OVERRIDE": str(shadow),
                    "STORY_AGENT_PROJECT_ROOT": str(project.resolve()),
                    "STORY_AGENT_WORKER_DIR": str(work),
                    "STORY_AGENT_RUN_EPOCH": str(epoch),
                }
            )
            process = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "story_agent.py"),
                    "run-stage",
                    "--project-dir",
                    str(project),
                    "--story-name",
                    "worker",
                    "--slug",
                    "worker",
                    "--stage",
                    "import_inbox",
                    "--result-file",
                    str(result_file),
                    "--run-epoch",
                    str(epoch),
                    "--attempt-id",
                    "attempt-1",
                ],
                cwd=str(Path(__file__).resolve().parents[1]),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertEqual(json.loads(result_file.read_text(encoding="utf-8"))["status"], "done")
            self.assertEqual(file_sha256(paths.manifest), canonical_before)

    def test_worker_shadow_three_way_merge_preserves_disjoint_updates_and_rejects_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：merge"
            base = ensure_manifest_v2(init_project(project, story_name="merge", slug="merge"))
            paths = project_paths(project)
            work = Path(directory) / "attempt"
            work.mkdir()
            base_path = work / "base.json"
            shadow_path = work / "shadow.json"
            result_path = work / "result.json"
            log_path = work / "worker.log"
            save_json(base_path, base)
            shadow = json.loads(json.dumps(base))
            shadow["outputs"]["worker_value"] = "A"
            save_json(shadow_path, shadow)
            current = json.loads(json.dumps(base))
            current["inputs"]["coordinator_value"] = "B"
            write_manifest(paths, current)
            attempt = WorkerAttempt(
                "music_request",
                "attempt-1",
                1,
                work,
                base_path,
                shadow_path,
                result_path,
                log_path,
                SimpleNamespace(),
            )
            agent = StoryAgent(
                AgentContext(
                    project_dir=project,
                    inbox=None,
                    story_name="merge",
                    slug="merge",
                    execute=True,
                    update_latest_episode=False,
                    codex_mode="handoff",
                    codex_model="",
                    codex_sandbox="workspace-write",
                    codex_approval="never",
                    codex_path="codex",
                    codex_timeout=30,
                )
            )
            self.assertEqual(agent._merge_worker_shadow(attempt), "")
            merged = load_manifest(paths)
            assert merged is not None
            self.assertEqual(merged["outputs"]["worker_value"], "A")
            self.assertEqual(merged["inputs"]["coordinator_value"], "B")

            save_json(base_path, merged)
            shadow = json.loads(json.dumps(merged))
            shadow["story"]["name"] = "worker-name"
            save_json(shadow_path, shadow)
            current = json.loads(json.dumps(merged))
            current["story"]["name"] = "coordinator-name"
            write_manifest(paths, current)
            self.assertIn("story.name", agent._merge_worker_shadow(attempt))
            self.assertEqual(load_manifest(paths)["story"]["name"], "coordinator-name")

    def test_control_epoch_updates_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：control"
            init_project(project, story_name="control", slug="control")
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(lambda _index: update_control(project, cancel_requested=False, increment_epoch=True), range(12)))
            self.assertEqual(load_control(project)["run_epoch"], 12)

    def test_external_blockers_are_kept_separate_in_internal_exception_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：外部阻塞"
            manifest = ensure_manifest_v2(init_project(project, story_name="外部阻塞", slug="external-blocker"))
            paths = project_paths(project)
            manifest["agent"]["external_blockers"].append(
                {
                    "kind": "browser_policy",
                    "status": "blocked",
                    "message": "浏览器策略拒绝",
                    "recovery_action": "策略允许后重试",
                }
            )
            write_manifest(paths, manifest)
            write_internal_agent_reports(project)
            report = (paths.status / "异常说明.md").read_text(encoding="utf-8")
            self.assertIn("## 外部阻塞", report)
            self.assertIn("browser_policy", report)
            self.assertIn("策略允许后重试", report)

    def test_source_review_quality_attempts_ignore_infrastructure_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：审核次数"
            manifest = ensure_manifest_v2(init_project(project, story_name="审核次数", slug="review-attempts"))
            paths = project_paths(project)
            manifest["agent"]["stages"]["source_edit_review"]["attempts"] = 7
            write_manifest(paths, manifest)
            current = paths.status / "source_edit" / "source_edit_review.json"
            current.parent.mkdir(parents=True, exist_ok=True)
            current.write_text(json.dumps({"artifact_sha256": "b" * 64}), encoding="utf-8")
            archived = paths.status / "rejected" / "source_edit" / "first" / "source_edit_review.json"
            archived.parent.mkdir(parents=True, exist_ok=True)
            archived.write_text(json.dumps({"artifact_sha256": "a" * 64}), encoding="utf-8")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="审核次数",
                slug="review-attempts",
                execute=True,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context)
            quality_attempts = agent._source_edit_quality_attempts(current)
            self.assertEqual(quality_attempts, 2)
            self.assertTrue(agent._can_retry_stage("source_edit_review", critical=True, attempts_override=quality_attempts))
            self.assertTrue(agent._can_retry_stage("source_edit_review", critical=True, attempts_override=3))
            self.assertFalse(agent._can_retry_stage("source_edit_review", critical=True, attempts_override=4))

    def test_source_edit_revision_delta_preserves_prior_segment_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：累积覆盖"
            init_project(project, story_name="累积覆盖", slug="cumulative-overrides")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="累积覆盖",
                slug="cumulative-overrides",
                execute=True,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            source_dir = project_paths(project).status / "source_edit"
            source_dir.mkdir(parents=True, exist_ok=True)
            cumulative = source_dir / "source_edit_keep_overrides.json"
            delta = source_dir / "source_edit_keep_overrides_delta.json"
            cumulative.write_text(
                json.dumps({"overrides": [{"segment": 18, "keep": False}, {"segment": 7, "keep": True}]}),
                encoding="utf-8",
            )
            delta.write_text(
                json.dumps({"overrides": [{"segment": 7, "keep": True, "trim_start": 71.1}, {"segment": 26, "keep": False}]}),
                encoding="utf-8",
            )
            merged = StoryAgent(context)._merge_source_edit_overrides(cumulative, delta)
            by_segment = {item["segment"]: item for item in merged["overrides"]}
            self.assertFalse(by_segment[18]["keep"])
            self.assertFalse(by_segment[26]["keep"])
            self.assertEqual(by_segment[7]["trim_start"], 71.1)

    def test_video_review_quarantine_forces_new_provider_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：视频重试身份"
            init_project(project, story_name="视频重试身份", slug="video-retry-id")
            paths = project_paths(project)
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="视频重试身份",
                slug="video-retry-id",
                execute=True,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            jobs = paths.video_jobs / "video-retry-id_image_video_jobs.csv"
            jobs.parent.mkdir(parents=True, exist_ok=True)
            jobs.write_text(
                "scene,target_video_filename,status,task_id,video_url,error,api_response,query_response,prompt\n"
                "12,12_vri.mp4,downloaded,old-task,https://old.example/video.mp4,,{},{}\u002c兔子离开\n",
                encoding="utf-8-sig",
            )
            videos = paths.video_jobs / "videos"
            videos.mkdir(parents=True)
            (videos / "12_vri.mp4").write_bytes(b"rejected-video")

            moved = StoryAgent(context)._quarantine_story_videos([12], jobs, {"12": "保持森林舞台"})

            self.assertEqual(moved, [12])
            with jobs.open(encoding="utf-8-sig", newline="") as file:
                row = next(csv.DictReader(file))
            self.assertEqual(row["status"], "todo")
            self.assertEqual(row["task_id"], "")
            self.assertEqual(row["provider_attempt"], "1")
            self.assertIn("保持森林舞台", row["prompt"])
            rejected_jobs = list((paths.status / "rejected" / "story_videos").rglob("scene_12_rejected_job.json"))
            self.assertEqual(len(rejected_jobs), 1)
            self.assertEqual(json.loads(rejected_jobs[0].read_text(encoding="utf-8"))["task_id"], "old-task")

    def test_storyboard_change_archives_stale_scene_images_but_keeps_style_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：分镜缓存"
            init_project(project, story_name="分镜缓存", slug="story-cache")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="分镜缓存",
                slug="story-cache",
                execute=True,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context)
            staging = Path(directory) / "staging" / "images"
            staging.mkdir(parents=True)
            storyboard = staging.parent / "story-cache_storyboard_lines.txt"
            storyboard.write_text("旧分镜\n", encoding="utf-8")
            style_anchor = staging / "story-cache_style_anchor.png"
            stale_staging = staging / "story-cache_scene_01.png"
            stale_prompt = staging / "story-cache_flow_video_prompts.csv"
            stale_bible = staging.parent / "story-cache_visual_bible.md"
            style_anchor.write_bytes(b"anchor")
            stale_staging.write_bytes(b"old-staging")
            stale_prompt.write_text("old", encoding="utf-8")
            stale_bible.write_text("old bible", encoding="utf-8")
            final_images = project_paths(project).images / "images"
            final_images.mkdir(parents=True, exist_ok=True)
            stale_final = final_images / "story-cache_scene_01.png"
            stale_final.write_bytes(b"old-final")

            self.assertTrue(agent._ensure_storyboard_from_lines(storyboard, ["新分镜一", "新分镜二"]))
            quarantine = agent._invalidate_story_image_derivatives(staging)

            self.assertEqual(storyboard.read_text(encoding="utf-8"), "新分镜一\n新分镜二\n")
            self.assertTrue(style_anchor.exists())
            self.assertFalse(stale_staging.exists())
            self.assertFalse(stale_final.exists())
            self.assertFalse(stale_prompt.exists())
            self.assertFalse(stale_bible.exists())
            self.assertGreaterEqual(len(list(quarantine.iterdir())), 4)

    def test_partial_story_image_batch_is_retrying_not_passed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：图片分批"
            manifest = as_frozen_v3_legacy(init_project(project, story_name="图片分批", slug="image-batch"))
            paths = project_paths(project)
            story = paths.inputs / "image-batch_source.txt"
            story.write_text("第一镜。\n第二镜。\n", encoding="utf-8")
            manifest["inputs"]["story_text"] = str(story)
            write_manifest(paths, manifest)
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="图片分批",
                slug="image-batch",
                execute=True,
                update_latest_episode=False,
                codex_mode="cli",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
                codex_story_image_batch_size=2,
            )
            agent = StoryAgent(context)
            staging_root = agent._codex_stage_dir("codex_story_images")
            shutil.rmtree(staging_root, ignore_errors=True)
            staging_root.mkdir(parents=True, exist_ok=True)
            self.addCleanup(shutil.rmtree, staging_root, True)

            def generate_one(**_kwargs):
                staging = staging_root / "images"
                staging.mkdir(parents=True, exist_ok=True)
                (staging / "image-batch_scene_01.png").write_bytes(b"one")
                return StageResult("done", "one image")

            with patch.object(agent, "_codex_task", side_effect=generate_one), patch.object(
                agent, "_has_story_visual_control", return_value=True
            ):
                result = agent._stage_codex_story_images(manifest)
            self.assertEqual(result.status, "retrying")
            self.assertIn("1/2", result.message)

    def test_story_image_batch_rejects_producer_storyboard_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：分镜只读"
            manifest = as_frozen_v3_legacy(init_project(project, story_name="分镜只读", slug="readonly-board"))
            paths = project_paths(project)
            story = paths.inputs / "readonly-board_source.txt"
            story.write_text("第一镜。\n第二镜。\n", encoding="utf-8")
            manifest["inputs"]["story_text"] = str(story)
            write_manifest(paths, manifest)
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="分镜只读",
                slug="readonly-board",
                execute=True,
                update_latest_episode=False,
                codex_mode="cli",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
                codex_story_image_batch_size=2,
            )
            agent = StoryAgent(context)
            staging_root = agent._codex_stage_dir("codex_story_images")
            shutil.rmtree(staging_root, ignore_errors=True)
            staging_root.mkdir(parents=True, exist_ok=True)
            self.addCleanup(shutil.rmtree, staging_root, True)

            def rewrite_board(**_kwargs):
                staging_images = staging_root / "images"
                staging_images.mkdir(parents=True, exist_ok=True)
                (staging_images / "readonly-board_scene_01.png").write_bytes(b"wrong")
                (staging_root / "readonly-board_storyboard_lines.txt").write_text("擅自合并。\n", encoding="utf-8")
                return StageResult("done", "rewritten")

            with patch.object(agent, "_codex_task", side_effect=rewrite_board), patch.object(
                agent, "_has_story_visual_control", return_value=True
            ):
                result = agent._stage_codex_story_images(manifest)
            self.assertEqual(result.status, "retrying")
            self.assertIn("改写了只读权威分镜", result.message)
            self.assertEqual(
                (staging_root / "readonly-board_storyboard_lines.txt").read_text(encoding="utf-8"),
                "第一镜。\n第二镜。\n",
            )
            self.assertFalse((staging_root / "images" / "readonly-board_scene_01.png").exists())

    def test_story_image_rejection_quarantines_final_and_staging_copies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：双层隔离"
            init_project(project, story_name="双层隔离", slug="dual-quarantine")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="双层隔离",
                slug="dual-quarantine",
                execute=True,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context)
            final_images = project_paths(project).images / "images"
            final_images.mkdir(parents=True, exist_ok=True)
            final = final_images / "dual-quarantine_scene_01.png"
            final.write_bytes(b"final")
            staging_root = agent._codex_stage_dir("codex_story_images")
            shutil.rmtree(staging_root, ignore_errors=True)
            staging = staging_root / "images"
            staging.mkdir(parents=True, exist_ok=True)
            self.addCleanup(shutil.rmtree, staging_root, True)
            staged = staging / "dual-quarantine_scene_01.png"
            staged.write_bytes(b"staging")
            with patch.object(agent, "_image_dir", return_value=final_images):
                moved = agent._quarantine_story_images([1])
            self.assertEqual(moved, [1])
            self.assertFalse(final.exists())
            self.assertFalse(staged.exists())
            quarantine = sorted((project_paths(project).status / "rejected" / "story_images").iterdir())[-1]
            self.assertTrue((quarantine / "dual-quarantine_scene_01.png").exists())
            self.assertTrue((quarantine / "staging_dual-quarantine_scene_01.png").exists())

    def test_story_image_retry_prompt_includes_list_form_review_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：重做指令"
            init_project(project, story_name="重做指令", slug="retry-list")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="重做指令",
                slug="retry-list",
                execute=True,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context)
            review = project_paths(project).status / "reviews" / "story_images_review_review.json"
            review.parent.mkdir(parents=True, exist_ok=True)
            review.write_text(
                json.dumps(
                    {
                        "retry_instructions": [
                            {"scene": 3, "instruction": "不得出现兔妈妈。"},
                            "第4镜：只保留小兔子。",
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            staging = Path(directory) / "staging"
            images = staging / "images"
            images.mkdir(parents=True)
            storyboard = staging / "retry-list_storyboard_lines.txt"
            storyboard.write_text("第三镜。\n第四镜。\n", encoding="utf-8")
            prompt = agent._story_images_batch_prompt(
                handoff=staging / "handoff.md",
                staging_images=images,
                staging_storyboard=storyboard,
                story_lines=["第三镜。", "第四镜。"],
                indices=[3],
            )
            self.assertIn("这是独立视觉审核给出的强制重做要求", prompt)
            self.assertIn("第3镜：不得出现兔妈妈。", prompt)
            self.assertNotIn("第4镜：只保留小兔子。", prompt)

    def test_text_only_source_archive_preserves_verified_clean_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：文字校对归档"
            manifest = init_project(project, story_name="文字校对归档", slug="text-only-archive")
            paths = project_paths(project)
            original = paths.inputs / "original.mp4"
            clean = paths.inputs / "clean.mp4"
            story = paths.inputs / "story.txt"
            decisions = paths.status / "source_edit" / "edit_decisions.json"
            original.write_bytes(b"original")
            clean.write_bytes(b"verified-clean")
            story.write_text("旧文本", encoding="utf-8")
            decisions.parent.mkdir(parents=True, exist_ok=True)
            decisions.write_text("{}", encoding="utf-8")
            manifest["inputs"].update(
                {"greenscreen_video_original": str(original), "greenscreen_video": str(clean), "story_text": str(story)}
            )
            manifest["outputs"]["source_edit_decisions"] = str(decisions)
            write_manifest(paths, manifest)
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="文字校对归档",
                slug="text-only-archive",
                execute=True,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            archive = StoryAgent(context)._archive_source_edit_attempt(preserve_media=True)
            self.assertTrue(clean.exists())
            self.assertEqual(clean.read_bytes(), b"verified-clean")
            self.assertFalse(story.exists())
            self.assertTrue((archive / story.name).exists())

    def test_resume_preserves_cumulative_active_runtime_and_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：累计时限"
            manifest = init_project(project, story_name="累计时限", slug="cumulative-runtime")
            paths = project_paths(project)
            manifest["agent"]["active_elapsed_seconds"] = 300.0
            manifest["agent"]["started_at"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600)
            )
            manifest["agent"]["deadline_hours"] = 2.0
            manifest["agent"]["status"] = "blocked"
            write_manifest(paths, manifest)
            resumed = resume_job(project)
            self.assertGreaterEqual(float(resumed["agent"]["active_elapsed_seconds"]), 3899.0)
            self.assertEqual(resumed["agent"]["started_at"], "")
            resumed["agent"]["active_elapsed_seconds"] = 2.1 * 3600
            with self.assertRaisesRegex(AgentRuntimeError, "累计运行时限"):
                assert_runnable(resumed)

    def test_reconcile_marks_interrupted_but_verified_prior_stage_passed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：中断核验"
            manifest = init_project(project, story_name="中断核验", slug="reconcile-interrupted")
            paths = project_paths(project)
            story = paths.inputs / "story.txt"
            original = paths.inputs / "original.mp4"
            clean = paths.inputs / "clean.mp4"
            decisions = paths.status / "source_edit" / "edit_decisions.json"
            story.write_text("完整故事。", encoding="utf-8")
            original.write_bytes(b"original")
            clean.write_bytes(b"clean")
            decisions.parent.mkdir(parents=True, exist_ok=True)
            decisions.write_text("{}", encoding="utf-8")
            manifest["inputs"].update(
                {"story_text": str(story), "greenscreen_video_original": str(original), "greenscreen_video": str(clean)}
            )
            manifest["outputs"]["source_edit_decisions"] = str(decisions)
            mark_stage(manifest, "source_edit", "running")
            write_manifest(paths, manifest)
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="中断核验",
                slug="reconcile-interrupted",
                execute=True,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context)
            current = load_manifest(paths)
            assert current is not None
            agent._reconcile_completed_stage_records(current, "source_text_correction")
            reconciled = load_manifest(paths)
            assert reconciled is not None
            self.assertEqual(reconciled["agent"]["stages"]["source_edit"]["status"], "passed")
            self.assertEqual(reconciled["agent"]["blocked_reason"], "")

    def test_reconcile_marks_recovered_blocked_stage_passed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：阻塞后恢复"
            manifest = init_project(project, story_name="阻塞后恢复", slug="reconcile-blocked")
            paths = project_paths(project)
            story = paths.inputs / "story.txt"
            original = paths.inputs / "original.mp4"
            clean = paths.inputs / "clean.mp4"
            decisions = paths.status / "source_edit" / "edit_decisions.json"
            story.write_text("完整故事。", encoding="utf-8")
            original.write_bytes(b"original")
            clean.write_bytes(b"clean")
            decisions.parent.mkdir(parents=True, exist_ok=True)
            decisions.write_text("{}", encoding="utf-8")
            manifest["inputs"].update(
                {"story_text": str(story), "greenscreen_video_original": str(original), "greenscreen_video": str(clean)}
            )
            manifest["outputs"]["source_edit_decisions"] = str(decisions)
            mark_stage(manifest, "source_edit", "blocked", message="等待外部恢复")
            write_manifest(paths, manifest)
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="阻塞后恢复",
                slug="reconcile-blocked",
                execute=True,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            current = load_manifest(paths)
            assert current is not None
            StoryAgent(context)._reconcile_completed_stage_records(current, "source_text_correction")
            reconciled = load_manifest(paths)
            assert reconciled is not None
            self.assertEqual(reconciled["agent"]["stages"]["source_edit"]["status"], "passed")
            self.assertEqual(reconciled["agent"]["blocked_reason"], "")

    def test_hashed_source_qa_rejects_media_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：源媒体哈希"
            init_project(project, story_name="源媒体哈希", slug="source-hash")
            paths = project_paths(project)
            artifact = paths.inputs / "clean.mp4"
            artifact.write_bytes(b"verified")
            stat = artifact.stat()
            report = paths.status / "qa_source_report.json"
            report.write_text(
                json.dumps(
                    {
                        "passed": True,
                        "artifacts": {
                            "clean_video": {
                                "path": str(artifact),
                                "sha256": file_sha256(artifact),
                                "bytes": stat.st_size,
                                "mtime_ns": stat.st_mtime_ns,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="源媒体哈希",
                slug="source-hash",
                execute=True,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context)
            self.assertTrue(agent._hashed_qa_report_passes(report))
            artifact.write_bytes(b"changed")
            self.assertFalse(agent._hashed_qa_report_passes(report))

    def test_reconciling_earlier_stage_does_not_regress_last_checkpoint(self) -> None:
        manifest: dict = {}
        ensure_manifest_v2(manifest)
        mark_stage(manifest, "source_text_correction", "passed")
        mark_stage(manifest, "source_edit", "running")
        mark_stage(manifest, "source_edit", "passed")
        self.assertEqual(manifest["agent"]["last_checkpoint"], "source_text_correction")

    def test_transient_stage_failure_retries_and_then_checkpoints(self) -> None:
        class TransientAgent(StoryAgent):
            calls = 0

            def _next_stage(self, manifest):
                if manifest["agent"]["stages"]["generate_videos"]["status"] == "passed":
                    return "done", lambda _manifest: StageResult("done", "done")

                def action(_manifest):
                    self.calls += 1
                    return StageResult("failed", "HTTP 502 temporary") if self.calls == 1 else StageResult("done", "recovered")

                return "generate_videos", action

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：重试"
            init_project(project, story_name="重试", slug="retry")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="重试",
                slug="retry",
                execute=True,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = TransientAgent(context)
            self.assertEqual(agent.run(max_steps=4), 0)
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            record = manifest["agent"]["stages"]["generate_videos"]
            self.assertEqual(record["status"], "passed")
            self.assertEqual(record["attempts"], 2)
            self.assertEqual(agent.calls, 2)

    def test_codex_subtask_failure_retries_in_a_new_attempt(self) -> None:
        class CodexFailOnceAgent(StoryAgent):
            calls = 0

            def _next_stage(self, manifest):
                stage = "codex_story_images"
                if manifest["agent"]["stages"][stage]["status"] == "passed":
                    return "done", lambda _manifest: StageResult("done", "done")

                def action(_manifest):
                    self.calls += 1
                    return StageResult("failed", "Codex CLI 子任务失败") if self.calls == 1 else StageResult("done", "Codex recovered")

                return stage, action

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：Codex重试"
            init_project(project, story_name="Codex重试", slug="codex-retry")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="Codex重试",
                slug="codex-retry",
                execute=True,
                update_latest_episode=False,
                codex_mode="cli",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = CodexFailOnceAgent(context)
            self.assertEqual(agent.run(max_steps=4), 0)
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            record = manifest["agent"]["stages"]["codex_story_images"]
            self.assertEqual(record["status"], "passed")
            self.assertEqual(record["attempts"], 2)

    def test_codex_exec_places_prompt_before_variadic_image_option(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：视觉审核命令"
            init_project(project, story_name="视觉审核命令", slug="visual-command")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="视觉审核命令",
                slug="visual-command",
                execute=True,
                update_latest_episode=False,
                codex_mode="cli",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context)
            prompt_path = Path(directory) / "prompt.md"
            image_path = Path(directory) / "sheet.jpg"
            prompt_path.write_text("视觉审核提示", encoding="utf-8")
            image_path.write_bytes(b"image")
            with patch("story_agent.subprocess.Popen") as popen:
                process = popen.return_value
                process.communicate.return_value = ("", "")
                process.returncode = 0
                result = agent._run_codex_exec("visual_review", prompt_path, [image_path])
            command = popen.call_args.args[0]
            self.assertEqual(result.status, "done")
            self.assertLess(command.index("视觉审核提示"), command.index("--image"))
            self.assertEqual(command[command.index("--image") + 1], str(image_path))

    def test_visual_review_uses_native_multimodal_without_user_vision_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：原生视觉审核"
            init_project(project, story_name="原生视觉审核", slug="native-vision-review")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="原生视觉审核",
                slug="native-vision-review",
                execute=True,
                update_latest_episode=False,
                codex_mode="cli",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context)
            prompt_path = Path(directory) / "prompt.md"
            image_path = Path(directory) / "sheet.jpg"
            prompt_path.write_text("原生多模态审核提示", encoding="utf-8")
            image_path.write_bytes(b"image")
            with patch("story_agent.subprocess.Popen") as popen:
                process = popen.return_value
                process.communicate.return_value = ("", "")
                process.returncode = 0
                result = agent._run_codex_exec("story_images_review", prompt_path, [image_path])
            command = popen.call_args.args[0]
            self.assertEqual(result.status, "done")
            self.assertIn("--ignore-user-config", command)
            self.assertLess(command.index("exec"), command.index("--ignore-user-config"))
            self.assertEqual(command[command.index("--image") + 1], str(image_path))

    def test_visual_producer_with_images_keeps_user_config_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：视觉生产兼容"
            init_project(project, story_name="视觉生产兼容", slug="visual-producer-compat")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="视觉生产兼容",
                slug="visual-producer-compat",
                execute=True,
                update_latest_episode=False,
                codex_mode="cli",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context)
            prompt_path = Path(directory) / "prompt.md"
            image_path = Path(directory) / "sheet.jpg"
            prompt_path.write_text("视觉生产提示", encoding="utf-8")
            image_path.write_bytes(b"image")
            with patch("story_agent.subprocess.Popen") as popen:
                process = popen.return_value
                process.communicate.return_value = ("", "")
                process.returncode = 0
                result = agent._run_codex_exec("product_annotation", prompt_path, [image_path])
            command = popen.call_args.args[0]
            self.assertEqual(result.status, "done")
            self.assertNotIn("--ignore-user-config", command)

    def test_dead_process_lock_is_reclaimed_but_live_lock_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：锁恢复"
            init_project(project, story_name="锁恢复", slug="lock")
            lock = project_paths(project).status / "story_agent.lock"
            lock.write_text(json.dumps({"pid": 99999999}), encoding="utf-8")
            with job_lock(project):
                self.assertTrue(lock.exists())
            self.assertFalse(lock.exists())

            lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
            with self.assertRaises(AgentRuntimeError):
                with job_lock(project):
                    pass
            lock.unlink()

    def test_missing_target_video_is_not_hidden_by_unrelated_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：缺帧"
            manifest = as_frozen_v3_legacy(init_project(project, story_name="缺帧", slug="missing-frame"))
            paths = project_paths(project)
            jobs = paths.video_jobs / "missing-frame_image_video_jobs.csv"
            jobs.write_text(
                "scene,target_video_filename\n1,01.mp4\n2,02.mp4\n",
                encoding="utf-8-sig",
            )
            manifest["outputs"]["jobs_csv"] = str(jobs)
            from story_project import write_manifest

            write_manifest(paths, manifest)
            videos = paths.video_jobs / "videos"
            videos.mkdir()
            (videos / "01.mp4").write_bytes(b"one")
            (videos / "unrelated.mp4").write_bytes(b"extra")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="缺帧",
                slug="missing-frame",
                execute=False,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context, read_only=True)
            self.assertFalse(agent._has_generated_videos(manifest))
            (videos / "02.mp4").write_bytes(b"two")
            self.assertTrue(agent._has_generated_videos(manifest))

    def test_suno_login_or_browser_loss_writes_recoverable_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：Suno阻塞"
            manifest = as_frozen_v3_legacy(init_project(project, story_name="Suno阻塞", slug="suno-block"))
            write_manifest(project_paths(project), manifest)
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="Suno阻塞",
                slug="suno-block",
                execute=True,
                update_latest_episode=False,
                codex_mode="cli",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context)
            with patch.object(agent, "_codex_task", return_value=StageResult("blocked", "No browser is available")):
                result = agent._stage_suno_generate(manifest)
            self.assertEqual(result.status, "blocked")
            blocker = project / "02_图生视频" / "music" / "suno_cli_blocker.md"
            self.assertTrue(blocker.exists())
            self.assertIn("Codex 主任务", blocker.read_text(encoding="utf-8"))

    def test_stage_record_tracks_context_output_provider_cost_and_retry_reason(self) -> None:
        manifest = ensure_manifest_v2({"story": {"name": "证据测试"}})
        input_sha = manifest_context_sha256(manifest)
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "result.json"
            artifact.write_text('{"ok": true}', encoding="utf-8")
            mark_stage(
                manifest,
                "generate_videos",
                "running",
                input_hashes={"manifest_context": input_sha},
                provider="stub",
            )
            mark_stage(
                manifest,
                "generate_videos",
                "retrying",
                output_hashes=existing_artifact_hashes([artifact]),
                provider="stub",
                actual_cost=3.25,
                retry_reason="temporary timeout",
            )
            mark_stage(manifest, "generate_videos", "running")
            record = manifest["agent"]["stages"]["generate_videos"]
            self.assertEqual(record["input_hashes"]["manifest_context"], input_sha)
            self.assertEqual(record["output_hashes"][str(artifact)], file_sha256(artifact))
            self.assertEqual(record["provider"], "stub")
            self.assertEqual(record["actual_cost"], 3.25)
            self.assertEqual(record["retry_reason"], "temporary timeout")
            self.assertEqual(record["attempts"], 2)

    def test_failure_classifier_blocks_login_but_retries_network_failures(self) -> None:
        self.assertEqual(classify_command_failure("Suno CAPTCHA / login required"), "blocked")
        self.assertEqual(classify_command_failure("No browser is available: browser discovery returned an empty list"), "blocked")
        self.assertEqual(classify_command_failure("HTTP 502 connection reset"), "failed")

    def test_long_task_heartbeat_observes_cancel_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：心跳"
            init_project(project, story_name="心跳", slug="heartbeat")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="心跳",
                slug="heartbeat",
                execute=True,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context)
            self.assertFalse(agent._heartbeat_and_cancelled())
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            self.assertTrue(manifest["agent"]["heartbeat_at"])
            request_cancel(project)
            self.assertTrue(agent._heartbeat_and_cancelled())

    def test_status_reports_remaining_work_eta_and_recovery_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：状态"
            init_project(project, story_name="状态", slug="status")
            context = AgentContext(
                project_dir=project,
                inbox=None,
                story_name="状态",
                slug="status",
                execute=False,
                update_latest_episode=False,
                codex_mode="handoff",
                codex_model="",
                codex_sandbox="workspace-write",
                codex_approval="never",
                codex_path="codex",
                codex_timeout=30,
            )
            agent = StoryAgent(context, read_only=True)
            output = io.StringIO()
            with redirect_stdout(output):
                agent.status()
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["remaining_work"])
            remaining_names = [item["stage"] for item in payload["remaining_work"]]
            self.assertIn("source_text_correction", remaining_names)
            self.assertIn("source_edit_review", remaining_names)
            self.assertGreater(payload["estimated_remaining_minutes"]["nominal"], 0)
            self.assertIn("supervisor", payload)
            self.assertFalse(payload["completion_valid"])
            self.assertFalse((project / "02_图生视频" / "music").exists())

    def test_manifest_v2_and_budget_limits(self) -> None:
        manifest = ensure_manifest_v2({}, soft_budget_cny=50, hard_budget_cny=100)
        self.assertEqual(manifest["version"], 2)
        self.assertEqual(manifest["agent"]["stages"]["source_edit"]["status"], "pending")
        self.assertEqual(manifest["agent"]["stages"]["doctor"]["attempts"], 0)
        ledger = BudgetLedger(manifest)
        reservation = ledger.authorize(40, label="first")
        ledger.settle(reservation, 35, provider="stub", request_id="r1")
        self.assertEqual(ledger.data["spent"], 35)
        with self.assertRaises(BudgetExceeded):
            ledger.authorize(20, label="non-critical retry")
        critical = ledger.authorize(20, label="required output", critical=True)
        ledger.release(critical, reason="test")
        with self.assertRaises(BudgetExceeded):
            ledger.authorize(70, label="over hard", critical=True)

    def test_review_requires_score_no_critical_and_matching_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.json"
            artifact.write_text("{}", encoding="utf-8")
            payload = {
                "approved": True,
                "score": 90,
                "critical_errors": [],
                "artifact_sha256": file_sha256(artifact),
            }
            self.assertTrue(review_passes(payload, artifact=artifact))
            payload["critical_errors"] = ["semantic deletion"]
            self.assertFalse(review_passes(payload, artifact=artifact))
            payload["critical_errors"] = []
            artifact.write_text('{"changed": true}', encoding="utf-8")
            self.assertFalse(review_passes(payload, artifact=artifact))
            self.assertFalse(review_passes({"approved": True, "score": float("nan"), "critical_errors": []}))
            self.assertFalse(review_passes({"approved": True, "score": "invalid", "critical_errors": []}))

    def test_submit_is_idempotent_and_cancel_is_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "测试故事_绿幕.mp4"
            fixture = Path(__file__).resolve().parents[1] / "tools" / "video-subtitle-remover" / "test" / "test2.mp4"
            shutil.copy2(fixture, video)
            registry = JobRegistry(root / "registry.json")
            job_id, project, created = submit_video_job(video, projects_root=root / "projects", registry=registry)
            self.assertTrue(created)
            same_id, same_project, created_again = submit_video_job(video, projects_root=root / "projects", registry=registry)
            self.assertFalse(created_again)
            self.assertEqual((same_id, same_project), (job_id, project))
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            self.assertEqual(manifest["version"], 2)
            self.assertEqual(manifest["agent"]["budget"]["hard_limit"], 100.0)
            self.assertTrue(Path(manifest["inputs"]["greenscreen_video"]).exists())
            self.assertEqual(manifest["agent"]["source"]["media"]["video"]["width"], 1280)
            self.assertEqual(manifest["agent"]["source"]["media"]["video"]["height"], 720)
            self.assertGreater(manifest["agent"]["source"]["media"]["duration_sec"], 0)
            contract = manifest["agent"]["input_contract"]
            self.assertEqual(contract["mode"], "single_greenscreen")
            self.assertEqual(len(contract["user_inputs"]), 1)
            self.assertEqual(contract["user_inputs"][0]["role"], "greenscreen_video")
            self.assertEqual(contract["derived_inputs"], {})

            cancelled = request_cancel(project)
            self.assertTrue(cancelled["agent"]["cancel_requested"])
            resumed = resume_job(project)
            self.assertFalse(resumed["agent"]["cancel_requested"])
            report = render_job_report(project)
            self.assertTrue(report.exists())
            report_text = report.read_text(encoding="utf-8")
            self.assertIn(job_id, report_text)
            self.assertIn("## 剩余工作", report_text)
            self.assertIn("恢复动作", report_text)
            self.assertTrue((project_paths(project).status / "成本报告.md").exists())
            self.assertTrue((project_paths(project).status / "QA汇总.md").exists())
            self.assertTrue((project_paths(project).status / "异常说明.md").exists())

    def test_submit_copies_and_hashes_input_lut(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "测试故事_绿幕.mp4"
            fixture = Path(__file__).resolve().parents[1] / "tools" / "video-subtitle-remover" / "test" / "test2.mp4"
            shutil.copy2(fixture, video)
            lut = root / "sony.cube"
            lut.write_text("LUT_3D_SIZE 2\n0 0 0\n0 0 1\n0 1 0\n0 1 1\n1 0 0\n1 0 1\n1 1 0\n1 1 1\n", encoding="utf-8")
            registry = JobRegistry(root / "registry.json")
            _, project, created = submit_video_job(video, lut=lut, projects_root=root / "projects", registry=registry)
            self.assertTrue(created)
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            copied = Path(manifest["inputs"]["color_lut"])
            self.assertTrue(copied.is_file())
            self.assertEqual(manifest["agent"]["source"]["color_lut"]["sha256"], file_sha256(lut))
            processing_assets = manifest["agent"]["input_contract"]["processing_assets"]
            self.assertEqual(len(processing_assets), 1)
            self.assertEqual(processing_assets[0]["role"], "color_lut")
            self.assertEqual(processing_assets[0]["sha256"], file_sha256(lut))

    def test_submit_replaces_interrupted_partial_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "恢复测试_绿幕.mp4"
            fixture = Path(__file__).resolve().parents[1] / "tools" / "video-subtitle-remover" / "test" / "test2.mp4"
            shutil.copy2(fixture, video)
            project = root / "projects" / "故事剪辑：恢复测试"
            partial = project / "00_输入素材" / "recovery-test_greenscreen_source.mp4"
            partial.parent.mkdir(parents=True)
            partial.write_bytes(video.read_bytes()[:1024])
            _, submitted_project, _ = submit_video_job(
                video,
                projects_root=root / "projects",
                story_name="恢复测试",
                slug="recovery-test",
                registry=JobRegistry(root / "registry.json"),
            )
            copied = submitted_project / "00_输入素材" / "recovery-test_greenscreen_source.mp4"
            self.assertEqual(copied.stat().st_size, video.stat().st_size)
            self.assertEqual(file_sha256(copied), file_sha256(video))

    def test_final_delivery_does_not_mark_incomplete_project_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：不完整"
            init_project(project, story_name="不完整", slug="incomplete")
            report = final_delivery(project)
            manifest = load_manifest(project_paths(project))
            assert manifest is not None
            self.assertTrue(report.exists())
            self.assertNotIn("completed_at", manifest)
            self.assertIn("部分交付", report.read_text(encoding="utf-8"))

    def test_low_disk_space_blocks_before_media_generation(self) -> None:
        manifest = ensure_manifest_v2({})
        manifest["agent"]["min_free_disk_gb"] = 10.0
        fake_usage = shutil._ntuple_diskusage(total=20 * 1024**3, used=19 * 1024**3, free=1 * 1024**3)
        with tempfile.TemporaryDirectory() as directory, patch("story_agent_runtime.shutil.disk_usage", return_value=fake_usage):
            with self.assertRaisesRegex(Exception, "磁盘可用空间不足"):
                assert_runnable(manifest, Path(directory))


if __name__ == "__main__":
    unittest.main()
