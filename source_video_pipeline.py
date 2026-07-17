from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from story_agent_runtime import file_sha256, now, record_derived_input
from story_project import load_manifest, project_paths, write_manifest


# 只自动删除明确的犹豫占位词。啊呜、哎哟、哦、好等可能是表演内容，必须保留。
FILLER_ONLY = re.compile(r"^(?:[嗯呃额]+|嗯啊|这个|那个|就是|然后|所以|对吧)[，。！？、\s]*$")
CLAUSE_PATTERN = re.compile(r'.+?(?:[。！？!?；;：:](?:[”’」』】"“‘「『【])?|$)')
SOFT_CLAUSE_PATTERN = re.compile(r'.+?(?:[，,、](?:[”’」』】"“‘「『【])?|$)')


@dataclass
class TranscriptSegment:
    index: int
    start: float
    end: float
    text: str
    keep: bool = True
    reason: str = ""
    source_start: float | None = None
    source_end: float | None = None
    source_text: str = ""


def normalize_spoken_text(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\s+", "", text)
    text = text.replace("妳", "你")
    text = re.sub(r"([。！？!?，,])\1+", r"\1", text)
    return text


def comparable_text(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", normalize_spoken_text(text)).lower()


def is_restart_or_duplicate(earlier: str, later: str) -> bool:
    a, b = comparable_text(earlier), comparable_text(later)
    if len(a) < 3 or len(b) < 3:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 4 and longer.startswith(shorter):
        return True
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio >= 0.78


def choose_clean_segments(segments: list[TranscriptSegment], *, restart_window_seconds: float = 24.0) -> list[TranscriptSegment]:
    decided = [TranscriptSegment(**asdict(segment)) for segment in segments]
    for segment in decided:
        clean = comparable_text(segment.text)
        if not clean:
            segment.keep = False
            segment.reason = "empty"
        elif FILLER_ONLY.match(normalize_spoken_text(segment.text)):
            segment.keep = False
            segment.reason = "filler_only"

    for index, earlier in enumerate(decided):
        if not earlier.keep:
            continue
        for later in decided[index + 1 : index + 4]:
            if later.start - earlier.end > restart_window_seconds:
                break
            if not later.keep:
                continue
            if is_restart_or_duplicate(earlier.text, later.text):
                earlier.keep = False
                earlier.reason = f"repeated_by_segment_{later.index};keep_last_complete_take"
                break
    return decided


def merge_keep_intervals(segments: list[TranscriptSegment], *, padding: float = 0.35, max_gap: float = 1.25) -> list[dict[str, float]]:
    """Build conservative keep ranges without treating ASR silence as disposable.

    Whisper commonly omits laughter, animal sounds, breaths and mime-only acting.
    A long gap between two *kept* transcript segments is therefore not evidence
    that the picture or sound in that gap should be removed.  Start a new keep
    range only when the gap contains a segment that was explicitly rejected as a
    failed take/filler.  ``max_gap`` remains as a compatibility argument for old
    callers, but it is no longer used as an automatic silence cutter.
    """
    kept = [segment for segment in segments if segment.keep]
    if not kept:
        return []
    ordered = sorted(segments, key=lambda item: (item.start, item.end, item.index))
    intervals: list[dict[str, float]] = []
    for segment in kept:
        start = max(0.0, segment.start - padding)
        end = max(start, segment.end + padding)
        rejected_between = [
            candidate
            for candidate in ordered
            if intervals
            and not candidate.keep
            and candidate.end > intervals[-1]["end"] - padding
            and candidate.start < start + padding
        ]
        if intervals and not rejected_between:
            intervals[-1]["end"] = max(intervals[-1]["end"], end)
        elif intervals:
            # Split around explicit rejects; never infer a cut from an ASR gap.
            cut_start = min(item.start for item in rejected_between)
            cut_end = max(item.end for item in rejected_between)
            intervals[-1]["end"] = round(max(intervals[-1]["start"], cut_start - padding), 3)
            intervals.append({"start": round(max(0.0, cut_end + padding), 3), "end": round(end, 3)})
        else:
            intervals.append({"start": round(start, 3), "end": round(end, 3)})
    return intervals


def edit_rhythm_guard(segments: list[TranscriptSegment], intervals: list[dict[str, float]]) -> dict[str, Any]:
    """Flag jump-cut-heavy edits before they can silently become a final master."""
    source_duration = max((segment.end for segment in segments), default=0.0)
    cut_count = max(0, len(intervals) - 1)
    max_cuts = max(4, int(source_duration // 30) + 1)
    removed_expressive = [
        segment.index
        for segment in segments
        if not segment.keep and re.search(r"啊呜|哎哟|哎呀|天哪|哈哈|呜呜", segment.text)
    ]
    return {
        "passed": cut_count <= max_cuts and not removed_expressive,
        "cut_count": cut_count,
        "max_automatic_cuts": max_cuts,
        "source_duration": round(source_duration, 3),
        "removed_expressive_segments": removed_expressive,
        "policy": "只删除明确失败重录；保留呼吸、角色转换停顿、拟声词和句尾余量。超过阈值必须人工/独立复核。",
    }


def retime_kept_segments(segments: list[TranscriptSegment], intervals: list[dict[str, float]]) -> list[TranscriptSegment]:
    retimed: list[TranscriptSegment] = []
    elapsed = 0.0
    for interval in intervals:
        start, end = interval["start"], interval["end"]
        for segment in segments:
            if not segment.keep or segment.end < start or segment.start > end:
                continue
            new_start = elapsed + max(0.0, segment.start - start)
            new_end = elapsed + min(end - start, segment.end - start)
            if new_end <= new_start:
                continue
            retimed.append(
                TranscriptSegment(
                    index=segment.index,
                    start=round(new_start, 3),
                    end=round(new_end, 3),
                    text=segment.text,
                    keep=True,
                    reason=segment.reason,
                    source_start=segment.source_start,
                    source_end=segment.source_end,
                    source_text=segment.source_text,
                )
            )
        elapsed += end - start
    seen: set[int] = set()
    unique: list[TranscriptSegment] = []
    for segment in retimed:
        if segment.index not in seen:
            unique.append(segment)
            seen.add(segment.index)
    return unique


def _timed_text_units(segment: TranscriptSegment, *, max_unit_seconds: float = 12.0) -> list[dict[str, Any]]:
    text = normalize_spoken_text(segment.text)
    if not text:
        return []
    pieces = [piece for piece in CLAUSE_PATTERN.findall(text) if comparable_text(piece)]
    if not pieces:
        pieces = [text]
    duration = max(0.001, segment.end - segment.start)
    weights = [max(1, len(comparable_text(piece))) for piece in pieces]
    if len(pieces) == 1 and duration > max_unit_seconds:
        softer = [piece for piece in SOFT_CLAUSE_PATTERN.findall(text) if comparable_text(piece)]
        if len(softer) > 1:
            pieces = softer
            weights = [max(1, len(comparable_text(piece))) for piece in pieces]
    total_weight = sum(weights)
    cursor = segment.start
    units: list[dict[str, Any]] = []
    for index, (piece, weight) in enumerate(zip(pieces, weights)):
        end = segment.end if index == len(pieces) - 1 else cursor + duration * weight / total_weight
        units.append({"start": round(cursor, 3), "end": round(end, 3), "text": piece, "source_segment": segment.index})
        cursor = end
    return units


def storyboard_plan(
    segments: list[TranscriptSegment],
    *,
    target_seconds: float = 10.0,
    max_seconds: float = 12.5,
) -> list[dict[str, Any]]:
    units = [unit for segment in segments if segment.keep for unit in _timed_text_units(segment)]
    plan: list[dict[str, Any]] = []
    buffer: list[dict[str, Any]] = []

    def flush() -> None:
        if not buffer:
            return
        plan.append(
            {
                "index": len(plan) + 1,
                "start": round(float(buffer[0]["start"]), 3),
                "end": round(float(buffer[-1]["end"]), 3),
                "duration": round(float(buffer[-1]["end"]) - float(buffer[0]["start"]), 3),
                "text": "".join(str(item["text"]) for item in buffer),
                "source_segments": list(dict.fromkeys(int(item["source_segment"]) for item in buffer)),
            }
        )
        buffer.clear()

    for unit in units:
        proposed_start = float(buffer[0]["start"]) if buffer else float(unit["start"])
        proposed_duration = float(unit["end"]) - proposed_start
        current_duration = float(buffer[-1]["end"]) - proposed_start if buffer else 0.0
        if buffer and proposed_duration > max_seconds and current_duration >= 5.0:
            flush()
        buffer.append(unit)
        duration = float(buffer[-1]["end"]) - float(buffer[0]["start"])
        semantic_break = bool(re.search(r"[。！？!?；;]$", str(buffer[-1]["text"])))
        if duration >= target_seconds or (duration >= 6.0 and semantic_break):
            flush()
    flush()
    if len(plan) >= 2 and float(plan[-1]["duration"]) < 5.0:
        combined_duration = float(plan[-1]["end"]) - float(plan[-2]["start"])
        if combined_duration <= max_seconds + 1.5:
            tail = plan.pop()
            plan[-1]["end"] = tail["end"]
            plan[-1]["duration"] = round(combined_duration, 3)
            plan[-1]["text"] = str(plan[-1]["text"]) + str(tail["text"])
            plan[-1]["source_segments"] = list(dict.fromkeys([*plan[-1]["source_segments"], *tail["source_segments"]]))
    for index, item in enumerate(plan, start=1):
        item["index"] = index
    return plan


def storyboard_lines(segments: list[TranscriptSegment], *, target_seconds: float = 10.0) -> list[str]:
    return [str(item["text"]) for item in storyboard_plan(segments, target_seconds=target_seconds)]


def manuscript_paragraphs(text: str) -> list[str]:
    """Split prose at sentence ends without breaking quoted dialogue."""
    open_to_close = {"“": "”", "‘": "’", "「": "」", "『": "』", "【": "】"}
    close_stack: list[str] = []
    ascii_double_quote_open = False
    paragraphs: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        paragraph = "".join(buffer).strip()
        if paragraph:
            paragraphs.append(paragraph)
        buffer.clear()

    for character in text:
        buffer.append(character)
        if character in open_to_close:
            close_stack.append(open_to_close[character])
            continue
        if close_stack and character == close_stack[-1]:
            close_stack.pop()
            if not close_stack and re.search(r'[。！？!?][”’」』】"]*$', "".join(buffer)):
                flush()
            continue
        if character == '"' and not close_stack:
            ascii_double_quote_open = not ascii_double_quote_open
            if not ascii_double_quote_open and re.search(r'[。！？!?]"$', "".join(buffer)):
                flush()
            continue
        if character in "。！？!?" and not close_stack and not ascii_double_quote_open:
            flush()
    flush()
    return paragraphs


def consumer_manuscript(text: str, story_name: str) -> str:
    cleaned = normalize_spoken_text(text)
    cleaned = re.sub(r"^(大家好[，,]?)?我是绵羊姐姐[。！!，,]?", "", cleaned)
    cleaned = re.sub(r"(我的故事讲完了[。！!]?|谢谢大家[。！!]?)$", "", cleaned)
    # A closing quotation mark belongs to the sentence before it. Splitting only
    # after punctuation produced consumer manuscripts with standalone `”`
    # paragraphs, which is both visually broken and ambiguous for readers.
    paragraphs = manuscript_paragraphs(cleaned)
    return f"故事文稿：{story_name}\n\n" + "\n\n".join(paragraphs) + "\n"


def seconds_to_srt(value: float) -> str:
    millis = max(0, int(round(value * 1000)))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    seconds, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def write_srt(segments: list[TranscriptSegment], path: Path) -> None:
    blocks: list[str] = []
    for index, segment in enumerate((item for item in segments if item.keep), start=1):
        blocks.append(
            f"{index}\n{seconds_to_srt(segment.start)} --> {seconds_to_srt(segment.end)}\n{normalize_spoken_text(segment.text)}"
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def transcribe(audio: Path, *, model_name: str = "base", model_dir: Path | None = None, language: str = "zh") -> dict[str, Any]:
    try:
        import whisper
    except ImportError as exc:
        raise RuntimeError("缺少 openai-whisper，无法从绿幕视频生成文本。") from exc
    kwargs: dict[str, Any] = {}
    if model_dir is not None:
        model_dir.mkdir(parents=True, exist_ok=True)
        kwargs["download_root"] = str(model_dir)
    model = whisper.load_model(model_name, **kwargs)
    return model.transcribe(str(audio), language=language, word_timestamps=True, verbose=False)


def run_command(command: list[str]) -> None:
    process = subprocess.run(command, text=True, capture_output=True)
    if process.returncode != 0:
        raise RuntimeError((process.stderr or process.stdout or "命令执行失败").strip())


def extract_audio(video: Path, audio: Path) -> None:
    audio.parent.mkdir(parents=True, exist_ok=True)
    run_command(["ffmpeg", "-y", "-i", str(video), "-vn", "-c:a", "aac", "-b:a", "192k", str(audio)])


def media_duration(path: Path) -> float:
    process = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        text=True,
        capture_output=True,
    )
    if process.returncode != 0:
        raise RuntimeError(f"无法读取媒体时长：{path}: {process.stderr.strip()}")
    return float(process.stdout.strip())


def write_source_qa_report(
    project_dir: Path,
    *,
    source_video: Path,
    decisions_path: Path,
    clean_video: Path,
    clean_audio: Path,
    color_lut: Path | None,
) -> dict[str, Any]:
    paths = project_paths(project_dir)
    errors: list[str] = []
    try:
        decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        decisions = {}
        errors.append(f"剪辑决定 JSON 无效：{exc}")
    expected_duration = sum(
        max(0.0, float(item.get("end", 0)) - float(item.get("start", 0)))
        for item in decisions.get("keep_intervals", [])
        if isinstance(item, dict)
    )
    artifacts: dict[str, dict[str, Any]] = {}
    for key, artifact in (
        ("source_video", source_video),
        ("edit_decisions", decisions_path),
        ("clean_video", clean_video),
        ("clean_audio", clean_audio),
        ("color_lut", color_lut),
    ):
        if artifact is None:
            continue
        if not artifact.is_file() or artifact.stat().st_size <= 0:
            errors.append(f"缺少有效文件：{key}: {artifact}")
            continue
        stat = artifact.stat()
        artifacts[key] = {
            "path": str(artifact),
            "sha256": file_sha256(artifact),
            "bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    if artifacts.get("source_video") and decisions.get("source_sha256") != artifacts["source_video"]["sha256"]:
        errors.append("剪辑决定绑定的原片 SHA-256 与当前原片不一致")
    if color_lut and artifacts.get("color_lut") and decisions.get("color_lut_sha256") != artifacts["color_lut"]["sha256"]:
        errors.append("剪辑决定绑定的 LUT SHA-256 与当前 LUT 不一致")
    durations: dict[str, float] = {"expected_from_keep_intervals": round(expected_duration, 3)}
    for key, artifact in (("clean_video", clean_video), ("clean_audio", clean_audio)):
        if key not in artifacts:
            continue
        try:
            durations[key] = round(media_duration(artifact), 3)
        except (RuntimeError, ValueError) as exc:
            errors.append(str(exc))
    for key in ("clean_video", "clean_audio"):
        actual = durations.get(key)
        if actual is not None and abs(actual - expected_duration) > 0.5:
            errors.append(f"{key} 时长 {actual:.3f}s 与保留区间 {expected_duration:.3f}s 不一致")
    report = {
        "version": 1,
        "generated_at": now(),
        "passed": not errors and expected_duration > 0,
        "errors": errors,
        "durations": durations,
        "artifacts": artifacts,
    }
    report_path = paths.status / "qa_source_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def ffmpeg_filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def concat_kept_video(
    video: Path,
    intervals: list[dict[str, float]],
    output: Path,
    *,
    color_lut: Path | None = None,
    working_max_width: int = 1920,
) -> None:
    if not intervals:
        raise RuntimeError("没有可保留的口播区间，拒绝生成空视频。")
    output.parent.mkdir(parents=True, exist_ok=True)
    filters: list[str] = []
    concat_inputs: list[str] = []
    for index, item in enumerate(intervals):
        start, end = item["start"], item["end"]
        color_filter = f",lut3d=file='{ffmpeg_filter_path(color_lut)}'" if color_lut else ""
        scale_filter = f",scale=w='min({working_max_width},iw)':h=-2:flags=lanczos" if working_max_width > 0 else ""
        filters.append(f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS{color_filter}{scale_filter}[v{index}]")
        filters.append(f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{index}]")
        concat_inputs.append(f"[v{index}][a{index}]")
    graph = ";".join(filters + [f"{''.join(concat_inputs)}concat=n={len(intervals)}:v=1:a=1[v][a]"])
    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-filter_complex",
            graph,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "15",
            "-preset",
            "medium",
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            str(output),
        ]
    )


def render_edit_proxy(master_video: Path, output: Path, max_width: int) -> None:
    """Create a lightweight review/edit proxy while the master stays source-resolution."""
    if max_width <= 0:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            "ffmpeg", "-y", "-i", str(master_video),
            "-vf", f"scale=w='min({max_width},iw)':h=-2:flags=lanczos",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output),
        ]
    )


def build_source_outputs(
    project_dir: Path,
    transcript_payload: dict[str, Any],
    *,
    source_video: Path,
    color_lut: Path | None = None,
    working_max_width: int = 1920,
    proxy_max_width: int = 0,
    render_clean_video: bool = True,
    keep_overrides: dict[int, tuple[bool, str] | dict[str, Any]] | None = None,
    text_overrides: dict[int, str] | None = None,
    text_insertions: list[dict[str, Any]] | None = None,
    text_overrides_source_sha256: str = "",
) -> dict[str, Path]:
    paths = project_paths(project_dir)
    manifest = load_manifest(paths) or {}
    story = manifest.get("story", {})
    slug = str(story.get("slug") or "story")
    story_name = str(story.get("name") or "新故事")
    work = paths.status / "source_edit"
    work.mkdir(parents=True, exist_ok=True)
    raw_path = work / "raw_transcript.json"
    raw_path.write_text(json.dumps(transcript_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    source_segments: list[TranscriptSegment] = []
    for index, item in enumerate(transcript_payload.get("segments", []), start=1):
        words = item.get("words", []) if isinstance(item.get("words"), list) else []
        word_starts = [float(word["start"]) for word in words if isinstance(word, dict) and word.get("start") is not None]
        word_ends = [float(word["end"]) for word in words if isinstance(word, dict) and word.get("end") is not None]
        item_start = float(item.get("start", 0))
        item_end = float(item.get("end", 0))
        source_start = min([item_start, *word_starts]) if word_starts else item_start
        source_end = max([item_end, *word_ends]) if word_ends else item_end
        source_segments.append(
            TranscriptSegment(
                index=index,
                start=source_start,
                end=source_end,
                text=str(item.get("text", "")),
                source_start=source_start,
                source_end=source_end,
                source_text=str(item.get("text", "")),
            )
        )
    next_index = len(source_segments) + 1
    for offset, insertion in enumerate(text_insertions or []):
        start = float(insertion.get("start", -1))
        end = float(insertion.get("end", -1))
        text = normalize_spoken_text(str(insertion.get("text", "")))
        if start < 0 or end <= start or not text:
            raise ValueError(f"audio-grounded insertion {offset + 1} 缺少有效 start/end/text")
        source_segments.append(
            TranscriptSegment(
                index=next_index + offset,
                start=start,
                end=end,
                text=text,
                source_start=start,
                source_end=end,
                source_text=text,
                reason="audio_grounded_asr_omission",
            )
        )
    source_segments.sort(key=lambda item: (item.start, item.end, item.index))
    decided = choose_clean_segments(source_segments)
    for segment in decided:
        if text_overrides and segment.index in text_overrides:
            segment.text = normalize_spoken_text(text_overrides[segment.index])
        if keep_overrides and segment.index in keep_overrides:
            override = keep_overrides[segment.index]
            if isinstance(override, tuple):
                keep, reason = override
                trim_start = None
                trim_end = None
                clean_text = ""
            else:
                keep = bool(override.get("keep", True))
                reason = str(override.get("reason", "independent review"))
                trim_start = override.get("trim_start")
                trim_end = override.get("trim_end")
                clean_text = str(override.get("text", "")).strip()
            original_start = float(segment.source_start if segment.source_start is not None else segment.start)
            original_end = float(segment.source_end if segment.source_end is not None else segment.end)
            if trim_start is not None:
                segment.start = float(trim_start)
            if trim_end is not None:
                segment.end = float(trim_end)
            tolerance = 0.001
            if (
                segment.start < original_start - tolerance
                or segment.end > original_end + tolerance
                or segment.end <= segment.start
            ):
                raise ValueError(
                    f"segment {segment.index} 的精确剪辑区间 {segment.start:.3f}-{segment.end:.3f} "
                    f"超出原区间 {original_start:.3f}-{original_end:.3f}"
                )
            segment.start = max(original_start, segment.start)
            segment.end = min(original_end, segment.end)
            if clean_text:
                segment.text = normalize_spoken_text(clean_text)
            segment.keep = keep
            segment.reason = f"review_override:{reason}"
    intervals = merge_keep_intervals(decided)
    decisions_path = work / "edit_decisions.json"
    decisions_payload = {
        "version": 1,
        "created_at": now(),
        "source_video": str(source_video),
        "source_sha256": file_sha256(source_video),
        "color_lut": str(color_lut) if color_lut else "",
        "color_lut_sha256": file_sha256(color_lut) if color_lut else "",
        "policy": "保守剪辑：只删除可确认的失败重录；ASR 无字区默认属于表演，保留自然停顿、呼吸、角色切换、拟声词、笑声、哼唱和完整句尾",
        "segments": [asdict(item) for item in decided],
        "keep_intervals": intervals,
        "rhythm_guard": edit_rhythm_guard(decided, intervals),
        "text_overrides_source_sha256": text_overrides_source_sha256,
    }
    retimed = retime_kept_segments(decided, intervals)
    decisions_payload["clean_timeline_segments"] = [asdict(item) for item in retimed]
    decisions_payload["storyboard_segments"] = storyboard_plan(retimed)
    decisions_path.write_text(json.dumps(decisions_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    kept_text = "".join(normalize_spoken_text(item.text) for item in decided if item.keep)
    lines = [str(item["text"]) for item in decisions_payload["storyboard_segments"]]
    story_text = paths.inputs / f"{slug}_source.txt"
    story_text.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manuscript = paths.inputs / f"{slug}_consumer_manuscript.txt"
    manuscript.write_text(consumer_manuscript(kept_text, story_name), encoding="utf-8")
    srt = paths.inputs / f"{slug}_clean_subtitles.srt"
    write_srt(retimed, srt)
    clean_video = paths.inputs / f"{slug}_greenscreen_clean.mp4"
    clean_audio = paths.inputs / f"{slug}_clean_narration.m4a"
    edit_proxy = work / f"{slug}_edit_proxy.mp4"
    if render_clean_video:
        concat_kept_video(
            source_video,
            intervals,
            clean_video,
            color_lut=color_lut,
            working_max_width=working_max_width,
        )
        extract_audio(clean_video, clean_audio)
        render_edit_proxy(clean_video, edit_proxy, proxy_max_width)
    manifest.setdefault("inputs", {})["story_text"] = str(story_text)
    manifest["inputs"]["greenscreen_video_original"] = str(source_video)
    if color_lut:
        manifest["inputs"]["color_lut"] = str(color_lut)
    if clean_video.exists():
        manifest["inputs"]["greenscreen_video"] = str(clean_video)
    if clean_audio.exists():
        manifest["inputs"]["extracted_narration"] = str(clean_audio)
    source_sha256 = file_sha256(source_video)
    decisions_sha256 = file_sha256(decisions_path)
    record_derived_input(
        manifest,
        role="story_text",
        path=story_text,
        source_sha256=source_sha256,
        decisions_sha256=decisions_sha256,
    )
    record_derived_input(
        manifest,
        role="clean_greenscreen_video",
        path=clean_video,
        source_sha256=source_sha256,
        decisions_sha256=decisions_sha256,
    )
    record_derived_input(
        manifest,
        role="clean_narration",
        path=clean_audio,
        source_sha256=source_sha256,
        decisions_sha256=decisions_sha256,
    )
    manifest.setdefault("outputs", {})["consumer_manuscript"] = str(manuscript)
    manifest["outputs"]["source_edit_decisions"] = str(decisions_path)
    manifest["outputs"]["source_subtitles"] = str(srt)
    if edit_proxy.exists():
        manifest["outputs"]["source_edit_proxy"] = str(edit_proxy)
    source_qa = write_source_qa_report(
        project_dir,
        source_video=source_video,
        decisions_path=decisions_path,
        clean_video=clean_video,
        clean_audio=clean_audio,
        color_lut=color_lut,
    )
    source_qa_path = paths.status / "qa_source_report.json"
    manifest.setdefault("qa", {})["source"] = str(source_qa_path)
    write_manifest(paths, manifest)
    if (render_clean_video or clean_video.exists() or clean_audio.exists()) and not source_qa["passed"]:
        raise RuntimeError("源视频机器 QA 未通过：" + "；".join(source_qa["errors"]))
    return {
        "raw_transcript": raw_path,
        "edit_decisions": decisions_path,
        "story_text": story_text,
        "consumer_manuscript": manuscript,
        "subtitles": srt,
        "clean_video": clean_video,
        "clean_audio": clean_audio,
        "edit_proxy": edit_proxy,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="从唯一绿幕原片生成可追溯转写、剪辑决定和清洁素材")
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--lut", type=Path, help="可选 .cube 输入 LUT；应用于清洁绿幕视频并记录哈希")
    parser.add_argument("--working-max-width", type=int, default=0, help="清洁母版最大宽度；0 表示保留原始分辨率（推荐）")
    parser.add_argument("--proxy-max-width", type=int, default=1280, help="审核/预览代理文件最大宽度；0 表示不生成")
    parser.add_argument("--transcript-json", type=Path, help="使用已有 Whisper JSON，便于复跑和测试")
    parser.add_argument("--whisper-model", default="base")
    parser.add_argument("--whisper-model-dir", type=Path)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--keep-overrides-json", type=Path, help="独立审核后的 segment 保留/删除覆盖 JSON")
    parser.add_argument("--text-overrides-json", type=Path, help="独立校对后的 segment 文本覆盖 JSON")
    parser.add_argument("--no-render", action="store_true", help="只生成文本和决策，不渲染清洁视频")
    args = parser.parse_args()
    project_dir = args.project_dir.expanduser()
    video = args.video.expanduser()
    paths = project_paths(project_dir)
    audio = paths.inputs / "source_extracted_audio.m4a"
    if args.transcript_json:
        payload = json.loads(args.transcript_json.expanduser().read_text(encoding="utf-8"))
    else:
        extract_audio(video, audio)
        payload = transcribe(
            audio,
            model_name=args.whisper_model,
            model_dir=args.whisper_model_dir.expanduser() if args.whisper_model_dir else None,
            language=args.language,
        )
    overrides: dict[int, tuple[bool, str] | dict[str, Any]] = {}
    if args.keep_overrides_json:
        override_payload = json.loads(args.keep_overrides_json.expanduser().read_text(encoding="utf-8"))
        for item in override_payload.get("overrides", []):
            try:
                segment_index = int(item["segment"])
            except (KeyError, TypeError, ValueError):
                continue
            overrides[segment_index] = {
                "keep": bool(item.get("keep", True)),
                "reason": str(item.get("reason", "independent review")),
                "trim_start": item.get("trim_start"),
                "trim_end": item.get("trim_end"),
                "text": str(item.get("text", "")),
            }
    text_overrides: dict[int, str] = {}
    text_insertions: list[dict[str, Any]] = []
    text_overrides_source_sha256 = ""
    if args.text_overrides_json:
        text_overrides_source_sha256 = file_sha256(args.text_overrides_json.expanduser())
        text_payload = json.loads(args.text_overrides_json.expanduser().read_text(encoding="utf-8"))
        for item in text_payload.get("segments", []):
            try:
                segment_index = int(item["segment"])
            except (KeyError, TypeError, ValueError):
                continue
            corrected = str(item.get("corrected_text", "")).strip()
            if corrected:
                text_overrides[segment_index] = corrected
        raw_segments = [item for item in payload.get("segments", []) if isinstance(item, dict)]
        for insertion_index, item in enumerate(text_payload.get("insertions", []), start=1):
            if not isinstance(item, dict):
                raise ValueError(f"insertions[{insertion_index}] 必须是对象")
            start = float(item.get("start", -1))
            end = float(item.get("end", -1))
            corrected = str(item.get("text", "")).strip()
            if start < 0 or end <= start or not corrected:
                raise ValueError(f"insertions[{insertion_index}] 缺少有效 start/end/text")
            if any(float(segment.get("start", 0)) < end and float(segment.get("end", 0)) > start for segment in raw_segments):
                raise ValueError(f"insertions[{insertion_index}] 与已有 ASR segment 重叠；应修正对应 corrected_text")
            text_insertions.append({"start": start, "end": end, "text": corrected})
    outputs = build_source_outputs(
        project_dir,
        payload,
        source_video=video,
        color_lut=args.lut.expanduser() if args.lut else None,
        working_max_width=max(0, args.working_max_width),
        proxy_max_width=max(0, args.proxy_max_width),
        render_clean_video=not args.no_render,
        keep_overrides=overrides,
        text_overrides=text_overrides,
        text_insertions=text_insertions,
        text_overrides_source_sha256=text_overrides_source_sha256,
    )
    print(json.dumps({key: str(value) for key, value in outputs.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
