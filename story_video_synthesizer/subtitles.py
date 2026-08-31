from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .align import LineTiming, sanitize_script_line


SUBTITLE_PUNCTUATION_RE = re.compile(r"[\s，。！？、；：“”‘’《》【】（）(),.!?;:\"'…—-]+")
SPLIT_PUNCTUATION_RE = re.compile(r"[，。！？、；：,.!?;:]+")
NUMERIC_COMMA_SENTINEL = "\uf000"


@dataclass(frozen=True)
class SubtitleCue:
    index: int
    text: str
    start: float
    end: float


def build_subtitle_cues(
    timings: list[LineTiming],
    max_chars: int = 18,
    *,
    preserve_input_lines: bool = False,
) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    for timing in timings:
        if preserve_input_lines:
            text = str(timing.line).replace("\r", "").replace("\n", "").strip()
            parts = [text] if text else []
        else:
            parts = split_subtitle_text(sanitize_script_line(timing.line), max_chars=max_chars)
        if not parts:
            continue
        weights = [max(1, len(part)) for part in parts]
        total_weight = sum(weights)
        cursor = timing.source_start
        speech_duration = max(0.1, timing.source_end - timing.source_start)

        for part, weight in zip(parts, weights):
            duration = speech_duration * weight / total_weight
            end = timing.source_end if part == parts[-1] else cursor + duration
            cues.append(
                SubtitleCue(
                    index=len(cues) + 1,
                    text=part,
                    start=round(cursor, 3),
                    end=round(max(cursor + 0.12, end), 3),
                )
            )
            cursor = end
    return cues


def write_srt(
    timings: list[LineTiming],
    path: Path,
    max_chars: int = 18,
    *,
    preserve_input_lines: bool = False,
) -> None:
    cues = build_subtitle_cues(
        timings,
        max_chars=max_chars,
        preserve_input_lines=preserve_input_lines,
    )
    blocks: list[str] = []
    for cue in cues:
        blocks.append(
            "\n".join(
                [
                    str(cue.index),
                    f"{_srt_time(cue.start)} --> {_srt_time(cue.end)}",
                    cue.text,
                ]
            )
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def clean_subtitle_text(text: str) -> str:
    return SUBTITLE_PUNCTUATION_RE.sub("", text)


def split_subtitle_text(text: str, max_chars: int = 12) -> list[str]:
    """Project spoken text into balanced, punctuation-free single-line cues.

    Punctuation remains useful as a semantic break before it is removed. Long
    clauses are divided into evenly sized chunks instead of leaving an ugly
    one- or two-character tail, and short neighbouring phrases are merged when
    they still fit the requested one-line limit.
    """

    if max_chars < 2:
        raise ValueError("max_chars must be at least 2")

    # A comma inside a number is a grouping mark, not a subtitle break.  The
    # old generic punctuation split turned ``18,000年`` into an isolated
    # follow-up cue ``000年``, which is especially misleading for children.
    protected = re.sub(
        r"(?<=\d),(?=\d)",
        NUMERIC_COMMA_SENTINEL,
        text,
    )
    raw_parts = [
        part.replace(NUMERIC_COMMA_SENTINEL, ",")
        for part in SPLIT_PUNCTUATION_RE.split(protected)
        if part.strip()
    ]
    if not raw_parts:
        raw_parts = [text]

    chunks: list[str] = []
    for raw_part in raw_parts:
        cleaned = clean_subtitle_text(raw_part)
        if not cleaned:
            continue
        chunk_count = max(1, (len(cleaned) + max_chars - 1) // max_chars)
        base_size, extra = divmod(len(cleaned), chunk_count)
        cursor = 0
        for index in range(chunk_count):
            size = base_size + (1 if index < extra else 0)
            chunks.append(cleaned[cursor : cursor + size])
            cursor += size

    result: list[str] = []
    for chunk in chunks:
        if result and len(result[-1]) + len(chunk) <= max_chars:
            result[-1] += chunk
        else:
            result.append(chunk)
    return result


def _srt_time(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},{milliseconds:03}"
