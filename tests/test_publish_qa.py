from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from story_agent import AgentContext, StageResult, StoryAgent
from story_agent_runtime import file_sha256
from cover_quality import (
    COVER_GRAPH,
    cover_relative,
    cover_review_image_paths,
    cover_review_payload_issues,
    expand_retry_files,
    file_sha256 as cover_file_sha256,
    required_cover_issues,
)
from story_codex_tasks import build_publish_package_agent_prompt
from story_project import apply_fixed_cover_branding, init_project, load_manifest, project_paths, qa_publish, save_json


def make_publish_fixture(project: Path, *, mechanical: bool = False) -> None:
    paths = project_paths(project)
    init_project(project, story_name="发布QA", slug="publish-qa")
    sizes = {"3x4": (900, 1200), "4x3": (1200, 900), "16x9": (1280, 720)}
    colors = {
        "main": {"3x4": (60, 100, 170), "4x3": (150, 80, 120), "16x9": (70, 160, 105)},
        "library": {"3x4": (180, 120, 70), "4x3": (75, 130, 180), "16x9": (145, 75, 165)},
    }
    for account in ("main", "library"):
        copy = paths.publish / account / "copy.md"
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text("# 发布标题\n\n这里是完整正文内容，介绍故事亮点和适龄信息。\n\n#儿童故事 #亲子阅读\n", encoding="utf-8")
        for ratio, size in sizes.items():
            cover = paths.publish / account / "covers" / f"cover_{ratio}.png"
            cover.parent.mkdir(parents=True, exist_ok=True)
            color = (90, 120, 150) if mechanical else colors[account][ratio]
            image = Image.new("RGB", size, color)
            if not mechanical:
                draw = ImageDraw.Draw(image)
                draw.rectangle((size[0] // 5, size[1] // 5, size[0] // 2, size[1] // 2), fill=(240, 210, 120))
            image.save(cover)


def make_required_cover_fixture(project: Path, root: Path) -> tuple[Path, Path]:
    paths = project_paths(project)
    init_project(project, story_name="通用故事标题", slug="required-cover")
    manifest = load_manifest(paths)
    manifest.setdefault("agent", {}).setdefault("story_contract", {})["policy"] = "required_v1"
    save_json(paths.manifest, manifest)
    logo = root / "official-logo.png"
    Image.new("RGBA", (180, 60), (220, 80, 20, 255)).save(logo)
    sizes = {"3x4": (900, 1200), "4x3": (1200, 900), "16x9": (1280, 720)}
    entries = []
    for index, (asset_id, parent_id) in enumerate(COVER_GRAPH.items()):
        account, ratio = asset_id.split(":")
        base = paths.publish / cover_relative(account, ratio, creative=True)
        base.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", sizes[ratio], (45 + index * 22, 100 + index * 11, 155 - index * 9)).save(base)
        parent_sha = None
        if parent_id:
            pa, pr = parent_id.split(":")
            parent_sha = cover_file_sha256(paths.publish / cover_relative(pa, pr, creative=True))
        entries.append({
            "asset_id": asset_id,
            "parent_asset_id": parent_id,
            "generation_mode": "root_master" if parent_id is None else ("branch_master" if asset_id == "library:4x3" else "edit_derived"),
            "reference_files": ["current_story_reference.png"],
            "creative_base_sha256": cover_file_sha256(base),
            "parent_sha256": parent_sha,
        })
    save_json(paths.publish / "cover_creative_lineage.json", {"version": 1, "covers": entries})
    variants = []
    for ratio in ("3:4", "4:3", "16:9"):
        variants.append({
            "variant_id": f"cover-{ratio}", "aspect_ratio": ratio,
            "regions": [
                {"role": "logo", "x": .38, "y": .02, "width": .24, "height": .06},
                {"role": "title_safe", "x": .12, "y": .11, "width": .76, "height": .18},
                {"role": "secondary_info", "x": .20, "y": .32, "width": .60, "height": .07},
                {"role": "usage_info", "x": .08, "y": .89, "width": .84, "height": .06},
            ],
        })
    compiled = {
        "version": 1, "consumer": "cover", "contract_schema_version": "1.0.0",
        "story_contract_sha256": "1" * 64, "story_contract_dependency_sha256": "2" * 64,
        "contract_projection_sha256": "3" * 64,
        "official_assets": [{"asset_id": "official", "sha256": cover_file_sha256(logo), "max_per_frame": 1, "allowed_uses": ["cover"]}],
        "brand_rules": [], "semantic_artifacts": {}, "visual_style": {}, "characters": {},
        "layout_rules": [], "variants": variants,
    }
    compiled_path = paths.status / "contracts" / "consumers" / "cover.compiled.json"
    save_json(compiled_path, compiled)
    for account in ("main", "library"):
        copy = paths.publish / account / "copy.md"
        copy.write_text("# 标题\n\n完整发布文案内容。\n\n#儿童故事\n", encoding="utf-8")
    return logo, compiled_path


def agent_context(project: Path) -> AgentContext:
    return AgentContext(
        project_dir=project,
        inbox=None,
        story_name="通用故事标题",
        slug="required-cover",
        execute=True,
        update_latest_episode=False,
        codex_mode="cli",
        codex_model="gpt-5.6-sol",
        codex_sandbox="workspace-write",
        codex_approval="never",
        codex_path="codex",
        codex_timeout=30,
    )


class PublishQaTests(unittest.TestCase):
    def test_publish_stage_passes_twelve_originals_and_contact_sheet_to_native_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：原生视觉输入"
            logo, compiled_path = make_required_cover_fixture(project, root)
            with patch("story_project.load_config", return_value={"brand_assets": {"logo": str(logo)}}):
                apply_fixed_cover_branding(project, contract_spec=compiled_path)
            paths = project_paths(project)
            qa_report = paths.status / "qa_publish_report.md"
            qa_json = paths.status / "qa_publish_report.json"
            qa_report.write_text("离线机器 QA fixture", encoding="utf-8")
            qa_json.write_text('{"passed": true, "artifacts": {}}', encoding="utf-8")
            captured: dict[str, object] = {}
            expected = [
                cover_relative(account, ratio, creative=creative)
                for creative in (False, True)
                for account in ("main", "library")
                for ratio in ("3x4", "4x3", "16x9")
            ]

            def fake_review(**kwargs):
                captured.update(kwargs)
                payload = {
                    "p0_errors": [],
                    "evidence_matrix": [{"asset": asset, "result": "pass"} for asset in expected],
                }
                return StageResult("done", "offline review"), payload

            agent = StoryAgent(agent_context(project))
            manifest = load_manifest(paths)
            with (
                patch.object(agent, "_workflow", return_value=StageResult("done", "offline qa")),
                patch.object(agent, "_json_qa_report_passes", return_value=True),
                patch.object(agent, "_structured_review", side_effect=fake_review),
            ):
                result = agent._stage_publish_package_review(manifest)

            self.assertEqual(result.status, "done")
            native_images = captured["images"]
            self.assertIsInstance(native_images, list)
            original_paths = [path for path in native_images if path.is_relative_to(paths.publish)]
            self.assertEqual(
                {str(path.relative_to(paths.publish)) for path in original_paths},
                set(expected),
            )
            self.assertEqual(len(original_paths), 12)
            self.assertEqual(len(native_images), 13)
            self.assertEqual(native_images[-1].name, "publish_covers_contact_sheet.jpg")

    def test_required_native_review_receives_six_final_and_six_creative_originals(self) -> None:
        publish = Path("/tmp/neutral-publish")
        required = cover_review_image_paths(publish, required_v1=True)
        self.assertEqual(len(required), 12)
        self.assertEqual(
            {str(path.relative_to(publish)) for path in required[:6]},
            {cover_relative(account, ratio) for account in ("main", "library") for ratio in ("3x4", "4x3", "16x9")},
        )
        self.assertEqual(
            {str(path.relative_to(publish)) for path in required[6:]},
            {cover_relative(account, ratio, creative=True) for account in ("main", "library") for ratio in ("3x4", "4x3", "16x9")},
        )
        legacy = cover_review_image_paths(publish, required_v1=False)
        self.assertEqual(len(legacy), 6)
        self.assertFalse(any("creative_base_" in path.name for path in legacy))

    def test_required_review_requires_twelve_assets_and_blocks_creative_p0(self) -> None:
        expected = [
            cover_relative(account, ratio, creative=creative)
            for creative in (False, True)
            for account in ("main", "library")
            for ratio in ("3x4", "4x3", "16x9")
        ]
        clean = {
            "p0_errors": [],
            "evidence_matrix": [{"asset": asset, "result": "pass"} for asset in expected],
        }
        self.assertEqual(cover_review_payload_issues(clean, expected), [])
        missing = dict(clean)
        missing["evidence_matrix"] = clean["evidence_matrix"][:-1]
        self.assertTrue(any("creative_base_16x9.png" in item for item in cover_review_payload_issues(missing, expected)))
        for p0 in ("creative_base_fake_text", "creative_base_fake_logo"):
            rejected = dict(clean)
            rejected["p0_errors"] = [p0]
            self.assertTrue(any("P0" in item for item in cover_review_payload_issues(rejected, expected)))

    def test_required_review_evidence_distinguishes_same_named_account_assets(self) -> None:
        expected = [
            cover_relative(account, ratio)
            for account in ("main", "library")
            for ratio in ("3x4", "4x3", "16x9")
        ]
        main_only = {
            "p0_errors": [],
            "evidence_matrix": [
                {"asset": cover_relative("main", ratio), "result": "pass"}
                for ratio in ("3x4", "4x3", "16x9")
            ],
        }
        issues = cover_review_payload_issues(main_only, expected)
        self.assertTrue(any("library/covers/cover_3x4.png" in item for item in issues))
        complete = {
            "p0_errors": [],
            "evidence_matrix": [{"asset": asset, "result": "pass"} for asset in expected],
        }
        self.assertEqual(cover_review_payload_issues(complete, expected), [])
        self.assertTrue(cover_review_payload_issues(None, expected))
        complete["p0_errors"] = ["fake_logo"]
        self.assertTrue(any("P0" in item for item in cover_review_payload_issues(complete, expected)))

    def test_required_prompt_requests_text_free_bases_and_deterministic_branding(self) -> None:
        prompt = build_publish_package_agent_prompt(Path("handoff.md"), Path("project"), required_v1=True)
        self.assertIn("creative_base_4x3.png", prompt)
        self.assertIn("严禁任何可读文字", prompt)
        self.assertIn("正式文字与唯一官方 Logo 由 Runtime 确定性排版", prompt)
        self.assertIn("cover_creative_lineage.json", prompt)
        self.assertIn("semantic_artifacts", prompt)
        self.assertIn("visual_style", prompt)

    def test_required_cover_renderer_adds_exact_title_logo_and_current_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：合同封面"
            logo, compiled_path = make_required_cover_fixture(project, root)
            with patch("story_project.load_config", return_value={"brand_assets": {"logo": str(logo)}}):
                apply_fixed_cover_branding(project, contract_spec=compiled_path)
            paths = project_paths(project)
            compiled = json.loads(compiled_path.read_text(encoding="utf-8"))
            issues, retries = required_cover_issues(
                paths.publish,
                render_manifest_path=paths.publish / "cover_render_manifest.json",
                lineage_path=paths.publish / "cover_lineage.json",
                compiled_spec=compiled,
                expected_title="通用故事标题",
            )
            self.assertEqual(issues, [])
            self.assertEqual(retries, [])
            render = json.loads((paths.publish / "cover_render_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(set(render["covers"]), set(COVER_GRAPH))
            self.assertTrue(all(item["official_logo_count"] == 1 for item in render["covers"].values()))
            self.assertTrue(all(item["title_text"] == "通用故事标题" for item in render["covers"].values()))

    def test_required_cover_lineage_tamper_and_branch_retry_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：血缘失效"
            logo, compiled_path = make_required_cover_fixture(project, root)
            with patch("story_project.load_config", return_value={"brand_assets": {"logo": str(logo)}}):
                apply_fixed_cover_branding(project, contract_spec=compiled_path)
            paths = project_paths(project)
            branch = paths.publish / cover_relative("library", "4x3", creative=True)
            Image.new("RGB", (1200, 900), (1, 2, 3)).save(branch)
            compiled = json.loads(compiled_path.read_text(encoding="utf-8"))
            issues, retries = required_cover_issues(
                paths.publish,
                render_manifest_path=paths.publish / "cover_render_manifest.json",
                lineage_path=paths.publish / "cover_lineage.json",
                compiled_spec=compiled,
                expected_title="通用故事标题",
            )
            self.assertTrue(issues)
            expected = expand_retry_files([cover_relative("library", "4x3", creative=True)])
            self.assertEqual(expected, [
                "library/covers/cover_16x9.png", "library/covers/cover_3x4.png", "library/covers/cover_4x3.png",
                "library/covers/creative_base_16x9.png", "library/covers/creative_base_3x4.png", "library/covers/creative_base_4x3.png",
            ])
            self.assertNotIn("main/covers/cover_4x3.png", retries)

    def test_root_master_retry_expands_to_all_six_cover_families(self) -> None:
        expanded = expand_retry_files([cover_relative("main", "4x3", creative=True)])
        self.assertEqual(len(expanded), 12)
        self.assertIn("main/covers/cover_4x3.png", expanded)
        self.assertIn("library/covers/cover_16x9.png", expanded)
        self.assertIn("library/covers/creative_base_3x4.png", expanded)

    def test_required_cover_rejects_brand_projection_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：品牌投影失效"
            logo, compiled_path = make_required_cover_fixture(project, root)
            with patch("story_project.load_config", return_value={"brand_assets": {"logo": str(logo)}}):
                apply_fixed_cover_branding(project, contract_spec=compiled_path)
            compiled = json.loads(compiled_path.read_text(encoding="utf-8"))
            compiled["brand_rules"] = [{"rule": "new reviewed placement"}]
            issues, retries = required_cover_issues(
                project_paths(project).publish,
                render_manifest_path=project_paths(project).publish / "cover_render_manifest.json",
                lineage_path=project_paths(project).publish / "cover_lineage.json",
                compiled_spec=compiled,
                expected_title="通用故事标题",
            )
            self.assertIn("品牌投影绑定已失效", "\n".join(issues))
            self.assertEqual(len([item for item in retries if "creative_base" not in item]), 6)

    def test_required_cover_blocks_impossible_title_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：标题安全区"
            logo, compiled_path = make_required_cover_fixture(project, root)
            paths = project_paths(project)
            manifest = load_manifest(paths)
            manifest["story"]["name"] = "极长标题" * 120
            save_json(paths.manifest, manifest)
            compiled = json.loads(compiled_path.read_text(encoding="utf-8"))
            for variant in compiled["variants"]:
                for region in variant["regions"]:
                    if region["role"] == "title_safe":
                        region["width"] = .03
                        region["height"] = .03
            save_json(compiled_path, compiled)
            with patch("story_project.load_config", return_value={"brand_assets": {"logo": str(logo)}}):
                with self.assertRaisesRegex(ValueError, "无法在合同安全区"):
                    apply_fixed_cover_branding(project, contract_spec=compiled_path)

    def test_required_cover_rejects_wrong_official_logo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：品牌真实性"
            _logo, compiled_path = make_required_cover_fixture(project, root)
            wrong = root / "wrong.png"
            Image.new("RGBA", (180, 60), (0, 0, 0, 255)).save(wrong)
            with patch("story_project.load_config", return_value={"brand_assets": {"logo": str(wrong)}}):
                with self.assertRaisesRegex(ValueError, "官方 Logo"):
                    apply_fixed_cover_branding(project, contract_spec=compiled_path)
    def test_fixed_branding_overlays_exact_logo_once_and_writes_hash_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "故事剪辑：品牌定版"
            make_publish_fixture(project)
            logo = root / "logo.png"
            Image.new("RGBA", (240, 80), (255, 0, 0, 220)).save(logo)
            with patch("story_project.load_config", return_value={"brand_assets": {"logo": str(logo)}}):
                receipt = apply_fixed_cover_branding(project)
                first = json.loads(receipt.read_text(encoding="utf-8"))
                hashes = {path: item["output_sha256"] for path, item in first["covers"].items()}
                apply_fixed_cover_branding(project)
                second = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(first["logo_sha256"], file_sha256(logo))
            self.assertEqual(hashes, {path: item["output_sha256"] for path, item in second["covers"].items()})

    def test_passes_six_distinct_correct_ratio_covers_and_two_copies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：发布QA"
            make_publish_fixture(project)
            qa_publish(project)
            payload = json.loads((project_paths(project).status / "qa_publish_report.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["passed"], payload["issues"])
            self.assertEqual(len(payload["artifacts"]), 8)

    def test_rejects_wrong_ratio_and_mechanically_resized_master(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：机械封面"
            make_publish_fixture(project, mechanical=True)
            wrong = project_paths(project).publish / "main" / "covers" / "cover_16x9.png"
            Image.new("RGB", (900, 1200), (90, 120, 150)).save(wrong)
            qa_publish(project)
            payload = json.loads((project_paths(project).status / "qa_publish_report.json").read_text(encoding="utf-8"))
            self.assertFalse(payload["passed"])
            combined = "\n".join(payload["issues"])
            self.assertIn("比例错误", combined)
            self.assertIn("机械缩放", combined)
            self.assertTrue(payload["retry_files"])

    def test_requires_current_master_edit_lineage_for_new_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "故事剪辑：封面血缘"
            make_publish_fixture(project)
            paths = project_paths(project)
            (paths.publish / "publish_package_codex_handoff.md").write_text("必须写 cover_lineage.json\n", encoding="utf-8")
            qa_publish(project)
            missing = json.loads((paths.status / "qa_publish_report.json").read_text(encoding="utf-8"))
            self.assertFalse(missing["passed"])
            self.assertIn("缺少 cover_lineage.json", "\n".join(missing["issues"]))

            parents = {
                "main/covers/cover_4x3.png": None,
                "main/covers/cover_3x4.png": "main/covers/cover_4x3.png",
                "main/covers/cover_16x9.png": "main/covers/cover_4x3.png",
                "library/covers/cover_4x3.png": "main/covers/cover_4x3.png",
                "library/covers/cover_3x4.png": "library/covers/cover_4x3.png",
                "library/covers/cover_16x9.png": "library/covers/cover_4x3.png",
            }
            covers = []
            for relative, parent in parents.items():
                path = paths.publish / relative
                covers.append(
                    {
                        "path": relative,
                        "parent": parent,
                        "parent_sha256": file_sha256(paths.publish / parent) if parent else None,
                        "generation_mode": "edit-derived" if parent else "master",
                        "reference_files": ["current_story_reference.png"],
                        "sha256": file_sha256(path),
                    }
                )
            (paths.publish / "cover_lineage.json").write_text(json.dumps({"version": 1, "covers": covers}, ensure_ascii=False), encoding="utf-8")
            qa_publish(project)
            passed = json.loads((paths.status / "qa_publish_report.json").read_text(encoding="utf-8"))
            self.assertTrue(passed["passed"], passed["issues"])

            stale = json.loads((paths.publish / "cover_lineage.json").read_text(encoding="utf-8"))
            stale["covers"][0]["sha256"] = "0" * 64
            (paths.publish / "cover_lineage.json").write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")
            qa_publish(project)
            rejected = json.loads((paths.status / "qa_publish_report.json").read_text(encoding="utf-8"))
            self.assertFalse(rejected["passed"])
            self.assertIn("当前哈希失效", "\n".join(rejected["issues"]))


if __name__ == "__main__":
    unittest.main()
