"""Deterministic cover rendering, lineage and machine validation.

The reviewed Story Contract remains the source of truth.  This module only
compiles the minimal cover projection into final pixels and receipts.  AI
image generation supplies text-free creative bases; all customer-facing text
and the official logo are placed here deterministically.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageDraw, ImageFont


COVER_RENDER_VERSION = "cover-render-v1"
RATIO_NAMES = {"3x4": "3:4", "4x3": "4:3", "16x9": "16:9"}
RATIO_VALUES = {"3:4": 3 / 4, "4:3": 4 / 3, "16:9": 16 / 9}
COVER_GRAPH: dict[str, str | None] = {
    "main:4x3": None,
    "main:3x4": "main:4x3",
    "main:16x9": "main:4x3",
    "library:4x3": "main:4x3",
    "library:3x4": "library:4x3",
    "library:16x9": "library:4x3",
}


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def cover_id(account: str, ratio: str) -> str:
    return f"{account}:{ratio}"


def cover_relative(account: str, ratio: str, *, creative: bool = False) -> str:
    name = f"creative_base_{ratio}.png" if creative else f"cover_{ratio}.png"
    return f"{account}/covers/{name}"


def cover_descendants(asset_ids: list[str]) -> list[str]:
    selected = set(asset_ids)
    changed = True
    while changed:
        changed = False
        for child, parent in COVER_GRAPH.items():
            if parent in selected and child not in selected:
                selected.add(child)
                changed = True
    return sorted(selected)


def creative_lineage_issues(publish_dir: Path, lineage_path: Path) -> tuple[list[str], list[str]]:
    """Validate the Agent-produced creative-base edit graph before rendering."""
    try:
        payload = json.loads(lineage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"- 创意底图 lineage 缺失或损坏：{exc}"], [cover_relative(*key.split(":"), creative=True) for key in COVER_GRAPH]
    entries = {str(item.get("asset_id")): item for item in payload.get("covers", []) if isinstance(item, Mapping)}
    issues: list[str] = []
    retries: list[str] = []
    for asset_id, parent_id in COVER_GRAPH.items():
        account, ratio = asset_id.split(":")
        relative = cover_relative(account, ratio, creative=True)
        path = publish_dir / relative
        item = entries.get(asset_id)
        local: list[str] = []
        if not path.is_file() or item is None:
            local.append("创意底图或记录缺失")
        else:
            expected_mode = "root_master" if parent_id is None else ("branch_master" if asset_id == "library:4x3" else "edit_derived")
            if item.get("generation_mode") != expected_mode:
                local.append(f"generation_mode 应为 {expected_mode}")
            if item.get("parent_asset_id") != parent_id:
                local.append("parent_asset_id 错误")
            if item.get("creative_base_sha256") != file_sha256(path):
                local.append("创意底图哈希失效")
            references = item.get("reference_files")
            if not isinstance(references, list) or not references:
                local.append("缺少生成参考记录")
            if parent_id:
                parent_account, parent_ratio = parent_id.split(":")
                parent = publish_dir / cover_relative(parent_account, parent_ratio, creative=True)
                if not parent.is_file() or item.get("parent_sha256") != file_sha256(parent):
                    local.append("父创意底图哈希失效")
        if local:
            issues.append(f"- {asset_id}：{'；'.join(local)}")
            retries.append(relative)
    return issues, expand_retry_files(retries)


def expand_retry_files(retry_files: list[str]) -> list[str]:
    """Expand only lineage descendants of failed cover assets."""
    by_final = {cover_relative(*key.split(":")): key for key in COVER_GRAPH}
    by_base = {cover_relative(*key.split(":"), creative=True): key for key in COVER_GRAPH}
    asset_ids = [mapping[path.replace("\\", "/")] for path in retry_files for mapping in (by_final, by_base) if path.replace("\\", "/") in mapping]
    expanded = set(retry_files)
    for asset_id in cover_descendants(asset_ids):
        account, ratio = asset_id.split(":")
        expanded.add(cover_relative(account, ratio))
        expanded.add(cover_relative(account, ratio, creative=True))
    return sorted(expanded)


def cover_review_payload_issues(payload: Any, expected_assets: list[str]) -> list[str]:
    """Validate required-v1 P0 and per-asset visual review evidence."""
    if not isinstance(payload, Mapping):
        return ["缺少结构化独立审核结果"]
    issues: list[str] = []
    p0 = payload.get("p0_errors")
    if not isinstance(p0, list):
        issues.append("缺少机器可读 p0_errors")
    elif p0:
        issues.append("存在 P0：" + "；".join(str(item) for item in p0))
    evidence = payload.get("evidence_matrix")
    serialized = json.dumps(evidence, ensure_ascii=False) if isinstance(evidence, (list, dict)) else ""
    # Main and library files intentionally share basenames. Require their
    # publish-relative paths so one account cannot impersonate all six assets.
    missing = [asset for asset in expected_assets if asset not in serialized]
    if missing:
        issues.append("六张原图 evidence_matrix 未逐张覆盖：" + "、".join(missing))
    return issues


def _font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ):
        path = Path(candidate)
        if path.is_file():
            return ImageFont.truetype(str(path), size=max(1, size))
    raise FileNotFoundError("封面确定性排版缺少可用中文字体")


def _pixel_box(region: Mapping[str, Any] | None, width: int, height: int, fallback: tuple[float, float, float, float]) -> list[int]:
    values = fallback if not region else tuple(float(region[name]) for name in ("x", "y", "width", "height"))
    return [round(values[0] * width), round(values[1] * height), round(values[2] * width), round(values[3] * height)]


def _contains(outer: list[int], inner: list[int]) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return ix >= ox and iy >= oy and ix + iw <= ox + ow and iy + ih <= oy + oh


def _overlap(left: list[int] | None, right: list[int] | None) -> bool:
    if not left or not right:
        return False
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    return max(lx, rx) < min(lx + lw, rx + rw) and max(ly, ry) < min(ly + lh, ry + rh)


def _fit_text(draw: ImageDraw.ImageDraw, text: str, region: list[int], *, max_lines: int = 2) -> tuple[ImageFont.FreeTypeFont, list[str], list[int]]:
    x, y, width, height = region
    maximum = max(12, round(height * 0.72))
    minimum = max(12, round(height * 0.18))
    candidates = [[text]]
    if max_lines >= 2 and len(text) >= 4:
        candidates.extend([[text[:cut], text[cut:]] for cut in range(1, len(text))])
    for size in range(maximum, minimum - 1, -2):
        font = _font(size)
        for lines in candidates:
            bboxes = [draw.textbbox((0, 0), line, font=font, stroke_width=max(1, size // 28)) for line in lines]
            line_height = max(box[3] - box[1] for box in bboxes)
            total_height = line_height * len(lines) + round(size * 0.16) * (len(lines) - 1)
            max_width = max(box[2] - box[0] for box in bboxes)
            if max_width <= width and total_height <= height:
                return font, lines, [x, y + (height - total_height) // 2, max_width, total_height]
    raise ValueError(f"封面标题无法在合同安全区内可靠排版：{text}")


def _draw_centered_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    region: list[int],
    *,
    fill: tuple[int, int, int],
    stroke_fill: tuple[int, int, int],
) -> list[int]:
    x, y, width, height = region
    stroke = max(1, font.size // 28)
    boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=stroke) for line in lines]
    line_height = max(box[3] - box[1] for box in boxes)
    gap = round(font.size * 0.16)
    total = line_height * len(lines) + gap * (len(lines) - 1)
    top = y + (height - total) // 2
    bounds: list[list[int]] = []
    for index, (line, box) in enumerate(zip(lines, boxes)):
        line_width = box[2] - box[0]
        left = x + (width - line_width) // 2
        baseline_y = top + index * (line_height + gap)
        draw.text((left, baseline_y), line, font=font, fill=fill, stroke_width=stroke, stroke_fill=stroke_fill)
        bounds.append([left, baseline_y, line_width, line_height])
    return [min(item[0] for item in bounds), min(item[1] for item in bounds), max(item[0] + item[2] for item in bounds) - min(item[0] for item in bounds), max(item[1] + item[3] for item in bounds) - min(item[1] for item in bounds)]


def _variant(compiled: Mapping[str, Any], ratio_name: str) -> Mapping[str, Any]:
    candidates = [item for item in compiled.get("variants", []) if isinstance(item, Mapping) and item.get("aspect_ratio") == ratio_name]
    if len(candidates) != 1:
        raise ValueError(f"封面合同必须为 {ratio_name} 提供唯一布局 variant")
    return candidates[0]


def _brand_projection_sha256(compiled: Mapping[str, Any]) -> str:
    return canonical_sha256({
        "official_assets": compiled.get("official_assets", []),
        "brand_rules": compiled.get("brand_rules", []),
    })


def render_required_covers(
    publish_dir: Path,
    *,
    compiled_spec: Mapping[str, Any],
    logo_path: Path,
    story: Mapping[str, Any],
) -> tuple[Path, Path, Path]:
    """Render six final covers and deterministic receipts from creative bases."""
    creative_lineage_path = publish_dir / "cover_creative_lineage.json"
    creative_issues, _ = creative_lineage_issues(publish_dir, creative_lineage_path)
    if creative_issues:
        raise ValueError("；".join(item.removeprefix("- ") for item in creative_issues))
    creative_lineage = json.loads(creative_lineage_path.read_text(encoding="utf-8"))
    creative_entries = {str(item.get("asset_id")): item for item in creative_lineage.get("covers", []) if isinstance(item, Mapping)}
    bindings = {field: str(compiled_spec.get(field) or "") for field in (
        "contract_schema_version", "story_contract_sha256", "story_contract_dependency_sha256",
        "contract_projection_sha256",
    )}
    if not all(bindings.values()):
        raise ValueError("封面合同投影缺少绑定字段")
    spec_sha = canonical_sha256(compiled_spec)
    brand_projection_sha = _brand_projection_sha256(compiled_spec)
    logo_sha = file_sha256(logo_path)
    official = [item for item in compiled_spec.get("official_assets", []) if isinstance(item, Mapping)]
    if not any(item.get("sha256") == logo_sha and int(item.get("max_per_frame", 0)) == 1 for item in official):
        raise ValueError("官方 Logo 与已审核封面品牌投影不匹配，或 max_per_frame 不是 1")
    title = str(story.get("name") or "").strip()
    if not title:
        raise ValueError("封面确定性排版缺少故事标题")
    secondary = " · ".join(item for item in (
        str(story.get("story_type") or "").strip(),
        str(story.get("duration_text") or "").strip(),
        str(story.get("age_range") or "").strip(),
    ) if item)
    usage = "背景视频 · PPT · 配乐 · 文稿 · 朗读标注 · 示范视频"
    with Image.open(logo_path) as source:
        logo = source.convert("RGBA")
        bbox = logo.getbbox()
        if bbox is None:
            raise ValueError("官方 Logo 全透明")
        logo = logo.crop(bbox)

    render_records: dict[str, Any] = {}
    lineage_entries: list[dict[str, Any]] = []
    for asset_id, parent_id in COVER_GRAPH.items():
        account, ratio = asset_id.split(":")
        ratio_name = RATIO_NAMES[ratio]
        base_path = publish_dir / cover_relative(account, ratio, creative=True)
        output_path = publish_dir / cover_relative(account, ratio)
        if not base_path.is_file():
            raise FileNotFoundError(f"required_v1 封面缺少无字创意底图：{base_path}")
        with Image.open(base_path) as source:
            canvas = source.convert("RGBA")
        if abs(canvas.width / max(1, canvas.height) - RATIO_VALUES[ratio_name]) > 0.015:
            raise ValueError(f"封面创意底图比例错误：{base_path.name}")
        variant = _variant(compiled_spec, ratio_name)
        regions = {str(item.get("role")): item for item in variant.get("regions", []) if isinstance(item, Mapping) and item.get("role")}
        if not (regions.get("title_safe") or regions.get("title")):
            raise ValueError(f"封面合同 {ratio_name} 缺少标题安全区")
        if not (regions.get("logo") or regions.get("brand_logo")):
            raise ValueError(f"封面合同 {ratio_name} 缺少官方 Logo 安全区")
        title_safe = _pixel_box(regions.get("title_safe") or regions.get("title"), canvas.width, canvas.height, (.16, .08, .68, .24))
        secondary_safe = _pixel_box(regions.get("secondary_info") or regions.get("metadata"), canvas.width, canvas.height, (.20, .32, .60, .09))
        logo_safe = _pixel_box(regions.get("logo") or regions.get("brand_logo"), canvas.width, canvas.height, (.38, .015, .24, .07))
        usage_safe = _pixel_box(regions.get("usage_info") or regions.get("footer"), canvas.width, canvas.height, (.08, .88, .84, .08))
        person_safe = _pixel_box(regions.get("person") or regions.get("host"), canvas.width, canvas.height, (0, 0, 0, 0)) if (regions.get("person") or regions.get("host")) else None
        story_safe = _pixel_box(regions.get("story_character") or regions.get("story_media"), canvas.width, canvas.height, (0, 0, 0, 0)) if (regions.get("story_character") or regions.get("story_media")) else None
        for label, box in (("title", title_safe), ("secondary", secondary_safe), ("logo", logo_safe), ("usage", usage_safe)):
            if not _contains([0, 0, canvas.width, canvas.height], box):
                raise ValueError(f"封面 {asset_id} 的 {label} 安全区越界")
        if any(_overlap(title_safe, protected) for protected in (person_safe, story_safe) if protected):
            raise ValueError(f"封面 {asset_id} 的标题安全区与受保护主体区域冲突")

        draw = ImageDraw.Draw(canvas)
        title_font, title_lines, _ = _fit_text(draw, title, title_safe, max_lines=2)
        title_bbox = _draw_centered_lines(draw, title_lines, title_font, title_safe, fill=(255, 246, 214), stroke_fill=(142, 65, 16))
        secondary_bbox = None
        if secondary:
            secondary_font, secondary_lines, _ = _fit_text(draw, secondary, secondary_safe, max_lines=1)
            secondary_bbox = _draw_centered_lines(draw, secondary_lines, secondary_font, secondary_safe, fill=(255, 255, 255), stroke_fill=(91, 74, 34))
        usage_font, usage_lines, _ = _fit_text(draw, usage, usage_safe, max_lines=1)
        usage_bbox = _draw_centered_lines(draw, usage_lines, usage_font, usage_safe, fill=(255, 255, 255), stroke_fill=(70, 93, 31))
        logo_scale = min(logo_safe[2] / logo.width, logo_safe[3] / logo.height)
        rendered_logo = logo.resize((max(1, round(logo.width * logo_scale)), max(1, round(logo.height * logo_scale))), Image.Resampling.LANCZOS)
        logo_x = logo_safe[0] + (logo_safe[2] - rendered_logo.width) // 2
        logo_y = logo_safe[1] + (logo_safe[3] - rendered_logo.height) // 2
        logo_bbox = [logo_x, logo_y, rendered_logo.width, rendered_logo.height]
        occupied = [title_bbox, secondary_bbox, usage_bbox, logo_bbox]
        if any(_overlap(left, right) for index, left in enumerate(occupied) if left for right in occupied[index + 1:] if right):
            raise ValueError(f"封面 {asset_id} 的确定性文字或 Logo 发生碰撞")
        if any(_overlap(item, protected) for item in occupied if item for protected in (person_safe, story_safe) if protected):
            raise ValueError(f"封面 {asset_id} 的确定性信息与受保护主体区域冲突")
        canvas.alpha_composite(rendered_logo, (logo_x, logo_y))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_name(f".{output_path.name}.tmp.png")
        canvas.convert("RGB").save(temporary, format="PNG", optimize=True)
        os.replace(temporary, output_path)
        parent_base = publish_dir / cover_relative(*parent_id.split(":"), creative=True) if parent_id else None
        record = {
            "asset_id": asset_id,
            "account": account,
            "ratio": ratio_name,
            "canvas": [canvas.width, canvas.height],
            "creative_base_path": str(base_path),
            "creative_base_sha256": file_sha256(base_path),
            "output_path": str(output_path),
            "output_sha256": file_sha256(output_path),
            "title_text": title,
            "title_bbox": title_bbox,
            "title_safe_region": title_safe,
            "secondary_text": secondary,
            "secondary_bbox": secondary_bbox,
            "secondary_safe_region": secondary_safe,
            "usage_text": usage,
            "usage_bbox": usage_bbox,
            "usage_safe_region": usage_safe,
            "logo_bbox": logo_bbox,
            "logo_safe_region": logo_safe,
            "logo_sha256": logo_sha,
            "official_logo_count": 1,
            "protected_regions": {key: value for key, value in (("presenter", person_safe), ("story_character", story_safe)) if value},
            "margins": {
                "title": [title_bbox[0], title_bbox[1], canvas.width - title_bbox[0] - title_bbox[2], canvas.height - title_bbox[1] - title_bbox[3]],
                "logo": [logo_bbox[0], logo_bbox[1], canvas.width - logo_bbox[0] - logo_bbox[2], canvas.height - logo_bbox[1] - logo_bbox[3]],
            },
            "layout_variant_id": variant.get("variant_id"),
            "render_version": COVER_RENDER_VERSION,
            "compiled_cover_spec_sha256": spec_sha,
            "brand_projection_sha256": brand_projection_sha,
            **bindings,
        }
        render_records[asset_id] = record
        lineage_entries.append({
            "asset_id": asset_id,
            "account": account,
            "ratio": ratio_name,
            "path": cover_relative(account, ratio),
            "creative_base_path": cover_relative(account, ratio, creative=True),
            "generation_mode": "root_master" if parent_id is None else ("branch_master" if asset_id == "library:4x3" else "edit_derived"),
            "parent_asset_id": parent_id,
            "parent_sha256": file_sha256(parent_base) if parent_base else None,
            "root_asset_id": "main:4x3",
            "root_sha256": file_sha256(publish_dir / cover_relative("main", "4x3", creative=True)),
            "creative_base_sha256": file_sha256(base_path),
            "output_sha256": file_sha256(output_path),
            "compiled_cover_spec_sha256": spec_sha,
            "brand_projection_sha256": brand_projection_sha,
            "reference_files": list(creative_entries[asset_id].get("reference_files", [])),
            **bindings,
        })

    lineage_payload = {
        "version": 2,
        "render_version": COVER_RENDER_VERSION,
        "brand_projection_sha256": brand_projection_sha,
        "covers": lineage_entries,
        **bindings,
    }
    lineage_path = write_json_atomic(publish_dir / "cover_lineage.json", lineage_payload)
    render_payload = {
        "version": 2,
        "consumer": "cover",
        "render_version": COVER_RENDER_VERSION,
        "compiled_cover_spec_sha256": spec_sha,
        "brand_projection_sha256": brand_projection_sha,
        "logo_sha256": logo_sha,
        "lineage_sha256": canonical_sha256(lineage_payload),
        "covers": render_records,
        **bindings,
    }
    render_path = write_json_atomic(publish_dir / "cover_render_manifest.json", render_payload)
    receipt_path = write_json_atomic(publish_dir.parent / "99_项目状态" / "publish_cover_branding.json", render_payload)
    return receipt_path, render_path, lineage_path


def required_cover_issues(
    publish_dir: Path,
    *,
    render_manifest_path: Path,
    lineage_path: Path,
    compiled_spec: Mapping[str, Any],
    expected_title: str,
) -> tuple[list[str], list[str]]:
    """Validate final covers, deterministic geometry and lineage bindings."""
    issues: list[str] = []
    retries: list[str] = []
    try:
        render = json.loads(render_manifest_path.read_text(encoding="utf-8"))
        lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"- 封面确定性 manifest 损坏或缺失：{exc}"], [cover_relative(*key.split(":")) for key in COVER_GRAPH]
    bindings = {field: str(compiled_spec.get(field) or "") for field in (
        "contract_schema_version", "story_contract_sha256", "story_contract_dependency_sha256",
        "contract_projection_sha256",
    )}
    spec_sha = canonical_sha256(compiled_spec)
    brand_projection_sha = _brand_projection_sha256(compiled_spec)
    if render.get("render_version") != COVER_RENDER_VERSION or render.get("compiled_cover_spec_sha256") != spec_sha:
        issues.append("- 封面 render manifest 的生产版本或合同投影已失效")
        retries.extend(cover_relative(*key.split(":")) for key in COVER_GRAPH)
    if render.get("brand_projection_sha256") != brand_projection_sha or lineage.get("brand_projection_sha256") != brand_projection_sha:
        issues.append("- 封面品牌投影绑定已失效")
        retries.extend(cover_relative(*key.split(":")) for key in COVER_GRAPH)
    if any(render.get(field) != value for field, value in bindings.items()):
        issues.append("- 封面 render manifest 的 Story Contract 绑定已失效")
        retries.extend(cover_relative(*key.split(":")) for key in COVER_GRAPH)
    if lineage.get("render_version") != COVER_RENDER_VERSION or any(lineage.get(field) != value for field, value in bindings.items()):
        issues.append("- 封面 lineage 的合同或渲染版本绑定已失效")
        retries.extend(cover_relative(*key.split(":")) for key in COVER_GRAPH)
    if render.get("lineage_sha256") != canonical_sha256(lineage):
        issues.append("- 封面 render manifest 与 lineage 内容哈希不一致")
        retries.extend(cover_relative(*key.split(":")) for key in COVER_GRAPH)
    entries = {str(item.get("asset_id")): item for item in lineage.get("covers", []) if isinstance(item, Mapping)}
    records = render.get("covers", {}) if isinstance(render.get("covers"), Mapping) else {}
    for asset_id, parent_id in COVER_GRAPH.items():
        account, ratio = asset_id.split(":")
        relative = cover_relative(account, ratio)
        output = publish_dir / relative
        base = publish_dir / cover_relative(account, ratio, creative=True)
        entry = entries.get(asset_id)
        record = records.get(asset_id) if isinstance(records.get(asset_id), Mapping) else None
        local: list[str] = []
        if not output.is_file() or not base.is_file() or entry is None or record is None:
            local.append("产物、创意底图、lineage 或 geometry receipt 缺失")
        else:
            output_sha = file_sha256(output)
            base_sha = file_sha256(base)
            if entry.get("output_sha256") != output_sha or record.get("output_sha256") != output_sha:
                local.append("最终图像哈希失效")
            if entry.get("creative_base_sha256") != base_sha or record.get("creative_base_sha256") != base_sha:
                local.append("创意底图哈希失效")
            if entry.get("parent_asset_id") != parent_id:
                local.append("父子 lineage 错误")
            expected_root = publish_dir / cover_relative("main", "4x3", creative=True)
            if entry.get("root_asset_id") != "main:4x3" or not expected_root.is_file() or entry.get("root_sha256") != file_sha256(expected_root):
                local.append("根母版绑定失效")
            if parent_id:
                parent_account, parent_ratio = parent_id.split(":")
                parent_base = publish_dir / cover_relative(parent_account, parent_ratio, creative=True)
                if not parent_base.is_file() or entry.get("parent_sha256") != file_sha256(parent_base):
                    local.append("父资产哈希失效")
            if record.get("title_text") != expected_title:
                local.append("确定性标题不准确")
            if record.get("brand_projection_sha256") != brand_projection_sha or entry.get("brand_projection_sha256") != brand_projection_sha:
                local.append("品牌投影指纹失效")
            if record.get("official_logo_count") != 1:
                local.append("官方 Logo 数量不是 1")
            canvas = record.get("canvas")
            if not isinstance(canvas, list) or len(canvas) != 2:
                local.append("画布 geometry 缺失")
            else:
                try:
                    with Image.open(output) as actual:
                        actual_size = [actual.width, actual.height]
                except OSError:
                    actual_size = []
                if actual_size != canvas:
                    local.append("画布 geometry 与最终图像不一致")
                whole = [0, 0, int(canvas[0]), int(canvas[1])]
                for name in ("title", "secondary", "usage", "logo"):
                    bbox = record.get(f"{name}_bbox")
                    safe = record.get(f"{name}_safe_region")
                    if name == "secondary" and not record.get("secondary_text"):
                        continue
                    if not isinstance(bbox, list) or not isinstance(safe, list) or not _contains(whole, bbox) or not _contains(safe, bbox):
                        local.append(f"{name} 几何不在安全区")
                for protected in (record.get("protected_regions") or {}).values():
                    if any(_overlap(record.get(f"{name}_bbox"), protected) for name in ("title", "secondary", "usage", "logo")):
                        local.append("确定性信息与受保护主体区域碰撞")
                visible = [record.get(f"{name}_bbox") for name in ("title", "secondary", "usage", "logo") if record.get(f"{name}_bbox")]
                if any(_overlap(left, right) for index, left in enumerate(visible) for right in visible[index + 1:]):
                    local.append("确定性文字或 Logo 相互碰撞")
        if local:
            issues.append(f"- {asset_id}：{'；'.join(local)}")
            retries.append(relative)
    return issues, expand_retry_files(retries)
