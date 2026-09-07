"""Paid R2V quality-retry gates for the Codex-native story workflow.

Infrastructure retries that never yield a playable review candidate are tracked
by the provider adapter and are deliberately separate from these quality
versions.  This module governs only director-requested regeneration of a
playable R2V shot.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Mapping, Sequence


POLICY_VERSION = "story-r2v-retry-policy/v2"
DEFAULT_MAX_QUALITY_VERSIONS = 2
ESCALATED_MAX_QUALITY_VERSIONS = 3
V3_QUOTA_RATIO = 0.10
BATCH_RETRY_FREEZE_RATIO = 0.30
REPEATED_DEFECT_FREEZE_COUNT = 3


class RetryPolicyError(ValueError):
    """Raised before any files move or paid retry is submitted."""


@dataclass(frozen=True)
class RetryApproval:
    scene: str
    prior_retry_count: int
    next_retry_count: int
    defect_code: str
    evidence: str
    root_cause: str
    retry_strategy: str
    severity: str
    v3_escalation_approved: bool
    batch_calibration_status: str
    batch_calibration_notes: str


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "approved", "是", "通过"}


def _retry_count(row: Mapping[str, object]) -> int:
    raw = row.get("quality_retry_count")
    if raw is None or str(raw).strip() == "":
        return 0
    try:
        value = int(str(raw))
    except ValueError as exc:
        raise RetryPolicyError(f"镜头 {row.get('scene', '?')} 的 quality_retry_count 无效") from exc
    if value < 0:
        raise RetryPolicyError(f"镜头 {row.get('scene', '?')} 的 quality_retry_count 不能为负数")
    return value


def _decision_text(decision: Mapping[str, object], field: str, fallback: str = "") -> str:
    return str(decision.get(field) or fallback or "").strip()


def evaluate_quality_redos(
    rows: Sequence[Mapping[str, object]],
    decisions: Mapping[str, Mapping[str, object]],
) -> dict[str, RetryApproval]:
    """Validate a targeted redo batch and return per-scene approval metadata.

    ``decisions`` is keyed by zero-padded scene number.  Only rows whose review
    status is ``redo`` should be included by the caller.
    """

    if not rows:
        return {}
    row_by_scene = {
        str(row.get("scene") or "").strip().zfill(2): row
        for row in rows
        if str(row.get("scene") or "").strip()
    }
    requested_redos = sorted(
        scene
        for scene, decision in decisions.items()
        if _decision_text(decision, "review_status").lower() == "redo"
    )
    # A preference or advisory is retained by the review file, but it is not a
    # blocking defect and must never become a generation request.
    redo_scenes = [
        scene
        for scene in requested_redos
        if _decision_text(decisions[scene], "defect_severity").lower()
        in {"hard", "critical"}
    ]
    if not redo_scenes:
        return {}
    missing = [scene for scene in redo_scenes if scene not in row_by_scene]
    if missing:
        raise RetryPolicyError("审核 CSV 含未知镜头：" + ", ".join(missing))

    initial_redos = [scene for scene in redo_scenes if _retry_count(row_by_scene[scene]) == 0]
    initial_redo_ratio = len(initial_redos) / max(1, len(row_by_scene))
    defect_counts = Counter(
        _decision_text(decisions[scene], "defect_code")
        for scene in redo_scenes
        if _decision_text(decisions[scene], "defect_code")
    )
    repeated_defects = sorted(
        code for code, count in defect_counts.items() if count >= REPEATED_DEFECT_FREEZE_COUNT
    )
    calibration_required = (
        initial_redo_ratio > BATCH_RETRY_FREEZE_RATIO or bool(repeated_defects)
    )
    calibration_approved = any(
        _truthy(decisions[scene].get("batch_calibration_status"))
        and _decision_text(decisions[scene], "batch_calibration_notes")
        for scene in redo_scenes
    )
    if calibration_required and not calibration_approved:
        reasons: list[str] = []
        if initial_redo_ratio > BATCH_RETRY_FREEZE_RATIO:
            reasons.append(
                f"首轮退件 {len(initial_redos)}/{len(row_by_scene)}={initial_redo_ratio:.1%}，超过 30%"
            )
        if repeated_defects:
            reasons.append("同类缺陷至少出现 3 镜：" + ", ".join(repeated_defects))
        raise RetryPolicyError(
            "R2V 付费重做已冻结："
            + "；".join(reasons)
            + "。请先轻量复核退件集合与相邻连续预览，并填写 "
            "batch_calibration_status=approved 和 batch_calibration_notes。"
        )

    existing_v3 = sum(_retry_count(row) >= 2 for row in row_by_scene.values())
    requested_v3 = sum(_retry_count(row_by_scene[scene]) == 1 for scene in redo_scenes)
    v3_quota = max(1, math.ceil(len(row_by_scene) * V3_QUOTA_RATIO))
    if existing_v3 + requested_v3 > v3_quota:
        raise RetryPolicyError(
            f"V3 名额不足：正文 {len(row_by_scene)} 镜最多 {v3_quota} 镜进入 V3，"
            f"已有 {existing_v3} 镜，本次申请 {requested_v3} 镜；本轮停止新增质量版本。"
        )

    approvals: dict[str, RetryApproval] = {}
    for scene in redo_scenes:
        row = row_by_scene[scene]
        decision = decisions[scene]
        prior = _retry_count(row)
        if prior >= 2:
            raise RetryPolicyError(
                f"镜头 {scene} 已达到质量版本上限 V3；本轮停止新增质量版本。"
            )
        evidence = _decision_text(decision, "evidence")
        defect_code = _decision_text(decision, "defect_code")
        root_cause = _decision_text(decision, "root_cause")
        retry_strategy = _decision_text(decision, "retry_strategy")
        severity = _decision_text(decision, "defect_severity").lower()
        required_basis = {
            "defect_code": defect_code,
            "requirement_source": _decision_text(decision, "requirement_source"),
            "requirement_scope": _decision_text(decision, "requirement_scope"),
            "artifact_sha256": _decision_text(decision, "artifact_sha256"),
            "evidence": evidence,
            "delivery_impact": _decision_text(decision, "delivery_impact"),
            "retry_strategy": retry_strategy,
        }
        missing_basis = [name for name, value in required_basis.items() if not value]
        artifact_sha = required_basis["artifact_sha256"].lower()
        if artifact_sha and (
            len(artifact_sha) != 64
            or any(ch not in "0123456789abcdef" for ch in artifact_sha)
        ):
            missing_basis.append("artifact_sha256(valid)")
        current_artifact_sha = _decision_text(row, "current_artifact_sha256").lower()
        if not current_artifact_sha:
            raise RetryPolicyError(
                f"镜头 {scene} 缺少当前可播放视频哈希；先补当前证据，不提交重做"
            )
        if artifact_sha != current_artifact_sha:
            raise RetryPolicyError(
                f"镜头 {scene} 退件绑定的 artifact_sha256 不是当前可播放视频；"
                "先纠正审核，不提交重做"
            )
        if missing_basis:
            raise RetryPolicyError(
                f"镜头 {scene} 退件缺少当前规则/范围/产物证据："
                + ", ".join(missing_basis)
                + "；先纠正审核，不提交重做"
            )
        previous_evidence = _decision_text(decision, "previous_retry_evidence")
        previous_sha = _decision_text(decision, "previous_artifact_sha256").lower()
        if previous_evidence == evidence and previous_sha == artifact_sha:
            raise RetryPolicyError(
                f"镜头 {scene} 在相同产物哈希上重复相同理由且无新证据；"
                "停止审核循环，不伪造通过"
            )
        v3_approved = _truthy(decision.get("v3_escalation_approved"))
        if prior == 1:
            missing_v3 = [
                name
                for name, value in (
                    ("defect_severity=hard/critical", severity in {"hard", "critical"}),
                    ("root_cause", bool(root_cause)),
                    ("retry_strategy", bool(retry_strategy)),
                    ("v3_escalation_approved", v3_approved),
                )
                if not value
            ]
            if missing_v3:
                raise RetryPolicyError(
                    f"镜头 {scene} 申请 V3 缺少升级证据：" + ", ".join(missing_v3)
                )
        approvals[scene] = RetryApproval(
            scene=scene,
            prior_retry_count=prior,
            next_retry_count=prior + 1,
            defect_code=defect_code,
            evidence=evidence,
            root_cause=root_cause,
            retry_strategy=retry_strategy,
            severity=severity,
            v3_escalation_approved=v3_approved,
            batch_calibration_status=("approved" if calibration_approved else "not_required"),
            batch_calibration_notes=next(
                (
                    _decision_text(decisions[item], "batch_calibration_notes")
                    for item in redo_scenes
                    if _truthy(decisions[item].get("batch_calibration_status"))
                    and _decision_text(decisions[item], "batch_calibration_notes")
                ),
                "",
            ),
        )
    return approvals


def retry_row_issues(rows: Sequence[Mapping[str, object]]) -> list[str]:
    """Fail closed on manually edited native jobs before a paid submission."""

    tagged = [row for row in rows if str(row.get("retry_policy_version") or "").strip()]
    if not tagged:
        return []
    issues: list[str] = []
    v3_count = 0
    quality_redo_rows: list[Mapping[str, object]] = []
    for row in tagged:
        scene = str(row.get("scene") or "?")
        if str(row.get("retry_policy_version") or "") != POLICY_VERSION:
            issues.append(f"第 {scene} 镜 retry_policy_version 无效")
            continue
        try:
            count = _retry_count(row)
        except RetryPolicyError as exc:
            issues.append(str(exc))
            continue
        if count > 2:
            issues.append(f"第 {scene} 镜 quality_retry_count 超过允许上限 2")
        if count >= 1 and not str(row.get("retry_evidence") or "").strip():
            issues.append(f"第 {scene} 镜付费重做缺少 retry_evidence")
        if count >= 1:
            quality_redo_rows.append(row)
        if count >= 2:
            v3_count += 1
            if not _truthy(row.get("v3_escalation_approved")):
                issues.append(f"第 {scene} 镜 V3 未通过升级门")
    quota = max(1, math.ceil(len(tagged) * V3_QUOTA_RATIO))
    if v3_count > quota:
        issues.append(f"V3 镜头 {v3_count} 个，超过 {len(tagged)} 镜的 10% 名额 {quota} 个")
    repeated_defects = Counter(
        str(row.get("retry_defect_code") or "").strip()
        for row in quality_redo_rows
        if str(row.get("retry_defect_code") or "").strip()
    )
    calibration_required = (
        len(quality_redo_rows) / max(1, len(tagged)) > BATCH_RETRY_FREEZE_RATIO
        or any(count >= REPEATED_DEFECT_FREEZE_COUNT for count in repeated_defects.values())
    )
    calibration_current = any(
        str(row.get("batch_retry_calibration_status") or "").strip().lower() == "approved"
        and str(row.get("batch_retry_calibration_notes") or "").strip()
        for row in tagged
    )
    if calibration_required and not calibration_current:
        issues.append("整组重做超过 30% 或同类缺陷至少 3 镜，但缺少付费前退件校准记录")
    return issues
