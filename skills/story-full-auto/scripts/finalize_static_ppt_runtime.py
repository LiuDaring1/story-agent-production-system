#!/usr/bin/env python3
"""Apply the production automatic-advance and embedded-BGM contract to a static PPT."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from product_package import embed_ppt_bgm, patch_pptx_slide_advances


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--pptx", required=True, type=Path)
    args = parser.parse_args()

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    slides = plan.get("slides")
    if not isinstance(slides, list) or len(slides) < 2:
        raise ValueError("静态 PPT 计划必须至少包含 TITLE 和一个导演镜头")
    slide_ids = [str(row.get("shot_id") or "") for row in slides if isinstance(row, dict)]
    if len(slide_ids) != len(slides) or slide_ids[0] != "TITLE" or len(set(slide_ids)) != len(slide_ids):
        raise ValueError("静态 PPT 计划页 ID 缺失、重复或未以 TITLE 开头")
    durations = [float(row.get("duration_seconds") or 0) for row in slides]
    if any(value <= 0 for value in durations):
        raise ValueError("静态 PPT 存在无效自动翻页时长")
    music = Path(str(plan.get("music_path") or ""))
    if not music.is_file():
        raise FileNotFoundError(f"静态 PPT 配乐不存在：{music}")
    patch_pptx_slide_advances(args.pptx, durations)
    embed_ppt_bgm(args.pptx, music)
    print(json.dumps({
        "status": "ok",
        "pptx": str(args.pptx),
        "slide_count": len(slides),
        "slide_ids": slide_ids,
        "duration_seconds": sum(durations),
        "music_path": str(music),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
