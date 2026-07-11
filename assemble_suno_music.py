from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import tempfile
from pathlib import Path


AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


def main() -> None:
    parser = argparse.ArgumentParser(description="按 Suno 音乐分段表裁剪并拼接完整背景音乐")
    parser.add_argument("--plan-csv", required=True, type=Path)
    parser.add_argument("--clips-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--fade", default=1.0, type=float)
    parser.add_argument("--bitrate", default="192k")
    args = parser.parse_args()

    plan_csv = args.plan_csv.expanduser()
    clips_dir = args.clips_dir.expanduser()
    output = args.output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)

    rows = _read_plan(plan_csv)
    if not rows:
        raise ValueError(f"音乐分段表为空：{plan_csv}")

    with tempfile.TemporaryDirectory(prefix="story_music_") as temp_name:
        temp_dir = Path(temp_name)
        segment_paths: list[Path] = []
        for row in rows:
            segment = int(row["segment"])
            duration = float(row["duration_sec"])
            if duration <= 0:
                raise ValueError(f"第 {segment} 段 duration_sec 无效：{duration}")
            source = _find_audio(row, clips_dir)
            target = temp_dir / f"segment_{segment:03}.wav"
            _render_segment(source, target, duration, min(args.fade, max(0.05, duration / 3)))
            segment_paths.append(target)

        concat_path = temp_dir / "concat.txt"
        concat_path.write_text("\n".join(f"file '{path.as_posix()}'" for path in segment_paths) + "\n", encoding="utf-8")
        _run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-c:a",
                "libmp3lame",
                "-b:a",
                args.bitrate,
                str(output),
            ]
        )

    print(f"已生成完整背景音乐：{output}")
    print(f"音乐段落数：{len(rows)}")
    print(f"计划总时长：{sum(float(row['duration_sec']) for row in rows):.2f} 秒")


def _read_plan(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        rows = [row for row in csv.DictReader(file) if row.get("segment", "").strip()]
    return sorted(rows, key=lambda row: int(row["segment"]))


def _find_audio(row: dict[str, str], clips_dir: Path) -> Path:
    target_name = row.get("target_audio_filename", "").strip()
    candidates: list[Path] = []
    if target_name:
        candidates.append(clips_dir / target_name)
    segment = int(row["segment"])
    candidates.extend(sorted(path for path in clips_dir.iterdir() if path.suffix.lower() in AUDIO_EXTENSIONS and path.name.startswith(f"{segment:02d}")))
    for candidate in candidates:
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    raise FileNotFoundError(f"找不到第 {segment:02d} 段音乐，请放到 {clips_dir}，文件名建议：{target_name}")


def _render_segment(source: Path, target: Path, duration: float, fade: float) -> None:
    fade_out_start = max(0.0, duration - fade)
    audio_filter = (
        f"atrim=0:{duration:.3f},"
        "asetpts=PTS-STARTPTS,"
        f"afade=t=in:st=0:d={fade:.3f},"
        f"afade=t=out:st={fade_out_start:.3f}:d={fade:.3f},"
        "loudnorm=I=-24:LRA=11:TP=-2,"
        "aresample=48000"
    )
    _run(
        [
            "ffmpeg",
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(source),
            "-t",
            f"{duration:.3f}",
            "-af",
            audio_filter,
            "-ac",
            "2",
            str(target),
        ]
    )


def _run(command: list[str]) -> None:
    if shutil.which(command[0]) is None:
        raise RuntimeError(f"没有找到命令：{command[0]}")
    process = subprocess.run(command, text=True, capture_output=True)
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip())


if __name__ == "__main__":
    main()
