from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence


MOTION_REQUEST_SCHEMA = "story-semantic-card-motion-request/v1"
MOTION_RECEIPT_SCHEMA = "story-semantic-card-motion/v1"
MOTION_PROMPT_VERSION = "story-semantic-card-motion-prompt/v1"
MOTION_PROVIDER_SECONDS = (6.0, 10.0)
MOTION_PROVIDER_SAFE_PROMPT_CHARS = 120
MOTION_PROVIDER_RESOLUTION = "720p"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stable_json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def semantic_card_motion_prompt() -> str:
    """A stable image-to-video instruction that treats all text as locked pixels."""

    return (
        "以首帧为唯一依据，镜头固定。所有中文文字和文字背板完全静止，不得改字、增删、"
        "变形、闪烁、位移、缩放或重绘。仅让光影、羽毛、麦穗、树叶、云雾等非文字元素"
        "轻微运动。不得新增人物、文字、Logo、水印或字幕，首尾衔接自然。"
    )


def select_motion_provider_seconds(presentation_duration: float) -> float:
    """Choose the nearest Grok 1.0 native duration for one card window.

    Grok Video 1.0 exposes two exact duration choices.  On an exact tie the
    longer clip wins, preserving more native temporal detail before the final
    compositor applies a single uniform speed change.
    """

    target = max(0.5, float(presentation_duration))
    return min(MOTION_PROVIDER_SECONDS, key=lambda seconds: (abs(seconds - target), -seconds))


def _load_static_card_receipt(card_dir: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    path = card_dir / "semantic_card_generation_receipt.json"
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("缺少可用于图生视频的 ImageGen 语义卡回执") from exc
    if (
        receipt.get("schema_version") != "story-semantic-card-generation/v1"
        or receipt.get("imagegen_native") is not True
        or receipt.get("post_render_text_overlay") is not False
    ):
        raise RuntimeError("图生视频输入必须是已审核的 ImageGen 一体化图卡")
    items = receipt.get("cards") if isinstance(receipt.get("cards"), list) else []
    by_kind = {
        str(item.get("card_kind") or ""): item
        for item in items
        if isinstance(item, dict) and str(item.get("card_kind") or "")
    }
    return receipt, by_kind


def write_semantic_card_motion_request(
    *,
    card_dir: Path,
    windows: Sequence[Mapping[str, Any]],
    artifact_semantic_plan_sha256: str,
) -> Path:
    """Write a provider-neutral, hash-bound motion request for title/moral cards."""

    card_dir = card_dir.expanduser().resolve()
    card_dir.mkdir(parents=True, exist_ok=True)
    static_receipt, static_by_kind = _load_static_card_receipt(card_dir)
    prompt = semantic_card_motion_prompt()
    prompt_chars = len(prompt.encode("utf-16-le")) // 2
    if prompt_chars > MOTION_PROVIDER_SAFE_PROMPT_CHARS:
        raise RuntimeError(
            "片头/寓意卡微动 Prompt 超过当前 provider 安全上限："
            f"{prompt_chars}>{MOTION_PROVIDER_SAFE_PROMPT_CHARS}"
        )
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for window in windows:
        kind = str(window.get("card_kind") or "")
        if not kind or kind in seen:
            continue
        item = static_by_kind.get(kind)
        if item is None or str(item.get("text") or "") != str(window.get("text") or ""):
            raise RuntimeError(f"图生视频请求未找到当前 {kind} 的图卡与文案绑定")
        source = Path(str(item.get("path") or "")).expanduser().resolve()
        try:
            source.relative_to(card_dir)
        except ValueError as exc:
            raise RuntimeError(f"图生视频源图不在当前项目目录：{kind}") from exc
        if not source.is_file() or file_sha256(source) != item.get("sha256"):
            raise RuntimeError(f"图生视频源图或哈希已失效：{kind}")
        presentation_duration = max(0.5, float(window["end"]) - float(window["start"]))
        provider_duration = select_motion_provider_seconds(presentation_duration)
        cards.append(
            {
                "card_kind": kind,
                "semantic_kind": str(window.get("semantic_kind") or ""),
                "text": str(window.get("text") or ""),
                "source_image_path": str(source),
                "source_image_sha256": file_sha256(source),
                "output_video_path": str(card_dir / f"{kind}_motion.mp4"),
                # The provider clip is selected from Grok 1.0's exact 6/10s
                # choices, then uniformly time-fitted to the real presentation
                # window.  It is never repeated as a loop.
                "required_duration_seconds": provider_duration,
                "presentation_window_seconds": round(presentation_duration, 3),
                "time_fit_policy": "uniform_setpts_to_presentation_window",
                "loop_policy": "forbidden",
                "requested_ratio": "16:9",
                # The configured ToAPIs routes are native 720p. The final
                # release compositor still encodes 1080p; requesting 1080p
                # here would fail before generation on the current channels.
                "requested_resolution": MOTION_PROVIDER_RESOLUTION,
                "prompt": prompt,
                "prompt_sha256": prompt_sha256,
                "text_region_locked": True,
                "non_text_motion_only": True,
                "max_attempt_count": 3,
            }
        )
        seen.add(kind)
    payload = {
        "schema_version": MOTION_REQUEST_SCHEMA,
        "artifact_semantic_plan_sha256": artifact_semantic_plan_sha256,
        "static_card_receipt_sha256": stable_json_sha256(static_receipt),
        "prompt_version": MOTION_PROMPT_VERSION,
        "prompt_sha256": prompt_sha256,
        "production_requires_provider_video": True,
        "static_preview_allowed_only_for_explicit_short_test": True,
        "cards": cards,
    }
    path = card_dir / "semantic_card_motion_request.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    handoff = card_dir / "semantic_card_motion_handoff.md"
    handoff.write_text(
        "\n".join(
            [
                "# 片头/寓意卡图生视频任务",
                "",

                "必须用当前正式图生视频 provider 执行，不得用缩放静态图或本地假动效冒充。",
                "每张卡必须按请求中的 6 秒或 10 秒生成；合成时只允许整段统一变速贴合真实语音窗口，禁止循环。",
                "每张卡最多首次生成加两轮定向修正。中文文字任一抽检帧不稳定都不得通过。",
                f"机器请求：`{path}`",
                f"完成后写入：`{card_dir / 'semantic_card_motion_receipt.json'}`",
                "回执必须绑定请求哈希、源图/输出哈希、provider 请求 ID 和实际尝试次数，并记录首帧、中间帧、末帧 OCR 与文字稳定性审核。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def probe_video_duration(path: Path) -> float:
    process = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
        capture_output=True,
    )
    if process.returncode != 0:
        return 0.0
    try:
        return float(process.stdout.strip())
    except ValueError:
        return 0.0


def semantic_card_motion_receipt_issues(request_path: Path, receipt_path: Path) -> list[str]:
    """Fail closed unless each provider clip is current and its native text stayed stable."""

    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["semantic_card_motion_receipt_missing_or_invalid"]
    issues: list[str] = []
    if request.get("schema_version") != MOTION_REQUEST_SCHEMA:
        issues.append("semantic_card_motion_request_schema_invalid")
    if receipt.get("schema_version") != MOTION_RECEIPT_SCHEMA:
        issues.append("semantic_card_motion_receipt_schema_invalid")
    if receipt.get("request_sha256") != file_sha256(request_path):
        issues.append("semantic_card_motion_request_binding_mismatch")
    if receipt.get("artifact_semantic_plan_sha256") != request.get("artifact_semantic_plan_sha256"):
        issues.append("semantic_card_motion_plan_binding_mismatch")
    request_items = request.get("cards") if isinstance(request.get("cards"), list) else []
    receipt_items = receipt.get("cards") if isinstance(receipt.get("cards"), list) else []
    expected = {
        str(item.get("card_kind") or ""): item
        for item in request_items
        if isinstance(item, dict) and str(item.get("card_kind") or "")
    }
    actual = {
        str(item.get("card_kind") or ""): item
        for item in receipt_items
        if isinstance(item, dict) and str(item.get("card_kind") or "")
    }
    if set(actual) != set(expected):
        issues.append("semantic_card_motion_set_mismatch")
    root = request_path.parent.resolve()
    for kind, request_item in expected.items():
        item = actual.get(kind)
        if item is None:
            continue
        for field in ("source_image_sha256", "prompt_sha256"):
            if item.get(field) != request_item.get(field):
                issues.append(f"semantic_card_motion_{field}_mismatch:{kind}")
        if str(item.get("text") or "") != str(request_item.get("text") or ""):
            issues.append(f"semantic_card_motion_text_binding_mismatch:{kind}")
        try:
            attempt_count = int(item.get("attempt_count") or 0)
        except (TypeError, ValueError):
            attempt_count = 0
        if not 1 <= attempt_count <= int(request_item.get("max_attempt_count") or 3):
            issues.append(f"semantic_card_motion_attempt_count_invalid:{kind}")
        if not str(item.get("provider") or "") or not str(item.get("provider_request_id") or ""):
            issues.append(f"semantic_card_motion_provider_binding_missing:{kind}")
        for field in (
            "text_region_locked",
            "non_text_motion_only",
            "ocr_first_frame_passed",
            "ocr_middle_frame_passed",
            "ocr_last_frame_passed",
            "text_stability_passed",
            "visual_review_passed",
        ):
            if item.get(field) is not True:
                issues.append(f"semantic_card_motion_{field}_failed:{kind}")
        output = Path(str(item.get("output_video_path") or "")).expanduser().resolve()
        try:
            output.relative_to(root)
        except ValueError:
            issues.append(f"semantic_card_motion_path_outside_project:{kind}")
            continue
        if (
            str(output) != str(Path(str(request_item.get("output_video_path") or "")).expanduser().resolve())
            or not output.is_file()
            or item.get("output_video_sha256") != (file_sha256(output) if output.is_file() else "")
        ):
            issues.append(f"semantic_card_motion_file_binding_mismatch:{kind}")
            continue
        required = float(request_item.get("required_duration_seconds") or 0.0)
        produced = probe_video_duration(output)
        if produced + 0.05 < required:
            issues.append(f"semantic_card_motion_duration_too_short:{kind}")
    return sorted(set(issues))


def load_semantic_card_motion_paths(request_path: Path, receipt_path: Path) -> dict[str, Path]:
    issues = semantic_card_motion_receipt_issues(request_path, receipt_path)
    if issues:
        raise RuntimeError("片头/寓意卡微动回执未通过：" + "；".join(issues))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    return {
        str(item["card_kind"]): Path(str(item["output_video_path"])).expanduser().resolve()
        for item in receipt["cards"]
    }
