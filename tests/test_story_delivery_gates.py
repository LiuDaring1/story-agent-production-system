from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from delivery_gate_fixtures import binding, make_delivery, release_qa
from story_artifact_validation import validate_artifact_semantics


class StoryDeliveryGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.checklist = make_delivery(self.root / "project")
        self.checklist_path = self.write("checklist.json", self.checklist)
        self.voice = self.root / "voice.wav"
        self.music = self.root / "music.wav"
        self.voice.write_bytes(b"full narration")
        self.music.write_bytes(b"music")
        release = self.root / "project" / "04_发布视频"
        videos = {
            "main_release_video": binding(release / "主账号发布视频.mp4"),
            "library_release_video": binding(release / "宝库号发布视频.mp4"),
        }
        self.qa = release_qa(videos, self.voice, self.music)
        self.qa_path = self.write("qa.json", self.qa)
        self.registered = {
            **videos, "final_delivery_checklist": binding(self.checklist_path),
            "qa_release_report": binding(self.qa_path),
        }
        self.inputs = {"audio": binding(self.voice)}
        self.bundle = {"artifacts": self.checklist["artifacts"] + [binding(self.checklist_path), binding(self.qa_path)]}
        self.bundle_path = self.write("bundle.json", self.bundle)
        self.review = {
            "schema_version": "independent-review/v1", "approved": True, "score": 97,
            "critical_errors": [], "artifact_path": str(self.bundle_path),
            "artifact_sha256": binding(self.bundle_path)["sha256"],
        }

    def write(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def validate(self, name, payload):
        path = self.write("candidate.json", payload)
        return validate_artifact_semantics(
            name, path, registered_artifacts=self.registered, registered_inputs=self.inputs,
        )

    def test_existing_v1_checklist_and_review_need_no_role_field_migration(self):
        self.validate("final_delivery_checklist", self.checklist)
        self.validate("final_delivery_review", self.review)
        self.assertEqual(len(self.checklist["artifacts"]), 25)  # PPT pair is inside advanced, not duplicated.

    def test_old_review_rejected_when_new_delivery_is_registered_but_old_file_remains(self):
        new = self.root / "new_video.mp4"
        new.write_bytes(b"unreviewed version")
        self.registered["main_release_video"] = binding(new)
        with self.assertRaisesRegex(ValueError, "正式文件不一致"):
            self.validate("final_delivery_review", self.review)

    def test_bundle_containing_both_old_delivery_and_new_registered_video_is_rejected(self):
        new = self.root / "version2" / "主账号发布视频.mp4"
        new.parent.mkdir()
        new.write_bytes(b"new version")
        self.registered["main_release_video"] = binding(new)
        self.bundle["artifacts"].append(binding(new))
        self.write("bundle.json", self.bundle)
        self.review["artifact_sha256"] = binding(self.bundle_path)["sha256"]
        with self.assertRaisesRegex(ValueError, "正式文件不一致"):
            self.validate("final_delivery_review", self.review)

    def test_review_must_cover_current_checklist_itself_and_qa(self):
        for excluded in (self.checklist_path, self.qa_path):
            with self.subTest(excluded=excluded.name):
                payload = copy.deepcopy(self.bundle)
                payload["artifacts"] = [item for item in payload["artifacts"] if item["path"] != str(excluded)]
                self.write("bundle.json", payload)
                self.review["artifact_sha256"] = binding(self.bundle_path)["sha256"]
                with self.assertRaisesRegex(ValueError, "未覆盖当前"):
                    self.validate("final_delivery_review", self.review)

    def test_review_must_cover_every_delivered_member(self):
        self.bundle["artifacts"].pop(0)
        self.write("bundle.json", self.bundle)
        self.review["artifact_sha256"] = binding(self.bundle_path)["sha256"]
        with self.assertRaisesRegex(ValueError, "未覆盖当前"):
            self.validate("final_delivery_review", self.review)

    def test_final_review_requires_current_registered_targets(self):
        del self.registered["final_delivery_checklist"]
        with self.assertRaisesRegex(ValueError, "缺少账本目标"):
            self.validate("final_delivery_review", self.review)

    def test_claimed_counts_do_not_replace_actual_members(self):
        self.checklist["artifacts"] = self.checklist["artifacts"][:1]
        with self.assertRaisesRegex(ValueError, "交付矩阵"):
            self.validate("final_delivery_checklist", self.checklist)

    def test_duplicate_member_cannot_count_as_a_second_deliverable(self):
        self.checklist["artifacts"].append(self.checklist["artifacts"][0])
        with self.assertRaisesRegex(ValueError, "重复"):
            self.validate("final_delivery_checklist", self.checklist)

    def test_missing_cover_and_wrong_account_copy_are_rejected(self):
        for fragment in ("cover_16x9.png", "library/copy.md"):
            with self.subTest(fragment=fragment):
                payload = copy.deepcopy(self.checklist)
                payload["artifacts"] = [r for r in payload["artifacts"] if fragment not in r["path"]]
                with self.assertRaisesRegex(ValueError, "六张封面或双账号文案"):
                    self.validate("final_delivery_checklist", payload)

    def test_unlisted_customer_file_is_rejected_but_finder_metadata_is_ignored(self):
        directory = next(Path(r["path"]).parent for r in self.checklist["artifacts"] if "基础版" in r["path"])
        (directory / ".DS_Store").write_bytes(b"metadata")
        self.validate("final_delivery_checklist", self.checklist)
        (directory / "forgotten.txt").write_text("not a customer deliverable")
        with self.assertRaisesRegex(ValueError, "实际目录"):
            self.validate("final_delivery_checklist", self.checklist)

    def test_same_count_with_duplicate_product_role_is_rejected(self):
        item = next(r for r in self.checklist["artifacts"] if "基础版" in r["path"] and "背景图片" in r["path"])
        path = Path(item["path"])
        replacement = path.with_name("故事文稿：副本.docx")
        path.rename(replacement)
        item.update(binding(replacement))
        with self.assertRaisesRegex(ValueError, "重复角色"):
            self.validate("final_delivery_checklist", self.checklist)

    def test_old_release_qa_without_audio_evidence_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "v3"):
            self.validate("qa_release_report", {"schema_version": "story-release-machine-qa/v2", "passed": True, "critical_errors": []})

    def test_product_machine_qa_must_bind_exact_final_package_members(self):
        product = [
            item for item in self.checklist["artifacts"]
            if "故事锦囊（基础版）" in item["path"] or "故事锦囊（进阶版）" in item["path"]
        ]
        report = {
            "schema_version": "story-product-machine-qa/v2", "passed": True,
            "critical_errors": [], "errors": [],
            "artifacts": {f"member-{index}": item for index, item in enumerate(product)},
        }
        self.validate("qa_product_report", report)
        report["artifacts"]["member-0"] = self.checklist["artifacts"][0]
        with self.assertRaisesRegex(ValueError, r"当前 5\+10"):
            self.validate("qa_product_report", report)

    def test_publish_machine_qa_must_bind_exact_final_publish_members(self):
        artifacts = {}
        for account in ("main", "library"):
            artifacts[f"{account}:copy"] = next(
                item for item in self.checklist["artifacts"]
                if f"publish_package/{account}/copy.md" in item["path"]
            )
            for ratio in ("3x4", "4x3", "16x9"):
                artifacts[f"{account}:cover_{ratio}"] = next(
                    item for item in self.checklist["artifacts"]
                    if f"publish_package/{account}/covers/cover_{ratio}.png" in item["path"]
                )
        report = {
            "schema_version": "story-publish-machine-qa/v2", "passed": True,
            "critical_errors": [], "errors": [], "artifacts": artifacts,
        }
        self.validate("qa_publish_report", report)
        report["artifacts"]["main:copy"] = artifacts["library:copy"]
        with self.assertRaisesRegex(ValueError, "不是最终交付清单"):
            self.validate("qa_publish_report", report)

    @patch("story_artifact_validation.probe_duration", return_value=2.0)
    def test_current_v3_report_is_accepted_without_rewriting_it(self, _probe):
        self.validate("qa_release_report", self.qa)

    @patch("story_artifact_validation.probe_duration", return_value=2.0)
    def test_v3_report_requires_results_and_real_audio_metrics(self, _probe):
        for field in ("results", "audio_contract"):
            with self.subTest(field=field):
                payload = copy.deepcopy(self.qa)
                del payload[field]
                with self.assertRaises(ValueError):
                    self.validate("qa_release_report", payload)
        self.qa["results"][0]["audio_role_fit"] = {"passed": True}
        with self.assertRaisesRegex(ValueError, "指标不完整"):
            self.validate("qa_release_report", self.qa)

    @patch("story_artifact_validation.probe_duration", return_value=2.0)
    def test_release_qa_must_match_current_videos_and_authoritative_narration(self, _probe):
        replacement = self.root / "replacement.mp4"
        replacement.write_bytes(b"replacement")
        original = copy.deepcopy(self.registered)
        self.registered["main_release_video"] = binding(replacement)
        with self.assertRaisesRegex(ValueError, "未绑定当前"):
            self.validate("qa_release_report", self.qa)
        self.registered = original
        self.inputs["audio"] = binding(replacement)
        with self.assertRaisesRegex(ValueError, "权威完整音频"):
            self.validate("qa_release_report", self.qa)

    @patch("story_artifact_validation.probe_duration", return_value=2.0)
    def test_duplicate_account_result_does_not_cover_both_videos(self, _probe):
        self.qa["results"][1] = copy.deepcopy(self.qa["results"][0])
        with self.assertRaisesRegex(ValueError, "重复"):
            self.validate("qa_release_report", self.qa)

    def test_old_v3_truncated_report_is_rejected_by_actual_duration_probe(self):
        with patch("story_artifact_validation.probe_duration", side_effect=lambda p: 10 if p in {self.voice, self.music} else 2):
            with self.assertRaisesRegex(ValueError, "完整口播时长"):
                self.validate("qa_release_report", self.qa)

    @patch("story_artifact_validation.probe_duration", return_value=2.0)
    def test_qa_cannot_hide_failing_or_nonfinite_audio_metrics(self, _probe):
        for field, value in (("music_gain", 0), ("residual_energy_ratio", 0.9), ("rms", float("nan"))):
            with self.subTest(field=field):
                payload = copy.deepcopy(self.qa)
                payload["results"][0]["audio_role_fit"][field] = value
                with self.assertRaisesRegex(ValueError, "音轨指标未通过"):
                    self.validate("qa_release_report", payload)


if __name__ == "__main__":
    unittest.main()
