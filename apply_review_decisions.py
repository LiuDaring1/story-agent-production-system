from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path

from story_video_synthesizer.volcengine_video import read_jobs_csv, write_jobs_csv


def main() -> None:
    parser = argparse.ArgumentParser(description="根据审核结果整理最终合成用 clips 和逐行文本")
    parser.add_argument("--jobs-csv", required=True, type=Path)
    parser.add_argument("--videos-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--decisions-csv", default="", help="审核页导出的 review_decisions.csv；不传则默认使用已下载片段")
    parser.add_argument("--short-slug", default="hlbyg")
    args = parser.parse_args()

    jobs_path = args.jobs_csv.expanduser()
    videos_dir = args.videos_dir.expanduser()
    output_dir = args.output_dir.expanduser()
    clips_dir = output_dir / "clips"

    decisions = _read_decisions(Path(args.decisions_csv).expanduser()) if args.decisions_csv.strip() else {}
    rows = read_jobs_csv(jobs_path)
    redo_rows = _redo_scenes(rows, decisions)
    if redo_rows:
        scenes = "、".join(redo_rows)
        raise ValueError(f"仍有标记为重做的镜头未处理：{scenes}。请先重跑这些镜头并审核通过，再应用审核/合成。")

    if clips_dir.exists():
        shutil.rmtree(clips_dir)
    clips_dir.mkdir(parents=True, exist_ok=True)

    selected: list[dict[str, str]] = []
    review_rows: list[dict[str, str]] = []

    for row in rows:
        scene = row["scene"].zfill(2)
        decision = decisions.get(scene, {})
        review_status = decision.get("review_status") or ("approved" if row.get("status") == "downloaded" else "todo")
        notes = decision.get("notes", row.get("notes", ""))
        prompt = (decision.get("prompt") or "").strip()
        if prompt:
            row["prompt"] = prompt
        row["review_status"] = review_status
        row["review_notes"] = notes
        review_rows.append(
            {
                "scene": scene,
                "review_status": review_status,
                "notes": notes,
                "video": row.get("target_video_filename", ""),
            }
        )
        if review_status != "approved":
            continue
        source = videos_dir / row["target_video_filename"]
        if not source.exists():
            raise FileNotFoundError(f"已通过但找不到视频：{source}")
        target = clips_dir / f"{int(scene):02d}_{args.short_slug}.mp4"
        shutil.copy2(source, target)
        selected.append(row)

    if not selected:
        raise ValueError("没有可合成的已通过片段。")

    script_path = output_dir / "script_lines.txt"
    script_path.write_text("\n".join(row.get("story_text", "") for row in selected) + "\n", encoding="utf-8")
    summary_path = output_dir / "review_summary.csv"
    _write_csv(summary_path, review_rows)
    write_jobs_csv(jobs_path, rows)

    print(f"已整理通过片段：{len(selected)} 个")
    print(f"片段文件夹：{clips_dir}")
    print(f"逐行文本：{script_path}")
    print(f"审核摘要：{summary_path}")


def _read_decisions(path: Path) -> dict[str, dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    return {row["scene"].zfill(2): row for row in rows if row.get("scene", "").strip()}


def _redo_scenes(rows: list[dict[str, str]], decisions: dict[str, dict[str, str]]) -> list[str]:
    redo_rows: list[str] = []
    for row in rows:
        scene = row["scene"].zfill(2)
        decision = decisions.get(scene, {})
        review_status = decision.get("review_status") or ("approved" if row.get("status") == "downloaded" else "todo")
        if review_status == "redo":
            redo_rows.append(scene)
    return redo_rows


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
