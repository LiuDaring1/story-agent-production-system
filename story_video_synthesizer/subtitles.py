from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .align import LineTiming, sanitize_script_line


SUBTITLE_PUNCTUATION_RE = re.compile(r"[\s，。！？、；：“”‘’《》【】（）(),.!?;:\"'…—-]+")
SPLIT_PUNCTUATION_RE = re.compile(r"[，。！？、；：,.!?;:]+")


@dataclass(frozen=True)
class SubtitleCue:
    index: int
    text: str
    start: float
    end: float


def build_subtitle_cues(timings: list[LineTiming], max_chars: int = 18) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    for timing in timings:
        parts = _split_line(sanitize_script_line(timing.line), max_chars=max_chars)
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


def write_srt(timings: list[LineTiming], path: Path, max_chars: int = 18) -> None:
    cues = build_subtitle_cues(timings, max_chars=max_chars)
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


def _split_line(text: str, max_chars: int) -> list[str]:
    raw_parts = [part for part in SPLIT_PUNCTUATION_RE.split(text) if part.strip()]
    if not raw_parts:
        raw_parts = [text]

    result: list[str] = []
    for raw_part in raw_parts:
        cleaned = clean_subtitle_text(raw_part)
        if not cleaned:
            continue
        while len(cleaned) > max_chars:
            result.append(cleaned[:max_chars])
            cleaned = cleaned[max_chars:]
        if cleaned:
            result.append(cleaned)
    return result


def _srt_time(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},{milliseconds:03}"
