from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from story_artifact_validation import (
    INDEPENDENT_REVIEW_TARGETS,
    MACHINE_QA_ARTIFACTS,
)
from story_timeline import write_authoritative_timeline_receipt
from story_run import (
    CODEX_NATIVE_REQUIRED_ARTIFACTS,
    PACKAGE_NAMES,
    file_sha256,
    finalize_run,
    init_run,
    load_run,
    record_run,
    record_request_observation,
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
        input_dir = project / "00_输入素材"
        input_dir.mkdir(parents=True, exist_ok=True)
        (input_dir / "确认字幕.txt").write_text("确认正文，不允许系统改写。\n", encoding="utf-8")
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
        with (
            patch("story_run.validate_compile_receipt", return_value={"shot_count": 1}),
            patch("story_run.validate_delivery_receipt", return_value={}),
            patch("story_run.validate_theme_assets_manifest", return_value={}),
            patch("story_run.semantic_card_generation_receipt_issues", return_value=[]),
            patch("story_run.semantic_card_motion_receipt_issues", return_value=[]),
            patch("story_artifact_validation.validate_release_package_receipt", return_value={}),
        ):
            for artifact_id in CODEX_NATIVE_REQUIRED_ARTIFACTS:
                existing = load_run(run_file)["artifacts"]
                if artifact_id in existing or artifact_id in (skip or set()):
                    continue
                artifact = root / f"{artifact_id}.json"
                if artifact_id in INDEPENDENT_REVIEW_TARGETS:
                    target_id = INDEPENDENT_REVIEW_TARGETS[artifact_id]
                    if target_id:
                        target = existing[target_id]
                        reviewed_path = Path(target["path"])
                        reviewed_sha = target["sha256"]
                    else:
                        reviewed_member = root / f"{artifact_id}.reviewed.bin"
                        reviewed_member.write_bytes(artifact_id.encode("utf-8"))
                        reviewed_path = root / f"{artifact_id}.bundle.json"
                        reviewed_path.write_text(
                            json.dumps(
                                {
                                    "artifacts": [
                                        {
                                            "path": str(reviewed_member),
                                            "sha256": file_sha256(reviewed_member),
                                        }
                                    ]
                                }
                            ),
                            encoding="utf-8",
                        )
                        reviewed_sha = file_sha256(reviewed_path)
                    review_payload = {
                        "schema_version": "independent-fixture-review/v1",
                        "approved": True,
                        "score": 90,
                        "critical_errors": [],
                        "artifact_path": str(reviewed_path),
                        "artifact_sha256": reviewed_sha,
                    }
                    if artifact_id == "customer_media_independent_review":
                        customer_media = json.loads(reviewed_path.read_text(encoding="utf-8"))
                        demo = customer_media["artifacts"]["product_demo"]
                        full_srt = root / "customer-media-full.srt"
                        logo = root / "official-logo.png"
                        full_srt.write_text(
                            "1\n00:00:00,000 --> 00:00:01,000\n完整字幕\n",
                            encoding="utf-8",
                        )
                        logo.write_bytes(b"official-logo")
                        frames = []
                        for index in range(5):
                            frame = root / f"formal-demo-frame-{index}.png"
                            frame.write_bytes(f"frame-{index}".encode("utf-8"))
                            frames.append(
                                {
                                    "time_seconds": float(index),
                                    "path": str(frame),
                                    "sha256": file_sha256(frame),
                                }
                            )
                        review_payload.update(
                            {
                                "checks": {
                                    "three_customer_videos_music_only_and_not_silent": True,
                                    "customer_videos_do_not_contain_authoritative_narration": True,
                                    "demo_contains_narration": True,
                                    "demo_contains_music": True,
                                    "demo_logo_visible_in_all_formal_samples": True,
                                    "demo_presenter_scaled_to_output_canvas": True,
                                    "demo_wide_gesture_not_cropped": True,
                                    "demo_subtitles_match_authoritative_timeline": True,
                                    "customer_subtitles_bottom_centered": True,
                                },
                                "bindings": {
                                    "demo_video": {
                                        "path": demo["path"],
                                        "sha256": demo["sha256"],
                                    },
                                    "authoritative_full_srt": {
                                        "path": str(full_srt),
                                        "sha256": file_sha256(full_srt),
                                    },
                                    "official_logo": {
                                        "path": str(logo),
                                        "sha256": file_sha256(logo),
                                    },
                                },
                                "presenter_geometry": {
                                    "source_canvas": [1920, 1080],
                                    "output_canvas": [1920, 1080],
                                    "rendered_size": [1920, 1080],
                                    "scale": 1.0,
                                },
                                "formal_frame_evidence": frames,
                            }
                        )
                    artifact.write_text(
                        json.dumps(review_payload),
                        encoding="utf-8",
                    )
                elif artifact_id in MACHINE_QA_ARTIFACTS:
                    artifact.write_text(
                        json.dumps(
                            {
                                "schema_version": f"{artifact_id}-fixture/v1",
                                "passed": True,
                                "critical_errors": [],
                                "errors": [],
                            }
                        ),
                        encoding="utf-8",
                    )
                elif artifact_id == "qa_music_report":
                    music = root / "reviewed_music.mp3"
                    narration = root / "bound_narration.wav"
                    music.write_bytes(b"reviewed-music")
                    narration.write_bytes(b"bound-narration")
                    artifact.write_text(
                        json.dumps(
                            {
                                "schema_version": "qa-music-report-fixture/v1",
                                "approved": True,
                                "score": 90,
                                "critical_errors": [],
                                "reviewed_final_audio": {
                                    "path": str(music),
                                    "sha256": file_sha256(music),
                                },
                                "bound_inputs": {
                                    "authoritative_narration": {
                                        "path": str(narration),
                                        "sha256": file_sha256(narration),
                                    }
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                elif artifact_id == "authoritative_timeline_receipt":
                    run = load_run(run_file)
                    subtitle = Path(run["inputs"]["subtitle_txt"]["path"])
                    authoritative_audio = Path(run["inputs"]["audio"]["path"])
                    alignment_audio = root / "alignment.wav"
                    timings = root / "confirmed_line_timings.json"
                    metadata = root / "alignment_run_metadata.json"
                    output_srt = root / "confirmed_spoken_timeline.srt"
                    alignment_audio.write_bytes(b"alignment-audio")
                    timings.write_text(
                        json.dumps(
                            [
                                {
                                    "line": "确认正文，不允许系统改写。",
                                    "source_start": 0.0,
                                    "source_end": 1.0,
                                }
                            ],
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                    metadata.write_text(
                        json.dumps(
                            {
                                "model": "base",
                                "timed_char_count": 10,
                                "audio_path": str(alignment_audio),
                                "audio_sha256": file_sha256(alignment_audio),
                                "subtitle_path": str(subtitle),
                                "subtitle_sha256": file_sha256(subtitle),
                            }
                        ),
                        encoding="utf-8",
                    )
                    write_authoritative_timeline_receipt(
                        receipt_path=artifact,
                        source_kind="whisper_confirmed_line_timings",
                        timings_path=timings,
                        alignment_metadata_path=metadata,
                        subtitle_txt=subtitle,
                        authoritative_audio=authoritative_audio,
                        alignment_audio=alignment_audio,
                        output_srt=output_srt,
                    )
                elif artifact_id == "customer_media_receipt":
                    music = root / "customer-media-music.mp3"
                    subtitle = root / "customer-media-body.srt"
                    music.write_bytes(b"music")
                    subtitle.write_text(
                        "1\n00:00:00,000 --> 00:00:01,000\n正文\n",
                        encoding="utf-8",
                    )
                    timeline = Path(
                        load_run(run_file)["artifacts"]["authoritative_timeline_receipt"]["path"]
                    )
                    roles = {
                        "product_background_with_subtitles": "music_only",
                        "product_background_without_subtitles": "music_only",
                        "product_a_only_background": "music_only",
                        "product_demo": "narration_plus_music",
                    }
                    role_payload = {}
                    for role, audio_role in roles.items():
                        media = root / f"{role}.mp4"
                        media.write_bytes(role.encode("utf-8"))
                        role_payload[role] = {
                            "path": str(media),
                            "sha256": file_sha256(media),
                            "audio_role": audio_role,
                            "audio_present": True,
                        }
                        if audio_role == "music_only":
                            role_payload[role]["audio_fit"] = {"passed": True}
                    artifact.write_text(
                        json.dumps(
                            {
                                "schema_version": "story-customer-media-receipt/v1",
                                "passed": True,
                                "critical_errors": [],
                                "music_source": {
                                    "path": str(music),
                                    "sha256": file_sha256(music),
                                },
                                "authoritative_timeline_receipt": {
                                    "path": str(timeline),
                                    "sha256": file_sha256(timeline),
                                },
                                "subtitle_srt": {
                                    "path": str(subtitle),
                                    "sha256": file_sha256(subtitle),
                                },
                                "artifacts": role_payload,
                                "subtitle_geometry": {"passed": True},
                            }
                        ),
                        encoding="utf-8",
                    )
                elif artifact_id == "final_delivery_checklist":
                    delivered = root / "delivered.bin"
                    delivered.write_bytes(b"delivered")
                    artifact.write_text(
                        json.dumps(
                            {
                                "schema_version": "story-final-delivery-checklist/v1",
                                "status": "complete_pending_independent_final_review",
                                "missing": [],
                                "matrix": {
                                    "release_videos": 2,
                                    "account_copy_files": 2,
                                    "covers": 6,
                                    "basic_customer_files": 5,
                                    "advanced_customer_files": 10,
                                    "static_ppt_variants": 2,
                                },
                                "artifacts": [
                                    {
                                        "path": str(delivered),
                                        "sha256": file_sha256(delivered),
                                    }
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                else:
                    artifact.write_text(
                        f'{{"artifact_id": "{artifact_id}"}}\n',
                        encoding="utf-8",
                    )
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
            self.assertEqual(len(payload["inputs"]["subtitle_txt"]["sha256"]), 64)
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

    def test_unreported_request_cost_and_tokens_remain_null_and_block_new_paid_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, text, video, audio, run_file = self.fixture(Path(directory))
            init_run(
                run_file=run_file,
                confirmed_text=text,
                greenscreen_video=video,
                audio=audio,
                project_dir=project,
            )
            recorded = record_request_observation(
                run_file=run_file,
                package="r2v_visuals",
                provider="toapis",
                request_id="video-unknown-cost",
                operation="image_to_video",
                status="completed",
                model="grok-video-1.0",
                token_status="not_applicable",
                cost_status="provider_not_exposed",
            )
            request = recorded["observability"]["requests"]["toapis:video-unknown-cost"]
            self.assertIsNone(request["actual_cost"])
            self.assertIsNone(request["input_tokens"])
            self.assertIsNone(request["total_tokens"])
            summary = status_summary(recorded)
            self.assertIsNone(summary["paid_total"])
            self.assertIsNone(summary["remaining_hard_budget"])
            self.assertEqual(summary["unsettled_request_count"], 1)
            self.assertFalse(summary["can_start_paid_work"])
            with self.assertRaisesRegex(RuntimeError, "未知不得当作 0"):
                record_run(
                    run_file=run_file,
                    package="music",
                    status="running",
                    paid_amount=0.01,
                )

    def test_reported_request_tokens_and_settled_cost_are_aggregated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, text, video, audio, run_file = self.fixture(Path(directory))
            init_run(
                run_file=run_file,
                confirmed_text=text,
                greenscreen_video=video,
                audio=audio,
                project_dir=project,
            )
            recorded = record_request_observation(
                run_file=run_file,
                package="director_plan",
                provider="codex",
                request_id="director-1",
                operation="director_plan",
                status="completed",
                model="gpt-fixture",
                duration_seconds=12.5,
                wait_seconds=1.5,
                retry_index=0,
                token_status="reported",
                input_tokens=100,
                output_tokens=25,
                actual_cost=1.25,
                cost_status="settled",
            )
            summary = status_summary(recorded)
            self.assertEqual(summary["paid_total"], 1.25)
            self.assertEqual(summary["token_usage"]["total_tokens"], 125)
            self.assertEqual(summary["remaining_hard_budget"], 98.75)
            self.assertTrue(summary["can_start_paid_work"])
            metrics = recorded["observability"]["packages"]["director_plan"]
            self.assertEqual(metrics["active_seconds"], 12.5)
            self.assertEqual(metrics["wait_seconds"], 1.5)
            self.assertEqual(metrics["retry_count"], 0)

    def test_foreground_and_fifteen_minute_background_fixture_is_lock_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, text, video, audio, run_file = self.fixture(root)
            init_run(
                run_file=run_file,
                confirmed_text=text,
                greenscreen_video=video,
                audio=audio,
                project_dir=project,
            )

            def write_observation(index: int) -> None:
                background = index % 2 == 1
                record_request_observation(
                    run_file=run_file,
                    package="r2v_visuals",
                    provider="fixture-background" if background else "fixture-foreground",
                    request_id=f"request-{index:02d}",
                    operation="fifteen_minute_check" if background else "foreground_update",
                    status="completed",
                    started_at=(
                        "2026-09-03T00:15:00+00:00"
                        if background
                        else "2026-09-03T00:00:00+00:00"
                    ),
                    ended_at=(
                        "2026-09-03T00:15:01+00:00"
                        if background
                        else "2026-09-03T00:00:01+00:00"
                    ),
                    duration_seconds=1.0,
                    retry_index=0,
                    token_status="not_applicable",
                    cost_status="not_applicable",
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(write_observation, range(40)))

            payload = load_run(run_file)
            self.assertEqual(len(payload["observability"]["requests"]), 40)
            self.assertEqual(
                len(
                    [
                        event
                        for event in payload["observability"]["events"]
                        if event["event"] == "request_observation"
                    ]
                ),
                40,
            )
            self.assertEqual(status_summary(payload)["cost_status"], "settled")

    def test_foreground_and_fifteen_minute_background_processes_share_file_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, text, video, audio, run_file = self.fixture(Path(directory))
            init_run(
                run_file=run_file,
                confirmed_text=text,
                greenscreen_video=video,
                audio=audio,
                project_dir=project,
            )
            worker = """
import sys
from pathlib import Path
from story_run import record_request_observation
run_file = Path(sys.argv[1])
provider, timestamp = sys.argv[2], sys.argv[3]
for index in range(20):
    record_request_observation(
        run_file=run_file,
        package='r2v_visuals',
        provider=provider,
        request_id=f'{provider}-{index:02d}',
        operation='fifteen_minute_check' if provider == 'background' else 'foreground_update',
        status='completed',
        started_at=timestamp,
        ended_at=timestamp,
        duration_seconds=0.0,
        wait_seconds=0.0,
        retry_index=0,
        token_status='not_applicable',
        cost_status='not_applicable',
    )
"""
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        worker,
                        str(run_file),
                        "foreground",
                        "2026-09-03T00:00:00+00:00",
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ),
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        worker,
                        str(run_file),
                        "background",
                        "2026-09-03T00:15:00+00:00",
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ),
            ]
            for process in processes:
                _stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr)
            payload = load_run(run_file)
            self.assertEqual(len(payload["observability"]["requests"]), 40)
            self.assertEqual(
                len(payload["observability"]["events"]),
                40,
            )

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
                patch("story_run.semantic_card_generation_receipt_issues", return_value=[]),
                patch("story_run.semantic_card_motion_receipt_issues", return_value=[]),
                patch("story_artifact_validation.validate_release_package_receipt", return_value={}),
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
                patch("story_artifact_validation.validate_release_package_receipt", return_value={}),
                patch("story_run.semantic_card_generation_receipt_issues", return_value=[]),
                patch("story_run.semantic_card_motion_receipt_issues", return_value=[]),
            ):
                finalized = finalize_run(run_file=run_file, required_artifacts=[])
            self.assertTrue(finalized["finalized_at"])

    def test_record_compile_receipt_allows_provider_job_status_writeback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, text, video, audio, run_file = self.fixture(root)
            init_run(
                run_file=run_file,
                confirmed_text=text,
                greenscreen_video=video,
                audio=audio,
                project_dir=project,
            )
            compile_receipt = root / "compile_receipt.json"
            compile_receipt.write_text("{}", encoding="utf-8")

            with patch(
                "story_run.validate_compile_receipt",
                return_value={"shot_count": 2},
            ) as validator:
                record_run(
                    run_file=run_file,
                    package="r2v_visuals",
                    status="done",
                    artifact_id="shot_storyboard_compile_receipt",
                    artifact_path=compile_receipt,
                )

            validator.assert_called_once_with(
                compile_receipt.resolve(),
                require_current_r2v_jobs=False,
            )

    def test_record_rejects_placeholder_independent_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, text, video, audio, run_file = self.fixture(root)
            init_run(
                run_file=run_file,
                confirmed_text=text,
                greenscreen_video=video,
                audio=audio,
                project_dir=project,
            )
            placeholder = root / "director_plan_review.json"
            placeholder.write_text(
                '{"artifact_id": "director_plan_review"}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "独立审核"):
                record_run(
                    run_file=run_file,
                    package="director_plan",
                    status="done",
                    artifact_id="director_plan_review",
                    artifact_path=placeholder,
                )

    def test_storyboard_review_binds_semantic_bundle_and_current_manifest_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, text, video, audio, run_file = self.fixture(root)
            init_run(
                run_file=run_file,
                confirmed_text=text,
                greenscreen_video=video,
                audio=audio,
                project_dir=project,
            )
            bundle_sha = "a" * 64
            manifest = root / "shot_storyboards_sealed.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "story-shot-storyboards/v1",
                        "status": "sealed",
                        "storyboard_bundle_sha256": bundle_sha,
                    }
                ),
                encoding="utf-8",
            )
            record_run(
                run_file=run_file,
                package="r2v_visuals",
                status="running",
                artifact_id="storyboard_manifest_sealed",
                artifact_path=manifest,
            )
            review = root / "storyboards_review.json"
            review_payload = {
                "schema_version": "story-shot-storyboards-independent-review/v1",
                "reviewer_independence": {"producer_claims_trusted": False},
                "approved": True,
                "score": 95,
                "critical_errors": [],
                "artifact_path": str(manifest),
                "artifact_sha256": bundle_sha,
            }
            review.write_text(json.dumps(review_payload), encoding="utf-8")
            recorded = record_run(
                run_file=run_file,
                package="r2v_visuals",
                status="running",
                artifact_id="storyboard_review",
                artifact_path=review,
            )
            self.assertEqual(
                recorded["artifacts"]["storyboard_review"]["path"],
                str(review.resolve()),
            )

            review_payload["artifact_sha256"] = "b" * 64
            review.write_text(json.dumps(review_payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "storyboard_bundle_sha256"):
                record_run(
                    run_file=run_file,
                    package="r2v_visuals",
                    status="running",
                    artifact_id="storyboard_review",
                    artifact_path=review,
                    replace=True,
                )

    def test_legacy_storyboard_review_without_semantic_bundle_keeps_file_sha_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, text, video, audio, run_file = self.fixture(root)
            init_run(
                run_file=run_file,
                confirmed_text=text,
                greenscreen_video=video,
                audio=audio,
                project_dir=project,
            )
            manifest = root / "legacy_storyboard_manifest.json"
            manifest.write_text(
                json.dumps({"schema_version": "legacy-storyboard-manifest/v1"}),
                encoding="utf-8",
            )
            record_run(
                run_file=run_file,
                package="r2v_visuals",
                status="running",
                artifact_id="storyboard_manifest_sealed",
                artifact_path=manifest,
            )
            review = root / "legacy_storyboard_review.json"
            review.write_text(
                json.dumps(
                    {
                        "schema_version": "story-independent-review/v1",
                        "approved": True,
                        "score": 95,
                        "critical_errors": [],
                        "artifact_path": str(manifest),
                        "artifact_sha256": file_sha256(manifest),
                    }
                ),
                encoding="utf-8",
            )
            recorded = record_run(
                run_file=run_file,
                package="r2v_visuals",
                status="running",
                artifact_id="storyboard_review",
                artifact_path=review,
            )
            self.assertEqual(
                recorded["artifacts"]["storyboard_review"]["path"],
                str(review.resolve()),
            )

    def test_customer_media_review_requires_logo_geometry_and_formal_frames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, text, video, audio, run_file = self.fixture(root)
            init_run(
                run_file=run_file,
                confirmed_text=text,
                greenscreen_video=video,
                audio=audio,
                project_dir=project,
            )
            for package in PACKAGE_NAMES:
                record_run(run_file=run_file, package=package, status="done")
            self.record_finalize_profile(
                root,
                run_file,
                skip={"customer_media_independent_review"},
            )
            target = load_run(run_file)["artifacts"]["customer_media_receipt"]
            review = root / "customer_media_independent_review.json"
            review.write_text(
                json.dumps(
                    {
                        "schema_version": "story-customer-media-independent-review/v1",
                        "approved": True,
                        "score": 92,
                        "critical_errors": [],
                        "artifact_path": target["path"],
                        "artifact_sha256": target["sha256"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Logo|几何|证据"):
                record_run(
                    run_file=run_file,
                    package="product_assets",
                    status="done",
                    artifact_id="customer_media_independent_review",
                    artifact_path=review,
                )

    def test_record_rejects_failed_machine_qa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, text, video, audio, run_file = self.fixture(root)
            init_run(
                run_file=run_file,
                confirmed_text=text,
                greenscreen_video=video,
                audio=audio,
                project_dir=project,
            )
            report = root / "qa_product_report.json"
            report.write_text(
                json.dumps(
                    {
                        "schema_version": "story-product-machine-qa/v1",
                        "passed": False,
                        "critical_errors": ["customer_audio_contains_narration"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "机器 QA"):
                record_run(
                    run_file=run_file,
                    package="product_assets",
                    status="done",
                    artifact_id="qa_product_report",
                    artifact_path=report,
                )

    def test_finalize_revalidates_review_against_replaced_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, text, video, audio, run_file = self.fixture(root)
            init_run(
                run_file=run_file,
                confirmed_text=text,
                greenscreen_video=video,
                audio=audio,
                project_dir=project,
            )
            for package in PACKAGE_NAMES:
                record_run(run_file=run_file, package=package, status="done")
            self.record_finalize_profile(root, run_file)

            plan_record = load_run(run_file)["artifacts"]["master_director_plan"]
            plan = Path(plan_record["path"])
            plan.write_text('{"revision": 2}\n', encoding="utf-8")
            record_run(
                run_file=run_file,
                package="director_plan",
                status="done",
                artifact_id="master_director_plan",
                artifact_path=plan,
                replace=True,
            )
            with (
                patch("story_run.validate_compile_receipt", return_value={"shot_count": 1}),
                patch("story_run.validate_delivery_receipt", return_value={}),
                patch("story_run.validate_theme_assets_manifest", return_value={}),
                patch("story_run.semantic_card_generation_receipt_issues", return_value=[]),
                patch("story_run.semantic_card_motion_receipt_issues", return_value=[]),
                patch("story_artifact_validation.validate_release_package_receipt", return_value={}),
            ):
                with self.assertRaisesRegex(RuntimeError, "产物语义门禁失败.*独立审核"):
                    finalize_run(run_file=run_file, required_artifacts=[])


if __name__ == "__main__":
    unittest.main()
