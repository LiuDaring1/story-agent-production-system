import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from PIL import Image

from run_image_video_jobs import create_provider_task, row_reference_paths
from shot_storyboard_pipeline import (
    StoryboardPipelineError,
    build_storyboard_manifest,
    compile_consumers,
    create_asset_bundle,
    seal_storyboard_manifest,
    validate_compile_receipt,
)
from story_video_synthesizer.image_video import validate_image_video_jobs
from story_video_synthesizer.toapis_video import ToAPIsVideoClient
from tests.test_story_r2v_skill import valid_plan


STATIC_VALIDATOR_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills/story-full-auto/scripts/validate_static_ppt_plan.py"
)
STATIC_SPEC = importlib.util.spec_from_file_location(
    "static_ppt_storyboard_validator", STATIC_VALIDATOR_PATH
)
STATIC_VALIDATOR = importlib.util.module_from_spec(STATIC_SPEC)
assert STATIC_SPEC and STATIC_SPEC.loader
STATIC_SPEC.loader.exec_module(STATIC_VALIDATOR)


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class ShotStoryboardPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.plan = valid_plan()
        for index, asset in enumerate(self.plan["assets"], start=1):
            path = self.root / "assets" / f"asset-{index:02d}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (256, 256), (20 * index, 80, 120)).save(path)
            asset["path"] = str(path)
            import hashlib

            asset["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.director = self.root / "director.json"
        write_json(self.director, self.plan)
        self.asset_bundle = self.root / "asset_bundle.json"
        bundle = create_asset_bundle(self.director, self.asset_bundle)
        self.asset_review = self.root / "asset_review.json"
        write_json(
            self.asset_review,
            {
                "approved": True,
                "score": 92,
                "critical_errors": [],
                "artifact_sha256": bundle["asset_bundle_sha256"],
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build_and_seal(self) -> tuple[Path, dict]:
        planned = self.root / "storyboards_planned.json"
        images = self.root / "storyboards"
        plan = build_storyboard_manifest(
            self.director,
            self.asset_bundle,
            self.asset_review,
            images,
            planned,
        )
        self.assertEqual(plan["ordered_shot_ids"], ["shot-001"])
        Image.new("RGB", (1600, 900), (120, 160, 200)).save(images / "shot-001.png")
        sealed_path = self.root / "storyboards_sealed.json"
        sealed = seal_storyboard_manifest(planned, sealed_path)
        return sealed_path, sealed

    def test_one_manifest_compiles_matching_ppt_r2v_plan_and_provider_jobs(self) -> None:
        sealed_path, sealed = self.build_and_seal()
        storyboard_review = self.root / "storyboard_review.json"
        write_json(
            storyboard_review,
            {
                "approved": True,
                "score": 94,
                "critical_errors": [],
                "artifact_sha256": sealed["storyboard_bundle_sha256"],
            },
        )
        previous_ppt = self.root / "previous_ppt.json"
        title = self.root / "title.png"
        Image.new("RGB", (1600, 900), (40, 60, 80)).save(title)
        import hashlib

        write_json(
            previous_ppt,
            {
                "story_name": "sample-story",
                "music_path": "/music.mp3",
                "music_sha256": "d" * 64,
                "slides": [
                    {
                        "shot_id": "TITLE",
                        "poster_path": str(title),
                        "poster_sha256": hashlib.sha256(title.read_bytes()).hexdigest(),
                        "duration_seconds": 2.0,
                    },
                    {"shot_id": "shot-001", "poster_path": "/old.png", "duration_seconds": 10.0},
                ],
            },
        )
        r2v = self.root / "compiled_r2v.json"
        jobs = self.root / "r2v_jobs.csv"
        ppt = self.root / "ppt_plan.json"
        receipt = self.root / "compile_receipt.json"
        compile_consumers(
            sealed_path,
            storyboard_review,
            r2v,
            jobs,
            receipt,
            previous_ppt,
            ppt,
        )

        compiled = json.loads(r2v.read_text(encoding="utf-8"))
        shot = compiled["shots"][0]
        self.assertEqual(shot["reference_asset_ids"][-1], "storyboard__shot-001")
        self.assertIn("[SEMANTIC_STORYBOARD_V1]", shot["prompt"])
        storyboard_asset = compiled["assets"][-1]
        self.assertEqual(storyboard_asset["kind"], "storyboard")
        self.assertEqual(storyboard_asset["sha256"], sealed["entries"][0]["image_sha256"])

        with jobs.open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        references = json.loads(row["reference_image_paths_json"])
        self.assertEqual(references[-1], sealed["entries"][0]["image_path"])
        self.assertEqual(row["generation_mode"], "reference_to_video")
        self.assertEqual(validate_image_video_jobs(jobs), [])

        ppt_plan = json.loads(ppt.read_text(encoding="utf-8"))
        self.assertEqual([row["shot_id"] for row in ppt_plan["slides"]], ["TITLE", "shot-001"])
        self.assertEqual(ppt_plan["slides"][1]["poster_path"], references[-1])
        expected_ids, _slides = STATIC_VALIDATOR.validate_plan(self.director, ppt)
        self.assertEqual(expected_ids, ["TITLE", "shot-001"])
        compile_receipt = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(compile_receipt["ordered_shot_ids"], ["shot-001"])
        self.assertEqual(validate_compile_receipt(receipt)["shot_count"], 1)
        jobs.write_text(jobs.read_text(encoding="utf-8-sig") + "\n", encoding="utf-8-sig")
        with self.assertRaisesRegex(StoryboardPipelineError, "r2v_jobs_csv_path"):
            validate_compile_receipt(receipt)
        self.assertEqual(
            validate_compile_receipt(receipt, require_current_r2v_jobs=False)["shot_count"],
            1,
        )

    def test_storyboard_review_must_bind_sealed_bundle(self) -> None:
        sealed_path, _sealed = self.build_and_seal()
        bad_review = self.root / "storyboard_review.json"
        write_json(
            bad_review,
            {
                "approved": True,
                "score": 95,
                "critical_errors": [],
                "artifact_sha256": "0" * 64,
            },
        )
        with self.assertRaisesRegex(StoryboardPipelineError, "未绑定当前"):
            compile_consumers(
                sealed_path,
                bad_review,
                self.root / "r2v.json",
                self.root / "jobs.csv",
                self.root / "receipt.json",
            )

    def test_result_heavy_storyboard_can_remain_director_only(self) -> None:
        self.plan["shots"][0]["storyboard_reference_mode"] = "director_only"
        self.plan["shots"][0]["storyboard_reference_reason"] = (
            "故事板呈现镜头末尾结果，运行时入口需要完整过程"
        )
        write_json(self.director, self.plan)
        bundle = create_asset_bundle(self.director, self.asset_bundle)
        write_json(
            self.asset_review,
            {
                "approved": True,
                "score": 92,
                "critical_errors": [],
                "artifact_sha256": bundle["asset_bundle_sha256"],
            },
        )
        sealed_path, sealed = self.build_and_seal()
        storyboard_review = self.root / "storyboard_review.json"
        write_json(
            storyboard_review,
            {
                "approved": True,
                "score": 94,
                "critical_errors": [],
                "artifact_sha256": sealed["storyboard_bundle_sha256"],
            },
        )
        r2v = self.root / "compiled_r2v.json"
        jobs = self.root / "r2v_jobs.csv"
        compile_consumers(
            sealed_path,
            storyboard_review,
            r2v,
            jobs,
            self.root / "receipt.json",
        )
        compiled = json.loads(r2v.read_text(encoding="utf-8"))
        shot = compiled["shots"][0]
        self.assertEqual(shot["storyboard_reference_mode"], "director_only")
        self.assertNotIn("storyboard__shot-001", shot["reference_asset_ids"])
        self.assertNotIn("[SEMANTIC_STORYBOARD_V1]", shot["prompt"])
        with jobs.open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        references = json.loads(row["reference_image_paths_json"])
        self.assertNotIn(sealed["entries"][0]["image_path"], references)
        self.assertEqual(row["storyboard_image_path"], sealed["entries"][0]["image_path"])
        self.assertEqual(row["storyboard_reference_mode"], "director_only")
        self.assertEqual(validate_image_video_jobs(jobs), [])
        self.assertEqual(row_reference_paths(row), [Path(value).resolve() for value in references])

    def test_modified_storyboard_blocks_compilation_and_paid_job_validation(self) -> None:
        sealed_path, sealed = self.build_and_seal()
        storyboard_review = self.root / "storyboard_review.json"
        write_json(
            storyboard_review,
            {
                "approved": True,
                "score": 90,
                "critical_errors": [],
                "artifact_sha256": sealed["storyboard_bundle_sha256"],
            },
        )
        Image.new("RGB", (1600, 900), (255, 0, 0)).save(Path(sealed["entries"][0]["image_path"]))
        with self.assertRaisesRegex(StoryboardPipelineError, "图片已变化"):
            compile_consumers(
                sealed_path,
                storyboard_review,
                self.root / "r2v.json",
                self.root / "jobs.csv",
                self.root / "receipt.json",
            )

    def test_changed_storyboard_review_blocks_paid_job_validation(self) -> None:
        sealed_path, sealed = self.build_and_seal()
        storyboard_review = self.root / "storyboard_review.json"
        write_json(
            storyboard_review,
            {
                "approved": True,
                "score": 91,
                "critical_errors": [],
                "artifact_sha256": sealed["storyboard_bundle_sha256"],
            },
        )
        jobs = self.root / "jobs.csv"
        compile_consumers(
            sealed_path,
            storyboard_review,
            self.root / "r2v.json",
            jobs,
            self.root / "receipt.json",
        )
        self.assertEqual(validate_image_video_jobs(jobs), [])
        write_json(
            storyboard_review,
            {
                "approved": False,
                "score": 20,
                "critical_errors": ["composition_mismatch"],
                "artifact_sha256": sealed["storyboard_bundle_sha256"],
            },
        )
        self.assertTrue(
            any("审核缺失或哈希已变化" in item for item in validate_image_video_jobs(jobs))
        )

    def test_runner_uses_all_ordered_references_for_toapis_r2v(self) -> None:
        first = self.root / "a.png"
        last = self.root / "storyboard.png"
        Image.new("RGB", (32, 32)).save(first)
        Image.new("RGB", (32, 32)).save(last)
        row = {
            "scene": "01",
            "reference_image_paths_json": json.dumps([str(first), str(last)]),
            "storyboard_image_path": str(last),
        }
        paths = row_reference_paths(row)
        self.assertEqual(paths, [first.resolve(), last.resolve()])
        client = Mock(spec=ToAPIsVideoClient)
        client.create_reference_task.return_value = object()
        create_provider_task(
            client=client,
            is_toapis=True,
            model="grok-video-1.0",
            prompt="角色完成动作。",
            image_path=last,
            reference_paths=paths,
            ratio="16:9",
            duration=10.0,
            resolution="720p",
            frames=None,
            seconds="10",
            size="720P",
            parameter_style="prompt",
            camera_fixed=False,
            watermark=False,
            extra_body={"client_business_id": "test-r2v"},
        )
        self.assertEqual(
            client.create_reference_task.call_args.kwargs["reference_paths"], paths
        )
        client.create_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
