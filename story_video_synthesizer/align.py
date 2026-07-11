from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .media import probe_duration

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMBA_NUM_THREADS", "1")
os.environ.setdefault("NUMBA_THREADING_LAYER", "workqueue")


PUNCTUATION_RE = re.compile(r"[\s\.,!?;:'\"，。！？；：“”‘’、（）()《》<>【】\[\]…—\-]+")
VISUAL_NOTE_RE = re.compile(r"[（(][^（）()]{0,80}(?:不要|画面|镜头|出现|形象|审核|备注|字幕|水印|人物|角色)[^（）()]{0,120}[）)]")


@dataclass(frozen=True)
class LineTiming:
    index: int
    line: str
    source_start: float
    source_end: float
    duration: float
    timeline_start: float
    timeline_end: float


@dataclass(frozen=True)
class TimedTextToken:
    text: str
    start: float
    end: float


def read_script_lines(script_path: Path) -> list[str]:
    text = script_path.read_text(encoding="utf-8-sig")
    return [line for line in (sanitize_script_line(raw) for raw in text.splitlines()) if line]


def sanitize_script_line(text: str) -> str:
    """Remove production/visual notes that must not enter narration subtitles."""
    cleaned = VISUAL_NOTE_RE.sub("", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def clean_text(text: str) -> str:
    return PUNCTUATION_RE.sub("", text).lower()


def align_script_to_narration(
    script_lines: list[str],
    narration_path: Path,
    whisper_model: str = "base",
    language: str | None = None,
    mode: str = "whisper",
    whisper_model_dir: Path | None = None,
) -> list[LineTiming]:
    script_lines = [sanitize_script_line(line) for line in script_lines]
    if mode == "even":
        return align_evenly(script_lines, narration_path)
    return align_with_whisper(script_lines, narration_path, whisper_model, language, whisper_model_dir)


def align_evenly(script_lines: list[str], narration_path: Path) -> list[LineTiming]:
    total_duration = probe_duration(narration_path)
    clean_lengths = [max(1, len(clean_text(line))) for line in script_lines]
    total_units = sum(clean_lengths)
    cursor = 0.0
    result: list[LineTiming] = []

    for index, (line, units) in enumerate(zip(script_lines, clean_lengths), start=1):
        duration = total_duration * units / total_units
        start = cursor
        end = total_duration if index == len(script_lines) else start + duration
        result.append(_timing(index, line, start, end, cursor))
        cursor += end - start

    return result


def align_with_whisper(
    script_lines: list[str],
    narration_path: Path,
    whisper_model: str,
    language: str | None,
    whisper_model_dir: Path | None,
) -> list[LineTiming]:
    try:
        import whisper
    except ImportError as exc:
        raise RuntimeError("没有找到 openai-whisper。请先安装 requirements.txt 里的依赖。") from exc

    if whisper_model_dir:
        whisper_model_dir.mkdir(parents=True, exist_ok=True)
        model = whisper.load_model(whisper_model, download_root=str(whisper_model_dir))
    else:
        model = whisper.load_model(whisper_model)
    options: dict[str, Any] = {
        "word_timestamps": True,
        "verbose": False,
    }
    if language:
        options["language"] = language
    transcription = model.transcribe(str(narration_path), **options)
    tokens = _tokens_from_transcription(transcription)

    if not tokens:
        raise RuntimeError("Whisper 没有返回可用的词/字时间戳。")

    char_times = _char_times(tokens)
    script_clean_lines = [clean_text(line) for line in script_lines]
    script_clean = "".join(script_clean_lines)
    recognized_clean = "".join(char for char, _, _ in char_times)

    if not script_clean or not recognized_clean:
        raise RuntimeError("台词或识别结果为空，无法对齐。")

    script_to_audio_char = _map_script_chars_to_audio_chars(script_clean, recognized_clean)
    total_audio_chars = len(char_times)
    result: list[LineTiming] = []
    source_cursor_floor = 0.0
    timeline_cursor = 0.0
    script_cursor = 0

    for index, (line, line_clean) in enumerate(zip(script_lines, script_clean_lines), start=1):
        if line_clean:
            start_char = _nearest_audio_char(script_to_audio_char, script_cursor, total_audio_chars)
            end_char = _nearest_audio_char(
                script_to_audio_char,
                script_cursor + len(line_clean) - 1,
                total_audio_chars,
            )
            start = char_times[start_char][1]
            end = char_times[end_char][2]
        else:
            start = source_cursor_floor
            end = source_cursor_floor + 0.5

        start = max(source_cursor_floor, start)
        end = max(start + 0.15, end)
        if index == len(script_lines):
            end = max(end, start + 0.15)

        item = _timing(index, line, start, end, timeline_cursor)
        result.append(item)
        source_cursor_floor = item.source_end
        timeline_cursor = item.timeline_end
        script_cursor += len(line_clean)

    return result


def save_timings(timings: list[LineTiming], path: Path) -> None:
    path.write_text(
        json.dumps([asdict(timing) for timing in timings], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _tokens_from_transcription(transcription: dict[str, Any]) -> list[TimedTextToken]:
    tokens: list[TimedTextToken] = []
    for segment in transcription.get("segments", []):
        words = segment.get("words") or []
        if words:
            for word in words:
                text = str(word.get("word", "")).strip()
                start = word.get("start")
                end = word.get("end")
                if text and start is not None and end is not None:
                    tokens.append(TimedTextToken(text=text, start=float(start), end=float(end)))
        else:
            text = str(segment.get("text", "")).strip()
            start = segment.get("start")
            end = segment.get("end")
            if text and start is not None and end is not None:
                tokens.append(TimedTextToken(text=text, start=float(start), end=float(end)))
    return tokens


def _char_times(tokens: list[TimedTextToken]) -> list[tuple[str, float, float]]:
    chars: list[tuple[str, float, float]] = []
    for token in tokens:
        cleaned = clean_text(token.text)
        if not cleaned:
            continue
        duration = max(0.01, token.end - token.start)
        unit = duration / len(cleaned)
        for offset, char in enumerate(cleaned):
            start = token.start + unit * offset
            end = token.start + unit * (offset + 1)
            chars.append((char, start, end))
    return chars


def _map_script_chars_to_audio_chars(script: str, recognized: str) -> dict[int, int]:
    matcher = SequenceMatcher(a=script, b=recognized, autojunk=False)
    mapping: dict[int, int] = {}

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        script_length = i2 - i1
        recognized_length = j2 - j1
        if script_length <= 0:
            continue

        if tag == "equal":
            for offset in range(script_length):
                mapping[i1 + offset] = min(j1 + offset, len(recognized) - 1)
        elif recognized_length > 0:
            for offset in range(script_length):
                ratio = offset / max(1, script_length - 1)
                mapping[i1 + offset] = min(
                    len(recognized) - 1,
                    j1 + round(ratio * max(0, recognized_length - 1)),
                )

    return mapping


def _nearest_audio_char(mapping: dict[int, int], script_index: int, total_audio_chars: int) -> int:
    if script_index in mapping:
        return _clamp(mapping[script_index], total_audio_chars)

    lower = script_index - 1
    while lower >= 0 and lower not in mapping:
        lower -= 1

    upper = script_index + 1
    max_scan = script_index + total_audio_chars + 1
    while upper <= max_scan and upper not in mapping:
        upper += 1

    if lower in mapping and upper in mapping:
        midpoint = round((mapping[lower] + mapping[upper]) / 2)
        return _clamp(midpoint, total_audio_chars)
    if lower in mapping:
        return _clamp(mapping[lower], total_audio_chars)
    if upper in mapping:
        return _clamp(mapping[upper], total_audio_chars)
    return _clamp(script_index, total_audio_chars)


def _clamp(value: int, total: int) -> int:
    return max(0, min(total - 1, value))


def _timing(index: int, line: str, start: float, end: float, timeline_cursor: float) -> LineTiming:
    duration = max(0.15, end - start)
    return LineTiming(
        index=index,
        line=line,
        source_start=round(start, 3),
        source_end=round(start + duration, 3),
        duration=round(duration, 3),
        timeline_start=round(timeline_cursor, 3),
        timeline_end=round(timeline_cursor + duration, 3),
    )
