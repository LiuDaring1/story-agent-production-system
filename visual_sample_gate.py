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
VISUAL_SAMPLE_COMPILER_VERSION = "m2-2a1.1.4"
VISUAL_SAMPLE_LOCK_VERSION = 1
VISUAL_SAMPLE_MAX_ATTEMPTS = 3
MAX_STATE_SAMPLE_ASSETS = 3
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
    sample_id: str,
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
            "sample_id": sample_id,
            "kind": kind,
            "need_reason": reason,
            "contract_paths": contract_paths_,
            "content_refs": refs,
            "fulfillment": "contract_preview",
            "source_preview_id": str(reused["preview_id"]),
            "expected_path": str(reused["path"]),
            "asset": asset,
        }
    target = root / "99_项目状态" / "visual_samples" / "assets" / f"{sample_id}.png"
    return {
        "sample_id": sample_id,
        "kind": kind,
        "need_reason": reason,
        "contract_paths": contract_paths_,
        "content_refs": refs,
        "fulfillment": "supplemental_sample",
        "source_preview_id": None,
        "expected_path": _project_relative(target, root),
        "asset": _ready_asset(target, root),
    }


def _safe_sample_id(kind: str, *parts: str) -> str:
    raw = "__".join((kind, *(str(part) for part in parts if str(part))))
    safe = "".join(character if character.isascii() and (character.isalnum() or character in "-_") else "-" for character in raw)
    while "--" in safe:
        safe = safe.replace("--", "-")
    safe = safe.strip("-_") or kind
    if len(safe) > 112:
        suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        safe = f"{safe[:99].rstrip('-_')}__{suffix}"
    return safe


def _mother_character_ids(character_rows: list[Any], *, limit: int = 3) -> list[str]:
    """Select a small cast for the one shared identity/style mother reference."""

    ranked: list[tuple[int, int, str]] = []
    for index, item in enumerate(character_rows):
        if not isinstance(item, Mapping):
            continue
        character_id = str(item.get("character_id") or "")
        if not character_id:
            continue
        role = str(item.get("role") or "").lower()
        if any(token in role for token in ("protagonist", "hero", "lead", "main", "主角")):
            priority = 0
        elif any(token in role for token in ("guide", "helper", "beneficiary", "伙伴", "帮助")):
            priority = 1
        elif any(token in role for token in ("incidental", "group", "群", "路人")):
            priority = 3
        else:
            priority = 2
        ranked.append((priority, index, character_id))
    return [item[2] for item in sorted(ranked)[: max(0, limit)]]


def _representative_state_rows(state_rows: list[Any]) -> list[tuple[int, int, Mapping[str, Any], Mapping[str, Any]]]:
    """Choose a bounded set of standalone state proofs, never one giant contact sheet.

    The complete state sequence remains authoritative JSON consumed by formal
    storyboard generation.  Samples are only representative visual anchors.
    Long discrete sequences get initial/middle/final assets for the most complex
    machine; simpler stories get one changed-state asset per machine, capped by
    ``MAX_STATE_SAMPLE_ASSETS``.
    """

    machines: list[tuple[int, Mapping[str, Any], list[Mapping[str, Any]]]] = []
    for machine_index, machine in enumerate(state_rows):
        if not isinstance(machine, Mapping):
            continue
        states = [item for item in machine.get("states", []) if isinstance(item, Mapping)]
        if states:
            machines.append((machine_index, machine, states))
    if not machines:
        return []
    machines.sort(
        key=lambda item: (
            -len(item[2]),
            -len(item[1].get("transitions", [])) if isinstance(item[1].get("transitions"), list) else 0,
            str(item[1].get("machine_id") or ""),
        )
    )
    primary_index, primary, primary_states = machines[0]
    if len(primary_states) >= 4:
        indices = sorted({0, (len(primary_states) - 1) // 2, len(primary_states) - 1})
        return [
            (primary_index, state_index, primary, primary_states[state_index])
            for state_index in indices[:MAX_STATE_SAMPLE_ASSETS]
        ]
    selected: list[tuple[int, int, Mapping[str, Any], Mapping[str, Any]]] = []
    for machine_index, machine, states in machines[:MAX_STATE_SAMPLE_ASSETS]:
        state_index = len(states) - 1 if len(states) > 1 else 0
        selected.append((machine_index, state_index, machine, states[state_index]))
    return selected


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
    mother_character_ids = _mother_character_ids(character_rows)
    scale = projection.get("world_scale", {})
    scale_rows = scale.get("relationships", []) if isinstance(scale, Mapping) else []
    # Environment/action references guide scene composition but do not need a
    # separate cross-character size proof.  Requiring one caused the system to
    # multiply reference images that could not establish identity continuity.
    comparable_scale_rows = [
        item
        for item in scale_rows
        if isinstance(item, Mapping) and item.get("qualitative_relation") != "environment_reference"
    ]
    scale_ids = [str(item["relationship_id"]) for item in comparable_scale_rows]
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
            and candidate.get("version") == 2
            and candidate.get("story_contract_dependency_sha256")
            == context["story_contract_dependency_sha256"]
            and isinstance(candidate.get("sample_ids"), list)
        ):
            forced = {str(item) for item in candidate["sample_ids"] if str(item)}
            supplemental_request = candidate
    except (OSError, json.JSONDecodeError):
        pass

    # New contracts are text-only and therefore create one combined style/cast
    # mother reference. Historical locked contracts may already have separate
    # style and character previews; preserve and reuse those files instead of
    # forcing a needless migration-time ImageGen call.
    reuse_separate_character_preview = bool(mother_character_ids) and any(
        _preview_covers(item, "style_anchor", [], root) for item in previews
    ) and any(
        _preview_covers(item, "character_sheet", mother_character_ids, root)
        for item in previews
    )
    style_refs = [] if reuse_separate_character_preview else mother_character_ids
    requirements = [
        _sample_requirement(
            root=root,
            sample_id="style_anchor",
            kind="style_anchor",
            reason=(
                "One shared mother reference proves the selected style, rendering quality, and recurring "
                "character identity before batch generation. It is not a separate image per character."
                if mother_character_ids
                else "Every visual story needs one reviewed style and rendering-quality anchor before batch generation."
            ),
            contract_paths_=["contracts.visual_style", *( ["contracts.characters"] if mother_character_ids else [] )],
            refs=style_refs,
            previews=previews,
            force_supplemental="style_anchor" in forced,
        )
    ]
    if reuse_separate_character_preview:
        requirements.append(
            _sample_requirement(
                root=root,
                sample_id="character_sheet",
                kind="character_sheet",
                reason=(
                    "Historical reviewed character identity preview is retained for compatibility; "
                    "new text-only contracts use the shared style mother reference instead."
                ),
                contract_paths_=["contracts.characters"],
                refs=mother_character_ids,
                previews=previews,
                force_supplemental="character_sheet" in forced,
            )
        )
    if scale_ids:
        requirements.append(
            _sample_requirement(
                root=root,
                sample_id="scale_anchor",
                kind="scale_anchor",
                reason="Declared qualitative or evidence-backed scale relationships need a visual comparison.",
                contract_paths_=["contracts.world_scale"],
                refs=scale_ids,
                previews=previews,
                force_supplemental="scale_anchor" in forced,
            )
        )
    for machine_index, state_index, machine, state_row in _representative_state_rows(state_rows):
        machine_id = str(machine.get("machine_id") or f"machine-{machine_index}")
        state_id = str(state_row.get("state_id") or f"state-{state_index}")
        sample_id = _safe_sample_id("state_anchor", machine_id, state_id)
        requirements.append(
            _sample_requirement(
                root=root,
                sample_id=sample_id,
                kind="state_anchor",
                reason=(
                    f"Standalone representative proof for state {state_id!r} of machine {machine_id!r}. "
                    "Generate only this one state in this image; the complete sequence remains structured JSON "
                    "for formal scene generation, never an all-states contact sheet."
                ),
                contract_paths_=[
                    f"contracts.story_state.machines[{machine_index}]",
                    f"contracts.story_state.machines[{machine_index}].states[{state_index}]",
                ],
                refs=[machine_id, state_id],
                previews=previews,
                force_supplemental=sample_id in forced,
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
    seen_ids: set[str] = set()
    seen_kinds: set[str] = set()
    for index, item in enumerate(rows):
        path = f"requirements[{index}]"
        if not isinstance(item, Mapping):
            issues.append(path)
            continue
        kind = str(item.get("kind") or "")
        if kind not in SAMPLE_KINDS:
            issues.append(path + ".kind")
        seen_kinds.add(kind)
        sample_id = str(item.get("sample_id") or "")
        if not sample_id or sample_id in seen_ids:
            issues.append(path + ".sample_id")
        seen_ids.add(sample_id)
        if item.get("fulfillment") not in {"contract_preview", "supplemental_sample"}:
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
    if "style_anchor" not in seen_kinds:
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


def visual_sample_generation_jobs(
    plan: Mapping[str, Any],
    projection: Mapping[str, Any],
    requirements: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project the full contract down to the exact constraint set for each sample."""

    supplemental = plan.get("supplemental_request")
    strategies = (
        supplemental.get("generation_strategy_by_sample", {})
        if isinstance(supplemental, Mapping)
        and isinstance(supplemental.get("generation_strategy_by_sample"), Mapping)
        else {}
    )
    retry_instructions = (
        supplemental.get("retry_instruction_by_sample", {})
        if isinstance(supplemental, Mapping)
        and isinstance(supplemental.get("retry_instruction_by_sample"), Mapping)
        else {}
    )
    rejected_references = (
        supplemental.get("rejected_reference_asset_by_sample", {})
        if isinstance(supplemental, Mapping)
        and isinstance(supplemental.get("rejected_reference_asset_by_sample"), Mapping)
        else {}
    )
    visual_style = projection.get("visual_style", {})
    characters = projection.get("characters", {})
    world_scale = projection.get("world_scale", {})
    story_state = projection.get("story_state", {})
    machines = story_state.get("machines", []) if isinstance(story_state, Mapping) else []
    character_rows = characters.get("characters", []) if isinstance(characters, Mapping) else []

    def project_characters(wanted_ids: set[str]) -> dict[str, Any]:
        if not isinstance(characters, Mapping):
            return {"mode": "none", "characters": []}
        projected = {
            key: value
            for key, value in characters.items()
            if key != "characters"
        }
        projected["characters"] = [
            row
            for row in character_rows
            if isinstance(row, Mapping) and str(row.get("character_id") or "") in wanted_ids
        ]
        return projected

    mother_ids = {
        str(value)
        for requirement in plan.get("requirements", [])
        if isinstance(requirement, Mapping) and requirement.get("kind") == "style_anchor"
        for value in requirement.get("content_refs", [])
    }
    requirement_by_id = {
        str(item.get("sample_id") or ""): item
        for item in plan.get("requirements", [])
        if isinstance(item, Mapping) and str(item.get("sample_id") or "")
    }

    def ready_reference(sample_id: str) -> dict[str, str] | None:
        item = requirement_by_id.get(sample_id)
        if not isinstance(item, Mapping) or not isinstance(item.get("asset"), Mapping):
            return None
        return {
            "sample_id": sample_id,
            "path": str(item["asset"].get("path") or item.get("expected_path") or ""),
            "sha256": str(item["asset"].get("sha256") or ""),
            "role": "approved_mother_sample",
        }

    jobs: list[dict[str, Any]] = []
    for item in requirements:
        sample_id = str(item.get("sample_id") or "")
        kind = str(item.get("kind") or "")
        relevant: dict[str, Any] = {"visual_style": visual_style}
        relevant_character_ids = set(mother_ids if kind in {"style_anchor", "character_sheet"} else ())
        if kind == "scale_anchor":
            wanted = {str(value) for value in item.get("content_refs", [])}
            relationships = world_scale.get("relationships", []) if isinstance(world_scale, Mapping) else []
            selected_relationships = [
                row
                for row in relationships
                if isinstance(row, Mapping) and str(row.get("relationship_id") or "") in wanted
            ]
            relevant["world_scale"] = {
                "rules": world_scale.get("rules", []) if isinstance(world_scale, Mapping) else [],
                "relationships": selected_relationships,
            }
            for row in selected_relationships:
                for field in ("subject", "reference"):
                    value = str(row.get(field) or "")
                    if value.startswith("character:"):
                        relevant_character_ids.add(value.split(":", 1)[1])
        if kind == "state_anchor":
            refs = [str(value) for value in item.get("content_refs", [])]
            machine_id = refs[0] if refs else ""
            state_id = refs[1] if len(refs) > 1 else ""
            machine = next(
                (
                    row
                    for row in machines
                    if isinstance(row, Mapping) and str(row.get("machine_id") or "") == machine_id
                ),
                {},
            )
            states = machine.get("states", []) if isinstance(machine, Mapping) else []
            selected_state = next(
                (
                    row
                    for row in states
                    if isinstance(row, Mapping) and str(row.get("state_id") or "") == state_id
                ),
                {},
            )
            transitions = machine.get("transitions", []) if isinstance(machine, Mapping) else []
            relevant["representative_story_state"] = {
                "machine_id": machine_id,
                "entity_ref": machine.get("entity_ref") if isinstance(machine, Mapping) else None,
                "initial_state": machine.get("initial_state") if isinstance(machine, Mapping) else None,
                "selected_state": selected_state,
                "adjacent_transitions": [
                    row
                    for row in transitions
                    if isinstance(row, Mapping)
                    and state_id in {str(row.get("from") or ""), str(row.get("to") or "")}
                ],
                "sampling_rule": (
                    "Render exactly this one selected state as a standalone asset. The full sequence is "
                    "structured production data and must not be squeezed into this image."
                ),
            }
            entity_text = str(machine.get("entity_ref") or "") if isinstance(machine, Mapping) else ""
            for row in character_rows:
                if not isinstance(row, Mapping):
                    continue
                character_id = str(row.get("character_id") or "")
                display_name = str(row.get("display_name") or "")
                if character_id and (
                    character_id in entity_text or display_name and display_name in entity_text
                ):
                    relevant_character_ids.add(character_id)
        relevant["characters"] = project_characters(relevant_character_ids)
        reference_assets: list[dict[str, str]] = []
        previous = rejected_references.get(sample_id)
        if isinstance(previous, Mapping) and previous.get("path") and previous.get("sha256"):
            reference_assets.append(
                {
                    "sample_id": sample_id,
                    "path": str(previous["path"]),
                    "sha256": str(previous["sha256"]),
                    "role": "rejected_sample_for_targeted_edit",
                }
            )
        dependency_sample_ids: list[str] = []
        if kind == "scale_anchor" and sample_id != "style_anchor":
            mother = ready_reference("style_anchor")
            if mother is not None:
                reference_assets.append(mother)
            elif "style_anchor" in requirement_by_id:
                dependency_sample_ids.append("style_anchor")
        elif kind == "state_anchor":
            refs = [str(value) for value in item.get("content_refs", [])]
            machine_id = refs[0] if refs else ""
            candidates = [
                candidate
                for candidate in plan.get("requirements", [])
                if isinstance(candidate, Mapping)
                and candidate.get("kind") == "state_anchor"
                and str(candidate.get("sample_id") or "") != sample_id
                and [str(value) for value in candidate.get("content_refs", [])][:1] == [machine_id]
            ]
            mother_row = next(
                (candidate for candidate in candidates if isinstance(candidate.get("asset"), Mapping)),
                candidates[0] if candidates else None,
            )
            if isinstance(mother_row, Mapping):
                mother_id = str(mother_row.get("sample_id") or "")
                mother = ready_reference(mother_id)
                if mother is not None:
                    reference_assets.append(mother)
                elif mother_id:
                    dependency_sample_ids.append(mother_id)
        jobs.append(
            {
                "sample_id": sample_id,
                "kind": kind,
                "expected_path": item.get("expected_path"),
                "need_reason": item.get("need_reason"),
                "strategy": str(strategies.get(sample_id) or "baseline_single_asset"),
                "retry_instruction": str(retry_instructions.get(sample_id) or ""),
                "reference_assets": reference_assets,
                "dependency_sample_ids": dependency_sample_ids,
                "relevant_contract": relevant,
            }
        )
    return jobs


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
        for field in ("p0_errors", "issues", "retry_instructions"):
            rows = review.get(field, [])
            if not isinstance(rows, list):
                continue
            requested.update(
                str(item.get("sample_id") or "")
                for item in rows
                if isinstance(item, Mapping) and str(item.get("sample_id") or "") in required_ids
            )
    if not requested:
        raise ValueError(
            "independent visual review failed but did not identify retry_sample_ids; "
            "refusing an unbounded all-sample retry"
        )
    previous: dict[str, Any] = {}
    try:
        candidate = json.loads(paths["supplemental_request"].read_text(encoding="utf-8"))
        if (
            isinstance(candidate, dict)
            and candidate.get("version") == 2
            and candidate.get("story_contract_dependency_sha256")
            == plan.get("story_contract_dependency_sha256")
        ):
            previous = candidate
    except (OSError, json.JSONDecodeError):
        pass
    failure_source = {
        "p0_errors": review.get("p0_errors", []),
        "critical_errors": review.get("critical_errors", []),
        "issues": review.get("issues", []),
        "retry_sample_ids": sorted(requested),
        "retry_instructions": review.get("retry_instructions", []),
    }
    fingerprint = hashlib.sha256(canonical_json_bytes(failure_source)).hexdigest()
    history = [
        dict(item)
        for item in previous.get("failure_history", [])
        if isinstance(item, Mapping)
    ]
    previous_fingerprints = {str(item.get("fingerprint") or "") for item in history}
    failed_attempt_count = int(previous.get("failed_attempt_count") or 0) + 1
    requirement_by_id = {
        str(item.get("sample_id")): item
        for item in plan.get("requirements", [])
        if isinstance(item, Mapping)
    }
    if failed_attempt_count == 1:
        strategy_by_sample = {sample_id: "targeted_regeneration" for sample_id in sorted(requested)}
    else:
        strategy_by_sample = {
            sample_id: (
                "single_state_single_asset"
                if str(requirement_by_id.get(sample_id, {}).get("kind") or "") == "state_anchor"
                else "simplify_to_single_subject_contract_evidence"
            )
            for sample_id in sorted(requested)
        }
    repeated_failure = fingerprint in previous_fingerprints
    retry_instruction_by_sample: dict[str, str] = {}
    instruction_rows = review.get("retry_instructions", [])
    if isinstance(instruction_rows, list):
        for item in instruction_rows:
            if not isinstance(item, Mapping):
                continue
            sample_id = str(item.get("sample_id") or "")
            instruction = str(item.get("instruction") or item.get("evidence") or "").strip()
            if sample_id in requested and instruction:
                retry_instruction_by_sample[sample_id] = instruction
        if not retry_instruction_by_sample and len(instruction_rows) == len(requested):
            for sample_id, instruction in zip(sorted(requested), instruction_rows):
                if isinstance(instruction, str) and instruction.strip():
                    retry_instruction_by_sample[sample_id] = instruction.strip()
    history.append(
        {
            "failed_attempt": failed_attempt_count,
            "fingerprint": fingerprint,
            "sample_ids": sorted(requested),
            "strategy_changed": failed_attempt_count > 1 or repeated_failure,
        }
    )
    payload = {
        "version": 2,
        "story_contract_dependency_sha256": plan["story_contract_dependency_sha256"],
        "source_visual_sample_plan_sha256": file_sha256(paths["plan"]),
        "source_review_bundle_sha256": file_sha256(paths["bundle"]),
        "source_review_sha256": file_sha256(paths["review"]),
        "sample_ids": sorted(requested),
        "failed_attempt_count": failed_attempt_count,
        "max_total_attempts": VISUAL_SAMPLE_MAX_ATTEMPTS,
        "next_generation_attempt": failed_attempt_count + 1,
        "failure_fingerprint": fingerprint,
        "repeated_failure": repeated_failure,
        "generation_strategy_by_sample": strategy_by_sample,
        "retry_instruction_by_sample": retry_instruction_by_sample,
        "rejected_reference_asset_by_sample": {},
        "failure_history": history[-VISUAL_SAMPLE_MAX_ATTEMPTS:],
        "reason": (
            "independent_review_failed_change_strategy"
            if failed_attempt_count > 1 or repeated_failure
            else "independent_review_failed_targeted_retry"
        ),
    }
    return write_json_atomic(paths["supplemental_request"], payload)


def enrich_visual_sample_supplemental_request(project_root: Path | str) -> Path | None:
    """Bind retry instructions and the latest quarantined source image.

    Review failure is recorded before rejected files are moved so the retry
    receipt can survive a crash between those operations.  This idempotent
    enrichment step is therefore called both immediately after quarantine and
    again before the next generation attempt.
    """

    root = Path(project_root)
    paths = visual_sample_paths(root)
    try:
        request = json.loads(paths["supplemental_request"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(request, dict) or request.get("version") != 2:
        return None
    requested = {
        str(value)
        for value in request.get("sample_ids", [])
        if isinstance(request.get("sample_ids"), list) and str(value)
    }
    if not requested:
        return None
    try:
        review = json.loads(paths["review"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        review = {}
    instructions = dict(request.get("retry_instruction_by_sample", {})) \
        if isinstance(request.get("retry_instruction_by_sample"), Mapping) else {}
    rows = review.get("retry_instructions", []) if isinstance(review, Mapping) else []
    if isinstance(rows, list):
        for item in rows:
            if not isinstance(item, Mapping):
                continue
            sample_id = str(item.get("sample_id") or "")
            instruction = str(item.get("instruction") or item.get("evidence") or "").strip()
            if sample_id in requested and instruction:
                instructions[sample_id] = instruction
    references = dict(request.get("rejected_reference_asset_by_sample", {})) \
        if isinstance(request.get("rejected_reference_asset_by_sample"), Mapping) else {}
    rejected_root = paths["directory"] / "rejected"
    for sample_id in sorted(requested):
        existing = references.get(sample_id)
        if isinstance(existing, Mapping):
            candidate = root / str(existing.get("path") or "")
            if candidate.is_file() and file_sha256(candidate) == existing.get("sha256"):
                continue
        candidates = sorted(
            rejected_root.glob(f"*/{sample_id}.png"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ) if rejected_root.is_dir() else []
        if candidates:
            candidate = candidates[0]
            references[sample_id] = {
                "path": _project_relative(candidate, root),
                "sha256": file_sha256(candidate),
            }
    request["retry_instruction_by_sample"] = instructions
    request["rejected_reference_asset_by_sample"] = references
    return write_json_atomic(paths["supplemental_request"], request)


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
                if not isinstance(item, Mapping) or item.get("passed") is not True:
                    continue
                evidence = item.get("evidence") or item.get("observations") or item.get("conclusion")
                if not evidence:
                    continue
                dimension = str(item.get("dimension") or "")
                if dimension:
                    actual_adherence.add(dimension)
                relevant = item.get("relevant_contract")
                contract_paths = relevant if isinstance(relevant, list) else [relevant]
                for contract_path in contract_paths:
                    value = str(contract_path or "")
                    for name in ("visual_style", "characters", "world_scale", "story_state"):
                        if value == name or f"contracts.{name}" in value:
                            actual_adherence.add(name)
        if not expected_adherence.issubset(actual_adherence):
            issues.append("contract_adherence missing: " + ",".join(sorted(expected_adherence - actual_adherence)))
    issues.extend(product_quality_review_issues(payload.get("product_quality"), plan.get("review_profile", {})))
    matrix = payload.get("evidence_matrix")
    sample_ids = {str(item.get("sample_id")) for item in plan.get("requirements", []) if isinstance(item, Mapping)}
    covered = {
        str(item.get("sample_id"))
        for item in matrix
        if isinstance(matrix, list)
        and isinstance(item, Mapping)
        and (item.get("evidence") or item.get("observations") or item.get("conclusion"))
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
    "enrich_visual_sample_supplemental_request",
    "validate_visual_sample_plan", "visual_sample_asset_paths", "visual_sample_binding",
    "visual_sample_generation_jobs",
    "visual_sample_lock_is_current", "visual_sample_machine_issues",
    "visual_sample_paths", "visual_sample_plan_is_current",
    "visual_sample_review_payload_issues", "visual_sample_schema_parity_issues",
    "write_visual_sample_lock", "write_visual_sample_machine_qa", "write_visual_sample_plan",
    "write_visual_sample_supplemental_request",
]
