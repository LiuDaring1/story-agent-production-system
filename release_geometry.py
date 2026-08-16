from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


RELEASE_GEOMETRY_SCHEMA_VERSION = "story-release-geometry/v1"
RELEASE_GEOMETRY_COMPILER_VERSION = "1.1.0"
DEMO_PRESENTER_GEOMETRY_SCHEMA_VERSION = "story-demo-presenter-geometry/v1"


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _box(region: Mapping[str, Any], width: int, height: int) -> dict[str, int]:
    box = {
        "x": round(float(region["x"]) * width),
        "y": round(float(region["y"]) * height),
        "width": round(float(region["width"]) * width),
        "height": round(float(region["height"]) * height),
    }
    if min(box["x"], box["y"]) < 0 or min(box["width"], box["height"]) <= 0:
        raise ValueError("release layout region has invalid geometry")
    if box["x"] + box["width"] > width or box["y"] + box["height"] > height:
        raise ValueError("release layout region exceeds canvas")
    return box


def regions_for_variant(spec: Mapping[str, Any], hint: str, aspect_ratio: str | None = None) -> dict[str, Mapping[str, Any]]:
    variants = [item for item in spec.get("variants", []) if isinstance(item, Mapping)]
    candidates = [item for item in variants if hint in str(item.get("variant_id", ""))]
    if aspect_ratio:
        exact = [item for item in candidates if str(item.get("aspect_ratio")) == aspect_ratio]
        if exact:
            candidates = exact
    if not candidates and aspect_ratio:
        candidates = [item for item in variants if str(item.get("aspect_ratio")) == aspect_ratio]
    selected = candidates[0] if candidates else (variants[0] if variants else None)
    return {
        str(item.get("role")): item
        for item in ((selected or {}).get("regions", []))
        if isinstance(item, Mapping) and str(item.get("role") or "")
    }


def approved_demo_geometry(
    source_width: int,
    source_height: int,
    canvas_width: int,
    canvas_height: int,
    detected_bbox: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Compile the same source-native transform used by the approved Demo.

    The detected box only removes transparent/green surroundings.  It does not
    fit the subject to a target box: scale is derived exclusively from the
    complete source frame and canvas.
    """
    if min(source_width, source_height, canvas_width, canvas_height) <= 0:
        raise ValueError("presenter source/canvas dimensions must be positive")
    scale = min(canvas_width / source_width, canvas_height / source_height)
    source_canvas_x = round((canvas_width - source_width * scale) / 2)
    source_canvas_y = round((canvas_height - source_height * scale) / 2)
    crop = [0, 0, source_width, source_height]
    if detected_bbox is not None:
        if len(detected_bbox) != 4:
            raise ValueError("detected presenter bbox must contain four integers")
        x, y, width, height = (int(value) for value in detected_bbox)
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > source_width or y + height > source_height:
            raise ValueError("detected presenter bbox exceeds source")
        crop = [x, y, width, height]
    x, y, width, height = crop
    return {
        "source_width": source_width,
        "source_height": source_height,
        "source_crop": crop,
        "rendered_width": max(1, round(width * scale)),
        "rendered_height": max(1, round(height * scale)),
        "scale": scale,
        "x": source_canvas_x + round(x * scale),
        "y": source_canvas_y + round(y * scale),
        "crop_mode": "detected_bbox_source_native" if detected_bbox is not None else "source_native",
        "vertical_alignment": "source_canvas_center",
        "source_native": True,
    }


def compile_demo_presenter_geometry(
    source_width: int,
    source_height: int,
    canvas_width: int,
    canvas_height: int,
    *,
    person_crop: Sequence[int] | None,
    detected_bbox: Sequence[int] | None,
    person_height_ratio: float,
    crop_mode: str,
    crop_bottom_ratio: float,
    vertical_alignment: str,
    keying_preset_sha256: str,
    keying_lock_sha256: str,
    source_greenscreen_sha256: str,
    production_keying_filter_fingerprint: str,
) -> dict[str, Any]:
    """Compile the presenter transform actually used by the Demo renderer.

    This receipt is deliberately derived from the same crop/scale/placement
    inputs consumed by FFmpeg.  It is not an aesthetic plan and is never
    recomputed by Release as a substitute for a reviewed final Demo receipt.
    """
    if min(source_width, source_height, canvas_width, canvas_height) <= 0:
        raise ValueError("Demo presenter source/canvas dimensions must be positive")
    if crop_mode not in {"source-native", "preset", "full-width"}:
        raise ValueError("unsupported Demo presenter crop mode")
    if vertical_alignment not in {"center", "bottom"}:
        raise ValueError("unsupported Demo presenter vertical alignment")
    if not 0 < float(person_height_ratio) <= 1:
        raise ValueError("Demo presenter height ratio must be in (0, 1]")
    if not 0 <= float(crop_bottom_ratio) <= 0.2:
        raise ValueError("Demo presenter crop bottom ratio must be in [0, 0.2]")

    source_native = crop_mode == "source-native" or person_crop is None
    if source_native:
        geometry = approved_demo_geometry(
            source_width,
            source_height,
            canvas_width,
            canvas_height,
            detected_bbox,
        )
        # Bottom cropping and vertical-align are not applied on the native
        # path.  Recording the resolved values prevents a requested-but-unused
        # option from being mistaken for an actual FFmpeg transform.
        geometry.update({
            "crop_mode": "source-native",
            "crop_bottom_ratio": 0.0,
            "vertical_alignment": "source_canvas_center",
            "source_native": True,
        })
    else:
        if person_crop is None or len(person_crop) != 4:
            raise ValueError("non-native Demo presenter geometry requires person_crop")
        crop_x, crop_y, crop_width, crop_height = (int(value) for value in person_crop)
        if crop_mode == "full-width":
            crop_x = 0
            crop_width = source_width
            crop_height = min(crop_height, source_height - crop_y)
        crop_height = max(1, int(round(crop_height * (1 - float(crop_bottom_ratio)))))
        if (
            crop_x < 0 or crop_y < 0 or crop_width <= 0 or crop_height <= 0
            or crop_x + crop_width > source_width or crop_y + crop_height > source_height
        ):
            raise ValueError("Demo presenter crop exceeds source")
        rendered_height = min(int(canvas_height * float(person_height_ratio)), canvas_height)
        rendered_width = max(1, round(crop_width * rendered_height / crop_height))
        x = round((canvas_width - rendered_width) / 2)
        y = canvas_height - rendered_height if vertical_alignment == "bottom" else round((canvas_height - rendered_height) / 2)
        geometry = {
            "source_width": source_width,
            "source_height": source_height,
            "source_crop": [crop_x, crop_y, crop_width, crop_height],
            "rendered_width": rendered_width,
            "rendered_height": rendered_height,
            "scale": rendered_height / crop_height,
            "x": x,
            "y": y,
            "crop_mode": crop_mode,
            "crop_bottom_ratio": float(crop_bottom_ratio),
            "vertical_alignment": vertical_alignment,
            "source_native": False,
        }
    payload = {
        "schema_version": DEMO_PRESENTER_GEOMETRY_SCHEMA_VERSION,
        **geometry,
        "canvas_width": canvas_width,
        "canvas_height": canvas_height,
        "keying_preset_sha256": keying_preset_sha256,
        "keying_lock_sha256": keying_lock_sha256,
        "source_greenscreen_sha256": source_greenscreen_sha256,
        "production_keying_filter_fingerprint": production_keying_filter_fingerprint,
    }
    payload["geometry_sha256"] = canonical_sha256(payload)
    return payload


def demo_presenter_geometry_issues(
    payload: Mapping[str, Any],
    expected_bindings: Mapping[str, Any] | None = None,
) -> list[str]:
    issues: list[str] = []
    if payload.get("schema_version") != DEMO_PRESENTER_GEOMETRY_SCHEMA_VERSION:
        issues.append("demo_presenter_geometry_schema_mismatch")
    for field in (
        "source_width", "source_height", "canvas_width", "canvas_height",
        "rendered_width", "rendered_height", "scale", "x", "y",
        "source_crop", "crop_mode", "crop_bottom_ratio", "vertical_alignment",
        "source_native", "keying_preset_sha256", "keying_lock_sha256",
        "source_greenscreen_sha256", "production_keying_filter_fingerprint",
    ):
        if field not in payload:
            issues.append(f"demo_presenter_geometry_field_missing:{field}")
    if not isinstance(payload.get("source_native"), bool):
        issues.append("demo_presenter_geometry_source_native_type_invalid")
    for field in (
        "source_width", "source_height", "canvas_width", "canvas_height",
        "rendered_width", "rendered_height",
    ):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            issues.append(f"demo_presenter_geometry_dimension_invalid:{field}")
    for field in ("x", "y"):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            issues.append(f"demo_presenter_geometry_position_invalid:{field}")
    scale = payload.get("scale")
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or scale <= 0:
        issues.append("demo_presenter_geometry_scale_invalid")
    crop = payload.get("source_crop")
    if (
        not isinstance(crop, list)
        or len(crop) != 4
        or any(isinstance(value, bool) or not isinstance(value, int) for value in crop)
        or (len(crop) == 4 and (crop[0] < 0 or crop[1] < 0 or crop[2] <= 0 or crop[3] <= 0))
    ):
        issues.append("demo_presenter_geometry_source_crop_invalid")
    elif (
        isinstance(payload.get("source_width"), int)
        and isinstance(payload.get("source_height"), int)
        and (crop[0] + crop[2] > payload["source_width"] or crop[1] + crop[3] > payload["source_height"])
    ):
        issues.append("demo_presenter_geometry_source_crop_out_of_bounds")
    elif isinstance(scale, (int, float)) and not isinstance(scale, bool) and scale > 0:
        expected_width = round(crop[2] * scale)
        expected_height = round(crop[3] * scale)
        if abs(int(payload.get("rendered_width") or 0) - expected_width) > 1:
            issues.append("demo_presenter_geometry_rendered_width_inconsistent")
        if abs(int(payload.get("rendered_height") or 0) - expected_height) > 1:
            issues.append("demo_presenter_geometry_rendered_height_inconsistent")
    crop_bottom_ratio = payload.get("crop_bottom_ratio")
    if (
        isinstance(crop_bottom_ratio, bool)
        or not isinstance(crop_bottom_ratio, (int, float))
        or not 0.0 <= float(crop_bottom_ratio) < 1.0
    ):
        issues.append("demo_presenter_geometry_crop_bottom_ratio_invalid")
    if payload.get("crop_mode") not in {"source-native", "preset", "full-width"}:
        issues.append("demo_presenter_geometry_crop_mode_invalid")
    if payload.get("vertical_alignment") not in {"bottom", "center", "source_canvas_center"}:
        issues.append("demo_presenter_geometry_vertical_alignment_invalid")
    if expected_bindings is not None:
        issues.extend(
            f"demo_presenter_geometry_binding_mismatch:{field}"
            for field, expected in sorted(expected_bindings.items())
            if str(payload.get(field) or "") != str(expected or "")
        )
    stored_hash = str(payload.get("geometry_sha256") or "")
    unsigned = dict(payload)
    unsigned.pop("geometry_sha256", None)
    if not stored_hash or stored_hash != canonical_sha256(unsigned):
        issues.append("demo_presenter_geometry_sha256_mismatch")
    return sorted(set(issues))


def release_a_geometry(
    demo: Mapping[str, Any],
    person_region: Mapping[str, Any],
    canvas_width: int,
    canvas_height: int,
) -> dict[str, Any]:
    """Place an approved presenter by horizontal translation only.

    A region narrower/shorter than the approved presenter would require a
    scale or vertical correction and is therefore blocked rather than silently
    shrinking the presenter.
    """
    safe = _box(person_region, canvas_width, canvas_height)
    width = int(demo["rendered_width"])
    height = int(demo["rendered_height"])
    y = int(demo["y"])
    if width > safe["width"]:
        raise ValueError("release person safe region requires presenter rescale; blocking instead of fit-to-box")
    if y < safe["y"] or y + height > safe["y"] + safe["height"]:
        raise ValueError("release person vertical safe region requires scale/vertical change; blocking")
    original_x = int(demo["x"])
    minimum_x = safe["x"]
    maximum_x = safe["x"] + safe["width"] - width
    x = min(max(original_x, minimum_x), maximum_x)
    correction = x - original_x
    return {
        **dict(demo),
        "x": x,
        "scale_changed": False,
        "position_correction": {"x": correction, "y": 0},
        "correction_reason": (
            "left_overflow_minimal_correction" if correction > 0
            else "right_overflow_minimal_correction" if correction < 0
            else "none"
        ),
        "person_safe_region": safe,
    }


def compile_text_group(
    region: Mapping[str, Any],
    canvas_width: int,
    canvas_height: int,
    lines: Sequence[Mapping[str, Any]],
    *,
    padding_fraction: float = 0.08,
) -> dict[str, Any]:
    group = _box(region, canvas_width, canvas_height)
    padding = max(8, round(min(group["width"], group["height"]) * padding_fraction))
    safe = {
        "x": group["x"] + padding,
        "y": group["y"] + padding,
        "width": group["width"] - 2 * padding,
        "height": group["height"] - 2 * padding,
    }
    if min(safe["width"], safe["height"]) <= 0:
        raise ValueError("library text group has no usable safe area")
    material = [dict(item) for item in lines if str(item.get("text") or "").strip()]
    if not material:
        raise ValueError("library text group requires at least one line")
    line_height = safe["height"] // len(material)
    if line_height < 24:
        raise ValueError("library text group lines cannot fit reviewed region")
    placements = []
    for index, item in enumerate(material):
        placements.append({
            **item,
            "x": safe["x"],
            "y": safe["y"] + index * line_height,
            "width": safe["width"],
            "height": line_height,
            "alignment": "center",
            "hierarchy": int(item.get("hierarchy", index + 1)),
        })
    return {
        "bounding_box": group,
        "safe_area": safe,
        "center": {"x": group["x"] + group["width"] / 2, "y": group["y"] + group["height"] / 2},
        "margins": {"left": padding, "right": padding, "top": padding, "bottom": padding},
        "line_spacing": 0,
        "alignment": "center",
        "lines": placements,
    }


def text_group_issues(group: Mapping[str, Any], required_center: Mapping[str, float] | None = None) -> list[str]:
    issues: list[str] = []
    box = group.get("bounding_box")
    safe = group.get("safe_area")
    if not isinstance(box, Mapping) or not isinstance(safe, Mapping):
        return ["library_text_group_geometry_missing"]
    if safe["x"] < box["x"] or safe["y"] < box["y"] or safe["x"] + safe["width"] > box["x"] + box["width"] or safe["y"] + safe["height"] > box["y"] + box["height"]:
        issues.append("library_text_group_safe_area_out_of_bounds")
    placements = group.get("lines")
    if not isinstance(placements, list) or not placements:
        issues.append("library_text_group_lines_missing")
    else:
        previous_bottom = None
        for line in placements:
            if line["x"] < safe["x"] or line["x"] + line["width"] > safe["x"] + safe["width"]:
                issues.append("library_text_group_line_out_of_bounds")
            if previous_bottom is not None and line["y"] < previous_bottom:
                issues.append("library_text_group_line_overlap")
            previous_bottom = line["y"] + line["height"]
    if required_center:
        center = group.get("center", {})
        tolerance = max(2.0, min(float(box["width"]), float(box["height"])) * 0.03)
        if abs(float(center.get("x", 0)) - float(required_center["x"])) > tolerance or abs(float(center.get("y", 0)) - float(required_center["y"])) > tolerance:
            issues.append("library_text_group_center_mismatch")
    return sorted(set(issues))


def binding_payload(
    spec: Mapping[str, Any],
    *,
    compiled_spec_sha256: str,
    semantic_plan_path: Path | None,
    keying_preset_path: Path | None,
    keying_filter_fingerprint: str,
) -> dict[str, Any]:
    payload = {
        "story_contract_sha256": str(spec.get("story_contract_sha256") or ""),
        "contract_schema_version": str(spec.get("contract_schema_version") or ""),
        "contract_projection_sha256": str(spec.get("contract_projection_sha256") or ""),
        "story_contract_dependency_sha256": str(spec.get("story_contract_dependency_sha256") or ""),
        "release_projection_sha256": str(spec.get("contract_projection_sha256") or ""),
        "release_dependency_sha256": str(spec.get("story_contract_dependency_sha256") or ""),
        "compiled_release_spec_sha256": compiled_spec_sha256,
        "production_keying_filter_fingerprint": keying_filter_fingerprint,
    }
    if semantic_plan_path is not None:
        payload["artifact_semantic_plan_sha256"] = file_sha256(semantic_plan_path)
    if keying_preset_path is not None:
        payload["keying_preset_sha256"] = file_sha256(keying_preset_path)
        lock = keying_preset_path.with_name("keying_preset.lock.json")
        payload["keying_lock_sha256"] = file_sha256(lock) if lock.is_file() else ""
    return payload


def geometry_manifest_issues(
    payload: Mapping[str, Any],
    expected_bindings: Mapping[str, Any] | None = None,
) -> list[str]:
    """Validate a persisted geometry manifest, including its self hash.

    This intentionally checks structure and deterministic integrity only.  The
    caller separately recompiles from the current locked inputs so a valid old
    manifest cannot authorize a changed contract or keying chain.
    """
    issues: list[str] = []
    if payload.get("schema_version") != RELEASE_GEOMETRY_SCHEMA_VERSION:
        issues.append("release_geometry_schema_version_mismatch")
    if payload.get("compiler_version") != RELEASE_GEOMETRY_COMPILER_VERSION:
        issues.append("release_geometry_compiler_version_mismatch")
    bindings = payload.get("bindings")
    required_bindings = {
        "story_contract_sha256", "contract_schema_version",
        "contract_projection_sha256", "story_contract_dependency_sha256",
        "release_projection_sha256", "release_dependency_sha256",
        "compiled_release_spec_sha256", "artifact_semantic_plan_sha256",
        "artifact_semantic_plan_schema_version",
        "artifact_semantic_plan_dependency_sha256",
        "production_keying_filter_fingerprint", "keying_preset_sha256",
        "keying_lock_sha256", "demo_render_manifest_sha256",
        "approved_demo_geometry_sha256",
    }
    if not isinstance(bindings, Mapping):
        issues.append("release_geometry_bindings_missing")
    else:
        issues.extend(
            f"release_geometry_binding_missing:{field}"
            for field in sorted(required_bindings)
            if not str(bindings.get(field) or "")
        )
        if expected_bindings is not None:
            issues.extend(
                f"release_geometry_binding_mismatch:{field}"
                for field, expected in sorted(expected_bindings.items())
                if str(bindings.get(field) or "") != str(expected or "")
            )
    if not isinstance(payload.get("main"), Mapping) or not isinstance(payload.get("library"), Mapping):
        issues.append("release_geometry_variant_geometry_missing")
    stored_hash = str(payload.get("geometry_sha256") or "")
    unsigned = dict(payload)
    unsigned.pop("geometry_sha256", None)
    if not stored_hash or stored_hash != canonical_sha256(unsigned):
        issues.append("release_geometry_sha256_mismatch")
    return sorted(set(issues))


def release_render_manifest_issues(
    payload: Mapping[str, Any],
    *,
    expected_bindings: Mapping[str, Any] | None = None,
    verify_outputs: bool = True,
) -> list[str]:
    """Validate the receipt for the geometry and bytes actually rendered."""
    issues: list[str] = []
    if payload.get("version") != 2 or payload.get("consumer") != "release_video":
        issues.append("release_render_manifest_version_mismatch")
    geometry = payload.get("actual_geometry")
    if not isinstance(geometry, Mapping):
        issues.append("release_render_actual_geometry_missing")
    else:
        issues.extend(geometry_manifest_issues(geometry, expected_bindings))
        if str(payload.get("release_geometry_sha256") or "") != str(geometry.get("geometry_sha256") or ""):
            issues.append("release_render_geometry_binding_mismatch")
    outputs = payload.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        issues.append("release_render_outputs_missing")
    elif verify_outputs:
        for item in outputs:
            if not isinstance(item, Mapping):
                issues.append("release_render_output_entry_invalid")
                continue
            path = Path(str(item.get("path") or ""))
            if not path.is_file():
                issues.append(f"release_render_output_missing:{path.name}")
            elif str(item.get("sha256") or "") != file_sha256(path):
                issues.append(f"release_render_output_sha256_mismatch:{path.name}")
    stored_hash = str(payload.get("render_manifest_sha256") or "")
    unsigned = dict(payload)
    unsigned.pop("render_manifest_sha256", None)
    if not stored_hash or stored_hash != canonical_sha256(unsigned):
        issues.append("release_render_manifest_sha256_mismatch")
    return sorted(set(issues))


__all__ = [
    "DEMO_PRESENTER_GEOMETRY_SCHEMA_VERSION",
    "RELEASE_GEOMETRY_COMPILER_VERSION", "RELEASE_GEOMETRY_SCHEMA_VERSION",
    "approved_demo_geometry", "binding_payload", "canonical_sha256",
    "compile_demo_presenter_geometry", "demo_presenter_geometry_issues",
    "compile_text_group", "file_sha256", "regions_for_variant",
    "geometry_manifest_issues", "release_a_geometry", "release_render_manifest_issues",
    "text_group_issues",
]
