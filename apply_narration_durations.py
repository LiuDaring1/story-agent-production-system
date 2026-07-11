from __future__ import annotations

import argparse
from pathlib import Path

from story_video_synthesizer.align import align_script_to_narration, sanitize_script_line, save_timings
from story_video_synthesizer.volcengine_video import duration_to_supported_frames, read_jobs_csv, write_jobs_csv


def main() -> None:
    parser = argparse.ArgumentParser(description="根据旁白音频把每个图片转视频任务填入真实时长")
    parser.add_argument("--jobs-csv", required=True, type=Path, help="prepare_image_video_jobs.py 生成的任务 CSV")
    parser.add_argument("--narration", required=True, type=Path, help="完整讲故事旁白音频")
    parser.add_argument("--output-timings", default="", help="保存 Whisper 对齐时间轴 JSON")
    parser.add_argument("--whisper-model", default="base")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--alignment-mode", choices=["whisper", "even"], default="whisper")
    parser.add_argument("--whisper-model-dir", default=None, type=Path)
    parser.add_argument("--min-duration", default=2.0, type=float, help="单段最短生成时长")
    parser.add_argument("--max-duration", default=10.0, type=float, help="单段 API 固定生成时长；青云 Grok video 3 当前每次生成 10 秒")
    parser.add_argument("--padding", default=0.15, type=float, help="给每句额外补一点点秒数，避免被切太紧")
    parser.add_argument("--fps", default=24, type=int, help="frames 换算帧率")
    parser.add_argument("--min-frames", default=29, type=int)
    parser.add_argument("--max-frames", default=289, type=int)
    args = parser.parse_args()

    jobs_path = args.jobs_csv.expanduser()
    rows = read_jobs_csv(jobs_path)
    script_lines = [
        sanitize_script_line(row.get("story_text", "").strip() or row.get("prompt", "").strip())
        for row in rows
    ]
    if not script_lines:
        raise ValueError("任务 CSV 里没有可用于对齐的文本。")

    timings = align_script_to_narration(
        script_lines=script_lines,
        narration_path=args.narration.expanduser(),
        whisper_model=args.whisper_model,
        language=args.language.strip() or None,
        mode=args.alignment_mode,
        whisper_model_dir=args.whisper_model_dir.expanduser() if args.whisper_model_dir else None,
    )
    if len(timings) != len(rows):
        raise RuntimeError(f"对齐结果 {len(timings)} 条，任务 CSV {len(rows)} 条，数量不一致。")

    for row, timing in zip(rows, timings):
        target_duration = max(args.min_duration, timing.duration + args.padding)
        generation_duration = args.max_duration
        frames = duration_to_supported_frames(
            generation_duration,
            fps=args.fps,
            min_frames=args.min_frames,
            max_frames=args.max_frames,
        )
        effective_duration = frames / args.fps
        row["duration"] = f"{generation_duration:.2f}".rstrip("0").rstrip(".")
        row["generation_duration"] = f"{generation_duration:.2f}".rstrip("0").rstrip(".")
        row["target_duration"] = f"{target_duration:.2f}".rstrip("0").rstrip(".")
        row["frames"] = str(frames)
        row["effective_duration"] = f"{effective_duration:.3f}"
        row["needs_slowdown"] = "yes" if target_duration > effective_duration + 0.05 else "no"
        row["needs_trim"] = "yes" if target_duration < effective_duration - 0.05 else "no"
        row["slowdown_ratio"] = f"{target_duration / effective_duration:.3f}" if effective_duration > 0 else ""
        row["narration_start"] = f"{timing.source_start:.3f}"
        row["narration_end"] = f"{timing.source_end:.3f}"
        row["narration_duration"] = f"{timing.duration:.3f}"

    write_jobs_csv(jobs_path, rows)

    timings_path = Path(args.output_timings).expanduser() if args.output_timings.strip() else None
    if timings_path is None:
        timings_path = jobs_path.with_name(jobs_path.stem + "_timings.json")
    save_timings(timings, timings_path)

    total_duration = sum(float(row["target_duration"]) for row in rows)
    total_generation_duration = sum(float(row["generation_duration"]) for row in rows)
    total_effective_duration = sum(float(row["effective_duration"]) for row in rows)
    slowdown_count = sum(1 for row in rows if row.get("needs_slowdown") == "yes")
    print(f"已写入 {len(rows)} 个视频时长。")
    print(f"目标旁白总时长约：{total_duration:.2f} 秒")
    trim_count = sum(1 for row in rows if row.get("needs_trim") == "yes")
    print(f"API 固定生成总时长约：{total_generation_duration:.2f} 秒")
    print(f"固定 {args.max_duration:g} 秒片段总时长约：{total_effective_duration:.2f} 秒")
    print(f"需要后期裁切匹配的镜头数：{trim_count}")
    print(f"需要后期慢放匹配的镜头数：{slowdown_count}")
    print(f"时间轴：{timings_path}")
    print(f"任务清单：{jobs_path}")


if __name__ == "__main__":
    main()
