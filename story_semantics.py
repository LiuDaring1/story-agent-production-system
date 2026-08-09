"""Deterministic semantic contract for a complete story transcript.

The production pipeline has several consumers of the same line-by-line
transcript.  They must agree on which lines are an opening, the narrative,
the moral, and the closing instead of each consumer applying a slightly
different regular expression.  This module deliberately keeps the input
lines untouched and only adds semantic metadata to them.

The classifier is intentionally conservative.  Opening labels are considered
only in the transcript prefix and moral/closing labels are considered only in
the tail after the opening.  Consequently a sentence such as ``妈妈说`` in
the story body cannot turn into a presenter introduction or an outro merely
because it contains the character ``说``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence


class SemanticKind(str, Enum):
    """The semantic sections understood by all story outputs."""

    TITLE = "title"
    HOST_INTRO = "host_intro"
    STORY_ANNOUNCEMENT = "story_announcement"
    STORY_BODY = "story_body"
    MORAL = "moral"
    OUTRO = "outro"


# A short alias is useful to callers that call these sections "segments".
SegmentKind = SemanticKind


class StoryOutput(str, Enum):
    """Named output policies.

    The values are intentionally stable strings because manifests and older
    callers commonly pass output names through JSON/CLI arguments.
    """

    DEMO = "demo"
    RELEASE = "release"
    NARRATION_FULL = "narration_full"
    SALES_SUBTITLES = "sales_subtitles"
    BACKGROUND_SUBTITLES = "background_subtitles"
    PPT = "ppt"
    STORY_TEXT = "story_text"
    READING_ANNOTATION = "reading_annotation"

    # Enum aliases used by a few older callers.
    FULL = "narration_full"
    SALES = "sales_subtitles"
    ANNOTATION = "reading_annotation"


OutputKind = StoryOutput
ArtifactKind = StoryOutput
SemanticSection = SemanticKind


@dataclass(frozen=True)
class StoryLine:
    """One source line with its original text and 1-based source line number."""

    line_number: int
    text: str

    @property
    def index(self) -> int:
        """Compatibility alias used by timing code and older scripts."""

        return self.line_number

    @property
    def number(self) -> int:
        return self.line_number

    @property
    def source_line(self) -> int:
        return self.line_number

    @property
    def original_text(self) -> str:
        return self.text

    @property
    def original(self) -> str:
        return self.text


@dataclass(frozen=True)
class SemanticSegment:
    """A contiguous semantic range, retaining the source lines verbatim."""

    kind: SemanticKind
    start_line: int
    end_line: int
    lines: tuple[StoryLine, ...]

    @property
    def label(self) -> str:
        return self.kind.value

    @property
    def kind_name(self) -> str:
        return self.kind.value

    @property
    def start(self) -> int:
        return self.start_line

    @property
    def end(self) -> int:
        return self.end_line

    @property
    def line_start(self) -> int:
        return self.start_line

    @property
    def line_end(self) -> int:
        return self.end_line

    @property
    def start_index(self) -> int:
        return self.start_line

    @property
    def end_index(self) -> int:
        return self.end_line

    @property
    def line_numbers(self) -> tuple[int, ...]:
        return tuple(line.line_number for line in self.lines)

    @property
    def line_indices(self) -> tuple[int, ...]:
        return self.line_numbers

    @property
    def text(self) -> str:
        # Joining is only a view over the original lines; each line's text is
        # never stripped, normalized, or otherwise rewritten.
        return "\n".join(line.text for line in self.lines)

    @property
    def original_text(self) -> str:
        return self.text


@dataclass(frozen=True)
class StorySemantics:
    """Classification result for a complete transcript."""

    lines: tuple[StoryLine, ...]
    segments: tuple[SemanticSegment, ...]
    _kinds: tuple[SemanticKind | None, ...]

    @property
    def original_lines(self) -> tuple[str, ...]:
        return tuple(line.text for line in self.lines)

    @property
    def original_text(self) -> str:
        return "\n".join(self.original_lines)

    @property
    def line_texts(self) -> tuple[str, ...]:
        return self.original_lines

    @property
    def source_lines(self) -> tuple[StoryLine, ...]:
        return self.lines

    @property
    def source_line_numbers(self) -> tuple[int, ...]:
        return tuple(line.line_number for line in self.lines)

    @property
    def sections(self) -> dict[str, SemanticSegment]:
        """First segment for each kind, for simple dictionary-style callers."""

        result: dict[str, SemanticSegment] = {}
        for segment in self.segments:
            result.setdefault(segment.kind.value, segment)
        return result

    @property
    def by_kind(self) -> dict[str, tuple[SemanticSegment, ...]]:
        result: dict[str, list[SemanticSegment]] = {}
        for segment in self.segments:
            result.setdefault(segment.kind.value, []).append(segment)
        return {name: tuple(items) for name, items in result.items()}

    @property
    def ranges(self) -> dict[str, tuple[int, int]]:
        return {name: (segment.start_line, segment.end_line) for name, segment in self.sections.items()}

    @property
    def semantic_ranges(self) -> dict[str, tuple[int, int]]:
        return self.ranges

    @property
    def kind_by_line(self) -> dict[int, SemanticKind | None]:
        return {line.line_number: kind for line, kind in zip(self.lines, self._kinds)}

    @property
    def title(self) -> str | None:
        segment = self.segment(SemanticKind.TITLE)
        return segment.text if segment else None

    @property
    def title_text(self) -> str | None:
        return self.title

    @property
    def title_line(self) -> StoryLine | None:
        segment = self.segment(SemanticKind.TITLE)
        return segment.lines[0] if segment and segment.lines else None

    def kind_at(self, line_number: int) -> SemanticKind | None:
        for line, kind in zip(self.lines, self._kinds):
            if line.line_number == line_number:
                return kind
        return None

    def segment(self, kind: SemanticKind | str) -> SemanticSegment | None:
        wanted = _coerce_kind(kind)
        for segment in self.segments:
            if segment.kind is wanted:
                return segment
        return None

    def segments_for(self, kind: SemanticKind | str) -> tuple[SemanticSegment, ...]:
        wanted = _coerce_kind(kind)
        return tuple(segment for segment in self.segments if segment.kind is wanted)

    def lines_for(self, output_kind: StoryOutput | str) -> tuple[StoryLine, ...]:
        return tuple(lines_for_output(self, output_kind))

    def select_lines(self, output_kind: StoryOutput | str) -> tuple[StoryLine, ...]:
        return self.lines_for(output_kind)

    def texts_for(self, output_kind: StoryOutput | str) -> tuple[str, ...]:
        return tuple(line.text for line in self.lines_for(output_kind))

    def to_dict(self) -> dict[str, Any]:
        """Serialize metadata without losing the original line text."""

        return {
            "lines": [
                {"line_number": line.line_number, "text": line.text}
                for line in self.lines
            ],
            "segments": [
                {
                    "kind": segment.kind.value,
                    "start_line": segment.start_line,
                    "end_line": segment.end_line,
                    "line_numbers": list(segment.line_numbers),
                    "text": segment.text,
                }
                for segment in self.segments
            ],
        }


# Opening phrases are anchored.  Matching an occurrence in the middle of a
# narrative line is precisely what caused ordinary dialogue (for example
# ``妈妈说``) to be misclassified by the old pipeline.
_GREETING_RE = re.compile(
    r"^(?:大家好|小朋友们好|小朋友们[,，、]?大家好|亲爱的小朋友们|嗨[,，、]?(?:呀[,，、]?)?小朋友们|哈喽[,，、]?(?:呀[,，、]?)?小朋友们|嗨[,，、]?大家|哈喽[,，、]?大家)"
)
_HOST_IDENTITY_RE = re.compile(
    r"^我(?:是|叫)(?:主持人|主播|讲故事的|[^，,。.!！？?；;：:]{1,16}(?:姐姐|哥哥|老师|阿姨|叔叔|姑姑))"
)
_ANNOUNCEMENT_RE = re.compile(
    r"^(?:"
    r"今天(?:(?:我(?:们)?|要|来)(?:要|来)?|我们要)?(?:给(?:大家|小朋友们))?(?:讲|分享|带来|听|说(?:一个故事)?)"
    r"|今天(?:的)?故事(?:是|叫|名叫)"
    r"|接下来(?:给(?:大家|小朋友们))?(?:讲|分享|带来|听)"
    r"|下面(?:给(?:大家|小朋友们))?(?:讲|分享|带来|听)"
    r"|我们(?:今天)?(?:一起)?(?:要|来)?(?:讲|听|分享)"
    r"|(?:这个|这则|本期)?故事(?:是|叫|名叫)"
    r")"
)

_SPEAKER_PREFIX_RE = re.compile(
    r"^(?:[一-鿿A-Za-z0-9]{1,12})(?:说|说道|问|问道|答|回答|答道|喊|喊道|叫|叫道|嘟囔|嘀咕)[:：]"
)
_MORAL_START_RE = re.compile(
    r"^(?:小朋友们[,，、:：]?)*(?:"
    r"(?:从)?(?:这个|这则)?故事(?:告诉我们|说明|启示|教会我们|提醒我们)"
    r"|这个故事的道理(?:是|告诉我们)"
    r"|道理(?:是|告诉我们)"
    r"|我们(?:要|应该|需要|一定要|不能|不要|得学会|要学会)"
    r"|做人(?:要|应该|不能|不要)"
    r"|(?:不要|不能|一定要|要学会|要懂得|要|应该|需要|记住|明白)"
    r")"
)
_OUTRO_START_RE = re.compile(
    r"^(?:小朋友们[,，、:：]?|亲爱的小朋友们[,，、:：]?)?(?:"
    r"(?:我的|这个|这则|今天的|本期的)?故事(?:已经)?(?:讲完[了啦]|结束[了啦]|就(?:讲|说)?到这里)"
    r"|今天(?:的)?故事(?:就)?(?:讲|说)?到这里"
    r"|(?:谢谢|感谢)(?:大家|小朋友们|你的收听|收听)"
    r"|(?:小朋友们[,，、:：]?)?(?:再见|下次再见|下期再见)"
    r"|喜欢(?:这个故事|故事)?(?:的话)?(?:请)?关注"
    r")"
)
_BODY_LIKE_TITLE_RE = re.compile(
    r"^(?:森林|从前|一天|有|一只|一个|这时|然后|此时|随后|在)"
    r"|(?:住着|来到|走到|跑到|飞到|站在|躲在|看到|看见|听见|说道?|问道?|回答)"
)


def _coerce_kind(value: SemanticKind | str) -> SemanticKind:
    if isinstance(value, SemanticKind):
        return value
    raw = str(value).strip().lower()
    aliases = {
        "title": SemanticKind.TITLE,
        "story_title": SemanticKind.TITLE,
        "host_intro": SemanticKind.HOST_INTRO,
        "host": SemanticKind.HOST_INTRO,
        "intro": SemanticKind.HOST_INTRO,
        "story_announcement": SemanticKind.STORY_ANNOUNCEMENT,
        "announcement": SemanticKind.STORY_ANNOUNCEMENT,
        "body": SemanticKind.STORY_BODY,
        "story_body": SemanticKind.STORY_BODY,
        "moral": SemanticKind.MORAL,
        "lesson": SemanticKind.MORAL,
        "outro": SemanticKind.OUTRO,
        "closing": SemanticKind.OUTRO,
    }
    try:
        return aliases[raw]
    except KeyError as exc:
        raise ValueError(f"未知语义区间：{value}") from exc


def _coerce_lines(
    lines: Iterable[str | StoryLine | Mapping[str, Any]] | str,
) -> tuple[StoryLine, ...]:
    if isinstance(lines, str):
        lines = lines.splitlines()
    result: list[StoryLine] = []
    for position, item in enumerate(lines, start=1):
        if isinstance(item, StoryLine):
            result.append(item)
            continue
        # Accept timing-like records without importing the align module (and
        # therefore without creating a package import cycle).
        if hasattr(item, "line") and (hasattr(item, "index") or hasattr(item, "line_number")):
            line_number = int(getattr(item, "line_number", getattr(item, "index", position)))
            result.append(StoryLine(line_number=line_number, text=str(getattr(item, "line"))))
            continue
        if isinstance(item, Mapping):
            line_number = int(item.get("line_number", item.get("index", position)))
            text_value = item.get("text", item.get("line"))
            if text_value is None:
                raise ValueError("语义台词行对象必须包含 text/line")
            result.append(StoryLine(line_number=line_number, text=str(text_value)))
            continue
        result.append(StoryLine(line_number=position, text=str(item)))
    return tuple(result)


def _clean_for_match(text: str) -> str:
    # Matching normalization is not written back to StoryLine.text.
    return re.sub(r"\s+", "", text).strip()


def _strip_outer_title_marks(text: str) -> str:
    return text.strip().strip("《》〈〉『』「」【】")


def _looks_like_standalone_title(text: str) -> bool:
    clean = _clean_for_match(text)
    if not clean:
        return False
    # A greeting or announcement can be a short, punctuation-free standalone
    # line (``大家好``/``我是……``).  Those are opening lines, never titles.
    if _is_host_intro_line(clean) or _is_story_announcement_line(clean):
        return False
    if clean.startswith(
        ("标题:", "标题：", "故事:", "故事：", "故事标题:", "故事标题：", "故事名称:", "故事名称：", "故事名:", "故事名：")
    ):
        return True
    unwrapped = _strip_outer_title_marks(clean)
    if not unwrapped or len(unwrapped) > 24:
        return False
    # A quoted title is unambiguous.  For unquoted titles, punctuation and
    # speaker/action endings make a short body sentence more likely.
    quoted_candidate = clean.rstrip("。.!！？!?")
    if quoted_candidate[:1] in "《〈『「【" and quoted_candidate[-1:] in "》〉』」】":
        return True
    if re.search(r"[,，。！？!?；;：:\"“”‘’、]", unwrapped):
        return False
    if _BODY_LIKE_TITLE_RE.search(unwrapped):
        return False
    if re.search(r"(?:说|说道|问|问道|回答|答道|喊道|叫道|嘀咕|告诉|想道)$", unwrapped):
        return False
    return len(unwrapped) <= 24


def _is_host_intro_line(text: str) -> bool:
    clean = _clean_for_match(text)
    if not clean:
        return False
    return bool(_GREETING_RE.match(clean) or _HOST_IDENTITY_RE.match(clean))


def _is_story_announcement_line(text: str) -> bool:
    clean = _clean_for_match(text)
    if not clean:
        return False
    return bool(_ANNOUNCEMENT_RE.match(clean))


def _is_moral_line(text: str) -> bool:
    clean = _clean_for_match(text)
    if not clean or _SPEAKER_PREFIX_RE.match(clean):
        return False
    # A question/quoted dialogue such as “这个故事告诉我们吗？” is not a
    # lesson.  Real morals in this workflow are declarative closing lines.
    if re.search(r"(?:吗|么)[？?]?$", clean):
        return False
    return bool(_MORAL_START_RE.match(clean))


def _is_outro_line(text: str) -> bool:
    clean = _clean_for_match(text)
    if not clean or _SPEAKER_PREFIX_RE.match(clean):
        return False
    return bool(_OUTRO_START_RE.match(clean))


def classify_story(lines: Iterable[str | StoryLine | Mapping[str, Any]]) -> StorySemantics:
    """Classify a complete transcript without changing its source text.

    The result is deterministic for a given ordered input.  Blank lines are
    retained in ``StorySemantics.lines`` but do not become subtitle/PPT items
    or semantic segments.
    """

    source_lines = _coerce_lines(lines)
    if not source_lines:
        return StorySemantics(lines=(), segments=(), _kinds=())

    kinds: list[SemanticKind | None] = [None] * len(source_lines)
    nonempty = [index for index, line in enumerate(source_lines) if _clean_for_match(line.text)]
    if not nonempty:
        return StorySemantics(lines=source_lines, segments=(), _kinds=tuple(kinds))

    first = nonempty[0]
    title_present = _looks_like_standalone_title(source_lines[first].text)
    if title_present:
        kinds[first] = SemanticKind.TITLE

    opening_start = nonempty[1:] if title_present else nonempty
    opening_active = True
    body_start_position = len(opening_start)
    for position, index in enumerate(opening_start):
        if not opening_active:
            break
        text = source_lines[index].text
        if _is_story_announcement_line(text):
            kinds[index] = SemanticKind.STORY_ANNOUNCEMENT
            continue
        if _is_host_intro_line(text):
            kinds[index] = SemanticKind.HOST_INTRO
            continue
        opening_active = False
        body_start_position = position
        break
    else:
        body_start_position = len(opening_start)

    # The first non-opening line starts the narrative body.  A title-only
    # transcript therefore has an empty body, while a title followed directly
    # by story action is handled naturally.
    body_indices = opening_start[body_start_position:]
    moral_seen = False
    outro_seen = False
    for index in body_indices:
        text = source_lines[index].text
        if outro_seen:
            kinds[index] = SemanticKind.OUTRO
            continue
        if _is_outro_line(text):
            kinds[index] = SemanticKind.OUTRO
            outro_seen = True
            continue
        if moral_seen:
            kinds[index] = SemanticKind.MORAL
            continue
        if _is_moral_line(text):
            kinds[index] = SemanticKind.MORAL
            moral_seen = True
            continue
        kinds[index] = SemanticKind.STORY_BODY

    # If a transcript consists only of a title/opening, retain any unassigned
    # non-empty line as body (this also protects unusual custom greetings).
    for index in nonempty:
        if kinds[index] is None:
            kinds[index] = SemanticKind.STORY_BODY

    segments = _build_segments(source_lines, kinds)
    return StorySemantics(lines=source_lines, segments=tuple(segments), _kinds=tuple(kinds))


def _build_segments(
    lines: tuple[StoryLine, ...], kinds: Sequence[SemanticKind | None]
) -> list[SemanticSegment]:
    segments: list[SemanticSegment] = []
    current_kind: SemanticKind | None = None
    current_lines: list[StoryLine] = []
    for line, kind in zip(lines, kinds):
        if kind is None:
            continue
        if current_kind is not None and kind is not current_kind:
            segments.append(
                SemanticSegment(
                    kind=current_kind,
                    start_line=current_lines[0].line_number,
                    end_line=current_lines[-1].line_number,
                    lines=tuple(current_lines),
                )
            )
            current_lines = []
        if current_kind is None or kind is not current_kind:
            current_kind = kind
        current_lines.append(line)
    if current_kind is not None and current_lines:
        segments.append(
            SemanticSegment(
                kind=current_kind,
                start_line=current_lines[0].line_number,
                end_line=current_lines[-1].line_number,
                lines=tuple(current_lines),
            )
        )
    return segments


def normalize_output_kind(output_kind: StoryOutput | str) -> StoryOutput:
    if isinstance(output_kind, StoryOutput):
        return output_kind
    raw = str(output_kind).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "demo": StoryOutput.DEMO,
        "demonstration": StoryOutput.DEMO,
        "示范": StoryOutput.DEMO,
        "示范视频": StoryOutput.DEMO,
        "release": StoryOutput.RELEASE,
        "publish": StoryOutput.RELEASE,
        "发布": StoryOutput.RELEASE,
        "发布视频": StoryOutput.RELEASE,
        "full": StoryOutput.NARRATION_FULL,
        "complete": StoryOutput.NARRATION_FULL,
        "narration": StoryOutput.NARRATION_FULL,
        "narration_full": StoryOutput.NARRATION_FULL,
        "完整旁白": StoryOutput.NARRATION_FULL,
        "sales": StoryOutput.SALES_SUBTITLES,
        "sales_subtitle": StoryOutput.SALES_SUBTITLES,
        "sales_subtitles": StoryOutput.SALES_SUBTITLES,
        "sales_subtitle_srt": StoryOutput.SALES_SUBTITLES,
        "销售": StoryOutput.SALES_SUBTITLES,
        "销售字幕": StoryOutput.SALES_SUBTITLES,
        "background_subtitles": StoryOutput.BACKGROUND_SUBTITLES,
        "background_subtitle": StoryOutput.BACKGROUND_SUBTITLES,
        "背景字幕": StoryOutput.BACKGROUND_SUBTITLES,
        "ppt": StoryOutput.PPT,
        "故事文稿": StoryOutput.STORY_TEXT,
        "story_text": StoryOutput.STORY_TEXT,
        "text": StoryOutput.STORY_TEXT,
        "reading_annotation": StoryOutput.READING_ANNOTATION,
        "annotation": StoryOutput.READING_ANNOTATION,
        "朗读标注": StoryOutput.READING_ANNOTATION,
        "朗读标注文稿": StoryOutput.READING_ANNOTATION,
    }
    try:
        return aliases[raw]
    except KeyError as exc:
        raise ValueError(f"未知产物类型：{output_kind}") from exc


OUTPUT_POLICY: dict[StoryOutput, frozenset[SemanticKind]] = {
    # These outputs are complete spoken material.  A title can be exposed as
    # metadata separately, but if it is an uttered line it remains included.
    StoryOutput.DEMO: frozenset(SemanticKind),
    StoryOutput.RELEASE: frozenset(SemanticKind),
    StoryOutput.NARRATION_FULL: frozenset(SemanticKind),
    # Customer-facing background subtitles intentionally begin at narrative
    # action and stop before both the moral and the outro.
    StoryOutput.SALES_SUBTITLES: frozenset({SemanticKind.STORY_BODY}),
    StoryOutput.BACKGROUND_SUBTITLES: frozenset({SemanticKind.STORY_BODY}),
    # Existing PPT/story-text/annotation rules retain the moral as their final
    # teaching page/paragraph while dropping presenter framing.
    StoryOutput.PPT: frozenset({SemanticKind.STORY_BODY, SemanticKind.MORAL}),
    StoryOutput.STORY_TEXT: frozenset({SemanticKind.STORY_BODY, SemanticKind.MORAL}),
    StoryOutput.READING_ANNOTATION: frozenset({SemanticKind.STORY_BODY, SemanticKind.MORAL}),
}
OUTPUT_POLICIES = OUTPUT_POLICY


def lines_for_output(
    semantics: StorySemantics | Iterable[str | StoryLine | Mapping[str, Any]],
    output_kind: StoryOutput | str,
) -> list[StoryLine]:
    """Return source lines included by an output policy, in source order."""

    if not isinstance(semantics, StorySemantics):
        semantics = classify_story(semantics)
    policy = OUTPUT_POLICY[normalize_output_kind(output_kind)]
    return [
        line
        for line, kind in zip(semantics.lines, semantics._kinds)
        if kind in policy
    ]


def select_lines(
    semantics: StorySemantics | Iterable[str | StoryLine | Mapping[str, Any]],
    output_kind: StoryOutput | str,
) -> list[StoryLine]:
    """Explicit alias for :func:`lines_for_output`."""

    return lines_for_output(semantics, output_kind)


def select_line_numbers(
    semantics: StorySemantics | Iterable[str | StoryLine | Mapping[str, Any]],
    output_kind: StoryOutput | str,
) -> list[int]:
    return [line.line_number for line in lines_for_output(semantics, output_kind)]


def texts_for_output(
    semantics: StorySemantics | Iterable[str | StoryLine | Mapping[str, Any]],
    output_kind: StoryOutput | str,
) -> list[str]:
    return [line.text for line in lines_for_output(semantics, output_kind)]


def segments_for_output(
    semantics: StorySemantics | Iterable[str | StoryLine | Mapping[str, Any]],
    output_kind: StoryOutput | str,
) -> list[SemanticSegment]:
    if not isinstance(semantics, StorySemantics):
        semantics = classify_story(semantics)
    policy = OUTPUT_POLICY[normalize_output_kind(output_kind)]
    return [segment for segment in semantics.segments if segment.kind in policy]


# Readable aliases for callers that use "parse" or "analyze" terminology.
parse_story_semantics = classify_story
analyze_story_semantics = classify_story
classify_lines = classify_story
classify_transcript = classify_story
segment_story = classify_story
get_lines_for_output = lines_for_output
select_lines_for_output = lines_for_output
select_for_output = lines_for_output
select_for_artifact = lines_for_output


__all__ = [
    "SemanticKind",
    "SegmentKind",
    "StoryOutput",
    "OutputKind",
    "ArtifactKind",
    "SemanticSection",
    "StoryLine",
    "SemanticSegment",
    "StorySemantics",
    "OUTPUT_POLICY",
    "OUTPUT_POLICIES",
    "classify_story",
    "classify_lines",
    "parse_story_semantics",
    "analyze_story_semantics",
    "classify_transcript",
    "segment_story",
    "normalize_output_kind",
    "lines_for_output",
    "get_lines_for_output",
    "select_lines_for_output",
    "select_lines",
    "select_for_output",
    "select_for_artifact",
    "select_line_numbers",
    "texts_for_output",
    "segments_for_output",
]
