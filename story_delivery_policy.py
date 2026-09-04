"""Cross-product delivery rules shared by every story consumer."""

from __future__ import annotations

import re


SUBTITLE_SCOPE_FULL = "opening_body_moral"
SUBTITLE_SCOPE_BODY = "body_only"
SUBTITLE_SCOPE_NONE = "none"
AUDIO_ROLE_MUSIC_ONLY = "music_only"
AUDIO_ROLE_NARRATION_MUSIC = "narration_plus_music"

SUBTITLE_SCOPE_BY_ARTIFACT = {
    "release_main": SUBTITLE_SCOPE_FULL,
    "release_library": SUBTITLE_SCOPE_FULL,
    "product_demo": SUBTITLE_SCOPE_FULL,
    "product_background_with_subtitles": SUBTITLE_SCOPE_BODY,
    "product_background_without_subtitles": SUBTITLE_SCOPE_NONE,
    "ppt_with_subtitles": SUBTITLE_SCOPE_BODY,
    "ppt_without_subtitles": SUBTITLE_SCOPE_NONE,
}

AUDIO_ROLE_BY_ARTIFACT = {
    "release_main": AUDIO_ROLE_NARRATION_MUSIC,
    "release_library": AUDIO_ROLE_NARRATION_MUSIC,
    "product_demo": AUDIO_ROLE_NARRATION_MUSIC,
    "product_background_with_subtitles": AUDIO_ROLE_MUSIC_ONLY,
    "product_background_without_subtitles": AUDIO_ROLE_MUSIC_ONLY,
    "product_a_only_background": AUDIO_ROLE_MUSIC_ONLY,
    "ppt_with_subtitles": AUDIO_ROLE_MUSIC_ONLY,
    "ppt_without_subtitles": AUDIO_ROLE_MUSIC_ONLY,
}


def subtitle_scope(artifact: str) -> str:
    try:
        return SUBTITLE_SCOPE_BY_ARTIFACT[artifact]
    except KeyError as exc:
        raise ValueError(f"unknown story subtitle artifact: {artifact}") from exc


def audio_role(artifact: str) -> str:
    """Return the required audible content for a delivered story artifact.

    ``music_only`` is deliberately not silence.  Customer reusable videos and
    PPTs retain the reviewed score while excluding the host narration.
    """

    try:
        return AUDIO_ROLE_BY_ARTIFACT[artifact]
    except KeyError as exc:
        raise ValueError(f"unknown story audio artifact: {artifact}") from exc


def customer_music_extension() -> str:
    """Return the single interoperable music extension used in customer packs."""

    return ".mp3"


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
