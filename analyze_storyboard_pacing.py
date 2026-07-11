from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

from story_video_synthesizer.align import align_evenly, align_script_to_narration, sanitize_script_line
from story_video_synthesizer.media import probe_duration


TARGET_MIN_SECONDS = 8.0
TARGET_MAX_SECONDS = 12.0
SHORT_SECONDS = 6.0
LONG_SECONDS = 15.0
DEFAULT_CHARS_PER_SECOND = 4.0


SCENE_SHIFT_MARKERS = (
    "第二天",
    "第三天",
    "第四天",
    "第五天",
    "第六天",
    "第七天",
    "过了",
    "后来",
    "这时",
    "突然",
    "回到",
    "来到",
    "走进",
    "钻进",
    "离开",
    "再见",
    "小朋友们",
    "这个故事告诉我们",
)
CONNECTIVE_MARKERS = (
    "说",
    "问",
    "回答",
    "看",
    "想着",
    "于是",
    "又",
    "接着",
    "然后",
    "这时",
    "站在",
    "端着",
    "拿着",
    "推着",
)


@dataclass(frozen=True)
class PacingLine:
    index: int
    text: str
    duration: float
    source: str
    rating: str
    suggestion: str
    reason: str


@dataclass(frozen=True)
class PacingAnalysis:
    lines: list[PacingLine]
    draft_lines: list[str]
    report_path: Path
    draft_path: Path
    summary: str
    timing_source: str


def analyze_storyboard_pacing(
    *,
    story_text: str,
    output_dir: Path,
    slug: str = "story",
    narration_path: Path | None = None,
    whisper_model: str = "base",
    language: str = "zh",
    whisper_model_dir: Path | None = None,
) -> PacingAnalysis:
    lines = [sanitize_script_line(line.strip()) for line in story_text.splitlines() if line.strip()]
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"{slug}_storyboard_pacing_report.md"
    draft_path = output_dir / f"{slug}_storyboard_lines_draft.txt"
    if not lines:
        report_path.write_text("# 分镜节奏分析\n\n没有可分析的故事文本。\n", encoding="utf-8")
        draft_path.write_text("", encoding="utf-8")
        return PacingAnalysis([], [], report_path, draft_path, "没有可分析的故事文本。", "empty")

    durations, timing_source = estimate_line_durations(
        lines,
        narration_path=narration_path,
        whisper_model=whisper_model,
        language=language,
        whisper_model_dir=whisper_model_dir,
    )
    pacing_lines = [
        PacingLine(
            index=index,
            text=text,
            duration=duration,
            source=timing_source,
            rating=rate_duration(duration),
            suggestion=suggest_line(index, text, duration, lines),
            reason=reason_for_line(index, text, duration, lines),
        )
        for index, (text, duration) in enumerate(zip(lines, durations), start=1)
    ]
    draft_lines = build_draft_lines(lines, durations)
    summary = build_summary(pacing_lines, draft_lines)
    report_path.write_text(render_report(pacing_lines, draft_lines, summary, timing_source), encoding="utf-8")
    draft_path.write_text("\n".join(draft_lines) + "\n", encoding="utf-8")
    return PacingAnalysis(pacing_lines, draft_lines, report_path, draft_path, summary, timing_source)


def estimate_line_durations(
    lines: list[str],
    *,
    narration_path: Path | None,
    whisper_model: str,
    language: str,
    whisper_model_dir: Path | None,
) -> tuple[list[float], str]:
    if narration_path and narration_path.exists():
        try:
            timings = align_script_to_narration(lines, narration_path, whisper_model, language, whisper_model_dir)
            return [timing.duration for timing in timings], "whisper"
        except Exception:
            try:
                timings = align_evenly(lines, narration_path)
                return [timing.duration for timing in timings], "audio-even-fallback"
            except Exception:
                pass
    durations = [max(0.8, len(clean_text(line)) / DEFAULT_CHARS_PER_SECOND) for line in lines]
    return durations, "text-estimate"


def rate_duration(duration: float) -> str:
    if duration < SHORT_SECONDS:
        return "偏短"
    if TARGET_MIN_SECONDS <= duration <= TARGET_MAX_SECONDS:
        return "理想"
    if TARGET_MAX_SECONDS < duration <= LONG_SECONDS:
        return "可接受"
    if duration > LONG_SECONDS:
        return "偏长"
    return "略短"


def suggest_line(index: int, text: str, duration: float, lines: list[str]) -> str:
    if duration < SHORT_SECONDS:
        if can_merge_with_next(index, text, lines):
            return "建议检查是否可与下一行合并"
        if index > 1 and can_merge_pair(lines[index - 2], text):
            return "建议检查是否可与上一行合并"
        return "短但建议保留"
    if TARGET_MIN_SECONDS <= duration <= TARGET_MAX_SECONDS:
        return "保持"
    if TARGET_MAX_SECONDS < duration <= LONG_SECONDS:
        return "可保持，提示词写清镜头内节奏"
    if duration > LONG_SECONDS:
        return "建议拆分或明确两段动作"
    return "可保持或与相邻短句合并"


def reason_for_line(index: int, text: str, duration: float, lines: list[str]) -> str:
    if duration < SHORT_SECONDS:
        if can_merge_with_next(index, text, lines):
            return "当前行低于 6 秒，且下一行语义/场景较连续。"
        if index > 1 and can_merge_pair(lines[index - 2], text):
            return "当前行低于 6 秒，且上一行语义/场景较连续。"
        return "当前行低于 6 秒，但相邻行可能存在场景、时间或叙事目的变化。"
    if TARGET_MIN_SECONDS <= duration <= TARGET_MAX_SECONDS:
        return "时长落在 Grok 10 秒模型的理想区间。"
    if TARGET_MAX_SECONDS < duration <= LONG_SECONDS:
        return "略长但可用，适合在同一视频里写中景到近景等镜头内调度。"
    if duration > LONG_SECONDS:
        return "超过 15 秒，单镜头可能承载过多叙事。"
    return "接近目标下限，可按画面连续性决定是否合并。"


def build_draft_lines(lines: list[str], durations: list[float]) -> list[str]:
    draft: list[str] = []
    index = 0
    while index < len(lines):
        group = [lines[index]]
        total = durations[index]
        while (
            index + 1 < len(lines)
            and total < TARGET_MIN_SECONDS
            and total + durations[index + 1] <= LONG_SECONDS
            and can_merge_pair(group[-1], lines[index + 1])
        ):
            index += 1
            group.append(lines[index])
            total += durations[index]
        draft.append(" ".join(group))
        index += 1
    return draft


def can_merge_with_next(index: int, text: str, lines: list[str]) -> bool:
    if index >= len(lines):
        return False
    return can_merge_pair(text, lines[index])


def can_merge_pair(left: str, right: str) -> bool:
    if has_hard_boundary(right):
        return False
    left_roles = role_markers(left)
    right_roles = role_markers(right)
    if left_roles and right_roles and left_roles.isdisjoint(right_roles):
        return any(marker in left + right for marker in CONNECTIVE_MARKERS)
    combined = left + right
    return any(marker in combined for marker in CONNECTIVE_MARKERS) or bool(left_roles & right_roles)


def has_hard_boundary(text: str) -> bool:
    return any(marker in text for marker in SCENE_SHIFT_MARKERS)


def role_markers(text: str) -> set[str]:
    known = ("黑熊", "狐狸", "乌龟", "小乌龟", "青蛇", "小青蛇", "鳄鱼", "小鳄鱼", "鸵鸟", "小鸵鸟")
    roles = {role for role in known if role in text}
    generic = re.findall(r"[\u4e00-\u9fa5]{1,4}(?:姐姐|哥哥|大嫂|姑娘|先生|夫人|孩子|妈妈|爸爸|爷爷|奶奶)", text)
    roles.update(generic)
    return roles


def clean_text(text: str) -> str:
    return re.sub(r"[\s\.,!?;:'\"，。！？；：“”‘’、（）()《》<>【】\[\]…—\-]+", "", text)


def build_summary(lines: list[PacingLine], draft_lines: list[str]) -> str:
    counts: dict[str, int] = {}
    for line in lines:
        counts[line.rating] = counts.get(line.rating, 0) + 1
    parts = [
        f"原始候选镜头 {len(lines)} 条",
        f"建议草稿 {len(draft_lines)} 条",
        f"偏短 {counts.get('偏短', 0)} 条",
        f"理想 {counts.get('理想', 0)} 条",
        f"可接受 {counts.get('可接受', 0)} 条",
        f"偏长 {counts.get('偏长', 0)} 条",
    ]
    return "；".join(parts) + "。"


def render_report(lines: list[PacingLine], draft_lines: list[str], summary: str, timing_source: str) -> str:
    source_label = {
        "whisper": "Whisper 旁白对齐",
        "audio-even-fallback": "旁白总时长均分估算",
        "text-estimate": "文本字数估算",
        "empty": "无输入",
    }.get(timing_source, timing_source)
    output = [
        "# 分镜节奏分析报告",
        "",
        f"- 模型策略：Grok 10 秒图生视频",
        f"- 目标区间：{TARGET_MIN_SECONDS:.0f}-{TARGET_MAX_SECONDS:.0f} 秒",
        f"- 时长来源：{source_label}",
        f"- 摘要：{summary}",
        "",
        "## 逐行体检",
        "",
        "| 行 | 时长 | 判断 | 建议 | 文本 | 理由 |",
        "|---:|---:|---|---|---|---|",
    ]
    for line in lines:
        output.append(
            f"| {line.index} | {line.duration:.1f}s | {line.rating} | {line.suggestion} | "
            f"{escape_md(line.text)} | {escape_md(line.reason)} |"
        )
    output.extend(
        [
            "",
            "## 建议换行草稿",
            "",
            "这只是草稿，不会自动覆盖工作台原文。人工确认后再使用。",
            "",
            "```text",
            *draft_lines,
            "```",
            "",
            "## Grok 10 秒提示词提醒",
            "",
            "- 连续同场景、同动作、同对话目的的短句优先合并。",
            "- 短句如果跨场景、跨角色目标、情绪转折明显，则保留独立镜头。",
            "- 图生视频提示词可以写镜头内调度，例如先中景建立关系，再轻推近到表情特写。",
        ]
    )
    return "\n".join(output) + "\n"


def escape_md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def main() -> None:
    parser = argparse.ArgumentParser(description="分析手动换行分镜是否适合 Grok 10 秒图生视频")
    parser.add_argument("--story-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--slug", default="story")
    parser.add_argument("--narration", default=None, type=Path)
    parser.add_argument("--whisper-model", default="base")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--whisper-model-dir", default=None, type=Path)
    parser.add_argument("--json", action="store_true", help="打印 JSON 摘要")
    args = parser.parse_args()

    analysis = analyze_storyboard_pacing(
        story_text=args.story_file.read_text(encoding="utf-8-sig"),
        output_dir=args.output_dir.expanduser(),
        slug=args.slug,
        narration_path=args.narration.expanduser() if args.narration else None,
        whisper_model=args.whisper_model,
        language=args.language,
        whisper_model_dir=args.whisper_model_dir.expanduser() if args.whisper_model_dir else None,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "report_path": str(analysis.report_path),
                    "draft_path": str(analysis.draft_path),
                    "summary": analysis.summary,
                    "timing_source": analysis.timing_source,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(f"已生成分镜节奏分析：{analysis.report_path}")
        print(f"已生成建议换行草稿：{analysis.draft_path}")
        print(analysis.summary)


if __name__ == "__main__":
    main()
