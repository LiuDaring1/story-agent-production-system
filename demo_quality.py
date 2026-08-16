from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from artifact_semantic_plan import load_current_artifact_semantic_plan, plan_binding, semantic_plan_path
from keying_quality import file_sha256, keying_preset_lock_issues, write_json_atomic
from production_keying import production_keying_fingerprint
from release_geometry import canonical_sha256, demo_presenter_geometry_issues
from story_contract_consumers import (
    BINDING_FIELDS,
    binding,
    demo_logo_arguments,
    load_consumer_context,
    projection_sha256,
)


DEMO_MANIFEST_VERSION = "story-demo-render/v2"


def load_demo_brand_spec(path: Path | str) -> tuple[dict[str, Any], dict[str, Any]]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("consumer") != "demo" or payload.get("version") != 1:
        raise ValueError("invalid Demo brand/render spec")
    for field in (*BINDING_FIELDS, "contract_projection_sha256"):
        value = str(payload.get(field) or "")
        if not value or (field != "contract_schema_version" and len(value) != 64):
            raise ValueError(f"Demo render spec missing or invalid {field}")
    context_path = Path(str(payload.get("source_contract_context_path") or ""))
    if (
        not context_path.is_file()
        or file_sha256(context_path) != payload.get("source_contract_context_sha256")
    ):
        raise ValueError("Demo render spec source contract context is stale")
    context = load_consumer_context(context_path, "release_video")
    if binding(context) != {field: payload.get(field) for field in BINDING_FIELDS}:
        raise ValueError("Demo render spec contract binding is stale")
    if projection_sha256(context) != payload.get("contract_projection_sha256"):
        raise ValueError("Demo render spec projection binding is stale")
    official_asset = payload.get("official_asset")
    if not isinstance(official_asset, Mapping):
        raise ValueError("Demo render spec missing reviewed official asset")
    allowed_uses = official_asset.get("allowed_uses")
    if (
        official_asset.get("sha256") != payload.get("official_logo_sha256")
        or not isinstance(allowed_uses, list)
        or not ({"demo", "release_video", "product_package"} & set(allowed_uses))
        or int(official_asset.get("max_per_frame", 0)) != 1
    ):
        raise ValueError("Demo render spec official asset is not authorized")
    arguments = demo_logo_arguments(payload, 1920, 1080)
    logo = arguments["logo_path"]
    if not logo.is_file() or file_sha256(logo) != arguments["logo_sha256"]:
        raise ValueError("Demo official logo bytes no longer match reviewed brand projection")
    return payload, arguments


def write_demo_render_manifest(
    output_path: Path,
    *,
    project_dir: Path,
    semantic_plan: Mapping[str, Any],
    demo_brand_spec_path: Path,
    demo_brand_spec: Mapping[str, Any],
    keying_preset_path: Path,
    source_greenscreen: Path,
    presenter_geometry: Mapping[str, Any],
    output_artifacts: list[Path],
    preview: bool,
) -> Path:
    lock_path = keying_preset_path.with_name("keying_preset.lock.json")
    lock_issues = keying_preset_lock_issues(keying_preset_path, lock_path)
    if lock_issues:
        raise ValueError("invalid reviewed keying preset: " + ";".join(lock_issues))
    plan_path = semantic_plan_path(project_dir)
    logo_path = Path(str(demo_brand_spec["official_logo_path"]))
    preset_payload = json.loads(keying_preset_path.read_text(encoding="utf-8"))
    expected_geometry_bindings = {
        "keying_preset_sha256": file_sha256(keying_preset_path),
        "keying_lock_sha256": file_sha256(lock_path),
        "source_greenscreen_sha256": file_sha256(source_greenscreen),
        "production_keying_filter_fingerprint": production_keying_fingerprint(preset_payload),
    }
    geometry_issues = demo_presenter_geometry_issues(presenter_geometry, expected_geometry_bindings)
    if geometry_issues:
        raise ValueError("invalid actual Demo presenter geometry: " + ";".join(geometry_issues))
    payload = {
        "schema_version": DEMO_MANIFEST_VERSION,
        "mode": "preview" if preview else "final",
        **plan_binding(plan_path, semantic_plan),
        **{field: str(demo_brand_spec[field]) for field in BINDING_FIELDS},
        "demo_brand_projection_sha256": str(demo_brand_spec["contract_projection_sha256"]),
        "demo_brand_spec_path": str(demo_brand_spec_path),
        "demo_brand_spec_sha256": file_sha256(demo_brand_spec_path),
        "official_logo_path": str(logo_path),
        "official_logo_sha256": file_sha256(logo_path),
        "official_logo_count": 1,
        "keying_preset_path": str(keying_preset_path),
        "keying_preset_sha256": file_sha256(keying_preset_path),
        "keying_lock_path": str(lock_path),
        "keying_lock_sha256": file_sha256(lock_path),
        "source_greenscreen_path": str(source_greenscreen),
        "source_greenscreen_sha256": file_sha256(source_greenscreen),
        "production_keying_filter_fingerprint": expected_geometry_bindings["production_keying_filter_fingerprint"],
        "presenter_geometry": dict(presenter_geometry),
        "approved_presenter_geometry_sha256": str(presenter_geometry["geometry_sha256"]),
        "artifacts": [
            {"path": str(path), "sha256": file_sha256(path)} for path in output_artifacts
        ],
    }
    payload["demo_render_manifest_sha256"] = canonical_sha256(payload)
    return write_json_atomic(output_path, payload)


def demo_render_manifest_issues(
    manifest_path: Path,
    project_dir: Path,
    *,
    require_final: bool = False,
) -> list[str]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        plan_path = semantic_plan_path(project_dir)
        plan = load_current_artifact_semantic_plan(project_dir)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return ["demo_render_manifest_missing_or_invalid"]
    if payload.get("schema_version") != DEMO_MANIFEST_VERSION:
        return ["demo_render_manifest_schema_invalid"]
    issues: list[str] = []
    unsigned = dict(payload)
    stored_manifest_hash = str(unsigned.pop("demo_render_manifest_sha256", ""))
    if not stored_manifest_hash or stored_manifest_hash != canonical_sha256(unsigned):
        issues.append("demo_render_manifest_sha256_mismatch")
    if require_final and payload.get("mode") != "final":
        issues.append("demo_render_manifest_not_final")
    for field, value in plan_binding(plan_path, plan).items():
        if payload.get(field) != value:
            issues.append(f"demo_semantic_plan_binding_mismatch:{field}")
    bindings = {
        "brand_spec": (payload.get("demo_brand_spec_path"), payload.get("demo_brand_spec_sha256")),
        "logo": (payload.get("official_logo_path"), payload.get("official_logo_sha256")),
        "keying_preset": (payload.get("keying_preset_path"), payload.get("keying_preset_sha256")),
        "keying_lock": (payload.get("keying_lock_path"), payload.get("keying_lock_sha256")),
        "source": (payload.get("source_greenscreen_path"), payload.get("source_greenscreen_sha256")),
    }
    for label, (raw_path, expected) in bindings.items():
        path = Path(str(raw_path or ""))
        if not path.is_file() or file_sha256(path) != expected:
            issues.append(f"demo_render_binding_mismatch:{label}")
    try:
        current_brand_spec, _arguments = load_demo_brand_spec(
            Path(str(payload.get("demo_brand_spec_path") or ""))
        )
        for field in BINDING_FIELDS:
            if payload.get(field) != current_brand_spec.get(field):
                issues.append(f"demo_brand_binding_mismatch:{field}")
        if payload.get("demo_brand_projection_sha256") != current_brand_spec.get("contract_projection_sha256"):
            issues.append("demo_brand_projection_sha256_mismatch")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        issues.append("demo_brand_spec_not_current")
    if payload.get("official_logo_count") != 1:
        issues.append("demo_official_logo_count_invalid")
    preset = Path(str(payload.get("keying_preset_path") or ""))
    issues.extend(keying_preset_lock_issues(preset))
    try:
        preset_payload = json.loads(preset.read_text(encoding="utf-8"))
        expected_geometry_bindings = {
            "keying_preset_sha256": payload.get("keying_preset_sha256"),
            "keying_lock_sha256": payload.get("keying_lock_sha256"),
            "source_greenscreen_sha256": payload.get("source_greenscreen_sha256"),
            "production_keying_filter_fingerprint": production_keying_fingerprint(preset_payload),
        }
        geometry = payload.get("presenter_geometry")
        if not isinstance(geometry, Mapping):
            issues.append("demo_presenter_geometry_missing")
        else:
            issues.extend(demo_presenter_geometry_issues(geometry, expected_geometry_bindings))
            if payload.get("approved_presenter_geometry_sha256") != geometry.get("geometry_sha256"):
                issues.append("demo_approved_presenter_geometry_sha256_mismatch")
        if payload.get("production_keying_filter_fingerprint") != expected_geometry_bindings["production_keying_filter_fingerprint"]:
            issues.append("demo_production_keying_filter_fingerprint_mismatch")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        issues.append("demo_presenter_geometry_binding_invalid")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        issues.append("demo_render_artifacts_missing")
    else:
        for item in artifacts:
            path = Path(str(item.get("path") or "")) if isinstance(item, dict) else Path("")
            if not path.is_file() or file_sha256(path) != (item.get("sha256") if isinstance(item, dict) else None):
                issues.append("demo_render_artifact_binding_mismatch")
                break
    return sorted(set(issues))


def load_current_final_demo_geometry(
    manifest_path: Path,
    project_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    issues = demo_render_manifest_issues(manifest_path, project_dir, require_final=True)
    if issues:
        raise ValueError("final Demo presenter geometry receipt invalid: " + "; ".join(issues))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    geometry = payload.get("presenter_geometry")
    if not isinstance(geometry, dict):
        raise ValueError("final Demo presenter geometry receipt missing geometry")
    return payload, geometry


__all__ = [
    "DEMO_MANIFEST_VERSION", "demo_render_manifest_issues", "load_current_final_demo_geometry", "load_demo_brand_spec",
    "write_demo_render_manifest",
]
