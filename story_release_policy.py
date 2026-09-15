"""Shared v2 release safety policy for planning, rendering, QA, and review."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from presenter_layout import (
    BODY_OVERFLOW_POLICY,
    PRESENTER_LAYOUT_POLICY,
    body_overflow_cache_key,
    severe_body_overflow_windows,
)


WINDOWS_PLAN_SCHEMA = "story-project-release-windows-plan/v2"
FRAME_DERIVATION_SCHEMA = "story-shared-frame-derivation/v1"
RELEASE_REVIEW_EVIDENCE_SCHEMA = "story-release-review-evidence/v1"
PRESENTER_REQUIREMENT_ID = "presenter-body-overflow-protection"
FRAME_REQUIREMENT_ID = "single-story-frame-source"


def release_safety_requirement_parameters() -> dict[str, dict[str, object]]:
    return {
        PRESENTER_REQUIREMENT_ID: {
            "person_layout_policy": PRESENTER_LAYOUT_POLICY,
            "body_core_quantiles": list(BODY_OVERFLOW_POLICY["body_core_quantiles"]),
            "sample_fps": BODY_OVERFLOW_POLICY["sample_fps"],
            "visible_threshold": BODY_OVERFLOW_POLICY["visible_threshold"],
            "trigger_threshold": BODY_OVERFLOW_POLICY["trigger_threshold"],
            "minimum_seconds": BODY_OVERFLOW_POLICY["minimum_seconds"],
            "padding_seconds": BODY_OVERFLOW_POLICY["padding_seconds"],
            "protection_modes": ["b", "c"],
            "dynamic_presenter_transform": False,
        },
        FRAME_REQUIREMENT_ID: {
            "mother_asset_count": 1,
            "a_b_derivation": "deterministic_fit_from_same_mother_asset",
            "independent_b_asset_allowed": False,
        },
    }


def release_safety_requirements(*, source: str) -> list[dict[str, object]]:
    parameters = release_safety_requirement_parameters()
    return [
        {
            "requirement_id": PRESENTER_REQUIREMENT_ID,
            "source": source,
            "scope": "v2 main-account A/B/C planning, rendering, QA, and review",
            "requirement": "Keep the fixed A anchor; persistent severe torso overflow must be covered by B/C using the existing thresholds.",
            "parameters": parameters[PRESENTER_REQUIREMENT_ID],
        },
        {
            "requirement_id": FRAME_REQUIREMENT_ID,
            "source": source,
            "scope": "v2 main-account A/B frame generation, rendering, QA, and review",
            "requirement": "Generate one story-frame mother asset and derive A/B layouts deterministically from it; no independent B design.",
            "parameters": parameters[FRAME_REQUIREMENT_ID],
        },
    ]


def validate_release_safety_projection(payload: Mapping[str, object]) -> dict[str, object]:
    """Require the complete executable release policy in the shared projection."""

    expected = release_safety_requirement_parameters()
    rows = {
        str(row.get("requirement_id") or ""): row
        for row in payload.get("requirements", [])
        if isinstance(row, Mapping)
    }
    missing = [key for key in expected if key not in rows]
    if missing:
        raise ValueError("v2 release applicable requirements missing: " + ", ".join(missing))
    for requirement_id, parameters in expected.items():
        if rows[requirement_id].get("parameters") != parameters:
            raise ValueError(f"v2 release applicable requirement parameters stale: {requirement_id}")
    evidence = set(payload.get("acceptance_evidence") or [])
    required_evidence = {
        "presenter_body_overflow_report",
        "release_plan_compliance",
        "decoded_video_execution",
        "shared_frame_derivation",
        "independent_visual_review",
    }
    if not required_evidence.issubset(evidence):
        raise ValueError("v2 release applicable requirements omit acceptance evidence: " + ", ".join(sorted(required_evidence - evidence)))
    return dict(payload)


def merge_windows(windows: Iterable[tuple[float, float]], *, duration: float) -> list[tuple[float, float]]:
    normalized: list[tuple[float, float]] = []
    for raw_start, raw_end in sorted(windows):
        start = max(0.0, min(float(duration), float(raw_start)))
        end = max(0.0, min(float(duration), float(raw_end)))
        if not all(math.isfinite(value) for value in (start, end)) or end <= start:
            continue
        if normalized and start <= normalized[-1][1] + 0.05:
            normalized[-1] = (normalized[-1][0], max(normalized[-1][1], end))
        else:
            normalized.append((start, end))
    return [(round(start, 3), round(end, 3)) for start, end in normalized]


def subtract_windows(
    windows: Iterable[tuple[float, float]],
    protected: Sequence[tuple[float, float]],
    *,
    duration: float,
) -> list[tuple[float, float]]:
    remaining: list[tuple[float, float]] = []
    for start, end in merge_windows(sorted(windows), duration=duration):
        fragments = [(start, end)]
        for cover_start, cover_end in merge_windows(sorted(protected), duration=duration):
            next_fragments: list[tuple[float, float]] = []
            for left, right in fragments:
                if cover_end <= left or cover_start >= right:
                    next_fragments.append((left, right))
                else:
                    if cover_start > left:
                        next_fragments.append((left, min(right, cover_start)))
                    if cover_end < right:
                        next_fragments.append((max(left, cover_end), right))
            fragments = next_fragments
        remaining.extend(fragments)
    return merge_windows(sorted(remaining), duration=duration)


def cover_presenter_risks(
    b_windows: Sequence[tuple[float, float]],
    c_windows: Sequence[tuple[float, float]],
    severe_windows: Sequence[tuple[float, float]],
    *,
    duration: float,
) -> tuple[list[tuple[float, float]], list[dict[str, object]]]:
    """Add only the risk portions not already protected by B/C to B."""

    base_b = merge_windows(sorted(b_windows), duration=duration)
    base_c = merge_windows(sorted(c_windows), duration=duration)
    additions = subtract_windows(severe_windows, [*base_b, *base_c], duration=duration)
    result_b = merge_windows(sorted([*base_b, *additions]), duration=duration)
    coverage = []
    for start, end in merge_windows(sorted(severe_windows), duration=duration):
        covered = subtract_windows([(start, end)], [*result_b, *base_c], duration=duration)
        coverage.append({
            "risk_window": [start, end],
            "covered_by": [
                mode for mode, windows in (("b", result_b), ("c", base_c))
                if any(left < end and right > start for left, right in windows)
            ],
            "uncovered": [list(window) for window in covered],
        })
    return result_b, coverage


def format_windows(windows: Sequence[tuple[float, float]]) -> str:
    return ",".join(f"{start:.3f}-{end:.3f}" for start, end in windows)


def presenter_scan_cache_path(
    project_root: Path,
    foreground: Path,
    **parameters: object,
) -> tuple[Path, dict[str, object]]:
    cache_key = body_overflow_cache_key(foreground, **parameters)
    digest = hashlib.sha256(
        json.dumps(cache_key, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return project_root / "99_项目状态" / "presenter_body_overflow" / f"{digest}.json", cache_key


def validate_presenter_scan_report(path: Path) -> dict[str, object]:
    from story_production_v2 import current
    from story_scene_windows import current_rule

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"presenter overflow evidence missing or invalid: {path}") from exc
    if payload.get("schema_version") != BODY_OVERFLOW_POLICY["schema_version"] or payload.get("scan_complete") is not True:
        raise ValueError("presenter overflow evidence schema/incomplete")
    source = current(payload.get("source") or {})
    current_rule(payload.get("scanner_code") or {})
    key = payload.get("cache_key")
    if not isinstance(key, Mapping):
        raise ValueError("presenter overflow cache identity missing")
    actual_key = body_overflow_cache_key(
        source,
        fixed_anchor_x=int(key.get("fixed_anchor_x")),
        fixed_anchor_y=int(key.get("fixed_anchor_y")),
        canvas_width=int(key.get("canvas_width")),
        source_width=int(key.get("source_width")),
        source_height=int(key.get("source_height")),
        rendered_height=int(key.get("rendered_height")),
        person_crop=key.get("person_crop"),
        person_layout_policy=str(key.get("person_layout_policy") or ""),
        sample_fps=float(key.get("sample_fps")),
        visible_threshold=float(key.get("visible_threshold")),
        trigger_threshold=float(key.get("trigger_threshold")),
        minimum_seconds=float(key.get("minimum_seconds")),
        padding_seconds=float(key.get("padding_seconds")),
    )
    if dict(key) != actual_key:
        raise ValueError("presenter overflow evidence is stale for source, geometry, parameters, or code")
    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples or payload.get("sample_count") != len(samples):
        raise ValueError("presenter overflow evidence lacks complete samples")
    duration = payload.get("duration_seconds")
    if duration is not None and len(samples) < max(1, math.floor(float(duration) * float(key["sample_fps"])) - 2):
        raise ValueError("presenter overflow evidence does not cover the authoritative duration")
    pairs = []
    for row in samples:
        if not isinstance(row, Mapping):
            raise ValueError("presenter overflow sample invalid")
        timestamp = float(row.get("time"))
        visible = row.get("visible_core_fraction")
        pairs.append((timestamp, None if visible is None else float(visible)))
    expected = severe_body_overflow_windows(
        pairs,
        visible_threshold=float(key["visible_threshold"]),
        trigger_threshold=float(key["trigger_threshold"]),
        minimum_seconds=float(key["minimum_seconds"]),
        sample_fps=float(key["sample_fps"]),
        padding_seconds=float(key["padding_seconds"]),
        duration=float(duration) if duration is not None else None,
    )
    if [list(window) for window in expected] != payload.get("severe_windows"):
        raise ValueError("presenter overflow windows do not recompute from recorded samples")
    return payload


def validate_frame_derivation(path: Path, *, expected_mother: Path | None = None) -> dict[str, object]:
    from story_production_v2 import current

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != FRAME_DERIVATION_SCHEMA:
        raise ValueError("shared frame derivation schema invalid")
    mother = current(payload.get("mother_asset") or {})
    if expected_mother is not None and mother.resolve() != expected_mother.resolve():
        raise ValueError("A/B frame derivation uses another mother asset")
    if payload.get("independent_b_asset") is not False:
        raise ValueError("independent B frame asset is forbidden for v2")
    derivatives = payload.get("derivatives")
    if not isinstance(derivatives, Mapping) or set(derivatives) != {"a", "b"}:
        raise ValueError("A/B frame derivation is incomplete")
    for mode, item in derivatives.items():
        if not isinstance(item, Mapping) or item.get("source_sha256") != payload["mother_asset"]["sha256"]:
            raise ValueError(f"{mode.upper()} frame does not bind the shared mother asset")
        if item.get("method") != "deterministic_fit_to_story_window":
            raise ValueError(f"{mode.upper()} frame derivation method invalid")
        current(item.get("output") or {})
    return payload


def validate_release_review_evidence(
    path: Path,
    *,
    expected_requirements: Mapping[str, object] | None = None,
    expected_mother: Path | None = None,
) -> dict[str, object]:
    """Validate the once-prepared visual evidence before review or approval."""
    from story_production_v2 import current
    from story_scene_windows import validate_plan

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("release review evidence missing or invalid") from exc
    if payload.get("schema_version") != RELEASE_REVIEW_EVIDENCE_SCHEMA:
        raise ValueError("release review evidence schema invalid")
    plan_path = current(payload.get("plan") or {})
    plan = validate_plan(plan_path)
    requirements_path = current(payload.get("applicable_requirements") or {})
    validate_release_safety_projection(json.loads(requirements_path.read_text(encoding="utf-8")))
    if expected_requirements is not None:
        observed = payload["applicable_requirements"]
        if (
            Path(str(observed.get("path") or "")).resolve()
            != Path(str(expected_requirements.get("path") or "")).resolve()
            or observed.get("sha256") != expected_requirements.get("sha256")
        ):
            raise ValueError("release review evidence binds another applicable-requirements projection")
    report_path = current(payload.get("presenter_overflow_report") or {})
    validate_presenter_scan_report(report_path)
    if payload["presenter_overflow_report"] != plan["presenter_protection"]["report"]:
        raise ValueError("release review evidence binds another presenter scan")
    validate_frame_derivation(
        current(payload.get("frame_derivation") or {}), expected_mother=expected_mother,
    )
    prepared = payload.get("prepared_frames")
    if not isinstance(prepared, list) or not prepared:
        raise ValueError("release review evidence has no actual prepared frames")
    expected = {
        round(float(row["time_seconds"]), 3): (
            str(row["expected_mode"]), tuple(row["reasons"])
        )
        for row in plan["review_samples"]
    }
    observed_times = set()
    for row in prepared:
        if not isinstance(row, Mapping):
            raise ValueError("release review prepared-frame row invalid")
        timestamp = round(float(row.get("time_seconds")), 3)
        observed_times.add(timestamp)
        if timestamp not in expected:
            raise ValueError("release review evidence contains an unplanned frame time")
        mode, reasons = expected[timestamp]
        if str(row.get("mode") or "") != mode or tuple(row.get("reasons") or []) != reasons:
            raise ValueError("release review prepared frame does not match plan mode/reasons")
        current(row.get("frame") or {})
    if observed_times != set(expected):
        raise ValueError("release review evidence omits required plan samples")
    return payload


__all__ = [
    "FRAME_DERIVATION_SCHEMA",
    "FRAME_REQUIREMENT_ID",
    "PRESENTER_REQUIREMENT_ID",
    "RELEASE_REVIEW_EVIDENCE_SCHEMA",
    "WINDOWS_PLAN_SCHEMA",
    "cover_presenter_risks",
    "format_windows",
    "presenter_scan_cache_path",
    "release_safety_requirements",
    "release_safety_requirement_parameters",
    "validate_frame_derivation",
    "validate_presenter_scan_report",
    "validate_release_review_evidence",
    "validate_release_safety_projection",
]
