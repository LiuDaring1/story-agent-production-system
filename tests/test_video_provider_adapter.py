from __future__ import annotations

import csv
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from video_provider_adapter import VideoProviderConfigError, resolve_row_generation_seconds, resolve_video_provider
from story_workflow import run_generate_until_complete
from story_video_synthesizer.toapis_video import (
    DEFAULT_MODEL as TOAPIS_DEFAULT_MODEL,
    DEFAULT_SECONDS as TOAPIS_DEFAULT_SECONDS,
    MAX_SECONDS as TOAPIS_MAX_SECONDS,
    MIN_SECONDS as TOAPIS_MIN_SECONDS,
    DEFAULT_USER_AGENT,
    ToAPIsVideoClient,
    build_toapis_task_body,
    extract_toapis_video_url,
)
from run_image_video_jobs import reset_retryable_failed_row, row_duration_value, row_request_seconds, toapis_extra_body
import run_image_video_jobs
from story_video_synthesizer.volcengine_video import CreateTaskResult


class VideoProviderAdapterTests(unittest.TestCase):
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
            with patch("story_workflow.run_script") as runner:
                with self.assertRaisesRegex(RuntimeError, "没有减少待处理任务"):
                    run_generate_until_complete(
                        [],
                        runner=root / "mock_video_provider.py",
                        jobs_csv=jobs,
                        videos_dir=videos,
                        start_scene=1,
                        end_scene=9999,
                        scenes="",
                        limit=0,
                    )
            runner.assert_called_once()

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
