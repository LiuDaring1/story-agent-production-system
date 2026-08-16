from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

from production_keying import (
    PRODUCTION_KEYING_FILTER_VERSION,
    production_keying_contract,
    production_keying_fingerprint,
    render_production_keyed_foreground,
)


QA_SCHEMA_VERSION = "story-keying-qa/v1"
LOCK_SCHEMA_VERSION = "story-keying-preset-lock/v1"


def file_sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def key_to_rgba(
    image: Image.Image,
    green: tuple[int, int, int],
    similarity: float,
    blend: float,
) -> Image.Image:
    source = image.convert("RGB")
    result = Image.new("RGBA", source.size)
    src = source.load()
    dst = result.load()
    lower = max(1.0, float(similarity) * 442)
    upper = max(lower + 1.0, (float(similarity) + float(blend)) * 442)
    for y in range(source.height):
        for x in range(source.width):
            r, g, b = src[x, y]
            distance = math.sqrt((r - green[0]) ** 2 + (g - green[1]) ** 2 + (b - green[2]) ** 2)
            dominant = g > r * 1.02 and g > b * 1.01
            alpha = 255 if not dominant or distance >= upper else (0 if distance <= lower else round(255 * (distance - lower) / (upper - lower)))
            dst[x, y] = (r, g, b, alpha)
    return result


def evidence_regions(alpha: Image.Image) -> dict[str, tuple[int, int, int, int]]:
    """Return applicable, silhouette-relative review regions.

    Names describe review intent rather than claiming pose estimation.  Missing
    lower-body regions are omitted when the detected foreground does not extend
    far enough down the frame.
    """
    mask = alpha.convert("L").point(lambda value: 255 if value >= 24 else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return {"full_body": (0, 0, alpha.width, alpha.height)}
    x1, y1, x2, y2 = bbox
    width, height = max(1, x2 - x1), max(1, y2 - y1)
    clamp = lambda box: (
        max(0, int(box[0])), max(0, int(box[1])), min(alpha.width, int(box[2])), min(alpha.height, int(box[3]))
    )
    result = {
        "full_body": clamp((x1, y1, x2, y2)),
        "head_hair": clamp((x1, y1, x2, y1 + height * 0.24)),
        "left_shoulder_forearm_hand": clamp((x1, y1 + height * 0.18, x1 + width * 0.43, y1 + height * 0.72)),
        "right_shoulder_forearm_hand": clamp((x1 + width * 0.57, y1 + height * 0.18, x2, y1 + height * 0.72)),
        "garment_outline": clamp((x1 + width * 0.12, y1 + height * 0.25, x2 - width * 0.12, y1 + height * 0.88)),
        "high_contrast_edges": clamp((x1 - width * 0.05, y1 - height * 0.03, x2 + width * 0.05, y2 + height * 0.03)),
    }
    if y2 >= alpha.height * 0.68:
        result["hem_lower_outline"] = clamp((x1, y1 + height * 0.72, x2, y2))
    if y2 >= alpha.height * 0.88:
        result["legs_feet"] = clamp((x1, y1 + height * 0.82, x2, y2))
    return {name: box for name, box in result.items() if box[2] > box[0] and box[3] > box[1]}


def _edge_mask(alpha: Image.Image) -> Image.Image:
    mask = alpha.convert("L").point(lambda value: 255 if value >= 24 else 0)
    return ImageChops.difference(mask.filter(ImageFilter.MaxFilter(5)), mask.filter(ImageFilter.MinFilter(5)))


def _alpha_topology(alpha: Image.Image) -> dict[str, float | int]:
    """Measure obvious matte breakage without assuming a realistic silhouette.

    The measurements are deliberately topology based: intended cartoon or
    fantasy proportions remain valid, while enclosed holes, detached fragments
    and high-frequency saw teeth are still observable.
    """
    mask = alpha.convert("L").point(lambda value: 255 if value >= 128 else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return {"alpha_hole_ratio": 0.0, "detached_component_count": 0, "edge_roughness": 0.0}
    cropped = mask.crop(bbox)
    width, height = cropped.size
    pixels = cropped.load()
    seen: set[tuple[int, int]] = set()

    def component(seed: tuple[int, int], foreground: bool) -> list[tuple[int, int]]:
        stack = [seed]
        found: list[tuple[int, int]] = []
        seen.add(seed)
        while stack:
            x, y = stack.pop()
            found.append((x, y))
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in seen:
                    if (pixels[nx, ny] > 0) is foreground:
                        seen.add((nx, ny))
                        stack.append((nx, ny))
        return found

    foreground_sizes: list[int] = []
    for y in range(height):
        for x in range(width):
            if pixels[x, y] and (x, y) not in seen:
                foreground_sizes.append(len(component((x, y), True)))
    foreground_area = max(1, sum(foreground_sizes))
    significant = sum(1 for size in foreground_sizes if size >= max(6, foreground_area * 0.004))

    # Transparent components not connected to the crop boundary are holes.
    seen.clear()
    hole_area = 0
    for y in range(height):
        for x in range(width):
            if not pixels[x, y] and (x, y) not in seen:
                transparent = component((x, y), False)
                touches_edge = any(px in {0, width - 1} or py in {0, height - 1} for px, py in transparent)
                if not touches_edge and len(transparent) >= 4:
                    hole_area += len(transparent)

    edge = _edge_mask(mask)
    edge_area = sum(1 for value in edge.get_flattened_data() if value > 0)
    smoothed = mask.filter(ImageFilter.GaussianBlur(1.5)).point(lambda value: 255 if value >= 128 else 0)
    unstable = sum(1 for value in ImageChops.difference(mask, smoothed).get_flattened_data() if value > 0)
    return {
        "alpha_hole_ratio": round(hole_area / foreground_area, 5),
        "detached_component_count": max(0, significant - 1),
        "edge_roughness": round(unstable / max(1, edge_area), 5),
    }


def analyze_keyed_rgba(image: Image.Image, *, region_boxes: dict[str, tuple[int, int, int, int]] | None = None) -> dict[str, Any]:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    regions = region_boxes or evidence_regions(alpha)
    edge = _edge_mask(alpha)
    rgb = rgba.convert("RGB")
    per_region: dict[str, Any] = {}
    critical: list[str] = []
    for name, box in regions.items():
        rgb_crop, alpha_crop, edge_crop = rgb.crop(box), alpha.crop(box), edge.crop(box)
        pixels = list(rgb_crop.get_flattened_data())
        alphas = list(alpha_crop.get_flattened_data())
        edges = list(edge_crop.get_flattened_data())
        edge_indices = [index for index, value in enumerate(edges) if value > 16 and alphas[index] > 4]
        green_spill = sum(1 for index in edge_indices if pixels[index][1] > max(pixels[index][0], pixels[index][2]) * 1.24)
        dark_halo = sum(1 for index in edge_indices if sum(pixels[index]) / 3 < 24)
        bright_halo = sum(1 for index in edge_indices if sum(pixels[index]) / 3 > 247)
        opaque = [value for value in alphas if value >= 220]
        partial = [value for value in alphas if 8 < value < 220]
        metrics = {
            "edge_pixel_count": len(edge_indices),
            "green_spill_ratio": round(green_spill / max(1, len(edge_indices)), 5),
            "dark_halo_ratio": round(dark_halo / max(1, len(edge_indices)), 5),
            "bright_halo_ratio": round(bright_halo / max(1, len(edge_indices)), 5),
            "partial_alpha_ratio": round(len(partial) / max(1, len(alphas)), 5),
            "opaque_ratio": round(len(opaque) / max(1, len(alphas)), 5),
        }
        issues: list[str] = []
        if len(edge_indices) >= 8 and metrics["green_spill_ratio"] > 0.20:
            issues.append("green_spill")
        if len(edge_indices) >= 8 and metrics["dark_halo_ratio"] > 0.28:
            issues.append("dark_halo")
        if len(edge_indices) >= 8 and metrics["bright_halo_ratio"] > 0.38:
            issues.append("bright_halo")
        # Large soft-alpha areas are suspicious in body/garment regions, but
        # are deliberately tolerated in hair evidence to avoid rejecting fine strands.
        if name != "head_hair" and metrics["partial_alpha_ratio"] > 0.34:
            issues.append("contour_or_internal_transparency")
        if name == "full_body" and metrics["opaque_ratio"] < 0.08:
            issues.append("foreground_missing")
        per_region[name] = {"box": list(box), "metrics": metrics, "issues": issues}
        critical.extend(f"{name}:{issue}" for issue in issues)

    # Corners should remain transparent in an extracted foreground.
    corner = max(2, min(alpha.size) // 16)
    corner_boxes = [(0, 0, corner, corner), (alpha.width - corner, 0, alpha.width, corner), (0, alpha.height - corner, corner, alpha.height), (alpha.width - corner, alpha.height - corner, alpha.width, alpha.height)]
    corner_alpha = [ImageStat.Stat(alpha.crop(box)).mean[0] / 255 for box in corner_boxes]
    if max(corner_alpha, default=0.0) > 0.15:
        critical.append("background_leak:corners")
    topology = _alpha_topology(alpha)
    if topology["alpha_hole_ratio"] > 0.012:
        critical.append("alpha_holes_or_internal_transparency")
    if topology["detached_component_count"] > 2:
        critical.append("contour_fragments")
    if topology["edge_roughness"] > 0.12:
        critical.append("severe_jagged_edge")
    return {
        "schema_version": QA_SCHEMA_VERSION,
        "passed": not critical,
        "critical_errors": sorted(set(critical)),
        "regions": per_region,
        "corner_alpha_ratios": [round(value, 5) for value in corner_alpha],
        "topology": topology,
        "review_note": "机器指标只识别明显技术异常；发丝自然度、融合感和光感仍须独立多模态审核。",
    }


def blurred_background_issues(image: Image.Image) -> list[str]:
    """Detect conspicuous low-frequency rectangular seams without scene-specific coordinates."""
    sample = image.convert("RGB").resize((192, 108), Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(3))
    px = sample.load()
    issues: list[str] = []
    for axis, limit in (("vertical", sample.width), ("horizontal", sample.height)):
        scores: list[float] = []
        for index in range(1, limit):
            diffs = []
            other = sample.height if axis == "vertical" else sample.width
            for value in range(other):
                left = px[index - 1, value] if axis == "vertical" else px[value, index - 1]
                right = px[index, value] if axis == "vertical" else px[value, index]
                diffs.append(sum(abs(a - b) for a, b in zip(left, right)) / 3)
            scores.append(sum(diffs) / max(1, len(diffs)))
        if scores:
            ordered = sorted(scores)
            baseline = ordered[len(ordered) // 2]
            peak = max(scores)
            # Require a long coherent discontinuity far above the image's own
            # normal low-frequency transitions; no absolute colour is assumed.
            if peak >= max(7.0, baseline * 5.0 + 3.0):
                issues.append(f"rectangular_seam:{axis}")
    return issues


def write_evidence_assets(
    standing_frame: Path,
    gesture_frame: Path,
    *,
    chroma_color: str,
    similarity: float,
    blend: float,
    output_dir: Path,
    candidate_id: str = "",
    machine_qa_path: Path | None = None,
    preset: dict[str, Any] | None = None,
    preset_path: Path | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    settings = dict(preset or {})
    settings.setdefault("keyer", "colorkey")
    settings.setdefault("chroma_color", chroma_color)
    settings.setdefault("chroma_similarity", similarity)
    settings.setdefault("chroma_blend", blend)
    settings.setdefault("person_grade", "none")
    settings.setdefault("person_beauty", "none")
    settings.setdefault("person_crop", None)
    preset_sha256 = file_sha256(preset_path) if preset_path is not None else ""
    render_contract = production_keying_contract(settings)
    render_fingerprint = production_keying_fingerprint(settings)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, str]] = []
    qa_by_pose: dict[str, Any] = {}
    candidate_file = output_dir / "selected_candidate.png"
    panels: list[Image.Image] = []
    for pose, source_path in (("standing", standing_frame), ("wide_gesture", gesture_frame)):
        pose_path = output_dir / f"{pose}_foreground.png"
        render_production_keyed_foreground(source_path, pose_path, settings)
        with Image.open(pose_path) as rendered:
            rgba = rendered.convert("RGBA")
        panels.append(rgba.copy())
        qa_by_pose[pose] = analyze_keyed_rgba(rgba)
        artifacts.append({"role": f"{pose}_full_body", "path": str(pose_path), "sha256": file_sha256(pose_path)})
        for region, box in evidence_regions(rgba.getchannel("A")).items():
            region_path = output_dir / f"{pose}_{region}.png"
            rgba.crop(box).save(region_path)
            artifacts.append({"role": f"{pose}_{region}", "path": str(region_path), "sha256": file_sha256(region_path)})
    canvas = Image.new("RGBA", (sum(panel.width for panel in panels), max(panel.height for panel in panels)), (0, 0, 0, 0))
    x = 0
    for panel in panels:
        canvas.paste(panel, (x, 0), panel)
        x += panel.width
    canvas.save(candidate_file)
    manifest_payload = {
        "version": 2,
        "renderer_kind": "production_ffmpeg",
        "filter_version": PRODUCTION_KEYING_FILTER_VERSION,
        "filter_fingerprint": render_fingerprint,
        "filter_contract": render_contract,
        "keying_candidate": candidate_id,
        "preset_path": str(preset_path) if preset_path is not None else "",
        "preset_sha256": preset_sha256,
        "artifacts": artifacts,
    }
    manifest = write_json_atomic(output_dir / "evidence_manifest.json", manifest_payload)
    qa_path = write_json_atomic(
        machine_qa_path or output_dir.parent / "keying_machine_qa.json",
        {
            "schema_version": QA_SCHEMA_VERSION,
            "keying_candidate": candidate_id,
            "renderer_kind": "production_ffmpeg",
            "filter_version": PRODUCTION_KEYING_FILTER_VERSION,
            "filter_fingerprint": render_fingerprint,
            "filter_contract": render_contract,
            "preset_path": str(preset_path) if preset_path is not None else "",
            "preset_sha256": preset_sha256,
            "candidate_file": str(candidate_file),
            "candidate_sha256": file_sha256(candidate_file),
            "evidence_manifest": str(manifest),
            "evidence_manifest_sha256": file_sha256(manifest),
            "poses": qa_by_pose,
            "passed": all(item.get("passed") is True for item in qa_by_pose.values()),
            "critical_errors": sorted({issue for item in qa_by_pose.values() for issue in item.get("critical_errors", [])}),
        },
    )
    return candidate_file, qa_path, json.loads(manifest.read_text(encoding="utf-8"))


def representative_evidence_images(evidence_manifest_path: Path) -> list[Path]:
    """Choose a bounded, deterministic multimodal evidence set."""

    payload = json.loads(evidence_manifest_path.read_text(encoding="utf-8"))
    artifacts = payload.get("artifacts", [])
    by_role = {
        str(item.get("role")): Path(str(item.get("path")))
        for item in artifacts
        if isinstance(item, dict) and item.get("role") and item.get("path")
    }
    priorities = [
        "standing_full_body",
        "wide_gesture_full_body",
        "standing_head_hair",
        "wide_gesture_left_shoulder_forearm_hand",
        "wide_gesture_right_shoulder_forearm_hand",
        "standing_garment_outline",
        "standing_hem_lower_outline",
        "standing_legs_feet",
        "wide_gesture_high_contrast_edges",
    ]
    return [by_role[role] for role in priorities if role in by_role and by_role[role].is_file()]


def keying_review_images(
    candidate_sheet: Path | None,
    evidence_manifest_path: Path,
    preview_images: list[Path],
) -> list[Path]:
    ordered: list[Path] = []
    if candidate_sheet is not None and candidate_sheet.is_file():
        ordered.append(candidate_sheet)
    ordered.extend(representative_evidence_images(evidence_manifest_path))
    ordered.extend(path for path in preview_images if path.is_file())
    result: list[Path] = []
    seen: set[Path] = set()
    for path in ordered:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(path)
    return result


def refresh_keying_quality_from_preset(preset_path: Path) -> tuple[Path, Path]:
    preset = json.loads(preset_path.read_text(encoding="utf-8"))
    search_path = Path(str(preset.get("keying_search") or preset_path.with_name("keying_search.json"))).expanduser()
    if not search_path.is_absolute():
        search_path = (preset_path.parent / search_path).resolve()
    search = json.loads(search_path.read_text(encoding="utf-8"))
    selected = str(preset.get("keying_candidate") or "").strip()
    candidates = {str(item.get("id")): item for item in search.get("candidates", []) if isinstance(item, dict)}
    if selected not in candidates:
        raise ValueError("keying preset 选择的 candidate 不在搜索记录中")
    candidate = candidates[selected]
    evidence_dir = preset_path.parent / "evidence" / selected
    candidate_file, qa_path, _manifest = write_evidence_assets(
        Path(str(search["standing_frame"])),
        Path(str(search["gesture_frame"])),
        chroma_color=str(search["chroma_color"]),
        similarity=float(candidate["similarity"]),
        blend=float(candidate["blend"]),
        output_dir=evidence_dir,
        candidate_id=selected,
        machine_qa_path=preset_path.parent / "keying_machine_qa.json",
        preset=preset,
        preset_path=preset_path,
    )
    search["selected_candidate_file"] = str(candidate_file)
    search["selected_candidate_sha256"] = file_sha256(candidate_file)
    search["machine_qa"] = str(qa_path)
    search["evidence_manifest"] = str(evidence_dir / "evidence_manifest.json")
    write_json_atomic(search_path, search)
    return qa_path, evidence_dir / "evidence_manifest.json"


def lock_keying_preset(
    preset_path: Path,
    *,
    machine_qa_path: Path,
    evidence_manifest_path: Path,
    review_bundle_path: Path,
    review_path: Path,
    output_path: Path | None = None,
) -> Path:
    from story_agent_runtime import review_bundle_is_current, review_passes

    preset = json.loads(preset_path.read_text(encoding="utf-8"))
    qa = json.loads(machine_qa_path.read_text(encoding="utf-8"))
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if qa.get("passed") is not True or qa.get("critical_errors"):
        raise ValueError("keying machine QA 未通过，不能锁定 preset")
    if qa.get("keying_candidate") != preset.get("keying_candidate"):
        raise ValueError("keying machine QA 未绑定当前 candidate")
    expected_fingerprint = production_keying_fingerprint(preset)
    expected_contract = production_keying_contract(preset)
    if qa.get("renderer_kind") != "production_ffmpeg":
        raise ValueError("keying machine QA 不是正式生产 FFmpeg chain 的证据")
    if qa.get("preset_sha256") != file_sha256(preset_path):
        raise ValueError("keying machine QA 未绑定当前 preset")
    if qa.get("filter_fingerprint") != expected_fingerprint:
        raise ValueError("keying machine QA 未绑定当前生产 keying filter")
    if qa.get("filter_contract") != expected_contract:
        raise ValueError("keying machine QA 的生产 filter contract 不一致")
    evidence_payload = json.loads(evidence_manifest_path.read_text(encoding="utf-8"))
    if evidence_payload.get("renderer_kind") != "production_ffmpeg":
        raise ValueError("keying evidence 不是正式生产 FFmpeg chain 的证据")
    if evidence_payload.get("preset_sha256") != file_sha256(preset_path):
        raise ValueError("keying evidence 未绑定当前 preset")
    if evidence_payload.get("filter_fingerprint") != expected_fingerprint:
        raise ValueError("keying evidence 未绑定当前生产 keying filter")
    if evidence_payload.get("filter_contract") != expected_contract:
        raise ValueError("keying evidence 的生产 filter contract 不一致")
    if evidence_payload.get("keying_candidate") != preset.get("keying_candidate"):
        raise ValueError("keying evidence 未绑定当前 candidate")
    if not review_bundle_is_current(review_bundle_path) or not review_passes(review, artifact=review_bundle_path):
        raise ValueError("keying 独立审核未通过或 bundle 已失效")
    search_path = Path(str(preset.get("keying_search") or preset_path.with_name("keying_search.json"))).expanduser()
    if not search_path.is_absolute():
        search_path = (preset_path.parent / search_path).resolve()
    search = json.loads(search_path.read_text(encoding="utf-8"))
    source = Path(str(search.get("source_video") or "")).expanduser()
    candidate = Path(str(qa.get("candidate_file") or "")).expanduser()
    target = output_path or preset_path.with_name("keying_preset.lock.json")
    payload = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "preset_version": str(preset.get("preset_version") or "1"),
        "status": "locked",
        "preset_path": str(preset_path),
        "preset_sha256": file_sha256(preset_path),
        "keying_candidate": preset.get("keying_candidate"),
        "candidate_path": str(candidate),
        "candidate_sha256": file_sha256(candidate),
        "source_video": str(source),
        "source_sha256": file_sha256(source),
        "machine_qa_path": str(machine_qa_path),
        "machine_qa_sha256": file_sha256(machine_qa_path),
        "evidence_manifest_path": str(evidence_manifest_path),
        "evidence_manifest_sha256": file_sha256(evidence_manifest_path),
        "review_bundle_path": str(review_bundle_path),
        "review_bundle_sha256": file_sha256(review_bundle_path),
        "review_path": str(review_path),
        "review_sha256": file_sha256(review_path),
        "renderer_kind": "production_ffmpeg",
        "filter_version": PRODUCTION_KEYING_FILTER_VERSION,
        "filter_fingerprint": expected_fingerprint,
    }
    return write_json_atomic(target, payload)


def keying_preset_lock_issues(preset_path: Path, lock_path: Path | None = None) -> list[str]:
    target = lock_path or preset_path.with_name("keying_preset.lock.json")
    try:
        lock = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["keying_preset_lock_missing_or_invalid"]
    required = {
        "schema_version", "preset_version", "status", "preset_path", "preset_sha256", "candidate_path",
        "candidate_sha256", "source_video", "source_sha256", "machine_qa_path", "machine_qa_sha256",
        "evidence_manifest_path", "evidence_manifest_sha256", "review_bundle_path", "review_bundle_sha256",
        "review_path", "review_sha256",
        "renderer_kind", "filter_version", "filter_fingerprint",
    }
    if required - set(lock):
        return ["keying_preset_lock_fields_missing"]
    if lock.get("schema_version") != LOCK_SCHEMA_VERSION or lock.get("status") != "locked":
        return ["keying_preset_lock_schema_or_status_invalid"]
    issues: list[str] = []
    bindings = {
        "preset": (preset_path, lock.get("preset_sha256")),
        "candidate": (Path(str(lock["candidate_path"])), lock.get("candidate_sha256")),
        "source": (Path(str(lock["source_video"])), lock.get("source_sha256")),
        "machine_qa": (Path(str(lock["machine_qa_path"])), lock.get("machine_qa_sha256")),
        "evidence": (Path(str(lock["evidence_manifest_path"])), lock.get("evidence_manifest_sha256")),
        "review_bundle": (Path(str(lock["review_bundle_path"])), lock.get("review_bundle_sha256")),
        "review": (Path(str(lock["review_path"])), lock.get("review_sha256")),
    }
    for label, (path, expected) in bindings.items():
        if not path.is_file() or file_sha256(path) != expected:
            issues.append(f"keying_preset_lock_binding_mismatch:{label}")
    if issues:
        return issues
    try:
        preset = json.loads(preset_path.read_text(encoding="utf-8"))
        qa = json.loads(Path(str(lock["machine_qa_path"])).read_text(encoding="utf-8"))
        evidence = json.loads(Path(str(lock["evidence_manifest_path"])).read_text(encoding="utf-8"))
        review = json.loads(Path(str(lock["review_path"])).read_text(encoding="utf-8"))
        from story_agent_runtime import review_bundle_is_current, review_passes
        bundle = Path(str(lock["review_bundle_path"]))
        if qa.get("passed") is not True or qa.get("critical_errors"):
            issues.append("keying_machine_qa_not_passed")
        if lock.get("keying_candidate") != preset.get("keying_candidate"):
            issues.append("keying_lock_candidate_not_current")
        if qa.get("keying_candidate") != preset.get("keying_candidate"):
            issues.append("keying_machine_qa_candidate_not_current")
        if qa.get("candidate_sha256") != lock.get("candidate_sha256"):
            issues.append("keying_machine_qa_candidate_binding_mismatch")
        if qa.get("evidence_manifest_sha256") != lock.get("evidence_manifest_sha256"):
            issues.append("keying_machine_qa_evidence_binding_mismatch")
        expected_fingerprint = production_keying_fingerprint(preset)
        expected_contract = production_keying_contract(preset)
        if lock.get("renderer_kind") != "production_ffmpeg" or qa.get("renderer_kind") != "production_ffmpeg":
            issues.append("keying_renderer_not_production_ffmpeg")
        if lock.get("filter_version") != PRODUCTION_KEYING_FILTER_VERSION:
            issues.append("keying_filter_version_stale")
        if lock.get("filter_fingerprint") != expected_fingerprint:
            issues.append("keying_filter_fingerprint_stale")
        if qa.get("filter_version") != PRODUCTION_KEYING_FILTER_VERSION or qa.get("filter_fingerprint") != expected_fingerprint:
            issues.append("keying_machine_qa_filter_stale")
        if qa.get("filter_contract") != expected_contract:
            issues.append("keying_machine_qa_filter_contract_mismatch")
        if qa.get("preset_sha256") != lock.get("preset_sha256"):
            issues.append("keying_machine_qa_preset_binding_mismatch")
        if evidence.get("renderer_kind") != "production_ffmpeg":
            issues.append("keying_evidence_renderer_invalid")
        if evidence.get("filter_version") != PRODUCTION_KEYING_FILTER_VERSION or evidence.get("filter_fingerprint") != expected_fingerprint:
            issues.append("keying_evidence_filter_stale")
        if evidence.get("filter_contract") != expected_contract:
            issues.append("keying_evidence_filter_contract_mismatch")
        if evidence.get("preset_sha256") != lock.get("preset_sha256"):
            issues.append("keying_evidence_preset_binding_mismatch")
        if evidence.get("keying_candidate") != preset.get("keying_candidate"):
            issues.append("keying_evidence_candidate_not_current")
        artifacts = evidence.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            issues.append("keying_evidence_manifest_empty")
        else:
            for item in artifacts:
                if not isinstance(item, dict):
                    issues.append("keying_evidence_manifest_invalid")
                    break
                path = Path(str(item.get("path") or ""))
                if not path.is_file() or file_sha256(path) != item.get("sha256"):
                    issues.append("keying_evidence_artifact_binding_mismatch")
                    break
        if not review_bundle_is_current(bundle) or not review_passes(review, artifact=bundle):
            issues.append("keying_review_not_current_or_passed")
    except (OSError, json.JSONDecodeError):
        issues.append("keying_preset_lock_bound_json_invalid")
    return issues


__all__ = [
    "LOCK_SCHEMA_VERSION", "QA_SCHEMA_VERSION", "analyze_keyed_rgba", "blurred_background_issues",
    "evidence_regions", "file_sha256", "key_to_rgba", "keying_preset_lock_issues", "lock_keying_preset",
    "keying_review_images", "refresh_keying_quality_from_preset", "representative_evidence_images",
    "write_evidence_assets", "write_json_atomic",
]
