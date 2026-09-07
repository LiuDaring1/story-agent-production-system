#!/usr/bin/env python3
"""Validate the story-directing offline benchmark contract without providers."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


REQUIRED_CASES = {
    "dialogue-reaction", "three-character-reverse", "future-imagination",
    "decreasing-prop", "offscreen-character", "long-dialogue-visual-duty",
    "review-preference-vs-defect",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="校验不调用供应商的故事导演/审核离线基准")
    parser.add_argument("--cases", type=Path, default=Path("benchmarks/story_offline_cases.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--candidate", type=Path, action="append",
        help="待评分候选 JSON；可重复传入。省略时使用仓库内离线结构夹具",
    )
    args = parser.parse_args()
    started = time.perf_counter()
    payload = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if payload.get("schema_version") != "story-offline-directing-benchmark/v1" or not isinstance(cases, list):
        raise ValueError("离线基准 schema 无效")
    ids = {str(item.get("case_id") or "") for item in cases if isinstance(item, dict)}
    missing = sorted(REQUIRED_CASES - ids)
    malformed = [
        item.get("case_id") for item in cases if not isinstance(item, dict)
        or not str(item.get("source") or "").strip()
        or not isinstance(item.get("required"), list) or not item.get("required")
        or not isinstance(item.get("rubric"), dict)
        or item["rubric"].get("passing_score") != 85
        or set(item["rubric"].get("critical_checks") or []) != set(item.get("required") or [])
        or not item["rubric"].get("creative_dimensions")
    ]
    candidate_paths = args.candidate or [Path("benchmarks/story_offline_candidate_fixture.json")]
    candidate_results = []
    case_by_id = {item["case_id"]: item for item in cases if isinstance(item, dict) and item.get("case_id")}
    for candidate_path in candidate_paths:
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        rows = candidate.get("cases") if isinstance(candidate, dict) else None
        if candidate.get("schema_version") != "story-offline-directing-candidate/v1" or not isinstance(rows, list):
            raise ValueError(f"候选 schema 无效：{candidate_path}")
        rows_by_id = {
            str(row.get("case_id") or ""): row
            for row in rows if isinstance(row, dict) and row.get("case_id")
        }
        per_case = []
        for case_id, case in case_by_id.items():
            row = rows_by_id.get(case_id, {})
            evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
            required = case["rubric"]["critical_checks"]
            covered = [name for name in required if str(evidence.get(name) or "").strip()]
            critical = row.get("critical_errors") if isinstance(row.get("critical_errors"), list) else ["critical_errors_missing"]
            score = round(100 * len(covered) / len(required), 2)
            per_case.append({
                "case_id": case_id, "score": score,
                "missing_checks": sorted(set(required) - set(covered)),
                "critical_errors": critical,
                "passed": score >= case["rubric"]["passing_score"] and not critical,
            })
        creative = candidate.get("creative_review")
        creative_result = None
        if creative is not None:
            if (
                not isinstance(creative, dict)
                or creative.get("reviewer_independent") is not True
                or not isinstance(creative.get("score"), (int, float))
                or isinstance(creative.get("score"), bool)
                or not isinstance(creative.get("critical_errors"), list)
            ):
                raise ValueError(f"候选 creative_review 无效：{candidate_path}")
            creative_result = {
                "score": float(creative["score"]),
                "critical_errors": creative["critical_errors"],
                "passed": float(creative["score"]) >= 85 and not creative["critical_errors"],
            }
        candidate_results.append({
            "candidate_id": str(candidate.get("candidate_id") or candidate_path.stem),
            "source_kind": str(candidate.get("source_kind") or "unknown"),
            "per_case": per_case,
            "structural_score": round(sum(row["score"] for row in per_case) / max(1, len(per_case)), 2),
            "structural_passed": all(row["passed"] for row in per_case),
            "creative_review": creative_result,
        })
    elapsed = round(time.perf_counter() - started, 6)
    result = {
        "schema_version": "story-offline-benchmark-result/v1",
        "case_count": len(cases), "missing_cases": missing, "malformed_cases": malformed,
        "structure_passed": not missing and not malformed,
        "elapsed_seconds": elapsed,
        "provider_requests": 0,
        "scoring_rubric": {
            "per_case_pass": "all declared critical checks evidenced, score >= 85, critical_errors empty",
            "creative_dimensions": [
                "child_comprehension", "locked_constraint_fidelity", "performable_beats",
                "continuity", "review_discipline",
            ],
            "creative_review_required_for_quality_claim": True,
        },
        "candidate_results": candidate_results,
        "performance": {
            "plan_duration_seconds": None,
            "machine_check_duration_seconds": elapsed,
            "independent_review_rounds": None,
            "invalid_rejection_count": None,
            "duplicate_encode_count": 0,
            "duplicate_provider_request_count": 0,
            "longest_dependency_chain": 0,
        },
        "quality_claim": "structure-only; does not prove model intelligence or finished-film quality",
        "creative_evaluation_pending": payload.get("creative_evaluation", {}).get("pending"),
    }
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if result["structure_passed"] and all(
        item["structural_passed"] for item in candidate_results
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
