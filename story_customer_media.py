"""Independent machine evidence for reusable customer story media."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np


SCHEMA_VERSION = "story-customer-media-receipt/v1"
MUSIC_ONLY_CORRELATION_MIN = 0.97
MUSIC_ONLY_RESIDUAL_MAX = 0.08


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _binding(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"客户媒体证据文件不存在：{resolved}")
    return {
        "path": str(resolved),
        "sha256": file_sha256(resolved),
        "bytes": resolved.stat().st_size,
    }


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _decode_audio(path: Path, sample_rate: int = 8000) -> np.ndarray:
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "f32le",
        "-",
    ]
    result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    signal = np.frombuffer(result.stdout, dtype="<f4").astype(np.float64)
    if signal.size < sample_rate or not np.isfinite(signal).all():
        raise ValueError(f"客户媒体音轨为空或无法解码：{path}")
    return signal


def music_only_fit(
    rendered_audio: np.ndarray,
    source_music: np.ndarray,
    *,
    sample_rate: int = 8000,
) -> dict[str, float | int | bool]:
    """Measure whether a rendered track is a gain-adjusted copy of the score."""

    length = min(rendered_audio.size, source_music.size)
    if length < sample_rate:
        raise ValueError("音频过短，无法审核配乐-only 角色")
    rendered = rendered_audio[:length]
    music = source_music[:length]
    best: tuple[float, float, int, float] | None = None
    max_lag = round(sample_rate * 0.2)
    step = max(1, round(sample_rate * 0.01))
    for lag in range(-max_lag, max_lag + 1, step):
        if lag >= 0:
            expected = music[: length - lag]
            actual = rendered[lag:length]
        else:
            expected = music[-lag:length]
            actual = rendered[: length + lag]
        expected_energy = float(np.dot(expected, expected))
        actual_energy = float(np.dot(actual, actual))
        if expected_energy <= 1e-12 or actual_energy <= 1e-12:
            continue
        gain = float(np.dot(expected, actual) / expected_energy)
        residual = float(np.mean((actual - gain * expected) ** 2) / np.mean(actual**2))
        correlation = float(np.dot(expected, actual) / math.sqrt(expected_energy * actual_energy))
        candidate = (residual, -correlation, lag, gain)
        if best is None or candidate < best:
            best = candidate
    if best is None:
        raise ValueError("音频能量不足，客户视频不能是静音")
    residual, negative_correlation, lag, gain = best
    correlation = -negative_correlation
    rms = float(math.sqrt(np.mean(rendered**2)))
    return {
        "passed": (
            rms > 1e-5
            and correlation >= MUSIC_ONLY_CORRELATION_MIN
            and residual <= MUSIC_ONLY_RESIDUAL_MAX
        ),
        "correlation": round(correlation, 6),
        "residual_energy_ratio": round(residual, 6),
        "best_lag_samples": int(lag),
        "gain": round(gain, 6),
        "rms": round(rms, 8),
    }


def _probe_video(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    payload = json.loads(result.stdout)
    stream = payload.get("streams", [{}])[0]
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "duration_seconds": round(float(payload.get("format", {}).get("duration") or 0), 3),
    }


def _parse_srt_midpoints(path: Path) -> list[float]:
    def seconds(value: str) -> float:
        hours, minutes, rest = value.replace(",", ".").split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(rest)

    values: list[float] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if "-->" not in line:
            continue
        raw_start, raw_end = line.split("-->", 1)
        start, end = seconds(raw_start.strip()), seconds(raw_end.strip())
        if end > start:
            values.append((start + end) / 2)
    if not values:
        raise ValueError("客户含字幕视频缺少可审核 SRT cue")
    if len(values) <= 5:
        return values
    indices = np.linspace(0, len(values) - 1, 5).round().astype(int)
    return [values[int(index)] for index in indices]


def _frame_rgb(path: Path, seconds: float, width: int, height: int) -> np.ndarray:
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{seconds:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    expected = width * height * 3
    if len(result.stdout) != expected:
        raise ValueError(f"无法抽取客户视频审核帧：{path} @ {seconds:.3f}s")
    return np.frombuffer(result.stdout, dtype=np.uint8).reshape((height, width, 3))


def subtitle_geometry_from_frames(
    with_subtitles: np.ndarray,
    without_subtitles: np.ndarray,
) -> dict[str, Any]:
    if with_subtitles.shape != without_subtitles.shape or with_subtitles.ndim != 3:
        raise ValueError("字幕位置审核帧尺寸不一致")
    height, width, _channels = with_subtitles.shape
    delta = np.max(
        np.abs(with_subtitles.astype(np.int16) - without_subtitles.astype(np.int16)),
        axis=2,
    )
    bright = np.min(with_subtitles, axis=2) >= 150
    mask = (delta >= 45) & bright
    row_counts = mask.sum(axis=1)
    dense_rows = np.flatnonzero(row_counts >= max(8, round(width * 0.003)))
    if dense_rows.size == 0:
        return {"passed": False, "reason": "subtitle_pixels_not_detected"}
    row_groups: list[tuple[int, int]] = []
    group_start = previous = int(dense_rows[0])
    for raw_row in dense_rows[1:]:
        row = int(raw_row)
        if row > previous + 1:
            row_groups.append((group_start, previous))
            group_start = row
        previous = row
    row_groups.append((group_start, previous))
    # Paired videos can have sparse bright codec differences away from the
    # subtitle. Select the strongest contiguous text band instead of stretching
    # one bbox across every dense difference row in the frame.
    y0, y1 = max(
        row_groups,
        key=lambda bounds: (
            int(row_counts[bounds[0] : bounds[1] + 1].sum()),
            bounds[1] - bounds[0] + 1,
            bounds[1],
        ),
    )
    band = mask[y0 : y1 + 1]
    dense_cols = np.flatnonzero(band.sum(axis=0) >= 2)
    if dense_cols.size == 0:
        return {"passed": False, "reason": "subtitle_columns_not_detected"}
    x0, x1 = int(dense_cols.min()), int(dense_cols.max())
    center_x = (x0 + x1) / 2
    center_y = (y0 + y1) / 2
    passed = (
        abs(center_x - width / 2) <= width * 0.12
        and center_y >= height * 0.72
        and y1 >= height * 0.76
    )
    return {
        "passed": passed,
        "bbox": [x0, y0, x1 + 1, y1 + 1],
        "center": [round(center_x, 2), round(center_y, 2)],
        "canvas": [width, height],
        "horizontal_center_error_ratio": round(abs(center_x - width / 2) / width, 6),
        "vertical_center_ratio": round(center_y / height, 6),
        "bottom_ratio": round(y1 / height, 6),
    }


def subtitle_geometry_from_videos(
    with_subtitles: Path,
    without_subtitles: Path,
    subtitle_srt: Path,
) -> dict[str, Any]:
    probe_with = _probe_video(with_subtitles)
    probe_without = _probe_video(without_subtitles)
    if probe_with["width"] != probe_without["width"] or probe_with["height"] != probe_without["height"]:
        return {"passed": False, "reason": "paired_video_canvas_mismatch"}
    width, height = probe_with["width"], probe_with["height"]
    samples = []
    for timestamp in _parse_srt_midpoints(subtitle_srt):
        with_frame = _frame_rgb(with_subtitles, timestamp, width, height)
        without_frame = _frame_rgb(without_subtitles, timestamp, width, height)
        samples.append(
            {
                "time_seconds": round(timestamp, 3),
                **subtitle_geometry_from_frames(with_frame, without_frame),
            }
        )
    return {
        "passed": bool(samples) and all(item.get("passed") is True for item in samples),
        "canvas": [width, height],
        "samples": samples,
    }


def write_customer_media_receipt(
    *,
    output_path: Path,
    music: Path,
    authoritative_timeline_receipt: Path,
    subtitle_srt: Path,
    background_with_subtitles: Path,
    background_without_subtitles: Path,
    a_only_video: Path,
    demo_video: Path,
) -> Path:
    source_music = _decode_audio(music)
    artifact_paths = {
        "product_background_with_subtitles": background_with_subtitles,
        "product_background_without_subtitles": background_without_subtitles,
        "product_a_only_background": a_only_video,
    }
    artifacts: dict[str, Any] = {}
    critical_errors: list[str] = []
    for role, path in artifact_paths.items():
        rendered = _decode_audio(path)
        fit = music_only_fit(rendered, source_music)
        probe = _probe_video(path)
        if fit["passed"] is not True:
            critical_errors.append(f"{role}:audio_not_music_only")
        if probe["width"] != 1920 or probe["height"] != 1080:
            critical_errors.append(f"{role}:canvas_not_1920x1080")
        artifacts[role] = {
            **_binding(path),
            "audio_role": "music_only",
            "audio_present": fit["rms"] > 1e-5,
            "audio_fit": fit,
            "video_probe": probe,
        }

    demo_signal = _decode_audio(demo_video)
    demo_fit = music_only_fit(demo_signal, source_music)
    if demo_fit["passed"] is True:
        critical_errors.append("product_demo:narration_missing")
    artifacts["product_demo"] = {
        **_binding(demo_video),
        "audio_role": "narration_plus_music",
        "audio_present": demo_fit["rms"] > 1e-5,
        "music_only_fit": demo_fit,
        "video_probe": _probe_video(demo_video),
    }

    subtitle_geometry = subtitle_geometry_from_videos(
        background_with_subtitles,
        background_without_subtitles,
        subtitle_srt,
    )
    if subtitle_geometry.get("passed") is not True:
        critical_errors.append("product_background_with_subtitles:subtitle_geometry_invalid")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "passed": not critical_errors,
        "critical_errors": critical_errors,
        "music_source": _binding(music),
        "authoritative_timeline_receipt": _binding(authoritative_timeline_receipt),
        "subtitle_srt": _binding(subtitle_srt),
        "artifacts": artifacts,
        "subtitle_geometry": subtitle_geometry,
    }
    _atomic_json(output_path, payload)
    if critical_errors:
        raise RuntimeError("客户媒体机器 QA 未通过：" + "；".join(critical_errors))
    return output_path


def _current_binding(item: Mapping[str, Any], label: str) -> Path:
    path = Path(str(item.get("path") or "")).expanduser().resolve()
    expected = str(item.get("sha256") or "")
    if not path.is_file() or len(expected) != 64 or file_sha256(path) != expected:
        raise ValueError(f"{label}缺失或哈希漂移")
    return path


def validate_customer_media_receipt(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("客户媒体回执不是有效 JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("客户媒体回执版本无效")
    if payload.get("passed") is not True or payload.get("critical_errors") != []:
        raise ValueError("客户媒体回执未通过")
    for key in ("music_source", "authoritative_timeline_receipt", "subtitle_srt"):
        item = payload.get(key)
        if not isinstance(item, dict):
            raise ValueError(f"客户媒体回执缺少 {key}")
        _current_binding(item, f"客户媒体 {key}")
    artifacts = payload.get("artifacts")
    expected_roles = {
        "product_background_with_subtitles": "music_only",
        "product_background_without_subtitles": "music_only",
        "product_a_only_background": "music_only",
        "product_demo": "narration_plus_music",
    }
    if not isinstance(artifacts, dict) or set(artifacts) != set(expected_roles):
        raise ValueError("客户媒体回执产物角色不完整")
    for role, expected_audio_role in expected_roles.items():
        item = artifacts[role]
        if not isinstance(item, dict):
            raise ValueError(f"客户媒体角色格式无效：{role}")
        _current_binding(item, f"客户媒体 {role}")
        if item.get("audio_role") != expected_audio_role or item.get("audio_present") is not True:
            raise ValueError(f"客户媒体音频角色无效：{role}")
        if expected_audio_role == "music_only":
            fit = item.get("audio_fit")
            if not isinstance(fit, dict) or fit.get("passed") is not True:
                raise ValueError(f"客户媒体不是配乐-only：{role}")
    geometry = payload.get("subtitle_geometry")
    if not isinstance(geometry, dict) or geometry.get("passed") is not True:
        raise ValueError("客户媒体字幕画布/位置未通过")
    return payload


__all__ = [
    "SCHEMA_VERSION",
    "music_only_fit",
    "subtitle_geometry_from_frames",
    "subtitle_geometry_from_videos",
    "validate_customer_media_receipt",
    "write_customer_media_receipt",
]
