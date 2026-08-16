from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from story_semantics import classify_story


BINDING_FIELDS = (
    "contract_schema_version",
    "story_contract_sha256",
    "story_contract_dependency_sha256",
)


def load_consumer_context(path: Path | str, consumer: str) -> dict[str, Any]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("consumer") != consumer:
        raise ValueError(f"contract context consumer must be {consumer}")
    for field in BINDING_FIELDS:
        value = str(payload.get(field) or "")
        if not value or (field != "contract_schema_version" and len(value) != 64):
            raise ValueError(f"contract context missing or invalid {field}")
    projection = payload.get("contract_projection")
    if not isinstance(projection, dict):
        raise ValueError("contract context missing contract_projection")
    return payload


def projection_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload.get("contract_projection", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def binding(payload: Mapping[str, Any]) -> dict[str, str]:
    return {field: str(payload[field]) for field in BINDING_FIELDS}


def write_json_atomic(path: Path | str, payload: Mapping[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return target


def compile_cover_spec(context_path: Path | str, output_path: Path | str) -> Path:
    context = load_consumer_context(context_path, "cover")
    projection = context["contract_projection"]
    brand = projection.get("brand", {})
    layout = projection.get("release_layout", {})
    payload = {
        "version": 1,
        "consumer": "cover",
        **binding(context),
        "contract_projection_sha256": projection_sha256(context),
        "official_assets": [item for item in brand.get("assets", []) if "cover" in item.get("allowed_uses", [])],
        "brand_rules": brand.get("rules", []),
        "semantic_artifacts": projection.get("semantic_artifacts", {}),
        "visual_style": projection.get("visual_style", {}),
        "characters": projection.get("characters", {}),
        "layout_rules": layout.get("rules", []),
        "variants": layout.get("variants", []),
    }
    return write_json_atomic(output_path, payload)


def compile_release_render_spec(context_path: Path | str, output_path: Path | str) -> Path:
    context = load_consumer_context(context_path, "release_video")
    projection = context["contract_projection"]
    brand = projection.get("brand", {})
    layout = projection.get("release_layout", {})
    payload = {
        "version": 2,
        "consumer": "release_video",
        "source_contract_context_path": str(Path(context_path).expanduser().resolve()),
        "source_contract_context_sha256": hashlib.sha256(Path(context_path).read_bytes()).hexdigest(),
        **binding(context),
        "contract_projection_sha256": projection_sha256(context),
        "official_assets": [item for item in brand.get("assets", []) if "release_video" in item.get("allowed_uses", [])],
        "brand_rules": brand.get("rules", []),
        "layout_rules": layout.get("rules", []),
        "variants": layout.get("variants", []),
    }
    return write_json_atomic(output_path, payload)


def compile_demo_render_spec(
    context_path: Path | str,
    output_path: Path | str,
    *,
    official_logo_path: Path | str,
) -> Path:
    """Compile the minimal deterministic brand/layout input used by Demo.

    The logo is never inferred from a prior project: its bytes must match an
    official asset in the reviewed brand projection, and the reviewed layout
    must provide one logo region for a 16:9/main variant.
    """
    context = load_consumer_context(context_path, "release_video")
    projection = context["contract_projection"]
    brand = projection.get("brand", {})
    layout = projection.get("release_layout", {})
    logo_path = Path(official_logo_path).expanduser().resolve()
    if not logo_path.is_file():
        raise ValueError(f"Demo official logo missing: {logo_path}")
    logo_sha = hashlib.sha256(logo_path.read_bytes()).hexdigest()
    permitted = [
        item for item in brand.get("assets", [])
        if isinstance(item, Mapping)
        and logo_sha == item.get("sha256")
        and ({"release_video", "demo", "product_package"} & set(item.get("allowed_uses", [])))
    ]
    if len(permitted) != 1 or int(permitted[0].get("max_per_frame", 1)) != 1:
        raise ValueError("Demo logo must match exactly one reviewed official asset with max_per_frame=1")
    variants = [item for item in layout.get("variants", []) if isinstance(item, Mapping)]
    selected = next(
        (item for item in variants if str(item.get("aspect_ratio")) == "16:9" or "main" in str(item.get("variant_id", ""))),
        None,
    )
    if selected is None:
        raise ValueError("Demo requires a reviewed 16:9/main release_layout variant")
    logo_regions = [
        item for item in selected.get("regions", [])
        if isinstance(item, Mapping) and str(item.get("role")) in {"logo", "brand_logo"}
    ]
    if len(logo_regions) != 1:
        raise ValueError("Demo release_layout must contain exactly one logo region")
    logo_region = dict(logo_regions[0])
    payload = {
        "version": 1,
        "consumer": "demo",
        "source_contract_context_path": str(Path(context_path).expanduser().resolve()),
        "source_contract_context_sha256": hashlib.sha256(Path(context_path).read_bytes()).hexdigest(),
        **binding(context),
        "contract_projection_sha256": projection_sha256(context),
        "official_logo_path": str(logo_path),
        "official_logo_sha256": logo_sha,
        "official_logo_count": 1,
        "official_asset": dict(permitted[0]),
        "logo_region": logo_region,
        "brand_rules": brand.get("rules", []),
        "layout_rules": layout.get("rules", []),
        "variant_id": selected.get("variant_id"),
        "aspect_ratio": selected.get("aspect_ratio"),
    }
    return write_json_atomic(output_path, payload)


def demo_logo_arguments(spec: Mapping[str, Any], width: int, height: int) -> dict[str, Any]:
    if spec.get("consumer") != "demo" or spec.get("official_logo_count") != 1:
        raise ValueError("invalid Demo render spec")
    region = spec.get("logo_region")
    if not isinstance(region, Mapping):
        raise ValueError("Demo render spec missing logo_region")
    x, y, logo_width, _logo_height = pixel_box(region, width, height)
    return {
        "logo_path": Path(str(spec["official_logo_path"])),
        "logo_sha256": str(spec["official_logo_sha256"]),
        "logo_x": x,
        "logo_y": y,
        "logo_width": max(1, logo_width),
    }


def compile_product_content_spec(context_path: Path | str, output_path: Path | str) -> Path:
    context = load_consumer_context(context_path, "product_package")
    semantic = context["contract_projection"].get("semantic_artifacts", {})
    payload = {
        "version": 1,
        "consumer": "product_package",
        **binding(context),
        "contract_projection_sha256": projection_sha256(context),
        "rules": semantic.get("rules", []),
        "mappings": semantic.get("mappings", []),
    }
    return write_json_atomic(output_path, payload)


ARTIFACT_ALIASES = {
    "ppt": {"ppt", "story_ppt", "presentation"},
    "customer_manuscript": {"customer_manuscript", "manuscript", "story_text"},
    "reading_annotation": {"reading_annotation", "annotation", "performance_script"},
    "demo": {"demo", "demo_video", "demonstration_video"},
}


def semantic_line_indices(lines: Iterable[str], spec: Mapping[str, Any], artifact: str) -> list[int]:
    material = list(lines)
    aliases = ARTIFACT_ALIASES.get(artifact, {artifact})
    decisions: dict[str, str] = {}
    for item in spec.get("mappings", []):
        if isinstance(item, Mapping) and str(item.get("artifact")) in aliases:
            decisions[str(item.get("semantic_kind"))] = str(item.get("action"))
    if not decisions:
        return list(range(len(material)))
    semantics = classify_story(material)
    selected: list[int] = []
    for index, line in enumerate(semantics.lines):
        kind = semantics.kind_at(line.line_number)
        action = decisions.get(kind.value if kind is not None else "")
        if action in {"include", "visual_substitute"}:
            selected.append(index)
    return selected


def select_semantic_lines(lines: Iterable[str], spec: Mapping[str, Any], artifact: str) -> list[str]:
    material = list(lines)
    return [material[index] for index in semantic_line_indices(material, spec, artifact)]


def regions_for_variant(spec: Mapping[str, Any], variant_hint: str) -> dict[str, Mapping[str, Any]]:
    variants = [item for item in spec.get("variants", []) if isinstance(item, Mapping)]
    selected = next((item for item in variants if variant_hint in str(item.get("variant_id", ""))), None)
    if selected is None and variants:
        selected = variants[0]
    return {
        str(item.get("role")): item
        for item in ((selected or {}).get("regions", []))
        if isinstance(item, Mapping) and str(item.get("role") or "")
    }


def pixel_box(region: Mapping[str, Any], width: int, height: int) -> tuple[int, int, int, int]:
    return (
        round(float(region["x"]) * width),
        round(float(region["y"]) * height),
        round(float(region["width"]) * width),
        round(float(region["height"]) * height),
    )


def release_argument_overrides(spec: Mapping[str, Any], variant_hint: str) -> dict[str, Any]:
    """Compile normalized reviewed regions into the existing FFmpeg CLI geometry."""
    regions = regions_for_variant(spec, variant_hint)
    result: dict[str, Any] = {"safe_regions": dict(regions)}
    person = regions.get("person") or regions.get("host")
    story = regions.get("story_media") or regions.get("story")
    logo = regions.get("logo") or regions.get("brand_logo")
    subtitle = regions.get("subtitle_safe") or regions.get("subtitle")
    # The presenter region is a *safe placement region*, not a fit-to-box
    # instruction.  M2-2C2 compiles the approved Demo/source-native transform
    # and permits horizontal correction only; applying this region as a target
    # height would silently shrink the presenter a second time.
    if person:
        result["person_safe_region"] = dict(person)
    if story:
        result["story_box"] = ",".join(str(value) for value in pixel_box(story, 1920, 1080))
    if logo:
        x, y, width, _height = pixel_box(logo, 1920, 1080)
        result.update(story_logo_x=x, story_logo_y=y, story_logo_width_a=max(1, width))
    if subtitle:
        _x, y, _width, height = pixel_box(subtitle, 1920, 1080)
        result["subtitle_margin_v"] = max(0, 1080 - y - height)
    return result


__all__ = [
    "BINDING_FIELDS", "binding", "compile_cover_spec", "compile_product_content_spec",
    "compile_demo_render_spec", "compile_release_render_spec", "demo_logo_arguments",
    "load_consumer_context", "pixel_box", "projection_sha256",
    "regions_for_variant", "release_argument_overrides", "select_semantic_lines",
    "semantic_line_indices", "write_json_atomic",
]
