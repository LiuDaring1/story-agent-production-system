from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from story_agent import AgentContext, StoryAgent
from story_agent_runtime import file_sha256
from story_module_adapters import MockImageGeneratorAdapter
from story_module_ports import (
    ImageGeneratorPort,
    ImageGeneratorRequest,
    ImageGeneratorResult,
    ModuleCapabilities,
    ModuleIdentity,
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
from tests.test_visual_sample_gate import _fixture
from visual_sample_gate import compile_visual_sample_plan, visual_sample_paths


def _mock_image_environment() -> dict[str, str]:
    return {
        MODULE_PROFILE_ENV: "mock-image",
        MODULE_PROFILE_REQUIRED_ENV: "mock-image",
        MODULE_EXECUTION_MODE_ENV: "test",
        MODULE_EXECUTION_MODE_REQUIRED_ENV: "test",
    }


def _legacy_story_agent(root: Path, *, lines: int, batch_size: int) -> tuple[StoryAgent, dict]:
    project = root / "故事剪辑：image-port"
    manifest = as_frozen_v3_legacy(init_project(project, story_name="image-port", slug="image-port"))
    source = project_paths(project).inputs / "image-port_source.txt"
    source.write_text("".join(f"第{index}镜。\n" for index in range(1, lines + 1)), encoding="utf-8")
    manifest["inputs"]["story_text"] = str(source)
    write_manifest(project_paths(project), manifest)
    context = AgentContext(
        project, None, "image-port", "image-port", True, False,
        "cli", "", "workspace-write", "never", "codex", 30,
        codex_story_image_batch_size=batch_size,
    )
    return StoryAgent(context), manifest


class ImageGeneratorProductionSeamTests(unittest.TestCase):
    def test_visual_sample_consumer_uses_mock_port_without_codex_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest, agent, context = _fixture(Path(directory))
            initial_plan = compile_visual_sample_plan(project, context)
            expected_targets = tuple(
                project / item["expected_path"]
                for item in initial_plan["requirements"]
                if not isinstance(item.get("asset"), dict)
            )
            locked = _mock_image_environment()
            with patch.dict(os.environ, locked, clear=False):
                registry = build_registry_for_profile("mock-image", execution_mode="test")
            adapter = registry.image_generator()
            self.assertIsInstance(adapter, MockImageGeneratorAdapter)
            agent._module_registry = registry
            captured: list[tuple[ImageGeneratorRequest, ImageGeneratorResult]] = []
            original_execute = adapter.execute

            def execute(request: ImageGeneratorRequest, *, executor):
                result = original_execute(request, executor=executor)
                captured.append((request, result))
                return result

            with patch.object(adapter, "execute", side_effect=execute), patch.object(
                agent, "_codex_task", side_effect=AssertionError("real Codex/ImageGen executor called")
            ) as executor:
                result = agent._stage_visual_samples(manifest)

            self.assertEqual(result.status, "done", result.message)
            executor.assert_not_called()
            self.assertEqual(len(captured), 1)
            request, port_result = captured[0]
            self.assertEqual(request.operation, "generate_visual_samples")
            self.assertEqual(request.output_targets, expected_targets)
            self.assertEqual(request.execution_request_path, visual_sample_paths(project)["handoff"])
            self.assertEqual(request.execution_request_sha256, file_sha256(request.execution_request_path))
            self.assertEqual(port_result.provider, "mock")
            self.assertFalse(port_result.production_eligible)
            self.assertTrue(all(not item["production_eligible"] for item in port_result.output_artifacts))
            self.assertTrue(visual_sample_paths(project)["machine_qa"].is_file())
            self.assertFalse(visual_sample_paths(project)["review"].exists())
            self.assertFalse(visual_sample_paths(project)["lock"].exists())
            for target in expected_targets:
                with Image.open(target) as image:
                    self.assertEqual(image.size, (256, 256))

    def test_visual_sample_existing_assets_skip_image_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest, agent, _context = _fixture(
                Path(directory), state=False,
                preview_kinds=("style_anchor", "character_sheet", "scale_anchor")
            )
            with patch.dict(os.environ, _mock_image_environment(), clear=False):
                registry = build_registry_for_profile("mock-image", execution_mode="test")
            agent._module_registry = registry
            with patch.object(registry.image_generator(), "execute") as execute:
                result = agent._stage_visual_samples(manifest)
            self.assertEqual(result.status, "done", result.message)
            execute.assert_not_called()

    def test_formal_story_image_consumer_uses_mock_port_without_codex_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, manifest = _legacy_story_agent(root, lines=3, batch_size=1)
            final_images = agent.context.paths.images / "images"
            final_images.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (256, 256), (1, 2, 3)).save(final_images / "image-port_scene_01.png")
            staging = agent._codex_stage_dir("codex_story_images")
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            self.addCleanup(shutil.rmtree, staging, True)
            with patch.dict(os.environ, _mock_image_environment(), clear=False):
                registry = build_registry_for_profile("mock-image", execution_mode="test")
            adapter = registry.image_generator()
            agent._module_registry = registry
            captured: list[tuple[ImageGeneratorRequest, ImageGeneratorResult]] = []
            original_execute = adapter.execute

            def execute(request: ImageGeneratorRequest, *, executor):
                result = original_execute(request, executor=executor)
                captured.append((request, result))
                return result

            with patch.object(adapter, "execute", side_effect=execute), patch.object(
                agent, "_codex_task", side_effect=AssertionError("real Codex/ImageGen executor called")
            ) as executor, patch.object(agent, "_has_story_visual_control", return_value=True):
                result = agent._stage_codex_story_images(manifest)

            self.assertEqual(result.status, "retrying", result.message)
            executor.assert_not_called()
            self.assertEqual(len(captured), 1)
            request, port_result = captured[0]
            expected = staging / "images" / "image-port_scene_02.png"
            self.assertEqual(request.output_targets, (expected,))
            self.assertEqual(request.operation, "generate_story_images")
            self.assertEqual(request.input_artifacts[0]["role"], "authoritative_storyboard")
            self.assertEqual(request.input_artifacts[0]["sha256"], file_sha256(staging / "image-port_storyboard_lines.txt"))
            self.assertFalse(port_result.production_eligible)
            self.assertTrue(expected.is_file())
            self.assertTrue((final_images / expected.name).is_file())
            self.assertFalse((final_images / "image-port_scene_03.png").exists())
            self.assertFalse((agent.context.paths.status / "reviews" / "story_images_review_review.json").exists())

    def test_protocol_only_image_generator_fake_reaches_formal_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, manifest = _legacy_story_agent(Path(directory), lines=1, batch_size=5)
            staging = agent._codex_stage_dir("codex_story_images")
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            self.addCleanup(shutil.rmtree, staging, True)
            captured: list[ImageGeneratorRequest] = []

            class ProtocolFake:
                identity = ModuleIdentity("image_generator", "fake/v1", "protocol-fake", "protocol-fake/v1")
                capabilities = ModuleCapabilities(provider="fake", deterministic=True)

                def execute(self, request: ImageGeneratorRequest, *, executor) -> ImageGeneratorResult:
                    del executor
                    captured.append(request)
                    for target in request.output_targets:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        Image.new("RGB", (256, 256), (4, 5, 6)).save(target)
                    return ImageGeneratorResult(
                        True, request.operation, (), "fake", "fixture", "fake-1",
                        request.attempt_id, request.execution_request_sha256, "protocol-fake/v1", True,
                    )

            fake = ProtocolFake()
            self.assertIsInstance(fake, ImageGeneratorPort)
            registry = ModuleRegistry(profile_name="production-default")
            registry.register("image_generator", fake)
            agent._module_registry = registry
            with patch.object(agent, "_has_story_visual_control", return_value=True), patch.object(
                agent, "_has_story_image_files", return_value=True
            ):
                result = agent._stage_codex_story_images(manifest)
            self.assertEqual(result.status, "done", result.message)
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0].output_targets[0].name, "image-port_scene_01.png")


if __name__ == "__main__":
    unittest.main()
