from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path

from story_video_synthesizer.image_video import IMAGE_EXTENSIONS, sorted_image_files


def main() -> None:
    parser = argparse.ArgumentParser(description="把文生图结果整理成图生视频流程使用的 images/ 目录")
    parser.add_argument("--source-dir", required=True, type=Path, help="文生图输出图片文件夹")
    parser.add_argument("--output-dir", required=True, type=Path, help="故事任务输出目录")
    parser.add_argument("--slug", required=True, help="故事 slug，例如 huluobo-yaoguai")
    parser.add_argument("--count", default=0, type=int, help="期望图片数量；0 表示不检查")
    args = parser.parse_args()

    source_dir = args.source_dir.expanduser()
    output_dir = args.output_dir.expanduser()
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    images = sorted_image_files(source_dir)
    if args.count and len(images) != args.count:
        raise ValueError(f"图片数量是 {len(images)}，期望是 {args.count}。")
    if not images:
        raise ValueError(f"没有找到图片：{source_dir}")

    rows: list[dict[str, str]] = []
    for index, source in enumerate(images, start=1):
        suffix = source.suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            continue
        target = images_dir / f"{args.slug}_scene_{index:02d}{suffix}"
        shutil.copy2(source, target)
        rows.append({"scene": f"{index:02d}", "source_image": str(source), "image_filename": target.name})

    manifest = output_dir / f"{args.slug}_images_manifest.csv"
    with manifest.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["scene", "source_image", "image_filename"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"已整理图片：{len(rows)} 张")
    print(f"图片目录：{images_dir}")
    print(f"清单：{manifest}")


if __name__ == "__main__":
    main()
