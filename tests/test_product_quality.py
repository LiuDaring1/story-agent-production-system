from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from product_package import (
    AnnotationBlock,
    build_ppt_manifest_rows,
    build_story_ppt,
    render_ppt_evidence,
    render_annotation_blocks_docx,
    render_story_docx,
    validate_annotation_coverage,
)
from product_quality import (
    annotation_review_payload_issues,
    annotation_receipt_issues,
    atomic_write_json,
    compile_product_content_manifest,
    manuscript_receipt_issues,
    ppt_render_manifest_issues,
    product_content_manifest_issues,
    product_package_manifest_issues,
    product_package_review_payload_issues,
    write_annotation_receipt,
    write_manuscript_receipt,
    write_ppt_render_manifest,
    write_product_package_manifest,
)
from story_agent import AgentContext, StageResult, StoryAgent
from story_agent_runtime import file_sha256
from story_project import init_project, project_paths
from story_video_synthesizer.align import LineTiming


def timing(index: int, text: str, start: float) -> LineTiming:
    return LineTiming(index, text, start, start + 1.0, 1.0, start, start + 1.0)


class ProductQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.plan = self.root / "artifact_semantic_plan.json"
        self.script = self.root / "story.txt"
        self.lines = ["第一句。", "第二句。"]
        self.script.write_text("\n".join(self.lines), encoding="utf-8")
        self.images = []
        for index in range(2):
            path = self.root / f"image_{index}.png"
            Image.new("RGB", (320, 180), (80 + index * 20, 120, 150)).save(path)
            self.images.append(path)
        self.timings = [timing(1, self.lines[0], 0.0), timing(2, self.lines[1], 1.0)]
        self.semantic = {
            "story_contract_sha256": "a" * 64,
            "contract_schema_version": "1.0",
            "story_contract_dependency_sha256": "b" * 64,
            "contract_projection_sha256": "c" * 64,
        }
        self.plan_payload = {
            **self.semantic,
            "schema_version": "1.0",
            "compiler_version": "1",
            "semantic_source": {"line_count": len(self.lines)},
            "artifacts": {
                artifact: {
                    "decisions": [{
                        "semantic_kind": "story_body",
                        "source_line_numbers": [1, 2],
                        "action": "include",
                        "subtitle_policy": "show",
                    }]
                }
                for artifact in (
                    "ppt", "customer_manuscript", "reading_annotation", "demo_subtitles"
                )
            },
        }
        self.plan.write_text(json.dumps(self.plan_payload, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_content_manifest(self) -> Path:
        path = self.root / "product_content_manifest.json"
        payload = compile_product_content_manifest(
            semantic_plan_path=self.plan,
            semantic_plan=self.semantic,
            source_script=self.script,
            source_lines=self.lines,
            selections={
                "ppt": [0, 1],
                "customer_manuscript": [0, 1],
                "reading_annotation": [0, 1],
                "demo_subtitles": [0, 1],
            },
            images=self.images,
            timings=self.timings,
        )
        atomic_write_json(path, payload)
        return path

    def make_package_dependencies(self, content: Path) -> dict[str, Path]:
        dependencies = {"product_content_manifest": content}
        names = (
            "customer_manuscript_receipt",
            "reading_annotation_receipt",
            "annotation_review",
            "ppt_with_subtitles_render_manifest",
            "ppt_without_subtitles_render_manifest",
            "demo_render_manifest",
            "music",
            "background_with_subtitles",
            "background_without_subtitles",
        )
        for name in names:
            path = self.root / f"dependency-{name}.json"
            if name.startswith("ppt_"):
                path.write_text('{"slides": []}', encoding="utf-8")
            else:
                path.write_text(f"fixture:{name}", encoding="utf-8")
            dependencies[name] = path
        return dependencies

    def test_content_manifest_is_deterministic_and_stales_on_source_or_plan(self) -> None:
        first = self.make_content_manifest()
        original = first.read_bytes()
        second = self.make_content_manifest()
        self.assertEqual(original, second.read_bytes())
        self.assertEqual(product_content_manifest_issues(first), [])
        self.plan.write_text('{"changed": true}', encoding="utf-8")
        self.assertIn("product_content_semantic_plan_stale", product_content_manifest_issues(first))

    def test_content_manifest_records_explicit_semantic_plan_bindings(self) -> None:
        path = self.make_content_manifest()
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["artifact_semantic_plan_schema_version"], "1.0")
        self.assertTrue(payload["artifact_semantic_plan_sha256"])
        self.assertTrue(payload["artifact_semantic_plan_dependency_sha256"])
        self.assertEqual(payload["contract_projection_sha256"], self.semantic["contract_projection_sha256"])

    def test_content_manifest_hand_edit_is_rejected(self) -> None:
        path = self.make_content_manifest()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["selections"]["ppt"]["source_line_indices"] = [1]
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIn("product_content_manifest_tampered", product_content_manifest_issues(path))

    def test_annotation_indices_are_exact_and_per_block_faithful(self) -> None:
        blocks = [
            AnnotationBlock("一", "**第一句**。", "自然", ["轻轻讲。"], (4,)),
            AnnotationBlock("二", "第二句。", "自然", ["自然讲。"], (7,)),
        ]
        validate_annotation_coverage(blocks, self.lines, selected_source_indices=[4, 7])
        with self.assertRaises(ValueError):
            validate_annotation_coverage(blocks, self.lines, selected_source_indices=[7, 4])

    def test_annotation_receipt_stales_on_json_or_docx_change(self) -> None:
        content = self.make_content_manifest()
        source = self.root / "annotation.json"
        annotation_blocks = [{
            "title": "一",
            "marked_text": "**第一**句。第二句。",
            "emotion": "自然",
            "notes": ["轻轻讲。"],
            "source_line_indices": [0, 1],
        }]
        source.write_text(json.dumps({"blocks": annotation_blocks}, ensure_ascii=False), encoding="utf-8")
        docx = self.root / "annotation.docx"
        blocks = [AnnotationBlock("一", "**第一**句。第二句。", "自然", ["轻轻讲。"], (0, 1))]
        render_annotation_blocks_docx("故事", blocks, docx)
        receipt = self.root / "annotation_receipt.json"
        write_annotation_receipt(
            receipt,
            annotation_json=source,
            annotation_docx=docx,
            blocks=annotation_blocks,
            selected_indices=[0, 1],
            selected_lines=self.lines,
            content_manifest=content,
        )
        self.assertEqual(annotation_receipt_issues(receipt), [])
        source.write_text('{"blocks": [1]}', encoding="utf-8")
        self.assertIn("annotation_json_stale", annotation_receipt_issues(receipt))

    def test_annotation_receipt_rejects_presenter_identity_in_notes(self) -> None:
        content = self.make_content_manifest()
        source = self.root / "annotation.json"
        annotation_blocks = [{
            "title": "一",
            "marked_text": "第一句。第二句。",
            "emotion": "自然",
            "notes": ["大家好，我是测试老师。"],
            "source_line_indices": [0, 1],
        }]
        source.write_text(json.dumps({"blocks": annotation_blocks}, ensure_ascii=False), encoding="utf-8")
        docx = self.root / "annotation.docx"
        render_annotation_blocks_docx(
            "故事", [AnnotationBlock("一", "第一句。第二句。", "自然", annotation_blocks[0]["notes"], (0, 1))], docx
        )
        receipt = self.root / "annotation_receipt.json"
        write_annotation_receipt(
            receipt,
            annotation_json=source,
            annotation_docx=docx,
            blocks=annotation_blocks,
            selected_indices=[0, 1],
            selected_lines=self.lines,
            content_manifest=content,
        )
        self.assertIn("annotation_presenter_identity", annotation_receipt_issues(receipt))

    def test_annotation_review_requires_exact_per_block_evidence_and_p0_free_result(self) -> None:
        content = self.make_content_manifest()
        annotation = self.root / "annotation.json"
        annotation.write_text(json.dumps({"blocks": [
            {
                "title": "第一段",
                "marked_text": "**第一句**。",
                "emotion": "平静",
                "notes": ["自然讲述。"],
                "source_line_indices": [0],
            },
            {
                "title": "第二段",
                "marked_text": "第二句。",
                "emotion": "明快",
                "notes": ["动作清楚。"],
                "source_line_indices": [1],
            },
        ]}, ensure_ascii=False), encoding="utf-8")
        clean = {
            "critical_errors": [],
            "p0_errors": [],
            "evidence_matrix": [
                {
                    "source_line_indices": [0], "original_text": "第一句。", "fidelity": "忠实",
                    "emotion_fit": "贴合", "pause_emphasis_quality": "自然",
                    "performance_guidance_quality": "可执行", "passed": True,
                },
                {
                    "source_line_indices": [1], "original_text": "第二句。", "fidelity": "忠实",
                    "emotion_fit": "贴合", "pause_emphasis_quality": "自然",
                    "performance_guidance_quality": "可执行", "passed": True,
                },
            ],
        }
        self.assertEqual(annotation_review_payload_issues(
            clean, annotation_json=annotation, content_manifest=content
        ), [])
        missing = {**clean, "evidence_matrix": clean["evidence_matrix"][:1]}
        self.assertIn("annotation_review_evidence_coverage_mismatch", annotation_review_payload_issues(
            missing, annotation_json=annotation, content_manifest=content
        ))
        p0 = {**clean, "p0_errors": ["改写对白"]}
        self.assertIn("annotation_review_has_p0", annotation_review_payload_issues(
            p0, annotation_json=annotation, content_manifest=content
        ))

    def test_customer_manuscript_exact_content_and_forbidden_identity(self) -> None:
        content = self.make_content_manifest()
        docx = self.root / "story.docx"
        render_story_docx("故事", "\n".join(self.lines), docx)
        receipt = self.root / "manuscript_receipt.json"
        write_manuscript_receipt(
            receipt,
            manuscript=docx,
            story_name="故事",
            selected_indices=[0, 1],
            selected_lines=self.lines,
            content_manifest=content,
        )
        self.assertEqual(manuscript_receipt_issues(receipt), [])
        bad = self.root / "bad.docx"
        render_story_docx("故事", "我是绵羊姐姐。", bad)
        write_manuscript_receipt(
            receipt,
            manuscript=bad,
            story_name="故事",
            selected_indices=[0],
            selected_lines=["我是绵羊姐姐。"],
            content_manifest=content,
        )
        self.assertTrue(any("forbidden_token" in issue for issue in manuscript_receipt_issues(receipt)))

    def test_customer_manuscript_rejects_missing_repeated_and_placeholder_text(self) -> None:
        content = self.make_content_manifest()
        receipt = self.root / "manuscript_receipt.json"
        cases = (
            ("missing", [self.lines[0]], "manuscript_current_selection_mismatch"),
            ("repeated", [self.lines[0], self.lines[0], self.lines[1]], "manuscript_current_selection_mismatch"),
            ("placeholder", [self.lines[0], "____", self.lines[1]], "manuscript_forbidden_token:____"),
        )
        for label, lines, expected_issue in cases:
            with self.subTest(label=label):
                docx = self.root / f"{label}.docx"
                render_story_docx("故事", "\n".join(lines), docx)
                write_manuscript_receipt(
                    receipt,
                    manuscript=docx,
                    story_name="故事",
                    selected_indices=[0, 1],
                    selected_lines=self.lines,
                    content_manifest=content,
                )
                self.assertIn(expected_issue, manuscript_receipt_issues(receipt))

    def test_ppt_manifest_binds_slides_subtitles_images_timing_and_music(self) -> None:
        content = self.make_content_manifest()
        music = self.root / "music.mp3"
        music.write_bytes(b"offline-music-fixture")
        pptx = self.root / "with.pptx"
        build_story_ppt("故事", self.images, self.lines, self.timings, music, pptx, True, 2.0)
        rows = build_ppt_manifest_rows(self.images, self.lines, self.timings, [0, 1], 2.0, with_subtitles=True)
        manifest = self.root / "ppt.json"
        write_ppt_render_manifest(
            manifest,
            pptx=pptx,
            with_subtitles=True,
            rows=rows,
            music=music,
            content_manifest=content,
        )
        self.assertEqual(ppt_render_manifest_issues(manifest), [])
        self.images[0].write_bytes(b"changed")
        self.assertTrue(any("source_image_stale" in issue for issue in ppt_render_manifest_issues(manifest)))

    def test_no_subtitle_ppt_has_no_story_text(self) -> None:
        content = self.make_content_manifest()
        music = self.root / "music.mp3"
        music.write_bytes(b"offline-music-fixture")
        pptx = self.root / "clean.pptx"
        build_story_ppt("故事", self.images, self.lines, self.timings, music, pptx, False, 2.0)
        rows = build_ppt_manifest_rows(self.images, self.lines, self.timings, [0, 1], 2.0, with_subtitles=False)
        manifest = self.root / "clean.json"
        write_ppt_render_manifest(manifest, pptx=pptx, with_subtitles=False, rows=rows, music=music, content_manifest=content)
        self.assertEqual(ppt_render_manifest_issues(manifest), [])

    def test_ppt_evidence_covers_required_roles_and_is_bound_by_manifest(self) -> None:
        content = self.make_content_manifest()
        music = self.root / "music.mp3"
        music.write_bytes(b"offline-music-fixture")
        pptx = self.root / "with-evidence.pptx"
        long_lines = ["第一句。", "这是一个用于验证最长字幕页和安全边界的较长句子。"]
        long_timings = [timing(1, long_lines[0], 0.0), timing(2, long_lines[1], 1.0)]
        build_story_ppt("故事", self.images, long_lines, long_timings, music, pptx, True, 2.0)
        rows = build_ppt_manifest_rows(self.images, long_lines, long_timings, [0, 1], 2.0, with_subtitles=True)
        evidence = render_ppt_evidence(self.root / "evidence", rows, with_subtitles=True)
        names = {path.name for path in evidence}
        for prefix in ("first_", "middle_", "last_", "longest_subtitle_", "boundary_"):
            self.assertTrue(any(name.startswith(prefix) for name in names), prefix)
        self.assertIn("contact_sheet.png", names)
        manifest = self.root / "ppt-evidence.json"
        write_ppt_render_manifest(
            manifest, pptx=pptx, with_subtitles=True, rows=rows, music=music,
            content_manifest=content, evidence=evidence,
        )
        # The fixture intentionally changes display text but keeps the same semantic rows in
        # content, so selection QA must reject it while evidence itself remains current.
        issues = ppt_render_manifest_issues(manifest)
        self.assertIn("ppt_content_selection_mismatch", issues)
        evidence[0].write_bytes(b"changed")
        self.assertIn("ppt_evidence_stale", ppt_render_manifest_issues(manifest))

    def test_ppt_duplicate_source_index_and_non_monotonic_timing_fail(self) -> None:
        content = self.make_content_manifest()
        music = self.root / "music.mp3"
        music.write_bytes(b"offline-music-fixture")
        pptx = self.root / "with.pptx"
        build_story_ppt("故事", self.images, self.lines, self.timings, music, pptx, True, 2.0)
        rows = build_ppt_manifest_rows(self.images, self.lines, self.timings, [0, 1], 2.0, with_subtitles=True)
        rows[1]["source_line_index"] = 0
        rows[1]["timing_start"] = -1
        manifest = self.root / "ppt-invalid.json"
        write_ppt_render_manifest(manifest, pptx=pptx, with_subtitles=True, rows=rows, music=music, content_manifest=content)
        issues = ppt_render_manifest_issues(manifest)
        self.assertIn("ppt_source_line_duplicate", issues)
        self.assertTrue(any(issue.startswith("ppt_timing_invalid") for issue in issues))

    def test_product_package_manifest_rejects_stale_and_internal_files(self) -> None:
        root = self.root / "06_资料包"
        base = root / "base"
        advanced = root / "advanced"
        base.mkdir(parents=True)
        advanced.mkdir()
        base_names = ["故事文稿.docx", "故事配乐.mp3", "朗读标注.docx", "示范表演.mp4", "背景图片.png"]
        advanced_names = [*base_names, "背景视频（含字幕）.mp4", "背景视频（无字幕）.mp4", "故事PPT（含字幕）.pptx", "故事PPT（无字幕）.pptx", "A镜无人物背景视频.mp4"]
        source_map = {}
        for package, directory, names in (("base", base, base_names), ("advanced", advanced, advanced_names)):
            for name in names:
                source = self.root / f"source-{package}-{name}"
                source.write_bytes(name.encode())
                target = directory / name
                target.write_bytes(source.read_bytes())
                source_map[f"{package}:{name}"] = source
        dependency = self.make_content_manifest()
        manifest = self.root / "package.json"
        write_product_package_manifest(
            manifest,
            product_root=root,
            base_dir=base,
            advanced_dir=advanced,
            dependencies=self.make_package_dependencies(dependency),
            source_map=source_map,
        )
        self.assertEqual(product_package_manifest_issues(manifest), [])
        (base / ".DS_Store").write_bytes(b"junk")
        payload = json.loads(manifest.read_text())
        payload["files"].append({"package": "base", "relative_path": "base/.DS_Store", "role": "unknown", "sha256": "x", "source_path": str(dependency), "source_sha256": "x"})
        manifest.write_text(json.dumps(payload))
        self.assertTrue(any("internal_file_leak" in issue for issue in product_package_manifest_issues(manifest)))

    def test_product_package_manifest_detects_shared_asset_and_output_replacement(self) -> None:
        root = self.root / "06_资料包"
        base = root / "base"
        advanced = root / "advanced"
        base.mkdir(parents=True)
        advanced.mkdir()
        base_names = ["故事文稿.docx", "故事配乐.mp3", "朗读标注.docx", "示范表演.mp4", "背景图片.png"]
        advanced_names = [*base_names, "背景视频（含字幕）.mp4", "背景视频（无字幕）.mp4", "故事PPT（含字幕）.pptx", "故事PPT（无字幕）.pptx", "A镜无人物背景视频.mp4"]
        source_map: dict[str, Path] = {}
        for package, directory, names in (("base", base, base_names), ("advanced", advanced, advanced_names)):
            for name in names:
                source = self.root / f"source-{package}-{name}"
                source.write_bytes((f"{package}:{name}" if name == "故事文稿.docx" else name).encode())
                (directory / name).write_bytes(source.read_bytes())
                source_map[f"{package}:{name}"] = source
        content = self.make_content_manifest()
        manifest = self.root / "package-currentness.json"
        write_product_package_manifest(
            manifest, product_root=root, base_dir=base, advanced_dir=advanced,
            dependencies=self.make_package_dependencies(content), source_map=source_map,
        )
        issues = product_package_manifest_issues(manifest)
        self.assertIn("product_shared_asset_mismatch:customer_manuscript", issues)
        (advanced / "故事文稿.docx").write_bytes(b"replaced")
        self.assertIn(
            "product_file_stale:advanced/故事文稿.docx",
            product_package_manifest_issues(manifest),
        )

    def test_product_review_requires_every_ppt_evidence_and_rejects_p0(self) -> None:
        root = self.root / "ppt_evidence"
        first = root / "with_subtitles" / "first_slide_001.png"
        second = root / "without_subtitles" / "contact_sheet.png"
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        Image.new("RGB", (32, 18), "white").save(first)
        Image.new("RGB", (32, 18), "white").save(second)
        clean = {
            "critical_errors": [],
            "p0_errors": [],
            "evidence_matrix": [
                {"relative_path": "with_subtitles/first_slide_001.png"},
                {"relative_path": "without_subtitles/contact_sheet.png"},
            ],
        }
        self.assertEqual(
            product_package_review_payload_issues(clean, evidence_root=root, required_evidence=[first, second]),
            [],
        )
        missing = {**clean, "evidence_matrix": clean["evidence_matrix"][:1]}
        self.assertTrue(any("evidence_missing" in issue for issue in product_package_review_payload_issues(
            missing, evidence_root=root, required_evidence=[first, second]
        )))
        p0 = {**clean, "p0_errors": ["subtitle pollution"]}
        self.assertIn(
            "product_package_review_has_p0",
            product_package_review_payload_issues(p0, evidence_root=root, required_evidence=[first, second]),
        )

    def test_product_package_review_passes_ppt_evidence_as_native_images(self) -> None:
        project = self.root / "project"
        manifest = init_project(project, story_name="通用故事", slug="generic-story")
        paths = project_paths(project)
        base = paths.product / "基础版"
        advanced = paths.product / "进阶版"
        base.mkdir(parents=True)
        advanced.mkdir(parents=True)
        (base / "story.txt").write_text("正文", encoding="utf-8")
        (advanced / "story.txt").write_text("正文", encoding="utf-8")
        manifest.setdefault("outputs", {})["product_base"] = str(base)
        manifest["outputs"]["product_advanced"] = str(advanced)

        evidence_root = paths.status / "product_package_work" / "ppt_evidence"
        expected_images = [
            evidence_root / "with_subtitles" / "first.png",
            evidence_root / "without_subtitles" / "contact_sheet.png",
        ]
        for index, image in enumerate(expected_images):
            image.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (320, 180), (80 + index * 20, 110, 140)).save(image)

        report = paths.status / "qa_product_report.md"
        report.write_text("PASS", encoding="utf-8")
        report_json = paths.status / "qa_product_report.json"
        report_json.write_text(json.dumps({
            "passed": True,
            "artifacts": {"report": {"path": str(report), "sha256": file_sha256(report)}},
        }), encoding="utf-8")
        context = AgentContext(
            project_dir=project,
            inbox=None,
            story_name="通用故事",
            slug="generic-story",
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
        review_payload = {"approved": True, "score": 95, "critical_errors": []}
        with (
            patch.object(agent, "_workflow", return_value=StageResult("done", "qa")),
            patch.object(
                agent,
                "_structured_review",
                return_value=(StageResult("done", "review"), review_payload),
            ) as structured_review,
            patch("product_quality.product_package_review_payload_issues", return_value=[]),
        ):
            result = agent._stage_product_package_review(manifest)

        self.assertEqual(result.status, "done")
        self.assertEqual(structured_review.call_args.kwargs["images"], sorted(expected_images))


if __name__ == "__main__":
    unittest.main()
