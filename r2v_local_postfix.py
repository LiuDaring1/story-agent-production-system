#!/usr/bin/env python3
"""Apply deterministic, non-paid local repairs to an approved R2V candidate set.

The operation recipe is data-driven so story-specific shot IDs, paths and edit
decisions stay outside reusable code.  Original provider files are never
modified; every derivative records its parent hash and exact operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SAFE_LOCAL_STRATEGIES = {"trim", "crop", "trim_crop"}
LEGACY_DISCONTINUOUS_STRATEGIES = {
    "video_bridge",
    "cut_to_hold",
    "petal_separation",
    "still_then_crop",
    "prior_then_crop",
}


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


def run(command: list[str]) -> None:
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(
            f"命令失败（{completed.returncode}）：{' '.join(command)}\n"
            f"{completed.stderr[-4000:]}"
        )


def normalize_filter() -> str:
    return "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,setsar=1,fps=24"


def concat_segments(segments: list[Path], output: Path, ffmpeg: str, work: Path) -> None:
    concat_file = work / f"{output.stem}_concat.txt"
    concat_file.write_text("".join(f"file '{path}'\n" for path in segments), encoding="utf-8")
    run([
        ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", "24", "-movflags", "+faststart", str(output),
    ])


def encode_trim(source: Path, output: Path, start: float, duration: float, ffmpeg: str) -> None:
    run([
        ffmpeg, "-y", "-ss", f"{start:.6f}", "-t", f"{duration:.6f}", "-i", str(source),
        "-vf", normalize_filter(), "-an", "-c:v", "libx264", "-preset", "medium",
        "-crf", "18", "-pix_fmt", "yuv420p", "-r", "24", "-movflags", "+faststart",
        str(output),
    ])


def apply_trim_crop(
    *, source: Path, output: Path, start: float, seconds: float,
    crop: str | None, ffmpeg: str,
) -> list[str]:
    if start < 0 or seconds <= 0:
        raise ValueError("trim/crop 的 start 与 seconds 必须形成正时长区间")
    filters = []
    if crop:
        filters.append(crop)
    filters.append(normalize_filter())
    command = [
        ffmpeg, "-y", "-ss", f"{start:.6f}", "-t", f"{seconds:.6f}",
        "-i", str(source), "-vf", ",".join(filters), "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", "24", "-movflags", "+faststart", str(output),
    ]
    run(command)
    return command


def validate_operations(operations: Any, *, allow_legacy: bool) -> list[dict[str, Any]]:
    if not isinstance(operations, list) or not operations:
        raise ValueError("本地修复配方缺少 operations")
    validated: list[dict[str, Any]] = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise ValueError(f"operations[{index}] 必须是对象")
        shot_id = str(operation.get("shot_id") or "").strip()
        strategy = str(operation.get("strategy") or "").strip()
        if not shot_id or not strategy:
            raise ValueError(f"operations[{index}] 缺少 shot_id 或 strategy")
        if strategy in LEGACY_DISCONTINUOUS_STRATEGIES and not allow_legacy:
            raise ValueError(
                f"{shot_id}: {strategy} 会插入定帧、静态素材或其他镜头内容；"
                "正式生产只允许同一源镜头的 trim/crop/trim_crop"
            )
        if strategy not in SAFE_LOCAL_STRATEGIES | LEGACY_DISCONTINUOUS_STRATEGIES:
            raise ValueError(f"未知本地修复策略：{strategy}")
        validated.append(operation)
    return validated


def apply_cut_to_hold(
    *, source: Path, hold_source: Path, output: Path, action_seconds: float,
    hold_at: float, total_seconds: float, ffmpeg: str,
) -> list[str]:
    hold_seconds = total_seconds - action_seconds
    if hold_seconds <= 0:
        raise ValueError("cut_to_hold 的 hold 时长必须为正")
    command = [
        ffmpeg, "-y", "-i", str(source), "-i", str(hold_source),
        "-filter_complex",
        (
            f"[0:v]trim=start=0:end={action_seconds:.6f},setpts=PTS-STARTPTS,"
            f"{normalize_filter()}[a];"
            f"[1:v]trim=start={hold_at:.6f}:end={hold_at + 1 / 24:.6f},"
            f"setpts=PTS-STARTPTS,{normalize_filter()},"
            f"tpad=stop_mode=clone:stop_duration={hold_seconds:.6f},"
            f"trim=duration={hold_seconds:.6f}[b];"
            "[a][b]concat=n=2:v=1:a=0,format=yuv420p[v]"
        ),
        "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", "24", "-movflags", "+faststart", str(output),
    ]
    run(command)
    return command


def apply_video_bridge(
    *, source: Path, bridge_source: Path, output: Path, action_seconds: float,
    bridge_start: float, bridge_seconds: float, ffmpeg: str, work: Path,
) -> list[list[str]]:
    action = work / "video_bridge_action.mp4"
    bridge = work / "video_bridge_destination.mp4"
    encode_trim(source, action, 0.0, action_seconds, ffmpeg)
    encode_trim(bridge_source, bridge, bridge_start, bridge_seconds, ffmpeg)
    concat_segments([action, bridge], output, ffmpeg, work)
    return [
        ["encode_trim", str(source), "0.000000", f"{action_seconds:.6f}"],
        ["encode_trim", str(bridge_source), f"{bridge_start:.6f}", f"{bridge_seconds:.6f}"],
    ]


def apply_petal_separation(
    *, source: Path, background: Path, flower_four: Path, flower_five: Path,
    arctic: Path, output: Path, total_seconds: float, ffmpeg: str, work: Path,
) -> list[list[str]]:
    action_seconds = 3.0
    insert_seconds = 3.0
    arctic_seconds = total_seconds - action_seconds - insert_seconds
    if arctic_seconds <= 0:
        raise ValueError("petal_separation 总时长过短")
    action = work / "petal_action.mp4"
    insert = work / "petal_insert.mp4"
    destination = work / "petal_destination.mp4"
    command_action = [
        ffmpeg, "-y", "-t", f"{action_seconds:.6f}", "-i", str(source),
        "-vf", normalize_filter(), "-an", "-c:v", "libx264", "-preset", "medium",
        "-crf", "18", "-pix_fmt", "yuv420p", "-r", "24", str(action),
    ]
    run(command_action)
    command_insert = [
        ffmpeg, "-y", "-loop", "1", "-t", f"{insert_seconds:.6f}", "-i", str(background),
        "-loop", "1", "-t", f"{insert_seconds:.6f}", "-i", str(flower_four),
        "-loop", "1", "-t", f"{insert_seconds:.6f}", "-i", str(flower_five),
        "-filter_complex",
        (
            "[0:v]scale=1280:720,setsar=1,fps=24[bg];"
            "[1:v]scale=-1:620[flower];"
            "[bg][flower]overlay=x=(W-w)/2:y=70[base];"
            "[2:v]crop=500:430:600:0,scale=300:-1[blue];"
            "[base][blue]overlay="
            "x='590+360*t/2.2':y='45-360*t/2.2':enable='lte(t,2.2)',"
            "format=yuv420p[out]"
        ),
        "-map", "[out]", "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", "24", "-t", f"{insert_seconds:.6f}", str(insert),
    ]
    run(command_insert)
    command_destination = [
        ffmpeg, "-y", "-loop", "1", "-t", f"{arctic_seconds:.6f}", "-i", str(arctic),
        "-vf",
        (
            "scale=1344:756,crop=1280:720:"
            f"x='(iw-ow)*t/{arctic_seconds:.6f}':y='(ih-oh)/2',setsar=1,fps=24"
        ),
        "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", "24", str(destination),
    ]
    run(command_destination)
    concat_segments([action, insert, destination], output, ffmpeg, work)
    return [command_action, command_insert, command_destination]


def apply_still_then_crop(
    *, still: Path, source: Path, output: Path, still_seconds: float,
    source_start: float, crop: str, total_seconds: float, ffmpeg: str, work: Path,
) -> list[list[str]]:
    motion_seconds = total_seconds - still_seconds
    still_video = work / "still_insert.mp4"
    motion_video = work / "cropped_motion.mp4"
    command_still = [
        ffmpeg, "-y", "-loop", "1", "-t", f"{still_seconds:.6f}", "-i", str(still),
        "-vf", "scale=1344:756,crop=1280:720:x='(iw-ow)*t/5':y=(ih-oh)/2,setsar=1,fps=24",
        "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", "24", str(still_video),
    ]
    run(command_still)
    command_motion = [
        ffmpeg, "-y", "-ss", f"{source_start:.6f}", "-t", f"{motion_seconds:.6f}",
        "-i", str(source), "-vf", f"{crop},scale=1280:720,setsar=1,fps=24",
        "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", "24", str(motion_video),
    ]
    run(command_motion)
    concat_segments([still_video, motion_video], output, ffmpeg, work)
    return [command_still, command_motion]


def apply_prior_then_crop(
    *, prior: Path, source: Path, output: Path, prior_start: float,
    prior_seconds: float, crop: str, total_seconds: float, ffmpeg: str, work: Path,
) -> list[list[str]]:
    motion_seconds = total_seconds - prior_seconds
    prior_video = work / "prior_context.mp4"
    motion_video = work / "current_crop.mp4"
    encode_trim(prior, prior_video, prior_start, prior_seconds, ffmpeg)
    command_motion = [
        ffmpeg, "-y", "-t", f"{motion_seconds:.6f}", "-i", str(source),
        "-vf", f"{crop},scale=1280:720,setsar=1,fps=24",
        "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", "24", str(motion_video),
    ]
    run(command_motion)
    concat_segments([prior_video, motion_video], output, ffmpeg, work)
    return [["encode_trim", str(prior), f"{prior_start:.6f}", f"{prior_seconds:.6f}"], command_motion]


def main() -> int:
    parser = argparse.ArgumentParser(description="按数据配方执行 R2V 确定性本地修复")
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--base-dir", required=True, type=Path)
    parser.add_argument("--base-receipt", required=True, type=Path)
    parser.add_argument("--assets-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument(
        "--allow-legacy-discontinuous-repair",
        action="store_true",
        help="仅用于复现旧项目；允许会跨镜头、插静帧或定帧的历史修复策略",
    )
    args = parser.parse_args()

    spec_path = args.spec.expanduser()
    base_dir = args.base_dir.expanduser()
    assets_dir = args.assets_dir.expanduser()
    output_dir = args.output_dir.expanduser()
    manifest_path = args.manifest.expanduser()
    receipt_path = args.base_receipt.expanduser()
    spec = load_json(spec_path)
    receipt = load_json(receipt_path)
    operations = validate_operations(
        spec.get("operations"),
        allow_legacy=args.allow_legacy_discontinuous_repair,
    )
    total_seconds = float(spec.get("target_duration_seconds") or 10.041667)
    output_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted(base_dir.glob("S*.mp4")):
        shutil.copy2(source, output_dir / source.name)

    decisions: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="r2v-local-postfix-") as temporary:
        temporary_root = Path(temporary)
        for operation in operations:
            if not isinstance(operation, dict):
                raise ValueError("operations 每项必须是对象")
            shot_id = str(operation.get("shot_id") or "").strip()
            strategy = str(operation.get("strategy") or "").strip()
            if not shot_id or not strategy:
                raise ValueError("本地修复项缺少 shot_id 或 strategy")
            source = base_dir / f"{shot_id}.mp4"
            output = output_dir / f"{shot_id}.mp4"
            if not source.is_file():
                raise FileNotFoundError(source)
            work = temporary_root / shot_id
            work.mkdir(parents=True, exist_ok=True)
            commands: Any
            if strategy in SAFE_LOCAL_STRATEGIES:
                start = float(operation.get("start") or 0.0)
                seconds = float(operation.get("seconds") or total_seconds)
                crop = str(operation.get("crop") or "").strip() or None
                if strategy == "trim":
                    crop = None
                elif strategy == "crop":
                    start = 0.0
                    seconds = total_seconds
                commands = [apply_trim_crop(
                    source=source,
                    output=output,
                    start=start,
                    seconds=seconds,
                    crop=crop,
                    ffmpeg=args.ffmpeg,
                )]
            elif strategy == "video_bridge":
                bridge_source = base_dir / str(operation["bridge_source"])
                commands = apply_video_bridge(
                    source=source,
                    bridge_source=bridge_source,
                    output=output,
                    action_seconds=float(operation["action_seconds"]),
                    bridge_start=float(operation.get("bridge_start") or 0.0),
                    bridge_seconds=float(operation["bridge_seconds"]),
                    ffmpeg=args.ffmpeg,
                    work=work,
                )
            elif strategy == "cut_to_hold":
                hold_source = base_dir / str(operation["hold_source"])
                commands = [apply_cut_to_hold(
                    source=source,
                    hold_source=hold_source,
                    output=output,
                    action_seconds=float(operation["action_seconds"]),
                    hold_at=float(operation.get("hold_at") or 0.2),
                    total_seconds=total_seconds,
                    ffmpeg=args.ffmpeg,
                )]
            elif strategy == "petal_separation":
                commands = apply_petal_separation(
                    source=source,
                    background=assets_dir / str(operation["background"]),
                    flower_four=assets_dir / str(operation["flower_four"]),
                    flower_five=assets_dir / str(operation["flower_five"]),
                    arctic=assets_dir / str(operation["arctic"]),
                    output=output,
                    total_seconds=total_seconds,
                    ffmpeg=args.ffmpeg,
                    work=work,
                )
            elif strategy == "still_then_crop":
                commands = apply_still_then_crop(
                    still=assets_dir / str(operation["still"]),
                    source=source,
                    output=output,
                    still_seconds=float(operation["still_seconds"]),
                    source_start=float(operation.get("source_start") or 0.0),
                    crop=str(operation["crop"]),
                    total_seconds=total_seconds,
                    ffmpeg=args.ffmpeg,
                    work=work,
                )
            elif strategy == "prior_then_crop":
                prior = base_dir / str(operation["prior_source"])
                commands = apply_prior_then_crop(
                    prior=prior,
                    source=source,
                    output=output,
                    prior_start=float(operation["prior_start"]),
                    prior_seconds=float(operation["prior_seconds"]),
                    crop=str(operation["crop"]),
                    total_seconds=total_seconds,
                    ffmpeg=args.ffmpeg,
                    work=work,
                )
            if not output.is_file() or output.stat().st_size <= 0:
                raise RuntimeError(f"本地修复没有产生有效文件：{shot_id}")
            decisions.append({
                "shot_id": shot_id,
                "strategy": strategy,
                "reason": str(operation.get("reason") or ""),
                "parent_path": str(source),
                "parent_sha256": sha256_path(source),
                "output_path": str(output),
                "output_sha256": sha256_path(output),
                "paid_generation": False,
                "source_shot_ids": [shot_id],
                "continuous_source_only": strategy in SAFE_LOCAL_STRATEGIES,
                "parameters": {k: v for k, v in operation.items() if k not in {"reason"}},
                "commands": commands,
            })

    decision_by_filename = {f"{row['shot_id']}.mp4": row for row in decisions}
    receipt_rows: list[dict[str, Any]] = []
    for row in receipt.get("shots") or []:
        if not isinstance(row, dict):
            continue
        updated = dict(row)
        filename = str(updated.get("filename") or "")
        decision = decision_by_filename.get(filename)
        if decision:
            updated["parent_output_sha256"] = str(updated.get("output_sha256") or "")
            updated["output_sha256"] = decision["output_sha256"]
            updated["status"] = "postprocessed"
            updated["local_strategy"] = decision["strategy"]
        receipt_rows.append(updated)
    post_receipt = dict(receipt)
    post_receipt["schema_version"] = "story-r2v-postprocessed-receipt-v1"
    post_receipt["shots"] = receipt_rows
    post_receipt_path = output_dir / "r2v_postprocessed_receipt.json"
    post_receipt_path.write_text(
        json.dumps(post_receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    payload = {
        "schema_version": "story-r2v-local-postfix-decisions-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "spec_path": str(spec_path),
        "spec_sha256": sha256_path(spec_path),
        "base_dir": str(base_dir),
        "base_receipt_path": str(receipt_path),
        "base_receipt_sha256": sha256_path(receipt_path),
        "output_dir": str(output_dir),
        "postprocessed_receipt_path": str(post_receipt_path),
        "postprocessed_receipt_sha256": sha256_path(post_receipt_path),
        "paid_generation_count": 0,
        "decisions": decisions,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output_dir": str(output_dir),
        "manifest": str(manifest_path),
        "manifest_sha256": sha256_path(manifest_path),
        "receipt": str(post_receipt_path),
        "repairs": len(decisions),
        "paid_generation_count": 0,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
