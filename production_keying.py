from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping


PRODUCTION_KEYING_FILTER_VERSION = "story-production-keying-filter/v1"


def _setting(settings: Any, name: str, default: Any) -> Any:
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def person_grade_filter(settings: Any) -> str:
    value = str(_setting(settings, "person_grade", "none"))
    if value == "natural":
        return ",eq=contrast=1.05:saturation=1.07:brightness=0.01:gamma=0.99"
    if value == "log-soft":
        return ",eq=contrast=1.18:saturation=1.25:brightness=0.03:gamma=0.96"
    if value == "log-strong":
        return ",eq=contrast=1.30:saturation=1.35:brightness=0.04:gamma=0.92"
    return ""


def person_beauty_filter(settings: Any) -> str:
    if str(_setting(settings, "person_beauty", "none")) == "light":
        return "hqdn3d=1.2:1.0:3.0:2.0,unsharp=5:5:0.18:5:5:0.0"
    return ""


def production_keying_filter_parts(
    source: str,
    settings: Any,
    crop_filter: str = "",
) -> list[str]:
    """Build the one production keying graph used by evidence, Demo and Release."""

    grade = person_grade_filter(settings)
    beauty = person_beauty_filter(settings)
    beauty_chain = f"{beauty}," if beauty else ""
    keyer = str(_setting(settings, "keyer", "colorkey"))
    color = str(_setting(settings, "chroma_color", "0x00FF00"))
    similarity = float(_setting(settings, "chroma_similarity", 0.10))
    blend = float(_setting(settings, "chroma_blend", 0.0))
    if crop_filter and not crop_filter.endswith(","):
        crop_filter += ","
    if keyer == "chromakey":
        return [
            f"{source}{crop_filter}{beauty_chain}chromakey={color}:{similarity}:{blend},"
            f"format=rgba{grade}[person_keyed]"
        ]
    return [
        f"{source}{crop_filter}{beauty_chain}format=rgba,split[person_orig][person_keysrc]",
        f"[person_keysrc]colorkey={color}:{similarity}:{blend},alphaextract,erosion,dilation[person_mask]",
        f"[person_orig][person_mask]alphamerge,despill=type=green:mix=0.35{grade}[person_keyed]",
    ]


def production_keying_filter_chain(source: str, settings: Any, crop_filter: str = "") -> str:
    return ";".join(production_keying_filter_parts(source, settings, crop_filter))


def production_keying_contract(settings: Any) -> dict[str, Any]:
    crop = _setting(settings, "person_crop", None)
    return {
        "filter_version": PRODUCTION_KEYING_FILTER_VERSION,
        "keyer": str(_setting(settings, "keyer", "colorkey")),
        "chroma_color": str(_setting(settings, "chroma_color", "0x00FF00")),
        "chroma_similarity": float(_setting(settings, "chroma_similarity", 0.10)),
        "chroma_blend": float(_setting(settings, "chroma_blend", 0.0)),
        "person_grade": str(_setting(settings, "person_grade", "none")),
        "person_beauty": str(_setting(settings, "person_beauty", "none")),
        "person_crop": list(crop) if crop is not None else None,
        # Hash the actual graph template as well as its named version.  A graph
        # implementation change therefore invalidates old evidence even if a
        # maintainer accidentally forgets to bump the version constant.
        "filter_graph_template": production_keying_filter_chain("[source]", settings),
    }


def production_keying_fingerprint(settings: Any) -> str:
    payload = json.dumps(
        production_keying_contract(settings), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def render_production_keyed_foreground(source_frame: Path, output_path: Path, settings: Any) -> Path:
    crop = _setting(settings, "person_crop", None)
    crop_filter = ""
    if crop is not None:
        x, y, width, height = (int(value) for value in crop)
        crop_filter = f"crop={width}:{height}:{x}:{y},"
    graph = production_keying_filter_chain("[0:v]", settings, crop_filter)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(source_frame), "-filter_complex", graph,
            "-map", "[person_keyed]", "-frames:v", "1", "-update", "1", str(output_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return output_path


__all__ = [
    "PRODUCTION_KEYING_FILTER_VERSION",
    "person_beauty_filter",
    "person_grade_filter",
    "production_keying_contract",
    "production_keying_filter_chain",
    "production_keying_filter_parts",
    "production_keying_fingerprint",
    "render_production_keyed_foreground",
]
