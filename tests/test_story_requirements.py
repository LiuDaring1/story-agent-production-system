from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from story_requirements import validate_projection, write_projection
from story_run import file_sha256, init_run, load_run, record_run, status_summary
from run_image_video_jobs import _observe_provider_request, _preflight_native_requirements
from release_video import preflight_release_requirements


class ApplicableRequirementsTests(unittest.TestCase):
    def make(self, root: Path) -> Path:
        source = root / "rules.md"; source.write_text("current rule")
        inp = root / "input.txt"; inp.write_text("confirmed")
        output = root / "requirements.json"
        write_projection(
            output,
            scope="main-account release preview",
            inputs={"confirmed_text": inp},
            rule_sources=[(source, "v4")],
            applicability={"accounts": ["main"], "shots": ["S01"], "artifacts": ["release_preview"]},
            requirements=[{
                "requirement_id": "main-layout", "source": "user explicit override",
                "scope": "main account only", "requirement": "presenter_x=1440 overrides project default",
            }],
            executable_checks=[{"parameter": "presenter_x", "operator": "equals", "expected": 1440}],
            acceptance_evidence=["machine_preview", "independent_visual_review"],
        )
        return output

    def test_user_override_and_scope_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = validate_projection(self.make(Path(directory)), parameters={"presenter_x": 1440})
            self.assertEqual(payload["applicability"]["accounts"], ["main"])
            self.assertIn("user explicit override", payload["requirements"][0]["source"])

    def test_bad_parameter_is_rejected_before_long_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "长时执行前参数"):
                validate_projection(self.make(Path(directory)), parameters={"presenter_x": 1200})

    def test_account_scope_misuse_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            projection = self.make(Path(directory))
            with self.assertRaisesRegex(ValueError, "范围错用"):
                validate_projection(
                    projection,
                    parameters={"presenter_x": 1440},
                    consumer_scope={"accounts": ["library"]},
                )

    def test_input_or_rule_source_change_expires_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); projection = self.make(root)
            root.joinpath("input.txt").write_text("changed")
            with self.assertRaisesRegex(ValueError, "证据过期"):
                validate_projection(projection)

    def test_replacing_upstream_marks_only_declared_downstream_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); project = root / "project"; project.mkdir()
            text = root / "text.txt"; subtitle = project / "确认字幕.txt"
            video = root / "video.mp4"; audio = root / "audio.wav"
            for path in (text, subtitle, video, audio): path.write_bytes(path.name.encode())
            run_file = project / "99_项目状态" / "story_run.json"
            init_run(run_file=run_file, confirmed_text=text, subtitle_txt=subtitle, greenscreen_video=video, audio=audio, project_dir=project)
            upstream = root / "upstream-v1"; upstream.write_text("v1")
            downstream = root / "downstream"; downstream.write_text("derived")
            unrelated = root / "unrelated"; unrelated.write_text("stable")
            record_run(run_file=run_file, package="product_assets", status="done", artifact_id="upstream", artifact_path=upstream)
            record_run(run_file=run_file, package="product_assets", status="done", artifact_id="downstream", artifact_path=downstream, input_hashes={"upstream": file_sha256(upstream)})
            record_run(run_file=run_file, package="music", status="done", artifact_id="unrelated", artifact_path=unrelated)
            v2 = root / "upstream-v2"; v2.write_text("v2")
            record_run(run_file=run_file, package="product_assets", status="done", artifact_id="upstream", artifact_path=v2, replace=True)
            stale = status_summary(load_run(run_file))["stale_artifacts"]
            self.assertEqual(set(stale), {"downstream"})

    def test_video_entry_preflights_rules_and_records_request_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); project = root / "project"; project.mkdir()
            text = root / "text.txt"; subtitle = project / "确认字幕.txt"
            video = root / "video.mp4"; audio = root / "audio.wav"; rules = root / "rules.md"
            for path in (text, subtitle, video, audio, rules):
                path.write_bytes(path.name.encode())
            run_file = project / "99_项目状态" / "story_run.json"
            init_run(
                run_file=run_file, confirmed_text=text, subtitle_txt=subtitle,
                greenscreen_video=video, audio=audio, project_dir=project,
            )
            projection = root / "requirements.json"
            write_projection(
                projection,
                scope="r2v provider request",
                inputs={"confirmed_text": text},
                rule_sources=[(rules, "v1")],
                applicability={"shots": ["S01"], "artifacts": ["r2v_provider_group_receipt"]},
                requirements=[{
                    "requirement_id": "provider-model", "source": "current adapter",
                    "scope": "R2V", "requirement": "model must be approved-model",
                }],
                executable_checks=[{
                    "parameter": "model", "operator": "equals", "expected": "approved-model",
                }],
                acceptance_evidence=["provider_receipt"],
            )
            record_run(
                run_file=run_file, package="director_plan", status="done",
                artifact_id="requirements_projection", artifact_path=projection,
            )
            with self.assertRaisesRegex(ValueError, "长时执行前参数"):
                _preflight_native_requirements(
                    run_file, model="wrong", ratio="16:9", resolution="720p", seconds="6", shots=["S01"],
                )
            _preflight_native_requirements(
                run_file, model="approved-model", ratio="16:9", resolution="720p", seconds="6", shots=["S01"],
            )
            row = {
                "task_id": "offline-task-1", "provider_prompt_sha256": "a" * 64,
                "provider_attempt": "1", "reference_image_paths_json": "[]",
            }
            _observe_provider_request(
                run_file, row, provider="offline", model="approved-model", status="submitted",
            )
            request = load_run(run_file)["observability"]["requests"]["offline:offline-task-1"]
            self.assertEqual(request["request_sha256"], "a" * 64)
            self.assertEqual(request["token_status"], "not_applicable")
            self.assertNotIn("legacy_financial_evidence", request)

    def test_status_detects_input_bytes_changed_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); project = root / "project"; project.mkdir()
            text = root / "text.txt"; subtitle = project / "确认字幕.txt"
            video = root / "video.mp4"; audio = root / "audio.wav"
            for path in (text, subtitle, video, audio): path.write_bytes(path.name.encode())
            run_file = project / "99_项目状态" / "story_run.json"
            run = init_run(
                run_file=run_file, confirmed_text=text, subtitle_txt=subtitle,
                greenscreen_video=video, audio=audio, project_dir=project,
            )
            derived = root / "derived.json"; derived.write_text("{}")
            record_run(
                run_file=run_file, package="director_plan", status="done",
                artifact_id="derived", artifact_path=derived,
                input_hashes={"confirmed_text": run["inputs"]["confirmed_text"]["sha256"]},
            )
            text.write_text("changed on disk")
            stale = status_summary(load_run(run_file))["stale_artifacts"]
            self.assertEqual(set(stale), {"derived"})

    def test_release_entry_rejects_account_scope_before_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); project = root / "project"; project.mkdir()
            text = root / "text.txt"; subtitle = project / "确认字幕.txt"
            video = root / "video.mp4"; audio = root / "audio.wav"; rules = root / "rules.md"
            for path in (text, subtitle, video, audio, rules):
                path.write_bytes(path.name.encode())
            run_file = project / "99_项目状态" / "story_run.json"
            init_run(
                run_file=run_file, confirmed_text=text, subtitle_txt=subtitle,
                greenscreen_video=video, audio=audio, project_dir=project,
            )
            projection = root / "release-requirements.json"
            write_projection(
                projection, scope="main release", inputs={"confirmed_text": text},
                rule_sources=[(rules, "v1")],
                applicability={"accounts": ["main"], "artifacts": ["main_release_video"]},
                requirements=[{
                    "requirement_id": "main-only", "source": "user override",
                    "scope": "main", "requirement": "only main is applicable",
                }],
                executable_checks=[], acceptance_evidence=["release_preview"],
            )
            record_run(
                run_file=run_file, package="director_plan", status="done",
                artifact_id="requirements_projection", artifact_path=projection,
            )
            with self.assertRaisesRegex(ValueError, "范围错用"):
                preflight_release_requirements(
                    output_dir=project / "04_发布视频", variant="library",
                    explicit_run_file=None, output_scale=1,
                )


if __name__ == "__main__":
    unittest.main()
