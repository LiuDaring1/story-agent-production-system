from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import story_workflow
from story_evidence import review_bundle_is_current
from story_workflow import (
    build_product_text_sources_from_story_source,
    bind_generated_product_outputs,
    confirmed_audio_from_story_run,
    confirmed_subtitle_from_story_run,
    confirmed_text_from_story_run,
    current_demo_preview_manifest,
    customer_manuscript_source,
    ensure_confirmed_spoken_timeline_srt,
    preview_times_with_library_tail_coverage,
    product_demo_audio_source,
    product_body_subtitles_from_customer_media_receipt,
    sealed_storyboard_images_dir,
    static_ppt_inputs_from_delivery_receipt,
    validate_static_ppt_full_timeline,
    write_release_preview_review_bundle,
)

try:
    from product_package import reject_full_subtitle_background
except ModuleNotFoundError:
    # The media-package test suite has an optional python-docx dependency in
    # the lightweight runtime used by alignment-only CI.
    reject_full_subtitle_background = None  # type: ignore[assignment]


def write_srt(path: Path, texts: list[str]) -> None:
    blocks = []
    for index, text in enumerate(texts, start=1):
        start = index - 1
        end = index
        blocks.append(
            f"{index}\n00:00:{start:02d},000 --> 00:00:{end:02d},000\n{text}"
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


class ProductTextSourceAlignmentTests(unittest.TestCase):
    def test_formal_product_rebuild_rebinds_qa_away_from_existing_old_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：测试故事"
            status = project / "99_项目状态"
            old_root = project / "05_产品素材准备" / "old" / "客户资料包"
            old_base = old_root / "绵羊故事锦囊：测试故事（基础版）"
            old_advanced = old_root / "绵羊故事锦囊：测试故事（进阶版）"
            new_base = project / "06_资料包" / "绵羊故事锦囊：测试故事（基础版）"
            new_advanced = project / "06_资料包" / "绵羊故事锦囊：测试故事（进阶版）"
            for path in (status, old_base, old_advanced, new_base, new_advanced):
                path.mkdir(parents=True, exist_ok=True)
            (status / "project_manifest.json").write_text(
                json.dumps({
                    "version": 1,
                    "story": {"name": "测试故事", "slug": "test"},
                    "inputs": {},
                    "outputs": {
                        "product_base": str(old_base),
                        "product_advanced": str(old_advanced),
                    },
                    "qa": {},
                    "manual_outputs": {},
                    "agent": {},
                }),
                encoding="utf-8",
            )

            manifest = bind_generated_product_outputs(project, "测试故事")

            self.assertEqual(manifest["outputs"]["product_base"], str(new_base))
            self.assertEqual(manifest["outputs"]["product_advanced"], str(new_advanced))

    def test_product_demo_audio_prefers_full_program_over_body_narration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            full_program = root / "full.m4a"
            body_only = root / "body.wav"
            full_program.write_bytes(b"full-program")
            body_only.write_bytes(b"body-only")

            self.assertEqual(
                product_demo_audio_source(full_program, body_only, None),
                full_program,
            )

    def test_body_subtitles_resolve_from_hash_bound_customer_media_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "99_项目状态"
            status.mkdir()
            subtitles = root / "body.srt"
            subtitles.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n正文\n",
                encoding="utf-8",
            )
            receipt = root / "customer_media_receipt.json"
            receipt.write_text(
                json.dumps({
                    "passed": True,
                    "critical_errors": [],
                    "subtitle_srt": {
                        "path": str(subtitles),
                        "sha256": hashlib.sha256(subtitles.read_bytes()).hexdigest(),
                    },
                }),
                encoding="utf-8",
            )
            (status / "story_run.json").write_text(
                json.dumps({
                    "artifacts": {
                        "customer_media_receipt": {
                            "path": str(receipt),
                            "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
                        }
                    }
                }),
                encoding="utf-8",
            )

            self.assertEqual(
                product_body_subtitles_from_customer_media_receipt(status),
                subtitles,
            )
            subtitles.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "哈希失效"):
                product_body_subtitles_from_customer_media_receipt(status)

    def test_static_ppt_inputs_come_from_validated_delivery_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / "static_ppt_delivery_receipt.json"
            receipt.write_text("{}\n", encoding="utf-8")
            payload = {
                "director_plan_path": str(root / "director.json"),
                "shot_storyboard_compile_receipt_path": str(root / "compile.json"),
                "ppt_plan_path": str(root / "plan.json"),
                "with_subtitles_pptx_path": str(root / "with.pptx"),
                "without_subtitles_pptx_path": str(root / "without.pptx"),
            }
            with patch("story_workflow.story_run_artifact_path", return_value=receipt), patch(
                "story_workflow.validate_static_ppt_delivery_receipt",
                return_value=payload,
            ):
                resolved = static_ppt_inputs_from_delivery_receipt(root)

            self.assertEqual(resolved["--static-ppt-with-subtitles"], root / "with.pptx")
            self.assertEqual(resolved["--static-ppt-without-subtitles"], root / "without.pptx")

    def test_product_alignment_reads_docx_paragraphs_not_zip_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "confirmed.docx"
            document_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>第一段故事。</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>第二段故事。</w:t></w:r></w:p></w:body></w:document>'
            )
            with zipfile.ZipFile(story, "w") as archive:
                archive.writestr("word/document.xml", document_xml)
            subtitles = root / "story.srt"
            write_srt(subtitles, ["第一段故事。", "第二段故事。"])

            script, timings = build_product_text_sources_from_story_source(
                story,
                subtitles,
                root / "work",
            )

            self.assertEqual(script.read_text(encoding="utf-8"), "第一段故事。\n第二段故事。\n")
            self.assertEqual(len(json.loads(timings.read_text(encoding="utf-8"))), 2)

    def test_customer_manuscript_source_prefers_confirmed_input_over_old_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            confirmed = root / "confirmed.docx"
            generated = root / "old-customer.docx"
            generic = root / "subtitles.txt"
            for path in (confirmed, generated, generic):
                path.write_bytes(path.name.encode())

            self.assertEqual(
                customer_manuscript_source(confirmed, generated, generic),
                confirmed,
            )

    def test_release_preview_generation_writes_current_hash_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preview = root / "release_preview_frames"
            preview.mkdir()
            frame = preview / "main_001s_c.png"
            geometry = preview / "release_geometry_manifest_main.json"
            frame.write_bytes(b"preview-frame")
            geometry.write_text('{"geometry": "current"}\n', encoding="utf-8")
            bundle = root / "reviews" / "release_preview_bundle.json"

            result = write_release_preview_review_bundle(preview, bundle)

            self.assertEqual(result, bundle)
            payload = json.loads(bundle.read_text(encoding="utf-8"))
            self.assertEqual(
                {Path(item["path"]).name for item in payload["artifacts"]},
                {frame.name, geometry.name},
            )
            self.assertTrue(review_bundle_is_current(bundle))
            frame.write_bytes(b"changed-after-review-bundle")
            self.assertFalse(review_bundle_is_current(bundle))

    def test_demo_preview_manifest_resolution_skips_dead_canonical_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            fallback = project / "99_项目状态" / "publish" / "repair_demo_render_manifest_preview.json"
            fallback.parent.mkdir(parents=True)
            fallback.write_text('{"mode":"preview"}\n', encoding="utf-8")
            manifest = {
                "outputs": {
                    "demo_preview_manifest": str(
                        project / "99_项目状态" / "product_package_work" / "demo_preview_manifest.json"
                    )
                }
            }

            with patch("story_workflow.demo_render_manifest_issues", return_value=[]):
                selected = current_demo_preview_manifest(project, manifest)

            self.assertEqual(selected, fallback)

    def test_library_preview_samples_expected_tail_onset_not_only_last_second(self) -> None:
        values = [
            float(item)
            for item in preview_times_with_library_tail_coverage(
                "1,179",
                duration=180.0,
                tail_seconds=0.0,
            ).split(",")
        ]
        # Automatic tail is 36 seconds for a three-minute story, so QA must
        # straddle the expected ~144 s transition instead of merely sampling
        # a final frame where even a one-second regression looks correct.
        self.assertTrue(any(143.0 <= item < 144.0 for item in values), values)
        self.assertTrue(any(144.0 < item <= 145.5 for item in values), values)

    def test_confirmed_audio_anchors_generate_full_spoken_srt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "99_项目状态"
            assembly = root / "03_背景成片"
            timing_dir = status / "preflight_alignment"
            timing_dir.mkdir(parents=True)
            assembly.mkdir(parents=True)
            subtitle_txt = root / "确认字幕.txt"
            subtitle_txt.write_text("大家好\n正文\n道理\n", encoding="utf-8")
            authoritative_audio = root / "narration.m4a"
            alignment_audio = timing_dir / "alignment.wav"
            authoritative_audio.write_bytes(b"authoritative-audio")
            alignment_audio.write_bytes(b"alignment-audio")
            status.mkdir(parents=True, exist_ok=True)
            (status / "story_run.json").write_text(
                json.dumps(
                    {
                        "inputs": {
                            "subtitle_txt": {
                                "path": str(subtitle_txt),
                                "sha256": hashlib.sha256(subtitle_txt.read_bytes()).hexdigest(),
                            },
                            "audio": {
                                "path": str(authoritative_audio),
                                "sha256": hashlib.sha256(authoritative_audio.read_bytes()).hexdigest(),
                            },
                        },
                        "artifacts": {},
                    }
                ),
                encoding="utf-8",
            )
            (timing_dir / "confirmed_line_timings.json").write_text(
                json.dumps(
                    [
                        {"line": "大家好", "source_start": 0, "source_end": 1},
                        {"line": "正文", "source_start": 1, "source_end": 2},
                        {"line": "道理", "source_start": 2, "source_end": 3},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (timing_dir / "alignment_run_metadata.json").write_text(
                json.dumps(
                    {
                        "model": "base",
                        "timed_token_count": 3,
                        "timed_char_count": 6,
                        "audio_path": str(alignment_audio),
                        "audio_sha256": hashlib.sha256(alignment_audio.read_bytes()).hexdigest(),
                        "subtitle_path": str(subtitle_txt),
                        "subtitle_sha256": hashlib.sha256(subtitle_txt.read_bytes()).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )

            result = ensure_confirmed_spoken_timeline_srt(status, assembly)

            self.assertIsNotNone(result)
            content = result.read_text(encoding="utf-8")
            self.assertIn("大家好", content)
            self.assertIn("正文", content)
            self.assertIn("道理", content)
            receipt = timing_dir / "authoritative_timeline_receipt.json"
            self.assertTrue(receipt.is_file())
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(payload["source_kind"], "whisper_confirmed_line_timings")
            self.assertFalse(payload["fallback_used"])

    def test_unreceipted_even_fallback_is_not_a_production_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "99_项目状态"
            assembly = root / "03_背景成片"
            fallback = status / "preflight"
            fallback.mkdir(parents=True)
            assembly.mkdir(parents=True)
            (fallback / "confirmed_spoken_timeline.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n均分时间轴\n",
                encoding="utf-8",
            )
            (fallback / "subtitle_timeline_preflight.json").write_text(
                "[]\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "fallback|均分"):
                ensure_confirmed_spoken_timeline_srt(status, assembly)

    def test_static_ppt_timeline_preserves_opening_and_moral_audio_slots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            srt = root / "body.srt"
            srt.write_text(
                "1\n00:00:09,000 --> 00:02:59,000\n正文\n",
                encoding="utf-8",
            )
            plan = root / "plan.json"
            valid = {
                "slides": [
                    {"shot_id": "TITLE", "duration_seconds": 9.0},
                    {"shot_id": "S01", "duration_seconds": 170.0},
                    {"shot_id": "MORAL", "duration_seconds": 21.0},
                ]
            }
            plan.write_text(json.dumps(valid), encoding="utf-8")
            validate_static_ppt_full_timeline(plan, srt, 200.0)
            valid["slides"][0]["duration_seconds"] = 0.0
            valid["slides"][1]["duration_seconds"] = 179.0
            plan.write_text(json.dumps(valid), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "TITLE 时长"):
                validate_static_ppt_full_timeline(plan, srt, 200.0)

    def test_sealed_storyboard_directory_requires_every_image_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "99_项目状态"
            storyboards = status / "storyboards"
            images = storyboards / "requests"
            images.mkdir(parents=True)
            image = images / "S01.png"
            image.write_bytes(b"image")
            import hashlib

            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            (storyboards / "storyboard_manifest_sealed.json").write_text(
                json.dumps({
                    "status": "sealed",
                    "entries": [{"image_path": str(image), "image_sha256": digest}],
                }),
                encoding="utf-8",
            )
            self.assertEqual(sealed_storyboard_images_dir(status), images.resolve())
            image.write_bytes(b"tampered")
            self.assertIsNone(sealed_storyboard_images_dir(status))

    def test_native_ledger_confirmed_text_is_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "99_项目状态"
            status.mkdir()
            confirmed = root / "字幕：故事.txt"
            confirmed.write_text("标题\n正文\n", encoding="utf-8")
            import hashlib

            digest = hashlib.sha256(confirmed.read_bytes()).hexdigest()
            (status / "story_run.json").write_text(
                json.dumps({"inputs": {"confirmed_text": {"path": str(confirmed), "sha256": digest}}}),
                encoding="utf-8",
            )
            self.assertEqual(confirmed_text_from_story_run(status), confirmed)
            confirmed.write_text("被篡改", encoding="utf-8")
            self.assertIsNone(confirmed_text_from_story_run(status))

    def test_native_ledger_confirmed_docx_and_full_audio_are_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "99_项目状态"
            status.mkdir()
            confirmed = root / "确认故事.docx"
            audio = root / "完整节目.m4a"
            confirmed.write_bytes(b"docx-fixture")
            audio.write_bytes(b"full-program-audio")
            (status / "story_run.json").write_text(
                json.dumps({
                    "inputs": {
                        "confirmed_text": {
                            "path": str(confirmed),
                            "sha256": hashlib.sha256(confirmed.read_bytes()).hexdigest(),
                        },
                        "audio": {
                            "path": str(audio),
                            "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                        },
                    }
                }),
                encoding="utf-8",
            )

            self.assertEqual(confirmed_text_from_story_run(status), confirmed)
            self.assertEqual(confirmed_audio_from_story_run(status), audio)
            audio.write_bytes(b"tampered")
            self.assertIsNone(confirmed_audio_from_story_run(status))

    def test_native_ledger_subtitle_txt_is_exact_and_fails_on_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "99_项目状态"
            status.mkdir()
            subtitle = root / "确认字幕.txt"
            subtitle.write_text("妈妈，这是太阳吗？\n不是，这是气球。\n", encoding="utf-8")
            import hashlib

            digest = hashlib.sha256(subtitle.read_bytes()).hexdigest()
            (status / "story_run.json").write_text(
                json.dumps({"inputs": {"subtitle_txt": {"path": str(subtitle), "sha256": digest}}}),
                encoding="utf-8",
            )
            self.assertEqual(confirmed_subtitle_from_story_run(status), subtitle)
            subtitle.write_text("被改过的字幕\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "哈希漂移"):
                confirmed_subtitle_from_story_run(status)

    def run_alignment(self, story_lines: list[str], subtitle_lines: list[str]):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            story_path = root / "story.txt"
            subtitles_path = root / "story_subtitles.srt"
            output_dir = root / "work"
            story_path.write_text("\n".join(story_lines) + "\n", encoding="utf-8")
            write_srt(subtitles_path, subtitle_lines)
            script_path, timings_path = build_product_text_sources_from_story_source(
                story_text=story_path,
                subtitles_srt=subtitles_path,
                output_dir=output_dir,
            )
            return (
                script_path.read_text(encoding="utf-8"),
                json.loads(timings_path.read_text(encoding="utf-8")),
            )

    def test_skips_semantic_title_and_host_intro_prefix(self) -> None:
        story_lines = [
            "一天，小壁虎爬呀爬，爬到小河边。",
            "小壁虎看见了妈妈。",
        ]
        script, timings = self.run_alignment(
            story_lines,
            [
                "小壁虎找妈妈",
                "大家好，我是小鱼姐姐。",
                *story_lines,
            ],
        )

        self.assertEqual(script, "\n".join(story_lines) + "\n")
        self.assertEqual([item["source_cue_start"] for item in timings], [3, 4])
        self.assertEqual([item["source_cue_end"] for item in timings], [3, 4])

    def test_confirmed_text_title_can_be_absent_from_body_subtitles(self) -> None:
        body = ["很久很久以前", "天地还没有分开"]
        script, timings = self.run_alignment(
            ["盘古开天辟地", *body],
            body,
        )
        self.assertEqual(script, "\n".join(body) + "\n")
        self.assertEqual([item["line"] for item in timings], body)

    def test_presenter_intro_absent_from_sales_subtitles_is_trimmed_before_alignment(self) -> None:
        body = ["一只狼饿了好几天", "这时走来一只小鸭子"]
        script, timings = self.run_alignment(
            ["大家好我是绵羊姐姐", *body],
            body,
        )
        self.assertEqual(script, "\n".join(body) + "\n")
        self.assertEqual([item["line"] for item in timings], body)

    def test_independent_moral_card_suffix_is_trimmed_after_exact_body_subtitles(self) -> None:
        body = ["狼气疯了", "没了声"]
        script, timings = self.run_alignment(
            [
                *body,
                "小朋友们",
                "这个故事告诉我们",
                "遇到危险不要慌",
            ],
            body,
        )
        self.assertEqual(script, "\n".join(body) + "\n")
        self.assertEqual([item["line"] for item in timings], body)

    def test_skips_semantic_story_announcement_prefix(self) -> None:
        story_lines = ["一天，小壁虎爬呀爬，爬到小河边。"]
        script, timings = self.run_alignment(
            story_lines,
            ["小壁虎找妈妈", "今天要给大家讲小壁虎找妈妈。", story_lines[0]],
        )

        self.assertEqual(script, story_lines[0] + "\n")
        self.assertEqual(timings[0]["source_cue_start"], 3)
        self.assertEqual(timings[0]["source_cue_end"], 3)

    def test_missing_story_body_still_fails_after_prefix(self) -> None:
        story_lines = [
            "一天，小壁虎爬呀爬，爬到小河边。",
            "小壁虎看见了妈妈。",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            story_path = root / "story.txt"
            subtitles_path = root / "story_subtitles.srt"
            story_path.write_text("\n".join(story_lines) + "\n", encoding="utf-8")
            write_srt(
                subtitles_path,
                ["小壁虎找妈妈", "大家好，我是小鱼姐姐。", story_lines[0]],
            )

            with self.assertRaisesRegex(ValueError, r"故事原文第 2 行无法与 story_subtitles\.srt 顺序对齐"):
                build_product_text_sources_from_story_source(
                    story_text=story_path,
                    subtitles_srt=subtitles_path,
                    output_dir=root / "work",
                )


class ProductPackageWorkflowForwardingTests(unittest.TestCase):
    def run_alignment(self, story_lines: list[str], subtitle_lines: list[str]):
        return ProductTextSourceAlignmentTests.run_alignment(self, story_lines, subtitle_lines)

    def test_forwards_complete_sealed_static_ppt_contract(self) -> None:
        argv = [
            "story_workflow.py",
            "product-package",
            "--story-name", "通用故事",
            "--story-text", "story.txt",
            "--script-lines", "lines.txt",
            "--narration", "narration.wav",
            "--music", "music.mp3",
            "--images-dir", "images",
            "--bg-video-with-sub", "with.mp4",
            "--bg-video-no-sub", "without.mp4",
            "--person-greenscreen", "person.mp4",
            "--annotation-skill-path", "skill.md",
            "--director-plan", "director.json",
            "--shot-storyboard-compile-receipt", "compile.json",
            "--static-ppt-plan", "ppt-plan.json",
            "--static-ppt-with-subtitles", "with.pptx",
            "--static-ppt-without-subtitles", "without.pptx",
        ]
        with patch.object(sys, "argv", argv), patch.object(story_workflow, "run_script") as runner:
            story_workflow.main()
        command = list(runner.call_args.args)
        self.assertEqual(command[0], "product_package.py")
        expected = {
            "--director-plan": "director.json",
            "--shot-storyboard-compile-receipt": "compile.json",
            "--static-ppt-plan": "ppt-plan.json",
            "--static-ppt-with-subtitles": "with.pptx",
            "--static-ppt-without-subtitles": "without.pptx",
        }
        for option, value in expected.items():
            self.assertIn(option, command)
            self.assertEqual(str(command[command.index(option) + 1]), value)

    def test_release_forwarding_requires_and_preserves_strong_contract(self) -> None:
        argv = [
            "story_workflow.py",
            "package-release",
            "--story-name", "通用故事",
            "--duration-text", "3分钟",
            "--age-text", "4-8岁",
            "--bg-video", "background.mp4",
            "--output-dir", "release",
            "--keyer", "rvm",
            "--contract-render-spec", "contract.json",
            "--artifact-semantic-plan", "semantic.json",
            "--demo-render-manifest", "demo.json",
            "--main-top-panel", "main-top.png",
            "--main-bottom-panel", "main-bottom.png",
            "--library-top-panel", "library-top.png",
            "--library-bottom-panel", "library-bottom.png",
            "--main-package-spec", "package-spec.json",
            "--main-package-receipt", "package-receipt.json",
            "--approved-preview-geometry", "approved-preview.json",
        ]
        with patch.object(sys, "argv", argv), patch.object(story_workflow, "run_script") as runner:
            story_workflow.main()

        command = list(runner.call_args.args)
        self.assertEqual(command[0], "release_video.py")
        for option, value in (
            ("--age-text", "4-8岁"),
            ("--contract-render-spec", "contract.json"),
            ("--artifact-semantic-plan", "semantic.json"),
            ("--demo-render-manifest", "demo.json"),
            ("--main-top-panel", "main-top.png"),
            ("--main-bottom-panel", "main-bottom.png"),
            ("--library-top-panel", "library-top.png"),
            ("--library-bottom-panel", "library-bottom.png"),
            ("--main-package-spec", "package-spec.json"),
            ("--main-package-receipt", "package-receipt.json"),
            ("--approved-preview-geometry", "approved-preview.json"),
        ):
            self.assertIn(option, command)
            self.assertEqual(str(command[command.index(option) + 1]), value)
        self.assertNotIn("--plate-image", command)

    def test_direct_alignment_without_prefix_remains_supported(self) -> None:
        story_lines = [
            "森林里住着一只小狐狸。",
            "小狐狸每天都练习唱歌。",
        ]
        script, timings = self.run_alignment(story_lines, story_lines)

        self.assertEqual(script, "\n".join(story_lines) + "\n")
        self.assertEqual([item["source_cue_start"] for item in timings], [1, 2])
        self.assertEqual([item["source_cue_end"] for item in timings], [1, 2])

    def test_missing_middle_story_line_is_not_skipped(self) -> None:
        story_lines = [
            "一天，小壁虎爬呀爬，爬到小河边。",
            "小壁虎看见了妈妈。",
            "小壁虎开心地笑了。",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            story_path = root / "story.txt"
            subtitles_path = root / "story_subtitles.srt"
            story_path.write_text("\n".join(story_lines) + "\n", encoding="utf-8")
            write_srt(subtitles_path, ["小壁虎找妈妈", "大家好，我是小鱼姐姐。", story_lines[0], story_lines[2]])

            with self.assertRaisesRegex(ValueError, r"故事原文第 2 行无法与 story_subtitles\.srt 顺序对齐"):
                build_product_text_sources_from_story_source(
                    story_text=story_path,
                    subtitles_srt=subtitles_path,
                    output_dir=root / "work",
                )


@unittest.skipIf(
    reject_full_subtitle_background is None,
    "product_package optional document dependencies are unavailable",
)
class ProductBackgroundSubtitleValidationTests(unittest.TestCase):
    def write_background_srt(self, root: Path, lines: list[str]) -> Path:
        srt = root / "story_sales_subtitles.srt"
        write_srt(srt, lines)
        return srt

    def test_legal_body_and_moral_small_friends_lines_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "story_sales_subs_bgm.mp4"
            video.touch()
            self.write_background_srt(
                root,
                [
                    "森林里住着一只小兔子。",
                    "小朋友们，你们觉得它会怎么做？",
                    "小朋友们，这个故事告诉我们要勇敢。",
                ],
            )

            reject_full_subtitle_background(video)

    def test_title_like_body_opening_is_allowed_when_not_the_reviewed_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "story_sales_subs_bgm.mp4"
            video.touch()
            self.write_background_srt(root, ["很久很久以前", "天地还没有分开"])
            reject_full_subtitle_background(video, story_title="盘古开天辟地")

    def test_host_intro_still_fails_semantic_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "story_sales_subs_bgm.mp4"
            video.touch()
            self.write_background_srt(root, ["大家好，我是小鱼姐姐。", "森林里住着一只小兔子。"])

            with self.assertRaisesRegex(ValueError, "host_intro"):
                reject_full_subtitle_background(video)

    def test_legacy_full_subtitle_filename_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "story_subs_bgm.mp4"
            video.touch()

            with self.assertRaisesRegex(ValueError, "完整字幕版"):
                reject_full_subtitle_background(video)


if __name__ == "__main__":
    unittest.main()
