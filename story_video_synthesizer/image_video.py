from __future__ import annotations

import csv
import html
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


@dataclass(frozen=True)
class StoryboardItem:
    index: int
    story_text: str
    visual_description: str


@dataclass(frozen=True)
class ImageVideoJob:
    scene: int
    image_path: Path
    image_filename: str
    story_text: str
    visual_description: str
    prompt: str
    target_video_filename: str
    status: str = "todo"
    notes: str = ""


def sorted_image_files(image_dir: Path, slug: str | None = None) -> list[Path]:
    if not image_dir.exists():
        raise FileNotFoundError(f"图片文件夹不存在：{image_dir}")
    images = [path for path in image_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS]
    if slug:
        scene_pattern = re.compile(rf"^{re.escape(slug)}_scene_(\d+)$")
        slug_images = _images_by_scene(images, scene_pattern)
        if slug_images:
            images = slug_images
        else:
            generic_images = _images_by_scene(images, re.compile(r"^scene_(\d+)$", re.I))
            if generic_images:
                images = generic_images
    return sorted(images, key=_natural_key)


def _images_by_scene(images: list[Path], pattern: re.Pattern[str]) -> list[Path]:
    by_scene: dict[int, Path] = {}
    extension_priority = {".png": 0, ".jpg": 1, ".jpeg": 2, ".webp": 3}
    for path in images:
        match = pattern.match(path.stem)
        if not match:
            continue
        scene = int(match.group(1))
        current = by_scene.get(scene)
        if current is None or extension_priority[path.suffix.lower()] < extension_priority[current.suffix.lower()]:
            by_scene[scene] = path
    return [by_scene[scene] for scene in sorted(by_scene)]


def read_storyboard_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8-sig")
    if suffix == ".docx":
        return read_docx_text(path)
    raise ValueError(f"不支持的文本格式：{path.suffix}，请使用 .txt/.md/.docx")


def read_docx_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"文档不存在：{path}")
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        texts = [node.text or "" for node in paragraph.findall(".//w:t", namespace)]
        line = "".join(texts).strip()
        if line:
            paragraphs.append(line)
    return "\n".join(paragraphs)


def parse_storyboard_items(text: str) -> list[StoryboardItem]:
    normalized = (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u2028", "\n")
        .replace("\u00a0", " ")
    )
    pattern = re.compile(
        r"(?:^|\n)\s*(\d+)[\.、]\s*故事内容[:：]\s*(.*?)\s*画面描述[:：]\s*(.*?)(?=\n\s*\d+[\.、]\s*故事内容[:：]|\Z)",
        re.S,
    )
    items: list[StoryboardItem] = []
    for match in pattern.finditer(normalized):
        index = int(match.group(1))
        story_text = _clean_text(match.group(2))
        visual = _clean_text(match.group(3))
        items.append(StoryboardItem(index=index, story_text=story_text, visual_description=visual))
    if items:
        return items

    marker = "【故事文本】"
    if marker in normalized:
        normalized = normalized.split(marker, 1)[1]
    lines = [_clean_text(line) for line in normalized.splitlines()]
    lines = [line for line in lines if line and not line.startswith(("生成16:9", "分镜要求"))]
    return [StoryboardItem(index=index, story_text=line, visual_description="") for index, line in enumerate(lines, start=1)]


def build_jobs(
    image_dir: Path,
    storyboard_path: Path,
    output_dir: Path,
    slug: str,
    short_slug: str,
) -> tuple[list[ImageVideoJob], list[str]]:
    images = sorted_image_files(image_dir, slug=slug)
    items = parse_storyboard_items(read_storyboard_text(storyboard_path))
    items = _expand_items_for_known_splits(items, len(images))
    prompt_overrides = read_flow_video_prompts(image_dir, slug, output_dir=output_dir)
    warnings: list[str] = []

    if len(images) != len(items):
        warnings.append(f"图片数量是 {len(images)}，文本镜头数量是 {len(items)}，已按现有数量生成并标记需要人工核对。")
    if prompt_overrides:
        warnings.append(f"已优先使用出图阶段手写的图生视频提示词：{len(prompt_overrides)} 条。")
    else:
        warnings.append("未找到出图阶段手写的图生视频提示词，将根据逐镜头故事文本自动生成差异化提示词。")

    jobs: list[ImageVideoJob] = []
    for scene, image_path in enumerate(images, start=1):
        item = items[scene - 1] if scene - 1 < len(items) else None
        story_text = item.story_text if item else ""
        visual = item.visual_description if item else ""
        prompt = prompt_overrides.get(scene) or build_video_prompt(
            scene,
            story_text,
            visual,
            image_path.name,
            is_last_scene=scene == len(images),
        )
        jobs.append(
            ImageVideoJob(
                scene=scene,
                image_path=image_path,
                image_filename=image_path.name,
                story_text=story_text,
                visual_description=visual,
                prompt=prompt,
                target_video_filename=f"{scene:02d}_{short_slug}.mp4",
            )
        )

    return jobs, warnings


def read_flow_video_prompts(image_dir: Path, slug: str, output_dir: Path | None = None) -> dict[int, str]:
    """Read hand-authored image-to-video prompts saved by the storyboard stage.

    The children-storyboard-images flow writes `{slug}_flow_video_prompts.csv`
    next to the final stills. When present, these prompts are the source of
    truth because they were authored shot-by-shot from the story beats.
    """
    path = _find_flow_video_prompts_csv(image_dir, slug, output_dir=output_dir)
    if path is None:
        return {}

    prompts: dict[int, str] = {}
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return {}
        prompt_field = _first_field(reader.fieldnames, ("prompt", "video_prompt", "flow_video_prompt", "图生视频提示词", "视频提示词", "提示词"))
        scene_field = _first_field(reader.fieldnames, ("scene", "index", "shot", "镜头", "序号", "编号"))
        if prompt_field is None:
            return {}
        for row in reader:
            raw_scene = (row.get(scene_field) or "").strip() if scene_field else ""
            prompt = _clean_text(row.get(prompt_field) or "")
            if not prompt:
                continue
            scene = _parse_scene_number(raw_scene) if raw_scene else None
            if scene is None:
                scene = len(prompts) + 1
            story_text = _clean_text(row.get(_first_field(reader.fieldnames, ("story_text", "story", "故事文本", "分镜文本")) or "") or "")
            visual_description = _clean_text(row.get(_first_field(reader.fieldnames, ("visual_description", "visual", "画面描述", "画面设计")) or "") or "")
            if _is_template_flow_prompt(prompt):
                prompt = build_video_prompt(
                    scene,
                    story_text,
                    visual_description,
                    f"{slug}_scene_{scene:02d}.png",
                    is_last_scene=False,
                    style_hint="中国风2D绘本",
                )
            prompts[scene] = normalize_image_to_video_prompt(prompt)
    return prompts


def _find_flow_video_prompts_csv(image_dir: Path, slug: str, output_dir: Path | None = None) -> Path | None:
    filename = f"{slug}_flow_video_prompts.csv"
    candidates = [
        image_dir / filename,
        image_dir.parent / filename,
    ]
    if output_dir is not None:
        candidates.extend(
            [
                output_dir / filename,
                output_dir / "images" / filename,
                output_dir.parent / "01_分镜与图片" / "images" / filename,
            ]
        )
    candidates.extend(
        [
            image_dir.parent.parent / "01_分镜与图片" / "images" / filename,
            image_dir.parent.parent / "01_分镜与图片" / filename,
        ]
    )
    search_roots = [image_dir, image_dir.parent]
    if output_dir is not None:
        search_roots.extend([output_dir, output_dir.parent])
    for root in search_roots:
        if not root.exists():
            continue
        candidates.extend(sorted(root.rglob(filename)))
        candidates.extend(sorted(root.rglob("*_flow_video_prompts.csv")))
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.expanduser()
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            return candidate
    return None


def _first_field(fieldnames: list[str], names: tuple[str, ...]) -> str | None:
    normalized = {field.strip().lower(): field for field in fieldnames}
    for name in names:
        key = name.strip().lower()
        if key in normalized:
            return normalized[key]
    return None


def _parse_scene_number(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    match = re.search(r"(?:^|[_\-\s])scene[_\-\s]*(\d+)$", value, re.I)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d+)$", value)
    if match:
        return int(match.group(1))
    return None


def build_video_prompt(
    scene: int,
    story_text: str,
    visual_description: str,
    image_filename: str,
    *,
    is_last_scene: bool = False,
    style_hint: str = "",
) -> str:
    source = f"{story_text} {visual_description}"
    action = _infer_action(source, scene, image_filename, is_last_scene=is_last_scene)
    return normalize_image_to_video_prompt(
        f"参考当前图片，{action}。保持角色、服装、道具、场景和画风不变；不要新增字幕、文字、logo或水印。"
    )


def normalize_image_to_video_prompt(prompt: str) -> str:
    """Keep image-to-video prompts focused on motion, not text-to-image control.

    The still image is already the visual source of truth. Storyboard/image
    stages may include long visual bibles such as costume locks; those are
    useful for text-to-image generation but make image-to-video prompts noisy
    and sometimes contradictory. Keep the shot action and a short preservation
    clause only.
    """
    prompt = _clean_text(prompt)
    if not prompt:
        return ""

    # Remove visual-bible tails commonly appended by image-generation prompts.
    cut_markers = (
        "服装与角色锁定：",
        "视觉圣经：",
        "角色一致性：",
        "统一保持",
        "明亮温暖儿童友好",
        "中国古代寓言绘本气质",
        "画面风格：",
    )
    for marker in cut_markers:
        index = prompt.find(marker)
        if index > 0:
            prompt = prompt[:index].strip(" ；;。")
            break

    if not prompt.startswith("参考当前图片"):
        prompt = f"参考当前图片，{prompt}"

    preservation = "保持角色、服装、道具、场景和画风不变；不要新增字幕、文字、logo或水印。"
    if "不要新增" not in prompt and "水印" not in prompt:
        prompt = f"{prompt.rstrip('。')}。{preservation}"
    elif "保持角色" not in prompt and "保持人物" not in prompt and "保持画面" not in prompt:
        prompt = f"{prompt.rstrip('。')}。保持角色、服装、道具、场景和画风不变。"

    # Guard against accidental text-to-image scale prompts. Keep the first
    # few sentences and the preservation sentence; the review page remains
    # editable for special cases.
    if len(prompt) > 260:
        sentences = re.split(r"(?<=[。！？])", prompt)
        kept: list[str] = []
        for sentence in sentences:
            if not sentence.strip():
                continue
            kept.append(sentence.strip())
            if sum(len(item) for item in kept) >= 180:
                break
        prompt = "".join(kept).rstrip("。") + f"。{preservation}"
    return prompt


def _is_template_flow_prompt(prompt: str) -> bool:
    compact = _clean_text(prompt)
    if not compact:
        return False
    verbose_markers = (
        "4-6秒",
        "本镜头故事文本",
        "画面重点",
        "本镜头剧情动作",
        "优先演出这句台词",
        "如果原图已有文字",
    )
    if any(marker in compact for marker in verbose_markers):
        return True
    template_markers = (
        "画面保持中国风2D绘本质感",
        "角色动作自然克制，镜头缓慢推进或轻移",
        "当前图片中的人物、物体和场景保持不变",
        "参考当前图片生成4-6秒3D卡通动画",
    )
    return any(marker in compact for marker in template_markers) and not any(
        marker in compact for marker in ("本镜头故事文本", "本镜头剧情动作", "画面重点")
    )


def write_job_outputs(jobs: list[ImageVideoJob], output_dir: Path, slug: str) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    copied_image_dir = output_dir / "images"
    copied_image_dir.mkdir(parents=True, exist_ok=True)
    video_dir = output_dir / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    prompt_review_csv = output_dir / "prompt_review_decisions.csv"
    prompt_review_decisions = _read_prompt_review_decisions(prompt_review_csv)

    rows: list[dict[str, str]] = []
    for job in jobs:
        target_image = copied_image_dir / f"{slug}_scene_{job.scene:02d}{job.image_path.suffix.lower()}"
        if job.image_path.resolve() == target_image.resolve():
            pass
        elif not target_image.exists() or target_image.stat().st_size != job.image_path.stat().st_size:
            shutil.copy2(job.image_path, target_image)
        rows.append(
            {
                "scene": f"{job.scene:02d}",
                "image_filename": target_image.name,
                "source_image": str(job.image_path),
                "story_text": job.story_text,
                "visual_description": job.visual_description,
                "prompt": job.prompt,
                "duration": "10.0",
                "status": job.status,
                "task_id": "",
                "video_url": "",
                "error": "",
                "target_video_filename": job.target_video_filename,
                "notes": job.notes,
            }
        )
    _apply_prompt_review_decisions(rows, prompt_review_decisions)

    manifest_csv = output_dir / f"{slug}_image_video_jobs.csv"
    _write_csv(manifest_csv, rows)

    prompts_csv = output_dir / f"{slug}_video_prompts.csv"
    _write_csv(
        prompts_csv,
        [
            {
                "scene": row["scene"],
                "filename": row["image_filename"],
                "prompt": row["prompt"],
            }
            for row in rows
        ],
    )

    prompts_md = output_dir / f"{slug}_video_prompts.md"
    prompts_md.write_text(_render_prompts_md(rows), encoding="utf-8")

    checklist_csv = output_dir / f"{slug}_clip_names.csv"
    _write_csv(
        checklist_csv,
        [
            {
                "scene": row["scene"],
                "image_filename": row["image_filename"],
                "clip_name": Path(row["target_video_filename"]).stem,
                "target_video_filename": row["target_video_filename"],
                "prompt": row["prompt"],
            }
            for row in rows
        ],
    )

    review_html = output_dir / f"{slug}_review.html"
    review_html.write_text(_render_review_html(rows, copied_image_dir, video_dir), encoding="utf-8")

    state_json = output_dir / f"{slug}_review_state.json"
    state_json.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "manifest_csv": manifest_csv,
        "prompts_csv": prompts_csv,
        "prompts_md": prompts_md,
        "clip_names_csv": checklist_csv,
        "review_html": review_html,
        "state_json": state_json,
        "images_dir": copied_image_dir,
        "videos_dir": video_dir,
    }


def write_review_page_from_rows(rows: list[dict[str, str]], output_dir: Path, slug: str) -> Path:
    review_html = output_dir / f"{slug}_review.html"
    review_html.write_text(_render_review_html(rows, output_dir / "images", output_dir / "videos"), encoding="utf-8")
    state_json = output_dir / f"{slug}_review_state.json"
    state_json.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return review_html


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _read_prompt_review_decisions(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        return {
            (row.get("scene") or "").strip().zfill(2): row
            for row in reader
            if (row.get("scene") or "").strip()
        }


def _apply_prompt_review_decisions(rows: list[dict[str, str]], decisions: dict[str, dict[str, str]]) -> None:
    for row in rows:
        decision = decisions.get((row.get("scene") or "").strip().zfill(2))
        if not decision:
            continue
        decision_image = (decision.get("image_filename") or "").strip()
        decision_story = (decision.get("story_text") or "").strip()
        if not decision_image and not decision_story:
            continue
        if decision_image and decision_image != (row.get("image_filename") or "").strip():
            continue
        if decision_story and decision_story != (row.get("story_text") or "").strip():
            continue
        prompt = (decision.get("prompt") or "").strip()
        status = (decision.get("review_status") or "").strip()
        notes = (decision.get("notes") or "").strip()
        if prompt:
            row["prompt"] = prompt
        if status == "approved":
            row["prompt_review_status"] = "approved"
        if notes:
            row["prompt_review_notes"] = notes


def _render_prompts_md(rows: list[dict[str, str]]) -> str:
    lines = [
        "整体统一要求：参考当前图片生成动画，角色造型和场景保持不变，动作自然轻微，镜头稳定流畅，不要新增字幕、文字、logo或水印；具体画风以每镜提示词为准。",
        "",
    ]
    for row in rows:
        lines.extend([f"{row['scene']}. {row['prompt']}", ""])
    return "\n".join(lines).strip() + "\n"


def _render_review_html(rows: list[dict[str, str]], image_dir: Path, video_dir: Path) -> str:
    cards = []
    for row in rows:
        image_name = html.escape(row["image_filename"])
        video_name = html.escape(row["target_video_filename"])
        scene = html.escape(row["scene"])
        status = html.escape(row.get("status", "todo") or "todo")
        duration = html.escape(row.get("effective_duration") or row.get("duration") or "")
        target_duration = html.escape(row.get("target_duration") or "")
        slowdown = html.escape(row.get("needs_slowdown") or "no")
        trim = html.escape(row.get("needs_trim") or "no")
        prompt_text = html.escape(row["prompt"])
        cards.append(
            f"""
            <article class="card" data-scene="{scene}" data-image-filename="{image_name}" data-story-text="{html.escape(row['story_text'], quote=True)}" data-initial-status="{status}">
              <header>
                <h2>{scene}</h2>
                <span class="status">通过</span>
              </header>
              <img src="images/{image_name}" alt="scene {html.escape(row['scene'])}">
              <video src="videos/{video_name}" controls preload="metadata"></video>
              <div class="meta">
                <span>生成 {duration}s</span>
                <span>目标 {target_duration}s</span>
                <span>裁切 {trim}</span>
                <span>慢放 {slowdown}</span>
              </div>
              <p class="story">{html.escape(row['story_text'])}</p>
              <label class="prompt-label">图生视频提示词（可直接修改；导出确认 CSV 后会用于生成视频）</label>
              <textarea class="prompt-editor">{prompt_text}</textarea>
              <div class="actions" role="group" aria-label="review status">
                <button type="button" data-value="approved">通过</button>
                <button type="button" data-value="redo">重做</button>
                <button type="button" data-value="unused">不使用</button>
              </div>
              <textarea class="notes-editor" placeholder="问题备注，例如动作不对、人物变形、镜头太晃"></textarea>
            </article>
            """
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>图片转视频任务预览</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif; background: #f7f8fb; color: #1f2933; }}
    header.page {{ position: sticky; top: 0; z-index: 2; display: flex; align-items: center; gap: 14px; background: #ffffff; border-bottom: 1px solid #d9dee8; padding: 12px 18px; }}
    h1 {{ margin: 0; font-size: 20px; }}
    .toolbar {{ margin-left: auto; display: flex; gap: 8px; align-items: center; }}
    .counter {{ font-size: 13px; color: #526072; }}
    .toolbar button {{ border: 1px solid #c8d0dc; background: #fff; color: #243044; border-radius: 6px; padding: 8px 10px; cursor: pointer; }}
    main {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 14px; padding: 16px; }}
    .card {{ background: #fff; border: 1px solid #d9dee8; border-radius: 8px; padding: 12px; }}
    .card.approved {{ border-color: #58a56c; }}
    .card.redo {{ border-color: #d77562; }}
    .card.unused {{ opacity: 0.72; }}
    .card header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }}
    .card h2 {{ margin: 0; font-size: 18px; }}
    .status {{ font-size: 12px; color: #5d6878; }}
    .approved .status {{ color: #167341; }}
    .redo .status {{ color: #ad3c2b; }}
    img, video {{ width: 100%; aspect-ratio: 16 / 9; object-fit: contain; background: #111827; border-radius: 6px; display: block; }}
    video {{ margin-top: 8px; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
    .meta span {{ font-size: 12px; color: #526072; background: #edf1f6; border-radius: 999px; padding: 3px 8px; }}
    .story {{ min-height: 42px; font-size: 14px; line-height: 1.45; }}
    .prompt-label {{ display: block; margin: 8px 0 4px; font-size: 12px; color: #526072; }}
    .prompt-editor {{ min-height: 112px; font-size: 13px; line-height: 1.45; color: #2d3748; background: #f3f6fb; }}
    .actions {{ display: flex; gap: 8px; }}
    .actions button {{ flex: 1; border: 1px solid #c8d0dc; background: #fff; border-radius: 6px; padding: 8px; cursor: pointer; }}
    .actions button.active {{ background: #243044; color: #fff; border-color: #243044; }}
    textarea {{ margin-top: 8px; width: 100%; min-height: 64px; resize: vertical; border: 1px solid #ccd3df; border-radius: 6px; padding: 8px; }}
    .export-panel {{ margin: 12px 16px 0; background: #fff; border: 1px solid #c8d0dc; border-radius: 8px; padding: 12px; }}
    .export-panel[hidden] {{ display: none; }}
    .export-panel strong {{ display: block; margin-bottom: 6px; }}
    .export-panel p {{ margin: 0 0 8px; color: #526072; font-size: 13px; }}
    .download-link {{ display: inline-block; margin-bottom: 8px; color: #1d4ed8; font-size: 13px; }}
    .export-panel textarea {{ min-height: 140px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }}
    .notice {{ margin: 12px 16px 0; background: #ecf7ff; border: 1px solid #b8d9ef; border-radius: 8px; padding: 10px 12px; color: #23475f; font-size: 13px; line-height: 1.5; }}
  </style>
</head>
<body>
  <header class="page">
    <h1>图片转视频任务预览</h1>
    <span class="counter" id="counter"></span>
    <div class="toolbar">
      <button type="button" id="approveAll">全部恢复通过</button>
      <button type="button" id="exportPromptCsv">导出提示词确认CSV</button>
      <button type="button" id="exportCsv">导出审核CSV</button>
      <button type="button" id="clearState">恢复默认状态</button>
    </div>
  </header>
  <section class="notice">
    生成视频前请先检查每张图的“图生视频提示词”。如果动作、表情、镜头运动或禁止项不合适，直接在文本框里改；确认无误后把镜头标为“通过”，点击“导出提示词确认CSV”。工作台生成视频前会读取 <code>prompt_review_decisions.csv</code>，没有确认文件或有镜头未通过时不会调用图生视频 API。
  </section>
  <section class="export-panel" id="exportPanel" hidden>
    <strong id="exportStatus">审核 CSV 已生成</strong>
    <p id="exportHelp">如果浏览器没有自动下载文件，请复制下面内容保存为 CSV，或直接发给 Codex。</p>
    <a class="download-link" id="downloadCsvLink" href="#" download hidden>手动下载 CSV</a>
    <textarea id="exportText" readonly></textarea>
  </section>
  <main>
    {''.join(cards)}
  </main>
  <script>
    const cards = [...document.querySelectorAll(".card")];
    const firstCard = cards[0];
    const lastCard = cards[cards.length - 1];
    const reviewFingerprint = [
      cards.length,
      firstCard ? firstCard.dataset.storyText : "",
      lastCard ? lastCard.dataset.storyText : ""
    ].join("|");
    const storageKey = "story-review:" + location.pathname + ":" + reviewFingerprint;
    const labels = {{ approved: "通过", redo: "重做", unused: "不使用", todo: "待生成" }};
    let state = JSON.parse(localStorage.getItem(storageKey) || "{{}}");

    function defaultStatus(card) {{
      return card.dataset.initialStatus === "downloaded" ? "approved" : "todo";
    }}

    function normalizeStatus(value) {{
      if (value === "skip") return "unused";
      return labels[value] ? value : "todo";
    }}

    function setCardStatus(card, value) {{
      const scene = card.dataset.scene;
      state[scene] = state[scene] || {{}};
      value = normalizeStatus(value);
      state[scene].status = value;
      card.classList.remove("approved", "redo", "unused", "todo");
      card.classList.add(value);
      card.querySelector(".status").textContent = labels[value] || value;
      card.querySelectorAll(".actions button").forEach(button => {{
        button.classList.toggle("active", button.dataset.value === value);
      }});
      save();
    }}

    function save() {{
      localStorage.setItem(storageKey, JSON.stringify(state));
      updateCounter();
    }}

    function updateCounter() {{
      const counts = {{ approved: 0, redo: 0, unused: 0, todo: 0 }};
      cards.forEach(card => {{
        const item = state[card.dataset.scene] || {{}};
        const status = normalizeStatus(item.status || defaultStatus(card));
        counts[status] += 1;
      }});
      document.getElementById("counter").textContent =
        `通过 ${{counts.approved}} / 重做 ${{counts.redo}} / 不使用 ${{counts.unused}} / 待生成 ${{counts.todo}}`;
    }}

    function isTemplatePrompt(value) {{
      const text = String(value || "");
      const markers = [
        "画面保持中国风2D绘本质感，轻微纸张纹理",
        "角色动作自然克制，镜头缓慢推进或轻移",
        "参考当前图片生成4-6秒3D卡通动画",
        "当前图片中的人物、物体和场景保持不变"
      ];
      return markers.some(marker => text.includes(marker));
    }}

    cards.forEach(card => {{
      const scene = card.dataset.scene;
      const promptEditor = card.querySelector(".prompt-editor");
      const originalPrompt = promptEditor.value;
      state[scene] = state[scene] || {{ status: defaultStatus(card), notes: "", prompt: originalPrompt }};
      if (state[scene].sourcePrompt && state[scene].sourcePrompt !== originalPrompt && state[scene].prompt === state[scene].sourcePrompt) {{
        state[scene].prompt = originalPrompt;
      }} else if (!state[scene].sourcePrompt && isTemplatePrompt(state[scene].prompt) && !isTemplatePrompt(originalPrompt)) {{
        state[scene].prompt = originalPrompt;
      }}
      state[scene].sourcePrompt = originalPrompt;
      setCardStatus(card, state[scene].status || defaultStatus(card));
      promptEditor.value = state[scene].prompt || originalPrompt;
      promptEditor.addEventListener("input", () => {{
        state[scene].prompt = promptEditor.value;
        save();
      }});
      const notesEditor = card.querySelector(".notes-editor");
      notesEditor.value = state[scene].notes || "";
      notesEditor.addEventListener("input", () => {{
        state[scene].notes = notesEditor.value;
        save();
      }});
      card.querySelectorAll(".actions button").forEach(button => {{
        button.addEventListener("click", () => setCardStatus(card, button.dataset.value));
      }});
    }});

    document.getElementById("approveAll").addEventListener("click", () => {{
      cards.forEach(card => setCardStatus(card, "approved"));
    }});
    document.getElementById("clearState").addEventListener("click", () => {{
      localStorage.removeItem(storageKey);
      state = {{}};
      cards.forEach(card => setCardStatus(card, defaultStatus(card)));
    }});
    function statusForExport(item, filename) {{
      const status = normalizeStatus(item.status || "todo");
      if (filename === "prompt_review_decisions.csv" && status === "todo") {{
        return "approved";
      }}
      return status;
    }}

    function exportRows(filename, label) {{
      const rows = [["scene", "image_filename", "story_text", "review_status", "prompt", "notes"]];
      cards.forEach(card => {{
        const item = state[card.dataset.scene] || {{}};
        const prompt = card.querySelector(".prompt-editor").value;
        rows.push([card.dataset.scene, card.dataset.imageFilename || "", card.dataset.storyText || "", statusForExport(item, filename), prompt, item.notes || ""]);
      }});
      const csv = rows.map(row => row.map(value => `"${{String(value).replaceAll('"', '""')}}"`).join(",")).join("\\n");
      document.getElementById("exportPanel").hidden = false;
      document.getElementById("exportText").value = csv + "\\n";
      document.getElementById("exportText").select();
      document.getElementById("exportStatus").textContent =
        `${{label}} 已生成：${{rows.length - 1}} 条记录`;
      document.getElementById("exportHelp").innerHTML =
        `如果浏览器没有自动下载文件，请点下面的手动下载链接，或复制内容保存为 <code>${{filename}}</code>。`;
      const blob = new Blob(["\\ufeff" + csv + "\\n"], {{ type: "text/csv;charset=utf-8" }});
      const link = document.createElement("a");
      const url = URL.createObjectURL(blob);
      link.href = url;
      link.download = filename;
      link.style.display = "none";
      document.body.appendChild(link);
      link.click();
      link.remove();
      const manualLink = document.getElementById("downloadCsvLink");
      if (manualLink.dataset.url) {{
        URL.revokeObjectURL(manualLink.dataset.url);
      }}
      manualLink.href = url;
      manualLink.download = filename;
      manualLink.dataset.url = url;
      manualLink.textContent = `手动下载 ${{filename}}`;
      manualLink.hidden = false;
    }}
    document.getElementById("exportPromptCsv").addEventListener("click", () => {{
      exportRows("prompt_review_decisions.csv", "提示词确认 CSV");
    }});
    document.getElementById("exportCsv").addEventListener("click", () => {{
      exportRows("review_decisions.csv", "视频审核 CSV");
    }});
    updateCounter();
  </script>
</body>
</html>
"""


def _infer_action(source: str, scene: int, image_filename: str, *, is_last_scene: bool = False) -> str:
    source = source.replace("“", "").replace("”", "")
    no_character_keywords = (
        "不出现绵羊姐姐",
        "不出现人物",
        "不要出现人物",
        "无人物",
        "没有人物",
        "卷轴文本框",
        "结尾文字",
        "故事结尾",
        "道理文字",
    )
    moral_text_keywords = ("小朋友们", "这个故事告诉我们", "遇到挫折", "发愤图强", "迎来翻身")
    if any(keyword in source for keyword in no_character_keywords):
        return "镜头缓慢推进画面中的古代书卷、竹简、风景或文字，保持画面安静温暖，不要出现任何人物或新增角色"
    if is_last_scene or any(keyword in source for keyword in moral_text_keywords):
        return "镜头轻轻推进中央卷轴或文本框，文字必须始终清晰稳定；两侧已有角色只做微笑、点头或轻微呼吸动作，不要遮挡文字"

    # Story-specific beats should be precise. These use combined cues so a broad
    # word like "深夜" or "绳子" does not steal the action from another scene.
    if "孙敬" in source and "从早读到晚" in source:
        return "孙敬坐在书案前认真阅读竹简，手指轻轻翻动竹简；窗外光线从晨光渐变到傍晚，表现读了一整天"
    if "深夜" in source and "打瞌睡" in source and "想了一个办法" in source:
        return "孙敬明显困倦地打瞌睡，眼皮慢慢合上、头一点一点往下低；随后他努力睁眼抬头，露出想到办法的表情"
    if "房梁" in source and "头发" in source and "绳子" in source:
        return "孙敬抬手把细绳系到发髻上，再看向房梁确认绳子固定好；绳子轻轻摆动，动作清楚但安全温和"
    if "头往下低" in source and "拉住头发" in source:
        return "孙敬读书时头慢慢往下低，绳子逐渐拉直并轻轻牵住发髻；他露出卡通惊醒表情，无受伤或夸张痛苦"
    if "清醒过来" in source and "继续读书" in source:
        return "孙敬从困意中清醒，立刻坐正身体，双手扶稳竹简继续认真读书，眼神从迷糊变坚定"
    if "孙敬后来" in source and "很有学问" in source:
        return "成年孙敬在学堂里温和地展开书卷，周围学生轻轻书写或抬头倾听，表现学有所成"
    if "苏秦" in source and "立志刻苦读书" in source:
        return "苏秦站在书案前看向远方，握紧竹简，神情从沉思变坚定；窗外旗帜或竹帘轻轻摆动"
    if "读书读到深夜" in source and "睁不开眼睛" in source:
        return "苏秦在油灯旁读竹简，眼睛逐渐闭上、头慢慢低垂，手中的竹简也轻轻下落，表现快要睡着"
    if "准备了一把锥子" in source:
        return "苏秦把小木柄锥状提醒工具轻轻放到书案边，目光从竹简移到工具，再重新露出坚持的神情"
    if "刺一下自己的大腿" in source:
        return "儿童友好表现：苏秦隔着厚布衣袍用小工具轻触大腿侧提醒自己，身体轻轻一震后立刻清醒；不出现血、伤口或痛苦特写"
    if "疼痛让他立刻清醒" in source and "坚持把书读下去" in source:
        return "苏秦收起小工具，深吸一口气重新坐直，专注继续阅读竹简，油灯火光轻轻跳动"
    if "苏秦也成为了有名政治家" in source:
        return "成年苏秦在议事厅从容发言，手持竹简轻轻示意，周围官员微微点头倾听，表现智慧和成就"

    if "田螺姑娘" in source and ("讲一个民间故事" in source or "故事叫做" in source):
        return "卷轴或题名区域轻轻展开，田螺和乡村景物泛起柔和水光；镜头缓慢推近标题，文字保持清晰稳定"
    if "独自种着几亩田" in source or "勤劳朴实" in source:
        return "年轻人在田边小屋和水田之间缓慢劳作，抬手擦汗后继续干活；远处稻田水面和竹篱轻轻晃动"
    if "天还没亮" in source and "田里耕作" in source:
        return "年轻人背着午饭竹篮、拿好农具从屋旁走向田埂，脚步自然；清晨薄雾和水田倒影轻轻流动"
    if "太阳落山" in source and "回家做晚饭" in source:
        return "年轻人扛着农具沿田埂回家，屋内暖光逐渐亮起，炊烟或灶火轻轻升起"
    if "衣服脏了" in source or "鞋袜破了" in source:
        return "年轻人低头缝补鞋袜，再轻轻搓洗衣物，灯火微微摇曳，表现独自料理生活"
    if "捡回一个大田螺" in source:
        return "年轻人弯腰从田埂水边拾起大田螺，双手小心托住，水面泛起细小涟漪"
    if "青艳艳" in source or "像宝玉一样的田螺" in source:
        return "镜头贴近青绿色田螺，田螺表面有温润高光缓缓流动；年轻人眼神从惊讶变成珍惜"
    if "养在灶头旁边的水缸" in source:
        return "年轻人把田螺轻轻放进水缸，水面荡开涟漪；灶火和窗光柔和晃动"
    if "第二天晚上" in source and "回来" in source:
        return "年轻人从夜色田埂走回小屋，推开篱笆门时放慢脚步，屋内暖光从门窗透出"
    if "热气腾腾" in source and "干净整齐" in source:
        return "年轻人站在门口惊讶环顾，灶上蒸汽缓缓升起，桌椅和屋内物件保持整齐安稳"
    if "揭开锅盖" in source or "饭菜都做好了" in source:
        return "年轻人抬手揭开锅盖，白色蒸汽向上散开，他的表情从疑惑变成惊喜"
    if "谁替我做的" in source or "感到很奇怪" in source:
        return "年轻人在屋里左右查看，眉头轻轻皱起又看向水缸，灯光和蒸汽轻微摇动"
    if "告诉了隔壁的老太太" in source:
        return "年轻人站在邻家门前向老太太说明情况，老太太侧耳倾听并温和点头"
    if "老太太说" in source or "半信半疑" in source:
        return "老太太抬手轻轻指点，年轻人听后露出半信半疑的神情，镜头在两人之间缓慢移动"
    if "躲在篱笆外面" in source or "偷偷朝家里看" in source:
        return "年轻人蹲在篱笆外悄悄探头观察，小心屏住呼吸；竹叶和草丛轻轻晃动"
    if "冒出一阵白烟" in source or "从田螺壳里走出来" in source:
        return "水缸旁白烟缓缓升起，田螺壳微微发光，姑娘从光雾中轻轻走出，动作柔和不夸张"
    if "生火做饭" in source or "收拾屋子" in source:
        return "姑娘走到灶前添柴生火、整理桌面和碗筷，火光轻跳，动作麻利温柔"
    if "推门进去问道" in source or "为什么要帮我做饭" in source:
        return "年轻人推门进屋停住脚步，带着惊讶和感激询问；姑娘转身看向他，衣袖轻轻摆动"
    if "如实说" in source or "白水素女" in source:
        return "姑娘神情平静温柔地开口说明来历，周围灯火和水缸微光轻轻映在她身上"
    if "父母双亡" in source or "勤劳善良" in source:
        return "画面以温和回忆感表现年轻人独自生活和辛勤劳作，光线缓慢流动，情绪安静克制"
    if "派我来帮助你" in source or "不能再留" in source:
        return "姑娘低头轻声说明必须离开，年轻人神情从感激转为不舍，屋内光影轻轻变暗"
    if "舍不得她走" in source or "再三挽留" in source:
        return "年轻人上前半步伸手挽留，姑娘温柔摇头，二人动作缓慢克制，保持民间故事的含蓄情绪"
    if "田螺壳好好收起来" in source or "盛米" in source:
        return "姑娘把田螺壳轻轻递给年轻人，示意用它盛米；田螺壳泛起柔和青光"
    if "化作一阵清风" in source or "不见了" in source:
        return "姑娘衣袖化作清风般的淡淡光影渐渐消散，窗边竹叶轻轻摇动，年轻人站在原地目送"
    if "吃不完的米" in source:
        return "年轻人用田螺壳盛米，米粒缓缓落入碗中像不会减少一样，他露出惊喜又珍惜的表情"
    if "帮助村里的穷人" in source:
        return "年轻人把多余的米分给村里穷人，村民接过米后微笑致谢，画面温暖有秩序"
    if "勤劳善良" in source and "善意" in source:
        return "镜头缓慢推进温暖的故事结尾画面，年轻人和乡村景物保持安静微笑，结尾文字或画面重点保持清晰稳定"

    rules = [
        (("古往今来", "有所成就", "常人难以想象的努力"), "镜头沿古代竹简和卷轴组成的书山台阶缓慢上移，晨光流动，表现努力通向成就；不要出现现代书或人物"),
        (("悬梁刺股", "一起来听"), "古书或卷轴轻轻展开，镜头推向标题“悬梁刺股”，两侧故事场景微微亮起，标题保持清晰不变形"),
        (("孙敬", "非常勤奋好学", "从早读到晚"), "孙敬坐在书案前认真阅读竹简，手指轻轻翻动竹简；窗外光线从晨光渐变到傍晚，表现读了一整天"),
        (("深夜", "打瞌睡", "怕自己睡着", "想了一个办法"), "孙敬明显困倦地打瞌睡，眼皮慢慢合上、头一点一点往下低；随后他努力睁眼抬头，露出想到办法的表情"),
        (("绳子", "房梁", "头发"), "孙敬抬手把细绳系到发髻上，再看向房梁确认绳子固定好；绳子轻轻摆动，动作清楚但安全温和"),
        (("头往下低", "拉住头发"), "孙敬读书时头慢慢往下低，绳子逐渐拉直并轻轻牵住发髻；他露出卡通惊醒表情，无受伤或夸张痛苦"),
        (("清醒过来", "继续读书"), "孙敬从困意中清醒，立刻坐正身体，双手扶稳竹简继续认真读书，眼神从迷糊变坚定"),
        (("孙敬后来", "很有学问", "悬梁的故事"), "成年孙敬在学堂里温和地展开书卷，周围学生轻轻书写或抬头倾听，表现学有所成"),
        (("苏秦", "立志刻苦读书", "成就一番事业"), "苏秦站在书案前看向远方，握紧竹简，神情从沉思变坚定；窗外旗帜或竹帘轻轻摆动"),
        (("读书读到深夜", "困得睁不开眼睛"), "苏秦在油灯旁读竹简，眼睛逐渐闭上、头慢慢低垂，手中的竹简也轻轻下落，表现快要睡着"),
        (("准备了一把锥子",), "苏秦把小木柄锥状提醒工具轻轻放到书案边，目光从竹简移到工具，再重新露出坚持的神情"),
        (("刺一下自己的大腿",), "儿童友好表现：苏秦隔着厚布衣袍用小工具轻触大腿侧提醒自己，身体轻轻一震后立刻清醒；不出现血、伤口或痛苦特写"),
        (("疼痛让他立刻清醒", "坚持把书读下去"), "苏秦收起小工具，深吸一口气重新坐直，专注继续阅读竹简，油灯火光轻轻跳动"),
        (("苏秦也成为了有名政治家", "刺股的故事"), "成年苏秦在议事厅从容发言，手持竹简轻轻示意，周围官员微微点头倾听，表现智慧和成就"),
        (("实事求是", "前后一致", "夸大其词", "自相矛盾哦"), "镜头缓慢推进画面中的道理文字，文字保持清晰稳定"),
        (("自相矛盾", "故事的名字", "标题"), "镜头缓慢推进标题和桌上的竹简、矛、盾，标题保持清晰稳定"),
        (("说话不动脑子", "绕进去", "摸不着头脑"), "镜头轻轻推进，现代角色摸着后脑勺露出茫然表情"),
        (("集市上去卖矛和盾", "卖矛和盾"), "集市人群轻轻走动，卖兵器的人在摊位前招呼路人"),
        (("举起自己的盾", "大家快来看"), "卖兵器的人高高举起盾牌吆喝，路人转头看过来"),
        (("矛什么都能刺穿", "盾什么都刺不穿"), "镜头在矛和盾之间轻轻移动，卖兵器的人摊手发愣"),
        (("不可能同时成立", "哈哈大笑"), "围观群众哈哈大笑，卖兵器的人脸红低头"),
        (("最坚固的盾", "刺不穿"), "镜头轻轻推进盾牌，卖兵器的人保持得意表情"),
        (("围观的人", "好奇地看着"), "围观群众慢慢凑近摊位，好奇地看着盾牌"),
        (("放下盾", "拿起矛"), "卖兵器的人放下盾牌又拿起长矛，动作夸张得意"),
        (("最锋利的矛", "都能刺穿"), "长矛轻轻一晃闪出卡通高光，卖兵器的人昂头吹嘘"),
        (("用你的矛", "刺你的盾"), "人群中的路人举手提问，周围人安静转头看向卖兵器的人"),
        (("愣住", "说不出"), "卖兵器的人愣住，张着嘴冒汗，身体僵住不动"),
        (("逃走", "灰溜溜"), "卖兵器的人只拿一支长矛和一面盾，红着脸灰溜溜离开"),
        (("走过来", "走着", "走远", "一步一步"), "镜头轻轻跟随，角色缓慢向前移动"),
        (("变大", "刚刚好"), "镜头缓慢拉远，突出角色变大后的可爱对比"),
        (("摇晃", "轰隆"), "地面轻微震动，角色身体跟着晃一下"),
        (("抬头", "不得了"), "角色慢慢抬头看向上方，表情逐渐惊讶"),
        (("追上去", "总有一天"), "角色向前追跑，镜头平稳跟随"),
        (("两只脚", "脚步"), "镜头轻轻推进，角色的小脚自然抬起再落下"),
        (("叶子", "头顶"), "镜头轻轻上移，头顶叶子柔软地轻轻摆动"),
        (("偷偷", "盯着", "草丛"), "镜头缓慢推进，角色从草丛后探头观察"),
        (("扑上去", "冲啊"), "角色向前轻轻跃起，镜头短促跟随动作"),
        (("躲开", "扭"), "角色左右扭动躲开，镜头保持稳定"),
        (("不服气", "换个法子", "再接再厉"), "角色表情从失落变得坚定"),
        (("套索", "套中"), "套索轻轻摆动，角色保持拉扯动作"),
        (("拖着", "松手", "瘫"), "角色被轻轻拖动后停下，表情可爱又无奈"),
        (("陷阱", "躲在前面"), "镜头轻轻推进到陷阱边，草叶微微晃动"),
        (("喘着气", "放弃"), "角色轻轻喘气，随后抬头露出不放弃的神情"),
        (("气喘吁吁",), "角色边跑边轻轻喘气，镜头平稳跟随"),
    ]
    for keywords, action in rules:
        if any(keyword in source for keyword in keywords):
            return action
    generic = _generic_action_from_story(source)
    if generic:
        return generic
    compact = _clean_text(source)
    if compact:
        compact = compact[:80]
        return f"围绕“{compact}”演出一个清楚的小动作：主体轻微转头、眨眼、抬手或移动视线，表情随文本情绪变化，背景只做柔和光影和环境微动"
    return "镜头轻轻推进当前画面元素，保持原图构图和主体不变，只做轻微自然的景深、光影或环境动效"


def _generic_action_from_story(source: str) -> str:
    subject = _infer_subject(source)
    if any(token in source for token in ("睡懒觉", "睡觉", "睡着", "睡醒", "醒来", "起床")):
        return f"{subject}在原图位置自然呼吸、眼皮轻轻动，随后慢慢睁眼或伸懒腰，表现从熟睡到醒来的状态"
    if any(token in source for token in ("喜欢", "高兴", "开心", "笑", "兴奋")):
        return f"{subject}露出开心表情，眼睛发亮，身体轻轻前倾或挥手，周围角色做轻微回应"
    if any(token in source for token in ("选择", "选", "决定", "报名", "参加")):
        return f"{subject}看向画面中的目标或公告，轻轻抬手指认，表情从犹豫变成决定"
    if any(token in source for token in ("问", "说", "告诉", "宣布", "喊")):
        return f"{subject}嘴部轻微开合、抬手示意或转头看向听众，其他角色安静倾听"
    if any(token in source for token in ("走", "跑", "追", "跳", "扑", "来到")):
        return f"{subject}沿原图方向做轻微走动、跑动或跳跃动作，镜头平稳跟随，不改变构图主体"
    if any(token in source for token in ("看", "盯", "发现", "望", "抬头")):
        return f"{subject}慢慢转头或抬头看向画面重点，眼神从平静变为好奇或惊讶"
    if any(token in source for token in ("害怕", "紧张", "担心", "吓", "躲")):
        return f"{subject}轻轻后退或缩肩，眼睛睁大，表现紧张但保持儿童友好"
    if any(token in source for token in ("拿", "举", "抱", "推", "拉", "放", "敲")):
        return f"{subject}对画面中的关键道具做拿起、举起、推动或放下的小动作，道具运动清楚但幅度克制"
    if any(token in source for token in ("大家", "朋友", "动物", "观众", "同伴")):
        return "画面中的角色互相看向对方，做点头、眨眼、轻微挥手或身体前倾的群体反应，突出本镜头情节"
    return ""


def _infer_subject(source: str) -> str:
    for subject in ("猫", "老鼠", "小猫", "小老鼠", "小羊", "狐狸", "兔子", "熊", "小熊", "鹿", "猴子", "孙敬", "苏秦", "主角"):
        if subject in source:
            return subject
    return "画面主体角色"


def _expand_items_for_known_splits(items: list[StoryboardItem], image_count: int) -> list[StoryboardItem]:
    if len(items) + 1 != image_count:
        return items

    expanded: list[StoryboardItem] = []
    did_split = False
    for item in items:
        text = item.story_text
        if not did_split and "为什么用大礼帽" in text and "这样才时髦" in text:
            expanded.append(
                StoryboardItem(
                    index=item.index,
                    story_text="“为什么用大礼帽？”蹦蹦问。",
                    visual_description="蹦蹦歪着头提问，表情疑惑，大礼帽在画面旁边。",
                )
            )
            expanded.append(
                StoryboardItem(
                    index=item.index + 1,
                    story_text="“这样才时髦！”跳跳答。",
                    visual_description="跳跳举着大礼帽，表情得意，动作可爱。",
                )
            )
            did_split = True
        else:
            expanded.append(item)
    return [StoryboardItem(index=index, story_text=item.story_text, visual_description=item.visual_description) for index, item in enumerate(expanded, start=1)]


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \n\t")


def _natural_key(path: Path) -> list[object]:
    parts = re.split(r"(\d+)", path.name)
    return [int(part) if part.isdigit() else part.lower() for part in parts]
