from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import prepare_suno_music_request
from release_video import ReleaseConfig, apply_release_contract_spec, build_release_render_manifest
from story_agent import StageResult
from story_agent_runtime import file_sha256
from story_contract_consumers import (
    BINDING_FIELDS,
    compile_cover_spec,
    compile_demo_render_spec,
    compile_product_content_spec,
    compile_release_render_spec,
    demo_logo_arguments,
    semantic_line_indices,
)
from demo_quality import load_demo_brand_spec
from story_contract_runtime import contract_consumer_path, write_contract_consumer_context
from story_project import apply_fixed_cover_branding, project_paths
from tests.test_publish_qa import make_publish_fixture, make_required_cover_fixture
from tests.test_story_contract_runtime import _lock_contract, _new_project
from tests.test_artifact_semantic_plan import _contract as semantic_contract_fixture
from artifact_semantic_plan import load_current_artifact_semantic_plan, plan_binding, write_artifact_semantic_plan


def _context(path: Path, consumer: str, projection: dict) -> dict:
    payload = {
        "version": 1,
        "consumer": consumer,
        "contract_schema_version": "1.0.0",
        "story_contract_sha256": "a" * 64,
        "story_contract_dependency_sha256": "b" * 64,
        "contract_projection": projection,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


class StoryContractConsumerTests(unittest.TestCase):
    def test_demo_spec_uses_one_reviewed_official_logo_and_layout_region(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logo = root / "official.png"
            Image.new("RGBA", (300, 100), (250, 180, 30, 255)).save(logo)
            projection = {
                "brand": {"assets": [{
                    "asset_id": "official", "sha256": file_sha256(logo),
                    "allowed_uses": ["demo", "release_video"], "max_per_frame": 1,
                }], "rules": []},
                "release_layout": {"rules": [], "variants": [{
                    "variant_id": "main", "aspect_ratio": "16:9", "regions": [{
                        "role": "logo", "x": .02, "y": .03, "width": .12, "height": .08,
                    }],
                }]},
            }
            context = root / "release.json"
            _context(context, "release_video", projection)
            spec_path = compile_demo_render_spec(context, root / "demo.compiled.json", official_logo_path=logo)
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            arguments = demo_logo_arguments(spec, 1920, 1080)
            self.assertEqual(spec["official_logo_count"], 1)
            self.assertEqual(arguments["logo_sha256"], file_sha256(logo))
            self.assertEqual((arguments["logo_x"], arguments["logo_y"]), (38, 32))
            loaded, loaded_arguments = load_demo_brand_spec(spec_path)
            self.assertEqual(loaded["contract_projection_sha256"], spec["contract_projection_sha256"])
            self.assertEqual(loaded_arguments["logo_sha256"], file_sha256(logo))
            context.write_bytes(context.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "source contract context is stale"):
                load_demo_brand_spec(spec_path)
            other = root / "historical.png"
            Image.new("RGBA", (300, 100), (0, 0, 0, 255)).save(other)
            with self.assertRaisesRegex(ValueError, "official asset"):
                compile_demo_render_spec(context, root / "bad.json", official_logo_path=other)

    def test_storyboard_handoff_carries_five_sections_and_plan_is_projection_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            agent, _paths = _lock_contract(project, manifest, contract_payload=semantic_contract_fixture(project, manifest, ("title", "story_body")))
            semantic_source = Path(manifest["inputs"]["story_text"])
            semantic_path = write_artifact_semantic_plan(project, semantic_source)
            semantic_binding = plan_binding(semantic_path, load_current_artifact_semantic_plan(project, semantic_source))
            context_path = write_contract_consumer_context(project, "storyboard_images")
            expected = json.loads(context_path.read_text(encoding="utf-8"))
            projection = expected["contract_projection"]
            self.assertEqual(
                set(projection),
                {"semantic_artifacts", "visual_style", "characters", "world_scale", "story_state"},
            )
            staging = Path(directory) / "staging"
            (staging / "images").mkdir(parents=True)
            storyboard = staging / "generic-contract_storyboard_lines.txt"
            storyboard.write_text("主角出发。\n", encoding="utf-8")
            prompt = agent._story_images_batch_prompt(
                handoff=staging / "handoff.md", staging_images=staging / "images",
                staging_storyboard=storyboard, story_lines=["主角出发。"], indices=[1],
            )
            for section in projection:
                self.assertIn(f'"{section}"', prompt)
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
            plan = staging / "generic-contract_storyboard_plan.json"
            sample_binding = {
                "visual_sample_plan_sha256": "c" * 64,
                "visual_sample_schema_version": "1.0",
                "visual_sample_dependency_sha256": "d" * 64,
                "visual_sample_review_bundle_sha256": "e" * 64,
            }
            plan.write_text(json.dumps({**{field: expected[field] for field in BINDING_FIELDS}, **semantic_binding, **sample_binding, "contract_projection": projection, "shots": [shot]}, ensure_ascii=False), encoding="utf-8")
            with patch("story_agent.visual_sample_lock_is_current", return_value=True), patch(
                "story_agent.visual_sample_binding", return_value=sample_binding
            ):
                self.assertTrue(agent._storyboard_plan_valid(plan, storyboard))
            changed = json.loads(context_path.read_text(encoding="utf-8"))
            changed["contract_projection"]["visual_style"]["rules"] = [{"value": "changed"}]
            context_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
            with patch("story_agent.visual_sample_lock_is_current", return_value=True), patch(
                "story_agent.visual_sample_binding", return_value=sample_binding
            ):
                self.assertFalse(agent._storyboard_plan_valid(plan, storyboard))

    def test_storyagent_music_chain_passes_projection_and_requires_current_plan_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, manifest = _new_project(Path(directory))
            agent, _paths = _lock_contract(project, manifest)

            def workflow(command, _label):
                self.assertIn("--story-contract-context", command)
                argv = ["prepare_suno_music_request.py", *command[1:]]
                with patch.object(sys, "argv", argv), patch.object(prepare_suno_music_request, "probe_duration", return_value=12.0):
                    prepare_suno_music_request.main()
                return StageResult("done", "ok")

            with patch.object(agent, "_workflow", side_effect=workflow):
                self.assertEqual(agent._stage_music_request(manifest).status, "done")
            request = (agent._music_dir() / "generic-contract_suno_music_request.md").read_text(encoding="utf-8")
            self.assertIn("semantic_artifacts", request)
            self.assertIn("story_state", request)
            expected = json.loads(contract_consumer_path(project, "music").read_text(encoding="utf-8"))
            plan = agent._music_plan()
            with plan.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["segment", *BINDING_FIELDS])
                writer.writeheader()
                writer.writerow({"segment": "opening", **{field: expected[field] for field in BINDING_FIELDS}})
            self.assertTrue(agent._music_plan_contract_bound(manifest))
            expected["story_contract_dependency_sha256"] = "c" * 64
            contract_consumer_path(project, "music").write_text(json.dumps(expected), encoding="utf-8")
            self.assertFalse(agent._music_plan_contract_bound(manifest))

    def test_cover_compiled_spec_drives_official_logo_and_layout_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：封面合同"
            logo, spec = make_required_cover_fixture(project, root)
            with patch("story_project.load_config", return_value={"brand_assets": {"logo": str(logo)}}):
                apply_fixed_cover_branding(project, contract_spec=spec)
            manifest = json.loads((project_paths(project).publish / "publish_asset_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["logo_sha256"], file_sha256(logo))
            self.assertTrue(all(item["official_logo_count"] == 1 for item in manifest["covers"].values()))
            self.assertTrue(all(item["title_safe_region"] for item in manifest["covers"].values()))
            compiled = json.loads(spec.read_text())
            self.assertIn("semantic_artifacts", compiled)
            self.assertIn("visual_style", compiled)
            self.assertIn("characters", compiled)
            for field in BINDING_FIELDS:
                self.assertEqual(manifest[field], json.loads(spec.read_text())[field])

    def test_release_compiled_spec_changes_ffmpeg_geometry_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projection = {"brand": {"assets": [], "rules": []}, "release_layout": {"rules": [], "variants": [{
                "variant_id": "main", "aspect_ratio": "16:9", "regions": [
                    {"role": "person", "x": .6, "y": .1, "width": .3, "height": .8},
                    {"role": "story_media", "x": .05, "y": .2, "width": .5, "height": .5},
                    {"role": "logo", "x": .02, "y": .03, "width": .1, "height": .08},
                    {"role": "title_safe", "x": .2, "y": .02, "width": .6, "height": .1},
                    {"role": "subtitle_safe", "x": .2, "y": .85, "width": .6, "height": .1},
                ]
            }]}}
            context = root / "release.json"
            _context(context, "release_video", projection)
            spec_path = compile_release_render_spec(context, root / "release.compiled.json")
            spec = json.loads(spec_path.read_text())
            base = ReleaseConfig(
                story_name="x", duration_text="1秒", bg_video=root/"bg.mp4", output_dir=root,
                variant="main", bg_image=None, person_greenscreen=None, audio_mix=None,
                watermark_logo=None, antipiracy_logo=None, plate_image=None, video_box=(0,0,1,1),
                watermark_width=1, watermark_opacity=1, watermark_speed=1, frame_image=None,
                story_box=(0,0,1,1), story_bleed=0, background_blur=0, frame_image_b=None,
                b_story_box=(0,0,1,1), b_windows=(), c_windows=(), story_logo=None,
                story_logo_width_a=1, story_logo_width_b=1, story_logo_x=0, story_logo_y=0,
                subtitle_srt=None, subtitle_font_size=1, subtitle_margin_v=0, mix_bg_audio=False,
                voice_volume=1, bg_audio_volume=0, person_height=1, person_x=0, person_y=0,
                chroma_color="0x00FF00", chroma_similarity=.1, chroma_blend=.1, keyer="colorkey",
                person_crop=None, detected_person_bbox=None, person_grade="none", person_beauty="none",
                library_watermark_text="", tail_seconds=0, tail_notice_text="", crf=17, preset="medium", output_scale=1,
            )
            compiled = apply_release_contract_spec(base, spec)
            # Contract person regions are safe regions, not a second
            # fit-to-box instruction.  Approved Demo/source-native presenter
            # geometry is compiled later by release_geometry.py.
            self.assertEqual((compiled.person_x, compiled.person_y, compiled.person_height), (0, 0, 1))
            self.assertEqual(compiled.story_box, (96, 216, 960, 540))
            self.assertEqual((compiled.story_logo_x, compiled.story_logo_y, compiled.story_logo_width_a), (38, 32, 192))
            self.assertEqual(compiled.subtitle_margin_v, 54)
            geometry = {
                "schema_version": "story-release-geometry/v1",
                "geometry_sha256": "d" * 64,
                "bindings": {"compiled_release_spec_sha256": "e" * 64},
                "presenter": {"a": {"source_native": True, "scale_changed": False}},
            }
            manifest = build_release_render_manifest(compiled, spec, [], geometry)
            self.assertIn("title_safe", manifest["safe_regions"])
            self.assertTrue(manifest["actual_geometry"]["presenter"]["a"]["source_native"])
            for field in BINDING_FIELDS:
                self.assertEqual(manifest[field], spec[field])

    def test_product_semantic_selector_is_deterministic_and_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projection = {"semantic_artifacts": {"rules": [], "mappings": [
                {"semantic_kind": "story_body", "artifact": artifact, "action": "include"}
                for artifact in ("ppt", "customer_manuscript", "reading_annotation", "demo")
            ]}}
            context = root / "product.json"
            _context(context, "product_package", projection)
            spec_path = compile_product_content_spec(context, root / "product.compiled.json")
            spec = json.loads(spec_path.read_text())
            lines = ["大家好", "今天讲一个故事", "小鸟飞进森林。", "小鸟找到了家。", "小朋友们，再见"]
            for artifact in ("ppt", "customer_manuscript", "reading_annotation", "demo"):
                self.assertEqual(semantic_line_indices(lines, spec, artifact), [2, 3])
            for field in BINDING_FIELDS:
                self.assertEqual(spec[field], json.loads(context.read_text())[field])


if __name__ == "__main__":
    unittest.main()
