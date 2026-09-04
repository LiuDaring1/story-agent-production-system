from __future__ import annotations

import re
import shutil
import json
import os
import tempfile
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


from .align import LineTiming, align_script_to_narration, clean_text, read_script_lines, save_timings
from .media import ensure_dir, probe_duration, run_command, sorted_video_files
from .subtitles import SubtitleCue, build_subtitle_cues, write_srt
from story_semantics import SemanticKind, classify_story, select_line_numbers
from story_module_ports import (
    CompositorPort,
    CompositorRequest,
    CompositorResult,
    ModuleFailure,
    ModuleFailureCode,
    ModuleUsageEvent,
)
from story_module_registry import build_compositor_registry
from semantic_card_motion import (
    load_semantic_card_motion_paths,
    write_semantic_card_motion_request,
)
from story_timeline import write_authoritative_timeline_receipt


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
    project_dir: Path | None = None
    artifact_semantic_plan_path: Path | None = None


@dataclass(frozen=True)
class SynthesisResult:
    no_subs_bgm: Path
    subs_bgm: Path
    sales_subs_bgm: Path
    demo_voice_bgm: Path
    timings_json: Path
    subtitles_srt: Path
    sales_subtitles_srt: Path
    timeline_receipt: Path | None
    semantic_plan_manifest: Path | None
    total_duration: float


def synthesize_story(
    config: SynthesisConfig,
    compositor_port: CompositorPort | None = None,
) -> SynthesisResult:
    """Run the existing background assembly through the local CompositorPort seam."""

    if config.project_dir is not None and config.alignment_mode != "whisper":
        raise ValueError("正式项目禁止 even/均分 fallback；必须使用 Whisper 音频对齐")

    input_paths: list[tuple[str, Path]] = [
        (f"video_segment:{index}", path)
        for index, path in enumerate(sorted_video_files(config.video_dir), start=1)
    ]
    input_paths.extend(
        [
            ("script", config.script_path),
            ("narration", config.narration_path),
            ("music", config.music_path),
        ]
    )
    if config.subtitle_script_path is not None:
        input_paths.append(("subtitle_script", config.subtitle_script_path))
    if config.artifact_semantic_plan_path is not None:
        input_paths.append(("artifact_semantic_plan", config.artifact_semantic_plan_path))
    output_targets = [
        config.output_dir / "story_no_subs_bgm.mp4",
        config.output_dir / "story_subs_bgm.mp4",
        config.output_dir / "story_sales_subs_bgm.mp4",
        config.output_dir / "story_demo_voice_bgm.mp4",
        config.output_dir / "timings.json",
        config.output_dir / "story_subtitles.srt",
        config.output_dir / "story_sales_subtitles.srt",
        config.output_dir / "story_semantic_timeline.srt",
    ]
    if config.alignment_mode == "whisper":
        output_targets.extend(
            [
                config.output_dir / "alignment_run_metadata.json",
                config.output_dir / "authoritative_timeline_receipt.json",
            ]
        )
    if config.artifact_semantic_plan_path is not None:
        output_targets.append(config.output_dir / "artifact_semantic_plan_manifest.json")
    request = CompositorRequest(
        artifact_id=f"background-story:{config.output_dir.name}",
        operation="background_story",
        input_artifacts=tuple(
            {"role": role, "path": str(path), "sha256": _file_sha256(path)}
            for role, path in input_paths
        ),
        output_targets=tuple(output_targets),
        execution_binding={
            "alignment_mode": config.alignment_mode,
            "whisper_model": config.whisper_model,
            "language": config.language,
            "width": config.width,
            "height": config.height,
            "fps": config.fps,
            "music_volume": config.music_volume,
            "narration_volume": config.narration_volume,
            "x264_preset": config.x264_preset,
            "x264_crf": config.x264_crf,
            "subtitle_style": config.subtitle_style,
            "sales_skip_head_lines": config.sales_skip_head_lines,
            "sales_skip_tail_lines": config.sales_skip_tail_lines,
            "keep_workdir": config.keep_workdir,
        },
        attempt_id="background-story-compositor",
    )
    port = compositor_port or build_compositor_registry().compositor()
    synthesis_result: SynthesisResult | None = None

    def execute_background(execution_request: CompositorRequest) -> CompositorResult:
        nonlocal synthesis_result
        try:
            synthesis_result = _synthesize_story_core(config)
            artifacts = tuple(
                {"path": str(path), "sha256": _file_sha256(path), "production_eligible": True}
                for path in execution_request.output_targets
            )
        except Exception as exc:
            return CompositorResult(
                False, execution_request.operation, (), execution_request.attempt_id,
                port.identity.adapter_version, True,
                failure=ModuleFailure(ModuleFailureCode.EXECUTION_FAILED, str(exc)),
            )
        return CompositorResult(
            True, execution_request.operation, artifacts, execution_request.attempt_id,
            port.identity.adapter_version, True,
            usage_events=(
                ModuleUsageEvent(
                    "local", "existing-python-ffmpeg-compositor", execution_request.operation,
                    unit_type="render", quantity=1.0, actual_amount_status="not_applicable",
                ),
            ),
        )

    result = port.execute(request, executor=execute_background)
    if not result.success:
        message = result.failure.message if result.failure is not None else "unknown compositor failure"
        raise RuntimeError(f"背景故事合成失败：{message}")
    if result.production_eligible is not True or synthesis_result is None:
        raise RuntimeError("compositor mock 结果不可作为正式背景故事合成")
    return synthesis_result


def _synthesize_story_core(config: SynthesisConfig) -> SynthesisResult:
    # Lazy import avoids the existing story_project -> package -> pipeline
    # import cycle while keeping validation on every required_v1 execution.
    from artifact_semantic_plan import (
        load_current_artifact_semantic_plan,
        plan_binding,
        presentation_windows,
        select_timings_for_artifact,
    )
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
    preserve_subtitle_lines = config.subtitle_script_path is not None

    semantic_plan = None
    semantic_plan_manifest = None
    if config.artifact_semantic_plan_path is not None:
        if config.project_dir is None:
            raise ValueError("--artifact-semantic-plan requires --project-dir for current-lock validation")
        semantic_plan = load_current_artifact_semantic_plan(config.project_dir, config.subtitle_script_path or config.script_path)

    _progress(config, "保存时间轴和字幕文件...")
    timings_json = config.output_dir / "timings.json"
    subtitle_timings_json = config.output_dir / "subtitle_timings.json"
    subtitles_srt = config.output_dir / "story_subtitles.srt"
    semantic_timeline_srt = config.output_dir / "story_semantic_timeline.srt"
    sales_subtitles_srt = config.output_dir / "story_sales_subtitles.srt"
    save_timings(timings, timings_json)
    if subtitle_timings is not timings:
        save_timings(subtitle_timings, subtitle_timings_json)
    # Preserve the complete, audited semantic cue stream for deterministic
    # downstream selectors.  The customer-facing background SRT may exclude
    # title/moral cues because those are rendered as visual cards.
    write_srt(
        subtitle_timings,
        semantic_timeline_srt,
        preserve_input_lines=preserve_subtitle_lines,
    )
    timeline_receipt: Path | None = None
    if config.alignment_mode == "whisper":
        alignment_metadata = config.output_dir / "alignment_run_metadata.json"
        subtitle_source = config.subtitle_script_path or config.script_path
        _write_json_atomic(
            alignment_metadata,
            {
                "alignment_mode": "whisper",
                "model": config.whisper_model,
                "language": config.language,
                "audio_path": str(config.narration_path.resolve()),
                "audio_sha256": _file_sha256(config.narration_path),
                "subtitle_path": str(subtitle_source.resolve()),
                "subtitle_sha256": _file_sha256(subtitle_source),
                "subtitle_line_count": len(subtitle_timings),
                "timed_char_count": sum(len(clean_text(item.line)) for item in subtitle_timings),
            },
        )
        timeline_receipt = config.output_dir / "authoritative_timeline_receipt.json"
        write_authoritative_timeline_receipt(
            receipt_path=timeline_receipt,
            source_kind="whisper_native_synthesis",
            timings_path=subtitle_timings_json if subtitle_timings is not timings else timings_json,
            alignment_metadata_path=alignment_metadata,
            subtitle_txt=subtitle_source,
            authoritative_audio=config.narration_path,
            alignment_audio=config.narration_path,
            output_srt=semantic_timeline_srt,
        )
    background_timings = (
        select_timings_for_artifact(subtitle_timings, semantic_plan, "background_subtitles")
        if semantic_plan is not None else subtitle_timings
    )
    demo_timings = (
        select_timings_for_artifact(subtitle_timings, semantic_plan, "demo_subtitles")
        if semantic_plan is not None else subtitle_timings
    )
    write_srt(
        background_timings,
        subtitles_srt,
        preserve_input_lines=preserve_subtitle_lines,
    )
    subtitle_cues = build_subtitle_cues(
        background_timings,
        preserve_input_lines=preserve_subtitle_lines,
    )
    sales_timings = (
        select_timings_for_artifact(subtitle_timings, semantic_plan, "sales_subtitles")
        if semantic_plan is not None else _sales_subtitle_timings(subtitle_timings, config)
    )
    write_srt(
        sales_timings,
        sales_subtitles_srt,
        preserve_input_lines=preserve_subtitle_lines,
    )
    sales_subtitle_cues = build_subtitle_cues(
        sales_timings,
        preserve_input_lines=preserve_subtitle_lines,
    )

    narration_duration = probe_duration(config.narration_path)
    music_duration = probe_duration(config.music_path)
    total_duration = narration_duration
    video_durations = _video_segment_durations(timings, total_duration)

    _progress(config, "处理视频片段...")
    segment_paths = _render_video_segments(videos, timings, video_durations, work_dir, config)
    _progress(config, "拼接完整静音视频...")
    silent_video = work_dir / "story_silent.mp4"
    _concat_videos(segment_paths, silent_video, work_dir / "concat.txt")
    presentation_base = silent_video
    card_windows: list[dict] = []
    if semantic_plan is not None:
        card_windows = presentation_windows(subtitle_timings, semantic_plan)
        if card_windows:
            if config.project_dir is None:
                raise ValueError("required semantic cards need --project-dir")
            presentation_base = work_dir / "story_semantic_cards.mp4"
            _overlay_semantic_cards(
                silent_video,
                card_windows,
                presentation_base,
                config.project_dir / "01_分镜与图片" / "semantic_cards",
                total_duration,
                config,
            )
        semantic_plan_manifest = config.output_dir / "artifact_semantic_plan_manifest.json"
        _write_json_atomic(semantic_plan_manifest, {
            "version": 1,
            "consumer": "assembly",
            **plan_binding(config.artifact_semantic_plan_path, semantic_plan),
            "selections": {
                "background_subtitles": [item.index for item in background_timings],
                "sales_subtitles": [item.index for item in sales_timings],
                "demo_subtitles": [item.index for item in demo_timings],
            },
            "visual_card_windows": card_windows,
            "source_video_unchanged": True,
        })

    no_subs_bgm = config.output_dir / "story_no_subs_bgm.mp4"
    subs_bgm = config.output_dir / "story_subs_bgm.mp4"
    sales_subs_bgm = config.output_dir / "story_sales_subs_bgm.mp4"
    demo_voice_bgm = config.output_dir / "story_demo_voice_bgm.mp4"

    _progress(config, "生成无字幕 + 背景音乐版...")
    _mux_with_music(
        video_path=presentation_base,
        music_path=config.music_path,
        output_path=no_subs_bgm,
        total_duration=total_duration,
        music_volume=config.music_volume,
    )
    _progress(config, "烧录字幕视频轨...")
    subtitled_video = work_dir / "story_subtitled_silent.mp4"
    _burn_subtitles_only(
        video_path=presentation_base,
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
        video_path=presentation_base,
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
    demo_subtitled_video = work_dir / "story_demo_subtitled_silent.mp4"
    _burn_subtitles_only(
        video_path=presentation_base,
        cues=build_subtitle_cues(
            demo_timings,
            preserve_input_lines=preserve_subtitle_lines,
        ),
        work_dir=work_dir / "demo_subtitles",
        output_path=demo_subtitled_video,
        total_duration=total_duration,
        config=config,
    )
    _mux_with_voice_music(
        video_path=demo_subtitled_video,
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
        timeline_receipt=timeline_receipt,
        semantic_plan_manifest=semantic_plan_manifest,
        total_duration=total_duration,
    )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


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
        if source_duration <= 0:
            raise RuntimeError(f"无法读取图生视频时长：{video_path}")
        # Use the complete provider clip and apply one uniform speed change.
        # Grok 1.0 requests the nearest native 6s/10s duration, so this avoids
        # both an abrupt tail cut and a visibly repeating motion loop.
        speed_ratio = target_duration / source_duration
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


def _overlay_semantic_cards(
    video_path: Path,
    windows: list[dict],
    output_path: Path,
    card_dir: Path,
    total_duration: float,
    config: SynthesisConfig,
    *,
    allow_static_preview: bool = False,
) -> None:
    """Overlay provider-motion cards; static cards are explicit short-test only."""

    receipt_path = card_dir / "semantic_card_generation_receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("缺少 ImageGen 一体化片头/寓意卡回执，禁止程序后期叠字替代") from exc
    if receipt.get("schema_version") != "story-semantic-card-generation/v1" or receipt.get("imagegen_native") is not True:
        raise RuntimeError("片头/寓意卡回执无效：必须由 ImageGen 一体成型")
    if receipt.get("post_render_text_overlay") is not False:
        raise RuntimeError("片头/寓意卡禁止程序后期叠字")
    receipt_cards = receipt.get("cards") if isinstance(receipt.get("cards"), list) else []
    by_kind = {str(item.get("card_kind") or ""): item for item in receipt_cards if isinstance(item, dict)}
    images = []
    for window in windows:
        kind = str(window["card_kind"])
        item = by_kind.get(kind)
        if item is None or str(item.get("text") or "") != str(window["text"]):
            raise RuntimeError(f"ImageGen 语义卡回执未绑定当前 {kind} 文案")
        card_path = Path(str(item.get("path") or "")).expanduser()
        if not card_path.is_file() or hashlib.sha256(card_path.read_bytes()).hexdigest() != item.get("sha256"):
            raise RuntimeError(f"ImageGen 语义卡文件或哈希失效：{kind}")
        images.append(card_path)
    motion_paths: dict[str, Path] = {}
    if not allow_static_preview:
        request_path = card_dir / "semantic_card_motion_request.json"
        plan_sha256 = str(receipt.get("artifact_semantic_plan_sha256") or "")
        if len(plan_sha256) != 64:
            raise RuntimeError("片头/寓意卡微动缺少当前语义计划哈希")
        # Rebuild deterministically from the real aligned audio windows.  A
        # provisional/manual request must not lock production to a fake 4s/6s
        # title duration.
        write_semantic_card_motion_request(
            card_dir=card_dir,
            windows=windows,
            artifact_semantic_plan_sha256=plan_sha256,
        )
        motion_paths = load_semantic_card_motion_paths(
            request_path,
            card_dir / "semantic_card_motion_receipt.json",
        )
    args = ["ffmpeg", "-y", "-i", str(video_path)]
    for window, image_path in zip(windows, images):
        if allow_static_preview:
            args.extend(["-loop", "1", "-i", str(image_path)])
        else:
            kind = str(window["card_kind"])
            if kind not in motion_paths:
                raise RuntimeError(f"片头/寓意卡微动缺少输出：{kind}")
            args.extend(["-i", str(motion_paths[kind])])
    current = "[0:v]"
    filters = []
    for offset, window in enumerate(windows, start=1):
        output = "[v]" if offset == len(windows) else f"[card{offset}]"
        card_label = f"[card_image{offset}]"
        if allow_static_preview:
            input_timeline = ""
        else:
            kind = str(window["card_kind"])
            source_duration = probe_duration(motion_paths[kind])
            presentation_duration = max(0.001, float(window["end"]) - float(window["start"]))
            if source_duration <= 0:
                raise RuntimeError(f"无法读取片头/寓意卡微动时长：{kind}")
            speed_ratio = presentation_duration / source_duration
            input_timeline = (
                f"trim=0:{source_duration:.3f},"
                f"setpts={speed_ratio:.8f}*(PTS-STARTPTS)+{float(window['start']):.3f}/TB,"
            )
        filters.append(
            f"[{offset}:v]{input_timeline}scale={config.width}:{config.height}:force_original_aspect_ratio=increase,"
            f"crop={config.width}:{config.height},setsar=1{card_label}"
        )
        filters.append(
            f"{current}{card_label}overlay=0:0:enable='between(t,{window['start']:.3f},{window['end']:.3f})'{output}"
        )
        current = output
    args.extend([
        "-filter_complex", ";".join(filters), "-map", "[v]", "-t", f"{total_duration:.3f}", "-an",
        "-c:v", "libx264", "-preset", config.x264_preset, "-crf", str(config.x264_crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output_path),
    ])
    run_command(args)


def _burn_subtitles_only(
    video_path: Path,
    cues: list[SubtitleCue],
    work_dir: Path,
    output_path: Path,
    total_duration: float,
    config: SynthesisConfig,
) -> None:
    if not cues:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(video_path, output_path)
        return
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
        line = str(cue.text).replace("\r", "").replace("\n", "").strip()
        safe_width = int(config.width * 0.86)
        if _text_width(draw, line, font) > safe_width:
            raise ValueError(
                "subtitle_single_line_overflow: 字幕 TXT 单行超出安全宽度；"
                "请修改用户字幕 TXT 的换行，渲染器不自动折成两行"
            )
        lines = [line]
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
        if _subtitle_palette_has_magenta_or_purple(image):
            raise ValueError("字幕图层出现紫色/洋红色像素，拒绝进入成片")
        image.save(output_path)
        outputs.append(output_path)

    return outputs


def _subtitle_palette_has_magenta_or_purple(image) -> bool:
    """Reject the former magenta-key subtitle fringe from customer videos."""

    rgba = image.convert("RGBA")
    pixels = rgba.get_flattened_data() if hasattr(rgba, "get_flattened_data") else rgba.getdata()
    for red, green, blue, alpha in pixels:
        if alpha > 16 and red >= 105 and blue >= 105 and green * 1.35 < min(red, blue):
            return True
    return False


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


def _text_width(draw, text: str, font) -> int:
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return right - left
