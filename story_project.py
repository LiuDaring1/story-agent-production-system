from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageStat

from story_video_synthesizer.image_video import IMAGE_EXTENSIONS, sorted_image_files
from story_video_synthesizer.media import VIDEO_EXTENSIONS, probe_duration


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "pipeline_config.json"
MANIFEST_NAME = "project_manifest.json"
STATUS_DIR_NAME = "99_项目状态"
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
TEXT_EXTENSIONS = {".txt", ".md", ".docx"}

PROJECT_DIRS = {
    "inputs": "00_输入素材",
    "images": "01_分镜与图片",
    "video_jobs": "02_图生视频",
    "assembly": "03_背景成片",
    "release": "04_发布视频",
    "publish": "05_发布物料",
    "product": "06_资料包",
    "status": STATUS_DIR_NAME,
}

DEFAULT_CONFIG: dict[str, Any] = {
    "latest_episode": 92,
    "default_story_type": "童话故事",
    "default_image_style": "自动",
    "story_type_options": ["寓言故事", "成语故事", "童话故事", "民间故事", "神话故事", "红色故事", "历史故事", "科普故事"],
    "age_range_options": ["3-5岁", "6-8岁", "9-11岁", "12-14岁", "15岁以上"],
    "default_age_range": "6-8岁",
    "brand_assets": {
        "assets_dir": "/Users/baiyanglin/Desktop/（常用）剪辑所使用的素材",
        "logo": "",
        "watermark_logo": "",
        "story_logo": "",
        "antipiracy_logo": "",
        "frame_reference": "",
        "cover_reference": "",
    },
    "release_defaults": {
        "video_box": "0,416,1080,608",
        "story_box": "210,270,910,512",
        "story_bleed": 0,
        "background_blur": 14,
        "b_story_box": "356,180,1209,680",
        "b_windows": "auto",
        "c_windows": "auto",
        "person_height": 900,
        "person_x": 755,
        "person_y": 108,
        "person_crop": "",
        "person_grade": "log-soft",
        "person_beauty": "light",
        "keyer": "colorkey",
        "chroma_color": "0x00FF00",
        "chroma_similarity": 0.095,
        "chroma_blend": 0.04,
        "watermark_width": 190,
        "watermark_opacity": 0.78,
        "watermark_speed": 1.0,
        "story_logo_width_a": 150,
        "story_logo_width_b": 175,
        "story_logo_x": 42,
        "story_logo_y": 44,
        "output_scale": 2,
        "crf": 17,
        "preset": "medium",
    },
    "product_defaults": {
        "music_volume": 0.22,
        "narration_volume": 1.0,
    },
    "external_tools": {
        "suno_story_score_skill": str(Path.home() / "Downloads" / "suno-story-score.skill"),
        "story_performance_script_skill": str(Path.home() / "Downloads" / "story-performance-script.skill"),
    },
    "video_api": {
        "provider": "qingyun_api",
        "fallback_provider": "browser",
        "browser_provider_name": "Flow",
        "adapters": {
            "qingyun_api": {
                "runner": "run_image_video_jobs.py",
                "base_url": "https://api.qingyuntop.top/v1",
                "model": "grok-video-3-10s",
                "api_key_env": "QINGYUN_API_KEY",
                "estimated_cost_cny_per_clip": 3.0,
            },
            "mock_local": {
                "runner": "mock_video_provider.py",
                "model": "ffmpeg-still-frame",
                "api_key_env": "",
                "estimated_cost_cny_per_clip": 0.0,
            }
        },
    },
    "agent_defaults": {
        "soft_budget_cny": 50.0,
        "hard_budget_cny": 100.0,
        "deadline_hours": 10.0,
        "review_pass_score": 85,
        "max_attempts": 2,
        "max_critical_attempts": 3,
        "whisper_model": "small",
    },
}


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    inputs: Path
    images: Path
    video_jobs: Path
    assembly: Path
    release: Path
    publish: Path
    product: Path
    status: Path
    manifest: Path


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG, ensure_ascii=False))
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return deep_merge(DEFAULT_CONFIG, config)


def save_config(config: dict[str, Any]) -> None:
    save_json(CONFIG_PATH, config)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(base, ensure_ascii=False))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def project_paths(project_dir: Path) -> ProjectPaths:
    root = project_dir.expanduser()
    return ProjectPaths(
        root=root,
        inputs=root / PROJECT_DIRS["inputs"],
        images=root / PROJECT_DIRS["images"],
        video_jobs=root / PROJECT_DIRS["video_jobs"],
        assembly=root / PROJECT_DIRS["assembly"],
        release=root / PROJECT_DIRS["release"],
        publish=root / PROJECT_DIRS["publish"],
        product=root / PROJECT_DIRS["product"],
        status=root / PROJECT_DIRS["status"],
        manifest=root / PROJECT_DIRS["status"] / MANIFEST_NAME,
    )


def ensure_project_dirs(paths: ProjectPaths) -> None:
    for directory in (
        paths.root,
        paths.inputs,
        paths.images,
        paths.video_jobs,
        paths.assembly,
        paths.release,
        paths.publish,
        paths.product,
        paths.status,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def init_project(project_dir: Path, story_name: str = "", slug: str = "", episode: int | None = None) -> dict[str, Any]:
    config = load_config()
    paths = project_paths(project_dir)
    ensure_project_dirs(paths)
    inferred_name = story_name.strip() or infer_story_name_from_folder(paths.root)
    requested_slug = slug.strip()
    safe_slug = requested_slug or slugify(inferred_name)
    next_episode = int(config.get("latest_episode", 0)) + 1
    manifest = load_manifest(paths) or {}
    manifest = deep_merge(
        default_manifest(paths, config, inferred_name, safe_slug, episode or next_episode),
        manifest,
    )
    final_slug = requested_slug or manifest["story"].get("slug") or safe_slug
    manifest["project_root"] = str(paths.root)
    manifest["story"].update(
        {
            "name": inferred_name,
            "slug": final_slug,
            "short_slug": short_slug(str(final_slug)),
            "episode": int(episode or manifest["story"].get("episode") or next_episode),
        }
    )
    write_manifest(paths, manifest)
    return manifest


def default_manifest(paths: ProjectPaths, config: dict[str, Any], story_name: str, slug: str, episode: int) -> dict[str, Any]:
    agent_defaults = config.get("agent_defaults", {}) if isinstance(config.get("agent_defaults"), dict) else {}
    return {
        "version": 2,
        "project_root": str(paths.root),
        "story": {
            "name": story_name,
            "slug": slug,
            "short_slug": short_slug(slug),
            "episode": episode,
            "story_type": config.get("default_story_type", "童话故事"),
            "image_style": config.get("default_image_style", "自动"),
            "age_range": config.get("default_age_range", "6-8岁"),
            "duration_text": "",
            "update_latest_episode_on_delivery": True,
            "does_not_count_episode": False,
            "manual_overrides": {},
        },
        "inputs": {
            "story_text": "",
            "narration": "",
            "music": "",
            "greenscreen_video": "",
            "greenscreen_video_original": "",
            "extracted_narration": "",
        },
        "outputs": {
            "storyboard": "",
            "jobs_csv": "",
            "review_html": "",
            "person_reference": "",
            "theme_assets_prompt": "",
            "release_plate_image": "",
            "main_background_image": "",
            "story_frame_source": "",
            "story_frame_a": "",
            "keying_preset": "",
            "background_video_with_sub": "",
            "background_video_no_sub": "",
            "main_release_video": "",
            "library_release_video": "",
            "publish_package": "",
            "product_base": "",
            "product_advanced": "",
            "consumer_manuscript": "",
            "source_edit_decisions": "",
            "source_subtitles": "",
        },
        "qa": {},
        "stages": {},
        "agent": {
            "job_id": "",
            "status": "pending",
            "cancel_requested": False,
            "heartbeat_at": "",
            "last_checkpoint": "",
            "blocked_reason": "",
            "deadline_hours": float(agent_defaults.get("deadline_hours", 10.0)),
            "min_free_disk_gb": float(agent_defaults.get("min_free_disk_gb", 10.0)),
            "started_at": "",
            "finished_at": "",
            "budget": {
                "currency": "CNY",
                "soft_limit": float(agent_defaults.get("soft_budget_cny", 50.0)),
                "hard_limit": float(agent_defaults.get("hard_budget_cny", 100.0)),
                "spent": 0.0,
                "reserved": 0.0,
                "entries": [],
            },
            "source": {},
            "stages": {},
            "events": [],
        },
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def load_manifest(paths: ProjectPaths) -> dict[str, Any] | None:
    if not paths.manifest.exists():
        return None
    return json.loads(paths.manifest.read_text(encoding="utf-8"))


def write_manifest(paths: ProjectPaths, manifest: dict[str, Any]) -> None:
    ensure_project_dirs(paths)
    manifest["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json(paths.manifest, manifest)


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def infer_story_name_from_folder(path: Path) -> str:
    name = path.name.strip()
    if name.startswith("故事剪辑："):
        name = name.split("：", 1)[1].strip()
    return name or "未命名故事"


def slugify(value: str) -> str:
    original = value.strip()
    value = original.lower()
    known = {
        "自相矛盾": "zixiangmaodun",
        "胡萝卜妖怪": "huluobo-yaoguai",
    }
    if original in known:
        return known[original]
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    if value and (re.search(r"[a-z]", value) or len(value) >= 3):
        return value
    if original:
        digest = hashlib.sha1(original.encode("utf-8")).hexdigest()[:8]
        return f"story-{digest}"
    return "story"


def short_slug(slug: str) -> str:
    parts = [part for part in re.split(r"[-_.]+", slug) if part]
    if len(parts) > 1:
        return "".join(part[0] for part in parts)[:8] or "st"
    return slug[:6] or "story"


def detect_project_assets(project_dir: Path, *, extract_audio: bool = False) -> dict[str, Any]:
    paths = project_paths(project_dir)
    manifest = init_project(paths.root)
    files = [path for path in paths.root.rglob("*") if path.is_file() and STATUS_DIR_NAME not in path.parts]
    input_files = [path for path in files if is_user_input_asset(paths, path)]
    story_text = choose_first(input_files, TEXT_EXTENSIONS, ("原文", "story", "source", "正文", "故事", "文稿"))
    audios = [path for path in input_files if path.suffix.lower() in AUDIO_EXTENSIONS]
    videos = [path for path in input_files if path.suffix.lower() in VIDEO_EXTENSIONS]
    narration = choose_preferred_audio(audios)
    music = choose_preferred_music(audios, narration)
    greenscreen = choose_preferred_video(videos)
    if not story_text:
        existing = manifest["inputs"].get("story_text")
        story_text = Path(existing) if existing else None
    if not narration and greenscreen and extract_audio:
        narration = paths.inputs / f"{manifest['story']['slug']}_extracted_narration.m4a"
        extract_audio_from_video(greenscreen, narration)
        manifest["inputs"]["extracted_narration"] = str(narration)
    if greenscreen:
        reference = paths.release / "person_reference.jpg"
        if not reference.exists():
            extract_reference_frame(greenscreen, reference)
        manifest["outputs"]["person_reference"] = str(reference)
    elif manifest["outputs"].get("person_reference"):
        manifest["outputs"]["person_reference"] = ""
    if story_text:
        manifest["inputs"]["story_text"] = str(story_text)
    if narration:
        manifest["inputs"]["narration"] = str(narration)
    if music:
        manifest["inputs"]["music"] = str(music)
    if greenscreen:
        manifest["inputs"]["greenscreen_video"] = str(greenscreen)
    else:
        existing_greenscreen = manifest["inputs"].get("greenscreen_video", "")
        if existing_greenscreen and not is_user_input_asset(paths, Path(existing_greenscreen)):
            manifest["inputs"]["greenscreen_video"] = ""
    infer_story_fields(manifest, story_text)
    write_manifest(paths, manifest)
    return manifest


def is_user_input_asset(paths: ProjectPaths, path: Path) -> bool:
    generated_dirs = {
        PROJECT_DIRS["images"],
        PROJECT_DIRS["video_jobs"],
        PROJECT_DIRS["assembly"],
        PROJECT_DIRS["release"],
        PROJECT_DIRS["publish"],
        PROJECT_DIRS["product"],
        PROJECT_DIRS["status"],
    }
    try:
        relative_parts = path.relative_to(paths.root).parts
    except ValueError:
        return False
    return not any(part in generated_dirs for part in relative_parts[:-1])


def choose_first(files: list[Path], extensions: set[str], preferred_tokens: tuple[str, ...]) -> Path | None:
    candidates = [path for path in files if path.suffix.lower() in extensions]
    if not candidates:
        return None
    def score(path: Path) -> tuple[int, int, str]:
        name = path.name.lower()
        preferred = 0 if any(token.lower() in name for token in preferred_tokens) else 1
        return preferred, len(path.parts), path.name
    return sorted(candidates, key=score)[0]


def choose_preferred_audio(audios: list[Path]) -> Path | None:
    if not audios:
        return None
    return sorted(audios, key=lambda p: (0 if any(t in p.name.lower() for t in ("原声", "narration", "voice")) else 1, p.name))[0]


def choose_preferred_music(audios: list[Path], narration: Path | None = None) -> Path | None:
    candidates = [path for path in audios if narration is None or path.resolve() != narration.resolve()]
    if not candidates:
        return None
    preferred_tokens = ("背景音乐", "配乐", "bgm", "music", "soundtrack", "score")
    preferred = [path for path in candidates if any(token in path.name.lower() for token in preferred_tokens)]
    if preferred:
        return sorted(preferred, key=lambda p: p.name)[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def choose_preferred_video(videos: list[Path]) -> Path | None:
    if not videos:
        return None
    tokens = ("绿幕", "greenscreen", "green", "key", "美颜", "调色")
    return sorted(videos, key=lambda p: (0 if any(t in p.name.lower() for t in tokens) else 1, p.name))[0]


def extract_audio_from_video(video: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video), "-vn", "-c:a", "aac", "-b:a", "192k", str(output)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def extract_reference_frame(video: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    duration = safe_duration(video)
    timestamp = max(0.1, min(duration * 0.28, max(duration - 0.1, 0.1)))
    subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{timestamp:.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(output)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def safe_duration(path: Path) -> float:
    try:
        return probe_duration(path)
    except Exception:
        return 1.0


def infer_story_fields(manifest: dict[str, Any], story_text: Path | None) -> None:
    text = ""
    if story_text and story_text.exists() and story_text.suffix.lower() in {".txt", ".md"}:
        text = story_text.read_text(encoding="utf-8-sig", errors="ignore")[:4000]
    manual = manifest["story"].setdefault("manual_overrides", {})
    if not manual.get("story_type"):
        manifest["story"]["story_type"] = infer_story_type(text, manifest["story"].get("story_type", "童话故事"))
    if not manual.get("age_range"):
        manifest["story"]["age_range"] = infer_age_range(text)


def infer_story_type(text: str, default: str) -> str:
    if any(token in text for token in ("成语", "矛盾", "画蛇添足", "刻舟求剑", "亡羊补牢")):
        return "成语故事"
    if any(token in text for token in ("寓言", "狐狸", "乌鸦", "农夫")):
        return "寓言故事"
    if any(token in text for token in ("很久以前", "村子", "传说", "民间")):
        return "民间故事"
    return default or "童话故事"


def infer_age_range(text: str) -> str:
    if any(token in text for token in ("战争", "牺牲", "历史", "将军", "英雄")):
        return "12-14岁"
    if len(text) > 1200 or any(token in text for token in ("成语", "道理", "智慧")):
        return "6-8岁"
    return "3-5岁"


def update_story_info(
    project_dir: Path,
    *,
    story_name: str | None = None,
    slug: str | None = None,
    episode: int | None = None,
    story_type: str | None = None,
    image_style: str | None = None,
    age_range: str | None = None,
    duration_text: str | None = None,
    update_latest_episode_on_delivery: bool | None = None,
    does_not_count_episode: bool | None = None,
) -> dict[str, Any]:
    paths = project_paths(project_dir)
    manifest = init_project(paths.root)
    manual = manifest["story"].setdefault("manual_overrides", {})
    for key, value in (
        ("name", story_name),
        ("slug", slug),
        ("episode", episode),
        ("story_type", story_type),
        ("image_style", image_style),
        ("age_range", age_range),
        ("duration_text", duration_text),
    ):
        if value is not None and str(value).strip():
            manifest["story"][key] = value
            manual[key] = True
            if key == "slug":
                manifest["story"]["short_slug"] = short_slug(str(value))
    if update_latest_episode_on_delivery is not None:
        manifest["story"]["update_latest_episode_on_delivery"] = update_latest_episode_on_delivery
    if does_not_count_episode is not None:
        manifest["story"]["does_not_count_episode"] = does_not_count_episode
    write_manifest(paths, manifest)
    return manifest


MANUAL_OUTPUT_DEFAULTS = {
    "background_video_no_sub": "",
    "background_video_with_sub": "",
    "demo_voice_bgm": "",
    "main_release_video": "",
    "library_release_video": "",
    "a_scene_no_person_video": "",
    "annotation_file": "",
    "ppt_file": "",
}


def ensure_semi_auto_manifest(manifest: dict[str, Any]) -> None:
    manual_outputs = manifest.setdefault("manual_outputs", {})
    for key, value in MANUAL_OUTPUT_DEFAULTS.items():
        manual_outputs.setdefault(key, value)
    semi_auto = manifest.setdefault("semi_auto", {})
    semi_auto.setdefault("mode", "manual_first")
    semi_auto.setdefault("current_stage", "")
    semi_auto.setdefault("parallel_tasks", {})


def register_manual_output(project_dir: Path, kind: str, source: Path) -> dict[str, Any]:
    if kind not in MANUAL_OUTPUT_DEFAULTS:
        raise ValueError(f"不支持的手工产物类型：{kind}")
    paths = project_paths(project_dir)
    manifest = init_project(paths.root)
    ensure_semi_auto_manifest(manifest)
    source = source.expanduser()
    if not source.exists():
        raise FileNotFoundError(f"手工产物不存在：{source}")
    saved = source
    try:
        source.resolve().relative_to(paths.root.resolve())
    except ValueError:
        saved = copy_manual_output_to_project(paths, kind, source)
    manifest["manual_outputs"][kind] = str(saved)
    manifest.setdefault("outputs", {})[kind] = str(saved)
    manifest["semi_auto"]["current_stage"] = infer_semi_auto_stage(manifest)
    write_manifest(paths, manifest)
    return manifest


def copy_manual_output_to_project(paths: ProjectPaths, kind: str, source: Path) -> Path:
    target_dirs = {
        "background_video_no_sub": paths.assembly,
        "background_video_with_sub": paths.assembly,
        "demo_voice_bgm": paths.assembly,
        "main_release_video": paths.release,
        "library_release_video": paths.release,
        "a_scene_no_person_video": paths.product / "_manual",
        "annotation_file": paths.product / "_manual",
        "ppt_file": paths.product / "_manual",
    }
    target_dir = target_dirs[kind]
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    if target.exists() and target.resolve() != source.resolve():
        target = target_dir / f"{source.stem}_{time.strftime('%Y%m%d_%H%M%S')}{source.suffix}"
    shutil.copy2(source, target)
    return target


def infer_semi_auto_stage(manifest: dict[str, Any]) -> str:
    outputs = manifest.get("outputs", {})
    manual = manifest.get("manual_outputs", {})
    inputs = manifest.get("inputs", {})
    if not inputs.get("story_text") or not inputs.get("narration"):
        return "素材准备"
    if not outputs.get("jobs_csv"):
        return "分镜出图"
    if not outputs.get("background_video_no_sub") and not manual.get("background_video_no_sub"):
        return "手工背景成片"
    if not outputs.get("main_release_video") and not manual.get("main_release_video"):
        return "发布视频"
    if not outputs.get("publish_package"):
        return "发布物料"
    return "资料包"


def semi_auto_status(project_dir: Path) -> Path:
    paths = project_paths(project_dir)
    manifest = refresh_project_outputs(paths.root)
    ensure_semi_auto_manifest(manifest)
    manifest["semi_auto"]["current_stage"] = infer_semi_auto_stage(manifest)
    write_manifest(paths, manifest)
    report = paths.status / "semi_auto_status.md"
    manual = manifest.get("manual_outputs", {})
    outputs = manifest.get("outputs", {})

    def mark(key: str) -> str:
        value = outputs.get(key) or manual.get(key) or ""
        if value and Path(value).expanduser().exists():
            return f"已找到：`{value}`"
        return "未找到"

    lines = [
        "# 半自动状态",
        "",
        f"- 项目目录：`{paths.root}`",
        f"- 当前阶段：{manifest['semi_auto']['current_stage']}",
        f"- 更新时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 手工产物",
    ]
    for key in MANUAL_OUTPUT_DEFAULTS:
        lines.append(f"- {key}：{mark(key)}")
    lines.extend(
        [
            "",
            "## 并行提醒",
            "- 出图或图生视频等待中，可先做配乐、发布视觉素材、达芬奇抠像准备。",
            "- 背景成片导出后，回工作台登记背景三版和发布视频。",
            "- 资料包前请登记外部朗读标注、A镜无人版和 PPT/图片素材。",
        ]
    )
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest.setdefault("qa", {})["semi_auto_status"] = str(report)
    write_manifest(paths, manifest)
    return report


def qa_manual_outputs(project_dir: Path) -> Path:
    paths = project_paths(project_dir)
    manifest = refresh_project_outputs(paths.root)
    ensure_semi_auto_manifest(manifest)
    manual = manifest.get("manual_outputs", {})
    outputs = manifest.get("outputs", {})
    required = (
        "background_video_no_sub",
        "background_video_with_sub",
        "demo_voice_bgm",
        "main_release_video",
        "library_release_video",
        "a_scene_no_person_video",
        "annotation_file",
        "ppt_file",
    )
    rows: list[dict[str, str]] = []
    issues: list[str] = []
    for key in required:
        value = manual.get(key) or outputs.get(key) or ""
        path = Path(value).expanduser() if value else None
        status = "ok" if path and path.exists() else "missing"
        rows.append({"section": "手工产物", "item": key, "status": status, "detail": str(path) if path else "未登记"})
        if status != "ok":
            issues.append(f"- {key}：未登记或文件不存在")
    report = paths.status / "qa_manual_outputs_report.md"
    table = ["| 分类 | 项目 | 状态 | 详情 |", "| --- | --- | --- | --- |"]
    for row in rows:
        table.append(f"| {row['section']} | {row['item']} | {row['status']} | `{row['detail']}` |")
    body = [
        "# 手工产物检查",
        "",
        f"- 项目目录：`{paths.root}`",
        f"- 检查时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 结论",
        "手工产物已登记完整。" if not issues else "\n".join(issues),
        "",
        "## 明细",
        "\n".join(table),
        "",
    ]
    report.write_text("\n".join(body), encoding="utf-8")
    manifest.setdefault("qa", {})["manual_outputs"] = str(report)
    write_manifest(paths, manifest)
    return report


def qa_images(project_dir: Path, image_dir: Path | None = None) -> Path:
    paths = project_paths(project_dir)
    manifest = init_project(paths.root)
    slug = manifest["story"]["slug"]
    target = image_dir.expanduser() if image_dir else paths.video_jobs / "images"
    if not target.exists():
        target = paths.images
    rows: list[dict[str, str]] = []
    issues: list[str] = []
    images = []
    if target.exists():
        images = sorted([p for p in target.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS])
    for index, path in enumerate(images, start=1):
        try:
            with Image.open(path) as image:
                width, height = image.size
                ratio = width / max(1, height)
                issue = []
                if abs(ratio - 16 / 9) > 0.08:
                    issue.append("比例不是16:9")
                if image.mode in {"RGBA", "LA"}:
                    extrema = image.getextrema()
                    if extrema and isinstance(extrema[-1], tuple) and extrema[-1][0] < 255:
                        issue.append("含透明通道")
                if re.search(r"text|caption|watermark|logo|字幕|水印", path.stem, re.I):
                    issue.append("文件名提示可能含文字/水印")
                status = "warning" if issue else "ok"
                rows.append({"index": str(index), "file": str(path), "size": f"{width}x{height}", "status": status, "notes": "；".join(issue)})
                if issue:
                    issues.append(f"- {path.name}：{'；'.join(issue)}")
        except Exception as exc:
            rows.append({"index": str(index), "file": str(path), "size": "", "status": "error", "notes": str(exc)})
            issues.append(f"- {path.name}：无法读取图片 {exc}")
    report = paths.status / "qa_images_report.md"
    write_qa_report(report, "图片机器审查", target, rows, issues, expected="16:9、稳定命名、无文字水印风险")
    manifest["qa"]["images"] = str(report)
    write_manifest(paths, manifest)
    return report


def qa_videos(project_dir: Path, videos_dir: Path | None = None) -> Path:
    paths = project_paths(project_dir)
    manifest = init_project(paths.root)
    target = videos_dir.expanduser() if videos_dir else paths.video_jobs / "videos"
    rows: list[dict[str, str]] = []
    issues: list[str] = []
    videos = sorted([p for p in target.iterdir() if p.suffix.lower() in VIDEO_EXTENSIONS]) if target.exists() else []
    frames_dir = paths.status / "video_review_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for index, path in enumerate(videos, start=1):
        duration = safe_duration(path)
        frame_paths = extract_video_review_frames(path, frames_dir / path.stem, duration)
        issue = []
        if duration < 1.0:
            issue.append("时长过短")
        if any(is_dark_frame(frame) for frame in frame_paths):
            issue.append("抽帧疑似黑屏")
        rows.append({"index": str(index), "file": str(path), "duration": f"{duration:.2f}s", "status": "warning" if issue else "ok", "notes": "；".join(issue)})
        if issue:
            issues.append(f"- {path.name}：{'；'.join(issue)}")
    report = paths.status / "qa_videos_report.md"
    write_qa_report(report, "视频片段机器审查", target, rows, issues, expected="时长正常、抽帧非黑屏、无明显坏片")
    manifest["qa"]["videos"] = str(report)
    write_manifest(paths, manifest)
    return report


def qa_music(project_dir: Path, music: Path, narration: Path, plan_csv: Path | None = None) -> Path:
    """Run a reproducible audio gate before final assembly.

    The JSON report carries hashes of every checked input so replacing an audio
    file after QA invalidates the stage instead of silently reusing a stale pass.
    """
    paths = project_paths(project_dir)
    manifest = init_project(paths.root)
    music = music.expanduser()
    narration = narration.expanduser()
    plan = plan_csv.expanduser() if plan_csv else None
    if not music.exists() or not narration.exists():
        missing = music if not music.exists() else narration
        raise FileNotFoundError(f"音乐 QA 缺少输入：{missing}")

    music_duration = safe_duration(music)
    narration_duration = safe_duration(narration)
    process = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(music),
            "-af",
            "volumedetect,silencedetect=n=-45dB:d=2",
            "-f",
            "null",
            "-",
        ],
        text=True,
        capture_output=True,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "ffmpeg 无法分析配乐")
    stderr = process.stderr

    def _db(label: str) -> float | None:
        match = re.search(rf"{re.escape(label)}:\s*(-?(?:inf|\d+(?:\.\d+)?))\s*dB", stderr, re.IGNORECASE)
        if not match or match.group(1).lower() == "-inf":
            return None
        return float(match.group(1))

    silence_durations = [float(value) for value in re.findall(r"silence_duration:\s*([0-9.]+)", stderr)]
    mean_volume = _db("mean_volume")
    max_volume = _db("max_volume")
    longest_silence = max(silence_durations, default=0.0)
    silence_total = sum(silence_durations)
    issues: list[dict[str, str]] = []

    def add_issue(code: str, severity: str, message: str) -> None:
        issues.append({"code": code, "severity": severity, "message": message})

    if music_duration <= 0:
        add_issue("invalid_duration", "critical", "配乐时长无法读取")
    if narration_duration > 0 and music_duration + 0.75 < narration_duration:
        add_issue("coverage_short", "critical", f"配乐比旁白短 {narration_duration - music_duration:.2f} 秒")
    if mean_volume is None or mean_volume < -40:
        add_issue("too_quiet", "critical", "配乐整体静音或平均响度过低")
    elif mean_volume > -8:
        add_issue("too_loud", "warning", f"配乐平均响度偏高：{mean_volume:.2f} dB")
    if max_volume is not None and max_volume >= -0.01:
        add_issue("possible_clipping", "critical", f"峰值达到 {max_volume:.2f} dB，疑似削波")
    if longest_silence > 8 or silence_total > max(8.0, music_duration * 0.18):
        add_issue("long_silence", "critical", f"长静音最长 {longest_silence:.2f} 秒，累计 {silence_total:.2f} 秒")

    segment_count = 0
    plan_duration = 0.0
    if plan is not None and plan.exists():
        with plan.open(encoding="utf-8-sig", newline="") as file:
            rows = [row for row in csv.DictReader(file) if (row.get("segment") or "").strip()]
        segment_count = len(rows)
        try:
            plan_duration = sum(float(row.get("duration_sec") or 0) for row in rows)
        except ValueError:
            add_issue("invalid_plan", "critical", "音乐分段表包含无效时长")
        if not rows:
            add_issue("empty_plan", "critical", "音乐分段表为空")
        elif abs(plan_duration - music_duration) > max(1.0, music_duration * 0.03):
            add_issue("plan_duration_mismatch", "critical", f"分段计划 {plan_duration:.2f} 秒与成品 {music_duration:.2f} 秒不一致")

    artifacts = {"music": {"path": str(music), "sha256": sha256_file(music)}}
    artifacts["narration"] = {"path": str(narration), "sha256": sha256_file(narration)}
    if plan is not None and plan.exists():
        artifacts["plan"] = {"path": str(plan), "sha256": sha256_file(plan)}
    passed = not any(issue["severity"] == "critical" for issue in issues)
    payload: dict[str, Any] = {
        "version": 1,
        "passed": passed,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "artifacts": artifacts,
        "metrics": {
            "music_duration_sec": round(music_duration, 3),
            "narration_duration_sec": round(narration_duration, 3),
            "mean_volume_db": mean_volume,
            "max_volume_db": max_volume,
            "longest_silence_sec": round(longest_silence, 3),
            "total_long_silence_sec": round(silence_total, 3),
            "segment_count": segment_count,
            "plan_duration_sec": round(plan_duration, 3),
        },
        "issues": issues,
    }
    report_json = paths.status / "qa_music_report.json"
    save_json(report_json, payload)
    report_md = paths.status / "qa_music_report.md"
    lines = [
        "# 配乐机器 QA",
        "",
        f"- 结论：{'通过' if passed else '不通过'}",
        f"- 配乐/旁白时长：{music_duration:.2f}s / {narration_duration:.2f}s",
        f"- 平均/峰值响度：{mean_volume if mean_volume is not None else '静音'} dB / {max_volume if max_volume is not None else '未知'} dB",
        f"- 长静音：最长 {longest_silence:.2f}s，累计 {silence_total:.2f}s",
        f"- 分段：{segment_count} 段，计划 {plan_duration:.2f}s",
        "",
        "## 问题",
        *(f"- [{item['severity']}] {item['message']}" for item in issues),
    ]
    if not issues:
        lines.append("- 未发现阻断问题")
    report_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest["qa"]["music"] = str(report_json)
    write_manifest(paths, manifest)
    return report_json


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_video_review_frames(video: Path, output_dir: Path, duration: float) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    frames: list[Path] = []
    for label, ratio in (("start", 0.0), ("q1", 0.25), ("mid", 0.5), ("q3", 0.75), ("end", 1.0)):
        frame = output_dir / f"{label}.jpg"
        ts = max(0.05, min(duration * ratio, max(duration - 0.1, 0.05)))
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{ts:.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "3", str(frame)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            frames.append(frame)
        except Exception:
            pass
    return frames


def is_dark_frame(path: Path) -> bool:
    try:
        with Image.open(path).convert("L") as image:
            stat = ImageStat.Stat(image.resize((64, 36)))
            return bool(stat.mean and stat.mean[0] < 12)
    except Exception:
        return False


def write_qa_report(report: Path, title: str, target: Path, rows: list[dict[str, str]], issues: list[str], expected: str) -> None:
    report.parent.mkdir(parents=True, exist_ok=True)
    table = ["| 序号 | 文件 | 状态 | 备注 |", "| --- | --- | --- | --- |"]
    for row in rows:
        table.append(f"| {row.get('index', '')} | `{Path(row.get('file', '')).name}` | {row.get('status', '')} | {row.get('notes', '') or row.get('duration', '')} |")
    body = [
        f"# {title}",
        "",
        f"- 目标目录：`{target}`",
        f"- 期望：{expected}",
        f"- 检查时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 结论",
        "未发现明显问题。" if not issues else "\n".join(issues),
        "",
        "## 明细",
        "\n".join(table) if rows else "未找到可检查文件。",
        "",
    ]
    report.write_text("\n".join(body), encoding="utf-8")


def generate_theme_assets(project_dir: Path, *, overwrite: bool = True) -> dict[str, Path]:
    raise RuntimeError(
        "第 12 步不再用本地 Pillow 从零生成主题素材。"
        "请使用 create_theme_asset_request() 生成 Codex/imagegen 任务书，"
        "由 Codex/imagegen 生成图片后，再用 qa_theme_assets() 验收。"
    )


def create_theme_asset_request(project_dir: Path) -> dict[str, Path]:
    paths = project_paths(project_dir)
    manifest = detect_project_assets(paths.root, extract_audio=False)
    story = manifest["story"]
    assets_dir = paths.release / "theme_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    config = load_config()
    theme = infer_theme_text(manifest)
    duration_text = story.get("duration_text") or "待定"
    request_path = assets_dir / "theme_assets_imagegen_request.md"
    handoff_path = assets_dir / "theme_assets_codex_handoff.txt"
    output_paths = {
        "main_plate": assets_dir / "main_release_plate.png",
        "main_plate_top": assets_dir / "main_release_plate_top.png",
        "main_plate_bottom": assets_dir / "main_release_plate_bottom.png",
        "library_plate": assets_dir / "library_release_plate.png",
        "library_plate_top": assets_dir / "library_release_plate_top.png",
        "library_plate_bottom": assets_dir / "library_release_plate_bottom.png",
        "main_bg": assets_dir / "main_background_16x9.png",
        "frame_source": assets_dir / "story_frame_source.png",
        "frame_a": assets_dir / "story_frame_a.png",
    }
    request = build_theme_asset_imagegen_request(
        manifest=manifest,
        output_paths=output_paths,
        theme=theme,
        duration_text=duration_text,
        config=config,
    )
    request_path.write_text(request, encoding="utf-8")
    handoff = build_theme_asset_handoff(request_path)
    handoff_path.write_text(handoff, encoding="utf-8")
    manifest["outputs"]["theme_assets_prompt"] = str(request_path)
    for output_key, asset_key in (
        ("release_plate_image", "main_plate"),
        ("main_background_image", "main_bg"),
        ("story_frame_source", "frame_source"),
        ("story_frame_a", "frame_a"),
    ):
        path = output_paths[asset_key]
        if path.exists() or not manifest["outputs"].get(output_key):
            manifest["outputs"][output_key] = str(path)
    manifest["outputs"]["library_release_plate_image"] = str(output_paths["library_plate"])
    write_manifest(paths, manifest)
    return {"request": request_path, "handoff": handoff_path, **output_paths}


def build_theme_asset_handoff(request_path: Path) -> str:
    return (
        "请执行第 12 步发布视觉定版，不要只总结任务书：\n"
        f"{request_path}\n\n"
        "这份 md 是工作台自动填入项目信息、尺寸和保存路径的规格书，不是最终图像提示词。"
        "请你先读取它，再根据故事主题、参考素材和规则，智能扩写成适合 Codex 原生图像生成能力的具体图像提示词。"
        "生成主题素材后，继续完成绿幕抠像、融合预览、看图调参，并把最终参数写回 keying_preset.json。\n\n"
        "执行要求：\n"
        "1. 使用当前 Codex 对话/API 的原生图像生成能力，生成/编辑两张 3:4 发布底板、一张 16:9 无框主账号背景、一个统一故事框源图。\n"
        "2. 不要调用旧的 CLI fallback、scripts/image_gen.py、OPENAI_API_KEY、XAI_API_KEY 或任何外部旧图像 API。\n"
        "3. 生成图像要有高质感儿童节目包装效果，不要用本地代码从零画占位图。\n"
        "4. 两张发布底板必须由 Codex 原生图像生成直接包含任务书列出的全部可见文字信息，"
        "不能先生成空白底板再用 Pillow 或其他本地代码后期添加文字；无文字底板视为失败。\n"
        "5. 底板必须拆成上半包装图和下半包装图分别生成，中间由程序保留固定 16:9 空挡，"
        "不要让 imagegen 直接生成整张 1080x1440 底板。上/下素材里的文字仍必须由 Codex 原生生成。\n"
        "6. 允许用 Pillow 只做后处理：裁切、三段拼接、尺寸整理、透明通道和 QA；不允许用 Pillow 添加、覆盖或修正底板文字。\n"
        "7. 故事框只生成一个统一源图和一个透明 PNG；A/B 景复用同一个框，具体缩放与摆放放到发布视频合成环节处理，"
        "不要在第 12 步生成两套故事框或机械裁坏 B 框。\n"
        "8. 最终文件必须保存到任务书指定的绝对路径，文件名完全一致。\n"
        "9. 主题素材通过 QA 后，运行 release-layout-handoff 或等效命令生成候选融合预览，自己读取预览图判断人物大小、位置、抠像边缘、故事框、字幕、Logo 和背景虚化；必须检查开头或手势动作帧，避免只看站定帧导致手部被裁切。\n"
        "10. 把最终布局和抠像参数写回桌面故事项目的 04_发布视频/keying/keying_preset.json，再运行 preview-release-project 生成最终 preview_contact_sheet.png 给用户确认。\n"
        "11. 只处理发布视觉定版，不要改分镜、图生视频、配乐、背景成片，也不要编码完整发布视频；完整视频等用户确认后再点工作台 ⑭。"
    )


def build_theme_asset_imagegen_request(
    *,
    manifest: dict[str, Any],
    output_paths: dict[str, Path],
    theme: str,
    duration_text: str,
    config: dict[str, Any],
) -> str:
    story = manifest["story"]
    story_name = story.get("name", "")
    story_type = story.get("story_type", "儿童故事")
    age_range = story.get("age_range", "")
    brand = config.get("brand_assets", {})
    release_defaults = config.get("release_defaults", {})
    assets_dir = brand.get("assets_dir", "")
    logo = brand.get("logo", "")
    frame_reference = brand.get("frame_reference", "")
    cover_reference = brand.get("cover_reference", "")
    video_box = release_defaults.get("video_box", "0,416,1080,608")
    story_box = release_defaults.get("story_box", "210,270,910,512")
    b_story_box = release_defaults.get("b_story_box", "356,180,1209,680")
    return f"""# 《{story_name}》发布素材 Codex/imagegen 任务

## 工作边界

只做发布视频第 12 步素材：两张 3:4 发布底板、主账号 16:9 无框背景图、统一视觉设计的故事框。不要重做分镜、图片、图生视频、配乐、背景成片。

请使用 imagegen 生成或编辑位图素材。生成完成后，把最终文件保存到下面这些绝对路径，文件名必须完全一致：

```text
主账号最终 3:4 发布底板：{output_paths['main_plate']}
主账号顶部源图 1080x416：{output_paths['main_plate_top']}
主账号底部源图 1080x416：{output_paths['main_plate_bottom']}
宝库号最终 3:4 发布底板：{output_paths['library_plate']}
宝库号顶部源图 1080x416：{output_paths['library_plate_top']}
宝库号底部源图 1080x416：{output_paths['library_plate_bottom']}
主账号 16:9 无框背景图：{output_paths['main_bg']}
统一故事框源图：{output_paths['frame_source']}
统一透明故事框：{output_paths['frame_a']}
```

## 本期信息

- 故事名称：《{story_name}》
- 故事类型：{story_type}
- 适龄段：{age_range}
- 时长文案：{duration_text}
- 主题元素：{theme}
- 常用素材目录：{assets_dir}
- Logo：{logo}
- 边框参考：{frame_reference}
- 封面/底板参考：{cover_reference}

## 总体视觉要求

- 参考“绵羊姐姐讲故事”的高质感儿童节目包装：亲和、明亮、精致、有主题感，不要做成简单 UI 占位图。
- 视觉元素不要套固定模板，也不要每期重复使用同一批装饰。请根据本期故事内容、故事类型、情绪和目标年龄自行判断画面语言，允许自由发挥；但不能杂乱，不能遮挡真实视频区域。
- 两张 3:4 发布底板必须在 Codex 原生生图结果中直接包含下面“可见内容”列出的文字；不要生成空白牌匾/面板后再用本地代码补字。
- 所有文字必须尽量准确；如果原生生图文字严重错误，重新生成，不要用 Pillow 或其他本地代码后期修字。
- 底板整体要清爽、直接、信息优先。主账号尤其要简洁，宝库号可以保留更丰富的商品资料包包装感；两者都不要把装饰、徽章、花纹、角色和道具堆满画面。
- 中间 16:9 视频安全区必须刚好处在 3:4 竖屏画布正中间：x=0, y=416, w=1080, h=608；上方和下方可视包装区各 416px，高度和面积完全相等。
- 主账号顶部只放故事类型和故事标题，不放时长、适龄段、品牌字或资料包信息；风格按故事类型轻量适配，例如历史故事可偏典雅，儿童童话可更可爱，但只做简单点缀。
- 主账号底部只放完整版时长、适合年龄和固定适用说明，避免商品资料清单式堆叠。
- 底板不得出现绵羊姐姐、羊头、小羊、卡通羊、人偶或任何人物/动物吉祥物形象；除非故事本身需要，避免无关角色或动物进入发布包装。
- 底板必须拆成顶部源图和底部源图分别生成，再由程序夹入固定 16:9 视频空挡。不要让 imagegen 直接生成完整 1080x1440 底板，因为它容易把安全区画错。Pillow 只负责拼接、裁切和尺寸整理，不负责生成或修正文案。
- 不要添加二维码、平台 UI、播放按钮、陌生 logo、水印。
- 宝库号资料包对外只表达 6 项内容：背景视频、PPT、配乐、文稿、示范视频、朗读标注。不要写“发布物料”，不要再写单独的“标注”。
- “联系私信客服，好作品有偿分享”只出现在发布视频结尾模糊提示里，不写进底板。

## 1. 主账号 3:4 发布底板

最终交付一张 1080x1440 的竖屏底板，用于承载“主账号 16:9 横版节目画面”。但不要直接生成整张底板，必须按下面三段式制作。

可见内容：
- 故事类型：{story_type}
- 主标题：《{story_name}》
- 底部信息栏：完整版时长：{duration_text}
- 底部信息栏：适合年龄：{age_range or '按本期设定'}
- 底部固定说明：适用于朗诵比赛、故事表演、少儿口才、技能比拼

硬性版式：
- 顶部源图：1080x416，保存到 `{output_paths['main_plate_top']}`。只呈现“{story_type}”和“《{story_name}》”，标题是主视觉，故事类型作为小标题或牌匾；不要放时长、适龄段、资料清单、品牌字或联系方式。
- 中间视频安全区：1080x608，位置是 x=0, y=416, w=1080, h=608，也就是 y=416 到 y=1024。这里不是创作区，最终拼接时填纯色空白，不要使用 imagegen 生成内容。
- 底部源图：1080x416，保存到 `{output_paths['main_plate_bottom']}`。做一个简洁信息栏，清楚写“完整版时长：{duration_text}”和“适合年龄：{age_range or '按本期设定'}”；信息栏下方写“适用于朗诵比赛、故事表演、少儿口才、技能比拼”。只允许少量主题点缀。
- 最终整图：把 1080x416 顶部源图 + 1080x608 纯色空白 + 1080x416 底部源图垂直拼接，保存到 `{output_paths['main_plate']}`。最终中间安全区必须一整条横向打穿，没有人物、装饰、边框、插画或文字。

## 2. 宝库号 3:4 发布底板

最终交付一张 1080x1440 的竖屏底板，用于承载“有人声、有字幕、有配乐的背景成片”。但不要直接生成整张底板，必须按同样三段式制作。

可见内容：
- 主标题：《{story_name}》
- 顶部时长：{duration_text}
- 底部适龄：适合年龄 {age_range or '按本期设定'}
- 底部资料包信息两行：背景视频 + PPT + 配乐；文稿 + 示范视频 + 朗读标注

硬性版式：
- 顶部源图：1080x416，保存到 `{output_paths['library_plate_top']}`。保留相对丰富的商品包装感，可放故事标题、故事类型和时长，排版可比主账号更热闹，但不要压入中间安全区。
- 中间视频安全区：1080x608，位置是 x=0, y=416, w=1080, h=608，也就是 y=416 到 y=1024。最终拼接时填纯色空白。
- 底部源图：1080x416，保存到 `{output_paths['library_plate_bottom']}`。保留较丰富的资料包信息区，重点写“适合年龄 {age_range or '按本期设定'}”“背景视频 + PPT + 配乐”“文稿 + 示范视频 + 朗读标注”；“示范视频”要排在“朗读标注”前面。
- 最终整图保存到 `{output_paths['library_plate']}`，中间安全区必须干净留空。

## 3. 主账号 16:9 无框背景图

生成 1920x1080 背景图，用于放置左侧故事视频框和右侧真人绿幕人物。背景图本身只提供环境氛围，不能自带故事框。

要求：
- 背景与本期故事主题强相关，必须开阔、连续、有明确场景感，整体漂亮但低复杂度，不能喧宾夺主。
- 背景必须是同一个横向展开的完整场景，不允许左右分区、半边纸面、半边色块、明显留白面板、舞台幕布感或“半屏设计”。
- 控制细节密度，避免拥挤纹样、密集道具、强装饰、复杂建筑群、标题栏或可被误认为视频窗口的矩形容器。
- 右侧要给真人留出干净空间，左侧要给故事框留出空间；但只能通过浅景深、低细节、低对比、开阔景别和自然光雾来实现，不能做成独立面板或空白块。
- 不要出现真实人物、字幕、水印、平台 UI。
- 不要画任何故事视频框、边框、牌匾、标题栏、空白卡片或可被误认为视频窗口的矩形容器；故事框会由后期透明 PNG 叠加。

## 4. 统一故事框源图

生成一张 1920x1080 的统一故事框源图，供后处理导出一个透明故事框。A/B 景复用同一个框，具体缩放与摆放放到发布视频合成环节处理。

要求：
- 参考边框素材 `{frame_reference}` 的比例和风格，做成更贴合本期故事的定制框。
- 框外与框内开口都通过同一种纯色抠图底（建议亮洋红）去底，后处理不得再额外裁出一个矩形透明洞。
- 合成逻辑是“故事视频在下，透明故事框在上”，由不规则框体自然遮挡视频边缘；不要把内缘硬切成直角矩形。
- 故事框必须是完整闭合的四边矩形框：上、下、左、右四条边和四个角都要完整在画布内，不能生成 L 形、缺左边、缺底边、局部出画或被裁断的框。
- 框体必须围绕 A 景窗口 `{story_box}`，窗口四周都应能看到框体；不要把右边框放到远离窗口的位置。
- 装饰元素围绕窗口，不能遮挡窗口。
- 保留 Logo 安全区，后续会叠加 `{logo}`。
- 源图按 A 景窗口构图（参考 `{story_box}`），确保框细节完整可读。

## 5. A/B 使用规则

- 第 12 步只交付一个统一透明故事框。
- A 景窗口参考 `{story_box}`。
- B 景窗口参考 `{b_story_box}`，但 B 景不在主题素材阶段另做一张框；发布视频合成环节复用统一框并调整缩放与摆放。

## 交付检查

保存后请检查：
- 顶部源图都是 1080x416，底部源图都是 1080x416。
- 两张最终底板都是 1080x1440，由 1080x416 顶部 + 1080x608 纯色空白安全区 + 1080x416 底部拼接而成。
- 中间视频安全区 x=0, y=416, w=1080, h=608 没有文字/装饰/人物，且没有被上/下装饰压入；视频区中心点必须等于整张 3:4 画布中心点。
- 背景图是 1920x1080，且不能自带故事框。
- 故事框源图为单一设计；只导出一个 1920x1080 透明 PNG，A/B 在发布视频合成环节复用。
- 文件已经保存到任务书指定路径。
"""


def infer_theme_text(manifest: dict[str, Any]) -> str:
    story_name = manifest["story"].get("name", "")
    story_type = manifest["story"].get("story_type", "")
    text_path = manifest["inputs"].get("story_text", "")
    text = ""
    if text_path and Path(text_path).exists() and Path(text_path).suffix.lower() in {".txt", ".md"}:
        text = Path(text_path).read_text(encoding="utf-8-sig", errors="ignore")[:1200]
    tokens = []
    for word in ("森林", "月亮", "小河", "山", "城堡", "竹简", "古代", "动物", "狐狸", "猴子", "小羊", "矛", "盾", "花园"):
        if word in story_name or word in text:
            tokens.append(word)
    return "、".join(tokens[:8]) or story_type or "儿童故事主题元素"


def qa_theme_assets(paths: ProjectPaths, assets: list[Path] | None = None, *, strict: bool = False) -> Path:
    config = load_config()
    release_defaults = config.get("release_defaults", {})
    video_box = parse_box(str(release_defaults.get("video_box", "0,416,1080,608")))
    story_box = parse_box(str(release_defaults.get("story_box", "210,270,910,512")))
    ensure_story_frame_variants(paths.release / "theme_assets", story_box)
    if assets is None:
        theme_dir = paths.release / "theme_assets"
        assets = [
            theme_dir / "main_release_plate_top.png",
            theme_dir / "main_release_plate_bottom.png",
            theme_dir / "main_release_plate.png",
            theme_dir / "library_release_plate_top.png",
            theme_dir / "library_release_plate_bottom.png",
            theme_dir / "library_release_plate.png",
            theme_dir / "main_background_16x9.png",
            theme_dir / "story_frame_a.png",
        ]
    expected_sizes = {
        "main_release_plate_top.png": (1080, 416),
        "main_release_plate_bottom.png": (1080, 416),
        "main_release_plate.png": (1080, 1440),
        "library_release_plate_top.png": (1080, 416),
        "library_release_plate_bottom.png": (1080, 416),
        "library_release_plate.png": (1080, 1440),
        "release_plate_placeholder.png": (1080, 1440),
        "main_background_16x9.png": (1920, 1080),
        "story_frame_a.png": (1920, 1080),
    }
    expected_windows = {
        "story_frame_a.png": story_box,
    }
    rows = []
    issues = []
    for index, path in enumerate(assets, start=1):
        notes = []
        try:
            with Image.open(path) as image:
                width, height = image.size
                if path.suffix.lower() != ".png":
                    notes.append("不是PNG")
                expected_size = expected_sizes.get(path.name)
                if expected_size is not None and (width, height) != expected_size:
                    notes.append(f"尺寸应为{expected_size[0]}x{expected_size[1]}")
                if path.name in {"main_release_plate.png", "library_release_plate.png", "release_plate_placeholder.png"}:
                    notes.extend(qa_release_plate_image(image, video_box))
                if path.name in expected_windows:
                    notes.extend(qa_story_frame_image(image, expected_windows[path.name]))
                if path.name == "main_background_16x9.png":
                    notes.extend(qa_main_background_image(image))
                rows.append({"index": str(index), "file": str(path), "status": "warning" if notes else "ok", "notes": f"{width}x{height} {'；'.join(notes)}"})
                if notes:
                    issues.append(f"- {path.name}：{'；'.join(notes)}")
        except Exception as exc:
            rows.append({"index": str(index), "file": str(path), "status": "error", "notes": str(exc)})
            issues.append(f"- {path.name}：无法读取")
    report = paths.status / "qa_theme_assets_report.md"
    write_qa_report(report, "主题底板/故事框机器审查", paths.release / "theme_assets", rows, issues, expected="底板3:4、故事框透明PNG、安全区清晰")
    manifest = init_project(paths.root)
    manifest["qa"]["theme_assets"] = str(report)
    write_manifest(paths, manifest)
    if strict and issues:
        raise ValueError("主题素材未通过机器审查：\n" + "\n".join(issues) + f"\n请先重新完成第 12 步素材，再继续发布成片：{report}")
    return report


def ensure_story_frame_variants(theme_dir: Path, a_window: tuple[int, int, int, int]) -> None:
    """Export the one transparent story frame used by both A and B scenes."""
    frame_a = theme_dir / "story_frame_a.png"
    source_candidates = [
        theme_dir / "story_frame_source.png",
        theme_dir / "story_frame_a_source.png",
    ]
    source = next((path for path in source_candidates if path.exists()), None)
    if frame_a.exists():
        if _normalize_existing_story_frame(frame_a, a_window):
            return
        if source is None:
            return
    if source is None:
        return
    if not frame_a.exists():
        export_frame_from_source(source, frame_a, a_window)
    else:
        export_frame_from_source(source, frame_a, a_window)


def _normalize_existing_story_frame(frame_path: Path, window: tuple[int, int, int, int]) -> bool:
    try:
        with Image.open(frame_path).convert("RGBA") as image:
            notes = qa_story_frame_image(image, window)
            if not notes:
                return True
            normalized = fit_frame_to_window(image, window)
            retry_notes = qa_story_frame_image(normalized, window)
            if retry_notes:
                return False
            normalized.save(frame_path)
            return True
    except Exception:
        return False


def export_frame_from_source(source: Path, output: Path, window: tuple[int, int, int, int]) -> None:
    with Image.open(source).convert("RGB") as source_image:
        frame = cover_crop(source_image, (1920, 1080))
    frame_rgba = chroma_to_alpha(frame)
    frame_rgba = fit_frame_to_window(frame_rgba, window)
    output.parent.mkdir(parents=True, exist_ok=True)
    frame_rgba.save(output)


def fit_frame_to_window(frame: Image.Image, window: tuple[int, int, int, int]) -> Image.Image:
    """Align a complete generated frame around the target story window."""
    frame = frame.convert("RGBA")
    bbox = frame.getchannel("A").getbbox()
    if bbox is None:
        return frame
    x, y, width, height = window
    margin_x = 112
    margin_y = 116
    target = (
        max(0, x - margin_x),
        max(0, y - margin_y),
        min(frame.width, x + width + margin_x),
        min(frame.height, y + height + margin_y),
    )
    subject = frame.crop(bbox)
    fitted = subject.resize((target[2] - target[0], target[3] - target[1]), Image.Resampling.LANCZOS)
    out = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    out.alpha_composite(fitted, (target[0], target[1]))
    return out


def export_frame_b_from_a(
    frame_a_path: Path,
    frame_b_path: Path,
    a_window: tuple[int, int, int, int],
    b_window: tuple[int, int, int, int],
) -> None:
    with Image.open(frame_a_path).convert("RGBA") as frame_a:
        ax, ay, aw, ah = a_window
        bx, by, bw, bh = b_window
        scale = min(bw / max(1, aw), bh / max(1, ah))
        scaled = frame_a.resize(
            (max(1, round(frame_a.width * scale)), max(1, round(frame_a.height * scale))),
            Image.Resampling.LANCZOS,
        )
        a_cx = ax + aw / 2
        a_cy = ay + ah / 2
        b_cx = bx + bw / 2
        b_cy = by + bh / 2
        paste_x = round(b_cx - a_cx * scale)
        paste_y = round(b_cy - a_cy * scale)
        frame_b = Image.new("RGBA", frame_a.size, (0, 0, 0, 0))
        frame_b.alpha_composite(scaled, (paste_x, paste_y))
    frame_b.paste(Image.new("RGBA", (bw, bh), (0, 0, 0, 0)), (bx, by))
    frame_b_path.parent.mkdir(parents=True, exist_ok=True)
    frame_b.save(frame_b_path)


def cover_crop(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    src_w, src_h = image.size
    dst_w, dst_h = size
    scale = max(dst_w / max(1, src_w), dst_h / max(1, src_h))
    resized = image.resize((round(src_w * scale), round(src_h * scale)), Image.Resampling.LANCZOS)
    x = max(0, (resized.width - dst_w) // 2)
    y = max(0, (resized.height - dst_h) // 2)
    return resized.crop((x, y, x + dst_w, y + dst_h))


def chroma_to_alpha(image: Image.Image, key: tuple[int, int, int] = (255, 0, 255), threshold: int = 72) -> Image.Image:
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    for y in range(rgba.height):
        for x in range(rgba.width):
            r, g, b, a = pixels[x, y]
            dist = abs(r - key[0]) + abs(g - key[1]) + abs(b - key[2])
            if dist < threshold or (r > 170 and b > 160 and g < 120):
                pixels[x, y] = (r, g, b, 0)
            elif r > 140 and b > 135 and g < 145:
                pixels[x, y] = (r, g, b, max(0, min(a, int((dist - threshold) * 3))))
    return rgba


def parse_box(value: str) -> tuple[int, int, int, int]:
    parts = [part.strip() for part in value.replace("，", ",").split(",")]
    if len(parts) != 4:
        raise ValueError(f"窗口坐标格式应为 x,y,w,h：{value}")
    x, y, width, height = (int(float(part)) for part in parts)
    return x, y, width, height


def qa_release_plate_image(image: Image.Image, video_box: tuple[int, int, int, int]) -> list[str]:
    notes: list[str] = []
    if image.mode not in {"RGB", "RGBA"}:
        notes.append("底板色彩模式异常")
    x, y, width, height = video_box
    expected_y = (image.size[1] - height) // 2
    if x != 0 or width != image.size[0] or y != expected_y:
        notes.append("视频安全区未按竖屏中心居中")
    if image.size[0] < x + width or image.size[1] < y + height:
        notes.append("视频安全区超出底板范围")
        return notes
    strip = image.convert("RGB").crop((x, y, x + width, y + height)).resize((108, 61))
    stat = ImageStat.Stat(strip)
    if max(stat.stddev) > 10:
        notes.append("中间视频安全区不够干净")
    return notes


def qa_story_frame_image(image: Image.Image, window: tuple[int, int, int, int]) -> list[str]:
    notes: list[str] = []
    if image.mode != "RGBA":
        notes.append("故事框不是透明RGBA")
        return notes
    alpha = image.getchannel("A")
    total = image.size[0] * image.size[1]
    opaque_pixels = sum(1 for value in alpha.getdata() if value > 8)
    visible_ratio = opaque_pixels / max(1, total)
    if visible_ratio < 0.01:
        notes.append("故事框几乎没有可见装饰")
    if visible_ratio > 0.38:
        notes.append("故事框可见面积过大，可能带入底色")
    x, y, width, height = window
    if image.size[0] < x + width or image.size[1] < y + height:
        notes.append("故事视频窗口超出画布")
        return notes
    inset_x = round(width * 0.16)
    inset_y = round(height * 0.16)
    safe_inner_alpha = alpha.crop((x + inset_x, y + inset_y, x + width - inset_x, y + height - inset_y))
    if safe_inner_alpha.getbbox() is not None:
        notes.append("故事框中央抠像开口不透明")
    side_checks = {
        "上边框": (x, max(0, y - 120), x + width, y),
        "下边框": (x, y + height, x + width, min(image.size[1], y + height + 120)),
        "左边框": (max(0, x - 120), y, x, y + height),
        "右边框": (x + width, y, min(image.size[0], x + width + 120), y + height),
    }
    for label, box in side_checks.items():
        band = alpha.crop(box)
        pixels = max(1, band.size[0] * band.size[1])
        visible = sum(1 for value in band.getdata() if value > 24)
        if visible / pixels < 0.012:
            notes.append(f"故事框{label}缺失或离窗口过远")
    outer_alpha = Image.new("L", image.size, 0)
    outer_alpha.paste(alpha)
    ImageDraw.Draw(outer_alpha).rectangle((x, y, x + width, y + height), fill=0)
    outer_visible = sum(1 for value in outer_alpha.getdata() if value > 8)
    if outer_visible / max(1, total - width * height) > 0.55:
        notes.append("故事框外圈透明区域不足，可能保留了色底")
    for point in ((0, 0), (image.size[0] - 1, 0), (0, image.size[1] - 1), (image.size[0] - 1, image.size[1] - 1)):
        if alpha.getpixel(point) > 8:
            notes.append("画布边角不是透明")
            break
    return notes


def qa_main_background_image(image: Image.Image) -> list[str]:
    notes: list[str] = []
    if image.mode not in {"RGB", "RGBA"}:
        notes.append("背景图色彩模式异常")
    sample = image.convert("RGB").resize((96, 54))
    stat = ImageStat.Stat(sample)
    if max(stat.stddev) < 5:
        notes.append("背景图过于单调，疑似占位图")
    return notes


def auto_keying(project_dir: Path, greenscreen: Path | None = None) -> Path:
    paths = project_paths(project_dir)
    manifest = detect_project_assets(paths.root)
    raw_video = greenscreen or manifest["inputs"].get("greenscreen_video")
    video = Path(raw_video).expanduser() if raw_video else Path("__missing_greenscreen__")
    if not video.exists():
        raise FileNotFoundError("未找到绿幕视频，无法自动生成抠像参数。")
    output_dir = paths.release / "keying"
    frames_dir = output_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    duration = safe_duration(video)
    frames = []
    for idx, ts in enumerate(keying_sample_timestamps(duration), start=1):
        frame = frames_dir / f"sample_{idx:02d}.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{ts:.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(frame)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        frames.append(frame)
    color = sample_green(frames[len(frames) // 2])
    person_crop = detect_person_crop(frames, color)
    # Default for current horizontal 16:9 green-screen shoots: presenter centered
    # in the source, full body visible, then placed in the right-side open area.
    preset = {
        "keyer": "colorkey",
        "chroma_color": color,
        "chroma_similarity": 0.095,
        "chroma_blend": 0.04,
        "person_crop": person_crop,
        "person_grade": "log-soft",
        "person_beauty": "light",
        "person_height_ratio": 0.9,
        "person_x": 755,
        "person_y": 108,
        "bottom_margin": 0,
        "auto_selected": True,
        "source_video": str(video),
    }
    preset_path = output_dir / "keying_preset.json"
    save_json(preset_path, preset)
    sheet = output_dir / "keying_samples.jpg"
    render_contact_sheet(frames, sheet, "绿幕自动采样帧")
    manifest["outputs"]["keying_preset"] = str(preset_path)
    manifest["qa"]["keying_samples"] = str(sheet)
    write_manifest(paths, manifest)
    return preset_path


def keying_sample_timestamps(duration: float) -> list[float]:
    """Sample the whole presenter video, including early gesture frames."""
    if duration <= 0:
        return [0.5]
    fixed = [0.5, 1.0, 2.0, 4.0]
    ratios = [0.08, 0.16, 0.30, 0.50, 0.70, 0.86]
    values = fixed + [duration * ratio for ratio in ratios]
    clipped = sorted({round(max(0.1, min(value, max(duration - 0.1, 0.1))), 3) for value in values})
    return clipped


def sample_green(frame: Path) -> str:
    with Image.open(frame).convert("RGB") as image:
        width, height = image.size
        samples = []
        for x_ratio in (0.05, 0.15, 0.85, 0.95):
            for y_ratio in (0.10, 0.25, 0.50, 0.75):
                r, g, b = image.getpixel((int(width * x_ratio), int(height * y_ratio)))
                if g > r * 1.2 and g > b * 1.08:
                    samples.append((r, g, b))
        if not samples:
            return "0x00FF00"
        samples.sort(key=lambda rgb: rgb[1])
        r, g, b = samples[len(samples) // 2]
        return f"0x{r:02X}{g:02X}{b:02X}"


def detect_person_crop(frames: list[Path], chroma_color: str) -> list[int] | None:
    """Find a padded union box around the presenter, excluding green wall and studio floor."""
    try:
        green = tuple(int(chroma_color[index : index + 2], 16) for index in (2, 4, 6))
    except Exception:
        green = (0, 255, 0)
    boxes: list[tuple[int, int, int, int]] = []
    for frame in frames:
        with Image.open(frame).convert("RGB") as image:
            width, height = image.size
            floor_y = find_green_wall_floor_y(image, green)
            search_bottom = floor_y or height
            row_step = max(2, height // 900)
            col_step = max(2, width // 600)
            col_counts: dict[int, int] = {}
            row_counts: dict[int, int] = {}
            for y in range(0, search_bottom, row_step):
                for x in range(0, width, col_step):
                    rgb = image.getpixel((x, y))
                    if is_presenter_pixel(rgb, green):
                        col_counts[x] = col_counts.get(x, 0) + 1
                        row_counts[y] = row_counts.get(y, 0) + 1
            min_col_pixels = max(8, round(search_bottom / row_step * 0.018))
            min_row_pixels = max(8, round(width / col_step * 0.035))
            xs = [x for x, count in col_counts.items() if count >= min_col_pixels]
            ys = [y for y, count in row_counts.items() if count >= min_row_pixels]
            if xs and ys:
                boxes.append((min(xs), min(ys), max(xs), max(ys)))
    if not boxes:
        return None
    with Image.open(frames[0]) as image:
        width, height = image.size
    x1 = min(box[0] for box in boxes)
    y1 = min(box[1] for box in boxes)
    x2 = max(box[2] for box in boxes)
    y2 = max(box[3] for box in boxes)
    pad_x = round(width * 0.13)
    pad_top = round(height * 0.035)
    pad_bottom = round(height * 0.018)
    x1 = max(0, x1 - pad_x)
    y1 = max(0, y1 - pad_top)
    x2 = min(width - 1, x2 + pad_x)
    y2 = min(height - 1, y2 + pad_bottom)
    return [x1, y1, x2 - x1 + 1, y2 - y1 + 1]


def find_green_wall_floor_y(image: Image.Image, green: tuple[int, int, int]) -> int | None:
    width, height = image.size
    scan_start = int(height * 0.52)
    step = max(2, height // 900)
    x_step = max(8, width // 240)
    for y in range(height - 1, scan_start, -step):
        row_is_green = 0
        samples = 0
        for x in range(0, width, x_step):
            if is_chroma_like(image.getpixel((x, y)), green):
                row_is_green += 1
            samples += 1
        if samples and row_is_green / samples >= 0.46:
            return min(height, y + 8)
    return None


def is_presenter_pixel(rgb: tuple[int, int, int], green: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    if is_chroma_like(rgb, green):
        return False
    # Keep skin, hair, white clothes, and shadows on the person; reject green spill.
    return not (g > r * 1.05 and g > b * 1.02 and g > 75)


def is_chroma_like(rgb: tuple[int, int, int], green: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    gr, gg, gb = green
    distance = ((r - gr) ** 2 + (g - gg) ** 2 + (b - gb) ** 2) ** 0.5
    return g > r * 1.12 and g > b * 1.04 and distance < 92


def render_contact_sheet(images: list[Path], output: Path, title: str) -> None:
    thumbs = []
    for path in images:
        image = Image.open(path).convert("RGB")
        image.thumbnail((360, 240))
        thumbs.append((path, image.copy()))
    sheet = Image.new("RGB", (1160, 360), (246, 244, 238))
    draw = ImageDraw.Draw(sheet)
    draw.text((24, 18), title, fill=(32, 32, 32), font=load_font(28))
    for index, (path, image) in enumerate(thumbs):
        x = 24 + index * 376
        y = 72
        sheet.paste(image, (x, y))
        draw.text((x, y + 252), path.name, fill=(48, 48, 48), font=load_font(20))
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, quality=92)


def final_delivery(project_dir: Path, *, update_latest_episode: bool = False) -> Path:
    paths = project_paths(project_dir)
    manifest = detect_project_assets(paths.root)
    discover_outputs(paths, manifest)
    for qa_func in (qa_images, qa_videos, qa_release, qa_product, qa_publish):
        try:
            qa_func(paths.root)  # type: ignore[arg-type]
        except Exception as exc:
            manifest.setdefault("qa_warnings", []).append(f"{qa_func.__name__}: {exc}")
    manifest = load_manifest(paths) or manifest
    discover_outputs(paths, manifest)
    report = paths.root / "总交付清单.md"
    story = manifest["story"]
    lines = [
        f"# 《{story.get('name', '')}》总交付清单",
        "",
        f"- 故事名：{story.get('name', '')}",
        f"- 集数：第 {story.get('episode', '')} 集",
        f"- 故事类型：{story.get('story_type', '')}",
        f"- 适龄段：{story.get('age_range', '')}",
        f"- 时长文案：{story.get('duration_text', '') or '未生成'}",
        "",
        "## 成品",
    ]
    for label, key in (
        ("主账号发布视频", "main_release_video"),
        ("宝库号发布视频", "library_release_video"),
        ("发布物料", "publish_package"),
        ("基础版资料包", "product_base"),
        ("进阶版资料包", "product_advanced"),
        ("主题素材", "theme_assets_prompt"),
        ("自动抠像参数", "keying_preset"),
        ("人物参考帧", "person_reference"),
        ("含字幕背景视频", "background_video_with_sub"),
        ("无字幕背景视频", "background_video_no_sub"),
    ):
        value = manifest["outputs"].get(key, "")
        status = "✅" if value and Path(value).exists() else "缺失"
        lines.append(f"- {status} {label}：`{value or '未找到'}`")
    lines.extend(["", "## QA 报告"])
    for label, value in manifest.get("qa", {}).items():
        lines.append(f"- {label}：`{value}`")
    lines.extend(["", "## 建议人工最终查看", "- 主账号发布视频", "- 宝库号发布视频", "- 封面素材", "- 发布物料目录", "- 基础版/进阶版资料包目录"])
    publish_dir = paths.publish
    required_paths = [
        paths.release / "主账号发布视频.mp4",
        paths.release / "宝库号发布视频.mp4",
        paths.assembly / "story_no_subs_bgm.mp4",
        paths.assembly / "story_sales_subs_bgm.mp4",
        *[
            publish_dir / account / "covers" / f"cover_{ratio}.png"
            for account in ("main", "library")
            for ratio in ("3x4", "4x3", "16x9")
        ],
        publish_dir / "main" / "copy.md",
        publish_dir / "library" / "copy.md",
    ]
    story_name = str(manifest.get("story", {}).get("name", ""))
    product_defaults = {
        "product_base": paths.product / f"绵羊故事锦囊：{story_name}（基础版）",
        "product_advanced": paths.product / f"绵羊故事锦囊：{story_name}（进阶版）",
    }
    for key, default_path in product_defaults.items():
        value = manifest.get("outputs", {}).get(key, "")
        required_paths.append(Path(value) if value else default_path)
    missing_delivery = [path for path in required_paths if not path.exists()]
    review_failures: list[str] = []
    if manifest.get("agent", {}).get("job_id"):
        review_paths = {
            "source_edit_review": paths.status / "source_edit" / "source_edit_review.json",
            "story_images_review": paths.status / "reviews" / "story_images_review.json",
            "video_prompt_review": paths.status / "reviews" / "video_prompt_review.json",
            "video_review": paths.status / "reviews" / "video_review.json",
            "release_preview": paths.status / "reviews" / "release_preview_review.json",
            "release_video_review": paths.status / "reviews" / "release_video_review.json",
            "publish_package_review": paths.status / "reviews" / "publish_package_review.json",
            "product_annotation_review": paths.status / "reviews" / "product_annotation_review.json",
            "product_package_review": paths.status / "reviews" / "product_package_review.json",
        }
        if not manifest.get("outputs", {}).get("source_edit_decisions"):
            review_paths.pop("source_edit_review", None)
        for name, review_path in review_paths.items():
            try:
                review = json.loads(review_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                review_failures.append(f"{name}: 缺少有效审核 JSON")
                continue
            if not review.get("approved") or float(review.get("score", 0)) < 85 or review.get("critical_errors"):
                review_failures.append(f"{name}: 未达到 85 分无关键错误门槛")
        music_qa_path = paths.status / "qa_music_report.json"
        try:
            music_qa = json.loads(music_qa_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            review_failures.append("music_qa: 缺少有效配乐 QA JSON")
        else:
            music_artifacts = music_qa.get("artifacts")
            music_current = isinstance(music_artifacts, dict) and bool(music_artifacts)
            if music_current:
                for item in music_artifacts.values():
                    if not isinstance(item, dict):
                        music_current = False
                        break
                    artifact_path = Path(str(item.get("path", "")))
                    if not artifact_path.is_file() or item.get("sha256") != sha256_file(artifact_path):
                        music_current = False
                        break
            if not music_qa.get("passed") or not music_current:
                review_failures.append("music_qa: 未通过或输入音频哈希已变化")
        release_qa_path = paths.status / "qa_release_report.json"
        try:
            release_qa = json.loads(release_qa_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            review_failures.append("release_qa: 缺少有效终片音画同步 QA JSON")
        else:
            release_artifacts = release_qa.get("artifacts")
            release_current = isinstance(release_artifacts, dict) and bool(release_artifacts)
            if release_current:
                for item in release_artifacts.values():
                    if not isinstance(item, dict):
                        release_current = False
                        break
                    artifact_path = Path(str(item.get("path", "")))
                    if not artifact_path.is_file() or item.get("sha256") != sha256_file(artifact_path):
                        release_current = False
                        break
            if not release_qa.get("passed") or not release_current:
                review_failures.append("release_qa: 未通过或发布视频哈希已变化")
    lines.extend(["", "## Agent 完成门槛"])
    if missing_delivery:
        lines.extend(f"- 缺失：`{path}`" for path in missing_delivery)
    if review_failures:
        lines.extend(f"- 审核失败：{item}" for item in review_failures)
    if not missing_delivery and not review_failures:
        lines.append("- ✅ 必备交付物和独立审核均已满足。")
    else:
        lines.append("- ❌ 当前只能视为部分交付，不得标记 completed。")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest["outputs"]["final_delivery_checklist"] = str(report)
    if not missing_delivery and not review_failures:
        manifest["completed_at"] = manifest.get("completed_at") or time.strftime("%Y-%m-%d %H:%M:%S")
    else:
        manifest.pop("completed_at", None)
    write_manifest(paths, manifest)
    if update_latest_episode:
        maybe_update_latest_episode(manifest)
    return report


def qa_release(project_dir: Path) -> Path:
    paths = project_paths(project_dir)
    manifest = init_project(paths.root)
    discover_outputs(paths, manifest)
    rows = []
    issues = []
    structured_results: list[dict[str, Any]] = []
    artifacts: dict[str, dict[str, str]] = {}
    for label, key in (("主账号发布视频", "main_release_video"), ("宝库号发布视频", "library_release_video")):
        value = manifest["outputs"].get(key, "")
        if not value or not Path(value).exists():
            rows.append({"index": str(len(rows) + 1), "file": label, "status": "missing", "notes": "未找到"})
            issues.append(f"- {label}：未找到")
            continue
        path = Path(value)
        duration = safe_duration(path)
        notes = []
        if duration <= 0:
            notes.append("时长异常")
        alignment = probe_av_alignment(path)
        notes.extend(alignment["issues"])
        width = int(alignment.get("width") or 0)
        height = int(alignment.get("height") or 0)
        if width <= 0 or height <= 0 or abs(width / height - 3 / 4) > 0.01:
            notes.append(f"画幅不是 3:4：{width}x{height}")
        rows.append({"index": str(len(rows) + 1), "file": str(path), "status": "warning" if notes else "ok", "notes": f"{duration:.2f}s {'；'.join(notes)}"})
        structured_results.append({"label": label, "path": str(path), "duration_sec": duration, **alignment, "issues": notes})
        artifacts[key] = {"path": str(path), "sha256": sha256_file(path)}
        if notes:
            issues.append(f"- {label}：{'；'.join(notes)}")
    report = paths.status / "qa_release_report.md"
    write_qa_report(report, "发布视频机器审查", paths.release, rows, issues, expected="主账号/宝库号发布视频存在、时长正常")
    manifest["qa"]["release"] = str(report)
    report_json = paths.status / "qa_release_report.json"
    save_json(
        report_json,
        {
            "version": 1,
            "passed": not issues,
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "artifacts": artifacts,
            "results": structured_results,
            "issues": issues,
        },
    )
    manifest["qa"]["release_json"] = str(report_json)
    write_manifest(paths, manifest)
    return report


def probe_av_alignment(path: Path) -> dict[str, Any]:
    process = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,start_time,duration,width,height",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        capture_output=True,
    )
    if process.returncode != 0:
        return {"issues": ["无法读取音视频流"], "width": 0, "height": 0}
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError:
        return {"issues": ["音视频流信息不是有效 JSON"], "width": 0, "height": 0}
    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    issues: list[str] = []
    if not isinstance(video, dict):
        issues.append("缺少视频轨")
        video = {}
    if not isinstance(audio, dict):
        issues.append("缺少音轨")
        audio = {}

    def number(item: dict[str, Any], key: str) -> float | None:
        try:
            return float(item.get(key))
        except (TypeError, ValueError):
            return None

    video_start = number(video, "start_time")
    audio_start = number(audio, "start_time")
    video_duration = number(video, "duration")
    audio_duration = number(audio, "duration")
    if video_start is not None and audio_start is not None and abs(video_start - audio_start) > 0.15:
        issues.append(f"音画起点相差 {abs(video_start - audio_start):.3f}s")
    if video_duration is not None and audio_duration is not None and abs(video_duration - audio_duration) > 0.35:
        issues.append(f"音画时长相差 {abs(video_duration - audio_duration):.3f}s")
    return {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "video_start_sec": video_start,
        "audio_start_sec": audio_start,
        "video_duration_sec": video_duration,
        "audio_duration_sec": audio_duration,
        "issues": issues,
    }


def qa_product(project_dir: Path) -> Path:
    paths = project_paths(project_dir)
    manifest = init_project(paths.root)
    discover_outputs(paths, manifest)
    rows = []
    issues = []
    required_base = ("故事文稿", "故事配乐", "朗读标注", "示范表演", "背景图片")
    required_advanced = ("故事文稿", "故事配乐", "朗读标注", "示范表演", "背景视频", "故事PPT")
    for label, key, required in (
        ("基础版资料包", "product_base", required_base),
        ("进阶版资料包", "product_advanced", required_advanced),
    ):
        value = manifest["outputs"].get(key, "")
        if not value or not Path(value).exists():
            rows.append({"index": str(len(rows) + 1), "file": label, "status": "missing", "notes": "未找到"})
            issues.append(f"- {label}：未找到")
            continue
        directory = Path(value)
        names = [path.name for path in directory.iterdir() if path.is_file()]
        missing = [token for token in required if not any(token in name for name in names)]
        rows.append({"index": str(len(rows) + 1), "file": str(directory), "status": "warning" if missing else "ok", "notes": "缺少：" + "、".join(missing) if missing else "文件齐全"})
        if missing:
            issues.append(f"- {label}：缺少 {'、'.join(missing)}")
    report = paths.status / "qa_product_report.md"
    write_qa_report(report, "资料包结构机器审查", paths.product, rows, issues, expected="基础版/进阶版目录存在且必备文件齐全")
    manifest["qa"]["product"] = str(report)
    write_manifest(paths, manifest)
    return report


def qa_publish(project_dir: Path) -> Path:
    paths = project_paths(project_dir)
    manifest = init_project(paths.root)
    discover_outputs(paths, manifest)
    rows = []
    issues = []
    value = manifest["outputs"].get("publish_package", "")
    publish_dir = Path(value) if value else paths.publish
    required = [
        ("Codex发布物料交接", publish_dir / "publish_package_codex_handoff.md"),
        ("主账号候选帧索引", publish_dir / "frame_candidates" / "main_候选帧索引.jpg"),
        ("宝库号候选帧索引", publish_dir / "frame_candidates" / "library_候选帧索引.jpg"),
    ]
    for index, (label, path) in enumerate(required, start=1):
        ok = path.exists()
        status = "ok" if ok else "missing"
        notes = "存在" if ok else "未找到"
        rows.append({"index": str(index), "file": str(path), "status": status, "notes": notes})
        if not ok:
            issues.append(f"- {label}：未找到")
    cover_requirements = [
        ("主账号三比例封面任务", publish_dir / "main" / "covers" / "main_cover_design_request.md"),
        ("主账号三比例封面参考帧", publish_dir / "main" / "covers" / "reference_3x4.png"),
        ("宝库号三比例封面参考帧", publish_dir / "library" / "covers" / "reference_3x4.png"),
        ("宝库号参考帧选择说明", publish_dir / "library" / "covers" / "reference_choice.md"),
        ("宝库号三比例衍生提示", publish_dir / "library" / "covers" / "cover_derivative_prompts.md"),
        ("主账号发布文案", publish_dir / "main" / "copy.md"),
        ("宝库号发布文案", publish_dir / "library" / "copy.md"),
    ]
    for account_label, account_key in (("主账号", "main"), ("宝库号", "library")):
        for ratio in ("3x4", "4x3", "16x9"):
            cover_requirements.append((f"{account_label}{ratio}封面", publish_dir / account_key / "covers" / f"cover_{ratio}.png"))
    cover_ok = True
    cover_notes = []
    for label, path in cover_requirements:
        if path.exists():
            cover_notes.append(f"{label}存在")
        else:
            cover_ok = False
            cover_notes.append(f"{label}缺失")
    rows.append(
        {
            "index": str(len(rows) + 1),
            "file": str(publish_dir),
            "status": "ok" if cover_ok else "warning",
            "notes": "；".join(cover_notes) if cover_notes else "发布文案或六张封面尚未生成",
        }
    )
    if not cover_ok:
        issues.append("- 发布物料：两份文案、六张封面或封面参考尚未完整生成")
    report = paths.status / "qa_publish_report.md"
    write_qa_report(report, "发布物料机器审查", publish_dir, rows, issues, expected="候选帧、两账号发布文案及主/宝库各 3:4、4:3、16:9 三张封面完整")
    manifest["qa"]["publish"] = str(report)
    write_manifest(paths, manifest)
    return report


def discover_outputs(paths: ProjectPaths, manifest: dict[str, Any]) -> None:
    def should_replace(existing: str | None) -> bool:
        if not existing:
            return True
        try:
            path = Path(existing).expanduser()
        except Exception:
            return True
        if not path.exists():
            return True
        try:
            path.resolve().relative_to(paths.root.resolve())
            return False
        except Exception:
            return True

    candidates = {
        "storyboard": list(paths.images.rglob("*_storyboard_lines.txt")),
        "jobs_csv": list(paths.video_jobs.rglob("*_image_video_jobs.csv")),
        "review_html": list(paths.video_jobs.rglob("*_review.html")),
        "main_release_video": list(paths.root.rglob("主账号发布视频.mp4")),
        "library_release_video": list(paths.root.rglob("宝库号发布视频.mp4")),
        "demo_voice_bgm": list(paths.root.rglob("story_demo_voice_bgm.mp4")),
        "timings_json": list(paths.root.rglob("timings.json")),
        "sales_subtitles_srt": list(paths.root.rglob("story_sales_subtitles.srt")),
        "background_video_with_sub": list(paths.root.rglob("story_sales_subs_bgm.mp4")) + list(paths.root.rglob("*含字幕*.mp4")),
        "background_video_no_sub": list(paths.root.rglob("story_no_subs_bgm.mp4")) + list(paths.root.rglob("*无字幕*.mp4")),
        "release_plate_image": list(paths.release.rglob("main_release_plate.png")) + list(paths.release.rglob("release_plate.png")),
        "library_release_plate_image": list(paths.release.rglob("library_release_plate.png")),
        "main_background_image": list(paths.release.rglob("main_background_16x9.png")),
        "story_frame_source": list(paths.release.rglob("story_frame_source.png")) + list(paths.release.rglob("story_frame_a_source.png")),
        "story_frame_a": list(paths.release.rglob("story_frame_a.png")),
        "keying_preset": list(paths.release.rglob("keying_preset.json")),
    }
    for key, values in candidates.items():
        if values and should_replace(manifest["outputs"].get(key)):
            manifest["outputs"][key] = str(sorted(values, key=lambda p: len(p.parts))[0])
    base_dirs = [p for p in paths.root.rglob("*基础版*") if p.is_dir() and "_旧版_" not in p.name]
    advanced_dirs = [p for p in paths.root.rglob("*进阶版*") if p.is_dir() and "_旧版_" not in p.name]
    if base_dirs:
        if should_replace(manifest["outputs"].get("product_base")):
            manifest["outputs"]["product_base"] = str(sorted(base_dirs, key=lambda p: (len(p.parts), p.name))[0])
    if advanced_dirs:
        if should_replace(manifest["outputs"].get("product_advanced")):
            manifest["outputs"]["product_advanced"] = str(sorted(advanced_dirs, key=lambda p: (len(p.parts), p.name))[0])
    if paths.publish.exists() and any(paths.publish.iterdir()):
        if should_replace(manifest["outputs"].get("publish_package")):
            manifest["outputs"]["publish_package"] = str(paths.publish)
    elif manifest["outputs"].get("publish_package") == str(paths.publish):
        manifest["outputs"]["publish_package"] = ""


def refresh_project_outputs(project_dir: Path) -> dict[str, Any]:
    paths = project_paths(project_dir)
    manifest = init_project(paths.root)
    discover_outputs(paths, manifest)
    if not manifest["story"].get("duration_text"):
        duration_source = manifest["outputs"].get("demo_voice_bgm") or manifest["outputs"].get("background_video_no_sub")
        if duration_source and Path(duration_source).exists():
            manifest["story"]["duration_text"] = format_duration_text(safe_duration(Path(duration_source)))
    write_manifest(paths, manifest)
    return manifest


def first_existing(*values: str | Path | None) -> Path | None:
    for value in values:
        if not value:
            continue
        path = Path(value).expanduser()
        if path.exists():
            return path
    return None


def first_image_in(directory: Path | None, slug: str | None = None) -> Path | None:
    if directory is None or not directory.exists():
        return None
    try:
        images = sorted_image_files(directory, slug=slug)
    except Exception:
        images = sorted(path for path in directory.iterdir() if path.suffix.lower() in IMAGE_EXTENSIONS)
    return images[0] if images else None


def doctor_project(project_dir: Path) -> Path:
    paths = project_paths(project_dir)
    manifest = refresh_project_outputs(paths.root)
    config = load_config()
    story = manifest.get("story", {})
    inputs = manifest.get("inputs", {})
    outputs = manifest.get("outputs", {})
    qa = manifest.get("qa", {})
    rows: list[dict[str, str]] = []
    issues: list[str] = []

    def add(section: str, item: str, status: str, detail: str = "") -> None:
        rows.append({"section": section, "item": item, "status": status, "detail": detail})
        if status in {"warning", "missing", "error"}:
            issues.append(f"- {section} / {item}：{detail or status}")

    add("项目", "根目录", "ok" if paths.root.exists() else "missing", str(paths.root))
    add("项目", "manifest", "ok" if paths.manifest.exists() else "missing", str(paths.manifest))
    for label, directory in PROJECT_DIRS.items():
        path = paths.root / directory
        add("目录", label, "ok" if path.exists() else "missing", str(path))

    for key in ("name", "slug", "episode", "story_type", "age_range"):
        value = story.get(key)
        add("故事信息", key, "ok" if value else "missing", str(value or "未设置"))
    if not story.get("duration_text"):
        add("故事信息", "duration_text", "warning", "未生成时长文案；后续发布/资料包会显示待定")

    for key, label in (
        ("story_text", "故事正文"),
        ("narration", "旁白原声"),
        ("greenscreen_video", "绿幕视频"),
    ):
        value = inputs.get(key, "")
        path = Path(value).expanduser() if value else None
        if path and path.exists():
            add("输入素材", label, "ok", str(path))
        elif key == "greenscreen_video":
            add("输入素材", label, "warning", "未找到；仅背景成片可继续，发布视频/资料包会受阻")
        else:
            add("输入素材", label, "missing", "未找到")

    output_checks = (
        ("jobs_csv", "图生视频任务CSV"),
        ("background_video_with_sub", "含字幕背景成片"),
        ("background_video_no_sub", "无字幕背景成片"),
        ("main_release_video", "主账号发布视频"),
        ("library_release_video", "宝库号发布视频"),
        ("publish_package", "发布物料目录"),
        ("product_base", "基础版资料包"),
        ("product_advanced", "进阶版资料包"),
        ("final_delivery_checklist", "总交付清单"),
    )
    for key, label in output_checks:
        value = outputs.get(key, "")
        path = Path(value).expanduser() if value else None
        status = "ok" if path and path.exists() else "warning"
        add("流程输出", label, status, str(path) if path else "未发现")

    for key in ("images", "videos", "theme_assets", "release", "publish", "product"):
        value = qa.get(key, "")
        path = Path(value).expanduser() if value else None
        status = "ok" if path and path.exists() else "warning"
        add("QA", key, status, str(path) if path else "未生成")

    brand_assets = config.get("brand_assets", {})
    for key, value in brand_assets.items():
        if not value or key == "assets_dir":
            continue
        path = Path(str(value)).expanduser()
        add("全局配置", f"brand_assets.{key}", "ok" if path.exists() else "warning", str(path))
    external_tools = config.get("external_tools", {})
    for key, value in external_tools.items():
        if not value:
            add("外部依赖", key, "warning", "未配置")
            continue
        path = Path(str(value)).expanduser()
        add("外部依赖", key, "ok" if path.exists() else "warning", str(path))

    stale_paths = find_stale_manifest_paths(paths, manifest)
    for key, value in stale_paths:
        add("manifest路径", key, "warning", f"路径不在当前项目内或不存在：{value}")

    report = paths.status / "doctor_project_report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    table = ["| 分类 | 项目 | 状态 | 详情 |", "| --- | --- | --- | --- |"]
    for row in rows:
        table.append(f"| {row['section']} | {row['item']} | {row['status']} | `{row['detail']}` |")
    body = [
        "# 工程体检报告",
        "",
        f"- 项目目录：`{paths.root}`",
        f"- 检查时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 结论",
        "未发现明显阻断项。" if not issues else "\n".join(issues),
        "",
        "## 明细",
        "\n".join(table),
        "",
    ]
    report.write_text("\n".join(body), encoding="utf-8")
    manifest.setdefault("qa", {})["doctor"] = str(report)
    write_manifest(paths, manifest)
    return report


def find_stale_manifest_paths(paths: ProjectPaths, manifest: dict[str, Any]) -> list[tuple[str, str]]:
    stale: list[tuple[str, str]] = []
    deprecated_outputs = {"story_frame_b"}
    for section in ("inputs", "outputs", "qa"):
        values = manifest.get(section, {})
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            if section == "outputs" and key in deprecated_outputs:
                continue
            if not isinstance(value, str) or not value.startswith("/"):
                continue
            path = Path(value).expanduser()
            if path.exists():
                try:
                    path.resolve().relative_to(paths.root.resolve())
                except Exception:
                    if section != "inputs":
                        stale.append((f"{section}.{key}", value))
            else:
                stale.append((f"{section}.{key}", value))
    return stale


def maybe_update_latest_episode(manifest: dict[str, Any]) -> None:
    story = manifest.get("story", {})
    if story.get("does_not_count_episode"):
        return
    if not story.get("update_latest_episode_on_delivery", True):
        return
    config = load_config()
    episode = int(story.get("episode", 0) or 0)
    if episode > int(config.get("latest_episode", 0)):
        config["latest_episode"] = episode
        save_config(config)


def format_duration_text(seconds: float) -> str:
    seconds = int(round(seconds))
    minutes, sec = divmod(seconds, 60)
    if minutes:
        return f"{minutes}分{sec:02d}秒" if sec else f"{minutes}分钟"
    return f"{sec}秒"


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ):
        font_path = Path(path)
        if font_path.exists():
            return ImageFont.truetype(str(font_path), size)
    return ImageFont.load_default()


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
