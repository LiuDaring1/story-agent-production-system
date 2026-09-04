#!/usr/bin/env python3
"""Shared contract for director-shot static PPT plans and delivered decks."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from shot_storyboard_pipeline import validate_compile_receipt


DELIVERY_RECEIPT_SCHEMA = "story-static-ppt-delivery/v1"
PML_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
DML_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_object(path: Path, label: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"{label}不存在：{resolved}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}不是有效 JSON：{resolved}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label}顶层必须是对象：{resolved}")
    return payload


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _single_line_subtitle(value: str) -> str:
    return "".join(str(value).splitlines()).strip()


def _subtitle_shapes(root: ET.Element) -> list[dict[str, Any]]:
    """Return explicitly named subtitle shapes and their paragraph evidence."""

    namespaces = {"p": PML_NS, "a": DML_NS}
    result: list[dict[str, Any]] = []
    for shape in root.findall(".//p:sp", namespaces):
        properties = shape.find("./p:nvSpPr/p:cNvPr", namespaces)
        name = str(properties.get("name") or "") if properties is not None else ""
        if name != "story-subtitle" and re.fullmatch(r"subtitle-\d+", name) is None:
            continue
        paragraphs = shape.findall("./p:txBody/a:p", namespaces)
        paragraph_texts = [
            "".join(node.text or "" for node in paragraph.findall(".//a:t", namespaces))
            for paragraph in paragraphs
        ]
        result.append(
            {
                "name": name,
                "paragraphs": paragraph_texts,
                "has_explicit_break": shape.find(".//a:br", namespaces) is not None,
                "text": "".join(paragraph_texts),
            }
        )
    return result


def validate_plan(
    director_path: Path,
    plan_path: Path,
    *,
    require_storyboard_manifest: bool = True,
) -> tuple[list[str], list[dict[str, Any]]]:
    director_path = director_path.expanduser().resolve()
    plan_path = plan_path.expanduser().resolve()
    director = load_object(director_path, "导演计划")
    plan = load_object(plan_path, "静态 PPT 计划")
    shots = director.get("shots")
    slides = plan.get("slides")
    if not isinstance(shots, list) or not shots:
        raise ValueError("导演计划缺少非空 shots")
    if not isinstance(slides, list) or not slides:
        raise ValueError("静态 PPT 计划缺少非空 slides")
    if any(not isinstance(row, dict) for row in shots + slides):
        raise ValueError("导演镜头和 PPT 页必须是对象")
    shot_ids = [str(row.get("shot_id") or "") for row in shots]
    if not all(shot_ids) or len(set(shot_ids)) != len(shot_ids):
        raise ValueError("导演计划 shot_id 缺失或重复")
    actual_ids = [str(row.get("shot_id") or "") for row in slides]
    expected_ids = ["TITLE", *shot_ids]
    if actual_ids == [*expected_ids, "MORAL"]:
        expected_ids.append("MORAL")
    if actual_ids != expected_ids:
        raise ValueError(
            "PPT 页 ID 必须严格等于 TITLE + 导演 shot_id + 可选 MORAL；"
            f"expected={expected_ids}, actual={actual_ids}"
        )
    if plan.get("director_plan_sha256") != file_sha256(director_path):
        raise ValueError("静态 PPT 计划未绑定当前导演计划 SHA-256")

    manifest_value = str(plan.get("shot_storyboard_manifest_path") or "").strip()
    if require_storyboard_manifest and not manifest_value:
        raise ValueError("正式静态 PPT 计划缺少封存故事板清单")
    storyboard_entries: dict[str, dict[str, Any]] = {}
    if manifest_value:
        manifest_path = Path(manifest_value).expanduser().resolve()
        manifest = load_object(manifest_path, "逐镜故事板清单")
        if manifest.get("schema_version") != "story-shot-storyboards/v1" or manifest.get("status") != "sealed":
            raise ValueError("静态 PPT 绑定的逐镜故事板清单尚未封存")
        if manifest.get("director_plan_sha256") != file_sha256(director_path):
            raise ValueError("逐镜故事板清单绑定的导演计划已过期")
        if manifest.get("storyboard_bundle_sha256") != plan.get("shot_storyboard_bundle_sha256"):
            raise ValueError("静态 PPT 计划绑定的故事板 bundle 已过期")
        if list(manifest.get("ordered_shot_ids") or []) != shot_ids:
            raise ValueError("逐镜故事板清单与导演镜头顺序不一致")
        storyboard_entries = {
            str(row.get("shot_id") or ""): row
            for row in manifest.get("entries") or []
            if isinstance(row, dict)
        }

    shot_map = {str(row.get("shot_id") or ""): row for row in shots}
    for index, row in enumerate(slides, start=1):
        if int(row.get("slide_index") or index) != index:
            raise ValueError(f"第 {index} 页 slide_index 与顺序不一致")
        poster = Path(str(row.get("poster_path") or "")).expanduser().resolve()
        if not poster.is_file():
            raise ValueError(f"第 {index} 页静态图不存在：{poster}")
        expected_sha = str(row.get("poster_sha256") or "")
        if not expected_sha or expected_sha != file_sha256(poster):
            raise ValueError(f"第 {index} 页静态图 SHA-256 缺失或已过期：{poster}")
        if float(row.get("duration_seconds") or 0) <= 0:
            raise ValueError(f"第 {index} 页时长无效")
        if row.get("shot_id") == "TITLE" and str(row.get("subtitle") or "").strip():
            raise ValueError("TITLE 页不得显示主持人报幕字幕")
        shot_id = str(row.get("shot_id") or "")
        if shot_id in {"TITLE", "MORAL"}:
            continue
        shot = shot_map[shot_id]
        story_text = str(shot.get("story_text") or "")
        subtitle = str(row.get("subtitle") or "")
        if "\n" in subtitle or "\r" in subtitle:
            raise ValueError(f"{shot_id} PPT 字幕必须合并为底部单行，禁止保留换行")
        expected_subtitle = _single_line_subtitle(story_text)
        if subtitle != expected_subtitle:
            raise ValueError(
                f"{shot_id} PPT 单行字幕必须逐字等于完整 story_text 去除换行后的文本"
            )
        if row.get("poster_origin") != "imagegen_shot_illustration":
            raise ValueError(f"{shot_id} PPT 主图必须来自逐镜 ImageGen 故事板")
        if row.get("story_text_sha256") != hashlib.sha256(story_text.encode("utf-8")).hexdigest():
            raise ValueError(f"{shot_id} PPT 主图未绑定完整 story_text")
        if row.get("director_shot_sha256") != _canonical_hash(shot):
            raise ValueError(f"{shot_id} PPT 主图未绑定当前导演镜头设计")
        if not str(row.get("imagegen_prompt_sha256") or ""):
            raise ValueError(f"{shot_id} 缺少 ImageGen 提示词哈希")
        references = row.get("reference_assets")
        if not isinstance(references, list) or not references:
            raise ValueError(f"{shot_id} 缺少角色/道具/环境参考资产绑定")
        entry = storyboard_entries.get(shot_id)
        if entry is None and require_storyboard_manifest:
            raise ValueError(f"{shot_id} 缺少封存故事板条目")
        if entry is not None:
            entry_path = Path(str(entry.get("image_path") or "")).expanduser().resolve()
            if poster != entry_path or expected_sha != str(entry.get("image_sha256") or ""):
                raise ValueError(f"{shot_id} PPT 主图没有消费同一份封存故事板")
    return expected_ids, slides


def validate_pptx(
    path: Path,
    expected_count: int,
    *,
    music_sha256: str,
    expected_durations: list[float],
    expected_subtitles: list[str] | None = None,
    subtitle_mode: str | None = None,
) -> None:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"PPTX 不存在：{path}")
    with zipfile.ZipFile(path) as archive:
        slide_names = sorted(
            (
                name
                for name in archive.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            ),
            key=lambda name: int(re.search(r"slide(\d+)\.xml", name).group(1)),
        )
        if len(slide_names) != expected_count:
            raise ValueError(f"PPTX 页数错误：expected={expected_count}, actual={len(slide_names)}")
        forbidden = [
            name
            for name in archive.namelist()
            if name.lower().endswith((".mp4", ".mov", ".webm", ".gif"))
        ]
        if forbidden:
            raise ValueError(f"静态 PPT 内含视频/GIF：{forbidden}")
        media = [name for name in archive.namelist() if name.startswith("ppt/media/")]
        if not music_sha256 or not any(
            hashlib.sha256(archive.read(name)).hexdigest() == music_sha256 for name in media
        ):
            raise ValueError("PPTX 没有内嵌计划绑定的配乐")
        if len(expected_durations) != len(slide_names):
            raise ValueError("PPTX 自动翻页时长数量与页数不一致")
        if expected_subtitles is not None and len(expected_subtitles) != len(slide_names):
            raise ValueError("PPTX 字幕期望数量与页数不一致")
        if subtitle_mode not in {None, "with", "without"}:
            raise ValueError(f"未知 PPT 字幕模式：{subtitle_mode}")
        for index, (name, seconds) in enumerate(zip(slide_names, expected_durations), start=1):
            root = ET.fromstring(archive.read(name))
            transition = root.find(f"{{{PML_NS}}}transition")
            expected_ms = max(500, int(round(float(seconds) * 1000)))
            actual_ms = int(transition.get("advTm") or 0) if transition is not None else 0
            if transition is None or transition.get("advClick") != "1" or abs(actual_ms - expected_ms) > 1:
                raise ValueError(
                    f"PPTX 第 {index} 页自动翻页错误：expected={expected_ms}, actual={actual_ms}"
                )
            if subtitle_mode is None:
                continue
            shapes = _subtitle_shapes(root)
            expected_text = (
                str(expected_subtitles[index - 1]) if expected_subtitles is not None else ""
            )
            if subtitle_mode == "without":
                if shapes:
                    raise ValueError(f"无字幕 PPT 第 {index} 页残留字幕对象")
                continue
            if not expected_text:
                if shapes:
                    raise ValueError(f"含字幕 PPT 第 {index} 页不应出现字幕对象")
                continue
            if len(shapes) != 1:
                raise ValueError(
                    f"含字幕 PPT 第 {index} 页必须恰好有一个单行字幕对象：actual={len(shapes)}"
                )
            shape = shapes[0]
            if len(shape["paragraphs"]) != 1 or shape["has_explicit_break"]:
                raise ValueError(f"含字幕 PPT 第 {index} 页字幕不是单行")
            if shape["text"] != expected_text:
                raise ValueError(f"含字幕 PPT 第 {index} 页字幕文本与计划不一致")


def validate_pair(
    director_path: Path,
    plan_path: Path,
    with_subtitles: Path,
    without_subtitles: Path,
) -> tuple[list[str], list[dict[str, Any]]]:
    slide_ids, slides = validate_plan(director_path, plan_path)
    plan = load_object(plan_path, "静态 PPT 计划")
    durations = [float(row.get("duration_seconds") or 0) for row in slides]
    subtitles = [
        ""
        if str(row.get("shot_id") or "") in {"TITLE", "MORAL"}
        else str(row.get("subtitle") or "")
        for row in slides
    ]
    music_sha256 = str(plan.get("music_sha256") or "")
    validate_pptx(
        with_subtitles,
        len(slide_ids),
        music_sha256=music_sha256,
        expected_durations=durations,
        expected_subtitles=subtitles,
        subtitle_mode="with",
    )
    validate_pptx(
        without_subtitles,
        len(slide_ids),
        music_sha256=music_sha256,
        expected_durations=durations,
        expected_subtitles=subtitles,
        subtitle_mode="without",
    )
    return slide_ids, slides


def write_delivery_receipt(
    *,
    director_path: Path,
    compile_receipt_path: Path,
    plan_path: Path,
    with_subtitles: Path,
    without_subtitles: Path,
    output_path: Path,
) -> dict[str, Any]:
    director_path = director_path.expanduser().resolve()
    compile_receipt_path = compile_receipt_path.expanduser().resolve()
    plan_path = plan_path.expanduser().resolve()
    with_subtitles = with_subtitles.expanduser().resolve()
    without_subtitles = without_subtitles.expanduser().resolve()
    # Provider execution updates status/result columns in the jobs CSV after the
    # immutable storyboard/PPT compile step.  Delivery must still bind the
    # compile receipt itself, director plan, PPT plan and sealed storyboards,
    # but it must not require the mutable jobs CSV to retain its pre-run hash.
    compile_receipt = validate_compile_receipt(
        compile_receipt_path,
        require_current_r2v_jobs=False,
    )
    if compile_receipt.get("director_plan_sha256") != file_sha256(director_path):
        raise ValueError("编译回执未绑定当前导演计划")
    if compile_receipt.get("ppt_plan_sha256") != file_sha256(plan_path):
        raise ValueError("编译回执未绑定当前静态 PPT 计划")
    slide_ids, _slides = validate_pair(
        director_path, plan_path, with_subtitles, without_subtitles
    )
    plan = load_object(plan_path, "静态 PPT 计划")
    receipt = {
        "schema_version": DELIVERY_RECEIPT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "director_plan_path": str(director_path),
        "director_plan_sha256": file_sha256(director_path),
        "shot_storyboard_compile_receipt_path": str(compile_receipt_path),
        "shot_storyboard_compile_receipt_sha256": file_sha256(compile_receipt_path),
        "ppt_plan_path": str(plan_path),
        "ppt_plan_sha256": file_sha256(plan_path),
        "storyboard_bundle_sha256": str(plan.get("shot_storyboard_bundle_sha256") or ""),
        "slide_ids": slide_ids,
        "director_shot_count": int(compile_receipt.get("shot_count") or 0),
        "total_slide_count": len(slide_ids),
        "with_subtitles_pptx_path": str(with_subtitles),
        "with_subtitles_pptx_sha256": file_sha256(with_subtitles),
        "without_subtitles_pptx_path": str(without_subtitles),
        "without_subtitles_pptx_sha256": file_sha256(without_subtitles),
    }
    output = output_path.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return receipt


def validate_delivery_receipt(path: Path) -> dict[str, Any]:
    receipt = load_object(path, "静态 PPT 交付回执")
    if receipt.get("schema_version") != DELIVERY_RECEIPT_SCHEMA:
        raise ValueError("静态 PPT 交付回执版本不受支持")
    bindings = (
        ("director_plan_path", "director_plan_sha256"),
        ("shot_storyboard_compile_receipt_path", "shot_storyboard_compile_receipt_sha256"),
        ("ppt_plan_path", "ppt_plan_sha256"),
        ("with_subtitles_pptx_path", "with_subtitles_pptx_sha256"),
        ("without_subtitles_pptx_path", "without_subtitles_pptx_sha256"),
    )
    for path_key, hash_key in bindings:
        target = Path(str(receipt.get(path_key) or "")).expanduser().resolve()
        if not target.is_file() or receipt.get(hash_key) != file_sha256(target):
            raise ValueError(f"静态 PPT 交付回执绑定失效：{path_key}")
    compile_receipt = validate_compile_receipt(
        Path(str(receipt["shot_storyboard_compile_receipt_path"])),
        require_current_r2v_jobs=False,
    )
    if compile_receipt.get("director_plan_sha256") != receipt.get("director_plan_sha256"):
        raise ValueError("静态 PPT 交付回执与编译回执绑定了不同导演计划")
    if compile_receipt.get("ppt_plan_sha256") != receipt.get("ppt_plan_sha256"):
        raise ValueError("静态 PPT 交付回执与编译回执不一致")
    if compile_receipt.get("storyboard_bundle_sha256") != receipt.get("storyboard_bundle_sha256"):
        raise ValueError("静态 PPT 交付回执与编译回执绑定了不同故事板 bundle")
    slide_ids, _slides = validate_pair(
        Path(str(receipt["director_plan_path"])),
        Path(str(receipt["ppt_plan_path"])),
        Path(str(receipt["with_subtitles_pptx_path"])),
        Path(str(receipt["without_subtitles_pptx_path"])),
    )
    if slide_ids != receipt.get("slide_ids") or len(slide_ids) != int(
        receipt.get("total_slide_count") or 0
    ):
        raise ValueError("静态 PPT 交付回执页序或页数无效")
    director_count = len(slide_ids) - 1 - int(slide_ids[-1] == "MORAL")
    if director_count != int(receipt.get("director_shot_count") or 0):
        raise ValueError("静态 PPT 交付回执的导演镜头数无效")
    return receipt
