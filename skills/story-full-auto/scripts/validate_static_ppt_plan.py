#!/usr/bin/env python3
"""CLI wrapper for the repository-wide static PPT contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from static_ppt_contract import validate_pair, validate_plan


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--director-plan", required=True, type=Path)
    parser.add_argument("--ppt-plan", required=True, type=Path)
    parser.add_argument("--pptx", action="append", default=[], type=Path)
    args = parser.parse_args()
    expected_ids, slides = validate_plan(args.director_plan, args.ppt_plan)
    if args.pptx:
        if len(args.pptx) != 2:
            raise ValueError("--pptx 必须按含字幕、无字幕顺序恰好传入两次")
        validate_pair(args.director_plan, args.ppt_plan, args.pptx[0], args.pptx[1])
    print(
        json.dumps(
            {
                "status": "ok",
                "director_shot_count": len(expected_ids)
                - 1
                - int(expected_ids[-1] == "MORAL"),
                "story_slide_count": len(slides)
                - 1
                - int(expected_ids[-1] == "MORAL"),
                "total_slide_count": len(slides),
                "slide_ids": expected_ids,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
