from __future__ import annotations

import argparse
import csv
import json
import re
import time
from pathlib import Path

from story_video_synthesizer.media import probe_duration
from story_video_synthesizer.volcengine_video import read_jobs_csv


def main() -> None:
    parser = argparse.ArgumentParser(description="生成交给 Codex/Suno skill 的故事配乐任务包")
    parser.add_argument("--story-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--story-title", default="")
    parser.add_argument("--narration", default=None, type=Path)
    parser.add_argument("--jobs-csv", default=None, type=Path)
    parser.add_argument("--skill-path", default=Path.home() / "Downloads" / "suno-story-score.skill", type=Path)
    parser.add_argument("--story-contract-context", default=None, type=Path)
    args = parser.parse_args()

    story_file = args.story_file.expanduser()
    output_dir = args.output_dir.expanduser()
    music_dir = output_dir / "music"
    clips_dir = music_dir / "suno_downloads"
    clips_dir.mkdir(parents=True, exist_ok=True)

    story = _extract_story_body(story_file.read_text(encoding="utf-8-sig").strip())
    if not story:
        raise ValueError(f"故事文本为空：{story_file}")

    narration_duration = ""
    narration_duration_value = 0.0
    narration_path = args.narration.expanduser() if args.narration else None
    if narration_path and narration_path.exists():
        narration_duration_value = probe_duration(narration_path)
        narration_duration = f"{narration_duration_value:.2f}"

    timeline_rows = _timeline_rows(args.jobs_csv.expanduser()) if args.jobs_csv and args.jobs_csv.expanduser().exists() else []
    timeline = _format_timeline(timeline_rows)
    contract_context = _load_music_contract_context(args.story_contract_context)

    request_path = music_dir / f"{args.slug}_suno_music_request.md"
    prompts_path = music_dir / f"{args.slug}_suno_prompts.md"
    plan_path = music_dir / f"{args.slug}_music_plan.csv"
    final_music_path = music_dir / f"{args.slug}_background_music.mp3"
    request_manifest_path = music_dir / f"{args.slug}_music_request_manifest.json"
    for stale_path in (prompts_path, plan_path):
        archived = _archive_stale_generated_file(stale_path)
        if archived:
            print(f"已归档旧本地草稿：{archived}")

    request_path.write_text(
        _request_text(
            title=args.story_title.strip() or args.slug,
            slug=args.slug,
            skill_path=args.skill_path.expanduser(),
            story_file=story_file,
            narration_path=narration_path,
            narration_duration=narration_duration,
            timeline=timeline,
            prompts_path=prompts_path,
            plan_path=plan_path,
            clips_dir=clips_dir,
            final_music_path=final_music_path,
            story=story,
        ),
        encoding="utf-8",
    )
    if contract_context:
        request_manifest_path.write_text(json.dumps(contract_context, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with request_path.open("a", encoding="utf-8") as file:
            file.write(
                "\n## 已审核故事合同（强制）\n\n"
                f"- 请求清单：`{args.story_contract_context.expanduser()}`\n"
                f"- story_contract_sha256：`{contract_context['story_contract_sha256']}`\n"
                f"- contract_schema_version：`{contract_context['contract_schema_version']}`\n"
                f"- story_contract_dependency_sha256：`{contract_context['story_contract_dependency_sha256']}`\n"
                "- 必须使用 contract_projection.semantic_artifacts 的故事边界/产物语义和 story_state 的阶段/情绪变化设计音乐段；不得让音乐覆盖对白或跨越不兼容的故事边界。\n"
                "- 生成的 prompts 与 music_plan 每行/每段必须记录上述三个合同绑定字段。\n"
                "\n```json\n" + json.dumps(contract_context.get("contract_projection", {}), ensure_ascii=False, indent=2) + "\n```\n"
            )

    print(f"已生成 Codex 智能配乐任务：{request_path}")
    print(f"待 Codex 生成 Suno 提示词：{prompts_path}")
    print(f"待 Codex 生成音乐分段表：{plan_path}")
    print(f"Suno 下载目录：{clips_dir}")
    print(f"最终背景音乐：{final_music_path}")


def _load_music_contract_context(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if payload.get("consumer") != "music":
        raise ValueError("story contract context consumer must be music")
    for key in ("contract_schema_version", "story_contract_sha256", "story_contract_dependency_sha256"):
        if not str(payload.get(key) or "").strip():
            raise ValueError(f"music contract context missing {key}")
    return payload


def _archive_stale_generated_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    archived = path.with_name(f"{path.stem}.old_local_draft_{stamp}{path.suffix}")
    path.rename(archived)
    return archived


def _extract_story_body(text: str) -> str:
    marker = "## 故事原文"
    marker_at = text.rfind(marker)
    if marker_at >= 0:
        tail = text[marker_at + len(marker) :]
        blocks = re.findall(r"```(?:text)?\s*(.*?)```", tail, flags=re.S)
        if blocks:
            return blocks[0].strip()
    return text.strip()


def _timeline_rows(jobs_csv: Path) -> list[dict[str, str]]:
    return read_jobs_csv(jobs_csv)


def _format_timeline(rows: list[dict[str, str]]) -> str:
    lines = []
    for row in rows:
        start = row.get("narration_start", "")
        end = row.get("narration_end", "")
        text = row.get("story_text", "").strip()
        if start and end:
            lines.append(f"{row.get('scene', '')}. {start}s-{end}s：{text}")
    return "\n".join(lines)


def _build_music_segments(
    *,
    title: str,
    slug: str,
    rows: list[dict[str, str]],
    narration_duration: float,
) -> list[dict[str, str]]:
    if rows:
        total = max(_float(row.get("narration_end", "0")) for row in rows)
    else:
        total = narration_duration or 150.0
    if narration_duration:
        total = max(total, narration_duration)

    if len(rows) >= 9:
        ranges = _scene_ranges(rows)
    else:
        ranges = [
            {
                "index": 1,
                "first_scene": 1,
                "last_scene": 1,
                "start": 0.0,
                "end": total * 0.38,
                "text": "",
            },
            {
                "index": 2,
                "first_scene": 2,
                "last_scene": 2,
                "start": total * 0.38,
                "end": total * 0.68,
                "text": "",
            },
            {
                "index": 3,
                "first_scene": 3,
                "last_scene": 3,
                "start": total * 0.68,
                "end": total,
                "text": "",
            },
        ]

    segments: list[dict[str, str]] = []
    for item in ranges:
        index = int(item["index"])
        start = float(item["start"])
        end = float(item["end"])
        text = str(item.get("text", ""))
        duration = max(1.0, end - start)
        label = _label_for_segment(index, text)
        story_range = _story_range_for_segment(item, text)
        segments.append(
            {
                "segment": str(index),
                "label": label,
                "start_sec": f"{start:.2f}",
                "end_sec": f"{end:.2f}",
                "duration_sec": f"{duration:.2f}",
                "story_range": story_range,
                "title": f"{title} 配乐 {index:02d}",
                "style_prompt": _style_prompt_for_segment(title=title, index=index, label=label, text=text),
                "target_audio_filename": f"{index:02d}_{slug}_music.mp3",
                "notes": "Suno Advanced/Custom；Lyrics 填 [Instrumental]；下载后按 target_audio_filename 改名。",
            }
        )
    return segments


def _scene_ranges(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    scene_count = len(rows)
    split_a = min(12, max(1, round(scene_count * 0.4)))
    split_b = min(20, max(split_a + 1, round(scene_count * 0.68)))
    ranges = [(1, split_a), (split_a + 1, split_b), (split_b + 1, scene_count)]
    result = []
    for index, (first_scene, last_scene) in enumerate(ranges, start=1):
        selected = rows[first_scene - 1 : last_scene]
        if not selected:
            continue
        start = _float(selected[0].get("narration_start", "0"))
        end = _float(selected[-1].get("narration_end", "0"))
        result.append(
            {
                "index": index,
                "first_scene": first_scene,
                "last_scene": last_scene,
                "start": start,
                "end": end,
                "text": _segment_text(selected),
            }
        )
    return result


def _segment_text(rows: list[dict[str, str]]) -> str:
    return " ".join(row.get("story_text", "").strip() for row in rows if row.get("story_text", "").strip())


def _label_for_segment(index: int, text: str) -> str:
    text = _clean_label_text(text)
    if not text:
        return ["开场与故事引入", "情节发展与轻微转折", "结果呈现与温暖收束"][min(index - 1, 2)]

    has_forgetting = any(keyword in text for keyword in ["忘", "不会走", "狼狈", "爬着", "本领"])
    has_moral = any(keyword in text for keyword in ["道理", "告诉我们", "不能盲目", "小朋友"])
    has_imitation = any(keyword in text for keyword in ["观察", "老人", "年轻人", "小孩", "模仿"])
    if has_forgetting and has_moral:
        return "忘记本领与寓意收束"
    if has_imitation and has_forgetting:
        return "认真模仿与逐渐迷失"

    candidates = [
        (("道理", "告诉我们", "不能盲目", "小朋友"), "故事道理与温暖收束"),
        (("忘", "不会走", "狼狈", "爬着", "本领"), "忘记本领与狼狈回家"),
        (("观察", "老人", "年轻人", "小孩", "模仿"), "观察模仿与认真学习"),
        (("听说", "羡慕", "决心", "专门跑去"), "向往邯郸与出发学习"),
        (("帮助", "善良", "村", "朋友"), "善良帮助与关系建立"),
        (("危险", "危机", "害怕", "紧张", "坏人"), "危机出现与紧张推进"),
        (("想办法", "机智", "聪明", "计划", "办法"), "机智应对与办法展开"),
        (("误会", "闹笑话", "失败", "出错"), "误会升级与趣味转折"),
        (("成功", "胜利", "解决", "团圆", "回家"), "问题解决与温暖结尾"),
    ]
    for keywords, label in candidates:
        if sum(1 for keyword in keywords if keyword in text) >= 2:
            return label

    if index == 1:
        return _compact_label(text, fallback="开场与故事引入")
    if index == 2:
        return _compact_label(text, fallback="情节发展与轻微转折")
    return _compact_label(text, fallback="结果呈现与温暖收束")


def _compact_label(text: str, *, fallback: str) -> str:
    clauses = [clause.strip(" ，。；！？、") for clause in re.split(r"[。；！？\n]", text) if clause.strip()]
    if not clauses:
        return fallback
    clause = clauses[0]
    clause = re.sub(r"^(大家好|今天这个故事|小朋友们|最后)[，,！!。 ]*", "", clause)
    clause = clause[:10]
    return f"{clause}…" if clause else fallback


def _story_range_for_segment(item: dict[str, object], text: str) -> str:
    first_scene = int(item.get("first_scene", item["index"]))
    last_scene = int(item.get("last_scene", item["index"]))
    summary = _content_summary(text)
    if summary:
        return f"镜头 {first_scene}-{last_scene}：{summary}"
    return f"镜头 {first_scene}-{last_scene}"


def _content_summary(text: str) -> str:
    text = _clean_label_text(text)
    if not text:
        return ""
    clauses = [clause.strip(" ，。；！？、") for clause in re.split(r"[。；！？\n]", text) if clause.strip()]
    clauses = [
        re.sub(r"^(大家好|我是[^。；！？，,]*|今天这个故事|小朋友们|最后)[，,！!。 ]*", "", clause).strip(" ，。；！？、")
        for clause in clauses
    ]
    clauses = [clause for clause in clauses if clause]
    if not clauses:
        return text[:28]
    if len(clauses) == 1:
        return clauses[0][:34]
    first = clauses[0][:16]
    last = clauses[-1][:16]
    return f"{first}，到{last}"


def _style_prompt_for_segment(*, title: str, index: int, label: str, text: str) -> str:
    combined = f"{title} {label} {text}"
    world = _story_world(combined)
    bpm = _bpm_for_segment(index, combined)
    mood = _mood_for_segment(index, combined)
    anchor, accent, extra = _instrument_plan(world, combined)
    parts = [
        mood,
        anchor,
        accent,
        extra,
        f"around {bpm} BPM",
        "low volume background",
        "spacious mix for narration",
        "never overpowering",
        "no heavy percussion",
        "instrumental only",
        "no vocals",
        "no lyrics",
        "no choir",
        "no spoken words",
    ]
    return ", ".join(part for part in parts if part)


def _story_world(text: str) -> str:
    chinese_markers = [
        "邯郸", "成语", "寓言", "民间", "神话", "历史", "中国古代", "古时候", "很久以前",
        "战国", "燕国", "赵国", "楚国", "齐国", "秦国", "皇帝", "将军", "书生", "农夫",
        "天帝", "龙王", "嫦娥", "后羿", "女娲", "盘古", "学步",
    ]
    if any(marker in text for marker in chinese_markers):
        return "chinese"
    return "western"


def _instrument_plan(world: str, text: str) -> tuple[str, str, str]:
    if world == "chinese":
        anchor = "Chinese children's story underscore led by warm guzheng and soft bowed strings"
        if "向往邯郸与出发学习" in text:
            accent = "subtle pentatonic dizi and pipa touches"
            extra = "warm traditional Chinese color, light and child-friendly"
        elif "认真模仿与逐渐迷失" in text:
            accent = "light pipa plucks with sparse woodblock taps"
            extra = "playful pentatonic motion with subtle unease"
        elif "忘记本领与寓意收束" in text:
            accent = "light guzheng ornament with subtle rhythmic pulse"
            extra = "reflective color with a clearer ending shape"
        elif any(keyword in text for keyword in ["危机", "危险", "紧张", "坏人"]):
            accent = "single pipa accent"
            extra = "pentatonic color, restrained tension"
        elif any(keyword in text for keyword in ["忘", "狼狈", "失败", "难过", "后悔"]):
            accent = "light guzheng ornament"
            extra = "gentle reflective color"
        elif any(keyword in text for keyword in ["模仿", "小孩", "玩", "观察", "年轻人", "老人"]):
            accent = "light pizzicato strings with sparse woodblock-like taps"
            extra = "playful motion, no flute lead"
        else:
            accent = "subtle dizi and pipa touches"
            extra = "warm traditional Chinese color, no modern pop drums"
        return anchor, accent, extra

    anchor = "piano-led children's underscore with pizzicato strings"
    if any(keyword in text for keyword in ["危机", "危险", "紧张", "坏人"]):
        accent = "one clarinet or oboe accent"
        extra = "controlled tension"
    elif any(keyword in text for keyword in ["忘", "狼狈", "失败", "难过", "后悔"]):
        accent = "soft glockenspiel accent with slight pulse"
        extra = "gentle reflective color with a little motion"
    elif any(keyword in text for keyword in ["模仿", "小孩", "玩", "观察", "年轻人", "老人"]):
        accent = "light woodwind sparkle"
        extra = "playful motion"
    else:
        accent = "light glockenspiel accent"
        extra = "storybook warmth"
    return anchor, accent, extra


def _bpm_for_segment(index: int, text: str) -> int:
    if "向往邯郸与出发学习" in text:
        return 68
    if "认真模仿与逐渐迷失" in text:
        return 74
    if "忘记本领与寓意收束" in text:
        return 68
    if any(keyword in text for keyword in ["危机", "危险", "紧张", "坏人", "追", "冲", "奔"]):
        return 84
    if any(keyword in text for keyword in ["忘", "狼狈", "失败", "难过", "后悔"]):
        return 66
    if any(keyword in text for keyword in ["道理", "告诉我们", "收束", "谢谢大家", "结尾"]):
        return 64
    if any(keyword in text for keyword in ["模仿", "小孩", "玩", "观察", "年轻人", "老人"]):
        return 74
    return 68 if index == 1 else 70 if index == 2 else 66


def _mood_for_segment(index: int, text: str) -> str:
    if "认真模仿与逐渐迷失" in text:
        return "playful imitation with subtle unease"
    if "忘记本领与寓意收束" in text:
        return "gentle reflective ending with a little forward motion, warm moral resolution"
    if any(keyword in text for keyword in ["道理", "告诉我们", "收束", "谢谢大家", "结尾"]):
        return "calm warm resolution, reflective ending"
    if any(keyword in text for keyword in ["忘", "狼狈", "失败", "难过", "后悔"]):
        return "gentle reflective mood, soft low strings"
    if any(keyword in text for keyword in ["危机", "危险", "坏人", "害怕", "紧张"]):
        return "light suspense, controlled intensity"
    if any(keyword in text for keyword in ["模仿", "闹笑话", "误会", "小孩", "玩"]):
        return "playful imitation with subtle unease"
    if index == 1:
        return "curious and hopeful mood"
    if index == 2:
        return "playful story development"
    return "calm warm resolution"


def _clean_label_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _write_plan(path: Path, segments: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        fieldnames = [
            "segment",
            "label",
            "start_sec",
            "end_sec",
            "duration_sec",
            "story_range",
            "title",
            "style_prompt",
            "target_audio_filename",
            "notes",
        ]
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(segments)


def _prompts_text(*, title: str, segments: list[dict[str, str]], clips_dir: Path, plan_path: Path) -> str:
    lines = [
        f"# Suno 可复制提示词：{title}",
        "",
        "使用 Suno Advanced / Custom。每一段分别生成一次。",
        "",
        "Lyrics 框统一填写：",
        "",
        "```text",
        "[Instrumental]",
        "```",
        "",
        f"下载目录：`{clips_dir}`",
        f"分段表：`{plan_path}`",
        "",
        "下载后请把每段音频改名成对应的 `target_audio_filename`，再回工作台点击 `⑩ 拼接音乐`。",
        "",
    ]
    for segment in segments:
        lines.extend(
            [
                f"## {segment['segment']}. {segment['label']}",
                "",
                f"- 时间：{segment['start_sec']}s - {segment['end_sec']}s，约 {segment['duration_sec']}s",
                f"- 故事范围：{segment['story_range']}",
                f"- Song Title：`{segment['title']}`",
                f"- 下载后文件名：`{segment['target_audio_filename']}`",
                "",
                "Style 框复制：",
                "",
                "```text",
                segment["style_prompt"],
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _request_text(
    *,
    title: str,
    slug: str,
    skill_path: Path,
    story_file: Path,
    narration_path: Path | None,
    narration_duration: str,
    timeline: str,
    prompts_path: Path,
    plan_path: Path,
    clips_dir: Path,
    final_music_path: Path,
    story: str,
) -> str:
    duration_line = f"旁白总时长约：{narration_duration} 秒" if narration_duration else "旁白总时长：请根据后续音频或文本估算"
    timeline_block = timeline or "暂无逐镜头旁白时间轴。请根据故事文本和旁白总时长自行判断分段。"
    narration_line = str(narration_path) if narration_path else "未指定"
    return f"""# Suno 故事配乐任务：{title}

请使用这个 skill：
[{skill_path.name}]({skill_path})

如果这个 `.skill` 文件是压缩包，请先读取其中的 `suno-story-score/SKILL.md`，再按它的音乐导演规则执行。

## 目标

为这个儿童故事智能设计 Suno V5 Instrumental 背景音乐提示词。音乐用于旁白底乐，必须低音量、给人声留空间，不要人声，不要歌词，不要重鼓点。

{duration_line}

请你作为音乐导演，综合阅读故事文本、逐镜头时间轴、旁白总时长和情绪变化，自行判断音乐分段和每段 Style 提示词。不要按固定百分比或本地模板机械分段。每段对应一个 Suno 生成任务。每段音乐后续会裁剪到 `duration_sec`，最后拼成一个完整背景音乐：

```text
{final_music_path}
```

## 分段原则

- 按情绪阶段分段，不按镜头数量或固定比例分段。
- 通常 2-4 段；只有情绪确实需要时才增加段数。
- 每段最好至少覆盖 30-40 秒旁白；太短的情绪变化应合并到相邻段。
- 必须考虑旁白持续存在，音乐永远是低音量背景，不抢人声。
- 每段之间要有统一的音乐世界、锚点乐器和可自然衔接的 BPM 走廊。
- 中国成语/历史/神话故事可以使用中国色彩乐器，但每段最多作为轻量点缀，不要堆砌。

## 必须保存的文件

请把可复制到 Suno 的提示词保存到：

```text
{prompts_path}
```

请把分段计划保存到 CSV：

```text
{plan_path}
```

CSV 字段必须保持：

```text
segment,label,start_sec,end_sec,duration_sec,story_range,title,style_prompt,target_audio_filename,notes
```

其中 `start_sec/end_sec/duration_sec` 必须根据旁白时间轴和你的分段判断填写；`style_prompt` 必须是可以直接粘贴到 Suno Style 框的英文提示词。`target_audio_filename` 请按下面格式填写，方便下载后自动拼接：

```text
01_{slug}_music.mp3
02_{slug}_music.mp3
03_{slug}_music.mp3
```

Suno 下载后的最终音频请放进：

```text
{clips_dir}
```

## Suno 操作约定

- Suno 页面使用 Advanced / Custom 方式。
- Lyrics 框填写 `[Instrumental]`。
- Style 框粘贴 `style_prompt`。
- Song Title 尽量填写 CSV 的 `title`，并让文件名保留段落序号。
- 如果网页下载文件名不可控，下载后手动改名成 `target_audio_filename`。

## 输入文件

```text
故事文本：{story_file}
旁白音频：{narration_line}
```

## 逐镜头时间轴参考

```text
{timeline_block}
```

请优先依据这个时间轴判断每段的起止时间。如果时间轴为空，再根据旁白总时长和故事文本估算。

## 故事原文

```text
{story}
```
"""


if __name__ == "__main__":
    main()
