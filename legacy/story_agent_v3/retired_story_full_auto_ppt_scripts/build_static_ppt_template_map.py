#!/usr/bin/env python3
"""Archived template-following map for historical project reproduction only."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", required=True, type=Path)
    parser.add_argument("--output-map", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--deviation-log", required=True, type=Path)
    args = parser.parse_args()

    records: dict[int, list[dict]] = defaultdict(list)
    for line in args.inspect.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row.get("slide"), int):
            records[row["slide"]].append(row)

    output_slides = []
    audit_lines = [
        "Static story PPT template audit",
        "Scope: all source slides inspected; output reuses every source slide one-to-one.",
        "Preserved: title page, slide size, subtitle backdrop/text, existing transitions and embedded media where supported by the importer.",
        "Edited: only the inherited full-slide story image on slides 2 onward.",
    ]
    for slide_no in sorted(records):
        images = [row for row in records[slide_no] if row.get("kind") == "image"]
        if len(images) != 1:
            raise ValueError(f"source slide {slide_no} must have exactly one inherited image; got {len(images)}")
        image_id = str(images[0].get("id") or "")
        if not image_id:
            raise ValueError(f"source slide {slide_no} image lacks id")
        if slide_no == 1:
            role = "brand-title preserve-only"
            action = "keep"
            intent = "Preserve the approved title art."
        else:
            role = f"story-content shot {slide_no - 1:02d}"
            action = "replace"
            intent = "Replace the inherited full-slide picture with the director-shot-bound ImageGen illustration; preserve all other inherited elements."
        output_slides.append({
            "outputSlide": slide_no,
            "sourceSlide": slide_no,
            "narrativeRole": role,
            "reuseMode": "duplicate-slide",
            "editTargets": [{"sourceElementId": image_id, "action": action, "intent": intent}],
        })
        audit_lines.append(f"Slide {slide_no:02d}: image {image_id} -> {action}; other inherited elements preserved.")

    payload = {
        "schemaVersion": "template-frame-map/v1",
        "intent": "One-to-one static story PPT image replacement; TITLE + ordered director shots + optional MORAL.",
        "outputSlides": output_slides,
    }
    args.output_map.parent.mkdir(parents=True, exist_ok=True)
    args.output_map.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.audit.write_text("\n".join(audit_lines) + "\n", encoding="utf-8")
    args.deviation_log.write_text(
        f"No layout deviations. Story images on slides 2-{len(output_slides)} are intentionally replaced in place; title, subtitles, slide geometry, and timing contract remain inherited.\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "ok", "slide_count": len(output_slides), "output_map": str(args.output_map)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
