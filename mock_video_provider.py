from __future__ import annotations

import argparse
import csv
import subprocess
from pathlib import Path


def parse_scenes(value: str) -> set[int]:
    return {int(part.strip()) for part in value.split(",") if part.strip()}


def main() -> None:
    parser = argparse.ArgumentParser(description="零费用本地图生视频供应商，仅用于测试和影子运行")
    parser.add_argument("--jobs-csv", required=True, type=Path)
    parser.add_argument("--images-dir", required=True, type=Path)
    parser.add_argument("--videos-dir", required=True, type=Path)
    parser.add_argument("--start-scene", default=1, type=int)
    parser.add_argument("--end-scene", default=9999, type=int)
    parser.add_argument("--scenes", default="")
    parser.add_argument("--limit", default=0, type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--submit-all-first", action="store_true")
    parser.add_argument("--max-submit-first", default=20, type=int)
    args, _unknown = parser.parse_known_args()

    jobs = args.jobs_csv.expanduser()
    images = args.images_dir.expanduser()
    videos = args.videos_dir.expanduser()
    videos.mkdir(parents=True, exist_ok=True)
    with jobs.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    for key in ("task_id", "status", "video_url", "error"):
        if key not in fieldnames:
            fieldnames.append(key)
    selected = parse_scenes(args.scenes)
    candidates = [
        row
        for row in rows
        if (int(row["scene"]) in selected if selected else args.start_scene <= int(row["scene"]) <= args.end_scene)
    ]
    if args.limit:
        candidates = candidates[: args.limit]
    for row in candidates:
        source = images / row["image_filename"]
        target = videos / row["target_video_filename"]
        if target.exists() and target.stat().st_size > 0:
            row["status"] = "downloaded"
            continue
        if not source.is_file():
            raise FileNotFoundError(f"模拟供应商缺少图片：{source}")
        if args.dry_run:
            print(f"MOCK DRY RUN scene={row['scene']} image={source} target={target}")
            continue
        duration = max(0.5, min(float(row.get("duration") or 1.0), 2.0))
        command = [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(source),
            "-t",
            f"{duration:.3f}",
            "-vf",
            "scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2",
            "-r",
            "24",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-an",
            str(target),
        ]
        process = subprocess.run(command, text=True, capture_output=True)
        if process.returncode != 0:
            row["status"] = "error"
            row["error"] = process.stderr[-1000:]
            _write_jobs(jobs, fieldnames, rows)
            raise RuntimeError(f"模拟供应商 FFmpeg 失败：{process.stderr[-500:]}")
        row["task_id"] = f"mock-{int(row['scene']):04d}"
        row["status"] = "downloaded"
        row["video_url"] = target.as_uri()
        row["error"] = ""
        _write_jobs(jobs, fieldnames, rows)
        print(f"MOCK GENERATED scene={row['scene']} target={target}")
    if args.dry_run:
        return
    _write_jobs(jobs, fieldnames, rows)


def _write_jobs(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
