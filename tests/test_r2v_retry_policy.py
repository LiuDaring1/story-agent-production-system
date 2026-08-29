from __future__ import annotations

import unittest

from r2v_retry_policy import (
    POLICY_VERSION,
    RetryPolicyError,
    evaluate_quality_redos,
    retry_row_issues,
)


def rows(count: int) -> list[dict[str, str]]:
    return [
        {
            "scene": str(index),
            "retry_policy_version": POLICY_VERSION,
            "quality_retry_count": "0",
        }
        for index in range(1, count + 1)
    ]


class R2VRetryPolicyTests(unittest.TestCase):
    def test_v2_requires_evidence_but_not_escalation(self) -> None:
        approvals = evaluate_quality_redos(
            rows(10),
            {
                "01": {
                    "review_status": "redo",
                    "notes": "人物身份错误，替换冲突角色资产",
                    "defect_code": "identity_or_clone",
                }
            },
        )
        self.assertEqual(approvals["01"].next_retry_count, 1)
        self.assertFalse(approvals["01"].v3_escalation_approved)

    def test_more_than_thirty_percent_initial_redos_freeze_until_calibrated(self) -> None:
        decisions = {
            f"{index:02d}": {"review_status": "redo", "notes": "存在硬伤"}
            for index in range(1, 5)
        }
        with self.assertRaisesRegex(RetryPolicyError, "超过 30%"):
            evaluate_quality_redos(rows(10), decisions)
        decisions["01"].update(
            {
                "batch_calibration_status": "approved",
                "batch_calibration_notes": "复核退件集合后确认四镜为独立硬伤",
            }
        )
        self.assertEqual(len(evaluate_quality_redos(rows(10), decisions)), 4)

    def test_three_matching_defects_trigger_calibration_even_below_ratio(self) -> None:
        decisions = {
            f"{index:02d}": {
                "review_status": "redo",
                "notes": "角色参考冲突",
                "defect_code": "identity_or_clone",
            }
            for index in range(1, 4)
        }
        with self.assertRaisesRegex(RetryPolicyError, "同类缺陷至少出现 3 镜"):
            evaluate_quality_redos(rows(20), decisions)

    def test_v3_is_strict_and_capped_at_ten_percent(self) -> None:
        source = rows(25)
        for index in range(3):
            source[index]["quality_retry_count"] = "1"
        base = {
            "review_status": "redo",
            "notes": "叙事因果仍然错误",
            "defect_code": "beat_order",
            "defect_severity": "hard",
            "root_cause": "原镜头动作阶段过密",
            "retry_strategy": "重分配节拍并删除无关动作",
            "v3_escalation_approved": "true",
        }
        decisions = {f"{index:02d}": dict(base) for index in range(1, 4)}
        decisions["01"].update(
            {
                "batch_calibration_status": "approved",
                "batch_calibration_notes": "复核后确认三个相同缺陷分别阻断各镜交付",
            }
        )
        approvals = evaluate_quality_redos(source, decisions)
        self.assertTrue(all(item.next_retry_count == 2 for item in approvals.values()))

        source[3]["quality_retry_count"] = "1"
        decisions["04"] = dict(base)
        with self.assertRaisesRegex(RetryPolicyError, "V3 名额不足"):
            evaluate_quality_redos(source, decisions)

    def test_quality_version_above_limit_is_detected_as_csv_tampering(self) -> None:
        source = rows(10)
        source[0].update(
            {
                "quality_retry_count": "3",
                "retry_evidence": "still broken",
                "v3_escalation_approved": "true",
            }
        )
        issues = retry_row_issues(source)
        self.assertTrue(any("超过允许上限" in issue for issue in issues), issues)

    def test_manual_batch_retry_edit_cannot_bypass_calibration_gate(self) -> None:
        source = rows(10)
        for row in source[:4]:
            row.update(
                {
                    "quality_retry_count": "1",
                    "retry_evidence": "交付硬伤",
                    "retry_defect_code": "director_review",
                }
            )
        issues = retry_row_issues(source)
        self.assertTrue(any("缺少付费前退件校准记录" in issue for issue in issues), issues)
        source[0].update(
            {
                "batch_retry_calibration_status": "approved",
                "batch_retry_calibration_notes": "轻量校准确认四镜均为真实硬伤",
            }
        )
        self.assertFalse(any("校准记录" in issue for issue in retry_row_issues(source)))


if __name__ == "__main__":
    unittest.main()
