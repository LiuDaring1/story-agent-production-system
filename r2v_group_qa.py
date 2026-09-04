#!/usr/bin/env python3
"""Deterministic integrity QA for a completed story R2V group."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from fractions import Fraction
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SAFE_POSTPROCESS_STRATEGIES = {"trim", "crop", "trim_crop"}


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 顶层必须是对象：{path}")
    return payload


def run_text(command: list[str]) -> str:
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(f"命令失败（{completed.returncode}）：{' '.join(command)}\n{completed.stderr[-2000:]}")
    return completed.stdout


def probe_video(path: Path, ffprobe: str) -> dict[str, Any]:
    payload = json.loads(run_text([
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,pix_fmt,r_frame_rate:format=duration",
        "-of", "json",
        str(path),
    ]))
    streams = payload.get("streams") or []
    if not streams:
        raise ValueError(f"缺少视频流：{path}")
    stream = streams[0]
    return {
        "duration": float((payload.get("format") or {}).get("duration") or 0.0),
        "codec": str(stream.get("codec_name") or ""),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "pix_fmt": str(stream.get("pix_fmt") or ""),
        "frame_rate": str(stream.get("r_frame_rate") or ""),
    }


def black_intervals(path: Path, ffmpeg: str) -> list[dict[str, float]]:
    completed = subprocess.run([
        ffmpeg,
        "-hide_banner", "-nostats", "-i", str(path),
        "-vf", "blackdetect=d=0.12:pic_th=0.98:pix_th=0.02",
        "-an", "-f", "null", "-",
    ], text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(f"黑帧检测失败：{path}\n{completed.stderr[-2000:]}")
    matches = re.findall(
        r"black_start:(?P<start>[0-9.]+)\s+black_end:(?P<end>[0-9.]+)\s+black_duration:(?P<duration>[0-9.]+)",
        completed.stderr,
    )
    return [
        {"start": float(start), "end": float(end), "duration": float(duration)}
        for start, end, duration in matches
    ]


def exact_freeze_intervals(
    path: Path, ffmpeg: str, frame_rate: str, minimum_seconds: float = 0.45
) -> list[dict[str, float]]:
    """Detect cloned decoded frames without penalizing naturally slow motion."""
    try:
        fps = float(Fraction(frame_rate))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    if fps <= 0:
        return []
    completed = subprocess.run([
        ffmpeg,
        "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-map", "0:v:0", "-f", "framemd5", "-",
    ], text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(f"重复帧检测失败：{path}\n{completed.stderr[-2000:]}")
    hashes: list[str] = []
    for line in completed.stdout.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 6:
            hashes.append(parts[-1])
    minimum_frames = max(2, int(round(minimum_seconds * fps)))
    intervals: list[dict[str, float]] = []
    run_start = 0
    for index in range(1, len(hashes) + 1):
        if index < len(hashes) and hashes[index] == hashes[run_start]:
            continue
        count = index - run_start
        if count >= minimum_frames:
            intervals.append({
                "start": run_start / fps,
                "end": index / fps,
                "duration": count / fps,
            })
        run_start = index
    return intervals


def expected_shots(plan: dict[str, Any]) -> list[dict[str, Any]]:
    shots = plan.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ValueError("计划缺少 shots")
    result: list[dict[str, Any]] = []
    for row in shots:
        if not isinstance(row, dict):
            raise ValueError("计划 shots 每项必须是对象")
        shot_id = str(row.get("shot_id") or row.get("id") or "").strip()
        if not shot_id:
            raise ValueError("计划镜头缺少 shot_id")
        generation = row.get("generation") if isinstance(row.get("generation"), dict) else {}
        seconds = int(
            row.get("provider_duration_seconds")
            or row.get("provider_seconds")
            or generation.get("duration_seconds")
            or row.get("seconds")
            or 0
        )
        result.append({"shot_id": shot_id, "seconds": seconds})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="R2V 整组机器完整性 QA")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--videos-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()

    plan_path = args.plan.expanduser()
    receipt_path = args.receipt.expanduser()
    videos_dir = args.videos_dir.expanduser()
    output_path = args.output.expanduser()
    plan = load_json(plan_path)
    receipt = load_json(receipt_path)
    plan_sha = sha256_path(plan_path)
    shots = expected_shots(plan)
    receipt_rows = receipt.get("shots") if isinstance(receipt.get("shots"), list) else []
    receipt_by_filename = {
        str(row.get("filename") or ""): row
        for row in receipt_rows
        if isinstance(row, dict)
    }

    errors: list[str] = []
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    seen_hashes: dict[str, str] = {}
    for shot in shots:
        shot_id = shot["shot_id"]
        filename = f"{shot_id}.mp4"
        path = videos_dir / filename
        row: dict[str, Any] = {"shot_id": shot_id, "path": str(path), "issues": []}
        if not path.is_file() or path.stat().st_size <= 0:
            issue = f"{shot_id}: 缺少或空视频 {filename}"
            errors.append(issue)
            row["issues"].append(issue)
            rows.append(row)
            continue
        try:
            metrics = probe_video(path, args.ffprobe)
            digest = sha256_path(path)
            row.update(metrics)
            row["sha256"] = digest
            row["bytes"] = path.stat().st_size
            if metrics["duration"] < 1.0:
                row["issues"].append("时长小于 1 秒")
            expected_seconds = int(shot["seconds"] or 0)
            if expected_seconds and abs(metrics["duration"] - expected_seconds) > 0.8:
                row["issues"].append(
                    f"生成时长 {metrics['duration']:.3f}s 与计划 {expected_seconds}s 偏差超过 0.8s"
                )
            if metrics["width"] <= 0 or metrics["height"] <= 0:
                row["issues"].append("视频尺寸无效")
            intervals = black_intervals(path, args.ffmpeg)
            row["black_intervals"] = intervals
            if any(value["duration"] >= 0.5 for value in intervals):
                row["issues"].append("存在持续至少 0.5 秒的黑屏")
            freezes = exact_freeze_intervals(path, args.ffmpeg, metrics["frame_rate"])
            row["exact_freeze_intervals"] = freezes
            if freezes:
                row["issues"].append("存在持续至少 0.45 秒的精确重复帧/定帧")
            if digest in seen_hashes:
                row["issues"].append(f"与 {seen_hashes[digest]} 文件完全相同")
            else:
                seen_hashes[digest] = shot_id
            receipt_row = receipt_by_filename.get(filename)
            if receipt_row:
                allowed_statuses = {"downloaded", "postprocessed"}
                if receipt_row.get("status") not in allowed_statuses:
                    row["issues"].append(
                        f"镜头状态不是 downloaded/postprocessed：{receipt_row.get('status')}"
                    )
                elif str(receipt_row.get("output_sha256") or "") != digest:
                    row["issues"].append("视频哈希与供应商回执不一致")
                if receipt_row.get("status") == "postprocessed":
                    strategy = str(receipt_row.get("local_strategy") or "")
                    if strategy not in SAFE_POSTPROCESS_STRATEGIES:
                        row["issues"].append(
                            "本地后期不是同源 trim/crop/trim_crop，可能引入重复、静帧或跨镜头内容"
                        )
            else:
                row["issues"].append("缺少供应商回执")
        except Exception as exc:
            row["issues"].append(str(exc))
        for issue in row["issues"]:
            errors.append(f"{shot_id}: {issue}")
        rows.append(row)

    task_ids = [str(row.get("task_id") or "") for row in receipt_rows if isinstance(row, dict)]
    expected_receipts = len(rows)
    if len(receipt_rows) != expected_receipts:
        errors.append(f"供应商回执应有 {expected_receipts} 项，实际 {len(receipt_rows)}")
    if any(not task_id for task_id in task_ids) or len(set(task_ids)) != len(task_ids):
        errors.append("供应商任务号缺失或重复")
    allowed_statuses = {"downloaded", "postprocessed"}
    if any(
        str(row.get("status") or "") not in allowed_statuses
        for row in receipt_rows if isinstance(row, dict)
    ):
        errors.append("镜头回执包含未下载完成或未登记的本地后期任务")

    payload = {
        "schema_version": "story-r2v-group-machine-qa-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plan_path": str(plan_path),
        "plan_sha256": plan_sha,
        "receipt_path": str(receipt_path),
        "receipt_sha256": sha256_path(receipt_path),
        "videos_dir": str(videos_dir),
        "expected_shots": len(shots),
        "checked_shots": len(rows),
        "paid_tasks": len(receipt_rows),
        "unique_paid_task_ids": len(set(task_ids)),
        "passed": not errors,
        # Keep the producer contract aligned with story_run's fail-closed
        # machine-QA registry.  A passing report must state this explicitly;
        # absence is not equivalent to an empty critical-error list.
        "critical_errors": [] if not errors else ["one_or_more_machine_checks_failed"],
        "errors": errors,
        "warnings": warnings,
        "clips": rows,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output_path),
        "passed": payload["passed"],
        "expected_shots": payload["expected_shots"],
        "checked_shots": payload["checked_shots"],
        "errors": errors,
    }, ensure_ascii=False, indent=2))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
