from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping


PRODUCTION_KEYING_FILTER_VERSION = "story-production-keying-filter/v5"

# The former matte used ``erosion,dilation``.  On real 4K presenter footage
# that opening operation removed fine hair before putting a hard one-pixel
# outline back around the subject.  The colour-key matte deliberately shrinks
# only the green fringe, then feathers the alpha by less than one source pixel.  A
# stronger despill pass removes reflected green without changing the matte.
COLKEY_ALPHA_FEATHER_SIGMA = 0.55
COLKEY_DESPILL_MIX = 0.60
COLKEY_DESPILL_EXPAND = 0.08
# FFmpeg's despill filter darkens pixels in proportion to the computed spill
# map unless brightness compensation is supplied.  Keep this as an explicit
# production constant so real-footage comparisons can tune it without
# forking Demo and Release filter graphs.
COLKEY_DESPILL_BRIGHTNESS = 0.0
RVM_DESPILL_MIX = 1.0
RVM_DESPILL_EXPAND = 0.20
RVM_DESPILL_BRIGHTNESS = 0.20
# RVM already supplies the temporally stable neural alpha.  A single 3x3
# erosion pass moves only that matte inward by one source pixel, which removes
# the residual chroma fringe visible on the real presenter without rebuilding
# the matte or applying the destructive opening used by the former colour-key
# path.  Do not add dilation: that would restore the fringe we are removing.
RVM_ALPHA_CHOKE_FILTER = "erosion"
RVM_ALPHA_CHOKE_PIXELS = 1
RVM_ALPHA_CHOKE_CANDIDATES = (0, 1, 2)


def resolved_rvm_alpha_choke_pixels(settings: Any) -> int:
    """Return the reviewed per-source RVM matte inset.

    The one-pixel value remains the conservative initial recommendation, but
    it is no longer a global production constant.  Every project writes the
    selected value into its hash-bound keying preset after comparing 0/1/2 px
    candidates made from that project's own temporal RVM cache.
    """

    raw = _setting(settings, "rvm_alpha_choke_pixels", RVM_ALPHA_CHOKE_PIXELS)
    if isinstance(raw, bool):
        raise ValueError("rvm_alpha_choke_pixels must be an integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("rvm_alpha_choke_pixels must be an integer") from exc
    if value not in RVM_ALPHA_CHOKE_CANDIDATES:
        raise ValueError(
            "rvm_alpha_choke_pixels must be one of "
            + "/".join(str(item) for item in RVM_ALPHA_CHOKE_CANDIDATES)
        )
    return value


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
    if keyer == "rvm":
        # RVM is run once upstream and supplies an actual alpha-bearing
        # foreground video.  The shared FFmpeg graph moves that supplied matte
        # inward by one source pixel, removes residual green reflection and
        # applies the approved grade.  It never rebuilds a colour-key matte or
        # repeats the neural inference per deliverable.
        choke_pixels = resolved_rvm_alpha_choke_pixels(settings)
        choke_chain = ",".join(RVM_ALPHA_CHOKE_FILTER for _ in range(choke_pixels))
        alpha_chain = "alphaextract" + (f",{choke_chain}" if choke_chain else "")
        return [
            f"{source}{crop_filter}format=rgba,split[rvm_person_rgb][rvm_person_alpha_source]",
            f"[rvm_person_alpha_source]{alpha_chain}"
            "[rvm_person_alpha]",
            f"[rvm_person_rgb][rvm_person_alpha]alphamerge,despill=type=green:"
            f"mix={RVM_DESPILL_MIX}:expand={RVM_DESPILL_EXPAND}:"
            f"brightness={RVM_DESPILL_BRIGHTNESS}{grade}[person_keyed]"
        ]
    if keyer == "chromakey":
        return [
            f"{source}{crop_filter}{beauty_chain}chromakey={color}:{similarity}:{blend},"
            f"format=rgba,despill=type=green:mix=0.55:expand=0.06{grade}[person_keyed]"
        ]
    if keyer != "colorkey":
        raise ValueError(f"unsupported production keyer: {keyer}")
    return [
        f"{source}{crop_filter}{beauty_chain}format=rgba,split[person_orig][person_keysrc]",
        f"[person_keysrc]colorkey={color}:{similarity}:{blend},alphaextract,"
        f"erosion,gblur=sigma={COLKEY_ALPHA_FEATHER_SIGMA}[person_mask]",
        f"[person_orig][person_mask]alphamerge,despill=type=green:"
        f"mix={COLKEY_DESPILL_MIX}:expand={COLKEY_DESPILL_EXPAND}:"
        f"brightness={COLKEY_DESPILL_BRIGHTNESS}{grade}[person_keyed]",
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
        "rvm_model_sha256": str(_setting(settings, "rvm_model_sha256", "")),
        "rvm_foreground_sha256": str(_setting(settings, "rvm_foreground_sha256", "")),
        "rvm_receipt_sha256": str(_setting(settings, "rvm_receipt_sha256", "")),
        "rvm_input_size": [
            int(_setting(settings, "rvm_input_width", 1920)),
            int(_setting(settings, "rvm_input_height", 1080)),
        ],
        "rvm_downsample_ratio": float(_setting(settings, "rvm_downsample_ratio", 0.4)),
        "rvm_alpha_choke_filter": RVM_ALPHA_CHOKE_FILTER,
        "rvm_alpha_choke_pixels": resolved_rvm_alpha_choke_pixels(settings),
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
    keyer = str(_setting(settings, "keyer", "colorkey"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    input_frame = source_frame
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if keyer == "rvm":
        from rvm_keying import render_rvm_foreground_frame

        model_value = str(_setting(settings, "rvm_model_path", "")).strip()
        if not model_value:
            raise ValueError("RVM evidence rendering requires rvm_model_path")
        runtime_value = str(_setting(settings, "rvm_runtime_path", "")).strip()
        temporary = tempfile.TemporaryDirectory(prefix="story-rvm-evidence-")
        input_frame = Path(temporary.name) / "rvm_foreground.png"
        render_rvm_foreground_frame(
            source_frame,
            input_frame,
            model_path=Path(model_value),
            runtime_path=Path(runtime_value) if runtime_value else None,
            width=int(_setting(settings, "rvm_input_width", 1920)),
            height=int(_setting(settings, "rvm_input_height", 1080)),
            downsample_ratio=float(_setting(settings, "rvm_downsample_ratio", 0.4)),
        )
    try:
        graph = production_keying_filter_chain("[0:v]", settings, crop_filter)
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(input_frame), "-filter_complex", graph,
                "-map", "[person_keyed]", "-frames:v", "1", "-update", "1", str(output_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        if temporary is not None:
            temporary.cleanup()
    return output_path


def render_cached_rvm_foreground_frame(
    foreground_video: Path,
    timestamp: float,
    output_path: Path,
    settings: Any,
) -> Path:
    """Post-process one frame from the exact temporal RVM cache used downstream."""

    if str(_setting(settings, "keyer", "")) != "rvm":
        raise ValueError("cached RVM evidence requires keyer=rvm")
    if not foreground_video.is_file():
        raise FileNotFoundError(f"RVM foreground cache missing: {foreground_video}")
    if timestamp < 0:
        raise ValueError("RVM evidence timestamp must be non-negative")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    graph = production_keying_filter_chain("[0:v]", settings)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-c:v", "libvpx-vp9",
            "-ss", f"{timestamp:.6f}", "-i", str(foreground_video),
            "-filter_complex", graph, "-map", "[person_keyed]",
            "-frames:v", "1", "-update", "1", str(output_path),
        ],
        check=True,
    )
    return output_path


__all__ = [
    "PRODUCTION_KEYING_FILTER_VERSION",
    "RVM_ALPHA_CHOKE_FILTER",
    "RVM_ALPHA_CHOKE_CANDIDATES",
    "RVM_ALPHA_CHOKE_PIXELS",
    "person_beauty_filter",
    "person_grade_filter",
    "production_keying_contract",
    "production_keying_filter_chain",
    "production_keying_filter_parts",
    "production_keying_fingerprint",
    "render_cached_rvm_foreground_frame",
    "render_production_keyed_foreground",
    "resolved_rvm_alpha_choke_pixels",
]
