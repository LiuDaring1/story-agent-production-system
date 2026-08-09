from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .align import LineTiming, align_script_to_narration, read_script_lines, save_timings
from .media import ensure_dir, probe_duration, run_command, sorted_video_files
from .subtitles import SubtitleCue, build_subtitle_cues, write_srt
from story_semantics import SemanticKind, classify_story, select_line_numbers


@dataclass(frozen=True)
class SynthesisConfig:
    video_dir: Path
    script_path: Path
    narration_path: Path
    music_path: Path
    output_dir: Path
    subtitle_script_path: Path | None = None
    whisper_model: str = "base"
    language: str | None = "zh"
    alignment_mode: str = "whisper"
    width: int = 1920
    height: int = 1080
    fps: int = 30
    music_volume: float = 0.22
    narration_volume: float = 1.0
    x264_preset: str = "veryfast"
    x264_crf: int = 20
    subtitle_style: str = "clean"
    sales_skip_head_lines: int = -1
    sales_skip_tail_lines: int = -1
    keep_workdir: bool = False
    whisper_model_dir: Path | None = None
    progress_callback: Callable[[str], None] | None = None


@dataclass(frozen=True)
class SynthesisResult:
    no_subs_bgm: Path
    subs_bgm: Path
    sales_subs_bgm: Path
    demo_voice_bgm: Path
    timings_json: Path
    subtitles_srt: Path
    sales_subtitles_srt: Path
    total_duration: float


def synthesize_story(config: SynthesisConfig) -> SynthesisResult:
    _progress(config, "检查工具和输入...")
    _validate_tools()
    ensure_dir(config.output_dir)
    work_dir = config.output_dir / "_work"
    ensure_dir(work_dir)

    _progress(config, "读取视频片段和台词...")
    script_lines = read_script_lines(config.script_path)
    subtitle_script_lines = (
        read_script_lines(config.subtitle_script_path)
        if config.subtitle_script_path is not None
        else script_lines
    )
    videos = sorted_video_files(config.video_dir)
    _validate_inputs(script_lines, videos, config)

    _progress(config, "分析旁白并对齐台词时间...")
    timings = align_script_to_narration(
        script_lines=script_lines,
        narration_path=config.narration_path,
        whisper_model=config.whisper_model,
        language=config.language,
        mode=config.alignment_mode,
        whisper_model_dir=config.whisper_model_dir,
    )
    subtitle_timings = (
        align_script_to_narration(
            script_lines=subtitle_script_lines,
            narration_path=config.narration_path,
            whisper_model=config.whisper_model,
            language=config.language,
            mode=config.alignment_mode,
            whisper_model_dir=config.whisper_model_dir,
        )
        if subtitle_script_lines != script_lines
        else timings
    )

    _progress(config, "保存时间轴和字幕文件...")
    timings_json = config.output_dir / "timings.json"
    subtitle_timings_json = config.output_dir / "subtitle_timings.json"
    subtitles_srt = config.output_dir / "story_subtitles.srt"
    sales_subtitles_srt = config.output_dir / "story_sales_subtitles.srt"
    save_timings(timings, timings_json)
    if subtitle_timings is not timings:
        save_timings(subtitle_timings, subtitle_timings_json)
    write_srt(subtitle_timings, subtitles_srt)
    subtitle_cues = build_subtitle_cues(subtitle_timings)
    sales_timings = _sales_subtitle_timings(subtitle_timings, config)
    write_srt(sales_timings, sales_subtitles_srt)
    sales_subtitle_cues = build_subtitle_cues(sales_timings)

    narration_duration = probe_duration(config.narration_path)
    music_duration = probe_duration(config.music_path)
    total_duration = narration_duration
    video_durations = _video_segment_durations(timings, total_duration)

    _progress(config, "处理视频片段...")
    segment_paths = _render_video_segments(videos, timings, video_durations, work_dir, config)
    _progress(config, "拼接完整静音视频...")
    silent_video = work_dir / "story_silent.mp4"
    _concat_videos(segment_paths, silent_video, work_dir / "concat.txt")

    no_subs_bgm = config.output_dir / "story_no_subs_bgm.mp4"
    subs_bgm = config.output_dir / "story_subs_bgm.mp4"
    sales_subs_bgm = config.output_dir / "story_sales_subs_bgm.mp4"
    demo_voice_bgm = config.output_dir / "story_demo_voice_bgm.mp4"

    _progress(config, "生成无字幕 + 背景音乐版...")
    _mux_with_music(
        video_path=silent_video,
        music_path=config.music_path,
        output_path=no_subs_bgm,
        total_duration=total_duration,
        music_volume=config.music_volume,
    )
    _progress(config, "烧录字幕视频轨...")
    subtitled_video = work_dir / "story_subtitled_silent.mp4"
    _burn_subtitles_only(
        video_path=silent_video,
        cues=subtitle_cues,
        work_dir=work_dir,
        output_path=subtitled_video,
        total_duration=total_duration,
        config=config,
    )
    _progress(config, "生成有字幕 + 背景音乐版...")
    _mux_with_music(
        video_path=subtitled_video,
        music_path=config.music_path,
        output_path=subs_bgm,
        total_duration=total_duration,
        music_volume=config.music_volume,
    )
    _progress(config, "生成销售版有字幕 + 背景音乐版...")
    sales_subtitled_video = work_dir / "story_sales_subtitled_silent.mp4"
    _burn_subtitles_only(
        video_path=silent_video,
        cues=sales_subtitle_cues,
        work_dir=work_dir / "sales_subtitles",
        output_path=sales_subtitled_video,
        total_duration=total_duration,
        config=config,
    )
    _mux_with_music(
        video_path=sales_subtitled_video,
        music_path=config.music_path,
        output_path=sales_subs_bgm,
        total_duration=total_duration,
        music_volume=config.music_volume,
    )
    _progress(config, "生成有字幕 + 旁白 + 背景音乐版...")
    _mux_with_voice_music(
        video_path=subtitled_video,
        narration_path=config.narration_path,
        music_path=config.music_path,
        output_path=demo_voice_bgm,
        total_duration=total_duration,
        config=config,
    )

    if not config.keep_workdir:
        shutil.rmtree(work_dir, ignore_errors=True)

    _progress(config, "合成完成。")
    return SynthesisResult(
        no_subs_bgm=no_subs_bgm,
        subs_bgm=subs_bgm,
        sales_subs_bgm=sales_subs_bgm,
        demo_voice_bgm=demo_voice_bgm,
        timings_json=timings_json,
        subtitles_srt=subtitles_srt,
        sales_subtitles_srt=sales_subtitles_srt,
        total_duration=total_duration,
    )


def _sales_subtitle_timings(timings: list[LineTiming], config: SynthesisConfig) -> list[LineTiming]:
    """Return customer subtitle timings under the shared semantic contract.

    ``sales_skip_head_lines``/``sales_skip_tail_lines`` remain an explicit
    compatibility escape hatch for old CLI callers.  When either value is
    omitted, only that side is inferred semantically; this preserves the
    historical ability to override one side without disabling the other.
    """

    if not timings:
        return []

    semantics = classify_story([timing.line for timing in timings])
    body_numbers = select_line_numbers(semantics, "sales_subtitles")
    if body_numbers:
        semantic_start = min(body_numbers) - 1
        semantic_end = max(body_numbers)
    else:
        semantic_start = len(timings)
        semantic_end = len(timings)

    if config.sales_skip_head_lines >= 0:
        start = min(len(timings), config.sales_skip_head_lines)
    else:
        start = semantic_start
    if config.sales_skip_tail_lines >= 0:
        end = max(0, len(timings) - config.sales_skip_tail_lines)
    else:
        end = semantic_end
    if end < start:
        return []
    return timings[start:end]


def _is_host_intro_line(text: str) -> bool:
    kind = classify_story([text]).kind_at(1)
    return kind in {SemanticKind.HOST_INTRO, SemanticKind.STORY_ANNOUNCEMENT}


def _looks_like_standalone_title(text: str) -> bool:
    return classify_story([text]).kind_at(1) is SemanticKind.TITLE


def _is_moral_or_outro_line(text: str) -> bool:
    kind = classify_story([text]).kind_at(1)
    return kind in {SemanticKind.MORAL, SemanticKind.OUTRO}


def _validate_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"没有找到 {tool}，请先安装它。")


def _progress(config: SynthesisConfig, message: str) -> None:
    if config.progress_callback:
        config.progress_callback(message)


def _validate_inputs(script_lines: list[str], videos: list[Path], config: SynthesisConfig) -> None:
    if not config.video_dir.exists():
        raise FileNotFoundError(f"视频片段文件夹不存在：{config.video_dir}")
    if not script_lines:
        raise ValueError("台词文本为空。")
    if not videos:
        raise ValueError("视频片段文件夹里没有找到视频文件。")
    if len(videos) != len(script_lines):
        raise ValueError(f"视频片段数量是 {len(videos)}，台词行数是 {len(script_lines)}，两者必须一致。")
    required_paths = [config.script_path, config.narration_path, config.music_path]
    if config.subtitle_script_path is not None:
        required_paths.append(config.subtitle_script_path)
    for path in required_paths:
        if not path.exists():
            raise FileNotFoundError(f"文件不存在：{path}")


def _video_segment_durations(timings: list[LineTiming], total_duration: float) -> list[float]:
    if not timings:
        return []

    boundaries = [0.0]
    for timing in timings[1:]:
        boundaries.append(max(boundaries[-1], timing.source_start))
    boundaries.append(max(total_duration, boundaries[-1] + 0.15))

    durations: list[float] = []
    for start, end in zip(boundaries, boundaries[1:]):
        durations.append(round(max(0.15, end - start), 3))
    return durations


def _render_video_segments(
    videos: list[Path],
    timings: list[LineTiming],
    durations: list[float],
    work_dir: Path,
    config: SynthesisConfig,
) -> list[Path]:
    outputs: list[Path] = []
    for video_path, timing, target_duration in zip(videos, timings, durations):
        _progress(config, f"第 {timing.index} 个片段：{video_path.name} -> {target_duration:.1f}s")
        source_duration = probe_duration(video_path)
        output_path = work_dir / f"segment_{timing.index:03}.mp4"
        if source_duration + 0.01 >= target_duration:
            trim_duration = target_duration
            timing_filter = f"trim=0:{trim_duration:.3f},setpts=PTS-STARTPTS"
        else:
            speed_ratio = target_duration / max(0.01, source_duration)
            timing_filter = f"trim=0:{source_duration:.3f},setpts={speed_ratio:.8f}*(PTS-STARTPTS)"

        video_filter = (
            f"{timing_filter},"
            f"scale={config.width}:{config.height}:force_original_aspect_ratio=decrease,"
            f"pad={config.width}:{config.height}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,fps={config.fps},format=yuv420p"
        )
        run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-vf",
                video_filter,
                "-t",
                f"{target_duration:.3f}",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                config.x264_preset,
                "-crf",
                str(config.x264_crf),
                str(output_path),
            ]
        )
        outputs.append(output_path)
    return outputs


def _concat_videos(segment_paths: list[Path], output_path: Path, list_path: Path) -> None:
    lines = [f"file '{path.resolve().as_posix()}'" for path in segment_paths]
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run_command(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            str(output_path),
        ]
    )


def _render_cut_narration(narration_path: Path, timings: list[LineTiming], output_path: Path) -> None:
    filters: list[str] = []
    labels: list[str] = []
    for index, timing in enumerate(timings):
        label = f"a{index}"
        labels.append(f"[{label}]")
        filters.append(
            f"[0:a]atrim=start={timing.source_start:.3f}:end={timing.source_end:.3f},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
    filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[outa]")
    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(narration_path),
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[outa]",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(output_path),
        ]
    )


def _mux_with_music(
    video_path: Path,
    music_path: Path,
    output_path: Path,
    total_duration: float,
    music_volume: float,
) -> None:
    audio_filter = (
        f"volume={music_volume},atrim=0:{total_duration:.3f},"
        f"apad=whole_dur={total_duration:.3f},asetpts=PTS-STARTPTS[a]"
    )
    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(music_path),
            "-filter_complex",
            f"[1:a]{audio_filter}",
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-t",
            f"{total_duration:.3f}",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


def _burn_subtitles_only(
    video_path: Path,
    cues: list[SubtitleCue],
    work_dir: Path,
    output_path: Path,
    total_duration: float,
    config: SynthesisConfig,
) -> None:
    subtitle_images = _render_subtitle_images(cues, work_dir / "subtitle_images", config)
    video_chain = _subtitle_overlay_chain(cues, first_image_input=1)
    args = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
    ]
    for image_path in subtitle_images:
        args.extend(["-loop", "1", "-i", str(image_path)])
    args.extend(
        [
            "-filter_complex",
            video_chain,
            "-map",
            "[v]",
            "-t",
            f"{total_duration:.3f}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            config.x264_preset,
            "-crf",
            str(config.x264_crf),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    run_command(args)


def _mux_with_voice_music(
    video_path: Path,
    narration_path: Path,
    music_path: Path,
    output_path: Path,
    total_duration: float,
    config: SynthesisConfig,
) -> None:
    filter_complex = (
        f"[1:a]volume={config.narration_volume},atrim=0:{total_duration:.3f},"
        f"apad=whole_dur={total_duration:.3f},asetpts=PTS-STARTPTS[n];"
        f"[2:a]volume={config.music_volume},atrim=0:{total_duration:.3f},"
        f"apad=whole_dur={total_duration:.3f},asetpts=PTS-STARTPTS[m];"
        "[n][m]amix=inputs=2:duration=first:dropout_transition=0[a]"
    )
    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(narration_path),
            "-i",
            str(music_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-t",
            f"{total_duration:.3f}",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


def _burn_subtitles_and_mux_music(
    video_path: Path,
    cues: list[SubtitleCue],
    work_dir: Path,
    music_path: Path,
    output_path: Path,
    total_duration: float,
    config: SynthesisConfig,
) -> None:
    subtitle_images = _render_subtitle_images(cues, work_dir / "subtitle_images", config)
    video_chain = _subtitle_overlay_chain(cues, first_image_input=2)
    filter_complex = (
        f"{video_chain};"
        f"[1:a]volume={config.music_volume},atrim=0:{total_duration:.3f},"
        f"apad=whole_dur={total_duration:.3f},asetpts=PTS-STARTPTS[a]"
    )
    args = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(music_path),
    ]
    for image_path in subtitle_images:
        args.extend(["-loop", "1", "-i", str(image_path)])
    args.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-t",
            f"{total_duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    run_command(args)


def _burn_subtitles_and_mix_voice_music(
    video_path: Path,
    cues: list[SubtitleCue],
    work_dir: Path,
    narration_path: Path,
    music_path: Path,
    output_path: Path,
    total_duration: float,
    config: SynthesisConfig,
) -> None:
    subtitle_images = _render_subtitle_images(cues, work_dir / "subtitle_images", config)
    video_chain = _subtitle_overlay_chain(cues, first_image_input=3)
    filter_complex = (
        f"{video_chain};"
        f"[1:a]volume={config.narration_volume},atrim=0:{total_duration:.3f},"
        f"apad=whole_dur={total_duration:.3f},asetpts=PTS-STARTPTS[n];"
        f"[2:a]volume={config.music_volume},atrim=0:{total_duration:.3f},"
        f"apad=whole_dur={total_duration:.3f},asetpts=PTS-STARTPTS[m];"
        "[n][m]amix=inputs=2:duration=first:dropout_transition=0[a]"
    )
    args = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(narration_path),
        "-i",
        str(music_path),
    ]
    for image_path in subtitle_images:
        args.extend(["-loop", "1", "-i", str(image_path)])
    args.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-t",
            f"{total_duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    run_command(args)


def _render_subtitle_images(
    cues: list[SubtitleCue],
    subtitle_dir: Path,
    config: SynthesisConfig,
) -> list[Path]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("没有找到 Pillow，无法生成字幕图层。请先安装 requirements.txt 里的依赖。") from exc

    ensure_dir(subtitle_dir)
    font_size = max(26, config.height // 24) if config.subtitle_style == "clean" else max(28, config.height // 18)
    font = _load_subtitle_font(ImageFont, font_size)
    outputs: list[Path] = []

    for cue in cues:
        image = Image.new("RGBA", (config.width, config.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        lines = _wrap_subtitle_text(draw, cue.text, font, int(config.width * 0.86))
        line_height = int(font_size * 1.35)
        text_height = line_height * len(lines)
        padding_x = max(24, config.width // 45)
        padding_y = max(14, config.height // 70)
        bottom_margin = max(20, config.height // 24) if config.subtitle_style == "clean" else max(36, config.height // 13)
        max_text_width = max(_text_width(draw, line, font) for line in lines)
        box_width = min(config.width - 40, max_text_width + padding_x * 2)
        box_height = text_height + padding_y * 2
        box_x = (config.width - box_width) // 2
        box_y = config.height - bottom_margin - box_height
        if config.subtitle_style != "clean":
            draw.rounded_rectangle(
                (box_x, box_y, box_x + box_width, box_y + box_height),
                radius=max(10, config.height // 80),
                fill=(0, 0, 0, 150),
            )
            y = box_y + padding_y
        else:
            y = config.height - bottom_margin - text_height
        for line in lines:
            x = (config.width - _text_width(draw, line, font)) // 2
            if config.subtitle_style == "clean":
                shadow_offset = max(2, font_size // 16)
                draw.text(
                    (x + shadow_offset, y + shadow_offset),
                    line,
                    font=font,
                    fill=(0, 0, 0, 160),
                    stroke_width=max(2, font_size // 18),
                    stroke_fill=(0, 0, 0, 160),
                )
            draw.text(
                (x, y),
                line,
                font=font,
                fill=(255, 255, 255, 255),
                stroke_width=max(2, font_size // 18) if config.subtitle_style == "clean" else max(1, font_size // 18),
                stroke_fill=(0, 0, 0, 230),
            )
            y += line_height

        output_path = subtitle_dir / f"subtitle_{cue.index:03}.png"
        image.save(output_path)
        outputs.append(output_path)

    return outputs


def _subtitle_overlay_chain(cues: list[SubtitleCue], first_image_input: int) -> str:
    current = "[0:v]"
    filters: list[str] = []
    for offset, cue in enumerate(cues):
        output = "[v]" if offset == len(cues) - 1 else f"[v{offset}]"
        input_index = first_image_input + offset
        filters.append(
            f"{current}[{input_index}:v]overlay=0:0:"
            f"enable='between(t,{cue.start:.3f},{cue.end:.3f})'{output}"
        )
        current = output
    return ";".join(filters)


def _load_subtitle_font(image_font_module, font_size: int):
    candidates = [
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
        Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
        Path("/Library/Fonts/Arial Unicode.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return image_font_module.truetype(str(path), font_size)
    return image_font_module.load_default()


def _wrap_subtitle_text(draw, text: str, font, max_width: int) -> list[str]:
    if _text_width(draw, text, font) <= max_width:
        return [text]

    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and _text_width(draw, candidate, font) > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _text_width(draw, text: str, font) -> int:
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return right - left
