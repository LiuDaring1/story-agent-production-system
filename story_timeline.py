"""Authoritative spoken-timeline receipts for story production.

Confirmed text is immutable and ASR contributes timestamps only.  A later
equal-duration estimate may be useful for an early preflight preview, but it
must never replace a successful audio alignment in production.
"""

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
from typing import Any, Mapping


SCHEMA_VERSION = "story-authoritative-timeline/v1"
PRODUCTION_SOURCE_KINDS = {
    "whisper_confirmed_line_timings",
    "whisper_native_synthesis",
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _binding(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"时间轴绑定文件不存在：{resolved}")
    return {
        "path": str(resolved),
        "sha256": file_sha256(resolved),
        "bytes": resolved.stat().st_size,
    }


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}不是有效 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label}顶层必须是 JSON 对象")
    return payload


def _load_timing_rows(path: Path) -> list[dict[str, Any]]:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("权威口播时间轴无法解析") from exc
    if not isinstance(rows, list) or not rows:
        raise ValueError("权威口播时间轴为空")
    normalized: list[dict[str, Any]] = []
    previous_end = -1.0
    for position, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"权威口播时间轴第 {position} 条格式错误")
        text = str(row.get("line") or "").strip()
        try:
            start = float(row["source_start"])
            end = float(row["source_end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"权威口播时间轴第 {position} 条缺少 source_start/source_end") from exc
        if not text or start < 0 or end <= start or start + 1e-6 < previous_end:
            raise ValueError("权威口播时间轴必须文本非空、时长为正且单调")
        normalized.append({"line": text, "source_start": start, "source_end": end})
        previous_end = end
    return normalized


def _subtitle_lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _stamp(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _write_srt(rows: list[dict[str, Any]], target: Path) -> None:
    blocks = [
        f"{index}\n{_stamp(row['source_start'])} --> {_stamp(row['source_end'])}\n{row['line']}"
        for index, row in enumerate(rows, start=1)
    ]
    _atomic_write(target, "\n\n".join(blocks) + "\n")


def _parse_srt(path: Path) -> list[dict[str, Any]]:
    def seconds(value: str) -> float:
        hours, minutes, rest = value.replace(",", ".").split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(rest)

    rows: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8-sig")
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            raise ValueError("权威 SRT 含无效 cue")
        raw_start, raw_end = lines[1].split("-->", 1)
        rows.append(
            {
                "line": "".join(lines[2:]).strip(),
                "source_start": seconds(raw_start.strip()),
                "source_end": seconds(raw_end.strip()),
            }
        )
    return rows


def _current_bound_path(item: Mapping[str, Any], label: str) -> Path:
    path = Path(str(item.get("path") or "")).expanduser().resolve()
    expected = str(item.get("sha256") or "").lower()
    if not path.is_file() or len(expected) != 64 or file_sha256(path) != expected:
        raise ValueError(f"{label}缺失或哈希漂移")
    return path


def _story_run_payload(status_dir: Path) -> dict[str, Any] | None:
    path = status_dir / "story_run.json"
    if not path.is_file():
        return None
    return _load_json_object(path, "story_run.json")


def _ledger_input_paths(status_dir: Path) -> tuple[Path, Path, dict[str, Any]]:
    run = _story_run_payload(status_dir)
    if run is None:
        raise ValueError("生产时间轴缺少 story_run.json 输入绑定")
    inputs = run.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("story_run.json 缺少 inputs")
    subtitle = inputs.get("subtitle_txt")
    audio = inputs.get("audio")
    if not isinstance(subtitle, dict) or not isinstance(audio, dict):
        raise ValueError("story_run.json 缺少 subtitle_txt 或权威 audio")
    return (
        _current_bound_path(subtitle, "确认字幕 TXT"),
        _current_bound_path(audio, "权威音频"),
        inputs,
    )


def write_authoritative_timeline_receipt(
    *,
    receipt_path: Path,
    source_kind: str,
    timings_path: Path,
    alignment_metadata_path: Path,
    subtitle_txt: Path,
    authoritative_audio: Path,
    alignment_audio: Path,
    output_srt: Path,
) -> Path:
    if source_kind not in PRODUCTION_SOURCE_KINDS:
        raise ValueError("均分/even fallback 不能写成生产权威时间轴")
    rows = _load_timing_rows(timings_path)
    lines = _subtitle_lines(subtitle_txt)
    if [row["line"] for row in rows] != lines:
        raise ValueError("时间轴文本与用户确认字幕 TXT 不完全一致")
    _write_srt(rows, output_srt)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "authority": "confirmed_audio_alignment",
        "source_kind": source_kind,
        "fallback_used": False,
        "time_basis": "source_start_source_end",
        "line_count": len(rows),
        "timings": _binding(timings_path),
        "alignment_metadata": _binding(alignment_metadata_path),
        "subtitle_txt": _binding(subtitle_txt),
        "authoritative_audio": _binding(authoritative_audio),
        "alignment_audio": _binding(alignment_audio),
        "output_srt": _binding(output_srt),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write(
        receipt_path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    validate_authoritative_timeline_receipt(receipt_path)
    return receipt_path


def validate_authoritative_timeline_receipt(
    path: Path,
    *,
    expected_inputs: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = _load_json_object(path, "权威时间轴回执")
    if payload.get("source_kind") == "confirmed_user_srt":
        return _validate_confirmed_user_srt(payload, expected_inputs=expected_inputs)
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("权威时间轴回执版本无效")
    if payload.get("authority") != "confirmed_audio_alignment":
        raise ValueError("权威时间轴没有声明确认音频对齐")
    if payload.get("source_kind") not in PRODUCTION_SOURCE_KINDS or payload.get("fallback_used") is not False:
        raise ValueError("fallback/均分时间轴不可用于生产")
    if payload.get("time_basis") != "source_start_source_end":
        raise ValueError("权威时间轴必须使用 source_start/source_end")

    bound: dict[str, Path] = {}
    for key in (
        "timings",
        "alignment_metadata",
        "subtitle_txt",
        "authoritative_audio",
        "alignment_audio",
        "output_srt",
    ):
        item = payload.get(key)
        if not isinstance(item, dict):
            raise ValueError(f"权威时间轴回执缺少 {key}")
        bound[key] = _current_bound_path(item, f"权威时间轴 {key}")

    if expected_inputs is not None:
        for receipt_key, input_key in (
            ("subtitle_txt", "subtitle_txt"),
            ("authoritative_audio", "audio"),
        ):
            expected = expected_inputs.get(input_key)
            actual = payload.get(receipt_key)
            if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
                raise ValueError(f"权威时间轴无法绑定账本输入 {input_key}")
            if str(actual.get("sha256") or "") != str(expected.get("sha256") or ""):
                raise ValueError(f"权威时间轴未绑定账本当前 {input_key}")

    metadata = _load_json_object(bound["alignment_metadata"], "音频对齐 metadata")
    mode = str(metadata.get("alignment_mode") or metadata.get("mode") or "whisper").lower()
    if mode == "even" or "fallback" in mode:
        raise ValueError("音频对齐 metadata 表明使用了均分 fallback")
    if not str(metadata.get("model") or "").strip():
        raise ValueError("音频对齐 metadata 缺少模型")
    if int(metadata.get("timed_char_count") or 0) <= 0:
        raise ValueError("音频对齐 metadata 没有 timed_char_count")
    metadata_subtitle_sha = str(metadata.get("subtitle_sha256") or "")
    if metadata_subtitle_sha and metadata_subtitle_sha != payload["subtitle_txt"]["sha256"]:
        raise ValueError("音频对齐 metadata 绑定了不同字幕")
    metadata_audio_sha = str(metadata.get("audio_sha256") or "")
    if metadata_audio_sha and metadata_audio_sha != payload["alignment_audio"]["sha256"]:
        raise ValueError("音频对齐 metadata 绑定了不同音频")

    rows = _load_timing_rows(bound["timings"])
    subtitle_lines = _subtitle_lines(bound["subtitle_txt"])
    if [row["line"] for row in rows] != subtitle_lines:
        raise ValueError("权威时间轴文字不等于确认字幕 TXT")
    srt_rows = _parse_srt(bound["output_srt"])
    if len(srt_rows) != len(rows) or len(rows) != payload.get("line_count"):
        raise ValueError("权威时间轴行数不一致")
    for expected, actual in zip(rows, srt_rows):
        if expected["line"] != actual["line"]:
            raise ValueError("权威 SRT 文本与 timing 行不一致")
        if abs(expected["source_start"] - actual["source_start"]) > 0.002 or abs(expected["source_end"] - actual["source_end"]) > 0.002:
            raise ValueError("权威 SRT 未使用 source_start/source_end")
    return payload


def _confirmed_srt_rows(path: Path) -> list[dict[str, Any]]:
    """Strict one-line cues: never normalize or rewrite confirmed subtitle text."""
    rows = []
    previous_end = 0.0
    for index, block in enumerate(re.split(r"\n[ \t]*\n", path.read_text(encoding="utf-8-sig").strip()), 1):
        lines = block.splitlines()
        if len(lines) != 3 or lines[0] != str(index):
            raise ValueError("确认 SRT 必须连续编号且每个 cue 只有一行原文")
        match = re.fullmatch(r"(\d{2,}):([0-5]\d):([0-5]\d),(\d{3}) --> (\d{2,}):([0-5]\d):([0-5]\d),(\d{3})", lines[1])
        if not match:
            raise ValueError("确认 SRT 时间格式无效")
        values = list(map(int, match.groups()))
        start, end = [v[0]*3600 + v[1]*60 + v[2] + v[3]/1000 for v in (values[:4], values[4:])]
        if not lines[2] or end <= start or start < previous_end:
            raise ValueError("确认 SRT 时间必须为正、按顺序且不重叠")
        rows.append(dict(line=lines[2], source_start=start, source_end=end))
        previous_end = end
    return rows


def _audio_duration(path: Path) -> float:
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)], check=True, capture_output=True, text=True, timeout=60)
    duration = float(json.loads(result.stdout)["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("权威音频时长无效")
    return duration


def _validate_confirmed_user_srt(payload: dict[str, Any], *, expected_inputs=None) -> dict[str, Any]:
    if (payload.get("schema_version") != SCHEMA_VERSION or payload.get("authority") != "user_confirmed_subtitle_timeline"
            or payload.get("fallback_used") is not False or payload.get("time_basis") != "source_start_source_end"):
        raise ValueError("用户确认时间轴声明无效")
    bound = {}
    for key in ("timings", "subtitle_txt", "subtitle_srt", "authoritative_audio", "output_srt"):
        if not isinstance(payload.get(key), dict):
            raise ValueError(f"确认时间轴缺少 {key}")
        bound[key] = _current_bound_path(payload[key], key)
    if payload["output_srt"]["sha256"] != payload["subtitle_srt"]["sha256"]:
        raise ValueError("确认 SRT 原字节被改变")
    if expected_inputs is not None:
        for key, input_key in (("subtitle_txt", "subtitle_txt"), ("subtitle_srt", "subtitle_srt"), ("authoritative_audio", "audio")):
            expected = expected_inputs.get(input_key)
            if not isinstance(expected, Mapping) or expected.get("sha256") != payload[key]["sha256"]:
                raise ValueError(f"确认时间轴未绑定当前输入 {input_key}")
            _current_bound_path(expected, input_key)
    rows = _confirmed_srt_rows(bound["subtitle_srt"])
    lines = bound["subtitle_txt"].read_text(encoding="utf-8-sig").splitlines()
    if [r["line"] for r in rows] != lines:
        raise ValueError("确认 SRT 与 TXT 逐行原文不完全一致")
    if rows != _load_timing_rows(bound["timings"]) or len(rows) != payload.get("line_count"):
        raise ValueError("确认 SRT 与 timings 不一致")
    duration = _audio_duration(bound["authoritative_audio"])
    recorded_duration = float(payload.get("audio_duration_seconds", -1))
    if not math.isfinite(recorded_duration) or rows[-1]["source_end"] > duration or abs(duration - recorded_duration) > 0.001:
        raise ValueError("确认时间轴超过音频时长或时长证据失效")
    return payload


def import_confirmed_user_srt(*, receipt_path: Path, timings_path: Path,
                              subtitle_txt: Path, subtitle_srt: Path,
                              authoritative_audio: Path, expected_inputs=None) -> Path:
    """Bind user-confirmed SRT verbatim; this is not an ASR alignment claim."""
    if receipt_path.resolve() in {subtitle_txt.resolve(), subtitle_srt.resolve(), authoritative_audio.resolve()} or timings_path.resolve() in {subtitle_txt.resolve(), subtitle_srt.resolve(), authoritative_audio.resolve(), receipt_path.resolve()}:
        raise ValueError("时间轴产物不得覆盖输入或彼此覆盖")
    rows = _confirmed_srt_rows(subtitle_srt)
    if [r["line"] for r in rows] != subtitle_txt.read_text(encoding="utf-8-sig").splitlines():
        raise ValueError("确认 SRT 与 TXT 逐行原文不完全一致")
    duration = _audio_duration(authoritative_audio)
    if rows[-1]["source_end"] > duration:
        raise ValueError("确认 SRT 超过权威音频时长")
    _atomic_write(timings_path, json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    payload = dict(schema_version=SCHEMA_VERSION, authority="user_confirmed_subtitle_timeline",
                   source_kind="confirmed_user_srt", fallback_used=False,
                   time_basis="source_start_source_end", line_count=len(rows),
                   timings=_binding(timings_path), subtitle_txt=_binding(subtitle_txt),
                   subtitle_srt=_binding(subtitle_srt), output_srt=_binding(subtitle_srt),
                   authoritative_audio=_binding(authoritative_audio), audio_duration_seconds=duration,
                   generated_at=datetime.now(timezone.utc).isoformat())
    _validate_confirmed_user_srt(payload, expected_inputs=expected_inputs)
    _atomic_write(receipt_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return receipt_path


def ensure_authoritative_timeline_srt(status_dir: Path, assembly_dir: Path) -> Path | None:
    """Return only a hash-bound production timeline, never an even fallback."""

    status_dir = status_dir.expanduser().resolve()
    assembly_dir = assembly_dir.expanduser().resolve()
    run = _story_run_payload(status_dir)
    if run is not None:
        record = run.get("artifacts", {}).get("authoritative_timeline_receipt")
        if isinstance(record, dict):
            receipt = _current_bound_path(record, "账本权威时间轴回执")
            payload = validate_authoritative_timeline_receipt(
                receipt,
                expected_inputs=run.get("inputs"),
            )
            return Path(payload["output_srt"]["path"])

    native_receipt = assembly_dir / "authoritative_timeline_receipt.json"
    if native_receipt.is_file():
        payload = validate_authoritative_timeline_receipt(
            native_receipt,
            expected_inputs=run.get("inputs") if run is not None else None,
        )
        return Path(payload["output_srt"]["path"])

    alignment_dir = status_dir / "preflight_alignment"
    timings = alignment_dir / "confirmed_line_timings.json"
    metadata_path = alignment_dir / "alignment_run_metadata.json"
    if timings.is_file() or metadata_path.is_file():
        if not timings.is_file() or not metadata_path.is_file():
            raise ValueError("Whisper 对齐证据不完整，禁止降级为均分时间轴")
        subtitle_txt, authoritative_audio, _inputs = _ledger_input_paths(status_dir)
        metadata = _load_json_object(metadata_path, "Whisper 对齐 metadata")
        alignment_audio = Path(str(metadata.get("audio_path") or "")).expanduser()
        if not alignment_audio.is_file():
            raise ValueError("Whisper 对齐 metadata 缺少当前 alignment audio")
        output_srt = alignment_dir / "confirmed_spoken_timeline.srt"
        receipt = alignment_dir / "authoritative_timeline_receipt.json"
        write_authoritative_timeline_receipt(
            receipt_path=receipt,
            source_kind="whisper_confirmed_line_timings",
            timings_path=timings,
            alignment_metadata_path=metadata_path,
            subtitle_txt=subtitle_txt,
            authoritative_audio=authoritative_audio,
            alignment_audio=alignment_audio,
            output_srt=output_srt,
        )
        return output_srt

    fallback_dir = status_dir / "preflight"
    fallback_candidates = (
        fallback_dir / "confirmed_spoken_timeline.srt",
        fallback_dir / "confirmed_line_timings.json",
        fallback_dir / "subtitle_timeline_preflight.json",
    )
    if any(path.is_file() for path in fallback_candidates):
        raise ValueError("只发现均分/even fallback；生产时间轴必须重新完成音频对齐")
    return None


__all__ = [
    "SCHEMA_VERSION",
    "import_confirmed_user_srt",
    "ensure_authoritative_timeline_srt",
    "validate_authoritative_timeline_receipt",
    "write_authoritative_timeline_receipt",
]
