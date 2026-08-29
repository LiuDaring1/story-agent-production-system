#!/usr/bin/env python3
"""Assemble approved semantic R2V shots onto the authoritative story timeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str]) -> None:
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(f"命令失败（{completed.returncode}）：{' '.join(command)}\n{completed.stderr[-4000:]}")


def duration(path: Path, ffprobe: str) -> float:
    completed = subprocess.run([
        ffprobe, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
    ], text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(f"无法读取时长：{path}\n{completed.stderr[-2000:]}")
    value = float(completed.stdout.strip())
    if value <= 0:
        raise ValueError(f"时长无效：{path}")
    return value


def load_plan(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("shots"), list):
        raise ValueError("R2V 计划缺少 shots")
    return payload


def encode_segment(
    *,
    source: Path,
    output: Path,
    target_duration: float,
    target_frames: int,
    ffmpeg: str,
    source_duration: float,
    trim_anchor: str = "start",
    explicit_trim_start: float | None = None,
) -> dict[str, Any]:
    """Fill one timeline window without freezing, looping, or borrowing another shot.

    Short sources are uniformly slowed.  Long sources are trimmed without
    speeding up; the reviewed trim anchor decides which portion survives.
    """
    if target_duration <= 0 or target_frames <= 0 or source_duration <= 0:
        raise ValueError("片段时长无效")
    spatial_normalize = (
        "scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    )
    if source_duration > target_duration + 0.02:
        removable = source_duration - target_duration
        if trim_anchor == "start":
            trim_start = 0.0
        elif trim_anchor == "center":
            trim_start = removable / 2
        elif trim_anchor == "end":
            trim_start = removable
        elif trim_anchor == "explicit":
            if explicit_trim_start is None:
                raise ValueError("explicit trim 需要 start_second")
            trim_start = explicit_trim_start
        else:
            raise ValueError(f"未知 trim anchor：{trim_anchor}")
        if trim_start < 0 or trim_start + target_duration > source_duration + 0.02:
            raise ValueError("trim 区间超出源视频")
        vf = (
            f"trim=start={trim_start:.6f},setpts=PTS-STARTPTS,"
            f"{spatial_normalize},fps=30"
        )
        timing = {
            "timing_strategy": "trim_without_speedup",
            "trim_start": trim_start,
            "trim_duration": target_duration,
            "retime_factor": 1.0,
        }
    else:
        speed = target_duration / source_duration
        vf = (
            f"{spatial_normalize},setpts=PTS*{speed:.12f},fps=30"
        )
        timing = {
            "timing_strategy": "uniform_slowdown" if speed > 1.002 else "duration_match",
            "trim_start": 0.0,
            "trim_duration": source_duration,
            "retime_factor": speed,
        }
    run([
        ffmpeg, "-y", "-i", str(source), "-vf", vf, "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", "30", "-frames:v", str(target_frames),
        "-movflags", "+faststart", str(output),
    ])
    return {**timing, "encoded_frame_count": target_frames, "encoded_duration": target_frames / 30.0}


def assembly_trim_policy(shot: dict[str, Any]) -> tuple[str, float | None]:
    policy = shot.get("assembly_trim")
    if policy is None:
        return "start", None
    if not isinstance(policy, dict):
        raise ValueError(f"{shot.get('shot_id')}: assembly_trim 必须是对象")
    anchor = str(policy.get("anchor") or "")
    start = policy.get("start_second")
    return anchor, float(start) if start is not None else None


def validate_contiguous_timeline(shots: list[dict[str, Any]], audio_duration: float) -> None:
    for index, shot in enumerate(shots[:-1]):
        end = float(shot.get("source_end") or 0.0)
        next_start = float(shots[index + 1].get("source_start") or 0.0)
        gap = next_start - end
        if abs(gap) > 0.08:
            raise ValueError(
                f"{shot.get('shot_id')}→{shots[index + 1].get('shot_id')} 时间轴存在 {gap:.3f}s 空档/重叠；"
                "必须在导演计划中把停顿分配给相邻镜头，禁止用定帧补齐"
            )
    final_end = float(shots[-1].get("source_end") or 0.0)
    if abs(audio_duration - final_end) > 0.08:
        raise ValueError("末镜头没有覆盖到权威音频结尾，禁止用定帧补齐")


def main() -> int:
    parser = argparse.ArgumentParser(description="按权威音频时间轴拼装完整 R2V 故事视觉母版")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--videos-dir", required=True, type=Path)
    parser.add_argument("--title-video", required=True, type=Path)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--decisions", required=True, type=Path)
    parser.add_argument("--clips-dir", required=True, type=Path)
    parser.add_argument("--ppt-plan", required=True, type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument(
        "--reuse-existing-clips",
        action="store_true",
        help="兼容旧命令；为避免复用历史定帧/循环片段，当前版本仍从已审核源视频重新编码",
    )
    args = parser.parse_args()

    plan_path = args.plan.expanduser()
    videos_dir = args.videos_dir.expanduser()
    title_video = args.title_video.expanduser()
    audio_path = args.audio.expanduser()
    output_path = args.output.expanduser()
    decisions_path = args.decisions.expanduser()
    clips_dir = args.clips_dir.expanduser()
    ppt_plan_path = args.ppt_plan.expanduser()
    plan = load_plan(plan_path)
    shots = plan["shots"]
    if not shots:
        raise ValueError("R2V 计划没有镜头")
    audio_duration = duration(audio_path, args.ffprobe)
    validate_contiguous_timeline(shots, audio_duration)
    first_start = float(shots[0].get("source_start") or 0.0)
    if first_start <= 0:
        raise ValueError("首镜头必须晚于 0 秒，以便放置锁字片头")

    rows: list[dict[str, Any]] = []
    clips_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="story-r2v-assembly-") as temporary:
        work = Path(temporary)
        segments: list[Path] = []
        title_source_duration = duration(title_video, args.ffprobe)
        title_segment = clips_dir / "TITLE.mp4"
        title_timing = encode_segment(
            source=title_video,
            output=title_segment,
            target_duration=round(first_start * 30) / 30,
            target_frames=round(first_start * 30),
            ffmpeg=args.ffmpeg,
            source_duration=title_source_duration,
        )
        segments.append(title_segment)
        rows.append({
            "segment_id": "TITLE",
            "source_path": str(title_video),
            "source_sha256": sha256_path(title_video),
            "timeline_start": 0.0,
            "timeline_end": first_start,
            "story_duration": first_start,
            "hold_duration": 0.0,
            "encoded_path": str(title_segment),
            "encoded_sha256": sha256_path(title_segment),
            **title_timing,
        })

        for index, shot in enumerate(shots):
            shot_id = str(shot.get("shot_id") or "").strip()
            if not shot_id:
                raise ValueError(f"shots[{index}] 缺少 shot_id")
            start = float(shot.get("source_start") or 0.0)
            end = float(shot.get("source_end") or 0.0)
            if end <= start:
                raise ValueError(f"{shot_id} 时间区间无效")
            source = videos_dir / f"{shot_id}.mp4"
            if not source.is_file() or source.stat().st_size <= 0:
                raise FileNotFoundError(f"缺少镜头视频：{source}")
            source_duration = duration(source, args.ffprobe)
            segment = clips_dir / f"{shot_id}.mp4"
            target_segment_duration = end - start
            start_frame = round(start * 30)
            end_frame = round(end * 30)
            target_frame_count = end_frame - start_frame
            encoded_target_duration = target_frame_count / 30
            trim_anchor, explicit_trim_start = assembly_trim_policy(shot)
            timing = encode_segment(
                source=source,
                output=segment,
                target_duration=encoded_target_duration,
                target_frames=target_frame_count,
                ffmpeg=args.ffmpeg,
                source_duration=source_duration,
                trim_anchor=trim_anchor,
                explicit_trim_start=explicit_trim_start,
            )
            segments.append(segment)
            rows.append({
                "segment_id": shot_id,
                "source_path": str(source),
                "source_sha256": sha256_path(source),
                "source_duration": source_duration,
                "timeline_start": start,
                "timeline_end": end,
                "story_duration": end - start,
                "hold_duration": 0.0,
                "encoded_path": str(segment),
                "encoded_sha256": sha256_path(segment),
                **timing,
            })

        concat_file = work / "concat.txt"
        concat_file.write_text("".join(f"file '{path}'\n" for path in segments), encoding="utf-8")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        raw_output = work / "story_visual_raw.mp4"
        run([
            args.ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-an", "-c", "copy", "-movflags", "+faststart", str(raw_output),
        ])
        # Per-segment frame quantization can accumulate by a few frames.  Keep
        # every encoded segment immutable, then trim only the final mux to the
        # authoritative audio duration.
        run([
            args.ffmpeg, "-y", "-i", str(raw_output), "-t", f"{audio_duration:.6f}",
            "-an", "-c", "copy", "-movflags", "+faststart", str(output_path),
        ])

    output_duration = duration(output_path, args.ffprobe)
    if abs(output_duration - audio_duration) > 0.08:
        raise ValueError(
            f"视觉母版时长 {output_duration:.6f}s 与权威音频 {audio_duration:.6f}s 偏差超过 0.08s"
        )
    payload = {
        "schema_version": "story-r2v-assembly-decisions-v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plan_path": str(plan_path),
        "plan_sha256": sha256_path(plan_path),
        "audio_path": str(audio_path),
        "audio_sha256": sha256_path(audio_path),
        "audio_duration": audio_duration,
        "output_path": str(output_path),
        "output_sha256": sha256_path(output_path),
        "output_duration": output_duration,
        "legacy_reuse_flag_requested": bool(args.reuse_existing_clips),
        "legacy_reuse_performed": False,
        "segments": rows,
    }
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    decisions_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ppt_slides = [{
        "shot_id": "TITLE",
        "video_path": str(clips_dir / "TITLE.mp4"),
        "subtitle": "",
        "semantic_card": True,
    }]
    ppt_slides.extend({
        "shot_id": str(shot.get("shot_id") or ""),
        "video_path": str(clips_dir / f"{shot.get('shot_id')}.mp4"),
        "subtitle": str(shot.get("story_text") or ""),
        "semantic_card": False,
    } for shot in shots)
    ppt_plan = {
        "schema_version": "story-ppt-plan-v1",
        "story_name": str(plan.get("story_name") or plan.get("story_title") or plan.get("story_id") or "story"),
        "slides": ppt_slides,
    }
    ppt_plan_path.parent.mkdir(parents=True, exist_ok=True)
    ppt_plan_path.write_text(json.dumps(ppt_plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output_path),
        "sha256": payload["output_sha256"],
        "duration": output_duration,
        "segments": len(rows),
        "decisions": str(decisions_path),
        "ppt_plan": str(ppt_plan_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
