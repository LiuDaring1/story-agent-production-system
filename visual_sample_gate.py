"""Deterministic visual-sample planning and approval gates.

The Story Production Contract remains the only rule source.  This module
compiles the reviewed visual projection into the smallest set of samples that
the current story actually needs, verifies the resulting files, and binds an
independent review with a crash-safe lock before batch image generation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from story_agent_runtime import file_sha256, review_bundle_is_current, review_passes
from story_contract_consumers import load_consumer_context, projection_sha256, write_json_atomic
from story_contract_runtime import contract_consumer_context_is_current
from story_contracts import canonical_json_bytes
from story_module_ports import VisualDesignPort, VisualDesignRequest, VisualDesignResult
from story_module_registry import build_visual_design_registry


VISUAL_SAMPLE_SCHEMA_VERSION = "1.0"
VISUAL_SAMPLE_COMPILER_VERSION = "m2-2a1.1.2"
VISUAL_SAMPLE_LOCK_VERSION = 1
VISUAL_SAMPLE_SCHEMA_PATH = (
    Path(__file__).resolve().parent
    / "schemas"
    / "visual_sample_gate"
    / "v1"
    / "visual_sample_plan.schema.json"
)

SAMPLE_KINDS = ("style_anchor", "character_sheet", "scale_anchor", "state_anchor")
IDENTITY_POLICY_FIELDS = frozenset(
    {
        "mode", "scope", "characters", "blocked_inferences", "allowed_contextual_inferences",
        "inferred_detail_persistence", "contract_precedence", "production_rule",
    }
)
REVIEW_PROFILE_FIELDS = frozenset(
    {"machine_completeness", "contract_adherence", "product_quality", "style_contract", "p0_categories"}
)
STYLE_CONTRACT_FIELDS = frozenset(
    {"description", "required_traits", "forbidden_traits", "provenance"}
)
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

NEUTRAL_PRODUCT_QUALITY_DIMENSIONS = (
    "audience_fit",
    "composition",
    "color",
    "lighting",
    "style_suitability",
)
CHARACTER_PRODUCT_QUALITY_DIMENSIONS = (
    "character_design_fit",
    "identity_coherence",
    "anatomical_coherence",
)
ANATOMICAL_COHERENCE_REVIEW_RULE = (
    "anatomical_coherence 的基准是当前 Story Contract 中的角色定义、物种/身份、visual_style "
    "和本镜头设计：阻断违反合同/角色设定或非意图性的多肢、缺肢、器官错位、结构崩坏；"
    "符合合同的风格化、拟人化、奇幻结构或故意夸张比例不得仅因不写实而失败。"
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


def compile_product_quality_profile(projection: Mapping[str, Any]) -> dict[str, Any]:
    """Compile style-neutral quality dimensions plus verbatim style requirements.

    The style contract, not the presence of a character, determines whether a
    specific aesthetic such as cute, solemn, historical, or realistic is
    required.  Required and forbidden traits are deliberately preserved
    verbatim so this compiler never creates a competing aesthetic rule source.
    """

    characters = projection.get("characters", {})
    character_rows = characters.get("characters", []) if isinstance(characters, Mapping) else []
    dimensions = list(NEUTRAL_PRODUCT_QUALITY_DIMENSIONS)
    if characters.get("mode") == "present" and character_rows:
        dimensions.extend(CHARACTER_PRODUCT_QUALITY_DIMENSIONS)
    scale = projection.get("world_scale", {})
    if isinstance(scale, Mapping) and scale.get("relationships"):
        dimensions.append("scale_readability")
    state = projection.get("story_state", {})
    if isinstance(state, Mapping) and state.get("machines"):
        dimensions.append("state_readability")
    visual_style = projection.get("visual_style", {})
    style_profile = visual_style.get("style_profile", {}) if isinstance(visual_style, Mapping) else {}
    return {
        "dimensions": dimensions,
        "style_contract": {
            "description": str(style_profile.get("description") or ""),
            "required_traits": list(style_profile.get("required_traits", [])),
            "forbidden_traits": list(style_profile.get("forbidden_traits", [])),
            "provenance": style_profile.get("provenance"),
        },
    }


def product_quality_review_issues(
    quality: Any,
    profile: Mapping[str, Any],
) -> list[str]:
    """Validate that neutral dimensions and every contract style rule were reviewed."""

    issues: list[str] = []
    if not isinstance(quality, Mapping) or quality.get("passed") is not True:
        return ["product_quality must pass"]
    actual_dimensions = {
        str(item.get("dimension") or "")
        for item in quality.get("dimensions", [])
        if isinstance(item, Mapping) and item.get("passed") is True and item.get("evidence")
    }
    expected_dimensions = set(profile.get("product_quality", []))
    if expected_dimensions - actual_dimensions:
        issues.append(
            "product_quality missing: " + ",".join(sorted(expected_dimensions - actual_dimensions))
        )

    expected_style = profile.get("style_contract")
    actual_style = quality.get("style_contract")
    if not isinstance(expected_style, Mapping) or not isinstance(actual_style, Mapping):
        issues.append("product_quality style_contract missing")
        return issues
    if (
        actual_style.get("description") != expected_style.get("description")
        or actual_style.get("description_fit") is not True
        or not actual_style.get("description_evidence")
    ):
        issues.append("product_quality style description not reviewed")
    for field, result_field in (("required_traits", "passed"), ("forbidden_traits", "absent")):
        expected_traits = set(expected_style.get(field, []))
        actual_traits = {
            str(item.get("trait") or "")
            for item in actual_style.get(field, [])
            if isinstance(item, Mapping) and item.get(result_field) is True and item.get("evidence")
        }
        if expected_traits - actual_traits:
            issues.append(
                f"product_quality style {field} missing: "
                + ",".join(sorted(expected_traits - actual_traits))
            )
    return issues


def _approved_visual_projection(
    root: Path,
    context: Mapping[str, Any],
    visual_design_port: VisualDesignPort,
) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
    expected_projection = context["contract_projection"]
    expected_projection_sha = projection_sha256(context)
    request = VisualDesignRequest(
        project_root=root,
        consumer="storyboard_images",
        operation="approved_projection",
        story_contract_sha256=str(context["story_contract_sha256"]),
        projection_sha256=expected_projection_sha,
        story_semantics_sha256="",
        current_artifact_references=(),
        attempt_id="visual-sample-plan",
    )
    result: VisualDesignResult = visual_design_port.resolve(request)
    if not result.success:
        message = result.failure.message if result.failure is not None else "unknown failure"
        raise ValueError(f"visual design port rejected current projection: {message}")
    actual_projection_sha = hashlib.sha256(canonical_json_bytes(result.approved_projection)).hexdigest()
    if (
        result.operation != request.operation
        or result.story_contract_sha256 != request.story_contract_sha256
        or result.projection_sha256 != request.projection_sha256
        or result.output_sha256 != request.projection_sha256
        or result.story_semantics_sha256 != request.story_semantics_sha256
        or result.attempt_id != request.attempt_id
        or actual_projection_sha != request.projection_sha256
        or canonical_json_bytes(result.approved_projection) != canonical_json_bytes(expected_projection)
    ):
        raise ValueError("visual design port returned a stale or mismatched projection binding")
    if not isinstance(result.artifact_references, tuple) or any(
        not isinstance(item, Mapping) for item in result.artifact_references
    ):
        raise ValueError("visual design port returned invalid artifact references")
    return result.approved_projection, list(result.artifact_references)


def compile_visual_sample_plan(
    project_root: Path | str,
    context_path: Path | str,
    visual_design_port: VisualDesignPort | None = None,
) -> dict[str, Any]:
    """Compile only story-applicable samples from the current reviewed contract."""

    root = Path(project_root)
    context = load_consumer_context(context_path, "storyboard_images")
    port = visual_design_port or build_visual_design_registry().visual_design()
    projection, previews = _approved_visual_projection(root, context, port)
    characters = projection.get("characters", {})
    character_rows = characters.get("characters", []) if isinstance(characters, Mapping) else []
    character_ids = [str(item["character_id"]) for item in character_rows if isinstance(item, Mapping)]
    scale = projection.get("world_scale", {})
    scale_rows = scale.get("relationships", []) if isinstance(scale, Mapping) else []
    scale_ids = [str(item["relationship_id"]) for item in scale_rows if isinstance(item, Mapping)]
    state = projection.get("story_state", {})
    state_rows = state.get("machines", []) if isinstance(state, Mapping) else []
    state_ids = [str(item["machine_id"]) for item in state_rows if isinstance(item, Mapping)]
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
                reason=(
                    "Recurring contract-declared characters need identity and contract/style-consistent "
                    "anatomical-coherence evidence."
                ),
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
        "scope": "identity_defining_features_only",
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
        "blocked_inferences": [
            "identity_defining_special_mark",
            "fixed_accessory",
            "emblem",
            "non_intentional_or_contract_violating_anatomy",
            "new_cross_shot_identity_anchor",
        ],
        "allowed_contextual_inferences": [
            "normal_human_or_animal_anatomy",
            "contract_consistent_stylized_or_fantastical_anatomy",
            "era_and_scene_appropriate_ordinary_clothing",
            "non_identity_natural_detail",
        ],
        "inferred_detail_persistence": "scene_local_unless_contract_promotes",
        "contract_precedence": "required_and_forbidden_features_are_authoritative",
        "production_rule": (
            "Do not invent identity-defining special marks, fixed accessories, emblems, non-intentional or "
            "contract-violating anatomy, or other cross-shot identity anchors. Contract-consistent stylized, "
            "anthropomorphic, fantastical, or intentionally exaggerated anatomy is allowed and must not fail "
            "merely for being unrealistic. Normal anatomy, era- and scene-appropriate ordinary clothing, and "
            "non-identity natural details are also allowed when they do not violate the contract. Inferred "
            "ordinary details remain scene-local and must not be promoted into permanent identity anchors. "
            "Contract-declared required and forbidden features are authoritative."
        ),
    }
    quality_profile = compile_product_quality_profile(projection)
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
            "product_quality": quality_profile["dimensions"],
            "style_contract": quality_profile["style_contract"],
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
        or set(policy) != set(IDENTITY_POLICY_FIELDS)
        or policy.get("mode") != "deny_unlisted"
        or policy.get("scope") != "identity_defining_features_only"
        or not isinstance(policy.get("characters"), list)
        or not isinstance(policy.get("blocked_inferences"), list)
        or not isinstance(policy.get("allowed_contextual_inferences"), list)
        or policy.get("inferred_detail_persistence") != "scene_local_unless_contract_promotes"
        or policy.get("contract_precedence") != "required_and_forbidden_features_are_authoritative"
        or not str(policy.get("production_rule") or "").strip()
    ):
        issues.append("identity_expansion_policy")
    profile = payload.get("review_profile")
    if (
        not isinstance(profile, Mapping)
        or set(profile) != set(REVIEW_PROFILE_FIELDS)
        or not all(isinstance(profile.get(field), list) for field in ("machine_completeness", "contract_adherence", "product_quality"))
        or not isinstance(profile.get("style_contract"), Mapping)
        or set(profile.get("style_contract", {})) != set(STYLE_CONTRACT_FIELDS)
        or not str(profile.get("style_contract", {}).get("description") or "").strip()
        or not isinstance(profile.get("style_contract", {}).get("required_traits"), list)
        or not isinstance(profile.get("style_contract", {}).get("forbidden_traits"), list)
        or not all(
            isinstance(value, str) and value.strip()
            for field in ("required_traits", "forbidden_traits")
            for value in profile.get("style_contract", {}).get(field, [])
        )
        or not isinstance(profile.get("style_contract", {}).get("provenance"), Mapping)
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
    properties = schema.get("properties", {})
    profile_schema = properties.get("review_profile", {})
    profile_required = set(profile_schema.get("required", []))
    if profile_required != set(REVIEW_PROFILE_FIELDS):
        issues.append("review_profile_fields")
    style_required = set(
        profile_schema.get("properties", {}).get("style_contract", {}).get("required", [])
    )
    if style_required != set(STYLE_CONTRACT_FIELDS):
        issues.append("style_contract_fields")
    identity_required = set(properties.get("identity_expansion_policy", {}).get("required", []))
    if identity_required != set(IDENTITY_POLICY_FIELDS):
        issues.append("identity_policy_fields")
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


def write_visual_sample_plan(
    project_root: Path | str,
    context_path: Path | str,
    visual_design_port: VisualDesignPort | None = None,
) -> Path:
    paths = visual_sample_paths(project_root)
    payload = compile_visual_sample_plan(project_root, context_path, visual_design_port)
    issues = validate_visual_sample_plan(payload)
    if issues:
        raise ValueError("invalid visual sample plan: " + ", ".join(issues))
    return write_json_atomic(paths["plan"], payload)


def load_current_visual_sample_plan(
    project_root: Path | str,
    context_path: Path | str,
    visual_design_port: VisualDesignPort | None = None,
) -> dict[str, Any]:
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
    expected = compile_visual_sample_plan(root, context_path, visual_design_port)
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
    issues.extend(product_quality_review_issues(payload.get("product_quality"), plan.get("review_profile", {})))
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
    "ANATOMICAL_COHERENCE_REVIEW_RULE",
    "CHARACTER_PRODUCT_QUALITY_DIMENSIONS", "NEUTRAL_PRODUCT_QUALITY_DIMENSIONS",
    "P0_CATEGORIES", "SAMPLE_KINDS", "VISUAL_SAMPLE_COMPILER_VERSION",
    "VISUAL_SAMPLE_SCHEMA_PATH", "VISUAL_SAMPLE_SCHEMA_VERSION",
    "compile_product_quality_profile", "compile_visual_sample_plan", "load_current_visual_sample_plan",
    "product_quality_review_issues",
    "validate_visual_sample_plan", "visual_sample_asset_paths", "visual_sample_binding",
    "visual_sample_lock_is_current", "visual_sample_machine_issues",
    "visual_sample_paths", "visual_sample_plan_is_current",
    "visual_sample_review_payload_issues", "visual_sample_schema_parity_issues",
    "write_visual_sample_lock", "write_visual_sample_machine_qa", "write_visual_sample_plan",
    "write_visual_sample_supplemental_request",
]
