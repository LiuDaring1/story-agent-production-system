from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from story_run import (
    CODEX_NATIVE_REQUIRED_ARTIFACTS,
    PACKAGE_NAMES,
    file_sha256,
    finalize_run,
    init_run,
    load_run,
    record_run,
    status_summary,
    validate_theme_assets_manifest,
)


class StoryRunLedgerTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path, Path, Path, Path]:
        project = root / "故事项目"
        text = root / "confirmed.txt"
        video = root / "restored.mp4"
        audio = root / "narration.wav"
        run_file = project / "99_项目状态" / "story_run.json"
        text.write_text("确认正文，不允许系统改写。\n", encoding="utf-8")
        video.write_bytes(b"restored-video")
        audio.write_bytes(b"authoritative-audio")
        return project, text, video, audio, run_file

    def record_finalize_profile(
        self,
        root: Path,
        run_file: Path,
        *,
        skip: set[str] | None = None,
    ) -> None:
        existing = load_run(run_file)["artifacts"]
        with (
            patch("story_run.validate_compile_receipt", return_value={"shot_count": 1}),
            patch("story_run.validate_delivery_receipt", return_value={}),
            patch("story_run.validate_theme_assets_manifest", return_value={}),
        ):
            for artifact_id in CODEX_NATIVE_REQUIRED_ARTIFACTS:
                if artifact_id in existing or artifact_id in (skip or set()):
                    continue
                artifact = root / f"{artifact_id}.json"
                artifact.write_text(f'{{"artifact_id": "{artifact_id}"}}\n', encoding="utf-8")
                record_run(
                    run_file=run_file,
                    package="delivery",
                    status="done",
                    artifact_id=artifact_id,
                    artifact_path=artifact,
                )

    def test_init_creates_only_six_coarse_packages_and_input_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, text, video, audio, run_file = self.fixture(Path(directory))
            payload = init_run(
                run_file=run_file,
                confirmed_text=text,
                greenscreen_video=video,
                audio=audio,
                project_dir=project,
            )
            self.assertEqual(set(payload["work_packages"]), set(PACKAGE_NAMES))
            self.assertTrue(all(item["status"] == "pending" for item in payload["work_packages"].values()))
            self.assertEqual(len(payload["inputs"]["confirmed_text"]["sha256"]), 64)
            self.assertNotIn("stages", payload)
            self.assertNotIn("attempts", payload)

    def test_record_is_idempotent_and_requires_replace_for_hash_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, text, video, audio, run_file = self.fixture(root)
            init_run(run_file=run_file, confirmed_text=text, greenscreen_video=video, audio=audio, project_dir=project)
            artifact = root / "story_r2v_plan.json"
            artifact.write_text('{"version": 1}\n', encoding="utf-8")
            first = record_run(
                run_file=run_file,
                package="director_plan",
                status="done",
                artifact_id="story_r2v_plan",
                artifact_path=artifact,
                input_hashes={"text": load_run(run_file)["inputs"]["confirmed_text"]["sha256"]},
                paid_amount=1.25,
            )
            second = record_run(
                run_file=run_file,
                package="director_plan",
                status="done",
                artifact_id="story_r2v_plan",
                artifact_path=artifact,
            )
            self.assertEqual(first["artifacts"]["story_r2v_plan"]["sha256"], second["artifacts"]["story_r2v_plan"]["sha256"])
            self.assertEqual(second["paid_total"], 1.25)
            artifact.write_text('{"version": 2}\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "--replace"):
                record_run(
                    run_file=run_file,
                    package="director_plan",
                    status="done",
                    artifact_id="story_r2v_plan",
                    artifact_path=artifact,
                )

    def test_hard_budget_blocks_record_and_status_warns_at_soft_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, text, video, audio, run_file = self.fixture(Path(directory))
            init_run(
                run_file=run_file,
                confirmed_text=text,
                greenscreen_video=video,
                audio=audio,
                project_dir=project,
                soft_budget=2,
                hard_budget=3,
            )
            record_run(run_file=run_file, package="music", status="running", paid_amount=2)
            summary = status_summary(load_run(run_file))
            self.assertTrue(summary["soft_budget_warning"])
            self.assertTrue(summary["can_start_paid_work"])
            with self.assertRaisesRegex(RuntimeError, "硬预算"):
                record_run(run_file=run_file, package="music", status="done", paid_amount=1.01)

    def test_finalize_rechecks_hashes_and_all_packages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, text, video, audio, run_file = self.fixture(root)
            init_run(run_file=run_file, confirmed_text=text, greenscreen_video=video, audio=audio, project_dir=project)
            artifact = root / "delivery.txt"
            artifact.write_text("ready\n", encoding="utf-8")
            for package in PACKAGE_NAMES:
                record_run(run_file=run_file, package=package, status="done")
            record_run(
                run_file=run_file,
                package="delivery",
                status="done",
                artifact_id="delivery_manifest",
                artifact_path=artifact,
            )
            self.record_finalize_profile(root, run_file)
            compile_sha = load_run(run_file)["artifacts"]["shot_storyboard_compile_receipt"]["sha256"]
            with (
                patch("story_run.validate_compile_receipt", return_value={"shot_count": 1}),
                patch(
                    "story_run.validate_delivery_receipt",
                    return_value={"shot_storyboard_compile_receipt_sha256": compile_sha},
                ),
                patch("story_run.validate_theme_assets_manifest", return_value={}),
            ):
                payload = finalize_run(run_file=run_file, required_artifacts=["delivery_manifest"])
            self.assertTrue(payload["finalized_at"])
            artifact.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "哈希漂移"):
                finalize_run(run_file=run_file, required_artifacts=["delivery_manifest"])

    def test_blocked_state_requires_reason_and_stays_compact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, text, video, audio, run_file = self.fixture(Path(directory))
            init_run(run_file=run_file, confirmed_text=text, greenscreen_video=video, audio=audio, project_dir=project)
            with self.assertRaisesRegex(ValueError, "--blocker"):
                record_run(run_file=run_file, package="music", status="blocked")
            record_run(run_file=run_file, package="music", status="blocked", blocker="Suno 登录失效")
            serialized = run_file.read_text(encoding="utf-8")
            self.assertLess(len(serialized.encode("utf-8")), 8000)
            self.assertEqual(json.loads(serialized)["blocker"], "Suno 登录失效")

    def test_theme_manifest_requires_imagegen_raster_lineage_and_rejects_svg(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, text, video, audio, run_file = self.fixture(root)
            init_run(run_file=run_file, confirmed_text=text, greenscreen_video=video, audio=audio, project_dir=project)
            background = root / "background.png"
            frame_source = root / "frame-source.png"
            frame_png = root / "frame.png"
            for path, content in (
                (background, b"imagegen-background"),
                (frame_source, b"imagegen-frame-source"),
                (frame_png, b"transparent-frame"),
            ):
                path.write_bytes(content)
            manifest = root / "theme_assets_manifest.json"
            payload = {
                "schema_version": "story-theme-assets-lightweight/v2",
                "story_name": "通用故事",
                "artifacts": {
                    "main_background_16x9": {
                        "path": str(background),
                        "sha256": file_sha256(background),
                        "generation_method": "imagegen_raster",
                    },
                    "story_frame_source": {
                        "path": str(frame_source),
                        "sha256": file_sha256(frame_source),
                        "generation_method": "imagegen_reference_edit",
                    },
                    "story_frame_png": {
                        "path": str(frame_png),
                        "sha256": file_sha256(frame_png),
                        "derived_from": ["story_frame_source"],
                        "transparency_method": "raster_alpha_postprocess",
                        "has_true_alpha": True,
                    },
                },
            }
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            recorded = record_run(
                run_file=run_file,
                package="product_assets",
                status="done",
                artifact_id="theme_assets_manifest",
                artifact_path=manifest,
            )
            self.assertIn("theme_assets_manifest", recorded["artifacts"])

            svg = root / "frame.svg"
            svg.write_text("<svg/>", encoding="utf-8")
            payload["artifacts"]["story_frame_svg"] = {
                "path": str(svg),
                "sha256": file_sha256(svg),
            }
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "禁止 SVG"):
                record_run(
                    run_file=run_file,
                    package="product_assets",
                    status="done",
                    artifact_id="theme_assets_manifest",
                    artifact_path=manifest,
                    replace=True,
                )

    def write_v3_theme_manifest(self, root: Path) -> Path:
        reference = root / "frame-reference.png"
        environment = root / "environment.png"
        frame_source = root / "frame-magenta.png"
        background = root / "background.png"
        frame_png = root / "frame-alpha.png"
        evidence = root / "frame-preview.png"
        for path in (reference, environment, frame_source, background, frame_png, evidence):
            path.write_bytes(path.name.encode("utf-8"))
        manifest = root / "theme_assets_v3.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": "story-theme-assets-lightweight/v3",
                    "story_name": "通用故事",
                    "frame_reference": {
                        "path": str(reference),
                        "sha256": file_sha256(reference),
                        "role": "geometry_only_not_theme_or_ornament",
                        "locked_properties": [
                            "screen_placement",
                            "large_16_9_aperture",
                            "continuous_practical_border_thickness",
                        ],
                    },
                    "sources": {
                        "environment": {
                            "path": str(environment),
                            "sha256": file_sha256(environment),
                        },
                        "story_frame_magenta": {
                            "path": str(frame_source),
                            "sha256": file_sha256(frame_source),
                            "method": "imagegen_reference_edit",
                            "background": "#FF00FF",
                            "checkerboard": False,
                        },
                    },
                    "artifacts": {
                        "main_background_16x9": {
                            "path": str(background),
                            "sha256": file_sha256(background),
                            "method": "imagegen_reference_edit",
                        },
                        "story_frame_png": {
                            "path": str(frame_png),
                            "sha256": file_sha256(frame_png),
                            "method": "connected_magenta_raster_alpha_postprocess",
                            "derived_from": ["story_frame_magenta"],
                            "has_true_alpha": True,
                        },
                    },
                    "frame_design_review": {
                        "passed": True,
                        "current_story_redesign": True,
                        "no_reference_theme_leak": True,
                        "solid_magenta_source": True,
                        "continuous_opaque_four_sides": True,
                        "inner_masking_lip": True,
                        "evidence": str(evidence),
                    },
                    "background_clean_review": {
                        "passed": True,
                        "not_preblurred": True,
                        "controlled_high_frequency_detail": True,
                        "no_text_logo_or_vignette": True,
                    },
                    "svg_used": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return manifest

    def test_v3_theme_manifest_enforces_geometry_magenta_and_clean_background(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.write_v3_theme_manifest(root)
            payload = validate_theme_assets_manifest(manifest, require_v3=True)
            self.assertEqual(payload["schema_version"], "story-theme-assets-lightweight/v3")
            payload["sources"]["story_frame_magenta"]["checkerboard"] = True
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "棋盘格"):
                validate_theme_assets_manifest(manifest, require_v3=True)

    def test_new_storyboard_pipeline_requires_bound_static_ppt_delivery_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, text, video, audio, run_file = self.fixture(root)
            init_run(run_file=run_file, confirmed_text=text, greenscreen_video=video, audio=audio, project_dir=project)
            compile_receipt = root / "compile_receipt.json"
            compile_receipt.write_text("{}", encoding="utf-8")
            theme_manifest = self.write_v3_theme_manifest(root)
            for package in PACKAGE_NAMES:
                record_run(run_file=run_file, package=package, status="done")
            with patch("story_run.validate_compile_receipt", return_value={"shot_count": 2}):
                record_run(
                    run_file=run_file,
                    package="r2v_visuals",
                    status="done",
                    artifact_id="shot_storyboard_compile_receipt",
                    artifact_path=compile_receipt,
                )
            record_run(
                run_file=run_file,
                package="product_assets",
                status="done",
                artifact_id="theme_assets_manifest",
                artifact_path=theme_manifest,
            )
            self.record_finalize_profile(
                root,
                run_file,
                skip={"static_ppt_delivery_receipt"},
            )
            with patch("story_run.validate_compile_receipt", return_value={"shot_count": 2}):
                with self.assertRaisesRegex(RuntimeError, "static_ppt_delivery_receipt"):
                    finalize_run(run_file=run_file, required_artifacts=[])

            delivery_receipt = root / "delivery_receipt.json"
            delivery_receipt.write_text("{}", encoding="utf-8")
            bound = {
                "shot_storyboard_compile_receipt_sha256": file_sha256(compile_receipt)
            }
            with patch("story_run.validate_delivery_receipt", return_value=bound):
                record_run(
                    run_file=run_file,
                    package="product_assets",
                    status="done",
                    artifact_id="static_ppt_delivery_receipt",
                    artifact_path=delivery_receipt,
                )
            with (
                patch("story_run.validate_compile_receipt", return_value={"shot_count": 2}),
                patch("story_run.validate_delivery_receipt", return_value=bound),
            ):
                finalized = finalize_run(run_file=run_file, required_artifacts=[])
            self.assertTrue(finalized["finalized_at"])


if __name__ == "__main__":
    unittest.main()
