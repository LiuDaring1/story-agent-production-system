from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from story_agent_runtime import file_sha256, now
from story_project import load_manifest, project_paths, write_manifest


FILLER_ONLY = re.compile(r"^[嗯啊呃哦诶唉哎呀这个那个就是然后所以对吧好]+[，。！？、\s]*$")
SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?])")


@dataclass
class TranscriptSegment:
    index: int
    start: float
    end: float
    text: str
    keep: bool = True
    reason: str = ""


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


def merge_keep_intervals(segments: list[TranscriptSegment], *, padding: float = 0.12, max_gap: float = 0.45) -> list[dict[str, float]]:
    kept = [segment for segment in segments if segment.keep]
    if not kept:
        return []
    intervals: list[dict[str, float]] = []
    for segment in kept:
        start = max(0.0, segment.start - padding)
        end = max(start, segment.end + padding)
        if intervals and start - intervals[-1]["end"] <= max_gap:
            intervals[-1]["end"] = max(intervals[-1]["end"], end)
        else:
            intervals.append({"start": round(start, 3), "end": round(end, 3)})
    return intervals


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


def storyboard_lines(segments: list[TranscriptSegment], *, target_seconds: float = 10.0) -> list[str]:
    lines: list[str] = []
    buffer: list[str] = []
    start: float | None = None
    end = 0.0
    for segment in [item for item in segments if item.keep]:
        if start is None:
            start = segment.start
        buffer.append(normalize_spoken_text(segment.text))
        end = segment.end
        joined = "".join(buffer)
        duration = end - start
        semantic_break = bool(re.search(r"[。！？!?]$", joined))
        if duration >= target_seconds or (duration >= 6.0 and semantic_break):
            lines.append(joined)
            buffer = []
            start = None
    if buffer:
        tail = "".join(buffer)
        if lines and start is not None and end - start < 3.0:
            lines[-1] += tail
        else:
            lines.append(tail)
    return [line for line in lines if comparable_text(line)]


def consumer_manuscript(text: str, story_name: str) -> str:
    cleaned = normalize_spoken_text(text)
    cleaned = re.sub(r"^(大家好[，,]?)?我是绵羊姐姐[。！!，,]?", "", cleaned)
    cleaned = re.sub(r"(我的故事讲完了[。！!]?|谢谢大家[。！!]?)$", "", cleaned)
    paragraphs = [part.strip() for part in SENTENCE_SPLIT.split(cleaned) if part.strip()]
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


def concat_kept_video(video: Path, intervals: list[dict[str, float]], output: Path) -> None:
    if not intervals:
        raise RuntimeError("没有可保留的口播区间，拒绝生成空视频。")
    output.parent.mkdir(parents=True, exist_ok=True)
    filters: list[str] = []
    concat_inputs: list[str] = []
    for index, item in enumerate(intervals):
        start, end = item["start"], item["end"]
        filters.append(f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{index}]")
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
            "-crf",
            "17",
            "-preset",
            "medium",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output),
        ]
    )


def build_source_outputs(
    project_dir: Path,
    transcript_payload: dict[str, Any],
    *,
    source_video: Path,
    render_clean_video: bool = True,
    keep_overrides: dict[int, tuple[bool, str]] | None = None,
    text_overrides: dict[int, str] | None = None,
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
    source_segments = [
        TranscriptSegment(
            index=index,
            start=float(item.get("start", 0)),
            end=float(item.get("end", 0)),
            text=str(item.get("text", "")),
        )
        for index, item in enumerate(transcript_payload.get("segments", []), start=1)
    ]
    decided = choose_clean_segments(source_segments)
    for segment in decided:
        if text_overrides and segment.index in text_overrides:
            segment.text = normalize_spoken_text(text_overrides[segment.index])
        if keep_overrides and segment.index in keep_overrides:
            keep, reason = keep_overrides[segment.index]
            segment.keep = keep
            segment.reason = f"review_override:{reason}"
    intervals = merge_keep_intervals(decided)
    decisions_path = work / "edit_decisions.json"
    decisions_payload = {
        "version": 1,
        "created_at": now(),
        "source_video": str(source_video),
        "source_sha256": file_sha256(source_video),
        "policy": "保留最后一次完整表达；删除纯语气词、空段和相邻重录段",
        "segments": [asdict(item) for item in decided],
        "keep_intervals": intervals,
        "text_overrides_source_sha256": text_overrides_source_sha256,
    }
    retimed = retime_kept_segments(decided, intervals)
    decisions_payload["clean_timeline_segments"] = [asdict(item) for item in retimed]
    decisions_path.write_text(json.dumps(decisions_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    kept_text = "".join(normalize_spoken_text(item.text) for item in decided if item.keep)
    lines = storyboard_lines(decided)
    story_text = paths.inputs / f"{slug}_source.txt"
    story_text.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manuscript = paths.inputs / f"{slug}_consumer_manuscript.txt"
    manuscript.write_text(consumer_manuscript(kept_text, story_name), encoding="utf-8")
    srt = paths.inputs / f"{slug}_clean_subtitles.srt"
    write_srt(retimed, srt)
    clean_video = paths.inputs / f"{slug}_greenscreen_clean.mp4"
    clean_audio = paths.inputs / f"{slug}_clean_narration.m4a"
    if render_clean_video:
        concat_kept_video(source_video, intervals, clean_video)
        extract_audio(clean_video, clean_audio)
    manifest.setdefault("inputs", {})["story_text"] = str(story_text)
    manifest["inputs"]["greenscreen_video_original"] = str(source_video)
    if clean_video.exists():
        manifest["inputs"]["greenscreen_video"] = str(clean_video)
    if clean_audio.exists():
        manifest["inputs"]["extracted_narration"] = str(clean_audio)
    manifest.setdefault("outputs", {})["consumer_manuscript"] = str(manuscript)
    manifest["outputs"]["source_edit_decisions"] = str(decisions_path)
    manifest["outputs"]["source_subtitles"] = str(srt)
    write_manifest(paths, manifest)
    return {
        "raw_transcript": raw_path,
        "edit_decisions": decisions_path,
        "story_text": story_text,
        "consumer_manuscript": manuscript,
        "subtitles": srt,
        "clean_video": clean_video,
        "clean_audio": clean_audio,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="从唯一绿幕原片生成可追溯转写、剪辑决定和清洁素材")
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--video", required=True, type=Path)
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
    overrides: dict[int, tuple[bool, str]] = {}
    if args.keep_overrides_json:
        override_payload = json.loads(args.keep_overrides_json.expanduser().read_text(encoding="utf-8"))
        for item in override_payload.get("overrides", []):
            try:
                segment_index = int(item["segment"])
            except (KeyError, TypeError, ValueError):
                continue
            overrides[segment_index] = (bool(item.get("keep", True)), str(item.get("reason", "independent review")))
    text_overrides: dict[int, str] = {}
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
    outputs = build_source_outputs(
        project_dir,
        payload,
        source_video=video,
        render_clean_video=not args.no_render,
        keep_overrides=overrides,
        text_overrides=text_overrides,
        text_overrides_source_sha256=text_overrides_source_sha256,
    )
    print(json.dumps({key: str(value) for key, value in outputs.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
