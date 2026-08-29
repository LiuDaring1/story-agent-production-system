from __future__ import annotations

from collections.abc import Iterable, Sequence


PRESENTER_LAYOUT_POLICY = "source-native-fixed-anchor/v2"


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
    coordinate stays fixed and natural source-frame hand overflow is allowed.
    The output contains one X coordinate and no time-varying motion track.
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


__all__ = [
    "PRESENTER_LAYOUT_POLICY",
    "compile_fixed_anchor",
    "source_native_layout_issues",
]
