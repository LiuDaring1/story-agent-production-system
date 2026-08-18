from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from story_agent_runtime import STORY_STAGE_DEPENDENCIES, STORY_STAGE_SEQUENCE
from story_module_adapters import (
    ApprovedStoryContractVisualDesignAdapter,
    CodexImageGeneratorAdapter,
    ExistingStorySemanticsAdapter,
    ExistingVideoGeneratorAdapter,
    LocalCompositorAdapter,
    LocalProductPackageAdapter,
    LocalPublishAssetAdapter,
    LocalReleaseLayoutAdapter,
    MockCompositorAdapter,
    MockImageGeneratorAdapter,
    MockKeyerAdapter,
    MockMusicProviderAdapter,
    MockProductPackageAdapter,
    MockPublishAssetAdapter,
    MockReleaseLayoutAdapter,
    MockStorySemanticsAdapter,
    MockVideoGeneratorAdapter,
    MockVisualDesignAdapter,
    ProductionKeyerAdapter,
    SunoMusicProviderAdapter,
    file_sha256,
)
from story_module_ports import (
    COMPOSITOR_PORT_VERSION,
    IMAGE_GENERATOR_PORT_VERSION,
    KEYER_PORT_VERSION,
    MUSIC_PROVIDER_PORT_VERSION,
    PRODUCT_PACKAGE_PORT_VERSION,
    PUBLISH_ASSET_PORT_VERSION,
    RELEASE_LAYOUT_PORT_VERSION,
    STORY_SEMANTICS_PORT_VERSION,
    VIDEO_GENERATOR_PORT_VERSION,
    VISUAL_DESIGN_PORT_VERSION,
    CompositorPort,
    CompositorRequest,
    ImageGeneratorPort,
    ImageGeneratorRequest,
    KeyerPort,
    KeyerRequest,
    MusicProviderPort,
    MusicProviderRequest,
    ProductPackagePort,
    ProductPackageRequest,
    PublishAssetPort,
    PublishAssetRequest,
    ReleaseLayoutPort,
    ReleaseLayoutRequest,
    StorySemanticsPort,
    VideoGeneratorPort,
    VideoGeneratorRequest,
    VisualDesignPort,
)
from story_module_registry import (
    MODULE_EXECUTION_MODE_ENV,
    MODULE_EXECUTION_MODE_REQUIRED_ENV,
    MODULE_PROFILE_ENV,
    MODULE_PROFILE_REQUIRED_ENV,
    ModuleRegistry,
    build_registry_for_profile,
)
from tests.test_front_half_module_ports import semantics_request, visual_request
from tests.test_story_contract_runtime import _lock_contract, _new_project


ROOT = Path(__file__).resolve().parents[1]


def _artifact(role: str, path: Path) -> dict[str, str]:
    return {"role": role, "path": str(path), "sha256": file_sha256(path)}


class M3CrossPortSmokeTests(unittest.TestCase):
    def test_ten_ports_form_one_offline_hash_bound_chain(self) -> None:
        """Exercise all ten replaceable Ports without any production provider call."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, manifest = _new_project(root)
            _lock_contract(project, manifest)
            currentness_sentinel = root / "qa_currentness_sentinel.json"
            currentness_sentinel.write_text(
                json.dumps({"qa": "owned-outside-ports", "current": True}, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            sentinel_sha = file_sha256(currentness_sentinel)

            registry = ModuleRegistry(profile_name="m3-offline-smoke", execution_mode="test")
            adapters = {
                "story_semantics": MockStorySemanticsAdapter(),
                "visual_design": MockVisualDesignAdapter(),
                "image_generator": MockImageGeneratorAdapter(),
                "video_generator": MockVideoGeneratorAdapter(),
                "music_provider": MockMusicProviderAdapter(),
                "keyer": MockKeyerAdapter(),
                "compositor": MockCompositorAdapter(),
                "release_layout": MockReleaseLayoutAdapter(),
                "publish_asset": MockPublishAssetAdapter(),
                "product_package": MockProductPackageAdapter(),
            }
            for port_name, adapter in adapters.items():
                registry.register(port_name, adapter)

            protocol_bindings = (
                (registry.story_semantics(), StorySemanticsPort),
                (registry.visual_design(), VisualDesignPort),
                (registry.image_generator(), ImageGeneratorPort),
                (registry.video_generator(), VideoGeneratorPort),
                (registry.music_provider(), MusicProviderPort),
                (registry.keyer(), KeyerPort),
                (registry.compositor(), CompositorPort),
                (registry.release_layout(), ReleaseLayoutPort),
                (registry.publish_asset(), PublishAssetPort),
                (registry.product_package(), ProductPackagePort),
            )
            for adapter, protocol in protocol_bindings:
                self.assertIsInstance(adapter, protocol)
                self.assertTrue(adapter.capabilities.deterministic)
                self.assertFalse(adapter.capabilities.external)
                self.assertFalse(adapter.capabilities.paid)

            external_calls: list[str] = []

            def forbidden_external(*_args, **_kwargs):
                external_calls.append("called")
                raise AssertionError("M3 offline smoke reached a real external executor")

            with patch("subprocess.run", side_effect=forbidden_external), patch(
                "subprocess.Popen", side_effect=forbidden_external
            ):
                semantic_request = semantics_request(root)
                semantic_result = registry.story_semantics().analyze(semantic_request)
                self.assertTrue(semantic_result.success)

                design_request = visual_request(project, semantic_result.source_sha256)
                design_result = registry.visual_design().resolve(design_request)
                self.assertTrue(design_result.success)
                projection = root / "approved_projection.json"
                projection.write_text(
                    json.dumps(design_result.approved_projection, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

                image_handoff = root / "image_handoff.md"
                image_handoff.write_text("offline image fixture\n", encoding="utf-8")
                image_target = root / "fixtures" / "scene_01.png"
                image_result = registry.image_generator().execute(
                    ImageGeneratorRequest(
                        "scene-01",
                        "generate_story_images",
                        image_handoff,
                        file_sha256(image_handoff),
                        (
                            _artifact("approved_visual_projection", projection),
                            _artifact("story_semantics", semantic_request.source_path),
                        ),
                        (image_target,),
                        "m3-smoke-image",
                    ),
                    executor=forbidden_external,
                )
                self.assertTrue(image_result.success)
                self.assertFalse(image_result.production_eligible)

                video_target = root / "fixtures" / "scene_01.mp4"
                prompt = "deterministic offline motion"
                video_result = registry.video_generator().generate(
                    VideoGeneratorRequest(
                        "scene-video-01",
                        "scene-01",
                        image_target,
                        file_sha256(image_target),
                        prompt,
                        hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                        1.0,
                        "16:9",
                        "720p",
                        video_target,
                        "m3-smoke-video",
                        story_contract_sha256=design_result.story_contract_sha256,
                    )
                )
                self.assertTrue(video_result.success)
                self.assertEqual(video_result.source_image_sha256, file_sha256(image_target))

                music_handoff = root / "music_handoff.md"
                music_handoff.write_text("offline music fixture\n", encoding="utf-8")
                music_target = root / "fixtures" / "music.mp3"
                music_result = registry.music_provider().execute(
                    MusicProviderRequest(
                        "music-01",
                        "generate_music_segments",
                        music_handoff,
                        file_sha256(music_handoff),
                        (_artifact("approved_video_fixture", video_target),),
                        (music_target,),
                        "m3-smoke-music",
                    ),
                    executor=forbidden_external,
                )
                self.assertTrue(music_result.success)
                self.assertFalse(music_result.production_eligible)

                keyed_target = root / "fixtures" / "keyed.mp4"
                keyer_result = registry.keyer().render(
                    KeyerRequest(
                        "keyed-presenter",
                        video_target,
                        file_sha256(video_target),
                        {"keyer": "colorkey", "chroma_color": "0x00FF00"},
                        keyed_target,
                        "m3-smoke-keyer",
                    )
                )
                self.assertTrue(keyer_result.success)
                self.assertEqual(keyer_result.source_sha256, file_sha256(video_target))

                formal_composite = root / "formal" / "composite.mp4"
                compositor_result = registry.compositor().execute(
                    CompositorRequest(
                        "composite-01",
                        "background_story",
                        (_artifact("keyed_presenter", keyed_target), _artifact("music", music_target)),
                        (formal_composite,),
                        {"timeline": "caller-owned"},
                        "m3-smoke-compositor",
                    ),
                    executor=forbidden_external,
                )
                self.assertTrue(compositor_result.success)
                self.assertFalse(compositor_result.production_eligible)
                composite_fixture = Path(str(compositor_result.output_artifacts[0]["path"]))

                formal_release = root / "formal" / "release.mp4"
                release_result = registry.release_layout().execute(
                    ReleaseLayoutRequest(
                        "release-01",
                        "preview_main",
                        (_artifact("composite", composite_fixture),),
                        {"geometry": "caller-owned"},
                        formal_release,
                        "m3-smoke-release",
                    ),
                    executor=forbidden_external,
                )
                self.assertTrue(release_result.success)
                self.assertFalse(release_result.production_eligible)
                release_fixture = Path(str(release_result.output_artifact["path"]))

                formal_cover = root / "formal" / "cover.png"
                publish_result = registry.publish_asset().execute(
                    PublishAssetRequest(
                        "publish-01",
                        "final_cover_render",
                        (_artifact("release_preview", release_fixture),),
                        {"branding": "caller-owned"},
                        (formal_cover,),
                        "m3-smoke-publish",
                    ),
                    executor=forbidden_external,
                )
                self.assertTrue(publish_result.success)
                self.assertFalse(publish_result.production_eligible)
                publish_fixture = Path(str(publish_result.output_artifacts[0]["path"]))

                product_root = root / "product"
                formal_product = product_root / "base" / "cover.png"
                product_result = registry.product_package().execute(
                    ProductPackageRequest(
                        "product-01",
                        "copy_product_packages",
                        "base_and_advanced",
                        product_root,
                        ({
                            "package_variant": "base",
                            "source_path": str(publish_fixture),
                            "source_sha256": file_sha256(publish_fixture),
                            "destination_path": str(formal_product),
                        },),
                        (formal_product,),
                        "m3-smoke-product",
                    ),
                    executor=forbidden_external,
                )
                self.assertTrue(product_result.success)
                self.assertFalse(product_result.production_eligible)

            self.assertEqual(external_calls, [])
            self.assertEqual(file_sha256(currentness_sentinel), sentinel_sha)
            self.assertFalse(formal_composite.exists())
            self.assertFalse(formal_release.exists())
            self.assertFalse(formal_cover.exists())
            self.assertFalse(formal_product.exists())
            self.assertTrue(all(
                artifact["production_eligible"] is False
                for artifact in product_result.output_artifacts
            ))

    def test_production_default_is_exactly_the_ten_current_adapters(self) -> None:
        expected = {
            "story_semantics": (ExistingStorySemanticsAdapter, STORY_SEMANTICS_PORT_VERSION),
            "visual_design": (ApprovedStoryContractVisualDesignAdapter, VISUAL_DESIGN_PORT_VERSION),
            "image_generator": (CodexImageGeneratorAdapter, IMAGE_GENERATOR_PORT_VERSION),
            "video_generator": (ExistingVideoGeneratorAdapter, VIDEO_GENERATOR_PORT_VERSION),
            "music_provider": (SunoMusicProviderAdapter, MUSIC_PROVIDER_PORT_VERSION),
            "keyer": (ProductionKeyerAdapter, KEYER_PORT_VERSION),
            "compositor": (LocalCompositorAdapter, COMPOSITOR_PORT_VERSION),
            "release_layout": (LocalReleaseLayoutAdapter, RELEASE_LAYOUT_PORT_VERSION),
            "publish_asset": (LocalPublishAssetAdapter, PUBLISH_ASSET_PORT_VERSION),
            "product_package": (LocalProductPackageAdapter, PRODUCT_PACKAGE_PORT_VERSION),
        }
        with patch.dict(os.environ, {}, clear=True):
            registry = build_registry_for_profile("production-default")
        descriptions = registry.list_descriptions()
        self.assertEqual(set(descriptions), set(expected))
        for port_name, (adapter_type, port_version) in expected.items():
            adapter = registry.get(port_name)
            self.assertIsInstance(adapter, adapter_type)
            self.assertEqual(adapter.identity.port_name, port_name)
            self.assertEqual(adapter.identity.port_version, port_version)
        self.assertEqual(registry.video_generator().capabilities.provider, "toapis_grok")
        self.assertEqual(registry.video_generator().capabilities.model_or_tool, "grok-video-1.5")

    def test_mock_profile_and_execution_mode_survive_subprocess_or_fail_closed(self) -> None:
        env = os.environ.copy()
        env.update({
            MODULE_PROFILE_ENV: "mock-image",
            MODULE_PROFILE_REQUIRED_ENV: "mock-image",
            MODULE_EXECUTION_MODE_ENV: "test",
            MODULE_EXECUTION_MODE_REQUIRED_ENV: "test",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        command = [
            sys.executable,
            str(ROOT / "story_module_registry.py"),
            "--profile",
            "mock-image",
            "--execution-mode",
            "test",
            "describe",
            "image_generator",
        ]
        selected = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, check=False)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        payload = json.loads(selected.stdout)
        self.assertEqual(payload["identity"]["adapter_name"], "mock-image")
        self.assertFalse(payload["capabilities"]["external"])
        self.assertFalse(payload["capabilities"]["paid"])

        env.pop(MODULE_PROFILE_REQUIRED_ENV)
        rejected = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("required module profile", rejected.stderr + rejected.stdout)

    def test_m3_keeps_schema_v1_and_the_38_stage_dag(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/module_ports/v1/module_ports.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(schema["$id"], "story-module-ports/v1")
        self.assertEqual(
            schema["properties"]["kind"]["enum"],
            [
                "identity", "capabilities", "failure", "usage_event",
                "video_request", "video_result", "keyer_request", "keyer_result",
            ],
        )
        independent_schemas = {
            "story_semantics_port.schema.json": STORY_SEMANTICS_PORT_VERSION,
            "visual_design_port.schema.json": VISUAL_DESIGN_PORT_VERSION,
            "image_generator_port.schema.json": IMAGE_GENERATOR_PORT_VERSION,
            "music_provider_port.schema.json": MUSIC_PROVIDER_PORT_VERSION,
            "product_package_port.schema.json": PRODUCT_PACKAGE_PORT_VERSION,
            "compositor_port.schema.json": COMPOSITOR_PORT_VERSION,
            "release_layout_port.schema.json": RELEASE_LAYOUT_PORT_VERSION,
            "publish_asset_port.schema.json": PUBLISH_ASSET_PORT_VERSION,
        }
        for filename, version in independent_schemas.items():
            payload = json.loads((ROOT / "schemas/module_ports/v1" / filename).read_text(encoding="utf-8"))
            self.assertEqual(payload["$id"], version)

        self.assertEqual(len(STORY_STAGE_SEQUENCE), 38)
        self.assertEqual(len(set(STORY_STAGE_SEQUENCE)), 38)
        positions = {stage: index for index, stage in enumerate(STORY_STAGE_SEQUENCE)}
        self.assertEqual(set(STORY_STAGE_DEPENDENCIES), set(STORY_STAGE_SEQUENCE))
        for stage, dependencies in STORY_STAGE_DEPENDENCIES.items():
            self.assertEqual(len(dependencies), len(set(dependencies)))
            for dependency in dependencies:
                self.assertIn(dependency, positions)
                self.assertLess(positions[dependency], positions[stage])
        self.assertIn("product_package_review", STORY_STAGE_DEPENDENCIES["release_preview"])
        self.assertLess(positions["product_package_review"], positions["release_preview"])


if __name__ == "__main__":
    unittest.main()
