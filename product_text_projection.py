from __future__ import annotations

import hashlib
import json
import re
from typing import Sequence

from story_semantics import SemanticKind, classify_story


PUBLIC_TEXT_TRANSFORM_VERSION = "story-public-text/v2"

_PRESENTER_INTRO = re.compile(
    r"(?:大家好\s*[，,。！？!?]?\s*)?"
    r"(?:我是|我叫)\s*"
    r"(?:[^\n。！？!?]{0,24}(?:姐姐|哥哥|老师|主持人)|_{2,})"
    r"\s*[。！？!?]?\s*"
)


def _clean_host_intro_line(text: str) -> str:
    """Clean one line already classified as ``HOST_INTRO``.

    The regular expression is intentionally applied only after the shared
    story semantic classifier has identified the line as an opening.  It may
    therefore retain an announcement following an inline presenter identity,
    but it can never erase matching dialogue in the story body or moral.
    """

    cleaned = _PRESENTER_INTRO.sub("", str(text)).strip()
    # Greeting-only and other conservatively classified host-opening rows have
    # no customer-facing story content.  Keep their source row as an empty
    # string so every later source index remains stable.
    return cleaned if cleaned != str(text).strip() else ""


def compile_public_story_lines(raw_lines: Sequence[str]) -> list[str]:
    """Project a complete transcript to stable customer-facing rows.

    Semantic classification is performed once over the complete ordered
    transcript.  Only trusted ``HOST_INTRO`` rows are cleaned; story body,
    moral, title, announcement and outro rows remain byte-for-byte unchanged.
    Row positions are preserved, so a removed presenter-only row becomes an
    empty row rather than shifting every following source index.
    """

    rows = [str(line) for line in raw_lines]
    semantics = classify_story(rows)
    return [
        _clean_host_intro_line(line)
        if semantics.kind_at(index) is SemanticKind.HOST_INTRO
        else line
        for index, line in enumerate(rows, start=1)
    ]


def clean_public_story_text(text: str) -> str:
    """Compatibility wrapper using the same semantic-aware projection.

    Multi-line callers retain their original line structure.  Production and
    currentness validation use :func:`compile_public_story_lines` directly so
    they always classify the complete transcript rather than isolated rows.
    """

    lines = str(text).splitlines()
    if not lines:
        return ""
    return "\n".join(compile_public_story_lines(lines)).strip()


def public_line_list_sha256(lines: Sequence[str]) -> str:
    normalized = [re.sub(r"\s+", "", str(line)).strip() for line in lines]
    encoded = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
