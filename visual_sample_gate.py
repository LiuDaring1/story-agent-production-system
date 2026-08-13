"""Deterministic visual-sample planning and approval gates.

The Story Production Contract remains the only rule source.  This module
compiles the reviewed visual projection into the smallest set of samples that
the current story actually needs, verifies the resulting files, and binds an
independent review with a crash-safe lock before batch image generation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from story_agent_runtime import file_sha256, review_bundle_is_current, review_passes
from story_contract_consumers import load_consumer_context, projection_sha256, write_json_atomic
from story_contract_runtime import (
    contract_consumer_context_is_current,
    contract_paths,
)
from story_contracts import canonical_json_bytes, load_story_contract


VISUAL_SAMPLE_SCHEMA_VERSION = "1.0"
VISUAL_SAMPLE_COMPILER_VERSION = "m2-2a1.1"
VISUAL_SAMPLE_LOCK_VERSION = 1
VISUAL_SAMPLE_SCHEMA_PATH = (
    Path(__file__).resolve().parent
    / "schemas"
    / "visual_sample_gate"
    / "v1"
    / "visual_sample_plan.schema.json"
)

SAMPLE_KINDS = ("style_anchor", "character_sheet", "scale_anchor", "state_anchor")
P0_CATEGORIES = frozenset(
    {
        "unsupported_identity_feature",
        "anatomy_or_organ_error",
        "character_identity_mismatch",
        "scale_contradiction",
        "state_contradiction",
        "unsafe_or_unsuitable_for_children",
        "unusable_composition",
    }
)


def visual_sample_paths(project_root: Path | str) -> dict[str, Path]:
    root = Path(project_root)
    status = root / "99_项目状态"
    directory = status / "visual_samples"
    reviews = status / "reviews"
    return {
        "directory": directory,
        "assets": directory / "assets",
        "plan": directory / "visual_sample_plan.json",
        "handoff": directory / "visual_sample_handoff.md",
        "machine_qa": directory / "visual_sample_machine_qa.json",
        "supplemental_request": directory / "supplemental_request.json",
        "lock": directory / "visual_sample.lock.json",
        "bundle": reviews / "visual_sample_bundle.json",
        "review": reviews / "visual_sample_review_review.json",
    }


def _project_relative(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


def _ready_asset(path: Path, root: Path) -> dict[str, str] | None:
    if not path.is_file():
        return None
    return {"path": _project_relative(path, root), "sha256": file_sha256(path)}


def _preview_covers(preview: Mapping[str, Any], kind: str, refs: list[str], root: Path) -> bool:
    if preview.get("kind") != kind or not preview.get("path") or not preview.get("sha256"):
        return False
    if refs and not set(refs).issubset({str(value) for value in preview.get("content_refs", [])}):
        return False
    target = root / str(preview["path"])
    return target.is_file() and file_sha256(target) == preview.get("sha256")


def _sample_requirement(
    *,
    root: Path,
    kind: str,
    reason: str,
    contract_paths_: list[str],
    refs: list[str],
    previews: list[Mapping[str, Any]],
    force_supplemental: bool = False,
) -> dict[str, Any]:
    reused = None if force_supplemental else next(
        (item for item in previews if _preview_covers(item, kind, refs, root)), None
    )
    if reused is not None:
        asset = _ready_asset(root / str(reused["path"]), root)
        return {
            "sample_id": kind,
            "kind": kind,
            "need_reason": reason,
            "contract_paths": contract_paths_,
            "content_refs": refs,
            "fulfillment": "contract_preview",
            "source_preview_id": str(reused["preview_id"]),
            "expected_path": str(reused["path"]),
            "asset": asset,
        }
    target = root / "99_项目状态" / "visual_samples" / "assets" / f"{kind}.png"
    return {
        "sample_id": kind,
        "kind": kind,
        "need_reason": reason,
        "contract_paths": contract_paths_,
        "content_refs": refs,
        "fulfillment": "supplemental_sample",
        "source_preview_id": None,
        "expected_path": _project_relative(target, root),
        "asset": _ready_asset(target, root),
    }


def compile_visual_sample_plan(project_root: Path | str, context_path: Path | str) -> dict[str, Any]:
    """Compile only story-applicable samples from the current reviewed contract."""

    root = Path(project_root)
    context = load_consumer_context(context_path, "storyboard_images")
    contract = load_story_contract(contract_paths(root)["contract"])
    projection = context["contract_projection"]
    characters = projection.get("characters", {})
    character_rows = characters.get("characters", []) if isinstance(characters, Mapping) else []
    character_ids = [str(item["character_id"]) for item in character_rows if isinstance(item, Mapping)]
    scale = projection.get("world_scale", {})
    scale_rows = scale.get("relationships", []) if isinstance(scale, Mapping) else []
    scale_ids = [str(item["relationship_id"]) for item in scale_rows if isinstance(item, Mapping)]
    state = projection.get("story_state", {})
    state_rows = state.get("machines", []) if isinstance(state, Mapping) else []
    state_ids = [str(item["machine_id"]) for item in state_rows if isinstance(item, Mapping)]
    previews = [item for item in contract.get("preview_assets", []) if isinstance(item, Mapping)]
    request_path = visual_sample_paths(root)["supplemental_request"]
    forced: set[str] = set()
    supplemental_request: dict[str, Any] | None = None
    try:
        candidate = json.loads(request_path.read_text(encoding="utf-8"))
        if (
            isinstance(candidate, dict)
            and candidate.get("version") == 1
            and candidate.get("story_contract_dependency_sha256")
            == context["story_contract_dependency_sha256"]
            and isinstance(candidate.get("sample_ids"), list)
        ):
            forced = {str(item) for item in candidate["sample_ids"] if str(item) in SAMPLE_KINDS}
            supplemental_request = candidate
    except (OSError, json.JSONDecodeError):
        pass

    requirements = [
        _sample_requirement(
            root=root,
            kind="style_anchor",
            reason="Every visual story needs one reviewed style and rendering-quality anchor before batch generation.",
            contract_paths_=["contracts.visual_style"],
            refs=[],
            previews=previews,
            force_supplemental="style_anchor" in forced,
        )
    ]
    if characters.get("mode") == "present" and character_ids:
        requirements.append(
            _sample_requirement(
                root=root,
                kind="character_sheet",
                reason="Recurring contract-declared characters need identity and natural-anatomy evidence.",
                contract_paths_=["contracts.characters"],
                refs=character_ids,
                previews=previews,
                force_supplemental="character_sheet" in forced,
            )
        )
    if scale_ids:
        requirements.append(
            _sample_requirement(
                root=root,
                kind="scale_anchor",
                reason="Declared qualitative or evidence-backed scale relationships need a visual comparison.",
                contract_paths_=["contracts.world_scale"],
                refs=scale_ids,
                previews=previews,
                force_supplemental="scale_anchor" in forced,
            )
        )
    if state_ids:
        requirements.append(
            _sample_requirement(
                root=root,
                kind="state_anchor",
                reason="State-changing entities need visual evidence for every declared state before batch generation.",
                contract_paths_=["contracts.story_state"],
                refs=state_ids,
                previews=previews,
                force_supplemental="state_anchor" in forced,
            )
        )

    identity_policy = {
        "mode": "deny_unlisted",
        "characters": [
            {
                "character_id": str(item.get("character_id", "")),
                "identity_anchors": list(item.get("identity_anchors", [])),
                "required_features": list(item.get("required_features", [])),
                "forbidden_features": list(item.get("forbidden_features", [])),
                "provenance": item.get("provenance"),
            }
            for item in character_rows
            if isinstance(item, Mapping)
        ],
        "production_rule": (
            "Do not add an identity mark, organ, accessory, decoration, costume feature, or anatomical feature "
            "unless it is supported by a contract-declared anchor/required feature with provenance."
        ),
    }
    product_dimensions = ["child_appeal", "composition", "color", "lighting", "style_suitability"]
    if character_ids:
        product_dimensions.extend(["cuteness", "natural_identity"])
    if scale_ids:
        product_dimensions.append("scale_readability")
    if state_ids:
        product_dimensions.append("state_readability")
    return {
        "schema_version": VISUAL_SAMPLE_SCHEMA_VERSION,
        "compiler_version": VISUAL_SAMPLE_COMPILER_VERSION,
        "consumer": "storyboard_images",
        "contract_schema_version": context["contract_schema_version"],
        "story_contract_sha256": context["story_contract_sha256"],
        "story_contract_dependency_sha256": context["story_contract_dependency_sha256"],
        "contract_projection_sha256": projection_sha256(context),
        "requirements": requirements,
        "identity_expansion_policy": identity_policy,
        "review_profile": {
            "machine_completeness": ["declared_file", "sha256", "decodable_image", "usable_dimensions"],
            "contract_adherence": ["visual_style", *(["characters"] if character_ids else []), *(["world_scale"] if scale_ids else []), *(["story_state"] if state_ids else [])],
            "product_quality": product_dimensions,
            "p0_categories": sorted(P0_CATEGORIES),
        },
        "supplemental_request": supplemental_request,
    }


def validate_visual_sample_plan(payload: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    required = {
        "schema_version", "compiler_version", "consumer", "contract_schema_version",
        "story_contract_sha256", "story_contract_dependency_sha256", "contract_projection_sha256",
        "requirements", "identity_expansion_policy", "review_profile",
    }
    allowed = required | {"supplemental_request"}
    missing = sorted(required - set(payload))
    if missing:
        issues.append("missing_fields:" + ",".join(missing))
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        issues.append("unexpected_fields:" + ",".join(unexpected))
    if payload.get("schema_version") != VISUAL_SAMPLE_SCHEMA_VERSION:
        issues.append("schema_version")
    if payload.get("compiler_version") != VISUAL_SAMPLE_COMPILER_VERSION:
        issues.append("compiler_version")
    if payload.get("consumer") != "storyboard_images":
        issues.append("consumer")
    for field in ("story_contract_sha256", "story_contract_dependency_sha256", "contract_projection_sha256"):
        value = str(payload.get(field) or "")
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            issues.append(field)
    rows = payload.get("requirements")
    if not isinstance(rows, list) or not rows:
        issues.append("requirements")
        rows = []
    seen: set[str] = set()
    for index, item in enumerate(rows):
        path = f"requirements[{index}]"
        if not isinstance(item, Mapping):
            issues.append(path)
            continue
        kind = str(item.get("kind") or "")
        if kind not in SAMPLE_KINDS or kind in seen:
            issues.append(path + ".kind")
        seen.add(kind)
        if item.get("sample_id") != kind or item.get("fulfillment") not in {"contract_preview", "supplemental_sample"}:
            issues.append(path + ".identity")
        expected_item_fields = {
            "sample_id", "kind", "need_reason", "contract_paths", "content_refs",
            "fulfillment", "source_preview_id", "expected_path", "asset",
        }
        if set(item) != expected_item_fields:
            issues.append(path + ".fields")
        if not isinstance(item.get("contract_paths"), list) or not item.get("contract_paths"):
            issues.append(path + ".contract_paths")
        if not str(item.get("expected_path") or "") or Path(str(item.get("expected_path") or "")).is_absolute():
            issues.append(path + ".expected_path")
        asset = item.get("asset")
        if asset is not None and (
            not isinstance(asset, Mapping)
            or asset.get("path") != item.get("expected_path")
            or len(str(asset.get("sha256") or "")) != 64
        ):
            issues.append(path + ".asset")
    if "style_anchor" not in seen:
        issues.append("requirements.style_anchor")
    policy = payload.get("identity_expansion_policy")
    if (
        not isinstance(policy, Mapping)
        or set(policy) != {"mode", "characters", "production_rule"}
        or policy.get("mode") != "deny_unlisted"
        or not isinstance(policy.get("characters"), list)
        or not str(policy.get("production_rule") or "").strip()
    ):
        issues.append("identity_expansion_policy")
    profile = payload.get("review_profile")
    if (
        not isinstance(profile, Mapping)
        or set(profile) != {"machine_completeness", "contract_adherence", "product_quality", "p0_categories"}
        or not all(isinstance(profile.get(field), list) for field in ("machine_completeness", "contract_adherence", "product_quality"))
        or set(profile.get("p0_categories", [])) != set(P0_CATEGORIES)
    ):
        issues.append("review_profile")
    return issues


def visual_sample_schema_parity_issues() -> list[str]:
    try:
        schema = json.loads(VISUAL_SAMPLE_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"schema:{exc}"]
    issues: list[str] = []
    if schema.get("properties", {}).get("schema_version", {}).get("const") != VISUAL_SAMPLE_SCHEMA_VERSION:
        issues.append("schema_version")
    if schema.get("properties", {}).get("compiler_version", {}).get("const") != VISUAL_SAMPLE_COMPILER_VERSION:
        issues.append("compiler_version")
    requirement = schema.get("$defs", {}).get("requirement", {})
    kinds = set(requirement.get("properties", {}).get("kind", {}).get("enum", []))
    if kinds != set(SAMPLE_KINDS):
        issues.append("sample_kinds")
    required = set(schema.get("required", []))
    validator_required = {
        "schema_version", "compiler_version", "consumer", "contract_schema_version",
        "story_contract_sha256", "story_contract_dependency_sha256", "contract_projection_sha256",
        "requirements", "identity_expansion_policy", "review_profile",
    }
    if required != validator_required:
        issues.append("required_fields")
    p0_schema = schema.get("properties", {}).get("review_profile", {}).get("properties", {}).get("p0_categories", {})
    p0_values = set(p0_schema.get("items", {}).get("enum", []))
    if (
        p0_values != set(P0_CATEGORIES)
        or p0_schema.get("minItems") != len(P0_CATEGORIES)
        or p0_schema.get("maxItems") != len(P0_CATEGORIES)
        or p0_schema.get("uniqueItems") is not True
    ):
        issues.append("p0_categories")
    return issues


def write_visual_sample_plan(project_root: Path | str, context_path: Path | str) -> Path:
    paths = visual_sample_paths(project_root)
    payload = compile_visual_sample_plan(project_root, context_path)
    issues = validate_visual_sample_plan(payload)
    if issues:
        raise ValueError("invalid visual sample plan: " + ", ".join(issues))
    return write_json_atomic(paths["plan"], payload)


def load_current_visual_sample_plan(project_root: Path | str, context_path: Path | str) -> dict[str, Any]:
    root = Path(project_root)
    paths = visual_sample_paths(root)
    if not contract_consumer_context_is_current(root, "storyboard_images", Path(context_path)):
        raise ValueError("storyboard_images contract context is stale")
    try:
        payload = json.loads(paths["plan"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"visual sample plan is unreadable: {exc}") from exc
    issues = validate_visual_sample_plan(payload)
    if issues:
        raise ValueError("visual sample plan is invalid: " + ", ".join(issues))
    expected = compile_visual_sample_plan(root, context_path)
    if canonical_json_bytes(payload) != canonical_json_bytes(expected):
        raise ValueError("visual sample plan is stale or manually modified")
    return payload


def visual_sample_plan_is_current(project_root: Path | str, context_path: Path | str, *, require_ready: bool = False) -> bool:
    try:
        payload = load_current_visual_sample_plan(project_root, context_path)
    except ValueError:
        return False
    return not require_ready or all(isinstance(item.get("asset"), Mapping) for item in payload["requirements"])


def visual_sample_asset_paths(project_root: Path | str, plan: Mapping[str, Any]) -> list[Path]:
    root = Path(project_root)
    return [root / str(item["expected_path"]) for item in plan.get("requirements", []) if isinstance(item, Mapping)]


def visual_sample_machine_issues(project_root: Path | str, plan: Mapping[str, Any]) -> list[str]:
    root = Path(project_root)
    issues: list[str] = []
    for item in plan.get("requirements", []):
        if not isinstance(item, Mapping):
            continue
        sample_id = str(item.get("sample_id") or "sample")
        asset = item.get("asset")
        target = root / str(item.get("expected_path") or "")
        if not isinstance(asset, Mapping) or not target.is_file():
            issues.append(f"{sample_id}:missing")
            continue
        if file_sha256(target) != asset.get("sha256"):
            issues.append(f"{sample_id}:sha256")
            continue
        try:
            with Image.open(target) as image:
                image.verify()
            with Image.open(target) as image:
                width, height = image.size
        except Exception as exc:
            issues.append(f"{sample_id}:decode:{exc}")
            continue
        if width < 256 or height < 256 or max(width / height, height / width) > 4:
            issues.append(f"{sample_id}:dimensions:{width}x{height}")
    return issues


def write_visual_sample_machine_qa(project_root: Path | str, plan: Mapping[str, Any]) -> Path:
    paths = visual_sample_paths(project_root)
    issues = visual_sample_machine_issues(project_root, plan)
    payload = {
        "version": 1,
        "visual_sample_plan_sha256": file_sha256(paths["plan"]),
        "passed": not issues,
        "issues": issues,
        "samples": [
            {"sample_id": item["sample_id"], "path": item["expected_path"], "sha256": item["asset"]["sha256"]}
            for item in plan["requirements"]
            if isinstance(item.get("asset"), Mapping)
        ],
    }
    return write_json_atomic(paths["machine_qa"], payload)


def write_visual_sample_supplemental_request(
    project_root: Path | str,
    plan: Mapping[str, Any],
    review: Mapping[str, Any],
) -> Path:
    """Persist a review-derived request to replace insufficient reused previews.

    The receipt carries no visual rule.  It only records which independently
    reviewed sample was insufficient; the replacement still derives all
    visual requirements from the current contract projection.
    """

    paths = visual_sample_paths(project_root)
    required_ids = {
        str(item.get("sample_id"))
        for item in plan.get("requirements", [])
        if isinstance(item, Mapping)
    }
    requested = {
        str(item)
        for item in review.get("retry_sample_ids", [])
        if isinstance(review.get("retry_sample_ids"), list)
    }
    retry_files = review.get("retry_files", [])
    if isinstance(retry_files, list):
        for item in plan.get("requirements", []):
            if not isinstance(item, Mapping):
                continue
            expected = str(item.get("expected_path") or "")
            if any(str(value) in {expected, Path(expected).name} for value in retry_files):
                requested.add(str(item.get("sample_id")))
    requested &= required_ids
    if not requested:
        requested = required_ids
    payload = {
        "version": 1,
        "story_contract_dependency_sha256": plan["story_contract_dependency_sha256"],
        "source_visual_sample_plan_sha256": file_sha256(paths["plan"]),
        "source_review_bundle_sha256": file_sha256(paths["bundle"]),
        "source_review_sha256": file_sha256(paths["review"]),
        "sample_ids": sorted(requested),
        "reason": "independent_review_found_existing_preview_insufficient",
    }
    return write_json_atomic(paths["supplemental_request"], payload)


def visual_sample_review_payload_issues(payload: Mapping[str, Any], plan: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    p0 = payload.get("p0_errors")
    if not isinstance(p0, list):
        issues.append("p0_errors must be an array")
    elif p0:
        issues.append("P0 hard gate failed: " + ",".join(str(item) for item in p0))
    machine = payload.get("machine_completeness")
    if not isinstance(machine, Mapping) or machine.get("passed") is not True or not machine.get("evidence"):
        issues.append("machine_completeness must pass with evidence")
    adherence = payload.get("contract_adherence")
    expected_adherence = set(plan.get("review_profile", {}).get("contract_adherence", []))
    actual_adherence: set[str] = set()
    if not isinstance(adherence, Mapping) or adherence.get("passed") is not True:
        issues.append("contract_adherence must pass")
    else:
        checks = adherence.get("checks")
        if isinstance(checks, list):
            for item in checks:
                if isinstance(item, Mapping) and item.get("passed") is True and item.get("evidence"):
                    actual_adherence.add(str(item.get("dimension") or ""))
        if not expected_adherence.issubset(actual_adherence):
            issues.append("contract_adherence missing: " + ",".join(sorted(expected_adherence - actual_adherence)))
    quality = payload.get("product_quality")
    expected_quality = set(plan.get("review_profile", {}).get("product_quality", []))
    actual_quality: set[str] = set()
    if not isinstance(quality, Mapping) or quality.get("passed") is not True:
        issues.append("product_quality must pass")
    else:
        dimensions = quality.get("dimensions")
        if isinstance(dimensions, list):
            for item in dimensions:
                if isinstance(item, Mapping) and item.get("passed") is True and item.get("evidence"):
                    actual_quality.add(str(item.get("dimension") or ""))
        if not expected_quality.issubset(actual_quality):
            issues.append("product_quality missing: " + ",".join(sorted(expected_quality - actual_quality)))
    matrix = payload.get("evidence_matrix")
    sample_ids = {str(item.get("sample_id")) for item in plan.get("requirements", []) if isinstance(item, Mapping)}
    covered = {
        str(item.get("sample_id"))
        for item in matrix
        if isinstance(matrix, list) and isinstance(item, Mapping) and item.get("evidence")
    } if isinstance(matrix, list) else set()
    if sample_ids - covered:
        issues.append("evidence_matrix missing samples: " + ",".join(sorted(sample_ids - covered)))
    return issues


def expected_visual_sample_lock(project_root: Path | str) -> dict[str, Any]:
    paths = visual_sample_paths(project_root)
    return {
        "version": VISUAL_SAMPLE_LOCK_VERSION,
        "schema_version": VISUAL_SAMPLE_SCHEMA_VERSION,
        "visual_sample_plan_sha256": file_sha256(paths["plan"]),
        "machine_qa_sha256": file_sha256(paths["machine_qa"]),
        "review_bundle_sha256": file_sha256(paths["bundle"]),
        "review_sha256": file_sha256(paths["review"]),
    }


def write_visual_sample_lock(project_root: Path | str) -> Path:
    paths = visual_sample_paths(project_root)
    return write_json_atomic(paths["lock"], expected_visual_sample_lock(project_root))


def visual_sample_lock_is_current(project_root: Path | str, context_path: Path | str) -> bool:
    paths = visual_sample_paths(project_root)
    try:
        plan = load_current_visual_sample_plan(project_root, context_path)
        current = json.loads(paths["lock"].read_text(encoding="utf-8"))
        review = json.loads(paths["review"].read_text(encoding="utf-8"))
        machine = json.loads(paths["machine_qa"].read_text(encoding="utf-8"))
        expected = expected_visual_sample_lock(project_root)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return (
        current == expected
        and set(current) == set(expected)
        and all(isinstance(item.get("asset"), Mapping) for item in plan["requirements"])
        and not visual_sample_machine_issues(project_root, plan)
        and machine.get("passed") is True
        and machine.get("visual_sample_plan_sha256") == file_sha256(paths["plan"])
        and review_bundle_is_current(paths["bundle"])
        and review_passes(review, artifact=paths["bundle"])
        and not visual_sample_review_payload_issues(review, plan)
    )


def visual_sample_binding(project_root: Path | str) -> dict[str, str]:
    paths = visual_sample_paths(project_root)
    return {
        "visual_sample_schema_version": VISUAL_SAMPLE_SCHEMA_VERSION,
        "visual_sample_plan_sha256": file_sha256(paths["plan"]),
        "visual_sample_review_bundle_sha256": file_sha256(paths["bundle"]),
        "visual_sample_lock_sha256": file_sha256(paths["lock"]),
    }


__all__ = [
    "P0_CATEGORIES", "SAMPLE_KINDS", "VISUAL_SAMPLE_COMPILER_VERSION",
    "VISUAL_SAMPLE_SCHEMA_PATH", "VISUAL_SAMPLE_SCHEMA_VERSION",
    "compile_visual_sample_plan", "load_current_visual_sample_plan",
    "validate_visual_sample_plan", "visual_sample_asset_paths", "visual_sample_binding",
    "visual_sample_lock_is_current", "visual_sample_machine_issues",
    "visual_sample_paths", "visual_sample_plan_is_current",
    "visual_sample_review_payload_issues", "visual_sample_schema_parity_issues",
    "write_visual_sample_lock", "write_visual_sample_machine_qa", "write_visual_sample_plan",
    "write_visual_sample_supplemental_request",
]
