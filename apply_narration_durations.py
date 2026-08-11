from __future__ import annotations

import argparse
import math
from pathlib import Path

from story_video_synthesizer.align import align_script_to_narration, sanitize_script_line, save_timings
from story_video_synthesizer.volcengine_video import duration_to_supported_frames, read_jobs_csv, write_jobs_csv


ADAPTIVE_DURATION_MODE = "adaptive-seconds"


def normalize_duration_mode(value: str) -> str:
    """Normalize timing mode aliases while keeping the legacy fixed mode."""

    mode = str(value or "fixed").strip().lower().replace("_", "-")
    if mode == "adaptive":
        mode = ADAPTIVE_DURATION_MODE
    if mode not in {"fixed", ADAPTIVE_DURATION_MODE}:
        raise ValueError(f"不支持的 duration mode：{value!r}；可选 fixed 或 adaptive-seconds")
    return mode


def _integer_generation_bounds(min_generation_seconds: float, max_generation_seconds: float) -> tuple[int, int]:
    """Return safe integer bounds for providers that bill whole seconds."""

    minimum = max(1, math.ceil(float(min_generation_seconds)))
    maximum = math.floor(float(max_generation_seconds))
    if maximum < minimum:
        raise ValueError(
            f"自适应生成时长范围无效：最小 {min_generation_seconds:g} 秒，大于最大 {max_generation_seconds:g} 秒。"
        )
    return minimum, maximum


def generation_duration_for_target(
    target_duration: float,
    *,
    duration_mode: str = "fixed",
    fixed_duration: float = 10.0,
    min_generation_seconds: float = 1.0,
    max_generation_seconds: float = 15.0,
) -> int | float:
    """Choose the provider request duration for one narration window.

    ``fixed`` is intentionally the legacy behavior: use the configured fixed
    duration unchanged.  ``adaptive-seconds`` rounds the target up to the next
    whole second and clamps it to the provider's advertised integer range.
    """

    mode = normalize_duration_mode(duration_mode)
    if mode == "fixed":
        return float(fixed_duration)
    minimum, maximum = _integer_generation_bounds(min_generation_seconds, max_generation_seconds)
    return max(minimum, min(maximum, math.ceil(float(target_duration))))


def _format_duration(value: int | float) -> str:
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


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
    parser.add_argument(
        "--duration-mode",
        choices=["fixed", "adaptive", ADAPTIVE_DURATION_MODE],
        default="fixed",
        help="生成时长策略；fixed 保持旧版固定时长，adaptive-seconds 按旁白向上取整并夹在供应商整数秒范围",
    )
    parser.add_argument(
        "--adaptive-seconds",
        action="store_true",
        help="兼容别名：等同于 --duration-mode adaptive-seconds",
    )
    parser.add_argument(
        "--min-generation-seconds",
        "--generation-min-seconds",
        dest="min_generation_seconds",
        default=1.0,
        type=float,
        help="adaptive-seconds 的供应商最小整数秒（默认 1）",
    )
    parser.add_argument(
        "--max-generation-seconds",
        "--generation-max-seconds",
        dest="max_generation_seconds",
        default=15.0,
        type=float,
        help="adaptive-seconds 的供应商最大整数秒（默认 15）",
    )
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

    duration_mode = ADAPTIVE_DURATION_MODE if args.adaptive_seconds else normalize_duration_mode(args.duration_mode)
    if duration_mode == ADAPTIVE_DURATION_MODE:
        generation_min_seconds, generation_max_seconds = _integer_generation_bounds(
            args.min_generation_seconds,
            args.max_generation_seconds,
        )
    else:
        generation_min_seconds, generation_max_seconds = (None, None)

    for row, timing in zip(rows, timings):
        target_duration = max(args.min_duration, timing.duration + args.padding)
        generation_duration = generation_duration_for_target(
            target_duration,
            duration_mode=duration_mode,
            fixed_duration=args.max_duration,
            min_generation_seconds=args.min_generation_seconds,
            max_generation_seconds=args.max_generation_seconds,
        )
        if duration_mode == ADAPTIVE_DURATION_MODE:
            # Keep a truthful compatibility value for old consumers, but do
            # not route adaptive requests through the legacy capped frame
            # conversion (which used to turn 15s into ~12s).
            frames = int(round(float(generation_duration) * args.fps))
            effective_duration = float(generation_duration)
        else:
            frames = duration_to_supported_frames(
                float(generation_duration),
                fps=args.fps,
                min_frames=args.min_frames,
                max_frames=args.max_frames,
            )
            effective_duration = frames / args.fps
        row["duration"] = _format_duration(generation_duration)
        row["generation_duration"] = _format_duration(generation_duration)
        row["target_duration"] = _format_duration(target_duration)
        row["duration_mode"] = duration_mode
        row["generation_min_seconds"] = "" if generation_min_seconds is None else str(generation_min_seconds)
        row["generation_max_seconds"] = "" if generation_max_seconds is None else str(generation_max_seconds)
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
    print(f"API {duration_mode} 生成总时长约：{total_generation_duration:.2f} 秒")
    print(f"实际请求片段总时长约：{total_effective_duration:.2f} 秒")
    print(f"需要后期裁切匹配的镜头数：{trim_count}")
    print(f"需要后期慢放匹配的镜头数：{slowdown_count}")
    print(f"时间轴：{timings_path}")
    print(f"任务清单：{jobs_path}")


if __name__ == "__main__":
    main()
