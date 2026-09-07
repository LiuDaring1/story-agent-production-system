"""Default finalizer requirements checks; unrelated media uses ledger fixtures."""
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_story_run as ledger_fixtures
from story_requirements import binding
from story_run import PACKAGE_NAMES, finalize_run, init_run, load_run, record_run


class FinalizeRequirementsTests(unittest.TestCase):
    def check_case(self, mutation, expected_error=None, *, historical=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = ledger_fixtures.StoryRunLedgerTests()
            project, text, video, audio, run = fixture.fixture(root)
            init_run(run_file=run, confirmed_text=text, greenscreen_video=video,
                     audio=audio, project_dir=project)
            for package in PACKAGE_NAMES:
                record_run(run_file=run, package=package, status="done")
            fixture.record_finalize_profile(root, run)
            projection = Path(load_run(run)["artifacts"]["requirements_projection"]["path"])
            rules = root / "applicable-rules.md"
            rules.write_text("original rule", encoding="utf-8")
            payload = json.loads(projection.read_text())
            payload["rule_sources"] = [binding(rules, version="v1")]
            projection.write_text(json.dumps(payload), encoding="utf-8")
            record_run(run_file=run, package="director_plan", status="done",
                       artifact_id="requirements_projection", artifact_path=projection, replace=True)
            if historical:
                payload = load_run(run)
                review = json.loads(Path(payload["artifacts"]["final_delivery_review"]["path"]).read_text())
                target = payload["artifacts"]["customer_media_receipt"]
                review.update(schema_version="story-customer-media-independent-review/v1",
                              artifact_path=target["path"], artifact_sha256=target["sha256"])
                review_path = root / "historical-customer-review.json"
                review_path.write_text(json.dumps(review))
                record_run(run_file=run, package="product_assets", status="done",
                           artifact_id="customer_media_independent_review", artifact_path=review_path)
                payload = load_run(run)
                payload.pop("requirements_policy", None)
                payload["artifacts"].pop("requirements_projection")
                run.write_text(json.dumps(payload), encoding="utf-8")
            mutation(root, run, projection, rules)
            before = run.read_bytes()
            self.assertFalse(load_run(run).get("finalized_at"))
            compile_sha = load_run(run)["artifacts"]["shot_storyboard_compile_receipt"]["sha256"]
            # Keep projection, dependency, review and finalizer validation real.
            with contextlib.ExitStack() as stack:
                for target, value in [
                    ("story_run.validate_compile_receipt", {"shot_count": 1}),
                    ("story_run.validate_delivery_receipt", {"shot_storyboard_compile_receipt_sha256": compile_sha}),
                    ("story_run.validate_theme_assets_manifest", {}),
                    ("story_run.semantic_card_generation_receipt_issues", []),
                    ("story_run.semantic_card_motion_receipt_issues", []),
                    ("story_artifact_validation.validate_release_package_receipt", {}),
                    ("story_artifact_validation.probe_duration", 2.0),
                ]:
                    stack.enter_context(patch(target, return_value=value))
                if expected_error:
                    with self.assertRaisesRegex(RuntimeError, expected_error):
                        finalize_run(run_file=run, required_artifacts=[])
                    self.assertEqual(run.read_bytes(), before)
                    self.assertFalse(load_run(run).get("finalized_at"))
                else:
                    self.assertTrue(finalize_run(run_file=run, required_artifacts=[])["finalized_at"])

    def test_current_projection_finalizes(self):
        self.check_case(lambda *args: None)

    def test_bound_rule_change_refuses_without_success_write(self):
        self.check_case(lambda root, run, projection, rules: rules.write_text("changed"), "规则来源")

    def test_bound_rule_deletion_refuses_without_success_write(self):
        self.check_case(lambda root, run, projection, rules: rules.unlink(), "规则来源")

    def test_missing_projection_refuses_without_success_write(self):
        def mutate(root, run, projection, rules):
            payload = load_run(run)
            payload["artifacts"].pop("requirements_projection")
            run.write_text(json.dumps(payload))
        self.check_case(mutate, "缺少必需产物.*requirements_projection")

    def test_projection_hash_drift_refuses_without_success_write(self):
        self.check_case(lambda root, run, projection, rules: projection.write_text("{}"), "哈希漂移")

    def test_unrelated_rule_does_not_invalidate_projection(self):
        self.check_case(lambda root, run, projection, rules: (root / "other-rule.md").write_text("unrelated"))

    def test_historical_ledger_needs_no_projection(self):
        self.check_case(lambda *args: None, historical=True)

    def test_historical_ledger_keeps_customer_review_requirement(self):
        def mutate(root, run, projection, rules):
            payload = load_run(run)
            payload["artifacts"].pop("customer_media_independent_review", None)
            run.write_text(json.dumps(payload))
        self.check_case(mutate, "缺少必需产物.*customer_media_independent_review", historical=True)
