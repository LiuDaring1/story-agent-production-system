from __future__ import annotations

import json
import subprocess
from pathlib import Path


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}


def run_command(args: list[str]) -> None:
    from story_render_task import current_render_task
    if (Path(args[0]).name == "ffmpeg" and Path(args[-1]).suffix.lower() in VIDEO_EXTENSIONS
        and (current_render_task() is not None or any(codec in args for codec in ("libx264", "libx265", "h264_videotoolbox", "prores_ks")))):
        from story_encode import run_encode
        run_encode(args)
        return
    process = subprocess.run(args, text=True, capture_output=True)
    if process.returncode != 0:
        message = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(f"Command failed: {' '.join(args)}\n{message}")


def probe_duration(path: Path) -> float:
    process = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        capture_output=True,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or f"Unable to read duration: {path}")

    payload = json.loads(process.stdout)
    return float(payload["format"]["duration"])


def sorted_video_files(video_dir: Path) -> list[Path]:
    return sorted(
        [path for path in video_dir.iterdir() if path.suffix.lower() in VIDEO_EXTENSIONS],
        key=lambda path: path.name,
    )


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def ffmpeg_filter_path(path: Path) -> str:
    value = str(path.resolve())
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
    )
