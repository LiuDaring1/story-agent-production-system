from __future__ import annotations

import hashlib
import json
import re
from typing import Sequence

from story_semantics import SemanticKind, classify_story


PUBLIC_TEXT_TRANSFORM_VERSION = "story-public-text/v2"
ANNOTATION_TEXT_TRANSFORM_VERSION = "story-annotation-text/v1"
ANNOTATION_IDENTITY_PLACEHOLDER = "大家好，我是________。"

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


def compile_annotation_story_lines(raw_lines: Sequence[str]) -> list[str]:
    """Project transcript rows for the customer-editable performance script.

    Unlike public video/manuscript output, the annotation document keeps an
    explicit blank identity slot so the customer can fill in their own name.
    The original presenter identity is never retained and source row indices
    remain unchanged.
    """

    rows = [str(line) for line in raw_lines]
    semantics = classify_story(rows)
    projected: list[str] = []
    for index, line in enumerate(rows, start=1):
        if semantics.kind_at(index) is not SemanticKind.HOST_INTRO:
            projected.append(line)
            continue
        match = _PRESENTER_INTRO.search(line)
        if match is None:
            projected.append(line)
            continue
        remainder = (line[:match.start()] + line[match.end():]).strip()
        remainder = re.sub(r"^[，,：:；;。！？!?\s]+", "", remainder)
        projected.append(ANNOTATION_IDENTITY_PLACEHOLDER + remainder)
    return projected


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


def customer_manuscript_paragraphs(story_name: str, source_text: str) -> list[str]:
    """Return the customer-facing title plus natural source paragraphs.

    Subtitle/script rows are a timing representation and must not become the
    document's paragraph structure.  This function preserves the paragraphs
    and punctuation of the confirmed manuscript while applying only the
    public presenter-identity projection.
    """

    cleaned = clean_public_story_text(source_text)
    paragraphs = [line.strip() for line in cleaned.splitlines() if line.strip()]
    normalized_name = re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", story_name)
    if paragraphs:
        normalized_first = re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", paragraphs[0])
        if normalized_first == normalized_name:
            paragraphs.pop(0)
    return [f"《{story_name}》", *paragraphs]


def customer_manuscript_form_issues(
    paragraphs: Sequence[str],
    selected_script_lines: Sequence[str],
) -> list[str]:
    """Validate semantic equality without demanding subtitle-shaped layout."""

    issues: list[str] = []
    body = [str(item).strip() for item in paragraphs[1:] if str(item).strip()]
    selected = [str(item).strip() for item in selected_script_lines if str(item).strip()]

    def content_key(items: Sequence[str]) -> str:
        return "".join(re.findall(r"[0-9A-Za-z\u3400-\u9fff]+", "".join(items)))

    if not body:
        return ["manuscript_natural_paragraphs_missing"]
    if content_key(body) != content_key(selected):
        issues.append("manuscript_natural_content_mismatch")

    if len(selected) >= 8:
        compact_lengths = [len(content_key([item])) for item in body]
        short_ratio = sum(length <= 18 for length in compact_lengths) / len(compact_lengths)
        line_count_ratio = len(body) / max(1, len(selected))
        punctuation_count = sum(
            len(re.findall(r"[，。！？；：,.!?;:]", item)) for item in body
        )
        if line_count_ratio >= 0.6 and short_ratio >= 0.5:
            issues.append("manuscript_subtitle_line_layout")
        if punctuation_count < max(2, len(body) // 2):
            issues.append("manuscript_natural_punctuation_missing")
    return issues


def public_line_list_sha256(lines: Sequence[str]) -> str:
    normalized = [re.sub(r"\s+", "", str(line)).strip() for line in lines]
    encoded = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
