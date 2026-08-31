"""Cross-product delivery rules shared by every story consumer."""

from __future__ import annotations

import re


SUBTITLE_SCOPE_FULL = "opening_body_moral"
SUBTITLE_SCOPE_BODY = "body_only"
SUBTITLE_SCOPE_NONE = "none"

SUBTITLE_SCOPE_BY_ARTIFACT = {
    "release_main": SUBTITLE_SCOPE_FULL,
    "release_library": SUBTITLE_SCOPE_FULL,
    "product_demo": SUBTITLE_SCOPE_FULL,
    "product_background_with_subtitles": SUBTITLE_SCOPE_BODY,
    "product_background_without_subtitles": SUBTITLE_SCOPE_NONE,
    "ppt_with_subtitles": SUBTITLE_SCOPE_BODY,
    "ppt_without_subtitles": SUBTITLE_SCOPE_NONE,
}


def subtitle_scope(artifact: str) -> str:
    try:
        return SUBTITLE_SCOPE_BY_ARTIFACT[artifact]
    except KeyError as exc:
        raise ValueError(f"unknown story subtitle artifact: {artifact}") from exc


def release_semantic_subtitle_artifact(variant: str) -> str:
    if variant not in {"main", "library"}:
        raise ValueError(f"unknown release variant: {variant}")
    return "demo_subtitles"


def normalize_duration_label(value: str) -> str:
    """Remove vague approximation wording from a factual duration label."""

    text = re.sub(r"^约\s*", "", str(value).strip())
    if "约" in text:
        raise ValueError("时长文案必须如实表达，不得使用“约”")
    return text


def high_quality_background_blur(label: str, radius: int, output: str) -> str:
    """Return the canonical Gaussian FFmpeg background blur stage."""

    amount = max(0, int(radius))
    if amount == 0:
        return f"[{label}]null[{output}]"
    return f"[{label}]gblur=sigma={amount}:steps=4[{output}]"
