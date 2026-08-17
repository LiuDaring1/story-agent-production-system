from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterable

from docx import Document
from lxml import etree
from PIL import Image

from artifact_semantic_plan import plan_binding, selected_line_indices
from product_text_projection import (
    PUBLIC_TEXT_TRANSFORM_VERSION,
    compile_public_story_lines,
    public_line_list_sha256,
)


PRODUCT_CONTENT_SCHEMA_VERSION = "story-product-content/v2"
PRODUCT_CONTENT_COMPILER_VERSION = "2.0.0"
PRODUCT_TIMING_COMPILER_VERSION = "story-product-timing/v1"
PPT_RENDER_MANIFEST_VERSION = "story-ppt-render/v1"
ANNOTATION_RECEIPT_VERSION = "story-reading-annotation/v1"
PRODUCT_PACKAGE_MANIFEST_VERSION = "story-product-package/v1"

CONTRACT_BINDING_FIELDS = (
    "story_contract_sha256",
    "contract_schema_version",
    "story_contract_dependency_sha256",
)
SEMANTIC_BINDING_FIELDS = (
    "artifact_semantic_plan_sha256",
    "artifact_semantic_plan_schema_version",
    "artifact_semantic_plan_dependency_sha256",
    "contract_projection_sha256",
)

FORBIDDEN_PUBLIC_TOKENS = (
    "我是绵羊姐姐",
    "____",
    "Codex",
    "Agent",
    "QA汇总",
    "handoff",
    "交接",
    "```",
    "**",
)
INTERNAL_FILE_TOKENS = (
    ".DS_Store",
    "_demo_work",
    "成本报告",
    "QA汇总",
    "异常说明",
    "codex",
    "handoff",
    ".log",
    ".tmp",
)

PRESENTER_IDENTITY_PATTERN = re.compile(
    r"(?:^|[\n。！？])\s*(?:大家好[,，！!]?\s*)?(?:我是|我叫)"
    r"[^\n。！？]{0,16}(?:姐姐|哥哥|老师|主持人)(?:[\n。！？]|$)"
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _normalise_public(text: str) -> str:
    return re.sub(r"\s+", "", text).strip()


def _public_text_issues(text: str, *, prefix: str) -> list[str]:
    issues: list[str] = []
    lowered = text.lower()
    for token in FORBIDDEN_PUBLIC_TOKENS:
        if token.lower() in lowered:
            issues.append(f"{prefix}_forbidden_token:{token}")
    if PRESENTER_IDENTITY_PATTERN.search(text):
        issues.append(f"{prefix}_presenter_identity")
    if re.search(r"(?:/Users/|/Volumes/|[A-Za-z]:\\\\)", text):
        issues.append(f"{prefix}_absolute_path_leak")
    if re.search(r"\[[^\]]+\]\([^\)]+\)", text):
        issues.append(f"{prefix}_markdown_link_leak")
    return issues


def _read_semantic_source(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _load_content_selection(content_manifest: Path, artifact: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(content_manifest.read_text(encoding="utf-8"))
    selection = payload.get("selections", {}).get(artifact)
    if not isinstance(selection, dict) or not isinstance(selection.get("rows"), list):
        raise ValueError(f"missing product content selection: {artifact}")
    return payload, selection["rows"]


def extract_docx_paragraphs(path: Path) -> list[str]:
    return [paragraph.text.strip() for paragraph in Document(path).paragraphs if paragraph.text.strip()]


def compile_product_content_manifest(
    *,
    semantic_plan_path: Path,
    semantic_plan: dict[str, Any],
    source_script: Path,
    source_lines: list[str],
    raw_source_lines: list[str],
    selections: dict[str, list[int]],
    images: list[Path],
    timings: list[Any],
    timings_source: Path,
) -> dict[str, Any]:
    if not timings_source.is_file():
        raise ValueError("required_v1 product content requires a formal timings source")
    public_lines = compile_public_story_lines(raw_source_lines)
    if source_lines != public_lines:
        raise ValueError("public source lines do not match the deterministic public-text projection")
    try:
        timings_payload = json.loads(timings_source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("formal timings source must be valid JSON") from exc
    if not isinstance(timings_payload, list):
        raise ValueError("formal timings source must contain a JSON list")
    binding: dict[str, Any] = {
        key: semantic_plan.get(key)
        for key in (*CONTRACT_BINDING_FIELDS, "contract_projection_sha256")
    }
    binding.update(plan_binding(semantic_plan_path))
    selection_payload: dict[str, Any] = {}
    for artifact, indices in selections.items():
        rows: list[dict[str, Any]] = []
        for index in indices:
            row: dict[str, Any] = {
                "source_line_index": index,
                "text": source_lines[index],
                "text_sha256": stable_sha256(source_lines[index]),
            }
            if artifact == "ppt":
                row["image_path"] = str(images[index])
                row["image_sha256"] = file_sha256(images[index])
                timing = timings[index]
                row["timing"] = {
                    "start": float(timing.source_start),
                    "end": float(timing.source_end),
                    "duration": float(timing.duration),
                }
            rows.append(row)
        selection_payload[artifact] = {
            "source_line_indices": list(indices),
            "rows": rows,
            "selection_sha256": stable_sha256(rows),
        }
    payload = {
        "schema_version": PRODUCT_CONTENT_SCHEMA_VERSION,
        "compiler_version": PRODUCT_CONTENT_COMPILER_VERSION,
        **binding,
        "semantic_plan_path": str(semantic_plan_path),
        "semantic_source": {
            "path": str(source_script),
            "sha256": file_sha256(source_script),
            "line_count": len(raw_source_lines),
        },
        "public_text_projection": {
            "transform_version": PUBLIC_TEXT_TRANSFORM_VERSION,
            "line_count": len(public_lines),
            "normalized_line_list_sha256": public_line_list_sha256(public_lines),
        },
        "timings_source": {
            "path": str(timings_source),
            "sha256": file_sha256(timings_source),
            "row_count": len(timings_payload),
            "normalized_rows_sha256": stable_sha256(timings_payload),
            "compiler_version": PRODUCT_TIMING_COMPILER_VERSION,
        },
        "selections": selection_payload,
    }
    payload["compiled_payload_sha256"] = stable_sha256(payload)
    return payload


def product_content_manifest_issues(
    manifest_path: Path,
    *,
    semantic_plan_path: Path | None = None,
    source_script: Path | None = None,
) -> list[str]:
    if not manifest_path.is_file():
        return ["product_content_manifest_missing"]
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["product_content_manifest_invalid_json"]
    issues: list[str] = []
    if payload.get("schema_version") != PRODUCT_CONTENT_SCHEMA_VERSION:
        issues.append("product_content_schema_version_mismatch")
    if payload.get("compiler_version") != PRODUCT_CONTENT_COMPILER_VERSION:
        issues.append("product_content_compiler_version_mismatch")
    stored_hash = payload.pop("compiled_payload_sha256", None)
    if stored_hash != stable_sha256(payload):
        issues.append("product_content_manifest_tampered")
    payload["compiled_payload_sha256"] = stored_hash
    for field in (*CONTRACT_BINDING_FIELDS, *SEMANTIC_BINDING_FIELDS):
        if not isinstance(payload.get(field), str) or not payload[field]:
            issues.append(f"product_content_binding_missing:{field}")
    plan = semantic_plan_path or Path(str(payload.get("semantic_plan_path") or ""))
    if not plan.is_file() or payload.get("artifact_semantic_plan_sha256") != file_sha256(plan):
        issues.append("product_content_semantic_plan_stale")
    source = source_script or Path(str(payload.get("semantic_source", {}).get("path") or ""))
    if not source.is_file() or payload.get("semantic_source", {}).get("sha256") != file_sha256(source):
        issues.append("product_content_semantic_source_stale")
        source_lines: list[str] = []
    else:
        raw_source_lines = _read_semantic_source(source)
        if payload.get("semantic_source", {}).get("line_count") != len(raw_source_lines):
            issues.append("product_content_semantic_source_line_count_mismatch")
        source_lines = compile_public_story_lines(raw_source_lines)
        projection = payload.get("public_text_projection", {})
        if projection.get("transform_version") != PUBLIC_TEXT_TRANSFORM_VERSION:
            issues.append("product_content_public_transform_version_stale")
        if projection.get("line_count") != len(source_lines):
            issues.append("product_content_public_line_count_mismatch")
        if projection.get("normalized_line_list_sha256") != public_line_list_sha256(source_lines):
            issues.append("product_content_public_projection_stale")
    timing_binding = payload.get("timings_source", {})
    timing_path = Path(str(timing_binding.get("path") or "")) if isinstance(timing_binding, dict) else Path()
    if not timing_path.is_file() or timing_binding.get("sha256") != file_sha256(timing_path):
        issues.append("product_content_timings_source_stale")
    else:
        try:
            timing_rows = json.loads(timing_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            timing_rows = None
        if not isinstance(timing_rows, list):
            issues.append("product_content_timings_source_invalid")
        else:
            if timing_binding.get("compiler_version") != PRODUCT_TIMING_COMPILER_VERSION:
                issues.append("product_content_timing_compiler_version_stale")
            if timing_binding.get("row_count") != len(timing_rows):
                issues.append("product_content_timing_row_count_stale")
            if timing_binding.get("normalized_rows_sha256") != stable_sha256(timing_rows):
                issues.append("product_content_timing_rows_stale")
    try:
        plan_payload = json.loads(plan.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        plan_payload = {}
    if plan_payload:
        try:
            current_binding = plan_binding(plan, plan_payload)
        except (KeyError, TypeError, ValueError):
            issues.append("product_content_semantic_plan_binding_invalid")
        else:
            for field, expected in current_binding.items():
                if payload.get(field) != expected:
                    issues.append(f"product_content_semantic_plan_binding_stale:{field}")
        if payload.get("contract_projection_sha256") != plan_payload.get("contract_projection_sha256"):
            issues.append("product_content_contract_projection_stale")
    for artifact in ("ppt", "customer_manuscript", "reading_annotation", "demo_subtitles"):
        selection = payload.get("selections", {}).get(artifact)
        if not isinstance(selection, dict) or not isinstance(selection.get("source_line_indices"), list):
            issues.append(f"product_content_selection_missing:{artifact}")
            continue
        if selection.get("selection_sha256") != stable_sha256(selection.get("rows", [])):
            issues.append(f"product_content_selection_tampered:{artifact}")
        rows = selection.get("rows", [])
        if source_lines:
            try:
                expected_indices = selected_line_indices(source_lines, plan_payload, artifact)
            except (KeyError, TypeError, ValueError):
                issues.append(f"product_content_selection_recompile_failed:{artifact}")
            else:
                if selection.get("source_line_indices") != expected_indices:
                    issues.append(f"product_content_selection_indices_stale:{artifact}")
                expected_rows = [(index, source_lines[index]) for index in expected_indices]
                actual_rows = [
                    (row.get("source_line_index"), str(row.get("text") or ""))
                    for row in rows if isinstance(row, dict)
                ]
                if actual_rows != expected_rows:
                    issues.append(f"product_content_selection_text_stale:{artifact}")
        if artifact == "ppt":
            for row in rows:
                image = Path(str(row.get("image_path") or ""))
                if not image.is_file() or row.get("image_sha256") != file_sha256(image):
                    issues.append(f"product_content_ppt_image_stale:{row.get('source_line_index')}")
    return issues


def write_manuscript_receipt(
    output: Path,
    *,
    manuscript: Path,
    story_name: str,
    selected_indices: list[int],
    selected_lines: list[str],
    content_manifest: Path,
) -> dict[str, Any]:
    paragraphs = extract_docx_paragraphs(manuscript)
    expected = [f"《{story_name}》", *selected_lines]
    payload = {
        "schema_version": "story-customer-manuscript/v1",
        "story_name": story_name,
        "product_content_manifest_path": str(content_manifest),
        "product_content_manifest_sha256": file_sha256(content_manifest),
        "manuscript_path": str(manuscript),
        "manuscript_sha256": file_sha256(manuscript),
        "source_line_indices": selected_indices,
        "selected_text_sha256": stable_sha256(selected_lines),
        "extracted_paragraphs": paragraphs,
        "extracted_text_sha256": stable_sha256(paragraphs),
        "expected_paragraphs_sha256": stable_sha256(expected),
    }
    atomic_write_json(output, payload)
    return payload


def manuscript_receipt_issues(receipt_path: Path) -> list[str]:
    if not receipt_path.is_file():
        return ["manuscript_receipt_missing"]
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        manuscript = Path(payload["manuscript_path"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return ["manuscript_receipt_invalid"]
    issues: list[str] = []
    content_manifest = Path(str(payload.get("product_content_manifest_path") or ""))
    if not content_manifest.is_file() or payload.get("product_content_manifest_sha256") != file_sha256(content_manifest):
        issues.append("manuscript_content_manifest_stale")
    if not manuscript.is_file() or payload.get("manuscript_sha256") != file_sha256(manuscript):
        return ["manuscript_stale"]
    paragraphs = extract_docx_paragraphs(manuscript)
    if stable_sha256(paragraphs) != payload.get("extracted_text_sha256"):
        issues.append("manuscript_extracted_text_stale")
    if payload.get("extracted_text_sha256") != payload.get("expected_paragraphs_sha256"):
        issues.append("manuscript_content_mismatch")
    try:
        _content, rows = _load_content_selection(content_manifest, "customer_manuscript")
        selected_indices = [row["source_line_index"] for row in rows]
        selected_lines = [str(row["text"]) for row in rows]
        expected = [f"《{payload.get('story_name', '')}》", *selected_lines]
        if payload.get("source_line_indices") != selected_indices:
            issues.append("manuscript_source_indices_stale")
        if payload.get("selected_text_sha256") != stable_sha256(selected_lines):
            issues.append("manuscript_selected_text_stale")
        if paragraphs != expected:
            issues.append("manuscript_current_selection_mismatch")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        issues.append("manuscript_content_selection_invalid")
    joined = "\n".join(paragraphs)
    issues.extend(_public_text_issues(joined, prefix="manuscript"))
    return issues


def write_annotation_receipt(
    output: Path,
    *,
    annotation_json: Path,
    annotation_docx: Path,
    blocks: list[dict[str, Any]],
    selected_indices: list[int],
    selected_lines: list[str],
    content_manifest: Path,
) -> dict[str, Any]:
    payload = {
        "schema_version": ANNOTATION_RECEIPT_VERSION,
        "product_content_manifest_path": str(content_manifest),
        "product_content_manifest_sha256": file_sha256(content_manifest),
        "annotation_json_path": str(annotation_json),
        "annotation_json_sha256": file_sha256(annotation_json),
        "annotation_docx_path": str(annotation_docx),
        "annotation_docx_sha256": file_sha256(annotation_docx),
        "source_line_indices": selected_indices,
        "selected_source_sha256": stable_sha256(selected_lines),
        "block_coverage_sha256": stable_sha256(blocks),
    }
    atomic_write_json(output, payload)
    return payload


def annotation_receipt_issues(receipt_path: Path) -> list[str]:
    if not receipt_path.is_file():
        return ["annotation_receipt_missing"]
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["annotation_receipt_invalid"]
    issues: list[str] = []
    content_manifest = Path(str(payload.get("product_content_manifest_path") or ""))
    if not content_manifest.is_file() or payload.get("product_content_manifest_sha256") != file_sha256(content_manifest):
        issues.append("annotation_content_manifest_stale")
    for key in ("annotation_json", "annotation_docx"):
        path = Path(str(payload.get(f"{key}_path") or ""))
        if not path.is_file() or payload.get(f"{key}_sha256") != file_sha256(path):
            issues.append(f"{key}_stale")
    if payload.get("schema_version") != ANNOTATION_RECEIPT_VERSION:
        issues.append("annotation_receipt_version_mismatch")
    try:
        annotation_path = Path(str(payload.get("annotation_json_path") or ""))
        annotation_payload = json.loads(annotation_path.read_text(encoding="utf-8"))
        blocks = annotation_payload.get("blocks", []) if isinstance(annotation_payload, dict) else annotation_payload
        _content, rows = _load_content_selection(content_manifest, "reading_annotation")
        selected_indices = [row["source_line_index"] for row in rows]
        selected_lines = [str(row["text"]) for row in rows]
        if payload.get("source_line_indices") != selected_indices:
            issues.append("annotation_source_indices_stale")
        if payload.get("selected_source_sha256") != stable_sha256(selected_lines):
            issues.append("annotation_selected_source_stale")
        if payload.get("block_coverage_sha256") != stable_sha256(blocks):
            issues.append("annotation_block_coverage_stale")
        line_by_index = dict(zip(selected_indices, selected_lines))
        flattened: list[int] = []
        for block in blocks:
            if not isinstance(block, dict):
                issues.append("annotation_block_invalid")
                continue
            indices = block.get("source_line_indices")
            if not isinstance(indices, list) or not indices or any(type(value) is not int for value in indices):
                issues.append("annotation_block_indices_invalid")
                continue
            flattened.extend(indices)
            if any(index not in line_by_index for index in indices):
                issues.append("annotation_block_unknown_source")
                continue
            expected_text = "".join(line_by_index[index] for index in indices)
            actual_text = str(block.get("marked_text") or "").replace("**", "").replace("/", "")
            if _normalise_public(actual_text) != _normalise_public(expected_text):
                issues.append("annotation_block_text_mismatch")
            public_block_text = "\n".join(
                [str(block.get("marked_text") or ""), *(str(note) for note in block.get("notes", []) if note)]
            )
            issues.extend(_public_text_issues(public_block_text.replace("**", ""), prefix="annotation"))
        if flattened != selected_indices:
            issues.append("annotation_block_indices_coverage_mismatch")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        issues.append("annotation_coverage_invalid")
    return issues


def annotation_review_payload_issues(
    payload: dict[str, Any],
    *,
    annotation_json: Path,
    content_manifest: Path,
) -> list[str]:
    """Validate required_v1's independent per-block annotation evidence."""

    if not annotation_json.is_file() or not content_manifest.is_file():
        return ["annotation_review_inputs_missing"]
    try:
        annotation_payload = json.loads(annotation_json.read_text(encoding="utf-8"))
        manifest = json.loads(content_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["annotation_review_inputs_invalid"]
    blocks = annotation_payload.get("blocks", []) if isinstance(annotation_payload, dict) else annotation_payload
    selection = manifest.get("selections", {}).get("reading_annotation", {}) if isinstance(manifest, dict) else {}
    rows = selection.get("rows", []) if isinstance(selection, dict) else []
    line_text = {
        row.get("source_line_index"): str(row.get("text") or "")
        for row in rows if isinstance(row, dict) and type(row.get("source_line_index")) is int
    }
    if not isinstance(blocks, list) or not blocks:
        return ["annotation_review_blocks_missing"]
    matrix = payload.get("evidence_matrix")
    if not isinstance(matrix, list):
        return ["annotation_review_evidence_matrix_missing"]
    issues: list[str] = []
    if payload.get("critical_errors") or payload.get("p0_errors"):
        issues.append("annotation_review_has_p0")
    expected: dict[tuple[int, ...], str] = {}
    flattened: list[int] = []
    for block in blocks:
        if not isinstance(block, dict):
            issues.append("annotation_review_block_invalid")
            continue
        indices = block.get("source_line_indices")
        if not isinstance(indices, list) or not indices or any(type(value) is not int for value in indices):
            issues.append("annotation_review_block_indices_invalid")
            continue
        key = tuple(indices)
        flattened.extend(indices)
        expected[key] = "".join(line_text.get(index, "") for index in indices)
        if any(index not in line_text for index in indices):
            issues.append(f"annotation_review_block_unknown_index:{list(key)}")
        marked = str(block.get("marked_text") or "")
        if _normalise_public(marked.replace("**", "").replace("/", "")) != _normalise_public(expected[key]):
            issues.append(f"annotation_review_marked_text_mismatch:{list(key)}")
    expected_indices = [row.get("source_line_index") for row in rows if isinstance(row, dict)]
    if flattened != expected_indices:
        issues.append("annotation_review_block_coverage_mismatch")
    actual: dict[tuple[int, ...], dict[str, Any]] = {}
    for item in matrix:
        if not isinstance(item, dict):
            issues.append("annotation_review_evidence_invalid")
            continue
        indices = item.get("source_line_indices")
        if not isinstance(indices, list) or any(type(value) is not int for value in indices):
            issues.append("annotation_review_evidence_indices_invalid")
            continue
        key = tuple(indices)
        if key in actual:
            issues.append(f"annotation_review_evidence_duplicate:{list(key)}")
        actual[key] = item
    if set(actual) != set(expected):
        issues.append("annotation_review_evidence_coverage_mismatch")
    for key, source_text in expected.items():
        item = actual.get(key)
        if item is None:
            continue
        if _normalise_public(str(item.get("original_text") or "")) != _normalise_public(source_text):
            issues.append(f"annotation_review_original_text_mismatch:{list(key)}")
        for field in ("fidelity", "emotion_fit", "pause_emphasis_quality", "performance_guidance_quality"):
            if not str(item.get(field) or "").strip():
                issues.append(f"annotation_review_evidence_field_missing:{list(key)}:{field}")
        if item.get("passed") is not True:
            issues.append(f"annotation_review_block_not_passed:{list(key)}")
    return issues


def _ppt_xml_texts(archive: zipfile.ZipFile, slide_name: str) -> list[str]:
    root = etree.fromstring(archive.read(slide_name))
    return [str(value) for value in root.xpath("//*[local-name()='t']/text()")]


def write_ppt_render_manifest(
    output: Path,
    *,
    pptx: Path,
    with_subtitles: bool,
    rows: list[dict[str, Any]],
    music: Path,
    content_manifest: Path,
    evidence: list[Path] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": PPT_RENDER_MANIFEST_VERSION,
        "product_content_manifest_path": str(content_manifest),
        "product_content_manifest_sha256": file_sha256(content_manifest),
        "pptx_path": str(pptx),
        "pptx_sha256": file_sha256(pptx),
        "with_subtitles": with_subtitles,
        "slide_count": len(rows),
        "canvas": {"width_emu": 12192000, "height_emu": 6858000, "aspect_ratio": "16:9"},
        "music_path": str(music),
        "music_sha256": file_sha256(music),
        "slides": rows,
        "evidence": [
            {"path": str(path), "sha256": file_sha256(path)} for path in (evidence or [])
        ],
    }
    atomic_write_json(output, payload)
    return payload


def ppt_render_manifest_issues(manifest_path: Path) -> list[str]:
    if not manifest_path.is_file():
        return ["ppt_render_manifest_missing"]
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        pptx = Path(payload["pptx_path"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return ["ppt_render_manifest_invalid"]
    issues: list[str] = []
    if payload.get("schema_version") != PPT_RENDER_MANIFEST_VERSION:
        issues.append("ppt_render_manifest_version_mismatch")
    content_manifest = Path(str(payload.get("product_content_manifest_path") or ""))
    if not content_manifest.is_file() or payload.get("product_content_manifest_sha256") != file_sha256(content_manifest):
        issues.append("ppt_content_manifest_stale")
    elif product_content_manifest_issues(content_manifest):
        issues.append("ppt_content_manifest_currentness_failed")
    if not pptx.is_file() or payload.get("pptx_sha256") != file_sha256(pptx):
        return [*issues, "pptx_stale"]
    rows = payload.get("slides")
    if not isinstance(rows, list) or payload.get("slide_count") != len(rows):
        issues.append("ppt_slide_manifest_count_mismatch")
        return issues
    indices = [row.get("slide_index") for row in rows if isinstance(row, dict)]
    if indices != list(range(1, len(rows) + 1)):
        issues.append("ppt_slide_indices_invalid")
    starts = [float(row.get("timing_start", -1)) for row in rows]
    if starts != sorted(starts):
        issues.append("ppt_timing_not_monotonic")
    source_indices = [row.get("source_line_index") for row in rows]
    if len(source_indices) != len(set(source_indices)):
        issues.append("ppt_source_line_duplicate")
    for row in rows:
        start = float(row.get("timing_start", -1))
        end = float(row.get("timing_end", -1))
        duration = float(row.get("duration", -1))
        if start < 0 or end <= start or duration <= 0 or abs((end - start) - duration) > 0.05:
            issues.append(f"ppt_timing_invalid:{row.get('slide_index')}")
    try:
        _content, content_rows = _load_content_selection(content_manifest, "ppt")
        expected_core = [
            (row.get("source_line_index"), str(row.get("text") or ""), str(row.get("image_sha256") or ""))
            for row in content_rows if isinstance(row, dict)
        ]
        actual_core = [
            (row.get("source_line_index"), str(row.get("source_text") or ""), str(row.get("image_sha256") or ""))
            for row in rows if isinstance(row, dict)
        ]
        if actual_core != expected_core:
            issues.append("ppt_content_selection_mismatch")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        issues.append("ppt_content_selection_invalid")
    try:
        with zipfile.ZipFile(pptx) as archive:
            archive.testzip()
            slide_names = sorted(
                (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                key=lambda name: int(re.search(r"(\d+)", Path(name).stem).group(1)),
            )
            if len(slide_names) != len(rows):
                issues.append("pptx_slide_count_mismatch")
            for row, slide_name in zip(rows, slide_names):
                texts = "".join(_ppt_xml_texts(archive, slide_name))
                expected = str(row.get("subtitle_text") or "")
                if bool(payload.get("with_subtitles")) and _normalise_public(texts) != _normalise_public(expected):
                    issues.append(f"ppt_subtitle_text_mismatch:{row.get('slide_index')}")
                if not bool(payload.get("with_subtitles")) and texts.strip():
                    issues.append(f"ppt_clean_variant_contains_subtitle:{row.get('slide_index')}")
                root = etree.fromstring(archive.read(slide_name))
                rel_name = f"ppt/slides/_rels/{Path(slide_name).name}.rels"
                relationship_targets: list[str] = []
                if rel_name in archive.namelist():
                    rel_root = etree.fromstring(archive.read(rel_name))
                    relationship_targets = [str(value) for value in rel_root.xpath("//*[local-name()='Relationship']/@Target")]
                expected_image_sha = str(row.get("image_sha256") or "")
                embedded_image_shas: list[str] = []
                for target in relationship_targets:
                    if "/media/" not in target and not target.startswith("../media/"):
                        continue
                    media_name = "ppt/media/" + Path(target).name
                    if media_name in archive.namelist() and not Path(media_name).name.startswith("bgm"):
                        embedded_image_shas.append(hashlib.sha256(archive.read(media_name)).hexdigest())
                if expected_image_sha not in embedded_image_shas:
                    issues.append(f"ppt_embedded_image_mismatch:{row.get('slide_index')}")
                image = Path(str(row.get("image_path") or ""))
                transforms = root.xpath("//*[local-name()='pic']/*[local-name()='spPr']/*[local-name()='xfrm']")
                if image.is_file() and transforms:
                    extents = transforms[0].xpath("./*[local-name()='ext']")
                    if extents:
                        rendered_width = int(extents[0].get("cx", 0))
                        rendered_height = int(extents[0].get("cy", 0))
                        try:
                            with Image.open(image) as source_image:
                                source_width, source_height = source_image.size
                            source_ratio = source_width / source_height
                            rendered_ratio = rendered_width / rendered_height
                            if (
                                rendered_width < 12192000
                                or rendered_height < 6858000
                                or abs(source_ratio - rendered_ratio) > 0.002
                            ):
                                issues.append(f"ppt_image_distorted_or_not_full_bleed:{row.get('slide_index')}")
                        except (OSError, ZeroDivisionError):
                            issues.append(f"ppt_image_geometry_invalid:{row.get('slide_index')}")
                else:
                    issues.append(f"ppt_image_geometry_missing:{row.get('slide_index')}")
                if bool(payload.get("with_subtitles")):
                    text_shapes = root.xpath(
                        "//*[local-name()='sp'][.//*[local-name()='txBody']]"
                        "/*[local-name()='spPr']/*[local-name()='xfrm']"
                    )
                    if len(text_shapes) != 1:
                        issues.append(f"ppt_subtitle_shape_count_invalid:{row.get('slide_index')}")
                    else:
                        offsets = text_shapes[0].xpath("./*[local-name()='off']")
                        extents = text_shapes[0].xpath("./*[local-name()='ext']")
                        if not offsets or not extents:
                            issues.append(f"ppt_subtitle_geometry_missing:{row.get('slide_index')}")
                        else:
                            actual_bbox = [
                                int(offsets[0].get("x", 0)) / 12192000,
                                int(offsets[0].get("y", 0)) / 6858000,
                                int(extents[0].get("cx", 0)) / 12192000,
                                int(extents[0].get("cy", 0)) / 6858000,
                            ]
                            expected_bbox = row.get("subtitle_bbox")
                            if (
                                not isinstance(expected_bbox, list)
                                or len(expected_bbox) != 4
                                or any(abs(float(actual_bbox[i]) - float(expected_bbox[i])) > 0.002 for i in range(4))
                            ):
                                issues.append(f"ppt_subtitle_geometry_mismatch:{row.get('slide_index')}")
                            if not _bbox_inside(actual_bbox, row.get("subtitle_safe_region")):
                                issues.append(f"ppt_actual_subtitle_outside_safe_region:{row.get('slide_index')}")
                advance = root.xpath("//*[local-name()='transition']/@advTm")
                if advance:
                    actual_duration = float(advance[0]) / 1000.0
                    if abs(actual_duration - float(row.get("duration", -1))) > 0.05:
                        issues.append(f"ppt_slide_duration_mismatch:{row.get('slide_index')}")
            presentation = etree.fromstring(archive.read("ppt/presentation.xml"))
            sizes = presentation.xpath("//*[local-name()='sldSz']")
            if not sizes or int(sizes[0].get("cx", 0)) != 12192000 or int(sizes[0].get("cy", 0)) != 6858000:
                issues.append("ppt_canvas_mismatch")
            media_names = [name for name in archive.namelist() if name.startswith("ppt/media/")]
            music_sha = str(payload.get("music_sha256") or "")
            music_media = [name for name in media_names if hashlib.sha256(archive.read(name)).hexdigest() == music_sha]
            relationship_bytes = b"\n".join(
                archive.read(name) for name in archive.namelist() if name.endswith(".rels")
            )
            if music_sha and (not music_media or not any(Path(name).name.encode() in relationship_bytes for name in music_media)):
                issues.append("ppt_bgm_not_embedded_or_stale")
    except (zipfile.BadZipFile, etree.XMLSyntaxError, OSError):
        issues.append("pptx_zip_invalid")
    for row in rows:
        image = Path(str(row.get("image_path") or ""))
        if not image.is_file() or row.get("image_sha256") != file_sha256(image):
            issues.append(f"ppt_source_image_stale:{row.get('slide_index')}")
        bbox = row.get("subtitle_bbox")
        safe = row.get("subtitle_safe_region")
        if row.get("subtitle_expected") and (not _bbox_inside(bbox, safe)):
            issues.append(f"ppt_subtitle_outside_safe_region:{row.get('slide_index')}")
    for evidence in payload.get("evidence", []):
        path = Path(str(evidence.get("path") or "")) if isinstance(evidence, dict) else Path()
        if not path.is_file() or evidence.get("sha256") != file_sha256(path):
            issues.append("ppt_evidence_stale")
    return issues


def _bbox_inside(bbox: Any, safe: Any) -> bool:
    if not isinstance(bbox, list) or not isinstance(safe, list) or len(bbox) != 4 or len(safe) != 4:
        return False
    x, y, w, h = map(float, bbox)
    sx, sy, sw, sh = map(float, safe)
    return x >= sx and y >= sy and x + w <= sx + sw and y + h <= sy + sh


def write_product_package_manifest(
    output: Path,
    *,
    product_root: Path,
    base_dir: Path,
    advanced_dir: Path,
    dependencies: dict[str, Path],
    source_map: dict[str, Path],
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for package, directory in (("base", base_dir), ("advanced", advanced_dir)):
        for path in sorted(directory.iterdir()):
            if path.is_file():
                source = source_map.get(f"{package}:{path.name}")
                files.append({
                    "package": package,
                    "relative_path": str(path.relative_to(product_root)),
                    "role": _role_for_product_file(path.name),
                    "sha256": file_sha256(path),
                    "source_path": str(source) if source else "",
                    "source_sha256": file_sha256(source) if source and source.is_file() else "",
                    "provenance": "derived_copy",
                })
    payload = {
        "schema_version": PRODUCT_PACKAGE_MANIFEST_VERSION,
        "product_root": str(product_root),
        "base_directory": str(base_dir),
        "advanced_directory": str(advanced_dir),
        "dependencies": {
            name: {"path": str(path), "sha256": file_sha256(path)} for name, path in dependencies.items()
        },
        "files": files,
    }
    try:
        content = json.loads(dependencies["product_content_manifest"].read_text(encoding="utf-8"))
        for field in (*CONTRACT_BINDING_FIELDS, *SEMANTIC_BINDING_FIELDS):
            payload[field] = content.get(field)
        payload["timings_provenance"] = content.get("timings_source")
    except (KeyError, OSError, json.JSONDecodeError):
        pass
    atomic_write_json(output, payload)
    return payload


def product_package_manifest_issues(manifest_path: Path) -> list[str]:
    if not manifest_path.is_file():
        return ["product_package_manifest_missing"]
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        root = Path(payload["product_root"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return ["product_package_manifest_invalid"]
    issues: list[str] = []
    if payload.get("schema_version") != PRODUCT_PACKAGE_MANIFEST_VERSION:
        issues.append("product_package_manifest_version_mismatch")
    dependencies = payload.get("dependencies", {})
    required_dependencies = {
        "product_content_manifest",
        "customer_manuscript_receipt",
        "reading_annotation_receipt",
        "annotation_review",
        "ppt_with_subtitles_render_manifest",
        "ppt_without_subtitles_render_manifest",
        "demo_render_manifest",
        "music",
        "background_with_subtitles",
        "background_without_subtitles",
        "timings_source",
    }
    if not isinstance(dependencies, dict):
        dependencies = {}
        issues.append("product_dependencies_invalid")
    for name in sorted(required_dependencies - set(dependencies)):
        issues.append(f"product_dependency_missing:{name}")
    for name, dependency in dependencies.items():
        if not isinstance(dependency, dict):
            issues.append(f"product_dependency_invalid:{name}")
            continue
        path = Path(str(dependency.get("path") or ""))
        if not path.is_file() or dependency.get("sha256") != file_sha256(path):
            issues.append(f"product_dependency_stale:{name}")
    expected_dirs = {str(payload.get("base_directory")), str(payload.get("advanced_directory"))}
    actual_entries = set(root.iterdir()) if root.is_dir() else set()
    actual_dirs = {str(path) for path in actual_entries if path.is_dir()}
    if actual_dirs != expected_dirs:
        issues.append("product_root_has_unexpected_directories")
    if any(not path.is_dir() for path in actual_entries):
        issues.append("product_root_has_unexpected_files")
    for field in (*CONTRACT_BINDING_FIELDS, *SEMANTIC_BINDING_FIELDS):
        if not isinstance(payload.get(field), str) or not payload.get(field):
            issues.append(f"product_binding_missing:{field}")
    content_dependency = dependencies.get("product_content_manifest", {})
    content_path = Path(str(content_dependency.get("path") or "")) if isinstance(content_dependency, dict) else Path()
    try:
        content_payload = json.loads(content_path.read_text(encoding="utf-8"))
        for field in (*CONTRACT_BINDING_FIELDS, *SEMANTIC_BINDING_FIELDS):
            if payload.get(field) != content_payload.get(field):
                issues.append(f"product_binding_stale:{field}")
        if payload.get("timings_provenance") != content_payload.get("timings_source"):
            issues.append("product_timings_provenance_stale")
        if product_content_manifest_issues(content_path):
            issues.append("product_content_currentness_failed")
    except (OSError, json.JSONDecodeError):
        issues.append("product_content_dependency_invalid")
    seen: set[tuple[str, str]] = set()
    for item in payload.get("files", []):
        rel = str(item.get("relative_path") or "")
        path = root / rel
        key = (str(item.get("package")), str(item.get("role")))
        if key[1] == "unknown":
            issues.append(f"product_unknown_role:{rel}")
        if key in seen and key[1] not in {"background_video"}:
            issues.append(f"product_duplicate_role:{key[0]}:{key[1]}")
        seen.add(key)
        if not path.is_file() or item.get("sha256") != file_sha256(path):
            issues.append(f"product_file_stale:{rel}")
        if any(token.lower() in rel.lower() for token in INTERNAL_FILE_TOKENS):
            issues.append(f"product_internal_file_leak:{rel}")
        if Path(rel).suffix.lower() == ".json":
            issues.append(f"product_internal_json_leak:{rel}")
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            issues.append(f"product_path_invalid:{rel}")
        source = Path(str(item.get("source_path") or ""))
        if not source.is_file() or item.get("source_sha256") != file_sha256(source):
            issues.append(f"product_source_stale:{rel}")
    recorded_files = {
        str((root / str(item.get("relative_path") or "")).resolve())
        for item in payload.get("files", []) if isinstance(item, dict)
    }
    actual_customer_files = {
        str(path.resolve())
        for directory in (Path(str(payload.get("base_directory"))), Path(str(payload.get("advanced_directory"))))
        if directory.is_dir()
        for path in directory.iterdir()
        if path.is_file()
    }
    if recorded_files != actual_customer_files:
        issues.append("product_file_inventory_mismatch")
    required_base = {"customer_manuscript", "music", "reading_annotation", "demo", "background_image"}
    required_advanced = required_base | {"ppt_with_subtitles", "ppt_without_subtitles", "background_video_with_subtitles", "background_video_without_subtitles", "a_only_video"}
    for package, required in (("base", required_base), ("advanced", required_advanced)):
        roles = {role for pkg, role in seen if pkg == package}
        for role in sorted(required - roles):
            issues.append(f"product_required_role_missing:{package}:{role}")
    by_role = {
        (str(item.get("package")), str(item.get("role"))): str(item.get("sha256") or "")
        for item in payload.get("files", []) if isinstance(item, dict)
    }
    for role in sorted(required_base):
        base_sha = by_role.get(("base", role))
        advanced_sha = by_role.get(("advanced", role))
        if base_sha and advanced_sha and base_sha != advanced_sha:
            issues.append(f"product_shared_asset_mismatch:{role}")
    ppt_manifests: list[dict[str, Any]] = []
    for name in ("ppt_with_subtitles_render_manifest", "ppt_without_subtitles_render_manifest"):
        dependency = payload.get("dependencies", {}).get(name, {})
        path = Path(str(dependency.get("path") or "")) if isinstance(dependency, dict) else Path()
        try:
            ppt_manifests.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    if len(ppt_manifests) == 2:
        def core_rows(value: dict[str, Any]) -> list[dict[str, Any]]:
            return [
                {key: row.get(key) for key in (
                    "slide_index", "source_line_index", "source_text", "image_sha256",
                    "timing_start", "timing_end", "duration", "layout_mode",
                )}
                for row in value.get("slides", []) if isinstance(row, dict)
            ]
        if core_rows(ppt_manifests[0]) != core_rows(ppt_manifests[1]):
            issues.append("product_ppt_core_lineage_mismatch")
    return issues


def product_package_review_payload_issues(
    payload: dict[str, Any],
    *,
    evidence_root: Path,
    required_evidence: Iterable[Path],
) -> list[str]:
    """Require required_v1's reviewer to cite every deterministic PPT visual."""

    issues: list[str] = []
    if payload.get("critical_errors") or payload.get("p0_errors"):
        issues.append("product_package_review_has_p0")
    matrix = payload.get("evidence_matrix")
    if not isinstance(matrix, list):
        return [*issues, "product_package_review_evidence_matrix_missing"]
    cited: set[str] = set()
    for item in matrix:
        if not isinstance(item, dict):
            continue
        for key in ("relative_path", "path", "file", "artifact_path"):
            value = str(item.get(key) or "").strip()
            if value:
                cited.add(value.replace("\\", "/"))
    for path in required_evidence:
        try:
            relative = path.relative_to(evidence_root).as_posix()
        except ValueError:
            relative = path.as_posix()
        if relative not in cited and path.as_posix() not in cited:
            issues.append(f"product_package_review_evidence_missing:{relative}")
    return issues


def _role_for_product_file(name: str) -> str:
    if "故事文稿" in name:
        return "customer_manuscript"
    if "故事配乐" in name:
        return "music"
    if "朗读标注" in name:
        return "reading_annotation"
    if "示范表演" in name:
        return "demo"
    if "背景图片" in name:
        return "background_image"
    if "故事PPT" in name and "含字幕" in name:
        return "ppt_with_subtitles"
    if "故事PPT" in name and "无字幕" in name:
        return "ppt_without_subtitles"
    if "背景视频" in name and "含字幕" in name:
        return "background_video_with_subtitles"
    if "背景视频" in name and "无字幕" in name:
        return "background_video_without_subtitles"
    if "A镜无人物" in name:
        return "a_only_video"
    return "unknown"
