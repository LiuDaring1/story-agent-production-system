from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from release_geometry import (
    RELEASE_GEOMETRY_COMPILER_VERSION,
    RELEASE_GEOMETRY_SCHEMA_VERSION,
    approved_demo_geometry,
    canonical_sha256,
    compile_text_group,
    geometry_manifest_issues,
    release_a_geometry,
    release_render_manifest_issues,
    text_group_issues,
)
from release_video import (
    BOTTOM_HEIGHT,
    CENTER_HEIGHT,
    FINAL_HEIGHT,
    FINAL_WIDTH,
    TOP_HEIGHT,
    ReleaseConfig,
    build_release_render_manifest,
    compile_release_geometry,
    package_deterministic_preview_frame,
    render_top_panel,
)
from story_workflow import compile_release_subtitle_srt
from story_agent import StoryAgent


def _config(root: Path, *, person_region_ready: bool = True) -> ReleaseConfig:
    plan = root / "project" / "99_项目状态" / "story_contract" / "artifact_semantic_plan.json"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("{}", encoding="utf-8")
    preset = root / "keying_preset.json"
    preset.write_text(json.dumps({"keyer": "colorkey"}), encoding="utf-8")
    preset.with_name("keying_preset.lock.json").write_text("{}", encoding="utf-8")
    source = root / "person.mp4"
    source.write_bytes(b"fixture-source")
    return ReleaseConfig(
        story_name="通用故事", duration_text="2分30秒", bg_video=root / "bg.mp4", output_dir=root,
        variant="main", bg_image=None, person_greenscreen=source, audio_mix=None,
        watermark_logo=None, antipiracy_logo=None, plate_image=None,
        video_box=(0, 0, FINAL_WIDTH, CENTER_HEIGHT), watermark_width=100,
        watermark_opacity=.5, watermark_speed=1, frame_image=None,
        story_box=(100, 200, 900, 500), story_bleed=0, background_blur=0,
        frame_image_b=None, b_story_box=(220, 150, 1200, 675),
        b_windows=((10.0, 20.0),), c_windows=((30.0, 40.0),), story_logo=None,
        story_logo_width_a=180, story_logo_width_b=180, story_logo_x=30, story_logo_y=30,
        subtitle_srt=None, subtitle_font_size=42, subtitle_margin_v=60,
        mix_bg_audio=False, voice_volume=1, bg_audio_volume=0,
        person_height=940, person_x=1190, person_y=100,
        chroma_color="0x00FF00", chroma_similarity=.1, chroma_blend=.1, keyer="colorkey",
        person_crop=None, detected_person_bbox=(1200, 100, 500, 900),
        person_grade="none", person_beauty="none", library_watermark_text="watermark",
        tail_seconds=30, tail_notice_text="notice", crf=18, preset="medium", output_scale=2,
        artifact_semantic_plan=plan, keying_preset_path=preset,
    )


def _spec(person_region: dict | None = None) -> dict:
    return {
        "consumer": "release_video",
        "contract_schema_version": "1.0.0",
        "story_contract_sha256": "a" * 64,
        "story_contract_dependency_sha256": "b" * 64,
        "contract_projection_sha256": "c" * 64,
        "official_assets": [],
        "layout_rules": [],
        "variants": [{
            "variant_id": "main",
            "aspect_ratio": "16:9",
            "regions": [{"role": "person", **(person_region or {
                "x": .55, "y": .05, "width": .40, "height": .90,
            })}],
        }],
    }


def _valid_manifest() -> dict:
    bindings = {
        "story_contract_sha256": "a" * 64,
        "contract_schema_version": "1.0.0",
        "contract_projection_sha256": "b" * 64,
        "story_contract_dependency_sha256": "c" * 64,
        "release_projection_sha256": "b" * 64,
        "release_dependency_sha256": "c" * 64,
        "compiled_release_spec_sha256": "d" * 64,
        "artifact_semantic_plan_sha256": "e" * 64,
        "artifact_semantic_plan_schema_version": "story-artifact-semantic-plan/v1",
        "artifact_semantic_plan_dependency_sha256": "f" * 64,
        "production_keying_filter_fingerprint": "1" * 64,
        "keying_preset_sha256": "2" * 64,
        "keying_lock_sha256": "3" * 64,
    }
    payload = {
        "schema_version": RELEASE_GEOMETRY_SCHEMA_VERSION,
        "compiler_version": RELEASE_GEOMETRY_COMPILER_VERSION,
        "bindings": bindings,
        "main": {},
        "library": {},
    }
    payload["geometry_sha256"] = canonical_sha256(payload)
    return payload


class ReleaseGeometryTests(unittest.TestCase):
    def test_independent_review_receives_explicit_abc_and_library_previews(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory)
            preview_dir = status / "release_preview_frames"
            preview_dir.mkdir()
            names = (
                "preview_contact_sheet.png",
                "main_001s_a.png",
                "main_090s_a_gesture.png",
                "main_020s_b.png",
                "main_040s_c.png",
                "library_001s.png",
            )
            for name in names:
                (preview_dir / name).write_bytes(b"offline-preview")
            agent = StoryAgent.__new__(StoryAgent)
            agent.context = SimpleNamespace(paths=SimpleNamespace(status=status))
            selected = {path.name for path in agent._release_preview_images()}
            self.assertTrue({
                "main_001s_a.png", "main_090s_a_gesture.png",
                "main_020s_b.png", "main_040s_c.png", "library_001s.png",
            }.issubset(selected))

    def test_demo_scale_is_inherited_and_large_gesture_bbox_does_not_shrink(self) -> None:
        demo = approved_demo_geometry(1920, 1080, 1920, 1080, (100, 50, 1700, 1000))
        self.assertEqual(demo["scale"], 1.0)
        release = release_a_geometry(
            demo, {"x": 0.04, "y": 0.0, "width": .92, "height": 1.0}, 1920, 1080,
        )
        self.assertEqual(release["scale"], 1.0)
        self.assertFalse(release["scale_changed"])
        self.assertEqual(release["rendered_width"], 1700)

    def test_safe_presenter_requires_no_correction(self) -> None:
        demo = approved_demo_geometry(1920, 1080, 1920, 1080, (1200, 100, 500, 900))
        release = release_a_geometry(
            demo, {"x": 1200 / 1920, "y": 50 / 1080, "width": 500 / 1920, "height": 1000 / 1080},
            1920, 1080,
        )
        self.assertEqual(release["position_correction"], {"x": 0, "y": 0})
        self.assertEqual(release["correction_reason"], "none")

    def test_horizontal_overflow_gets_horizontal_correction_only(self) -> None:
        demo = approved_demo_geometry(1920, 1080, 1920, 1080, (1200, 100, 500, 900))
        release = release_a_geometry(
            demo, {"x": .55, "y": .05, "width": .40, "height": .90}, 1920, 1080,
        )
        self.assertEqual(release["position_correction"]["y"], 0)
        self.assertFalse(release["scale_changed"])
        self.assertEqual(release["scale"], demo["scale"])

    def test_layout_that_requires_rescale_or_vertical_change_blocks(self) -> None:
        demo = approved_demo_geometry(1920, 1080, 1920, 1080, (1200, 100, 500, 900))
        with self.assertRaisesRegex(ValueError, "rescale"):
            release_a_geometry(demo, {"x": .7, "y": .05, "width": .20, "height": .90}, 1920, 1080)
        with self.assertRaisesRegex(ValueError, "vertical"):
            release_a_geometry(demo, {"x": .6, "y": .3, "width": .35, "height": .50}, 1920, 1080)

    def test_library_text_group_is_centered_and_tampering_is_detected(self) -> None:
        region = {"x": .05, "y": (TOP_HEIGHT + CENTER_HEIGHT) / FINAL_HEIGHT, "width": .90, "height": BOTTOM_HEIGHT / FINAL_HEIGHT}
        group = compile_text_group(region, FINAL_WIDTH, FINAL_HEIGHT, [
            {"id": "one", "text": "第一层", "hierarchy": 1},
            {"id": "two", "text": "第二层", "hierarchy": 2},
        ])
        self.assertEqual(text_group_issues(group, group["center"]), [])
        changed = copy.deepcopy(group)
        changed["center"]["x"] += 100
        self.assertIn("library_text_group_center_mismatch", text_group_issues(changed, group["center"]))

    def test_geometry_manifest_binds_contract_semantics_and_keying(self) -> None:
        payload = _valid_manifest()
        self.assertEqual(geometry_manifest_issues(payload), [])
        for field in (
            "release_projection_sha256", "artifact_semantic_plan_sha256",
            "production_keying_filter_fingerprint", "keying_lock_sha256",
        ):
            changed = copy.deepcopy(payload)
            changed["bindings"][field] = "9" * 64
            self.assertIn("release_geometry_sha256_mismatch", geometry_manifest_issues(changed))
        self.assertIn(
            "release_geometry_binding_mismatch:artifact_semantic_plan_sha256",
            geometry_manifest_issues(payload, {"artifact_semantic_plan_sha256": "9" * 64}),
        )

    def test_render_receipt_binds_actual_geometry_and_output_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "release.mp4"
            output.write_bytes(b"offline-release-fixture")
            geometry = _valid_manifest()
            manifest = build_release_render_manifest(_config(root), _spec(), [output], geometry)
            self.assertEqual(
                release_render_manifest_issues(
                    manifest, expected_bindings=geometry["bindings"], verify_outputs=True,
                ),
                [],
            )
            output.write_bytes(b"tampered")
            self.assertIn(
                "release_render_output_sha256_mismatch:release.mp4",
                release_render_manifest_issues(
                    manifest, expected_bindings=geometry["bindings"], verify_outputs=True,
                ),
            )
            stale = copy.deepcopy(manifest)
            stale["actual_geometry"]["bindings"]["production_keying_filter_fingerprint"] = "9" * 64
            self.assertTrue(any(
                "production_keying_filter_fingerprint" in issue
                for issue in release_render_manifest_issues(
                    stale, expected_bindings=geometry["bindings"], verify_outputs=False,
                )
            ))

    def test_compiler_records_abc_segments_and_actual_center_region(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            plan = {
                "schema_version": "story-artifact-semantic-plan/v1",
                "story_contract_dependency_sha256": "f" * 64,
            }
            with (
                patch("release_video.load_current_artifact_semantic_plan", return_value=plan),
                patch("release_video.semantic_plan_binding", return_value={
                    "artifact_semantic_plan_sha256": "e" * 64,
                    "artifact_semantic_plan_schema_version": plan["schema_version"],
                    "artifact_semantic_plan_dependency_sha256": "f" * 64,
                }),
                patch("release_video.keying_preset_lock_issues", return_value=[]),
                patch("release_video.production_keying_fingerprint", return_value="1" * 64),
                patch("release_video.probe_video_size", return_value=(1920, 1080)),
            ):
                geometry = compile_release_geometry(config, _spec())
            self.assertEqual(geometry["presenter"]["approved_demo_geometry"]["scale"], 1.0)
            self.assertFalse(geometry["presenter"]["a"]["scale_changed"])
            self.assertEqual(geometry["main"]["segments"]["b"]["active_time_ranges"], [[10.0, 20.0]])
            self.assertEqual(geometry["main"]["segments"]["c"]["active_time_ranges"], [[30.0, 40.0]])
            self.assertEqual(geometry["library"]["video_region"], [0, TOP_HEIGHT, FINAL_WIDTH, CENTER_HEIGHT])
            self.assertEqual(geometry_manifest_issues(geometry), [])

    def test_top_strip_has_only_type_and_title_and_long_title_does_not_grow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            short = render_top_panel(root / "short.png", "故事类型", "普通标题", "", (67, 143, 62))
            long = render_top_panel(root / "long.png", "故事类型", "这是一个需要确定性缩放或换行的较长通用故事标题", "", (67, 143, 62))
            with Image.open(short) as image:
                self.assertEqual(image.size, (FINAL_WIDTH, TOP_HEIGHT))
            with Image.open(long) as image:
                self.assertEqual(image.size, (FINAL_WIDTH, TOP_HEIGHT))
            with self.assertRaisesRegex(ValueError, "无法在审核安全区内排版"):
                render_top_panel(root / "blocked.png", "故事类型", "极" * 180, "", (67, 143, 62))

    def test_required_preview_mirrors_final_deterministic_strips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            top = Image.new("RGBA", (FINAL_WIDTH, TOP_HEIGHT), (255, 0, 0, 255))
            bottom = Image.new("RGBA", (FINAL_WIDTH, BOTTOM_HEIGHT), (0, 0, 255, 255))
            top.save(root / "top.png")
            bottom.save(root / "bottom.png")
            preview = package_deterministic_preview_frame(
                Image.new("RGBA", (1920, 1080), (0, 255, 0, 255)), root / "top.png", root / "bottom.png",
            )
            self.assertEqual(preview.size, (FINAL_WIDTH, FINAL_HEIGHT))
            self.assertEqual(preview.getpixel((1, 1))[:3], (255, 0, 0))
            self.assertEqual(preview.getpixel((1, FINAL_HEIGHT - 1))[:3], (0, 0, 255))
            self.assertEqual(preview.getpixel((FINAL_WIDTH // 2, TOP_HEIGHT + 10))[:3], (0, 255, 0))

    def test_release_variants_consume_their_own_semantic_plan_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "all.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n标题\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\n正文\n\n"
                "3\n00:00:02,000 --> 00:00:03,000\n道理\n",
                encoding="utf-8",
            )
            plan = {
                "semantic_source": {"line_count": 3},
                "artifacts": {
                    "demo_subtitles": {"decisions": [{
                        "action": "include", "subtitle_policy": "show", "source_line_numbers": [1, 2],
                    }]},
                    "background_subtitles": {"decisions": [{
                        "action": "include", "subtitle_policy": "show", "source_line_numbers": [2, 3],
                    }]},
                },
            }
            main = compile_release_subtitle_srt(source, plan, "demo_subtitles", root / "main.srt").read_text()
            library = compile_release_subtitle_srt(source, plan, "background_subtitles", root / "library.srt").read_text()
            self.assertIn("标题", main)
            self.assertNotIn("道理", main)
            self.assertNotIn("标题", library)
            self.assertIn("道理", library)


if __name__ == "__main__":
    unittest.main()
