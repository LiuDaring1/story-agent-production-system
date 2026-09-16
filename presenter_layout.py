from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path

from PIL import Image


PRESENTER_LAYOUT_POLICY = "source-native-fixed-anchor/v2"
BODY_OVERFLOW_POLICY = {
    "schema_version": "story-presenter-body-overflow/v3",
    "sample_fps": 5.0,
    "visible_threshold": 0.70,
    "trigger_threshold": 0.55,
    "minimum_seconds": 0.40,
    "padding_seconds": 0.25,
    "alpha_threshold": 32,
    "body_core_quantiles": [0.20, 0.80],
}


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def body_overflow_cache_key(
    foreground_video: Path,
    *,
    fixed_anchor_x: int,
    canvas_width: int,
    source_width: int,
    source_height: int,
    rendered_height: int,
    fixed_anchor_y: int = 0,
    person_crop: Sequence[int] | None = None,
    person_layout_policy: str = PRESENTER_LAYOUT_POLICY,
    sample_fps: float = BODY_OVERFLOW_POLICY["sample_fps"],
    visible_threshold: float = BODY_OVERFLOW_POLICY["visible_threshold"],
    trigger_threshold: float = BODY_OVERFLOW_POLICY["trigger_threshold"],
    minimum_seconds: float = BODY_OVERFLOW_POLICY["minimum_seconds"],
    padding_seconds: float = BODY_OVERFLOW_POLICY["padding_seconds"],
) -> dict[str, object]:
    """Return the complete cache identity for the formal presenter scan."""

    source = foreground_video.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"RVM 前景不存在：{source}")
    issues = source_native_fixed_anchor_issues(
        source_width=int(source_width),
        source_height=int(source_height),
        rendered_height=int(rendered_height),
        person_x=int(fixed_anchor_x),
        person_y=int(fixed_anchor_y),
        person_crop=person_crop,
        policy=person_layout_policy,
        expected_x=int(fixed_anchor_x),
    )
    if issues:
        raise ValueError("人物躯干保护几何无效：" + "; ".join(issues))
    return {
        "source_sha256": _sha256_path(source),
        "fixed_anchor_x": int(fixed_anchor_x),
        "fixed_anchor_y": int(fixed_anchor_y),
        "canvas_width": int(canvas_width),
        "source_width": int(source_width),
        "source_height": int(source_height),
        "rendered_height": int(rendered_height),
        "person_crop": list(person_crop) if person_crop is not None else None,
        "person_layout_policy": person_layout_policy,
        "sample_fps": float(sample_fps),
        "visible_threshold": float(visible_threshold),
        "trigger_threshold": float(trigger_threshold),
        "minimum_seconds": float(minimum_seconds),
        "padding_seconds": float(padding_seconds),
        "alpha_threshold": int(BODY_OVERFLOW_POLICY["alpha_threshold"]),
        "body_core_quantiles": list(BODY_OVERFLOW_POLICY["body_core_quantiles"]),
        "scanner_code_sha256": _sha256_path(Path(__file__).resolve()),
    }


def source_native_layout_issues(
    *,
    source_width: int,
    source_height: int,
    rendered_height: int,
    person_crop: Sequence[int] | None,
    policy: str,
) -> list[str]:
    """Hard gate for the no-resize/no-crop presenter production policy."""

    if not policy:
        return []
    issues: list[str] = []
    if policy != PRESENTER_LAYOUT_POLICY:
        issues.append("presenter_layout_policy_unsupported")
        return issues
    if person_crop is not None:
        issues.append("presenter_source_native_person_crop_forbidden")
    if int(rendered_height) != int(source_height):
        issues.append("presenter_source_native_scale_forbidden")
    if min(int(source_width), int(source_height)) <= 0:
        issues.append("presenter_source_dimensions_invalid")
    return issues


def source_native_fixed_anchor_issues(
    *,
    source_width: int,
    source_height: int,
    rendered_height: int,
    person_x: int,
    person_y: int,
    person_crop: Sequence[int] | None,
    policy: str,
    expected_x: int | None = None,
) -> list[str]:
    """Hard gate shared by formal renders and local presenter repairs.

    Scaling the complete 1920x1080 stage for delivery is allowed. Resizing,
    cropping, or vertically moving the presenter inside that stage is not.
    """

    issues = source_native_layout_issues(
        source_width=source_width,
        source_height=source_height,
        rendered_height=rendered_height,
        person_crop=person_crop,
        policy=policy,
    )
    if not policy or policy != PRESENTER_LAYOUT_POLICY:
        return issues
    if int(person_y) != 0:
        issues.append("presenter_source_native_y_shift_forbidden")
    if expected_x is not None and int(person_x) != int(expected_x):
        issues.append("presenter_fixed_anchor_x_mismatch")
    return issues


def compile_fixed_anchor(
    samples: Iterable[tuple[float, int, int]],
    *,
    active_windows: Sequence[tuple[float, float]],
    initial_subject_bbox: Sequence[int],
    right_blank_rect: Sequence[int],
    canvas_width: int,
    edge_margin: int = 12,
    anticipation_seconds: float = 0.6,
    simplify_tolerance: int = 6,
) -> dict[str, object]:
    """Compute one immutable A-shot anchor from the opening neutral frame.

    The presenter's opening-frame center is aligned to the center of the
    right-hand blank rectangle.  Full-duration alpha samples are deliberately
    *not* used to chase later gestures: after the initial placement, the X
    coordinate stays fixed and natural hand/forearm overflow is allowed. A
    separate body-core scan may recommend a B-scene cut only when the torso is
    persistently and substantially outside the canvas; it never changes this
    transform. The output contains one X coordinate and no time-varying motion
    track.
    """

    if len(initial_subject_bbox) != 4 or len(right_blank_rect) != 4:
        raise ValueError("initial_subject_bbox/right_blank_rect must contain four integers")
    subject_x, _subject_y, subject_w, _subject_h = (int(value) for value in initial_subject_bbox)
    blank_x, _blank_y, blank_w, _blank_h = (int(value) for value in right_blank_rect)
    anchor_x = round(blank_x + blank_w / 2 - (subject_x + subject_w / 2))
    return {
        "policy": PRESENTER_LAYOUT_POLICY,
        "anchor_x": anchor_x,
        "edge_margin": int(edge_margin),
        "initial_subject_bbox": [subject_x, _subject_y, subject_w, _subject_h],
        "right_blank_rect": [blank_x, _blank_y, blank_w, _blank_h],
        "minimum_x": anchor_x,
        "maximum_x": anchor_x,
        "dynamic_repositioning": False,
        "gesture_overlap_policy": "allow_source_frame_overflow",
    }


def body_core_visibility_fraction(
    alpha: Image.Image,
    *,
    anchor_x: float,
    canvas_width: int,
    alpha_threshold: int = 32,
    lower_quantile: float = 0.20,
    upper_quantile: float = 0.80,
) -> float | None:
    """Return the visible fraction of the presenter's central alpha mass.

    The outer 20% of alpha pixels on each side are deliberately ignored. That
    removes hands, forearms, hair tips, and other sparse gesture outliers from
    the decision while retaining the head/torso mass. A hand may therefore
    leave the canvas without creating a false severe-overflow event.
    """

    if canvas_width <= 0:
        raise ValueError("canvas_width must be positive")
    if not 0.0 <= lower_quantile < upper_quantile <= 1.0:
        raise ValueError("body-core quantiles are invalid")
    mask = alpha.convert("L").point(lambda value: 255 if value > alpha_threshold else 0)
    histogram = [0] * mask.width
    pixels = mask.load()
    total = 0
    for x in range(mask.width):
        count = sum(1 for y in range(mask.height) if pixels[x, y])
        histogram[x] = count
        total += count
    if total <= 0:
        return None

    def quantile_x(target: float) -> int:
        threshold = target * total
        cumulative = 0
        for x, count in enumerate(histogram):
            cumulative += count
            if cumulative >= threshold:
                return x
        return mask.width - 1

    left = float(quantile_x(lower_quantile)) + float(anchor_x)
    right = float(quantile_x(upper_quantile)) + float(anchor_x)
    width = max(1.0, right - left)
    visible = max(0.0, min(float(canvas_width), right) - max(0.0, left))
    return max(0.0, min(1.0, visible / width))


def body_overflow_sample_geometry(
    *,
    fixed_anchor_x: int,
    canvas_width: int,
    source_width: int,
    source_height: int,
    rendered_height: int,
    person_crop: Sequence[int] | None = None,
    maximum_sample_width: int = 480,
) -> dict[str, float | int | list[int] | None]:
    """Map release-canvas coordinates into the downsampled alpha scan.

    The scanner decodes source pixels, while Release may render those pixels at
    another size.  Converting the fixed anchor and canvas width through the
    same rendered-height scale is therefore required for the scan to describe
    the actual composition rather than the source file's nominal dimensions.
    """

    if min(int(source_width), int(source_height), int(rendered_height), int(canvas_width)) <= 0:
        raise ValueError("presenter scan dimensions must be positive")
    crop: list[int] | None = None
    effective_width, effective_height = int(source_width), int(source_height)
    if person_crop is not None:
        if len(person_crop) != 4:
            raise ValueError("presenter scan crop must contain x,y,width,height")
        crop = [int(value) for value in person_crop]
        x, y, width, height = crop
        if min(width, height) <= 0 or min(x, y) < 0 or x + width > source_width or y + height > source_height:
            raise ValueError("presenter scan crop is outside the source canvas")
        effective_width, effective_height = width, height
    sample_width = max(160, min(int(maximum_sample_width), effective_width))
    render_scale = float(rendered_height) / float(effective_height)
    canvas_to_sample = float(sample_width) / (float(effective_width) * render_scale)
    return {
        "sample_width": sample_width,
        "anchor_x": float(fixed_anchor_x) * canvas_to_sample,
        "canvas_width": max(1, round(float(canvas_width) * canvas_to_sample)),
        "render_scale": render_scale,
        "person_crop": crop,
    }


def severe_body_overflow_windows(
    samples: Iterable[tuple[float, float | None]],
    *,
    visible_threshold: float = 0.70,
    trigger_threshold: float = 0.55,
    minimum_seconds: float = 0.40,
    sample_fps: float = 5.0,
    padding_seconds: float = 0.25,
    duration: float | None = None,
) -> list[tuple[float, float]]:
    """Group persistent, unmistakable torso overflow into B-scene windows.

    ``visible_threshold`` keeps the existing one-third-loss warning band, but
    a group is actionable only after at least one sample crosses the lower
    ``trigger_threshold``.  This hysteresis prevents a broad hand, forearm, or
    sleeve hovering near the edge from turning a merely borderline 65%-70%
    core estimate into an automatic scene cut.  A real body-core excursion
    still opens the window, and the warning-band samples keep its full padded
    duration.
    """

    if (
        not 0.0 < trigger_threshold < visible_threshold <= 1.0
        or sample_fps <= 0
    ):
        raise ValueError("overflow scan thresholds are invalid")
    severe = [
        (float(timestamp), float(visible))
        for timestamp, visible in samples
        if visible is not None and float(visible) < visible_threshold
    ]
    if not severe:
        return []
    max_gap = 1.5 / sample_fps
    groups: list[list[tuple[float, float]]] = [[severe[0]]]
    for timestamp, visible in severe[1:]:
        if timestamp - groups[-1][-1][0] <= max_gap:
            groups[-1].append((timestamp, visible))
        else:
            groups.append([(timestamp, visible)])
    minimum_samples = max(1, math.ceil(minimum_seconds * sample_fps))
    windows: list[tuple[float, float]] = []
    frame_seconds = 1.0 / sample_fps
    for group in groups:
        if (
            len(group) < minimum_samples
            or min(visible for _timestamp, visible in group) > trigger_threshold
        ):
            continue
        start = max(0.0, group[0][0] - padding_seconds)
        end = group[-1][0] + frame_seconds + padding_seconds
        if duration is not None:
            end = min(float(duration), end)
        windows.append((round(start, 3), round(max(start, end), 3)))
    return windows


def scan_rvm_body_overflow(
    foreground_video: Path,
    *,
    fixed_anchor_x: int,
    canvas_width: int,
    source_width: int,
    source_height: int,
    rendered_height: int,
    report_path: Path,
    fixed_anchor_y: int = 0,
    person_crop: Sequence[int] | None = None,
    person_layout_policy: str = PRESENTER_LAYOUT_POLICY,
    sample_fps: float = BODY_OVERFLOW_POLICY["sample_fps"],
    visible_threshold: float = BODY_OVERFLOW_POLICY["visible_threshold"],
    trigger_threshold: float = BODY_OVERFLOW_POLICY["trigger_threshold"],
    minimum_seconds: float = BODY_OVERFLOW_POLICY["minimum_seconds"],
    padding_seconds: float = BODY_OVERFLOW_POLICY["padding_seconds"],
) -> dict[str, object]:
    """Scan a cached VP9-alpha presenter before the one formal encode.

    This is a pre-render scene-selection aid, not a transform repair. It
    ignores hand/forearm outliers and only recommends B-scene windows when the
    central body mass is less than 70% visible for at least 0.4 seconds.
    """

    source = foreground_video.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"RVM 前景不存在：{source}")
    cache_key = body_overflow_cache_key(
        source,
        fixed_anchor_x=fixed_anchor_x,
        fixed_anchor_y=fixed_anchor_y,
        canvas_width=canvas_width,
        source_width=source_width,
        source_height=source_height,
        rendered_height=rendered_height,
        person_crop=person_crop,
        person_layout_policy=person_layout_policy,
        sample_fps=sample_fps,
        visible_threshold=visible_threshold,
        trigger_threshold=trigger_threshold,
        minimum_seconds=minimum_seconds,
        padding_seconds=padding_seconds,
    )
    try:
        existing = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {}
    if existing.get("cache_key") == cache_key:
        return existing

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(source),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        duration = float(probe.stdout.strip())
    except ValueError:
        duration = 0.0
    sample_geometry = body_overflow_sample_geometry(
        fixed_anchor_x=fixed_anchor_x,
        canvas_width=canvas_width,
        source_width=source_width,
        source_height=source_height,
        rendered_height=rendered_height,
        person_crop=person_crop,
    )
    sample_width = int(sample_geometry["sample_width"])
    anchor_scaled = float(sample_geometry["anchor_x"])
    canvas_scaled = int(sample_geometry["canvas_width"])
    crop_filter = ""
    if person_crop is not None:
        crop_x, crop_y, crop_width, crop_height = (int(value) for value in person_crop)
        crop_filter = f"crop={crop_width}:{crop_height}:{crop_x}:{crop_y},"
    samples: list[tuple[float, float | None]] = []
    with tempfile.TemporaryDirectory(prefix="presenter-core-scan-") as temporary:
        pattern = Path(temporary) / "alpha_%06d.png"
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-c:v", "libvpx-vp9",
            "-i", str(source), "-vf",
            f"fps={sample_fps:.6f},{crop_filter}format=rgba,alphaextract,scale={sample_width}:-1:flags=neighbor",
            str(pattern), "-y",
        ]
        process = subprocess.run(command, text=True, capture_output=True, check=False)
        if process.returncode != 0:
            raise RuntimeError("人物躯干出画扫描失败：" + process.stderr.strip())
        for index, path in enumerate(sorted(Path(temporary).glob("alpha_*.png"))):
            with Image.open(path) as alpha:
                visible = body_core_visibility_fraction(
                    alpha,
                    anchor_x=anchor_scaled,
                    canvas_width=canvas_scaled,
                )
            samples.append((round(index / sample_fps, 3), visible))
    windows = severe_body_overflow_windows(
        samples,
        visible_threshold=visible_threshold,
        trigger_threshold=trigger_threshold,
        minimum_seconds=minimum_seconds,
        sample_fps=sample_fps,
        padding_seconds=padding_seconds,
        duration=duration if duration > 0 else None,
    )
    severe_samples = [
        {"time": timestamp, "visible_core_fraction": float(visible)}
        for timestamp, visible in samples
        if visible is not None and visible < visible_threshold
    ]
    payload: dict[str, object] = {
        "schema_version": BODY_OVERFLOW_POLICY["schema_version"],
        "cache_key": cache_key,
        "source": {
            "path": str(source),
            "sha256": cache_key["source_sha256"],
            "bytes": source.stat().st_size,
        },
        "scanner_code": {
            "path": str(Path(__file__).resolve()),
            "sha256": cache_key["scanner_code_sha256"],
            "bytes": Path(__file__).stat().st_size,
            "source_relative_path": "presenter_layout.py",
        },
        "policy": "fixed_anchor_never_scale_or_move; borderline_arm_or_hand_overflow_allowed; unmistakable_torso_excursion_cuts_to_b",
        "body_core_quantiles": list(BODY_OVERFLOW_POLICY["body_core_quantiles"]),
        "warning_visible_threshold": float(visible_threshold),
        "unmistakable_trigger_threshold": float(trigger_threshold),
        "duration_seconds": duration if duration > 0 else None,
        "severe_windows": [list(window) for window in windows],
        "samples": [
            {
                "time": timestamp,
                "visible_core_fraction": None if visible is None else float(visible),
            }
            for timestamp, visible in samples
        ],
        "severe_samples": severe_samples,
        "sample_count": len(samples),
        "scan_complete": True,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


__all__ = [
    "BODY_OVERFLOW_POLICY",
    "PRESENTER_LAYOUT_POLICY",
    "body_overflow_cache_key",
    "compile_fixed_anchor",
    "body_core_visibility_fraction",
    "scan_rvm_body_overflow",
    "severe_body_overflow_windows",
    "source_native_fixed_anchor_issues",
    "source_native_layout_issues",
]
