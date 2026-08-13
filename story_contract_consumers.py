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
        "version": 1,
        "consumer": "release_video",
        **binding(context),
        "contract_projection_sha256": projection_sha256(context),
        "official_assets": [item for item in brand.get("assets", []) if "release_video" in item.get("allowed_uses", [])],
        "brand_rules": brand.get("rules", []),
        "layout_rules": layout.get("rules", []),
        "variants": layout.get("variants", []),
    }
    return write_json_atomic(output_path, payload)


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
    if person:
        x, y, _width, height = pixel_box(person, 1920, 1080)
        result.update(person_x=x, person_y=y, person_height=height)
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
    "compile_release_render_spec", "load_consumer_context", "pixel_box", "projection_sha256",
    "regions_for_variant", "release_argument_overrides", "select_semantic_lines",
    "semantic_line_indices", "write_json_atomic",
]
