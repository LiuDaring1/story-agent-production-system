from __future__ import annotations

import hashlib
import json
import re
from typing import Sequence

PUBLIC_TEXT_TRANSFORM_VERSION = "story-public-text/v1"

_PRESENTER_INTRO = re.compile(
    r"(?:大家好\s*[，,。！？!?]?\s*)?"
    r"(?:我是|我叫)\s*"
    r"(?:[^\n。！？!?]{0,24}(?:姐姐|哥哥|老师|主持人)|_{2,})"
    r"\s*[。！？!?]?\s*"
)


def clean_public_story_text(text: str) -> str:
    """Remove presenter identity copy without changing story content.

    This transform is deliberately deterministic and shared by production and
    currentness validation.  It removes only an explicit presenter
    introduction; it does not infer or rewrite story characters.
    """

    return _PRESENTER_INTRO.sub("", str(text)).strip()


def compile_public_story_lines(raw_lines: Sequence[str]) -> list[str]:
    """Project raw semantic-source rows to stable customer-facing rows.

    Row positions are preserved so semantic-plan source indices remain bound to
    the raw source.  A removed presenter-only row therefore becomes an empty
    row rather than shifting every following source index.
    """

    return [clean_public_story_text(line) for line in raw_lines]


def public_line_list_sha256(lines: Sequence[str]) -> str:
    normalized = [re.sub(r"\s+", "", str(line)).strip() for line in lines]
    encoded = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
