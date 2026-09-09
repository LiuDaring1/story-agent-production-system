#!/usr/bin/env python3
"""Render reusable 16:9 customer backgrounds from a sealed visual master."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from story_timeline import validate_authoritative_timeline_receipt
from story_video_synthesizer.align import LineTiming
from story_video_synthesizer.media import probe_duration
from story_video_synthesizer.pipeline import (
    SynthesisConfig,
    _burn_subtitles_only,
    _mux_with_music,
)
from story_video_synthesizer.subtitles import build_subtitle_cues, write_srt


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _binding(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "sha256": file_sha256(path),
        "bytes": path.stat().st_size,
    }


def body_timings_from_plan(
    assembly_plan: dict[str, Any],
    timing_rows: list[dict[str, Any]],
) -> list[LineTiming]:
    body = [
        shot
        for shot in assembly_plan.get("shots", [])
        if str(shot.get("shot_id") or "").upper() != "MORAL"
    ]
    if not body:
        raise ValueError("权威拼装计划缺少正文镜头")
    first_line = min(int(shot.get("authoritative_line_start") or 0) for shot in body)
    last_line = max(int(shot.get("authoritative_line_end") or 0) for shot in body)
    if first_line < 1 or last_line > len(timing_rows) or first_line > last_line:
        raise ValueError("权威拼装计划的正文行范围无效")
    selected = timing_rows[first_line - 1 : last_line]
    results = []
    for index, row in enumerate(selected, start=1):
        start = float(row["source_start"])
        end = float(row["source_end"])
        results.append(
            LineTiming(
                index=index,
                line=str(row["line"]),
                source_start=start,
                source_end=end,
                duration=end - start,
                timeline_start=start,
                timeline_end=end,
            )
        )
    return results


from story_render_task import render_entry

@render_entry
def main() -> int:
    parser = argparse.ArgumentParser(description="生成有配乐无旁白的客户 16:9 背景视频")
    parser.add_argument("--visual-master", required=True, type=Path)
    parser.add_argument("--music", required=True, type=Path)
    parser.add_argument("--authoritative-timeline-receipt", required=True, type=Path)
    parser.add_argument("--assembly-plan", required=True, type=Path)
    parser.add_argument("--output-with-subtitles", required=True, type=Path)
    parser.add_argument("--output-without-subtitles", required=True, type=Path)
    parser.add_argument("--body-srt", required=True, type=Path)
    parser.add_argument("--render-receipt", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--music-volume", default=0.22, type=float)
    parser.add_argument("--width", default=1920, type=int)
    parser.add_argument("--height", default=1080, type=int)
    parser.add_argument("--fps", default=30, type=int)
    parser.add_argument("--crf", default=20, type=int)
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--run-file", type=Path, help="v2 正式渲染必须显式绑定所属 story_run.json")
    args = parser.parse_args()
    from story_render_task import bind_render_task
    run = bind_render_task(args.run_file, outputs=[args.output_with_subtitles, args.output_without_subtitles, args.body_srt, args.render_receipt, args.work_dir])
    if run is not None:
        from story_production_v2 import binding
        if binding(args.music) != run["inputs"]["finished_music"]:
            raise ValueError("Music must be the ledger finished_music input")

    timeline = validate_authoritative_timeline_receipt(args.authoritative_timeline_receipt, expected_inputs=run["inputs"] if run else None)
    timing_rows = json.loads(Path(timeline["timings"]["path"]).read_text(encoding="utf-8"))
    assembly_plan = json.loads(args.assembly_plan.read_text(encoding="utf-8"))
    body_timings = body_timings_from_plan(assembly_plan, timing_rows)
    args.body_srt.parent.mkdir(parents=True, exist_ok=True)
    write_srt(body_timings, args.body_srt, preserve_input_lines=True)

    duration = probe_duration(args.visual_master)
    config = SynthesisConfig(
        video_dir=args.visual_master.parent,
        script_path=Path(timeline["subtitle_txt"]["path"]),
        narration_path=Path(timeline["authoritative_audio"]["path"]),
        music_path=args.music,
        output_dir=args.output_without_subtitles.parent,
        width=args.width,
        height=args.height,
        fps=args.fps,
        music_volume=args.music_volume,
        x264_crf=args.crf,
        x264_preset=args.preset,
        subtitle_style="clean",
    )
    args.output_without_subtitles.parent.mkdir(parents=True, exist_ok=True)
    _mux_with_music(
        args.visual_master,
        args.music,
        args.output_without_subtitles,
        duration,
        args.music_volume,
    )
    args.work_dir.mkdir(parents=True, exist_ok=True)
    subtitled_silent = args.work_dir / "customer_subtitled_silent.mp4"
    _burn_subtitles_only(
        args.visual_master,
        build_subtitle_cues(body_timings, preserve_input_lines=True),
        args.work_dir,
        subtitled_silent,
        duration,
        config,
    )
    _mux_with_music(
        subtitled_silent,
        args.music,
        args.output_with_subtitles,
        duration,
        args.music_volume,
    )

    payload = {
        "schema_version": "story-customer-background-render/v1",
        "passed": True,
        "critical_errors": [],
        "visual_master": _binding(args.visual_master),
        "music": _binding(args.music),
        "authoritative_timeline_receipt": _binding(args.authoritative_timeline_receipt),
        "assembly_plan": _binding(args.assembly_plan),
        "body_srt": _binding(args.body_srt),
        "body_line_range": [body_timings[0].index, body_timings[-1].index],
        "canvas": {"width": args.width, "height": args.height},
        "subtitle_render": {
            "overlay_origin": [0, 0],
            "overlay_canvas_matches_video": True,
            "single_line": True,
            "style": "clean",
            "cue_count": len(body_timings),
        },
        "audio_roles": {
            "with_subtitles": "music_only",
            "without_subtitles": "music_only",
        },
        "outputs": {
            "with_subtitles": _binding(args.output_with_subtitles),
            "without_subtitles": _binding(args.output_without_subtitles),
        },
    }
    args.render_receipt.parent.mkdir(parents=True, exist_ok=True)
    args.render_receipt.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
