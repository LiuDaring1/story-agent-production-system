from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import assemble_suno_music
from story_agent import AgentContext, StageResult, StoryAgent
from story_agent_runtime import file_sha256
from story_module_adapters import MockMusicProviderAdapter, SunoMusicProviderAdapter
from story_module_ports import (
    ModuleCapabilities,
    ModuleIdentity,
    MusicProviderPort,
    MusicProviderRequest,
    MusicProviderResult,
)
from story_module_registry import (
    MODULE_EXECUTION_MODE_ENV,
    MODULE_EXECUTION_MODE_REQUIRED_ENV,
    MODULE_PROFILE_ENV,
    MODULE_PROFILE_REQUIRED_ENV,
    ModuleRegistry,
    build_registry_for_profile,
)
from story_project import init_project, project_paths, write_manifest
from tests.test_story_agent_runtime import as_frozen_v3_legacy


def _music_agent(root: Path, slug: str = "music-port") -> tuple[StoryAgent, dict]:
    project = root / f"故事剪辑：{slug}"
    manifest = as_frozen_v3_legacy(init_project(project, story_name=slug, slug=slug))
    write_manifest(project_paths(project), manifest)
    context = AgentContext(
        project, None, slug, slug, True, False,
        "cli", "", "workspace-write", "never", "codex", 30,
    )
    agent = StoryAgent(context)
    request = agent._music_dir() / f"{slug}_suno_music_request.md"
    request.write_text("existing Suno execution request\n", encoding="utf-8")
    return agent, manifest


def _mock_music_environment() -> dict[str, str]:
    return {
        MODULE_PROFILE_ENV: "mock-music",
        MODULE_PROFILE_REQUIRED_ENV: "mock-music",
        MODULE_EXECUTION_MODE_ENV: "test",
        MODULE_EXECUTION_MODE_REQUIRED_ENV: "test",
    }


class MusicProviderProductionSeamTests(unittest.TestCase):
    def test_production_default_delegates_suno_stage_to_original_codex_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, manifest = _music_agent(Path(directory), "music-production")
            registry = build_registry_for_profile("production-default")
            adapter = registry.music_provider()
            self.assertIsInstance(adapter, SunoMusicProviderAdapter)
            agent._module_registry = registry
            captured_task = {}
            captured_request: list[MusicProviderRequest] = []
            original_execute = adapter.execute

            def execute(request: MusicProviderRequest, *, executor):
                captured_request.append(request)
                return original_execute(request, executor=executor)

            def codex_task(**kwargs):
                captured_task.update(kwargs)
                music_dir = agent._music_dir()
                (music_dir / "music-production_suno_prompts.md").write_text("codex prompts\n", encoding="utf-8")
                (music_dir / "music-production_music_plan.csv").write_text(
                    "segment,target_audio_filename\n1,01_music-production_music.mp3\n",
                    encoding="utf-8",
                )
                target = agent._suno_downloads_dir() / "01_music-production_music.mp3"
                target.write_bytes(b"production audio")
                return StageResult("done", "original Codex/Suno result", kwargs["handoff"])

            with patch.object(adapter, "execute", side_effect=execute), patch.object(
                agent, "_codex_task", side_effect=codex_task
            ):
                result = agent._stage_suno_generate(manifest)

            self.assertEqual(result.status, "done")
            self.assertEqual(result.message, "original Codex/Suno result")
            self.assertEqual(captured_task["stage"], "suno_generate")
            self.assertEqual(captured_task["label"], "Codex/Suno 浏览器音乐生成")
            handoff = agent._music_dir() / "music-production_suno_browser_handoff.md"
            self.assertEqual(captured_task["handoff"], handoff)
            self.assertEqual(
                captured_task["prompt"],
                f"请读取并执行这份 Suno 浏览器自动化任务：\n{handoff}\n\n"
                "目标是生成并下载第一首可用音乐，按音乐分段 CSV 的 target_audio_filename 重命名，"
                "保存到指定 suno_downloads 目录。若当前 CLI 无浏览器控制能力、Suno 未登录、遇到验证码或付费弹窗，"
                f"请写入 `{agent._music_dir() / 'suno_cli_blocker.md'}` 说明原因，不要假装完成。",
            )
            self.assertEqual(len(captured_request), 1)
            request = captured_request[0]
            self.assertEqual(request.execution_request_path, handoff)
            self.assertEqual(request.execution_request_sha256, file_sha256(handoff))
            self.assertEqual(request.output_targets[0].name, "01_music-production_music.mp3")
            self.assertEqual(
                (agent._music_dir() / "music-production_suno_prompts.md").read_text(encoding="utf-8"),
                "codex prompts\n",
            )

    def test_mock_music_replaces_real_consumer_without_codex_or_browser_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, manifest = _music_agent(Path(directory))
            with patch.dict(os.environ, _mock_music_environment(), clear=False):
                registry = build_registry_for_profile("mock-music", execution_mode="test")
            adapter = registry.music_provider()
            self.assertIsInstance(adapter, MockMusicProviderAdapter)
            agent._module_registry = registry
            captured: list[tuple[MusicProviderRequest, MusicProviderResult]] = []
            original_execute = adapter.execute

            def execute(request: MusicProviderRequest, *, executor):
                result = original_execute(request, executor=executor)
                captured.append((request, result))
                return result

            with patch.object(adapter, "execute", side_effect=execute), patch.object(
                agent, "_codex_task", side_effect=AssertionError("real Codex/browser/Suno executor called")
            ) as executor, patch("story_agent.subprocess.Popen", side_effect=AssertionError("subprocess started")) as popen:
                result = agent._stage_suno_generate(manifest)

            self.assertEqual(result.status, "done", result.message)
            executor.assert_not_called()
            popen.assert_not_called()
            self.assertEqual(len(captured), 1)
            request, port_result = captured[0]
            self.assertEqual(request.operation, "generate_music_segments")
            self.assertEqual(request.output_targets[0].name, "01_music-port_music.mp3")
            self.assertEqual(port_result.provider, "mock")
            self.assertFalse(port_result.production_eligible)
            self.assertTrue(all(not item["production_eligible"] for item in port_result.output_artifacts))
            self.assertTrue(request.output_targets[0].is_file())
            self.assertFalse((agent._music_dir() / "music-port_suno_prompts.md").exists())
            self.assertFalse(agent._music_plan().exists())

    def test_protocol_only_music_provider_fake_reaches_suno_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, manifest = _music_agent(Path(directory), "music-fake")
            captured: list[MusicProviderRequest] = []

            class ProtocolFake:
                identity = ModuleIdentity("music_provider", "fake/v1", "protocol-fake", "protocol-fake/v1")
                capabilities = ModuleCapabilities(provider="fake", deterministic=True)

                def execute(self, request: MusicProviderRequest, *, executor) -> MusicProviderResult:
                    del executor
                    captured.append(request)
                    for target in request.output_targets:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(b"fake audio")
                    return MusicProviderResult(
                        True, request.operation, (), "fake", "fixture", "fake-music-1",
                        request.attempt_id, request.execution_request_sha256, "protocol-fake/v1", True,
                    )

            fake = ProtocolFake()
            self.assertIsInstance(fake, MusicProviderPort)
            registry = ModuleRegistry(profile_name="production-default")
            registry.register("music_provider", fake)
            agent._module_registry = registry
            with patch.object(agent, "_codex_task", side_effect=AssertionError("concrete executor called")) as executor:
                result = agent._stage_suno_generate(manifest)
            self.assertEqual(result.status, "done", result.message)
            executor.assert_not_called()
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0].output_targets[0].name, "01_music-fake_music.mp3")

    def test_has_suno_audio_keeps_any_audio_file_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, manifest = _music_agent(Path(directory), "any-audio")
            downloads = agent._suno_downloads_dir()
            self.assertFalse(agent._has_suno_audio(manifest))
            downloads.mkdir(parents=True, exist_ok=True)
            (downloads / "unrelated-name.wav").write_bytes(b"nonempty")
            self.assertTrue(agent._has_suno_audio(manifest))

    def test_assemble_find_audio_prefers_exact_name_then_segment_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clips = Path(directory)
            exact = clips / "required-name.mp3"
            fallback = clips / "01_downloaded-from-browser.wav"
            exact.write_bytes(b"exact")
            fallback.write_bytes(b"fallback")
            row = {"segment": "1", "target_audio_filename": exact.name}
            self.assertEqual(assemble_suno_music._find_audio(row, clips), exact)
            exact.unlink()
            self.assertEqual(assemble_suno_music._find_audio(row, clips), fallback)


if __name__ == "__main__":
    unittest.main()
