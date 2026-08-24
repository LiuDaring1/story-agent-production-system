from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from release_geometry import (
    RELEASE_GEOMETRY_COMPILER_VERSION,
    RELEASE_GEOMETRY_SCHEMA_VERSION,
    approved_demo_geometry,
    canonical_sha256,
    compile_demo_presenter_geometry,
    compile_text_group,
    geometry_manifest_issues,
    preview_formal_binding_sha256,
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
    content_overscan_size,
    deferred_presenter_safe_region,
    package_deterministic_preview_frame,
    resolve_library_watermark,
    render_library_preview_frame,
    render_library_window_video,
    render_main_preview_frame,
    render_main_wide,
    render_top_panel,
    validate_person_grade,
)
from story_workflow import compile_release_subtitle_srt, contract_release_brand_paths
from story_agent import StoryAgent
from story_project import build_main_package_spec, load_main_package_reference, save_json


def _config(root: Path, *, person_region_ready: bool = True) -> ReleaseConfig:
    plan = root / "project" / "99_项目状态" / "story_contract" / "artifact_semantic_plan.json"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("{}", encoding="utf-8")
    preset = root / "keying_preset.json"
    preset.write_text(json.dumps({"keyer": "colorkey"}), encoding="utf-8")
    preset.with_name("keying_preset.lock.json").write_text("{}", encoding="utf-8")
    source = root / "person.mp4"
    source.write_bytes(b"fixture-source")
    top_panel = root / "main_release_plate_top.png"
    bottom_panel = root / "main_release_plate_bottom.png"
    Image.new("RGBA", (2304, 888), (250, 240, 220, 255)).save(top_panel)
    Image.new("RGBA", (2304, 888), (235, 225, 205, 255)).save(bottom_panel)
    package_spec = root / "main_package_spec.json"
    package_receipt = root / "main_package_generation_receipt.json"
    expected_text = {
        "story_type": "儿童故事",
        "story_title": "通用故事",
        "duration": "2分30秒",
        "age_range": "3-6岁",
        "use_cases": "朗诵比赛、故事表演、少儿口才、技能比拼",
    }
    spec_payload = build_main_package_spec(
        reference=load_main_package_reference(),
        story_type=expected_text["story_type"],
        story_title=expected_text["story_title"],
        duration=expected_text["duration"],
        age_range=expected_text["age_range"],
        use_cases=expected_text["use_cases"],
        top_panel=top_panel,
        bottom_panel=bottom_panel,
    )
    save_json(package_spec, spec_payload)
    save_json(package_receipt, {
        "schema_version": "story-main-package-generation/v1",
        "attempt_count": 1,
        "imagegen_reference_attached": True,
        "reference_asset": spec_payload["reference_asset"],
        "reference_sha256": spec_payload["reference_sha256"],
        "fixed_prompt_template_sha256": spec_payload["fixed_prompt_template_sha256"],
        "ocr_validation": {"passed": True, "expected": expected_text, "observed": expected_text},
        "reference_content_leak_check": {"passed": True, "leaked_items": []},
        "outputs": {
            "top_plate": {"path": str(top_panel), "sha256": hashlib.sha256(top_panel.read_bytes()).hexdigest()},
            "bottom_plate": {"path": str(bottom_panel), "sha256": hashlib.sha256(bottom_panel.read_bytes()).hexdigest()},
        },
    })
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
        main_top_panel=top_panel, main_bottom_panel=bottom_panel,
        main_package_spec=package_spec, main_package_receipt=package_receipt,
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
        "demo_render_manifest_sha256": "4" * 64,
        "approved_demo_geometry_sha256": "5" * 64,
    }
    payload = {
        "schema_version": RELEASE_GEOMETRY_SCHEMA_VERSION,
        "compiler_version": RELEASE_GEOMETRY_COMPILER_VERSION,
        "bindings": bindings,
        "main": {},
        "library": {},
        "video_compositing": {
            "canvas_background_rect": [0, 0, 1920, 1080],
            "frame_outer_rect": [0, 0, 1100, 800],
            "frame_aperture_mask": {"mode": "natural_inner_aperture", "rect": [100, 100, 900, 600]},
            "story_video_transform": {"fit": "cover", "destination_rect": [98, 98, 904, 604]},
            "story_video_focus_point": [0.5, 0.5],
            "aperture_coverage": 1.0,
            "edge_gap_pixels": 0,
        },
    }
    payload["formal_render_binding_sha256"] = preview_formal_binding_sha256(payload)
    payload["geometry_sha256"] = canonical_sha256(payload)
    return payload


class ReleaseGeometryTests(unittest.TestCase):
    def test_main_package_age_must_be_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "age_range_user_input_required"):
                build_main_package_spec(
                    reference=load_main_package_reference(),
                    story_type="童话故事",
                    story_title="测试故事",
                    duration="2分钟",
                    age_range="",
                    use_cases="朗诵比赛",
                    top_panel=root / "top.png",
                    bottom_panel=root / "bottom.png",
                )

    def test_contract_release_generates_deterministic_library_watermark_when_logo_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            generated = root / "generated-watermark.png"
            with patch("release_video.render_watermark_png", return_value=generated) as render:
                self.assertEqual(resolve_library_watermark(config, root, {"required_v1": True}), generated)
                render.assert_called_once_with(root / "library_watermark.png", config.library_watermark_text)
                render.reset_mock()
                self.assertEqual(resolve_library_watermark(config, root, None), generated)
                render.assert_called_once_with(root / "library_watermark.png", config.library_watermark_text)

            official = root / "official-antipiracy.png"
            official.write_bytes(b"official")
            self.assertEqual(
                resolve_library_watermark(replace(config, antipiracy_logo=official), root, {"required_v1": True}),
                official,
            )

    def test_library_render_accepts_contract_release_without_watermark_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            tail = root / "tail.png"
            output = root / "library.mp4"
            with patch("release_video.probe_duration", return_value=10.0), \
                 patch("release_video.run_command") as run:
                render_library_window_video(config.bg_video, None, tail, output, config)
            command = run.call_args.args[0]
            self.assertEqual(command.count("-i"), 2)
            self.assertNotIn("[wm]", command[command.index("-filter_complex") + 1])
            self.assertIn("[1:v]scale=", command[command.index("-filter_complex") + 1])
            self.assertIn(
                "[0:v]scale=1102:620:force_original_aspect_ratio=increase,crop=1080:608",
                command[command.index("-filter_complex") + 1],
            )

    def test_release_accepts_natural_person_grade_from_keying_preset(self) -> None:
        self.assertEqual(validate_person_grade("natural"), "natural")
        with self.assertRaises(ValueError):
            validate_person_grade("cinematic")

    def test_required_package_blocks_missing_reference_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            assert config.main_package_receipt is not None
            receipt = json.loads(config.main_package_receipt.read_text(encoding="utf-8"))
            receipt["imagegen_reference_attached"] = False
            config.main_package_receipt.write_text(json.dumps(receipt), encoding="utf-8")
            plan = {"schema_version": "story-artifact-semantic-plan/v1", "story_contract_dependency_sha256": "f" * 64}
            with patch("release_video.load_current_artifact_semantic_plan", return_value=plan), patch(
                "release_video.keying_preset_lock_issues", return_value=[]
            ):
                with self.assertRaisesRegex(ValueError, "未实际附带"):
                    compile_release_geometry(config, _spec())

    def test_contract_release_omits_unregistered_and_legacy_logo_overlays(self) -> None:
        story = Path("story-logo.png")
        watermark = Path("watermark.png")
        antipiracy = Path("antipiracy.png")
        self.assertEqual(
            contract_release_brand_paths(
                {"official_assets": []}, story, watermark, antipiracy
            ),
            (None, None, None),
        )
        self.assertEqual(
            contract_release_brand_paths(
                {"official_assets": [{"asset_id": "reviewed-logo"}]},
                story,
                watermark,
                antipiracy,
            ),
            (story, None, antipiracy),
        )

    @staticmethod
    def _demo_geometry(config: ReleaseConfig, *, source_native: bool = True) -> dict:
        assert config.person_greenscreen is not None
        assert config.keying_preset_path is not None
        lock = config.keying_preset_path.with_name("keying_preset.lock.json")
        return compile_demo_presenter_geometry(
            1920, 1080, 1920, 1080,
            person_crop=None if source_native else (300, 80, 1200, 950),
            detected_bbox=(1200, 100, 500, 900) if source_native else None,
            person_height_ratio=.82,
            crop_mode="source-native" if source_native else "preset",
            crop_bottom_ratio=0.04 if not source_native else 0.0,
            vertical_alignment="bottom" if not source_native else "center",
            keying_preset_sha256=hashlib.sha256(config.keying_preset_path.read_bytes()).hexdigest(),
            keying_lock_sha256=hashlib.sha256(lock.read_bytes()).hexdigest(),
            source_greenscreen_sha256=hashlib.sha256(config.person_greenscreen.read_bytes()).hexdigest(),
            production_keying_filter_fingerprint="1" * 64,
        )

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

    def test_demo_source_native_keeps_full_frame_even_when_detection_bbox_exists(self) -> None:
        geometry = compile_demo_presenter_geometry(
            1920, 1080, 1920, 1080,
            person_crop=None,
            detected_bbox=(420, 60, 1080, 1000),
            person_height_ratio=1.0,
            crop_mode="source-native",
            crop_bottom_ratio=0.0,
            vertical_alignment="center",
            keying_preset_sha256="1" * 64,
            keying_lock_sha256="2" * 64,
            source_greenscreen_sha256="3" * 64,
            production_keying_filter_fingerprint="4" * 64,
        )
        self.assertEqual(geometry["source_crop"], [0, 0, 1920, 1080])
        self.assertEqual(geometry["rendered_width"], 1920)
        self.assertEqual(geometry["rendered_height"], 1080)

    def test_initial_anchor_centers_subject_in_right_blank_without_dynamic_repositioning(self) -> None:
        demo = approved_demo_geometry(1920, 1080, 1920, 1080, (0, 0, 1920, 1080))
        release = release_a_geometry(
            demo,
            {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            1920,
            1080,
            right_blank_region={"x": 1120 / 1920, "y": 0.0, "width": 800 / 1920, "height": 1.0},
            initial_subject_bbox=(1250, 100, 320, 900),
        )
        subject_center = release["x"] + 1250 + 160
        self.assertAlmostEqual(subject_center, 1520, delta=1)
        self.assertFalse(release["dynamic_repositioning"])
        self.assertEqual(release["gesture_overlap_policy"], "allowed")
        self.assertFalse(release["full_duration_zero_intersection_required"])

    def test_safe_presenter_requires_no_correction(self) -> None:
        demo = approved_demo_geometry(1920, 1080, 1920, 1080, (1200, 100, 500, 900))
        release = release_a_geometry(
            demo, {"x": 1200 / 1920, "y": 50 / 1080, "width": 500 / 1920, "height": 1000 / 1080},
            1920, 1080,
        )
        self.assertEqual(release["position_correction"], {"x": 0, "y": 0})
        self.assertEqual(release["correction_reason"], "none")

    def test_safe_off_center_presenter_keeps_exact_horizontal_position(self) -> None:
        for original_x in (180, 1240):
            demo = {
                **approved_demo_geometry(1920, 1080, 1920, 1080, (original_x, 100, 400, 800)),
                "x": original_x,
            }
            release = release_a_geometry(
                demo, {"x": .05, "y": .05, "width": .90, "height": .90}, 1920, 1080,
            )
            self.assertEqual(release["x"], original_x)
            self.assertEqual(release["position_correction"], {"x": 0, "y": 0})
            self.assertEqual(release["correction_reason"], "none")

    def test_left_and_right_overflow_receive_only_minimum_horizontal_correction(self) -> None:
        safe_region = {"x": 100 / 1920, "y": 0.0, "width": 1700 / 1920, "height": 1.0}
        left = {**approved_demo_geometry(1920, 1080, 1920, 1080, (80, 100, 400, 800)), "x": 80}
        corrected_left = release_a_geometry(left, safe_region, 1920, 1080)
        self.assertEqual(corrected_left["x"], 100)
        self.assertEqual(corrected_left["position_correction"], {"x": 20, "y": 0})
        self.assertFalse(corrected_left["scale_changed"])

        right = {**approved_demo_geometry(1920, 1080, 1920, 1080, (1420, 100, 400, 800)), "x": 1420}
        corrected_right = release_a_geometry(right, safe_region, 1920, 1080)
        self.assertEqual(corrected_right["x"], 1400)
        self.assertEqual(corrected_right["position_correction"], {"x": -20, "y": 0})
        self.assertFalse(corrected_right["scale_changed"])

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
            for binding_field in (
                "demo_render_manifest_sha256", "approved_demo_geometry_sha256",
            ):
                changed_demo = copy.deepcopy(geometry["bindings"])
                changed_demo[binding_field] = "9" * 64
                self.assertIn(
                    f"release_geometry_binding_mismatch:{binding_field}",
                    release_render_manifest_issues(
                        manifest, expected_bindings=changed_demo, verify_outputs=False,
                    ),
                )

    def test_compiler_records_abc_segments_and_actual_center_region(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            demo_manifest = root / "demo_render_manifest.json"
            demo_manifest.write_text("{}", encoding="utf-8")
            config = replace(config, demo_render_manifest=demo_manifest)
            demo_geometry = self._demo_geometry(config)
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
                patch("release_video.load_preview_demo_geometry_for_release_review", return_value=({}, demo_geometry)),
            ):
                geometry = compile_release_geometry(config, _spec())
            self.assertEqual(geometry["presenter"]["approved_demo_geometry"]["scale"], 1.0)
            self.assertFalse(geometry["presenter"]["a"]["scale_changed"])
            self.assertEqual(geometry["main"]["segments"]["b"]["active_time_ranges"], [[10.0, 20.0]])
            self.assertEqual(geometry["main"]["segments"]["c"]["active_time_ranges"], [[30.0, 40.0]])
            self.assertEqual(geometry["library"]["video_region"], [0, TOP_HEIGHT, FINAL_WIDTH, CENTER_HEIGHT])
            compositing = geometry["video_compositing"]
            self.assertEqual(compositing["canvas_background_rect"], [0, 0, 1920, 1080])
            self.assertEqual(compositing["frame_outer_rect"], [0, 84, 1112, 732])
            self.assertEqual(compositing["frame_aperture_mask"]["rect"], [100, 200, 900, 500])
            self.assertEqual(compositing["story_video_transform"]["fit"], "cover")
            self.assertEqual(compositing["story_video_focus_point"], [0.5, 0.5])
            presenter = geometry["presenter"]["a"]
            self.assertEqual(presenter["presenter_initial_y"], presenter["y"])
            self.assertEqual(presenter["presenter_initial_scale"], presenter["scale"])
            self.assertEqual(presenter["presenter_right_blank_region"]["x"], 1112)
            self.assertEqual(geometry_manifest_issues(geometry), [])

    def test_formal_geometry_rejects_any_post_preview_material_or_parameter_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            demo_manifest = root / "demo_render_manifest.json"
            demo_manifest.write_text("{}", encoding="utf-8")
            config = replace(config, demo_render_manifest=demo_manifest)
            demo_geometry = self._demo_geometry(config)
            plan = {
                "schema_version": "story-artifact-semantic-plan/v1",
                "story_contract_dependency_sha256": "f" * 64,
            }
            patches = (
                patch("release_video.load_current_artifact_semantic_plan", return_value=plan),
                patch("release_video.semantic_plan_binding", return_value={
                    "artifact_semantic_plan_sha256": "e" * 64,
                    "artifact_semantic_plan_schema_version": plan["schema_version"],
                    "artifact_semantic_plan_dependency_sha256": "f" * 64,
                }),
                patch("release_video.keying_preset_lock_issues", return_value=[]),
                patch("release_video.production_keying_fingerprint", return_value="1" * 64),
                patch("release_video.load_preview_demo_geometry_for_release_review", return_value=({}, demo_geometry)),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                preview = compile_release_geometry(config, _spec(), preview=True)
            approved = root / "approved-preview.json"
            approved.write_text(json.dumps(preview), encoding="utf-8")
            formal_config = replace(config, approved_preview_geometry=approved)
            with (
                patch("release_video.load_current_artifact_semantic_plan", return_value=plan),
                patch("release_video.semantic_plan_binding", return_value={
                    "artifact_semantic_plan_sha256": "e" * 64,
                    "artifact_semantic_plan_schema_version": plan["schema_version"],
                    "artifact_semantic_plan_dependency_sha256": "f" * 64,
                }),
                patch("release_video.keying_preset_lock_issues", return_value=[]),
                patch("release_video.production_keying_fingerprint", return_value="1" * 64),
                patch("release_video.load_preview_demo_geometry_for_release_review", return_value=({}, demo_geometry)),
            ):
                formal = compile_release_geometry(formal_config, _spec())
            self.assertEqual(
                formal["formal_render_binding_sha256"], preview["formal_render_binding_sha256"]
            )

            changed = replace(formal_config, story_box=(101, 200, 900, 500))
            with (
                patch("release_video.load_current_artifact_semantic_plan", return_value=plan),
                patch("release_video.semantic_plan_binding", return_value={
                    "artifact_semantic_plan_sha256": "e" * 64,
                    "artifact_semantic_plan_schema_version": plan["schema_version"],
                    "artifact_semantic_plan_dependency_sha256": "f" * 64,
                }),
                patch("release_video.keying_preset_lock_issues", return_value=[]),
                patch("release_video.production_keying_fingerprint", return_value="1" * 64),
                patch("release_video.load_preview_demo_geometry_for_release_review", return_value=({}, demo_geometry)),
            ):
                with self.assertRaisesRegex(ValueError, "preview_formal_binding_mismatch"):
                    compile_release_geometry(changed, _spec())

    def test_release_inherits_actual_non_native_demo_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            demo_manifest = root / "demo_render_manifest.json"
            demo_manifest.write_text("{}", encoding="utf-8")
            config = replace(config, demo_render_manifest=demo_manifest)
            actual = self._demo_geometry(config, source_native=False)
            plan = {"schema_version": "story-artifact-semantic-plan/v1", "story_contract_dependency_sha256": "f" * 64}
            with (
                patch("release_video.load_current_artifact_semantic_plan", return_value=plan),
                patch("release_video.semantic_plan_binding", return_value={
                    "artifact_semantic_plan_sha256": "e" * 64,
                    "artifact_semantic_plan_schema_version": plan["schema_version"],
                    "artifact_semantic_plan_dependency_sha256": "f" * 64,
                }),
                patch("release_video.keying_preset_lock_issues", return_value=[]),
                patch("release_video.production_keying_fingerprint", return_value="1" * 64),
                patch("release_video.load_preview_demo_geometry_for_release_review", return_value=({}, actual)),
            ):
                geometry = compile_release_geometry(config, _spec({
                    "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0,
                }))
            self.assertFalse(geometry["presenter"]["approved_demo_geometry"]["source_native"])
            self.assertEqual(
                geometry["presenter"]["approved_demo_geometry"]["source_crop"],
                actual["source_crop"],
            )
            self.assertEqual(geometry["presenter"]["approved_demo_geometry"]["scale"], actual["scale"])

    def test_release_defers_missing_regions_to_reviewed_demo_canvas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            demo_manifest = root / "demo_render_manifest.json"
            demo_manifest.write_text("{}", encoding="utf-8")
            config = replace(config, demo_render_manifest=demo_manifest)
            actual = self._demo_geometry(config, source_native=False)
            plan = {
                "schema_version": "story-artifact-semantic-plan/v1",
                "story_contract_dependency_sha256": "f" * 64,
            }
            spec = _spec()
            spec["variants"] = []
            spec["layout_rules"] = [
                {"rule_id": "layout.precise_variants_require_canvas_receipt", "value": True},
                {"rule_id": "layout.normalized_regions_require_canvas_receipt", "value": True},
            ]
            with (
                patch("release_video.load_current_artifact_semantic_plan", return_value=plan),
                patch("release_video.semantic_plan_binding", return_value={
                    "artifact_semantic_plan_sha256": "e" * 64,
                    "artifact_semantic_plan_schema_version": plan["schema_version"],
                    "artifact_semantic_plan_dependency_sha256": "f" * 64,
                }),
                patch("release_video.keying_preset_lock_issues", return_value=[]),
                patch("release_video.production_keying_fingerprint", return_value="1" * 64),
                patch("release_video.load_preview_demo_geometry_for_release_review", return_value=({}, actual)),
            ):
                geometry = compile_release_geometry(config, spec)
            presenter = geometry["presenter"]["a"]
            self.assertEqual(presenter["x"], actual["x"])
            self.assertEqual(presenter["y"], actual["y"])
            self.assertEqual(
                presenter["person_safe_region"],
                {"x": 0, "y": 0, "width": 1920, "height": 1080},
            )

    def test_release_preview_truthfully_marks_keying_lock_as_not_yet_applicable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            demo_manifest = root / "demo_preview_manifest.json"
            demo_manifest.write_text("{}", encoding="utf-8")
            config = replace(config, demo_render_manifest=demo_manifest)
            actual = self._demo_geometry(config, source_native=False)
            assert config.keying_preset_path is not None
            config.keying_preset_path.with_name("keying_preset.lock.json").unlink()
            actual["keying_lock_sha256"] = ""
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
                patch("release_video.production_keying_fingerprint", return_value="1" * 64),
                patch("release_video.load_preview_demo_geometry_for_release_review", return_value=({}, actual)),
            ):
                geometry = compile_release_geometry(
                    config,
                    _spec({"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}),
                    preview=True,
                )
            self.assertEqual(
                geometry["bindings"]["keying_lock_sha256"],
                "not_applicable:preview_before_independent_keying_lock",
            )
            self.assertEqual(geometry_manifest_issues(geometry), [])

    def test_release_missing_region_without_explicit_defer_still_blocks(self) -> None:
        with self.assertRaisesRegex(ValueError, "lacks presenter safe region"):
            deferred_presenter_safe_region({"variants": [], "layout_rules": []}, {
                "canvas_width": 1920,
                "canvas_height": 1080,
            })

    def test_required_release_without_reviewed_demo_preview_geometry_receipt_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            plan = {"schema_version": "story-artifact-semantic-plan/v1", "story_contract_dependency_sha256": "f" * 64}
            with (
                patch("release_video.load_current_artifact_semantic_plan", return_value=plan),
                patch("release_video.keying_preset_lock_issues", return_value=[]),
            ):
                with self.assertRaisesRegex(ValueError, "Demo 短预演几何回执"):
                    compile_release_geometry(config, _spec())

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

    def test_required_preview_samples_reuse_formal_ffmpeg_renderers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            background = root / "background.png"
            frame = root / "frame.png"
            tail = root / "tail.png"
            for path in (background, frame, tail):
                Image.new("RGBA", (1920, 1080), (20, 30, 40, 255)).save(path)
            config = replace(config, bg_image=background, frame_image=frame)
            geometry = {
                "presenter": {
                    "a": {"geometry_sha256": "a" * 64},
                    "c": {"geometry_sha256": "c" * 64},
                }
            }
            main_output = root / "main.png"
            library_output = root / "library.png"

            def write_main(*args, **kwargs):
                Path(args[2]).write_bytes(b"formal-main-sample")

            def write_library(*args, **kwargs):
                Path(kwargs["output_path"]).write_bytes(b"formal-library-sample")

            def write_vertical(*args, **kwargs):
                Path(kwargs["output_path"]).write_bytes(b"formal-vertical-sample")

            def write_frame(_video, output, _timestamp):
                size = (3840, 2160) if "sample_probe" in Path(output).name else (1080, 1440)
                Image.new("RGB", size, (80, 90, 100)).save(output)

            with (
                patch("release_video.render_main_wide", side_effect=write_main) as main_renderer,
                patch("release_video.render_library_window_video", side_effect=write_library) as library_renderer,
                patch("release_video.render_vertical_package", side_effect=write_vertical) as vertical_renderer,
                patch("release_video.probe_duration", return_value=0.4),
                patch("release_video.extract_video_frame", side_effect=write_frame),
            ):
                render_main_preview_frame(
                    config, frame, main_output, root, 12.5, "a", geometry,
                    config.main_top_panel, config.main_bottom_panel,
                )
                render_library_preview_frame(
                    config, None, tail, library_output, root, 22.0,
                    config.main_top_panel, config.main_bottom_panel,
                )

            self.assertEqual(main_renderer.call_args.kwargs["sample_start"], 12.5)
            self.assertEqual(main_renderer.call_args.kwargs["sample_scene"], "a")
            self.assertEqual(library_renderer.call_args.kwargs["sample_start"], 22.0)
            self.assertEqual(vertical_renderer.call_count, 2)
            self.assertTrue(main_output.is_file())
            self.assertTrue(library_output.is_file())

    def test_direct_bc_samples_do_not_enter_full_timeline_framesync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            background = root / "background.png"
            audio = root / "audio.m4a"
            frame = root / "frame.png"
            for path in (background, audio, frame):
                path.write_bytes(b"fixture")
            config = replace(
                config,
                bg_image=background,
                audio_mix=audio,
                frame_image=frame,
                b_windows=(),
                c_windows=(),
            )
            presenter = {
                "source_crop": [0, 0, 320, 360],
                "rendered_width": 600,
                "rendered_height": 675,
                "x": 1240,
                "y": 230,
            }
            commands: list[list[str]] = []
            with (
                patch("release_video.probe_duration", return_value=2.4),
                patch("release_video.probe_video_stream_duration", return_value=2.4),
                patch("release_video.probe_video_frame_duration", return_value=0.04),
                patch(
                    "release_video.prepare_story_frame_assets",
                    return_value=(frame, root / "mask.png", (170, 250, 990, 557)),
                ),
                patch("release_video.person_key_filters", return_value=["[2:v]null[person_keyed]"]),
                patch("release_video.run_command", side_effect=lambda command: commands.append(command)),
            ):
                render_main_wide(
                    config, frame, root / "b.mp4",
                    presenter_geometry=presenter, presenter_c_geometry=presenter,
                    sample_start=1.0, sample_duration=0.4, sample_scene="b",
                )
                render_main_wide(
                    config, frame, root / "c.mp4",
                    presenter_geometry=presenter, presenter_c_geometry=presenter,
                    sample_start=1.0, sample_duration=0.4, sample_scene="c",
                )
            for command in commands:
                self.assertIn("-filter_complex_threads", command)
                self.assertEqual(command[command.index("-i") + 1], str(background))
                graph = command[command.index("-filter_complex") + 1]
                self.assertIn("[2:v]setpts=PTS-STARTPTS[person_timeline]", graph)
                self.assertNotIn("blend=", graph)
            self.assertEqual(commands[0][commands[0].index("-map") + 1], "[b_framed]")
            self.assertEqual(commands[1][commands[1].index("-map") + 1], "[c_person]")

    def test_story_content_overscan_is_even_and_crops_encoded_rims(self) -> None:
        width, height = content_overscan_size(1080, 608)
        self.assertEqual((width % 2, height % 2), (0, 0))
        self.assertGreaterEqual(width, 1102)
        self.assertGreaterEqual(height, 620)

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

    def test_release_semantics_project_source_lines_to_split_subtitle_cues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            semantic_source = root / "story.txt"
            semantic_source.write_text(
                "标题在这里\n正文分成两个短字幕\n最后的道理\n",
                encoding="utf-8",
            )
            source = root / "all.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n标题在这里\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\n正文分成\n\n"
                "3\n00:00:02,000 --> 00:00:03,000\n两个短字幕\n\n"
                "4\n00:00:03,000 --> 00:00:04,000\n最后的道理\n",
                encoding="utf-8",
            )
            plan = {
                "semantic_source": {
                    "line_count": 3,
                    "sha256": hashlib.sha256(semantic_source.read_bytes()).hexdigest(),
                },
                "artifacts": {
                    "demo_subtitles": {"decisions": [{
                        "action": "include", "subtitle_policy": "show", "source_line_numbers": [2],
                    }]},
                },
            }
            rendered = compile_release_subtitle_srt(
                source,
                plan,
                "demo_subtitles",
                root / "main.srt",
                semantic_source=semantic_source,
            ).read_text()
            self.assertNotIn("标题在这里", rendered)
            self.assertIn("正文分成", rendered)
            self.assertIn("两个短字幕", rendered)
            self.assertNotIn("最后的道理", rendered)


if __name__ == "__main__":
    unittest.main()
