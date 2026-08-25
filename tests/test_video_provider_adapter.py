from __future__ import annotations

import csv
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from video_provider_adapter import VideoProviderConfigError, resolve_row_generation_seconds, resolve_video_provider
from story_workflow import run_generate_until_complete
from story_video_synthesizer.toapis_video import (
    DEFAULT_MODEL as TOAPIS_DEFAULT_MODEL,
    DEFAULT_SECONDS as TOAPIS_DEFAULT_SECONDS,
    MAX_SECONDS as TOAPIS_MAX_SECONDS,
    MIN_SECONDS as TOAPIS_MIN_SECONDS,
    MAX_PROMPT_CHARS as TOAPIS_MAX_PROMPT_CHARS,
    DEFAULT_USER_AGENT,
    GROK_VIDEO_1_0_DEFAULT_SECONDS,
    GROK_VIDEO_1_0_MAX_REFERENCE_IMAGES,
    ToAPIsVideoClient,
    build_toapis_reference_task_body,
    build_toapis_task_body,
    extract_toapis_video_url,
)
from run_image_video_jobs import (
    TOAPIS_OPERATIONAL_MIN_SECONDS,
    TOAPIS_SAFE_PROVIDER_PROMPT_CHARS,
    provider_prompt_for_row,
    reset_retryable_failed_row,
    row_duration_value,
    row_request_seconds,
    toapis_extra_body,
)
import run_image_video_jobs
from story_video_synthesizer.volcengine_video import CreateTaskResult, QueryTaskResult


class VideoProviderAdapterTests(unittest.TestCase):
    def test_toapis_provider_prompt_strips_internal_audit_context(self) -> None:
        full = (
            "公鸡轻轻低头，镜头缓慢推近。\n"
            "[STORY_CONTRACT_V1]{\"characters\":[\"rooster\"]}\n"
            "必须保持合同角色身份。\n"
            "[VIDEO_MOTION_PLAN_V1]{\"camera_motion\":\"push_in\"}"
        )
        row = {"scene": "05", "prompt": full}
        self.assertEqual(
            provider_prompt_for_row(row, model="grok-video-1.5", is_toapis=True),
            "公鸡轻轻低头，镜头缓慢推近。",
        )
        self.assertEqual(row["prompt"], full)

    def test_grok_video_1_0_uses_same_short_provider_prompt_boundary(self) -> None:
        full = "公鸡甩头。\n[STORY_CONTRACT_V1]{\"large\":\"internal\"}"
        row = {"scene": "05", "prompt": full}
        self.assertEqual(
            provider_prompt_for_row(row, model="grok-video-1.0", is_toapis=True),
            "公鸡甩头。",
        )
        self.assertEqual(row["prompt"], full)

    def test_toapis_provider_prompt_rejects_oversized_prose_before_submission(self) -> None:
        with self.assertRaisesRegex(ValueError, "缺少可安全压缩"):
            provider_prompt_for_row(
                {"scene": "05", "prompt": "动" * (TOAPIS_MAX_PROMPT_CHARS + 1)},
                model="grok-video-1.5",
                is_toapis=True,
            )

    def test_toapis_provider_prompt_compiles_long_review_text_from_structured_motion(self) -> None:
        row = {
            "scene": "08",
            "prompt": "很长的审核提示。" * 100 + "\n[STORY_CONTRACT_V1]{}",
            "subject_action": "公鸡短促哼一声，头轻轻撇开，原先抬起的翅膀落下并收拢。",
            "camera_motion": "镜头轻推近公鸡脸部后停住。",
            "environment_motion": "花枝和叶片轻微摇动。",
        }
        compact = provider_prompt_for_row(row, model="grok-video-1.5", is_toapis=True)
        self.assertLessEqual(
            len(compact.encode("utf-16-le")) // 2,
            TOAPIS_SAFE_PROVIDER_PROMPT_CHARS,
        )
        self.assertIn("公鸡短促哼一声", compact)
        self.assertIn("镜头轻推近", compact)
        self.assertIn("保持首图角色、物体数量、外观和画风", compact)
        self.assertIn("[STORY_CONTRACT_V1]", row["prompt"])

    def test_targeted_retry_prompt_reaches_provider_unchanged(self) -> None:
        retry = "公鸡自然抬起完整羽翼，羽翼始终为羽毛结构，不出现人手。"
        row = {
            "scene": "08",
            "prompt": "旧动作提示。[STORY_CONTRACT_V1]{}",
            "provider_retry_prompt": retry,
            "previous_provider_prompt_sha256": "a" * 64,
        }
        self.assertEqual(
            provider_prompt_for_row(row, model="grok-video-1.5", is_toapis=True),
            retry,
        )

    def test_identical_retry_provider_prompt_is_blocked_before_submission(self) -> None:
        retry = "公鸡自然抬起完整羽翼。"
        row = {
            "scene": "08",
            "prompt": "审计提示",
            "provider_retry_prompt": retry,
            "previous_provider_prompt_sha256": __import__("hashlib").sha256(retry.encode("utf-8")).hexdigest(),
        }
        with self.assertRaisesRegex(ValueError, "完全相同"):
            provider_prompt_for_row(row, model="grok-video-1.5", is_toapis=True)

    def test_toapis_request_body_rejects_oversized_prompt(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能超过 1200 字符"):
            build_toapis_task_body(
                model="grok-video-1.5",
                prompt="动" * (TOAPIS_MAX_PROMPT_CHARS + 1),
                image_url="https://example.com/frame.png",
            )

    def test_toapis_runner_validates_all_prompt_lengths_before_paid_submission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images, videos, jobs = self._write_grok_jobs_fixture(root)
            with jobs.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[1]["prompt"] = "动" * (TOAPIS_MAX_PROMPT_CHARS + 1)
            with jobs.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

            class FakeClient:
                calls = 0

                def __init__(self, *, api_key: str, base_url: str) -> None:
                    del api_key, base_url

                def create_task(self, **kwargs):
                    del kwargs
                    type(self).calls += 1
                    return CreateTaskResult(task_id="unexpected", raw={})

            argv = [
                "run_image_video_jobs.py",
                "--jobs-csv", str(jobs),
                "--images-dir", str(images),
                "--videos-dir", str(videos),
                "--base-url", "https://toapis.com/v1",
                "--model", "grok-video-1.5",
                "--api-key", "test-secret",
                "--submit-all-first",
                "--submit-only",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(run_image_video_jobs, "ToAPIsVideoClient", FakeClient),
                self.assertRaisesRegex(ValueError, "缺少可安全压缩"),
            ):
                run_image_video_jobs.main()
            self.assertEqual(FakeClient.calls, 0)

    def test_grok_row_seconds_prioritize_generation_duration_and_clamp_integer_range(self) -> None:
        for row, expected in [
            ({"generation_duration": "5", "duration": "9"}, "5"),
            ({"duration": "8.01"}, "9"),
            ({"duration": "15"}, "15"),
            ({"duration": "0.2"}, "1"),
            ({"duration": "99"}, "15"),
        ]:
            self.assertEqual(
                resolve_row_generation_seconds(
                    row,
                    model="grok-video-1.5",
                    fallback_seconds="8",
                    min_seconds=1,
                    max_seconds=15,
                ),
                expected,
            )

    def test_grok_video_1_0_selects_only_six_or_ten_seconds_per_scene(self) -> None:
        for requested, expected in [
            ("2", "6"),
            ("6", "6"),
            ("6.01", "6"),
            ("7.99", "6"),
            ("8", "10"),
            ("9", "10"),
            ("14", "10"),
        ]:
            self.assertEqual(
                resolve_row_generation_seconds(
                    {"generation_duration": requested},
                    model="grok-video-1.0",
                    fallback_seconds="6",
                    min_seconds=6,
                    max_seconds=10,
                ),
                expected,
            )
            self.assertEqual(
                row_request_seconds(
                    {"generation_duration": requested},
                    model="grok-video-1.0",
                    is_toapis=True,
                    fallback_seconds="6",
                ),
                expected,
            )

    def test_toapis_runner_applies_user_authorized_four_second_operational_floor(self) -> None:
        self.assertEqual(TOAPIS_OPERATIONAL_MIN_SECONDS, 4)
        self.assertEqual(
            row_request_seconds(
                {"generation_duration": "2", "target_duration": "2"},
                model="grok-video-1.5",
                is_toapis=True,
                fallback_seconds="8",
            ),
            "4",
        )
        self.assertEqual(
            row_request_seconds(
                {"generation_duration": "7"},
                model="grok-video-1.5",
                is_toapis=True,
                fallback_seconds="8",
            ),
            "7",
        )

    def test_legacy_models_keep_global_seconds_fallback(self) -> None:
        row = {"generation_duration": "5", "duration": "9"}
        self.assertEqual(
            resolve_row_generation_seconds(row, model="grok-video-3", fallback_seconds="provider-default"),
            "provider-default",
        )
        self.assertEqual(
            row_request_seconds(row, model="grok-video-3", is_toapis=True, fallback_seconds="10"),
            "10",
        )
        self.assertEqual(
            row_request_seconds(row, model="grok-video-1.5", is_toapis=False, fallback_seconds="10"),
            "10",
        )

    def test_row_duration_fallback_order_is_generation_then_duration_then_cli(self) -> None:
        self.assertEqual(row_duration_value({"generation_duration": "5", "duration": "9"}, 10), 5.0)
        self.assertEqual(row_duration_value({"duration": "9"}, 10), 9.0)
        self.assertEqual(row_duration_value({}, 10), 10.0)

    def _write_grok_jobs_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        images = root / "images"
        videos = root / "videos"
        images.mkdir()
        videos.mkdir()
        for scene in range(1, 4):
            Image.new("RGB", (320, 180), (scene * 40, 80, 120)).save(images / f"{scene:02d}.png")
        jobs = root / "jobs.csv"
        jobs.write_text(
            "scene,image_filename,story_text,prompt,target_video_filename,generation_duration,duration,status\n"
            "1,01.png,甲,甲,01.mp4,5,9,todo\n"
            "2,02.png,乙,乙,02.mp4,,9,todo\n"
            "3,03.png,丙,丙,03.mp4,15,4,todo\n",
            encoding="utf-8-sig",
        )
        return images, videos, jobs

    def test_grok_dry_run_prints_each_row_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images, videos, jobs = self._write_grok_jobs_fixture(root)
            output = io.StringIO()
            argv = [
                "run_image_video_jobs.py",
                "--jobs-csv",
                str(jobs),
                "--images-dir",
                str(images),
                "--videos-dir",
                str(videos),
                "--base-url",
                "https://toapis.com/v1",
                "--model",
                "grok-video-1.5",
                "--seconds",
                "8",
                "--dry-run",
            ]
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
                run_image_video_jobs.main()
            rendered = output.getvalue()
            self.assertEqual(rendered.count('"seconds": "5"'), 1)
            self.assertEqual(rendered.count('"seconds": "9"'), 1)
            self.assertEqual(rendered.count('"seconds": "15"'), 1)

    def test_grok_submit_all_first_passes_each_row_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images, videos, jobs = self._write_grok_jobs_fixture(root)
            calls: list[str] = []

            class FakeClient:
                def __init__(self, *, api_key: str, base_url: str) -> None:
                    del api_key, base_url

                def create_task(self, **kwargs):
                    calls.append(str(kwargs["seconds"]))
                    return CreateTaskResult(task_id=f"task-{len(calls)}", raw={"id": f"task-{len(calls)}"})

            argv = [
                "run_image_video_jobs.py",
                "--jobs-csv",
                str(jobs),
                "--images-dir",
                str(images),
                "--videos-dir",
                str(videos),
                "--base-url",
                "https://toapis.com/v1",
                "--model",
                "grok-video-1.5",
                "--api-key",
                "test-secret",
                "--seconds",
                "8",
                "--submit-all-first",
                "--submit-only",
            ]
            with patch.object(sys, "argv", argv), patch.object(run_image_video_jobs, "ToAPIsVideoClient", FakeClient):
                run_image_video_jobs.main()
            self.assertEqual(calls, ["5", "9", "15"])

    def test_contract_gate_failure_happens_before_any_paid_create_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images, videos, jobs = self._write_grok_jobs_fixture(root)
            with jobs.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            for row in rows:
                row.update(
                    {
                        "contract_schema_version": "1.0.0",
                        "story_contract_sha256": "a" * 64,
                        "story_contract_dependency_sha256": "b" * 64,
                    }
                )
            with jobs.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            paid_calls: list[dict] = []

            class FakeClient:
                def __init__(self, *, api_key: str, base_url: str) -> None:
                    del api_key, base_url

                def create_task(self, **kwargs):
                    paid_calls.append(kwargs)
                    return CreateTaskResult(task_id="must-not-run", raw={})

            argv = [
                "run_image_video_jobs.py",
                "--jobs-csv", str(jobs),
                "--project-dir", str(root),
                "--images-dir", str(images),
                "--videos-dir", str(videos),
                "--base-url", "https://toapis.com/v1",
                "--model", "grok-video-1.5",
                "--api-key", "test-secret",
                "--submit-only",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(run_image_video_jobs, "ToAPIsVideoClient", FakeClient),
                patch.object(
                    run_image_video_jobs,
                    "assert_request_contract_binding",
                    side_effect=ValueError("lock damaged"),
                ) as gate,
            ):
                with self.assertRaisesRegex(ValueError, "lock damaged"):
                    run_image_video_jobs.main()
            gate.assert_called_once()
            self.assertEqual(paid_calls, [])

    def test_api_timeout_or_no_progress_stops_without_infinite_loop(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            jobs = work / "jobs.csv"
            videos = work / "videos"
            videos.mkdir()
            jobs.write_text(
                "scene,target_video_filename,status\n1,01.mp4,todo\n",
                encoding="utf-8-sig",
            )
            invoke = Mock()
            with self.assertRaisesRegex(RuntimeError, "没有减少待处理任务"):
                run_generate_until_complete(
                    invoke,
                    jobs_csv=jobs,
                    videos_dir=videos,
                    start_scene=1,
                    end_scene=9999,
                    scenes="",
                    limit=0,
                )
            invoke.assert_called_once()

    def test_mock_provider_runs_through_real_workflow_without_paid_api(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            images = work / "images"
            videos = work / "videos"
            images.mkdir()
            Image.new("RGB", (320, 180), (60, 120, 180)).save(images / "01.png")
            jobs = work / "demo_image_video_jobs.csv"
            decisions = work / "prompt_review_decisions.csv"
            jobs.write_text(
                "scene,image_filename,story_text,prompt,target_video_filename,duration,frames,status\n"
                "1,01.png,小羊出发,小羊向前走,01_demo.mp4,1.0,24,todo\n",
                encoding="utf-8-sig",
            )
            decisions.write_text(
                "scene,image_filename,story_text,review_status,prompt,notes\n"
                "01,01.png,小羊出发,approved,小羊自然地向前走,通过\n",
                encoding="utf-8-sig",
            )
            process = subprocess.run(
                [
                    sys.executable,
                    str(root / "story_workflow.py"),
                    "generate",
                    "--jobs-csv",
                    str(jobs),
                    "--images-dir",
                    str(images),
                    "--videos-dir",
                    str(videos),
                    "--provider",
                    "mock_local",
                    "--execution-mode",
                    "test",
                ],
                cwd=root,
                text=True,
                capture_output=True,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertGreater((videos / "01_demo.mp4").stat().st_size, 0)
            with jobs.open(encoding="utf-8-sig", newline="") as file:
                row = next(csv.DictReader(file))
            self.assertEqual(row["status"], "downloaded")
            self.assertEqual(row["prompt"], "小羊自然地向前走")
            self.assertEqual(row["video_source_kind"], "ffmpeg_still_frame")
            self.assertEqual(row["video_execution_mode"], "test")
            self.assertEqual(row["production_eligible"], "false")
            self.assertTrue(Path(row["video_receipt_path"]).is_file())

    def test_selects_named_adapter_without_business_logic_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("pass\n", encoding="utf-8")
            second.write_text("pass\n", encoding="utf-8")
            config = {
                "video_api": {
                    "provider": "first",
                    "adapters": {
                        "first": {"runner": "first.py", "model": "m1", "estimated_cost_cny_per_clip": 1.0},
                        "second": {"runner": "second.py", "model": "m2", "estimated_cost_cny_per_clip": 2.5},
                    },
                }
            }
            selected = resolve_video_provider(config, root, "second")
            self.assertEqual(selected.name, "second")
            self.assertEqual(selected.runner, second)
            self.assertEqual(selected.model, "m2")
            self.assertEqual(selected.estimated_cost_cny_per_clip, 2.5)

    def test_adapter_supports_per_second_cost_and_generation_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "runner.py"
            runner.write_text("pass\n", encoding="utf-8")
            config = {
                "video_api": {
                    "provider": "toapis_grok",
                    "adapters": {
                        "toapis_grok": {
                            "runner": "runner.py",
                            "model": "grok-video-1.5",
                            "estimated_cost_cny_per_clip": 0.08,
                            "estimated_cost_cny_per_second": 0.01,
                            "default_seconds": 8,
                            "min_seconds": 1,
                            "max_seconds": 15,
                            "default_resolution": "720p",
                            "default_ratio": "16:9",
                        }
                    },
                }
            }
            selected = resolve_video_provider(config, root)
            self.assertEqual(selected.default_seconds, 8.0)
            self.assertEqual(selected.min_seconds, 1.0)
            self.assertEqual(selected.max_seconds, 15.0)
            self.assertEqual(selected.default_resolution, "720p")
            self.assertEqual(selected.default_ratio, "16:9")
            self.assertEqual(selected.estimate_cost(), 0.08)
            self.assertEqual(selected.estimate_cost(12), 0.12)

    def test_toapis_grok_video_15_defaults_to_eight_seconds_720p_and_16_9(self) -> None:
        body = build_toapis_task_body(
            model=TOAPIS_DEFAULT_MODEL,
            prompt="小动物轻轻眨眼",
            image_url="https://files.example/scene.png",
        )
        self.assertEqual(TOAPIS_DEFAULT_MODEL, "grok-video-1.5")
        self.assertEqual(TOAPIS_DEFAULT_SECONDS, "8")
        self.assertEqual(body["seconds"], "8")
        self.assertEqual(body["resolution"], "720p")
        self.assertEqual(body["aspect_ratio"], "16:9")

    def test_toapis_grok_video_15_rejects_seconds_outside_one_to_fifteen(self) -> None:
        self.assertEqual(TOAPIS_MIN_SECONDS, 1)
        self.assertEqual(TOAPIS_MAX_SECONDS, 15)
        for seconds in ("0", "16", "-1", "not-a-number"):
            with self.assertRaises(ValueError):
                build_toapis_task_body(
                    model=TOAPIS_DEFAULT_MODEL,
                    prompt="测试",
                    image_url="https://files.example/scene.png",
                    seconds=seconds,
                )

    def test_toapis_grok_video_1_0_defaults_to_six_and_rejects_other_durations(self) -> None:
        body = build_toapis_task_body(
            model="grok-video-1.0",
            prompt="花瓣和叶片轻轻摆动，文字保持稳定",
            image_url="https://files.example/title.png",
        )
        self.assertEqual(GROK_VIDEO_1_0_DEFAULT_SECONDS, "6")
        self.assertEqual(body["seconds"], "6")
        for seconds in ("5", "7", "8", "9", "11"):
            with self.assertRaisesRegex(ValueError, "只能是 6 或 10"):
                build_toapis_task_body(
                    model="grok-video-1.0",
                    prompt="测试",
                    image_url="https://files.example/scene.png",
                    seconds=seconds,
                )

    def test_toapis_grok_video_1_0_r2v_uses_references_without_first_frame(self) -> None:
        body = build_toapis_reference_task_body(
            model="grok-video-1.0",
            prompt="老婆婆走近迷路的小姑娘，停下关心地询问她。",
            reference_urls=[
                "https://files.example/jenny.png",
                "https://files.example/grandmother.png",
                "https://files.example/forest.png",
            ],
            seconds=6,
        )
        self.assertEqual(body["reference_images"], [
            "https://files.example/jenny.png",
            "https://files.example/grandmother.png",
            "https://files.example/forest.png",
        ])
        self.assertEqual(body["duration"], 6)
        self.assertNotIn("image", body)
        self.assertNotIn("images", body)

    def test_toapis_grok_video_1_0_r2v_validates_reference_count_and_duration(self) -> None:
        self.assertEqual(GROK_VIDEO_1_0_MAX_REFERENCE_IMAGES, 7)
        with self.assertRaisesRegex(ValueError, "至少需要一张"):
            build_toapis_reference_task_body(
                model="grok-video-1.0", prompt="测试", reference_urls=[], seconds=6,
            )
        with self.assertRaisesRegex(ValueError, "不能超过 7 张"):
            build_toapis_reference_task_body(
                model="grok-video-1.0",
                prompt="测试",
                reference_urls=[f"https://files.example/{index}.png" for index in range(8)],
                seconds=6,
            )
        with self.assertRaisesRegex(ValueError, "只能是 6 或 10"):
            build_toapis_reference_task_body(
                model="grok-video-1.0",
                prompt="测试",
                reference_urls=["https://files.example/jenny.png"],
                seconds=8,
            )

    def test_toapis_reference_task_uploads_each_reference_and_submits_r2v_body(self) -> None:
        client = ToAPIsVideoClient("test-secret")
        paths = [Path("jenny.png"), Path("grandmother.png")]
        with (
            patch.object(client, "upload_image", side_effect=[
                "https://files.example/jenny.png",
                "https://files.example/grandmother.png",
            ]) as upload,
            patch.object(client, "_request", return_value={"id": "r2v-123"}) as request,
        ):
            created = client.create_reference_task(
                model="grok-video-1.0",
                prompt="老婆婆关心地询问珍妮。",
                reference_paths=paths,
                seconds=6,
            )
        self.assertEqual(created.task_id, "r2v-123")
        self.assertEqual([call.args[0] for call in upload.call_args_list], paths)
        payload = __import__("json").loads(request.call_args.args[2].decode("utf-8"))
        self.assertEqual(payload["reference_images"], [
            "https://files.example/jenny.png",
            "https://files.example/grandmother.png",
        ])
        self.assertNotIn("images", payload)

    def test_toapis_reference_task_reuses_stable_business_id(self) -> None:
        client = ToAPIsVideoClient("test-secret")
        existing = QueryTaskResult(
            task_id="story-r2v-shot-a",
            status="in_progress",
            video_url=None,
            error=None,
            raw={"id": "provider-task-123", "status": "in_progress"},
        )
        with (
            patch.object(client, "upload_image", return_value="https://files.example/jenny.png"),
            patch.object(client, "get_task", return_value=existing),
            patch.object(client, "_request") as request,
        ):
            created = client.create_reference_task(
                model="grok-video-1.0",
                prompt="老婆婆走近珍妮。",
                reference_paths=[Path("jenny.png")],
                seconds=6,
                extra_body={"client_business_id": "story-r2v-shot-a"},
            )
        self.assertEqual(created.task_id, "provider-task-123")
        request.assert_not_called()

    def test_legacy_toapis_model_keeps_opaque_seconds_values_compatible(self) -> None:
        body = build_toapis_task_body(
            model="grok-video-3",
            prompt="测试",
            image_url="https://files.example/scene.png",
            seconds="provider-default",
        )
        self.assertEqual(body["seconds"], "provider-default")

    def test_toapis_adapter_passes_only_secret_environment_name(self) -> None:
        root = Path(__file__).resolve().parents[1]
        config = {
            "video_api": {
                "provider": "toapis_grok",
                "adapters": {
                    "toapis_grok": {
                        "runner": "run_image_video_jobs.py",
                        "base_url": "https://toapis.com/v1",
                        "model": "grok-video-3",
                        "api_key_env": "TOAPIS_API_KEY",
                        "estimated_cost_cny_per_clip": 3.0,
                    }
                },
            }
        }
        selected = resolve_video_provider(config, root)
        self.assertEqual(
            selected.runner_args(),
            ["--base-url", "https://toapis.com/v1", "--model", "grok-video-3", "--api-key-env", "TOAPIS_API_KEY"],
        )
        self.assertNotIn("sk-", " ".join(selected.runner_args()))

    def test_toapis_request_and_nested_result_contract(self) -> None:
        body = build_toapis_task_body(
            model="grok-video-3",
            prompt="大象缓慢抬起前腿",
            image_url="https://files.example/scene.png",
            seconds="10",
            resolution="720P",
        )
        self.assertEqual(body["images"], ["https://files.example/scene.png"])
        self.assertEqual(body["seconds"], "10")
        self.assertEqual(body["resolution"], "720p")
        payload = {"status": "completed", "result": {"data": [{"url": "https://files.example/out.mp4"}]}}
        self.assertEqual(extract_toapis_video_url(payload), "https://files.example/out.mp4")

    def test_toapis_request_uses_cloudflare_compatible_api_user_agent(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"ok": true}'

        client = ToAPIsVideoClient("test-secret")
        with patch("story_video_synthesizer.toapis_video.urlopen", return_value=Response()) as opener:
            self.assertEqual(client._request("GET", "health", None), {"ok": True})
        request = opener.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), DEFAULT_USER_AGENT)

    def test_toapis_business_id_is_stable_until_controlled_retry(self) -> None:
        row = {"scene": "1", "image_filename": "01.png", "prompt": "蚂蚁向前走", "provider_attempt": "0"}
        first = toapis_extra_body(Path("jobs.csv"), row, None)["client_business_id"]
        second = toapis_extra_body(Path("jobs.csv"), dict(row), None)["client_business_id"]
        self.assertEqual(first, second)
        row["provider_attempt"] = "1"
        retried = toapis_extra_body(Path("jobs.csv"), row, None)["client_business_id"]
        self.assertNotEqual(first, retried)

    def test_toapis_missing_business_id_proceeds_to_create(self) -> None:
        client = ToAPIsVideoClient("test-secret")
        with (
            patch.object(client, "upload_image", return_value="https://files.example/01.png"),
            patch.object(client, "get_task", side_effect=RuntimeError('ToAPIs HTTP 400：{"code":"task_not_exist"}')),
            patch.object(client, "_request", return_value={"id": "created-123"}) as request,
        ):
            created = client.create_task(
                model="grok-video-3",
                prompt="大象缓慢走动",
                image_path=Path("01.png"),
                extra_body={"client_business_id": "story-stable-id"},
            )
        self.assertEqual(created.task_id, "created-123")
        self.assertEqual(request.call_args.args[:2], ("POST", "videos/generations"))

    def test_presubmit_failure_can_be_retried_without_a_task_id(self) -> None:
        row = {
            "scene": "1",
            "status": "error",
            "task_id": "",
            "video_url": "",
            "error": "upload rejected",
            "api_response": "",
            "query_response": "",
            "provider_attempt": "0",
        }
        with tempfile.TemporaryDirectory() as directory:
            changed = reset_retryable_failed_row(row, Path(directory) / "01.mp4")
        self.assertTrue(changed)
        self.assertEqual(row["status"], "todo")
        self.assertEqual(row["error"], "")
        self.assertEqual(row["provider_attempt"], "1")

    def test_unknown_provider_fails_before_paid_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(VideoProviderConfigError, "未找到 provider"):
                resolve_video_provider({"video_api": {"provider": "missing", "adapters": {}}}, Path(directory))


if __name__ == "__main__":
    unittest.main()
