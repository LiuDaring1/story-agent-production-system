#!/usr/bin/env python3
"""Assemble approved semantic R2V shots onto the authoritative story timeline."""

from __future__ import annotations

import argparse
import copy
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
    from story_render_task import current_render_task
    if current_render_task() is not None:
        from story_video_synthesizer.media import run_command
        return run_command(command)
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


def validate_formal_r2v_bindings(
    *,
    plan_path: Path,
    plan: dict[str, Any],
    videos_dir: Path,
    moral_video: Path | None,
    machine_qa_path: Path,
    provider_receipt_record: dict[str, Any],
) -> None:
    """Bind the exact formal inputs to the QA/review evidence set."""

    qa = json.loads(machine_qa_path.read_text(encoding="utf-8"))
    resolved_plan = plan_path.expanduser().resolve()
    if (
        Path(str(qa.get("plan_path") or "")).expanduser().resolve() != resolved_plan
        or qa.get("plan_sha256") != sha256_path(resolved_plan)
    ):
        raise ValueError("正式 R2V 组装的 --plan 不是整组机器 QA 审过的当前计划")
    resolved_videos = videos_dir.expanduser().resolve()
    if Path(str(qa.get("videos_dir") or "")).expanduser().resolve() != resolved_videos:
        raise ValueError("正式 R2V 组装的 --videos-dir 不是整组机器 QA 检查的目录")
    qa_receipt = Path(str(qa.get("receipt_path") or "")).expanduser().resolve()
    ledger_receipt = Path(str(provider_receipt_record.get("path") or "")).expanduser().resolve()
    if (
        qa_receipt != ledger_receipt
        or qa.get("receipt_sha256") != provider_receipt_record.get("sha256")
    ):
        raise ValueError("正式 R2V 组装的机器 QA 与账本当前供应商组回执不是同一版")
    clips = qa.get("clips")
    if not isinstance(clips, list):
        raise ValueError("正式 R2V 组装的机器 QA 缺少逐镜绑定")
    qa_by_shot = {
        str(item.get("shot_id") or ""): item
        for item in clips
        if isinstance(item, dict)
    }
    for shot in plan["shots"]:
        shot_id = str(shot.get("shot_id") or "").strip()
        item = qa_by_shot.get(shot_id)
        if not shot_id or not isinstance(item, dict):
            raise ValueError(f"正式 R2V 组装镜头 {shot_id or '<missing>'} 缺少当前机器 QA")
        source = (
            moral_video.expanduser().resolve()
            if shot_id.upper() == "MORAL" and moral_video is not None
            else resolved_videos / f"{shot_id}.mp4"
        )
        output = item.get("output") or item.get("video")
        if not isinstance(output, dict):
            output = {"path": item.get("path"), "sha256": item.get("sha256")}
        reviewed_path = Path(str(output.get("path") or "")).expanduser().resolve()
        if reviewed_path != source or output.get("sha256") != sha256_path(source):
            raise ValueError(f"正式 R2V 组装镜头 {shot_id} 不是整组机器 QA 审过的当前视频")


def validate_semantic_card_video_bindings(
    *, title_video: Path, moral_video: Path | None,
    motion_receipt_path: Path, require_moral: bool,
) -> None:
    """Require the CLI card videos to be the exact reviewed provider outputs."""

    receipt = json.loads(motion_receipt_path.read_text(encoding="utf-8"))
    cards = {
        str(item.get("card_kind") or ""): item
        for item in receipt.get("cards", [])
        if isinstance(item, dict)
    }
    requested = {"title_card": title_video.expanduser().resolve()}
    if require_moral:
        if moral_video is None:
            raise ValueError("正式计划包含 MORAL，但未提供回执绑定的 --moral-video")
        requested["moral_card"] = moral_video.expanduser().resolve()
    for kind, actual_path in requested.items():
        binding = cards.get(kind)
        if not isinstance(binding, dict):
            raise ValueError(f"语义卡微动回执缺少 {kind}")
        expected_path = Path(str(binding.get("output_video_path") or "")).expanduser().resolve()
        expected_sha = str(binding.get("output_video_sha256") or "").lower()
        if (
            actual_path != expected_path
            or not actual_path.is_file()
            or sha256_path(actual_path) != expected_sha
        ):
            option = "--title-video" if kind == "title_card" else "--moral-video"
            raise ValueError(f"正式组装的 {option} 不是语义卡微动回执审过的当前视频")


def expand_body_assembly_segments(
    plan: dict[str, Any], *, ledger: dict[str, Any], audio_path: Path,
    audio_duration: float, title_video: Path, moral_video: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fill only explicitly evidenced card complements, without changing the plan."""
    from math import isfinite
    from story_timeline import validate_authoritative_timeline_receipt
    from semantic_card_motion import semantic_card_motion_receipt_issues

    window = plan.get("body_audio_window")
    if not isinstance(window, dict):
        raise ValueError("body_audio_window 必须是带权威时间轴绑定的对象")
    artifacts = ledger.get("artifacts", {})
    paths = {}
    for name in ("authoritative_timeline_receipt", "semantic_card_motion_receipt"):
        record = artifacts.get(name, {})
        path = Path(str(record.get("path") or "")).expanduser().resolve()
        if not path.is_file() or sha256_path(path) != record.get("sha256"):
            raise ValueError(f"正文窗口组装缺少当前证据或哈希漂移：{name}")
        paths[name] = path
    timeline_path = paths["authoritative_timeline_receipt"]
    binding = window.get("authoritative_timeline_receipt") or {}
    if (Path(str(binding.get("path") or "")).expanduser().resolve() != timeline_path
            or binding.get("sha256") != sha256_path(timeline_path)):
        raise ValueError("body_audio_window 未绑定账本当前权威时间轴")
    timeline = validate_authoritative_timeline_receipt(
        timeline_path, expected_inputs=ledger.get("inputs", {}),
    )
    actual_audio = audio_path.expanduser().resolve()
    for label, record in (("ledger", ledger.get("inputs", {}).get("audio", {})),
                          ("plan", plan.get("source_audio", {})),
                          ("timeline", timeline.get("authoritative_audio", {}))):
        if (Path(str(record.get("path") or "")).expanduser().resolve() != actual_audio
                or record.get("sha256") != sha256_path(actual_audio)):
            raise ValueError(f"正文窗口 {label} 音频不是当前权威输入")
    for value in (timeline.get("audio_duration_seconds"), plan.get("source_audio", {}).get("duration_seconds")):
        if value is None or not isfinite(float(value)) or abs(float(value) - audio_duration) > 0.001:
            raise ValueError("正文窗口完整音频时长与权威时间轴不符")
    start, end = float(window.get("source_start", -1)), float(window.get("source_end", -1))
    if not all(isfinite(v) for v in (start, end, audio_duration)) or not 0 < start < end <= audio_duration:
        raise ValueError("正文窗口范围无效")
    shots = copy.deepcopy(plan["shots"])
    if not shots or any(str(s.get("shot_id", "")).upper() in {"TITLE", "MORAL"} for s in shots):
        raise ValueError("body_audio_window 只允许正文镜头，语义卡不能重复")
    cursor = start
    for shot in shots:
        left, right = float(shot.get("source_start", -1)), float(shot.get("source_end", -1))
        if not all(isfinite(v) for v in (left, right)) or abs(left - cursor) > 1e-6 or right <= left:
            raise ValueError("正文镜头未连续精确覆盖 body_audio_window")
        cursor = right
    if abs(cursor - end) > 1e-6:
        raise ValueError("正文末镜头与 body_audio_window 末尾不符")
    motion_path = paths["semantic_card_motion_receipt"]
    request_path = motion_path.parent / "semantic_card_motion_request.json"
    if not request_path.is_file():
        raise ValueError("正文窗口组装缺少明确语义卡窗口请求")
    issues = semantic_card_motion_receipt_issues(request_path, motion_path)
    if issues:
        raise ValueError("正文窗口语义卡微动回执失效：" + "；".join(issues))
    request = json.loads(request_path.read_text(encoding="utf-8"))
    receipt = json.loads(motion_path.read_text(encoding="utf-8"))
    if receipt.get("request_sha256") != sha256_path(request_path):
        raise ValueError("语义卡窗口请求哈希不匹配")
    cards = {}
    for card in request.get("cards", []):
        kind = card.get("card_kind")
        if kind in cards:
            raise ValueError("语义卡窗口请求重复")
        cards[kind] = card
    tail = audio_duration - end
    expected = {"title_card": start}
    if tail > 1e-6:
        expected["moral_card"] = tail
    for kind, length in expected.items():
        # Native requests store milliseconds (round(..., 3)); no frame-sized gap allowance.
        value = cards.get(kind, {}).get("presentation_window_seconds")
        if value is None or not isfinite(float(value)) or abs(float(value) - length) > 0.000501:
            raise ValueError(f"{kind} presentation_window 与正文前后补集不符")
    moral_text_binding = None
    if tail > 1e-6:
        from story_visual_contracts import confirmed_moral_text
        moral_text_binding = confirmed_moral_text(ledger['inputs'], timeline, end)
        normalize_layout = lambda text: ''.join(str(text).split())
        if normalize_layout(cards['moral_card'].get('text', '')) != normalize_layout(moral_text_binding['text']):
            raise ValueError('MORAL必须保留确认文稿完整寓意文本，不得缩写或删句')
    validate_semantic_card_video_bindings(
        title_video=title_video, moral_video=moral_video,
        motion_receipt_path=motion_path, require_moral=tail > 1e-6,
    )
    if tail > 1e-6:
        shots.append({"shot_id": "MORAL", "source_start": end, "source_end": audio_duration,
                      "story_text": str(cards["moral_card"].get("text") or ""),
                      "semantic_card": True})
    validate_contiguous_timeline(shots, audio_duration)
    evidence = {
        "policy": "bound_semantic_card_complements/v1",
        "body_audio_window": copy.deepcopy(window),
        "title_window": [0.0, start], "moral_window": [end, audio_duration] if tail > 1e-6 else None,
        "moral_text_binding": moral_text_binding,
        "motion_request": {"path": str(request_path), "sha256": sha256_path(request_path)},
        "motion_receipt": {"path": str(motion_path), "sha256": sha256_path(motion_path)},
        "director_plan_unchanged": True,
    }
    return shots, evidence


def load_authoritative_timings(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("权威时间轴必须是非空列表")
    rows: list[dict[str, Any]] = []
    previous_end = -1.0
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"权威时间轴第 {index} 行格式无效")
        text = str(item.get("line") or "").strip()
        start = float(item.get("source_start", -1))
        end = float(item.get("source_end", -1))
        if not text or start < 0 or end <= start or start + 1e-6 < previous_end:
            raise ValueError("权威时间轴必须文本非空、时长为正且单调")
        rows.append({"line": text, "source_start": start, "source_end": end})
        previous_end = end
    return rows


def retime_plan_to_authoritative_audio(
    plan: dict[str, Any],
    timing_rows: list[dict[str, Any]],
    *,
    audio_path: Path,
    audio_duration: float,
    timings_path: Path,
) -> dict[str, Any]:
    """Project immutable shot text groups onto confirmed audio timestamps.

    The next shot begins exactly when its first confirmed line begins; pauses
    stay with the preceding visual. TITLE covers everything before the first
    body line and MORAL covers the remaining confirmed suffix and audio tail.
    """

    updated = copy.deepcopy(plan)
    shots = updated.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ValueError("R2V 计划缺少 shots")
    body_shots = [shot for shot in shots if str(shot.get("shot_id") or "").upper() != "MORAL"]
    moral_shots = [shot for shot in shots if str(shot.get("shot_id") or "").upper() == "MORAL"]
    if len(moral_shots) > 1:
        raise ValueError("R2V 完整计划最多允许一个 MORAL 语义卡")

    cursor = 0
    matches: list[tuple[dict[str, Any], int, int]] = []
    for shot in body_shots:
        lines = [line.strip() for line in str(shot.get("story_text") or "").splitlines() if line.strip()]
        if not lines:
            raise ValueError(f"{shot.get('shot_id')}: story_text 为空，无法绑定权威时间轴")
        found = None
        for start_index in range(cursor, len(timing_rows) - len(lines) + 1):
            if [row["line"] for row in timing_rows[start_index : start_index + len(lines)]] == lines:
                found = start_index
                break
        if found is None:
            raise ValueError(f"{shot.get('shot_id')}: story_text 无法与确认字幕连续对齐")
        end_index = found + len(lines) - 1
        matches.append((shot, found, end_index))
        cursor = end_index + 1

    first_body_index = matches[0][1]
    moral_start_index = cursor
    if moral_shots and moral_start_index >= len(timing_rows):
        raise ValueError("MORAL 已计划但确认时间轴没有剩余寓意/收束行")
    if not moral_shots and moral_start_index < len(timing_rows):
        raise ValueError("确认时间轴仍有未分配行，缺少 MORAL 或正文镜头")
    moral_start = (
        float(timing_rows[moral_start_index]["source_start"])
        if moral_shots else audio_duration
    )

    for index, (shot, start_index, end_index) in enumerate(matches):
        next_start = (
            float(timing_rows[matches[index + 1][1]]["source_start"])
            if index + 1 < len(matches)
            else moral_start
        )
        shot["source_start"] = float(timing_rows[start_index]["source_start"])
        shot["source_end"] = next_start
        shot["authoritative_line_start"] = start_index + 1
        shot["authoritative_line_end"] = end_index + 1
    if moral_shots:
        moral = moral_shots[0]
        moral["source_start"] = moral_start
        moral["source_end"] = audio_duration
        moral["authoritative_line_start"] = moral_start_index + 1
        moral["authoritative_line_end"] = len(timing_rows)

    title_end = float(timing_rows[first_body_index]["source_start"])
    updated["title_window"] = [0.0, title_end]
    updated["moral_window"] = [moral_start, audio_duration] if moral_shots else None
    updated["source_audio"] = {
        "path": str(audio_path.resolve()),
        "sha256": sha256_path(audio_path),
        "duration_seconds": audio_duration,
    }
    updated["authoritative_timings"] = {
        "path": str(timings_path.resolve()),
        "sha256": sha256_path(timings_path),
        "time_basis": "source_start_source_end",
    }
    updated["timing_projection"] = "confirmed_audio_first_line_boundaries/v1"
    return updated


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


from story_render_task import render_entry

@render_entry
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
    parser.add_argument("--authoritative-timings", type=Path)
    parser.add_argument("--retimed-plan", type=Path)
    parser.add_argument("--moral-video", type=Path)
    parser.add_argument("--run-file", type=Path, help="正式组装必需的当前 story_run.json")
    parser.add_argument(
        "--diagnostic-preview",
        action="store_true",
        help="生成不可交付的诊断连续预览，不代替整组审核",
    )
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
    ledger = None
    if "body_audio_window" in plan and (args.authoritative_timings is not None or args.retimed_plan is not None):
        raise ValueError("body_audio_window 已绑定权威时间轴，禁止现场 retime")
    if not args.diagnostic_preview:
        if args.authoritative_timings is not None or args.retimed_plan is not None:
            raise ValueError(
                "正式 R2V 组装禁止现场 retime；请先生成新计划，对新计划和时间轴重做机器 QA/整组审核后再组装"
            )
        if args.run_file is None:
            raise ValueError("正式 R2V 组装必须提供 --run-file；诊断连续预览请显式使用 --diagnostic-preview")
        from semantic_card_motion import (
            semantic_card_generation_receipt_issues,
            semantic_card_motion_receipt_issues,
        )
        from story_artifact_validation import validate_artifact_semantics
        from story_run import file_sha256 as ledger_sha256, load_run

        from story_render_task import bind_render_task
        bind_render_task(args.run_file, outputs=[output_path, decisions_path, clips_dir])
        ledger = load_run(args.run_file.expanduser())
        current_audio = ledger.get("inputs", {}).get("audio", {})
        resolved_audio = audio_path.resolve()
        if (
            Path(str(current_audio.get("path") or "")).expanduser().resolve() != resolved_audio
            or current_audio.get("sha256") != ledger_sha256(resolved_audio)
        ):
            raise ValueError("正式组装的音频未绑定账本当前权威输入")
        required_evidence = (
            "authoritative_timeline_receipt",
            "semantic_card_generation_receipt",
            "semantic_card_motion_receipt",
            "r2v_provider_group_receipt",
            "r2v_group_machine_qa",
            "r2v_group_visual_review",
        )
        missing = [name for name in required_evidence if name not in ledger.get("artifacts", {})]
        if missing:
            raise ValueError("正式 R2V 组装缺少当前证据：" + ", ".join(missing))
        for name in required_evidence:
            record = ledger["artifacts"][name]
            evidence_path = Path(record["path"])
            if ledger_sha256(evidence_path) != record["sha256"]:
                raise ValueError(f"正式 R2V 组装证据哈希漂移：{name}")
            validate_artifact_semantics(
                name,
                evidence_path,
                registered_artifacts=ledger["artifacts"],
                registered_inputs=ledger["inputs"],
            )
        validate_formal_r2v_bindings(
            plan_path=plan_path,
            plan=plan,
            videos_dir=videos_dir,
            moral_video=args.moral_video,
            machine_qa_path=Path(ledger["artifacts"]["r2v_group_machine_qa"]["path"]),
            provider_receipt_record=ledger["artifacts"]["r2v_provider_group_receipt"],
        )
        generation_path = Path(ledger["artifacts"]["semantic_card_generation_receipt"]["path"])
        generation_issues = semantic_card_generation_receipt_issues(generation_path)
        if generation_issues:
            raise ValueError("正式 R2V 组装的语义卡生成回执失效：" + "；".join(generation_issues))
        motion_path = Path(ledger["artifacts"]["semantic_card_motion_receipt"]["path"])
        motion_issues = semantic_card_motion_receipt_issues(
            motion_path.parent / "semantic_card_motion_request.json",
            motion_path,
        )
        if motion_issues:
            raise ValueError("正式 R2V 组装的语义卡微动回执失效：" + "；".join(motion_issues))
        validate_semantic_card_video_bindings(
            title_video=title_video,
            moral_video=args.moral_video,
            motion_receipt_path=motion_path,
            require_moral=any(
                str(shot.get("shot_id") or "").upper() == "MORAL"
                for shot in plan["shots"]
            ),
        )
    audio_duration = duration(audio_path, args.ffprobe)
    if args.authoritative_timings is not None:
        if args.retimed_plan is None:
            raise ValueError("--authoritative-timings 必须同时提供 --retimed-plan，禁止覆盖旧计划")
        authoritative_timings = args.authoritative_timings.expanduser()
        plan = retime_plan_to_authoritative_audio(
            plan,
            load_authoritative_timings(authoritative_timings),
            audio_path=audio_path,
            audio_duration=audio_duration,
            timings_path=authoritative_timings,
        )
        plan_path = args.retimed_plan.expanduser()
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    body_evidence = None
    if "body_audio_window" in plan:
        if args.run_file is None:
            raise ValueError("body_audio_window 组装（含 diagnostic）必须提供 --run-file 和明确语义卡窗口证据")
        if ledger is None:
            from story_run import load_run
            ledger = load_run(args.run_file.expanduser())
        shots, body_evidence = expand_body_assembly_segments(
            plan, ledger=ledger, audio_path=audio_path, audio_duration=audio_duration,
            title_video=title_video, moral_video=args.moral_video,
        )
    else:
        shots = plan["shots"]
    if not shots:
        raise ValueError("R2V 计划没有镜头")
    validate_contiguous_timeline(shots, audio_duration)
    first_start = float(shots[0].get("source_start") or 0.0)
    if first_start <= 0:
        raise ValueError("首镜头必须晚于 0 秒，以便放置锁字片头")

    rows: list[dict[str, Any]] = []
    clips_dir.mkdir(parents=True, exist_ok=True)
    from story_render_task import current_render_task
    if current_render_task() is not None:
        clips_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="story-r2v-assembly-", dir=clips_dir if current_render_task() is not None else None) as temporary:
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
            source = (
                args.moral_video.expanduser()
                if shot_id.upper() == "MORAL" and args.moral_video is not None
                else videos_dir / f"{shot_id}.mp4"
            )
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
        "qualification": "diagnostic_preview_not_deliverable" if args.diagnostic_preview else "formal_reviewed_master",
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
        "body_assembly_evidence": body_evidence,
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
        "semantic_card": str(shot.get("shot_id") or "").upper() == "MORAL",
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
