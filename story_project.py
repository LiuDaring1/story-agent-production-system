from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageStat, ImageOps

from story_video_synthesizer.image_video import IMAGE_EXTENSIONS, sorted_image_files
from story_video_synthesizer.media import VIDEO_EXTENSIONS, probe_duration
from video_motion import (
    file_sha256 as motion_file_sha256,
    formal_source_issues,
    motion_evidence_issues,
    motion_metrics,
    video_receipt_issues,
)
from keying_quality import write_evidence_assets
from production_keying import (
    RVM_ALPHA_CHOKE_CANDIDATES,
    RVM_ALPHA_CHOKE_PIXELS,
    render_cached_rvm_foreground_frame,
)
from cover_quality import (
    integrated_cover_issues,
    render_required_covers,
    required_cover_issues,
)
from story_delivery_policy import normalize_duration_label


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "pipeline_config.json"
MAIN_PACKAGE_REFERENCE_MANIFEST = ROOT / "assets" / "references" / "main_vertical_package_reference.json"
MAIN_PACKAGE_PROMPT_VERSION = "story-main-package-fixed-prompt/v3"
MAIN_PACKAGE_RECEIPT_SCHEMA_VERSION = "story-main-package-generation/v5"
MAIN_PACKAGE_PANEL_SIZE = (2304, 888)
THEME_ASSET_GENERATED_FILENAMES = (
    "main_release_plate.png",
    "main_release_plate_top.png",
    "main_release_plate_bottom.png",
    "library_release_plate.png",
    "library_release_plate_top.png",
    "library_release_plate_bottom.png",
    "main_background_16x9.png",
    "story_frame_source.png",
    "story_frame_a.png",
    "main_package_spec.json",
    "main_package_generation_receipt.json",
    "theme_assets_imagegen_request.md",
    "theme_assets_codex_handoff.txt",
)
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
    "default_image_style": "3D卡通",
    "story_type_options": ["寓言故事", "成语故事", "童话故事", "民间故事", "神话故事", "红色故事", "历史故事", "科普故事"],
    "age_range_options": ["3-6岁", "4-6岁", "6-8岁", "9-11岁", "12-14岁", "15岁以上"],
    "default_age_range": "",
    "brand_assets": {
        "assets_dir": "/Volumes/语苗计划/桌面整理2026-08-07/故事剪辑/（常用）剪辑所使用的素材",
        "logo": "assets/brand/program_logo.png",
        "watermark_logo": "assets/brand/program_logo.png",
        "story_logo": "assets/brand/program_logo.png",
        "antipiracy_logo": "assets/brand/program_logo.png",
        "frame_reference": "",
        "cover_reference": "",
        "main_package_reference": "assets/references/main_vertical_package_reference.png",
    },
    "release_defaults": {
        "video_box": "0,416,1080,608",
        "story_box": "210,270,910,512",
        "story_bleed": 0,
        "background_blur": 14,
        "b_story_box": "356,180,1209,680",
        "b_windows": "auto",
        "c_windows": "auto",
        "person_height": 1080,
        "person_x": 1280,
        "person_y": 0,
        "person_crop": "",
        "person_grade": "natural",
        "person_beauty": "light",
        "keyer": "rvm",
        "preferred_keyer": "rvm",
        "rvm_input_width": 1920,
        "rvm_input_height": 1080,
        "rvm_output_fps": 25,
        "rvm_downsample_ratio": 0.4,
        "rvm_alpha_choke_candidates": [0, 1, 2],
        "rvm_runtime_path": "",
        "rvm_model_path": "",
        "chroma_color": "0x00FF00",
        "chroma_similarity": 0.095,
        "chroma_blend": 0.04,
        "watermark_width": 96,
        "watermark_opacity": 0.62,
        "watermark_speed": 0.45,
        "tail_seconds": 0,
        "tail_notice_text": "有需要联系客服，好作品有偿分享！",
        "story_logo_width_a": 150,
        "story_logo_width_b": 175,
        "story_logo_x": 42,
        "story_logo_y": 44,
        "output_scale": 1,
        "crf": 15,
        "preset": "medium",
        "subtitle_font_size": 52,
        "subtitle_margin_v": 72,
    },
    "product_defaults": {
        "music_volume": 0.22,
        "narration_volume": 1.0,
        "include_demo_logo": True,
    },
    "external_tools": {
        "suno_story_score_skill": str(Path.home() / "Downloads" / "suno-story-score.skill"),
        "story_performance_script_skill": "skills/story-performance-script/SKILL.md",
    },
    "video_api": {
        "provider": "toapis_grok_1_0",
        "browser_provider_name": "Flow",
        "max_submit_first": 20,
        "adapters": {
            "toapis_grok_1_0": {
                "runner": "run_image_video_jobs.py",
                "base_url": "https://toapis.com/v1",
                "model": "grok-video-1.0",
                "api_key_env": "TOAPIS_API_KEY",
                "estimated_cost_cny_per_clip": 0.06,
                "estimated_cost_cny_per_second": 0.01,
                "default_seconds": 6,
                "min_seconds": 6,
                "max_seconds": 10,
                "duration_choices": [6, 10],
                "default_resolution": "720p",
                "default_ratio": "16:9",
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
        "target_delivery_seconds": 36000,
        "runtime_deadline_enabled": True,
        "deadline_behavior": "deliver_best_valid",
        "max_full_resolution_encodes": 1,
        "review_pass_score": 85,
        "whisper_model": "small",
        "working_video_max_width": 0,
        "proxy_video_max_width": 1280,
        "commander_model": "gpt-5.6-sol",
        "commander_reasoning_effort": "medium",
        "worker_model": "gpt-5.6-luna",
        "worker_reasoning_effort": "high",
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
    manifest_override = os.environ.get("STORY_AGENT_MANIFEST_OVERRIDE", "").strip()
    worker_root = os.environ.get("STORY_AGENT_PROJECT_ROOT", "").strip()
    manifest_path = root / PROJECT_DIRS["status"] / MANIFEST_NAME
    if manifest_override and worker_root:
        try:
            if root.resolve() == Path(worker_root).expanduser().resolve():
                manifest_path = Path(manifest_override).expanduser()
        except OSError:
            pass
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
        manifest=manifest_path,
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
            "image_style": config.get("default_image_style", "3D卡通"),
            "age_range": config.get("default_age_range", ""),
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
            "color_lut": "",
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
            "target_delivery_seconds": int(agent_defaults.get("target_delivery_seconds", 36000)),
            "runtime_deadline_enabled": bool(agent_defaults.get("runtime_deadline_enabled", True)),
            "deadline_behavior": str(agent_defaults.get("deadline_behavior") or "deliver_best_valid"),
            "max_full_resolution_encodes": int(agent_defaults.get("max_full_resolution_encodes", 1)),
            "min_free_disk_gb": float(agent_defaults.get("min_free_disk_gb", 10.0)),
            "started_at": "",
            "active_elapsed_seconds": 0.0,
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
    input_contract = manifest.get("agent", {}).get("input_contract", {}) if isinstance(manifest.get("agent"), dict) else {}
    prepared_mode = isinstance(input_contract, dict) and input_contract.get("mode") == "prepared_greenscreen_confirmed_text"
    files = [path for path in paths.root.rglob("*") if path.is_file() and STATUS_DIR_NAME not in path.parts]
    input_files = [path for path in files if is_user_input_asset(paths, path)]
    bound_story_text = Path(str(manifest.get("inputs", {}).get("story_text") or ""))
    story_text = bound_story_text if prepared_mode and bound_story_text.is_file() else choose_first(
        input_files, TEXT_EXTENSIONS, ("原文", "story", "source", "正文", "故事", "文稿")
    )
    audios = [path for path in input_files if path.suffix.lower() in AUDIO_EXTENSIONS]
    videos = [path for path in input_files if path.suffix.lower() in VIDEO_EXTENSIONS]
    narration = choose_preferred_audio(audios)
    music = choose_preferred_music(audios, narration)
    bound_greenscreen = Path(str(manifest.get("inputs", {}).get("greenscreen_video") or ""))
    greenscreen = bound_greenscreen if prepared_mode and bound_greenscreen.is_file() else choose_preferred_video(videos)
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
    directory_parts = relative_parts[:-1]
    if any(part in generated_dirs for part in directory_parts):
        return False
    # Story-full-auto projects may use story-specific production folders such as
    # ``02_背景动画`` or ``04_配乐与音频`` in addition to the canonical names
    # above.  Treat every numbered production folder as generated, while keeping
    # the dedicated ``00_输入素材`` folder eligible for authoritative inputs.
    if directory_parts:
        top_level = directory_parts[0]
        if re.match(r"^\d{2}_", top_level) and top_level != PROJECT_DIRS["inputs"]:
            return False
    return True


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
    candidates = [
        path
        for path in audios
        if (narration is None or path.resolve() != narration.resolve()) and not looks_like_narration_audio(path)
    ]
    if not candidates:
        return None
    preferred_tokens = ("背景音乐", "配乐", "bgm", "music", "soundtrack", "score")
    preferred = [path for path in candidates if any(token in path.name.lower() for token in preferred_tokens)]
    if preferred:
        return sorted(preferred, key=lambda p: p.name)[0]
    return None


def looks_like_narration_audio(path: Path) -> bool:
    name = path.stem.lower().replace("-", "_")
    narration_tokens = (
        "narration",
        "voice",
        "voiceover",
        "source_extracted",
        "extracted_audio",
        "clean_audio",
        "clean_narration",
        "原声",
        "旁白",
        "口播",
        "人声",
    )
    return any(token in name for token in narration_tokens)


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
    # Age is a product declaration, not a reliable semantic inference.  Leave
    # it unset until the user supplies it at intake or through update-story.


def infer_story_type(text: str, default: str) -> str:
    if any(token in text for token in ("成语", "矛盾", "画蛇添足", "刻舟求剑", "亡羊补牢")):
        return "成语故事"
    if any(token in text for token in ("寓言", "狐狸", "乌鸦", "农夫")):
        return "寓言故事"
    if any(token in text for token in ("很久以前", "村子", "传说", "民间")):
        return "民间故事"
    return default or "童话故事"


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
            manifest["story"][key] = (
                normalize_duration_label(str(value)) if key == "duration_text" else value
            )
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


def qa_videos(project_dir: Path, videos_dir: Path | None = None, *, execution_mode: str = "production") -> Path:
    paths = project_paths(project_dir)
    manifest = init_project(paths.root)
    target = videos_dir.expanduser() if videos_dir else paths.video_jobs / "videos"
    rows: list[dict[str, str]] = []
    issues: list[str] = []
    videos = sorted([p for p in target.iterdir() if p.suffix.lower() in VIDEO_EXTENSIONS]) if target.exists() else []
    frames_dir = paths.status / "video_review_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    jobs_rows: dict[str, dict[str, str]] = {}
    jobs_candidates = sorted(paths.video_jobs.glob("*_image_video_jobs.csv"))
    if jobs_candidates:
        with jobs_candidates[0].open(encoding="utf-8-sig", newline="") as handle:
            jobs_rows = {
                str(row.get("target_video_filename") or "").strip(): row
                for row in csv.DictReader(handle)
            }
    evidence_rows: list[dict[str, Any]] = []
    retry_indices: list[int] = []
    for index, path in enumerate(videos, start=1):
        duration = safe_duration(path)
        frame_paths = extract_video_review_frames(path, frames_dir / path.stem, duration)
        issue: list[str] = []
        row = jobs_rows.get(path.name, {})
        try:
            expected_motion = json.loads(row.get("expected_motion") or "{}")
            if not expected_motion and row.get("motion_shot"):
                motion_shot = json.loads(row["motion_shot"])
                expected_motion = motion_shot.get("expected_motion", {})
        except json.JSONDecodeError:
            expected_motion = {}
            issue.append("expected_motion 无效")
        metrics: dict[str, Any] = {}
        if expected_motion:
            try:
                metrics = motion_metrics(frame_paths, expected_motion)
                issue.extend(motion_evidence_issues(metrics, expected_motion))
            except Exception as exc:
                issue.append(f"运动证据生成失败：{exc}")
        if duration < 1.0:
            issue.append("时长过短")
        if any(is_dark_frame(frame) for frame in frame_paths):
            issue.append("抽帧疑似黑屏")
        contract_bound = bool(row.get("story_contract_dependency_sha256"))
        if row and contract_bound:
            if not expected_motion:
                issue.append("缺少逐镜 expected_motion")
            source_issues = formal_source_issues(row, production_mode=execution_mode == "production")
            issue.extend(source_issues)
            if execution_mode == "production":
                issue.extend(video_receipt_issues(row, path, production_mode=True))
        rows.append({"index": str(index), "file": str(path), "duration": f"{duration:.2f}s", "status": "warning" if issue else "ok", "notes": "；".join(issue)})
        scene = int(row.get("scene") or index)
        evidence_rows.append({
            "scene": scene, "file": str(path), "expected_motion": expected_motion,
            "metrics": metrics, "issues": issue, "passed": not issue,
            "video_source_kind": row.get("video_source_kind", ""),
            "video_provider": row.get("video_provider", ""),
            "video_execution_mode": row.get("video_execution_mode", ""),
        })
        if issue:
            issues.append(f"- {path.name}：{'；'.join(issue)}")
            retry_indices.append(scene)
    report = paths.status / "qa_videos_report.md"
    write_qa_report(report, "视频片段机器审查", target, rows, issues, expected="来源可用于正式生产、运动符合逐镜 expected_motion、时长正常且抽帧非黑屏")
    evidence_path = paths.status / "qa_videos_report.json"
    payload = {
        "version": 2,
        "execution_mode": execution_mode,
        "jobs_csv": str(jobs_candidates[0]) if jobs_candidates else "",
        "jobs_sha256": motion_file_sha256(jobs_candidates[0]) if jobs_candidates else "",
        "passed": not issues,
        "retry_indices": sorted(set(retry_indices)),
        "clips": evidence_rows,
    }
    evidence_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest["qa"]["videos"] = str(report)
    manifest["qa"]["videos_json"] = str(evidence_path)
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


def load_main_package_reference(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load the immutable main-account packaging reference and verify its hash."""

    try:
        receipt = json.loads(MAIN_PACKAGE_REFERENCE_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FileNotFoundError("package_reference_missing: 主账号包装参考资产清单缺失或损坏") from exc
    configured = str(
        ((config or load_config()).get("brand_assets", {}) or {}).get("main_package_reference")
        or receipt.get("asset")
        or ""
    ).strip()
    reference = Path(configured).expanduser()
    if not reference.is_absolute():
        reference = (ROOT / reference).resolve()
    if not reference.is_file():
        raise FileNotFoundError(f"package_reference_missing: {reference}")
    actual_sha256 = hashlib.sha256(reference.read_bytes()).hexdigest()
    if receipt.get("reference_role") != "main_vertical_package" or actual_sha256 != receipt.get("sha256"):
        raise ValueError("package_reference_invalid: 主账号包装参考图角色或 SHA-256 不匹配")
    return {**receipt, "asset_path": str(reference), "sha256": actual_sha256}


def main_package_fixed_prompt_template() -> str:
    """Return the stable ImageGen instruction; only named story fields vary."""

    return (
        "把随请求附上的参考图仅作为主账号竖屏包装的版式、层级和简洁视觉语言基准。"
        "必须实际查看并使用原图，不能只依据文字摘要重建。分别生成两张 2304×888 位图："
        "顶部只包含故事类型《{story_type}》和故事名称《{story_title}》；"
        "底部只包含‘完整版时长：{duration}’、‘适合年龄：{age_range}’和"
        "‘适用于{use_cases}’。上下图必须像参考图那样属于一块连续、从容的节目包装系统："
        "以大留白、清楚标题层级和少量主题点缀为主，不要把信息拆成许多相互竞争的牌匾、卡片或商品功能框。"
        "可随故事类型克制调整配色、字体和少量主题元素，但不得变成资料包商品页；主账号与宝库号必须一眼可区分。"
        "参考图里的历史故事、煮酒论英雄、4分50秒、8岁以上及示例人物只用于理解结构，"
        "不得出现在新图。文字必须原生融入图像，禁止生成空板后再由程序叠字。"
        "只要本次任务已经触发，就必须针对当前任务书实际重新执行 ImageGen 生成或参考图编辑；"
        "不得因为目标路径已有旧图而跳过生成、沿用旧版本上下板或只重写回执。"
        "不要生成中间视频区域，不要生成完整竖屏成图，不要添加二维码、平台 UI、陌生 Logo、人物或吉祥物。"
        "这张参考图不得用于片头卡、寓意卡或六比例营销封面。"
    )


def build_main_package_spec(
    *,
    reference: dict[str, Any],
    frame_reference: dict[str, Any],
    frame_story_box: tuple[int, int, int, int],
    story_type: str,
    story_title: str,
    duration: str,
    age_range: str,
    use_cases: str,
    top_panel: Path,
    bottom_panel: Path,
) -> dict[str, Any]:
    if not age_range.strip() or age_range.strip() in {"待定", "按本期设定"}:
        raise ValueError("age_range_user_input_required: 主账号包装适合年龄必须由用户明确指定")
    template = main_package_fixed_prompt_template()
    values = {
        "story_type": story_type,
        "story_title": story_title,
        "duration": duration,
        "age_range": age_range,
        "use_cases": use_cases,
    }
    return {
        "schema_version": "story-main-package-spec/v1",
        "reference_asset": reference["asset_path"],
        "reference_sha256": reference["sha256"],
        "reference_role": "main_vertical_package",
        "frame_reference_asset": frame_reference["asset_path"],
        "frame_reference_sha256": frame_reference["sha256"],
        "frame_reference_role": "geometry_only_not_theme_or_ornament",
        "frame_story_box": list(frame_story_box),
        "frame_source_background": "#FF00FF",
        "frame_source_mode": "imagegen_magenta_chroma_source",
        "frame_design_policy": "redesign_from_current_story_theme",
        **values,
        "top_plate": {"path": str(top_panel), "size": list(MAIN_PACKAGE_PANEL_SIZE)},
        "bottom_plate": {"path": str(bottom_panel), "size": list(MAIN_PACKAGE_PANEL_SIZE)},
        "text_integration": "imagegen_native",
        "fixed_prompt_version": MAIN_PACKAGE_PROMPT_VERSION,
        "fixed_prompt_template_sha256": hashlib.sha256(template.encode("utf-8")).hexdigest(),
        "fixed_prompt": template.format(**values),
        "required_imagegen_inputs": ["reference_asset", "frame_reference_asset", "fixed_prompt"],
        "ocr_validation": "required",
        "reference_content_leak_check": "required",
        "render_usage_proof": "required_before_release_qa",
        "excluded_scopes": ["title_card", "moral_card", "marketing_covers"],
        "account_role": "main_account_vertical_release",
        "story_identity": {"story_type": story_type, "story_title": story_title},
    }


def create_theme_asset_request(project_dir: Path) -> dict[str, Path]:
    paths = project_paths(project_dir)
    manifest = detect_project_assets(paths.root, extract_audio=False)
    story = manifest["story"]
    assets_dir = paths.release / "theme_assets"
    existing_generated = any((assets_dir / name).exists() for name in THEME_ASSET_GENERATED_FILENAMES)
    if existing_generated:
        existing_issues = main_package_receipt_issues(paths)
        if existing_issues:
            archive_invalid_theme_assets(paths, existing_issues)
    assets_dir.mkdir(parents=True, exist_ok=True)
    config = load_config()
    reference = load_main_package_reference(config)
    frame_reference_path = Path(str(config.get("brand_assets", {}).get("frame_reference") or "")).expanduser()
    if not frame_reference_path.is_file():
        raise FileNotFoundError(f"故事框几何参考图不存在：{frame_reference_path}")
    frame_reference = {
        "asset_path": str(frame_reference_path.resolve()),
        "sha256": hashlib.sha256(frame_reference_path.read_bytes()).hexdigest(),
    }
    frame_story_box = parse_box(str(config.get("release_defaults", {}).get("story_box", "210,270,910,512")))
    theme = infer_theme_text(manifest)
    duration_text = str(story.get("duration_text") or "").strip()
    if not duration_text:
        narration = first_existing(
            manifest.get("inputs", {}).get("narration"),
            manifest.get("inputs", {}).get("extracted_narration"),
        )
        if narration is not None:
            duration_text = format_duration_text(safe_duration(narration))
            story["duration_text"] = duration_text
    duration_text = duration_text or "待定"
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
        "main_package_spec": assets_dir / "main_package_spec.json",
        "main_package_receipt": assets_dir / "main_package_generation_receipt.json",
    }
    usage_text = "朗诵比赛、故事表演、少儿口才、技能比拼"
    age_range = str(story.get("age_range") or "").strip()
    manual_overrides = story.get("manual_overrides") if isinstance(story.get("manual_overrides"), dict) else {}
    if not age_range or manual_overrides.get("age_range") is not True:
        raise ValueError("age_range_user_input_required: 请先由用户指定适合年龄，再生成主账号包装")
    package_spec = build_main_package_spec(
        reference=reference,
        frame_reference=frame_reference,
        frame_story_box=frame_story_box,
        story_type=str(story.get("story_type") or "儿童故事"),
        story_title=str(story.get("name") or ""),
        duration=duration_text,
        age_range=age_range,
        use_cases=usage_text,
        top_panel=output_paths["main_plate_top"],
        bottom_panel=output_paths["main_plate_bottom"],
    )
    save_json(output_paths["main_package_spec"], package_spec)
    request = build_theme_asset_imagegen_request(
        manifest=manifest,
        output_paths=output_paths,
        theme=theme,
        duration_text=duration_text,
        config=config,
    )
    request_path.write_text(request, encoding="utf-8")
    package_spec["theme_request_path"] = str(request_path)
    package_spec["theme_request_sha256"] = hashlib.sha256(request_path.read_bytes()).hexdigest()
    save_json(output_paths["main_package_spec"], package_spec)
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
    manifest["outputs"]["main_package_spec"] = str(output_paths["main_package_spec"])
    manifest["outputs"]["main_package_reference"] = str(reference["asset_path"])
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
        "1. 使用当前 Codex 对话/API 的原生图像生成能力，生成/编辑四张上下包装板、一张 16:9 无框主账号背景、一个统一故事框源图。\n"
        "2. 不要调用旧的 CLI fallback、scripts/image_gen.py、OPENAI_API_KEY、XAI_API_KEY 或任何外部旧图像 API。\n"
        "3. 生成图像要有高质感儿童节目包装效果，不要用本地代码从零画占位图。\n"
        "4. 两张发布底板必须由 Codex 原生图像生成直接包含任务书列出的全部可见文字信息，"
        "不能先生成空白底板再用 Pillow 或其他本地代码后期添加文字；无文字底板视为失败。\n"
        "5. 主账号上下板必须把 main_package_spec.json 的 reference_asset 作为 imagegen 原图输入，"
        "同时使用其中 fixed_prompt；不得只读文字后丢弃参考图。底板拆成上半包装图和下半包装图分别生成，中间由程序保留固定 16:9 空挡，"
        "不要让 imagegen 直接生成整张 1080x1440 底板。上/下素材里的文字仍必须由 Codex 原生生成。\n"
        "6. 允许用 Pillow 只做后处理：裁切、三段拼接、尺寸整理、透明通道和 QA；不允许用 Pillow 添加、覆盖或修正底板文字。\n"
        "7. 故事框必须把 main_package_spec.json 的 frame_reference_asset 作为几何参考输入，但只能继承占位、开口比例和实用边框厚度；"
        "不得继承参考图的羊角、祥云、道具或其他故事主题。框体必须按当前故事从零重新设计。ImageGen 源图的整张底色（框内和框外）"
        "必须是纯 #FF00FF 洋红，禁止棋盘格、白底或直接透明输出；透明 PNG 只能由该洋红源图做固定色键/Alpha 后处理得到。"
        "A/B 景复用同一个框，具体缩放与摆放放到发布视频合成环节处理。禁止使用 SVG、HTML、Canvas、Pillow 或 FFmpeg 从零绘制框体。\n"
        "8. 最终文件必须保存到任务书指定的绝对路径，文件名完全一致；并按任务书写出 main_package_generation_receipt.json，记录参考图、Prompt、任务书、主账号背景、故事框源图/透明框和四张上下包装图哈希，OCR、示例内容泄漏检查、主/宝库号角色区分审核、背景清洁审核、generation_methods、svg_used=false 和 attempt_count（1–3，首次加最多两轮定向修正）。\n"
        "9. 主题素材通过 QA 后，只生成抠像候选与站立/大手势短样本，用来确定人物大小、初始 X 轴、抠像边缘和背景融合；不要在本阶段重复运行完整发布预演。\n"
        "10. 把最终布局和抠像参数写回桌面故事项目的 04_发布视频/keying/keying_preset.json。后续 release_preview 阶段会用完整背景成片、同一正式合成代码和最终 Demo 参数自动抽渲代表帧，并展示到 Dashboard；不要求用户半夜点击批准。\n"
        "11. 只处理发布视觉定版，不要改分镜、图生视频、配乐、背景成片，也不要编码完整发布视频；自动预检通过后，状态机才允许一次正式全片编码。"
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
    package_spec = json.loads(output_paths["main_package_spec"].read_text(encoding="utf-8"))
    return f"""# 《{story_name}》发布素材 Codex/imagegen 任务

## 工作边界

只做发布视频第 12 步素材：两张 3:4 发布底板、主账号 16:9 无框背景图、统一视觉设计的故事框。不要重做分镜、图片、图生视频、配乐、背景成片。

请使用 imagegen 生成或编辑位图素材。生成完成后，把最终文件保存到下面这些绝对路径，文件名必须完全一致：

```text
主账号最终 3:4 发布底板：{output_paths['main_plate']}
主账号顶部源图 2304x888：{output_paths['main_plate_top']}
主账号底部源图 2304x888：{output_paths['main_plate_bottom']}
宝库号最终 3:4 发布底板：{output_paths['library_plate']}
宝库号顶部源图 2304x888：{output_paths['library_plate_top']}
宝库号底部源图 2304x888：{output_paths['library_plate_bottom']}
主账号 16:9 无框背景图：{output_paths['main_bg']}
统一故事框源图：{output_paths['frame_source']}
统一透明故事框：{output_paths['frame_a']}
主账号包装合同：{output_paths['main_package_spec']}
主账号生成回执：{output_paths['main_package_receipt']}
```

## 必需参考输入

- 必需参考图：`{package_spec['reference_asset']}`
- 参考图 SHA-256：`{package_spec['reference_sha256']}`
- 参考图角色：`main_vertical_package`
- 固定 Prompt 版本：`{package_spec['fixed_prompt_version']}`
- 固定 Prompt（必须与参考图同时交给 imagegen，不能只转述图片）：

```text
{package_spec['fixed_prompt']}
```

如果参考图缺失或 SHA 不符，立即返回 `package_reference_missing`，不得凭空设计新包装。该参考图只用于主账号上下包装板，不得用于片头、寓意卡或六比例营销封面。

- 故事框几何参考图：`{package_spec['frame_reference_asset']}`
- 故事框几何参考 SHA-256：`{package_spec['frame_reference_sha256']}`
- 参考图角色：`geometry_only_not_theme_or_ornament`
- A 景窗口：`{','.join(str(value) for value in package_spec['frame_story_box'])}`

故事框参考图只锁定占位、16:9 开口比例和实用边框厚度。不得照抄参考图的羊角、祥云、道具、配色或任何旧故事装饰；必须按当前故事主题重新设计。

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
- 把“封面/底板参考”作为固定包装语言参考：沿用其标题层级、上下分区和醒目程度，但不要照搬与本故事无关的角色、文案或装饰。
- 视觉元素不要套固定模板，也不要每期重复使用同一批装饰。请根据本期故事内容、故事类型、情绪和目标年龄自行判断画面语言，允许自由发挥；但不能杂乱，不能遮挡真实视频区域。
- 两张 3:4 发布底板必须在 Codex 原生生图结果中直接包含下面“可见内容”列出的文字；不要生成空白牌匾/面板后再用本地代码补字。
- 所有文字必须尽量准确；如果原生生图文字严重错误，重新生成，不要用 Pillow 或其他本地代码后期修字。
- 底板整体要清爽、直接、信息优先。主账号尤其要简洁，宝库号可以保留更丰富的商品资料包包装感；两者都不要把装饰、徽章、花纹、角色和道具堆满画面。
- 中间 16:9 视频安全区必须刚好处在 3:4 竖屏画布正中间：x=0, y=416, w=1080, h=608；上方和下方可视包装区各 416px，高度和面积完全相等。
- 主账号顶部只放故事类型和故事标题，不放时长、适龄段、品牌字或资料包信息；风格按故事类型轻量适配，例如历史故事可偏典雅，儿童童话可更可爱，但只做简单点缀。
- 类型适配必须克制且明确：童话/动物故事可圆润可爱；民间、成语、神话和历史故事使用清雅中国绘本气质、传统色与少量纹样，禁止通用塑料 3D 装饰或网游仙侠风。
- 主账号底部只放完整版时长、适合年龄和固定适用说明，避免商品资料清单式堆叠。
- 底板不得出现绵羊姐姐、羊头、小羊、卡通羊、人偶或任何人物/动物吉祥物形象；除非故事本身需要，避免无关角色或动物进入发布包装。
- 底板必须拆成 2304x888 顶部源图和 2304x888 底部源图分别生成，再由程序夹入固定 16:9 视频空挡。不要让 imagegen 直接生成完整 1080x1440 底板，因为它容易把安全区画错。Pillow 只负责缩放、拼接、裁切和尺寸整理，不负责生成或修正文案。
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
- 顶部源图：2304x888，保存到 `{output_paths['main_plate_top']}`。只呈现“{story_type}”和“《{story_name}》”，标题是主视觉，故事类型作为小标题或牌匾；不要放时长、适龄段、资料清单、品牌字或联系方式。
- 中间视频安全区：1080x608，位置是 x=0, y=416, w=1080, h=608，也就是 y=416 到 y=1024。这里不是创作区，最终拼接时填纯色空白，不要使用 imagegen 生成内容。
- 底部源图：2304x888，保存到 `{output_paths['main_plate_bottom']}`。做一个简洁信息栏，清楚写“完整版时长：{duration_text}”和“适合年龄：{age_range or '按本期设定'}”；信息栏下方写“适用于朗诵比赛、故事表演、少儿口才、技能比拼”。只允许少量主题点缀。
- 最终整图：把 1080x416 顶部源图 + 1080x608 纯色空白 + 1080x416 底部源图垂直拼接，保存到 `{output_paths['main_plate']}`。最终中间安全区必须一整条横向打穿，没有人物、装饰、边框、插画或文字。

## 2. 宝库号 3:4 发布底板

最终交付一张 1080x1440 的竖屏底板，用于承载“有人声、有字幕、有配乐的背景成片”。但不要直接生成整张底板，必须按同样三段式制作。

可见内容：
- 主标题：《{story_name}》
- 顶部时长：{duration_text}
- 底部适龄：适合年龄 {age_range or '按本期设定'}
- 底部资料包信息两行：背景视频 + PPT + 配乐；文稿 + 示范视频 + 朗读标注

硬性版式：
- 顶部源图：2304x888，保存到 `{output_paths['library_plate_top']}`。保留相对丰富的商品包装感，可放故事标题、故事类型和时长，排版可比主账号更热闹，但不要压入中间安全区。
- 中间视频安全区：1080x608，位置是 x=0, y=416, w=1080, h=608，也就是 y=416 到 y=1024。最终拼接时填纯色空白。
- 底部源图：2304x888，保存到 `{output_paths['library_plate_bottom']}`。保留较丰富的资料包信息区，重点写“适合年龄 {age_range or '按本期设定'}”“背景视频 + PPT + 配乐”“文稿 + 示范视频 + 朗读标注”；“示范视频”要排在“朗读标注”前面。
- 最终整图保存到 `{output_paths['library_plate']}`，中间安全区必须干净留空。

## 3. 主账号 16:9 无框背景图

生成 1920x1080 背景图，用于放置左侧故事视频框和右侧真人绿幕人物。背景图本身只提供环境氛围，不能自带故事框。

要求：
- 背景与本期故事主题强相关，必须开阔、连续、有明确场景感，清晰、有可辨识细节但不能喧宾夺主。
- 背景必须是同一个横向展开的完整场景，不允许左右分区、半边纸面、半边色块、明显留白面板、舞台幕布感或“半屏设计”。
- 控制细节密度，避免拥挤纹样、密集道具、强装饰、复杂建筑群、标题栏或可被误认为视频窗口的矩形容器。
- 右侧要给真人留出干净空间，左侧要给故事框留出空间；只能通过构图、开阔景别、自然光线和元素疏密来实现，不能靠全局高斯模糊、独立面板或空白块。
- 交付的是可复用清晰原图，禁止预先虚化、柔焦、压暗或添加暗角；Demo 若需要虚化，必须在合成阶段生成私有派生图。
- 不要出现真实人物、字幕、水印、平台 UI、Logo、角标、徽章或右上角/四角装饰贴纸。
- 不要画任何故事视频框、边框、牌匾、标题栏、空白卡片或可被误认为视频窗口的矩形容器；故事框会由后期透明 PNG 叠加。

## 4. 统一故事框源图

生成一张 1920x1080 的统一故事框源图，供后处理导出一个透明故事框。A/B 景复用同一个框，具体缩放与摆放放到发布视频合成环节处理。

要求：
- 把 `{package_spec['frame_reference_asset']}` 作为 imagegen 的几何参考输入，只继承占位、16:9 开口比例和实用边框厚度；不得继承其主题造型或装饰。
- 根据《{story_name}》的具体人物、道具、场景和情绪从零设计一套新的框体。禁止只在旧框上换花、换角标，禁止保留旧故事的羊角、祥云、书画道具、动物食物或其他主题元素。
- 整张 1920x1080 源图必须使用完全一致的纯洋红 `#FF00FF` 底色，包括框外区域和框内开口。禁止棋盘格、白底、渐变底、透明预览或直接透明输出。
- 透明 PNG 只能由上述 ImageGen 洋红源图做固定色键/Alpha 后处理得到；后处理不得重新设计、增删或移动框体元素，也不得额外裁出一个矩形透明洞。
- 合成逻辑是“故事视频在下，透明故事框在上”，由不规则框体自然遮挡视频边缘；不要把内缘硬切成直角矩形。
- 故事框必须是完整闭合的四边矩形框：上、下、左、右四条边和四个角都要完整在画布内，不能生成 L 形、缺左边、缺底边、局部出画或被裁断的框。
- 框体必须围绕 A 景窗口 `{story_box}`，窗口四周都应能看到框体；不要把右边框放到远离窗口的位置。
- 装饰元素围绕窗口，不能遮挡窗口。
- 保留 Logo 安全区，后续会叠加 `{logo}`。
- 源图按 A 景窗口构图（参考 `{story_box}`），确保框细节完整可读。
- 框体必须由 ImageGen 直接生成或参考图编辑为栅格位图。禁止用 SVG、HTML、Canvas、Pillow、FFmpeg drawbox/drawtext 或其他程序绘图方式制作框体。
- 程序只负责对纯洋红源图去底和尺寸整理，不能重新设计框体。

## 5. A/B 使用规则

- 第 12 步只交付一个统一透明故事框。
- A 景窗口参考 `{story_box}`。
- B 景窗口参考 `{b_story_box}`，但 B 景不在主题素材阶段另做一张框；发布视频合成环节复用统一框并调整缩放与摆放。

## 交付检查

保存后请检查：
- 顶部源图都是 2304x888，底部源图都是 2304x888；合成时才缩放到 1080x416。
- 两张最终底板都是 1080x1440，由 1080x416 顶部 + 1080x608 纯色空白安全区 + 1080x416 底部拼接而成。
- 中间视频安全区 x=0, y=416, w=1080, h=608 没有文字/装饰/人物，且没有被上/下装饰压入；视频区中心点必须等于整张 3:4 画布中心点。
- 背景图是 1920x1080，且不能自带故事框。
- 故事框源图为单一设计；只导出一个 1920x1080 透明 PNG，A/B 在发布视频合成环节复用。
- 文件已经保存到任务书指定路径。
- `main_package_generation_receipt.json` 使用 `story-main-package-generation/v5`，必须声明 imagegen 同时接收了主账号包装参考图、故事框几何参考图和固定 Prompt，绑定任务书、四张上下包装图、主账号背景、故事框洋红源图与透明框 SHA-256，逐项记录主/宝库号 OCR 观察值并确认示例故事名/时长/年龄/人物没有泄漏；`attempt_count` 为 1–3。
- 回执写 `generation_methods`：主账号上下图为 `imagegen_reference_edit`，宝库号上下图为 `imagegen_raster` 或 `imagegen_reference_edit`，故事框源图为 `imagegen_raster` 或 `imagegen_reference_edit`，透明框只能为 `raster_alpha_postprocess`；并明确写 `svg_used=false`。
- 回执必须写 `story_identity`，并在 `account_role_review` 中确认：参考图的简洁层级被保留、主账号没有做成宝库号资料包商品页、信息层级清楚，并附具体 evidence。
- 回执必须写 `background_clean_review`，其中 `passed`、`not_preblurred`、`no_vignette`、`no_logo_badge_or_corner_emblem`、`no_text_or_watermark` 均为 true，并附具体 evidence。
- 回执必须写 `frame_design_review`，其中 `passed`、`frame_reference_attached`、`geometry_preserved`、`current_story_redesign`、`no_reference_theme_leak`、`solid_magenta_source` 均为 true，并附具体 evidence。
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


def main_package_receipt_issues(paths: ProjectPaths) -> list[str]:
    """Verify ImageGen reference/prompt lineage before packaging can proceed."""

    theme_dir = paths.release / "theme_assets"
    spec_path = theme_dir / "main_package_spec.json"
    receipt_path = theme_dir / "main_package_generation_receipt.json"
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["main_package_spec_missing_or_invalid"]
    issues: list[str] = []
    try:
        reference = load_main_package_reference()
    except (OSError, ValueError) as exc:
        return [str(exc)]
    if spec.get("reference_role") != "main_vertical_package":
        issues.append("main_package_reference_role_invalid")
    if spec.get("reference_asset") != reference["asset_path"] or spec.get("reference_sha256") != reference["sha256"]:
        issues.append("main_package_reference_binding_mismatch")
    expected_template_sha = hashlib.sha256(main_package_fixed_prompt_template().encode("utf-8")).hexdigest()
    if spec.get("fixed_prompt_template_sha256") != expected_template_sha:
        issues.append("main_package_fixed_prompt_stale")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [*issues, "main_package_generation_receipt_missing_or_invalid"]
    issues.extend(main_package_generation_receipt_issues(
        spec,
        receipt,
        {
            "top_plate": theme_dir / "main_release_plate_top.png",
            "bottom_plate": theme_dir / "main_release_plate_bottom.png",
            "library_top_plate": theme_dir / "library_release_plate_top.png",
            "library_bottom_plate": theme_dir / "library_release_plate_bottom.png",
            "main_background": theme_dir / "main_background_16x9.png",
            "story_frame_source": theme_dir / "story_frame_source.png",
            "story_frame_a": theme_dir / "story_frame_a.png",
        },
    ))
    return sorted(set(issues))


def main_package_generation_receipt_issues(
    spec: Mapping[str, Any],
    receipt: Mapping[str, Any],
    expected_outputs: Mapping[str, Path],
) -> list[str]:
    """Validate current-story lineage and account-role identity for release art."""

    issues: list[str] = []
    if receipt.get("schema_version") != MAIN_PACKAGE_RECEIPT_SCHEMA_VERSION:
        issues.append("main_package_generation_receipt_schema_invalid")
    if receipt.get("imagegen_reference_attached") is not True:
        issues.append("main_package_reference_not_attached_to_imagegen")
    if receipt.get("svg_used") is not False:
        issues.append("main_package_svg_or_unverified_vector_workflow_forbidden")
    generation_methods = receipt.get("generation_methods") if isinstance(receipt.get("generation_methods"), Mapping) else {}
    allowed_methods = {
        "top_plate": {"imagegen_reference_edit"},
        "bottom_plate": {"imagegen_reference_edit"},
        "library_top_plate": {"imagegen_raster", "imagegen_reference_edit"},
        "library_bottom_plate": {"imagegen_raster", "imagegen_reference_edit"},
        "main_background": {"imagegen_raster", "imagegen_reference_edit"},
        "story_frame_source": {"imagegen_raster", "imagegen_reference_edit"},
        "story_frame_a": {"raster_alpha_postprocess"},
    }
    for key in expected_outputs:
        if key in allowed_methods and generation_methods.get(key) not in allowed_methods[key]:
            issues.append(f"main_package_generation_method_invalid:{key}")
    try:
        attempt_count = int(receipt.get("attempt_count") or 0)
    except (TypeError, ValueError):
        attempt_count = 0
    if not 1 <= attempt_count <= 3:
        issues.append("main_package_generation_attempt_count_invalid")
    for field in (
        "reference_asset", "reference_sha256", "fixed_prompt_template_sha256",
        "frame_reference_asset", "frame_reference_sha256", "frame_reference_role",
        "frame_story_box", "frame_source_background", "frame_source_mode", "frame_design_policy",
        "theme_request_path", "theme_request_sha256",
    ):
        if receipt.get(field) != spec.get(field):
            issues.append(f"main_package_receipt_binding_mismatch:{field}")
    request_path = Path(str(spec.get("theme_request_path") or "")).expanduser()
    if (
        not request_path.is_file()
        or hashlib.sha256(request_path.read_bytes()).hexdigest() != spec.get("theme_request_sha256")
    ):
        issues.append("main_package_theme_request_stale")
    expected_identity = {
        "story_type": str(spec.get("story_type") or ""),
        "story_title": str(spec.get("story_title") or ""),
    }
    if spec.get("story_identity") != expected_identity or receipt.get("story_identity") != expected_identity:
        issues.append("main_package_story_identity_mismatch")
    if spec.get("account_role") != "main_account_vertical_release":
        issues.append("main_package_account_role_invalid")
    role_review = receipt.get("account_role_review") if isinstance(receipt.get("account_role_review"), Mapping) else {}
    for field in (
        "passed", "reference_hierarchy_preserved", "main_not_library_product_packaging",
        "simple_information_hierarchy", "flat_simple_reference_adherence",
    ):
        if role_review.get(field) is not True:
            issues.append(f"main_package_account_role_review_failed:{field}")
    if not str(role_review.get("evidence") or "").strip():
        issues.append("main_package_account_role_review_evidence_missing")
    background_review = (
        receipt.get("background_clean_review")
        if isinstance(receipt.get("background_clean_review"), Mapping)
        else {}
    )
    for field in (
        "passed", "not_preblurred", "no_vignette",
        "no_logo_badge_or_corner_emblem", "no_text_or_watermark",
    ):
        if background_review.get(field) is not True:
            issues.append(f"main_package_background_clean_review_failed:{field}")
    if not str(background_review.get("evidence") or "").strip():
        issues.append("main_package_background_clean_review_evidence_missing")
    frame_review = (
        receipt.get("frame_design_review")
        if isinstance(receipt.get("frame_design_review"), Mapping)
        else {}
    )
    for field in (
        "passed", "frame_reference_attached", "geometry_preserved",
        "current_story_redesign", "no_reference_theme_leak", "solid_magenta_source",
    ):
        if frame_review.get(field) is not True:
            issues.append(f"main_package_frame_design_review_failed:{field}")
    if not str(frame_review.get("evidence") or "").strip():
        issues.append("main_package_frame_design_review_evidence_missing")
    outputs = receipt.get("outputs") if isinstance(receipt.get("outputs"), Mapping) else {}
    for key, path in expected_outputs.items():
        item = outputs.get(key) if isinstance(outputs.get(key), Mapping) else {}
        if not path.is_file():
            issues.append(f"main_package_output_missing:{key}")
        elif item.get("path") != str(path) or item.get("sha256") != hashlib.sha256(path.read_bytes()).hexdigest():
            issues.append(f"main_package_output_binding_mismatch:{key}")
    ocr = receipt.get("ocr_validation") if isinstance(receipt.get("ocr_validation"), Mapping) else {}
    if ocr.get("passed") is not True:
        issues.append("main_package_ocr_not_passed")
    expected_text = {
        "story_type": str(spec.get("story_type") or ""),
        "story_title": str(spec.get("story_title") or ""),
        "duration": str(spec.get("duration") or ""),
        "age_range": str(spec.get("age_range") or ""),
        "use_cases": str(spec.get("use_cases") or ""),
    }
    if ocr.get("expected") != expected_text or ocr.get("observed") != expected_text:
        issues.append("main_package_ocr_text_mismatch")
    leak = receipt.get("reference_content_leak_check") if isinstance(receipt.get("reference_content_leak_check"), Mapping) else {}
    if leak.get("passed") is not True or leak.get("leaked_items") not in ([], None):
        issues.append("main_package_reference_content_leak")
    if {"library_top_plate", "library_bottom_plate"} & set(expected_outputs):
        library_ocr = receipt.get("library_ocr_validation") if isinstance(receipt.get("library_ocr_validation"), Mapping) else {}
        expected_library_text = {
            "story_title": str(spec.get("story_title") or ""),
            "duration": str(spec.get("duration") or ""),
            "age_range": str(spec.get("age_range") or ""),
            "package_items": ["背景视频", "PPT", "配乐", "文稿", "示范视频", "朗读标注"],
        }
        if library_ocr.get("passed") is not True:
            issues.append("library_package_ocr_not_passed")
        if library_ocr.get("expected") != expected_library_text or library_ocr.get("observed") != expected_library_text:
            issues.append("library_package_ocr_text_mismatch")
    return sorted(set(issues))


def write_main_package_reference_comparison(paths: ProjectPaths) -> Path:
    """Place the immutable reference beside the generated top/bottom panels.

    This is evidence for the existing visual review, not another review stage.
    It makes excess depth, scenery and decoration immediately visible before
    the reviewer either accepts the pair or requests one bounded regeneration.
    """

    theme_dir = paths.release / "theme_assets"
    reference = Path(load_main_package_reference()["asset_path"])
    sources = [
        ("REFERENCE", reference),
        ("GENERATED TOP", theme_dir / "main_release_plate_top.png"),
        ("GENERATED BOTTOM", theme_dir / "main_release_plate_bottom.png"),
    ]
    missing = [str(path) for _label, path in sources if not path.is_file()]
    if missing:
        raise FileNotFoundError("主账号包装参考对照缺少文件：" + "、".join(missing))
    cell_width, cell_height, header = 720, 520, 54
    sheet = Image.new("RGB", (cell_width * len(sources), cell_height + header), "#F2F2F2")
    draw = ImageDraw.Draw(sheet)
    for index, (label, path) in enumerate(sources):
        with Image.open(path) as opened:
            image = ImageOps.contain(opened.convert("RGB"), (cell_width - 24, cell_height - 24))
        x = index * cell_width + (cell_width - image.width) // 2
        y = header + (cell_height - image.height) // 2
        sheet.paste(image, (x, y))
        draw.text((index * cell_width + 18, 18), label, fill="#111111")
    output = paths.status / "theme_assets" / "main_package_reference_comparison.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output)
    return output


def archive_invalid_theme_assets(paths: ProjectPaths, issues: list[str]) -> Path | None:
    """Move a stale generated asset family aside before a new ImageGen task.

    Keeping invalid files at the canonical target paths encourages a producer
    to treat existence as completion.  Archiving is recoverable and only
    touches known generated outputs; user inputs and unrelated theme files are
    left in place.
    """

    theme_dir = paths.release / "theme_assets"
    candidates = [theme_dir / name for name in THEME_ASSET_GENERATED_FILENAMES]
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = paths.status / "rejected" / "theme_assets" / f"{stamp}-{uuid.uuid4().hex[:8]}"
    archive.mkdir(parents=True, exist_ok=False)
    archived: list[dict[str, str]] = []
    for source in existing:
        target = archive / source.name
        shutil.move(str(source), str(target))
        archived.append({"source": str(source), "archived_to": str(target)})
    save_json(
        archive / "invalidation.json",
        {
            "schema_version": "story-theme-assets-invalidation/v1",
            "issues": sorted(set(issues)),
            "archived": archived,
        },
    )
    return archive


def qa_theme_assets(paths: ProjectPaths, assets: list[Path] | None = None, *, strict: bool = False) -> Path:
    from release_video import release_plate_integrity_issues, story_frame_integrity_issues

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
            theme_dir / "story_frame_source.png",
            theme_dir / "story_frame_a.png",
        ]
    expected_sizes = {
        "main_release_plate_top.png": MAIN_PACKAGE_PANEL_SIZE,
        "main_release_plate_bottom.png": MAIN_PACKAGE_PANEL_SIZE,
        "main_release_plate.png": (1080, 1440),
        "library_release_plate_top.png": MAIN_PACKAGE_PANEL_SIZE,
        "library_release_plate_bottom.png": MAIN_PACKAGE_PANEL_SIZE,
        "library_release_plate.png": (1080, 1440),
        "release_plate_placeholder.png": (1080, 1440),
        "main_background_16x9.png": (1920, 1080),
        "story_frame_source.png": (1920, 1080),
        "story_frame_a.png": (1920, 1080),
    }
    expected_windows = {
        "story_frame_a.png": story_box,
    }
    comparison = write_main_package_reference_comparison(paths)
    rows = [{
        "index": "0",
        "file": str(comparison),
        "status": "review",
        "notes": "现有视觉审核必须对照参考判断：平面、简洁、大留白、低装饰；过度3D场景化则定向重生",
    }]
    issues = [f"- 主账号包装链路：{issue}" for issue in main_package_receipt_issues(paths)]
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
                    notes.extend(release_plate_integrity_issues(image, video_box))
                if path.name in expected_windows:
                    notes.extend(qa_story_frame_image(image, expected_windows[path.name]))
                    notes.extend(story_frame_integrity_issues(image, expected_windows[path.name]))
                if path.name == "story_frame_source.png":
                    notes.extend(qa_story_frame_source(image))
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
    frame = normalize_magenta_frame_source(frame)
    frame_rgba = chroma_to_alpha(frame)
    frame_rgba = fit_frame_to_window(frame_rgba, window)
    output.parent.mkdir(parents=True, exist_ok=True)
    frame_rgba.save(output)


def fit_frame_to_window(frame: Image.Image, window: tuple[int, int, int, int]) -> Image.Image:
    """Align a closed frame by its aperture so it actually masks video edges.

    The old implementation stretched the frame's outer alpha bounding box to a
    fixed rectangle.  That ignored the generated aperture and could leave a
    visible strip of background between a rounded inner edge and the video.
    The production contract is the opposite: the transparent aperture is the
    anchor, and the opaque frame must overlap the video on all four sides.
    """
    frame = frame.convert("RGBA")
    bbox = frame.getchannel("A").getbbox()
    if bbox is None:
        return frame
    x, y, width, height = window
    aperture = center_transparent_component_bbox(frame)
    if aperture is None:
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

    ax1, ay1, ax2, ay2 = aperture
    overlap_x = max(18, min(36, round(width * 0.028)))
    overlap_y = max(14, min(28, round(height * 0.035)))
    target_aperture = (
        x + overlap_x,
        y + overlap_y,
        x + width - overlap_x,
        y + height - overlap_y,
    )
    scale_x = (target_aperture[2] - target_aperture[0]) / max(1, ax2 - ax1)
    scale_y = (target_aperture[3] - target_aperture[1]) / max(1, ay2 - ay1)
    fitted = frame.resize(
        (max(1, round(frame.width * scale_x)), max(1, round(frame.height * scale_y))),
        Image.Resampling.LANCZOS,
    )
    paste_x = round(target_aperture[0] - ax1 * scale_x)
    paste_y = round(target_aperture[1] - ay1 * scale_y)
    out = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    out.alpha_composite(fitted, (paste_x, paste_y))
    return out


def center_transparent_component_bbox(frame: Image.Image, alpha_threshold: int = 8) -> tuple[int, int, int, int] | None:
    """Return the closed transparent aperture containing the canvas centre."""

    alpha = frame.convert("RGBA").getchannel("A")
    transparent = alpha.point(lambda value: 255 if value <= alpha_threshold else 0)
    center = (frame.width // 2, frame.height // 2)
    if transparent.getpixel(center) == 0:
        return None
    flooded = transparent.copy()
    ImageDraw.floodfill(flooded, center, 128, thresh=0)
    component = flooded.point(lambda value: 255 if value == 128 else 0)
    bbox = component.getbbox()
    if bbox is None:
        return None
    if bbox[0] <= 0 or bbox[1] <= 0 or bbox[2] >= frame.width or bbox[3] >= frame.height:
        return None
    return bbox


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


def connected_magenta_background_mask(image: Image.Image) -> Image.Image:
    """Select only magenta connected to the opening or canvas exterior.

    Color-keying every magenta-like pixel damages legitimate pink/purple story
    ornaments.  The production source has two background components: the
    closed centre aperture and the exterior around the frame.  Flooding from
    those known seeds removes the key background while preserving enclosed
    theme colors.
    """

    rgb = image.convert("RGB")
    candidate = Image.new("L", rgb.size, 0)
    source_pixels = rgb.load()
    candidate_pixels = candidate.load()
    for py in range(rgb.height):
        for px in range(rgb.width):
            red, green, blue = source_pixels[px, py]
            if red >= 170 and blue >= 160 and green <= 120 and ((red + blue) / 2 - green) >= 85:
                candidate_pixels[px, py] = 255
    seeds = [
        (rgb.width // 2, rgb.height // 2),
        (0, 0), (rgb.width - 1, 0), (0, rgb.height - 1), (rgb.width - 1, rgb.height - 1),
    ]
    for seed in seeds:
        if candidate.getpixel(seed) == 255:
            ImageDraw.floodfill(candidate, seed, 128, thresh=0)
    return candidate.point(lambda value: 255 if value == 128 else 0)


def normalize_magenta_frame_source(image: Image.Image) -> Image.Image:
    """Normalize only the connected chroma background to exact #FF00FF."""

    rgb = image.convert("RGB")
    mask = connected_magenta_background_mask(rgb)
    normalized = rgb.copy()
    normalized.paste(Image.new("RGB", rgb.size, (255, 0, 255)), mask=mask)
    return normalized


def chroma_to_alpha(image: Image.Image, key: tuple[int, int, int] = (255, 0, 255), threshold: int = 72) -> Image.Image:
    del key, threshold  # Compatibility parameters; topology now defines the safe key region.
    normalized = normalize_magenta_frame_source(image)
    mask = connected_magenta_background_mask(normalized)
    rgba = normalized.convert("RGBA")
    alpha = Image.new("L", rgba.size, 255)
    alpha.paste(0, mask=mask)
    rgba.putalpha(alpha)
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
    aperture = center_transparent_component_bbox(image)
    if aperture is None:
        notes.append("故事框没有闭合的中央透明开口")
    else:
        ax1, ay1, ax2, ay2 = aperture
        if ax1 < x or ay1 < y or ax2 > x + width or ay2 > y + height:
            notes.append("故事框内开口超出视频窗口，圆角/异形边缘可能露出背景缝")
        overlap = (ax1 - x, ay1 - y, x + width - ax2, y + height - ay2)
        if min(overlap) < 8:
            notes.append("故事框压住视频边缘的内侧遮挡带不足")
        if max(overlap) > max(72, round(min(width, height) * 0.16)):
            notes.append("故事框内侧遮挡过厚，侵占故事画面")
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
        if visible / pixels < 0.08:
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


def qa_story_frame_source(image: Image.Image) -> list[str]:
    """Validate the fixed ImageGen -> magenta source -> keying contract."""

    notes: list[str] = []
    if image.size != (1920, 1080):
        notes.append("故事框洋红源图必须规范化为1920x1080")
    rgb = image.convert("RGB")
    exact_key = Image.new("L", rgb.size, 0)
    source_pixels = rgb.load()
    key_pixels = exact_key.load()
    exact_count = 0
    for py in range(rgb.height):
        for px in range(rgb.width):
            if source_pixels[px, py] == (255, 0, 255):
                key_pixels[px, py] = 255
                exact_count += 1
    if exact_count / max(1, rgb.width * rgb.height) < 0.40:
        notes.append("故事框源图没有使用足量的纯#FF00FF洋红底")
    for point in (
        (rgb.width // 2, rgb.height // 2),
        (0, 0), (rgb.width - 1, 0), (0, rgb.height - 1), (rgb.width - 1, rgb.height - 1),
    ):
        if rgb.getpixel(point) != (255, 0, 255):
            notes.append("故事框源图的中央开口和画布外部必须统一为纯#FF00FF")
            break
    flooded = exact_key.copy()
    center = (rgb.width // 2, rgb.height // 2)
    if flooded.getpixel(center) == 255:
        ImageDraw.floodfill(flooded, center, 128, thresh=0)
        aperture = flooded.point(lambda value: 255 if value == 128 else 0).getbbox()
        if aperture is None or aperture[0] <= 0 or aperture[1] <= 0 or aperture[2] >= rgb.width or aperture[3] >= rgb.height:
            notes.append("故事框主体没有闭合，中央洋红开口与外部底色连通")
    else:
        notes.append("故事框中央不是纯洋红开口")
    return notes


def qa_main_background_image(image: Image.Image) -> list[str]:
    notes: list[str] = []
    if image.mode not in {"RGB", "RGBA"}:
        notes.append("背景图色彩模式异常")
    sample = image.convert("RGB").resize((96, 54))
    stat = ImageStat.Stat(sample)
    if max(stat.stddev) < 5:
        notes.append("背景图过于单调，疑似占位图")
    detail_sample = ImageOps.fit(image.convert("RGB"), (320, 180), method=Image.Resampling.LANCZOS)
    edges = detail_sample.convert("L").filter(ImageFilter.FIND_EDGES).crop((3, 3, 317, 177))
    edge_mean = ImageStat.Stat(edges).mean[0]
    if edge_mean < 3.0:
        notes.append("背景图细节过低，疑似已经整体预模糊")
    micro = ImageChops.difference(
        detail_sample,
        detail_sample.filter(ImageFilter.GaussianBlur(1.2)),
    ).convert("L").crop((3, 3, 317, 177))
    micro_mean = ImageStat.Stat(micro).mean[0]
    histogram = micro.histogram()
    micro_occupancy = sum(histogram[24:]) / max(1, sum(histogram))
    if edge_mean > 42.0 or micro_mean > 12.5 or micro_occupancy > 0.14:
        notes.append("背景图高频细节过密，草叶/碎花/颗粒在视频压缩后容易形成噪点")
    return notes


def configured_keying_backend() -> str:
    release_defaults = load_config().get("release_defaults", {})
    backend = str(release_defaults.get("preferred_keyer") or release_defaults.get("keyer") or "colorkey")
    if backend not in {"colorkey", "chromakey", "rvm"}:
        raise ValueError(f"不支持的 preferred_keyer：{backend}")
    return backend


def auto_keying(
    project_dir: Path,
    greenscreen: Path | None = None,
    *,
    backend: str | None = None,
    rvm_start_seconds: float = 0.0,
    rvm_duration_seconds: float | None = None,
) -> Path:
    paths = project_paths(project_dir)
    manifest = detect_project_assets(paths.root)
    raw_video = greenscreen or manifest["inputs"].get("greenscreen_video")
    video = Path(raw_video).expanduser() if raw_video else Path("__missing_greenscreen__")
    if not video.exists():
        raise FileNotFoundError("未找到绿幕视频，无法自动生成抠像参数。")
    selected_backend = str(backend or load_config().get("release_defaults", {}).get("keyer") or "colorkey")
    if selected_backend not in {"colorkey", "chromakey", "rvm"}:
        raise ValueError(f"不支持的抠像后端：{selected_backend}")
    release_defaults = load_config().get("release_defaults", {})
    output_dir = paths.release / "keying"
    frames_dir = output_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    duration = safe_duration(video)
    sample_times = keying_sample_timestamps(duration)
    frames = []
    for idx, ts in enumerate(sample_times, start=1):
        frame = frames_dir / f"sample_{idx:02d}.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{ts:.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(frame)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        frames.append(frame)
    color, green_variation = sample_green_across_frames(frames)
    person_crop = detect_person_crop(frames, color)
    standing_frame, gesture_frame = select_keying_representative_frames(frames, color)
    standing_person_bbox = detect_person_crop([standing_frame], color)
    standing_frame_seconds = sample_times[frames.index(standing_frame)]
    gesture_frame_seconds = sample_times[frames.index(gesture_frame)]
    base_similarity = 0.075 if green_variation < 10 else (0.095 if green_variation < 24 else 0.115)
    # Search a bounded conservative neighbourhood around the measured centre.
    # The centre is the default recommendation; any stricter/aggressive value
    # remains available for independent visual review instead of being silently
    # hard-coded into the generated preset.
    color_candidates = [
        {
            "id": f"s{similarity:.3f}_b{blend:.3f}",
            "similarity": round(similarity, 3),
            "blend": round(blend, 3),
        }
        for similarity in (max(0.04, base_similarity - 0.02), base_similarity, min(0.16, base_similarity + 0.02))
        for blend in (0.02, 0.04, 0.06)
    ]
    color_recommended = min(
        color_candidates,
        key=lambda item: abs(item["similarity"] - base_similarity) + abs(item["blend"] - 0.04),
    )
    candidates = list(color_candidates)
    recommended = color_recommended
    rvm_candidates: list[dict[str, Any]] = []
    if selected_backend == "rvm":
        downsample_ratio = float(release_defaults.get("rvm_downsample_ratio", 0.4))
        configured_chokes = tuple(
            int(value)
            for value in release_defaults.get(
                "rvm_alpha_choke_candidates", RVM_ALPHA_CHOKE_CANDIDATES
            )
        )
        if (
            not configured_chokes
            or any(value not in RVM_ALPHA_CHOKE_CANDIDATES for value in configured_chokes)
            or RVM_ALPHA_CHOKE_PIXELS not in configured_chokes
        ):
            raise ValueError("rvm_alpha_choke_candidates 必须包含 1，且只能使用 0/1/2")
        rvm_candidates = [
            {
                "id": f"rvm_mobilenetv3_s{downsample_ratio:.3f}_c{choke_pixels}",
                "backend": "rvm",
                "downsample_ratio": downsample_ratio,
                "alpha_choke_pixels": choke_pixels,
            }
            for choke_pixels in configured_chokes
        ]
        # One pixel is only the conservative starting recommendation learned
        # from the Canary.  The current source's visual evidence can select
        # 0/1/2 px before the preset is locked.
        candidates = rvm_candidates
        recommended = next(
            item for item in rvm_candidates
            if int(item["alpha_choke_pixels"]) == RVM_ALPHA_CHOKE_PIXELS
        )
    candidate_ids = {str(item["id"]) for item in candidates}
    if str(recommended["id"]) not in candidate_ids:
        raise RuntimeError("自动抠像推荐候选未出现在 keying_search candidates 中")
    candidate_sheet = output_dir / "keying_candidates.jpg"
    if selected_backend == "rvm":
        render_keying_candidate_sheet(
            standing_frame,
            gesture_frame,
            color,
            color_candidates,
            output_dir / "keying_color_fallback_candidates.jpg",
        )
    else:
        render_keying_candidate_sheet(
            standing_frame,
            gesture_frame,
            color,
            color_candidates,
            candidate_sheet,
        )
    candidate_detail_sheet = output_dir / "keying_candidates_detail.jpg"
    evidence_dir = output_dir / "evidence" / str(recommended["id"])
    search_path = output_dir / "keying_search.json"
    machine_qa_path = output_dir / "keying_machine_qa.json"
    selected_candidate_file = evidence_dir / "selected_candidate.png"
    evidence_manifest_path = evidence_dir / "evidence_manifest.json"
    rvm_fields: dict[str, Any] = {}
    if selected_backend == "rvm":
        from rvm_keying import (
            ensure_official_rvm_model,
            ensure_rvm_foreground_video,
            file_sha256 as rvm_file_sha256,
        )

        rvm_width = int(release_defaults.get("rvm_input_width", 1920))
        rvm_height = int(release_defaults.get("rvm_input_height", 1080))
        rvm_fps = int(release_defaults.get("rvm_output_fps", 25))
        configured_model = str(release_defaults.get("rvm_model_path") or "").strip()
        model_path = Path(configured_model).expanduser() if configured_model else output_dir / "models/rvm_mobilenetv3_fp32.onnx"
        if not model_path.is_absolute():
            model_path = (ROOT / model_path).resolve()
        model_path = ensure_official_rvm_model(model_path)
        configured_runtime = str(release_defaults.get("rvm_runtime_path") or "").strip()
        runtime_path = Path(configured_runtime).expanduser() if configured_runtime else None
        if runtime_path is not None and not runtime_path.is_absolute():
            runtime_path = (ROOT / runtime_path).resolve()
        rvm_dir = output_dir / "rvm"
        rvm_foreground = rvm_dir / "presenter_rvm_alpha.webm"
        rvm_receipt = rvm_dir / "rvm_keying_receipt.json"
        ensure_rvm_foreground_video(
            video,
            rvm_foreground,
            rvm_receipt,
            model_path=model_path,
            runtime_path=runtime_path,
            width=rvm_width,
            height=rvm_height,
            fps=rvm_fps,
            downsample_ratio=float(recommended["downsample_ratio"]),
            start_seconds=rvm_start_seconds,
            duration_seconds=rvm_duration_seconds,
        )
        source_width, source_height = Image.open(frames[0]).size

        def scale_bbox(box: list[int]) -> list[int]:
            x, y, width, height = box
            return [
                round(x * rvm_width / source_width),
                round(y * rvm_height / source_height),
                round(width * rvm_width / source_width),
                round(height * rvm_height / source_height),
            ]

        person_crop = scale_bbox(person_crop)
        standing_person_bbox = scale_bbox(standing_person_bbox)
        rvm_fields = {
            "rvm_model_path": str(model_path),
            "rvm_model_sha256": rvm_file_sha256(model_path),
            "rvm_runtime_path": str(runtime_path) if runtime_path is not None else "",
            "rvm_foreground_video": str(rvm_foreground),
            "rvm_foreground_sha256": rvm_file_sha256(rvm_foreground),
            "rvm_receipt_path": str(rvm_receipt),
            "rvm_receipt_sha256": rvm_file_sha256(rvm_receipt),
            "rvm_input_width": rvm_width,
            "rvm_input_height": rvm_height,
            "rvm_output_fps": rvm_fps,
            "rvm_downsample_ratio": float(recommended["downsample_ratio"]),
            "rvm_alpha_choke_pixels": int(recommended["alpha_choke_pixels"]),
            "rvm_source_start_seconds": float(rvm_start_seconds),
        }

        render_rvm_keying_candidate_sheet(
            rvm_foreground,
            max(0.0, standing_frame_seconds - rvm_start_seconds),
            max(0.0, gesture_frame_seconds - rvm_start_seconds),
            rvm_candidates,
            candidate_sheet,
            output_dir / "rvm_candidate_frames",
            base_settings={
                "keyer": "rvm",
                "person_grade": "natural",
                "person_beauty": "none",
                **rvm_fields,
            },
        )

    # Keep a separately named evidence artefact for downstream QA/review.  The
    # candidate renderer includes full-frame and edge-detail panels.
    shutil.copy2(candidate_sheet, candidate_detail_sheet)

    search = {
        "version": 2,
        "source_video": str(video),
        "source_sha256": sha256_file(video),
        "chroma_color": color,
        "green_variation": round(green_variation, 3),
        "standing_frame": str(standing_frame),
        "gesture_frame": str(gesture_frame),
        "candidates": candidates,
        "recommended_candidate": recommended["id"],
        "candidate_sheet": str(candidate_sheet),
        "candidate_detail_sheet": str(candidate_detail_sheet),
        "selected_candidate_file": str(selected_candidate_file),
        "machine_qa": str(machine_qa_path),
        "evidence_manifest": str(evidence_manifest_path),
        "detected_person_bbox": person_crop,
        "selection_policy": (
            "每条源素材独立抽取全片站立/大手势代表帧，再用连续帧循环记忆生成一次可复用 RVM Alpha 中间片；"
            "从该项目自己的中间片比较 0/1/2 像素内收，独立视觉审核选择兼顾发丝保留与色边清理的候选。"
            "人物检测框只作测量，任何镜别都不得据此裁切或缩放人物；A镜只允许按开场中性帧原尺寸定轴一次，禁止手势跟踪关键帧。"
            if selected_backend == "rvm"
            else "背景绿幕波动决定候选中心 similarity；默认保守中心候选（blend=0.04），其余候选只供独立视觉审核比较。人物检测框只作边界测量，任何镜别都不得据此裁切或缩放人物；A镜只允许按开场中性帧原尺寸定轴一次，禁止手势跟踪关键帧。站立与大手势双帧由独立视觉审核最终确认。"
        ),
        "visual_review_required": True,
        "visual_review_status": "pending",
    }
    save_json(search_path, search)
    # Default for current horizontal 16:9 green-screen shoots: presenter centered
    # in the source, full body visible, then placed in the right-side open area.
    preset = {
        "preset_version": "story-keying-preset/v2",
        "keyer": selected_backend,
        "chroma_color": color,
        "chroma_similarity": recommended.get("similarity", color_recommended["similarity"]),
        "chroma_blend": recommended.get("blend", color_recommended["blend"]),
        "person_crop": None,
        # Union bbox from standing + large-gesture samples is diagnostic only.
        # It must never become a production crop: a later gesture can extend
        # beyond any bounded sample set.
        "detected_person_bbox": person_crop,
        "person_grade": "natural",
        "person_beauty": "none" if selected_backend == "rvm" else "light",
        "person_height_ratio": 1.0,
        "person_x": 0,
        "person_y": 0,
        "bottom_margin": 0,
        "auto_selected": True,
        "source_video": str(video),
        "keying_search": str(search_path),
        "keying_candidate": recommended["id"],
        "visual_review_required": True,
        "visual_review_status": "pending",
        "presenter_initial_anchor_frame": str(standing_frame),
        "presenter_initial_anchor_frame_seconds": round(float(standing_frame_seconds), 3),
        "presenter_gesture_review_frame": str(gesture_frame),
        "presenter_gesture_review_frame_seconds": round(float(gesture_frame_seconds), 3),
        "presenter_initial_subject_bbox": standing_person_bbox,
        "presenter_initial_anchor_x": None,
        "person_layout_policy": "source-native-fixed-anchor/v2",
        "dynamic_repositioning": False,
        "gesture_overlap_policy": "allowed",
        "machine_qa": str(machine_qa_path),
        "evidence_manifest": str(evidence_dir / "evidence_manifest.json"),
        "selected_candidate_file": str(selected_candidate_file),
        **rvm_fields,
    }
    preset_path = output_dir / "keying_preset.json"
    save_json(preset_path, preset)
    selected_candidate_file, machine_qa_path, _evidence = write_evidence_assets(
        standing_frame,
        gesture_frame,
        chroma_color=color,
        similarity=float(recommended.get("similarity", color_recommended["similarity"])),
        blend=float(recommended.get("blend", color_recommended["blend"])),
        output_dir=evidence_dir,
        candidate_id=str(recommended["id"]),
        machine_qa_path=machine_qa_path,
        preset=preset,
        preset_path=preset_path,
    )
    search["selected_candidate_sha256"] = sha256_file(selected_candidate_file)
    save_json(search_path, search)
    sheet = output_dir / "keying_samples.jpg"
    render_contact_sheet(frames, sheet, "绿幕自动采样帧")
    manifest["outputs"]["keying_preset"] = str(preset_path)
    manifest["qa"]["keying_samples"] = str(sheet)
    manifest["qa"]["keying_candidates"] = str(candidate_sheet)
    manifest["qa"]["keying_candidate_detail"] = str(candidate_detail_sheet)
    manifest["qa"]["keying_search"] = str(search_path)
    manifest["qa"]["keying_machine_qa"] = str(machine_qa_path)
    manifest["qa"]["keying_evidence"] = str(evidence_dir / "evidence_manifest.json")
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


def sample_green_across_frames(frames: list[Path]) -> tuple[str, float]:
    samples: list[tuple[int, int, int]] = []
    for frame in frames:
        with Image.open(frame).convert("RGB") as image:
            width, height = image.size
            for x_ratio in (0.04, 0.12, 0.88, 0.96):
                for y_ratio in (0.08, 0.22, 0.45, 0.68):
                    rgb = image.getpixel((int(width * x_ratio), int(height * y_ratio)))
                    if rgb[1] > rgb[0] * 1.15 and rgb[1] > rgb[2] * 1.05:
                        samples.append(rgb)
    if not samples:
        return "0x00FF00", 30.0
    channels = list(zip(*samples))
    median = tuple(round(statistics.median(channel)) for channel in channels)
    variation = sum(statistics.pstdev(channel) for channel in channels) / 3
    return f"0x{median[0]:02X}{median[1]:02X}{median[2]:02X}", variation


def select_keying_representative_frames(frames: list[Path], chroma_color: str) -> tuple[Path, Path]:
    try:
        green = tuple(int(chroma_color[index : index + 2], 16) for index in (2, 4, 6))
    except Exception:
        green = (0, 255, 0)
    scored: list[tuple[float, float, Path]] = []
    for frame in frames:
        with Image.open(frame).convert("RGB") as image:
            sample = image.resize((160, 90), Image.Resampling.BILINEAR)
            points = [
                (x, y)
                for y in range(sample.height)
                for x in range(sample.width)
                if is_presenter_pixel(sample.getpixel((x, y)), green)
            ]
        if not points:
            continue
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        width_ratio = (max(xs) - min(xs) + 1) / 160
        area_ratio = ((max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1)) / (160 * 90)
        scored.append((width_ratio, area_ratio, frame))
    if not scored:
        return frames[0], frames[-1]
    standing = min(scored, key=lambda item: (item[0], item[1]))[2]
    gesture = max(scored, key=lambda item: (item[0], item[1]))[2]
    return standing, gesture


def render_rvm_keying_candidate_sheet(
    foreground_video: Path,
    standing_seconds: float,
    gesture_seconds: float,
    candidates: list[dict[str, Any]],
    output: Path,
    candidate_frames_dir: Path,
    *,
    base_settings: dict[str, Any],
) -> None:
    """Compare per-source RVM matte inset candidates using the real cache."""

    if not candidates:
        raise ValueError("RVM candidate list is empty")
    candidate_frames_dir.mkdir(parents=True, exist_ok=True)
    duration = safe_duration(foreground_video)
    maximum_timestamp = max(0.0, duration - 0.08)
    pose_times = (
        ("standing", min(maximum_timestamp, max(0.0, standing_seconds))),
        ("wide_gesture", min(maximum_timestamp, max(0.0, gesture_seconds))),
    )
    tile_width, tile_height = 960, 650
    full_size = (500, 270)
    detail_size = (420, 270)
    tiles: list[Image.Image] = []

    def review_panel(source: Image.Image, size: tuple[int, int]) -> Image.Image:
        rgba = source.convert("RGBA")
        rgba.thumbnail(size, Image.Resampling.LANCZOS)
        panel = Image.new("RGBA", size, (238, 235, 225, 255))
        draw = ImageDraw.Draw(panel)
        draw.rectangle((0, 0, size[0] // 2, size[1]), fill=(48, 47, 54, 255))
        x = (size[0] - rgba.width) // 2
        y = (size[1] - rgba.height) // 2
        panel.alpha_composite(rgba, (x, y))
        return panel.convert("RGB")

    def detail_crop(source: Image.Image, pose: str) -> Image.Image:
        rgba = source.convert("RGBA")
        bbox = rgba.getchannel("A").getbbox()
        if bbox is None:
            return rgba
        x1, y1, x2, y2 = bbox
        width = max(1, x2 - x1)
        height = max(1, y2 - y1)
        if pose == "standing":
            # Hair/head and shoulder boundary, where a too-aggressive inset is
            # easiest to spot.
            y2 = min(rgba.height, y1 + max(1, round(height * 0.46)))
            x1 = max(0, x1 - round(width * 0.12))
            x2 = min(rgba.width, x2 + round(width * 0.12))
        else:
            # Keep the widest gesture span so both hands remain visible.
            y1 = max(0, y1 - round(height * 0.08))
            y2 = min(rgba.height, y2 + round(height * 0.08))
        return rgba.crop((x1, y1, x2, y2))

    for candidate in candidates:
        settings = {**base_settings, "rvm_alpha_choke_pixels": int(candidate["alpha_choke_pixels"])}
        tile = Image.new("RGB", (tile_width, tile_height), (34, 32, 42))
        draw = ImageDraw.Draw(tile)
        draw.text(
            (14, 9),
            f"{candidate['id']}  Alpha 内收={candidate['alpha_choke_pixels']} px",
            fill=(245, 245, 245),
            font=load_font(21),
        )
        for row, (pose, timestamp) in enumerate(pose_times):
            rendered = candidate_frames_dir / f"{candidate['id']}_{pose}.png"
            render_cached_rvm_foreground_frame(
                foreground_video,
                timestamp,
                rendered,
                settings,
            )
            with Image.open(rendered) as image:
                full = review_panel(image, full_size)
                detail = review_panel(detail_crop(image, pose), detail_size)
            y = 48 + row * 296
            tile.paste(full, (8, y))
            tile.paste(detail, (526, y))
            label = "站立帧：整身 / 发丝肩线" if pose == "standing" else "大手势帧：整身 / 双手边缘"
            draw.text((14, y + 272), label, fill=(231, 219, 151), font=load_font(17))
        tiles.append(tile)

    sheet = Image.new("RGB", (len(tiles) * tile_width, 54 + tile_height), (238, 236, 232))
    draw = ImageDraw.Draw(sheet)
    draw.text(
        (18, 12),
        "RVM 当前素材逐案选参：同一时序 Alpha 缓存比较 0/1/2 px 内收；左半深底、右半浅底，兼看色边与发丝/手部保留",
        fill=(25, 25, 25),
        font=load_font(24),
    )
    for index, tile in enumerate(tiles):
        sheet.paste(tile, (index * tile_width, 54))
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, quality=94)


def render_keying_candidate_sheet(
    standing_frame: Path,
    gesture_frame: Path,
    chroma_color: str,
    candidates: list[dict[str, Any]],
    output: Path,
) -> None:
    try:
        green = tuple(int(chroma_color[index : index + 2], 16) for index in (2, 4, 6))
    except Exception:
        green = (0, 255, 0)
    # The old evidence sheet downscaled both frames to 300x180, which made
    # hair strands and hand silhouettes impossible to judge.  Each candidate
    # now includes a high-resolution full-frame preview plus a padded local
    # foreground zoom for both standing and wide-gesture samples.
    tile_width, tile_height = 960, 470
    full_size = (500, 282)
    zoom_size = (420, 282)
    tiles: list[Image.Image] = []

    def foreground_zoom(source: Image.Image, similarity: float, blend: float) -> Image.Image:
        width, height = source.size
        sample = source.resize((min(320, width), min(240, height)), Image.Resampling.BILINEAR)
        points = [
            (x, y)
            for y in range(sample.height)
            for x in range(sample.width)
            if is_presenter_pixel(sample.getpixel((x, y)), green)
        ]
        if points:
            sx, sy = width / sample.width, height / sample.height
            x1 = max(0, int(min(x for x, _ in points) * sx - width * 0.08))
            y1 = max(0, int(min(y for _, y in points) * sy - height * 0.10))
            x2 = min(width, int((max(x for x, _ in points) + 1) * sx + width * 0.08))
            y2 = min(height, int((max(y for _, y in points) + 1) * sy + height * 0.10))
            if x2 - x1 >= 8 and y2 - y1 >= 8:
                source = source.crop((x1, y1, x2, y2))
        source = source.copy()
        source.thumbnail(zoom_size, Image.Resampling.LANCZOS)
        source = preview_chromakey(source, green, similarity, blend)
        panel = Image.new("RGB", zoom_size, (48, 45, 56))
        panel.paste(source, ((zoom_size[0] - source.width) // 2, (zoom_size[1] - source.height) // 2))
        return panel

    for candidate in candidates:
        tile = Image.new("RGB", (tile_width, tile_height), (34, 32, 42))
        draw = ImageDraw.Draw(tile)
        draw.text(
            (12, 7),
            f"{candidate['id']}  similarity={candidate['similarity']:.3f}  blend={candidate['blend']:.3f}",
            fill=(245, 245, 245),
            font=load_font(20),
        )
        for row, frame in enumerate((standing_frame, gesture_frame)):
            with Image.open(frame).convert("RGB") as image:
                full_source = image.copy()
                full_source.thumbnail(full_size, Image.Resampling.LANCZOS)
                full = preview_chromakey(
                    full_source,
                    green,
                    float(candidate["similarity"]),
                    float(candidate["blend"]),
                )
                zoom_source = image.copy()
                zoom = foreground_zoom(
                    zoom_source,
                    float(candidate["similarity"]),
                    float(candidate["blend"]),
                )
            y = 42 + row * 210
            tile.paste(full, (8, y))
            tile.paste(zoom, (520, y))
            label = "站立帧" if row == 0 else "大手势帧"
            draw.text((516, y - 21), f"{label} 局部放大（发丝/手边缘）", fill=(230, 220, 150), font=load_font(16))
        tiles.append(tile)
    columns = 3
    rows = math.ceil(len(tiles) / columns)
    sheet = Image.new("RGB", (columns * tile_width, 54 + rows * tile_height), (238, 236, 232))
    draw = ImageDraw.Draw(sheet)
    draw.text(
        (18, 12),
        "抠像参数搜索：左=高分辨率整帧，右=发丝/手部边缘局部放大（独立视觉审核前仅供选参）",
        fill=(25, 25, 25),
        font=load_font(24),
    )
    for index, tile in enumerate(tiles):
        sheet.paste(tile, ((index % columns) * tile_width, 54 + (index // columns) * tile_height))
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, quality=92)


def preview_chromakey(
    image: Image.Image,
    green: tuple[int, int, int],
    similarity: float,
    blend: float,
) -> Image.Image:
    source = image.convert("RGB")
    rgba = Image.new("RGBA", source.size)
    src_pixels = source.load()
    dst_pixels = rgba.load()
    lower = max(1.0, similarity * 442)
    upper = max(lower + 1.0, (similarity + blend) * 442)
    for y in range(source.height):
        for x in range(source.width):
            r, g, b = src_pixels[x, y]
            distance = math.sqrt((r - green[0]) ** 2 + (g - green[1]) ** 2 + (b - green[2]) ** 2)
            green_dominant = g > r * 1.02 and g > b * 1.01
            if not green_dominant or distance >= upper:
                alpha = 255
            elif distance <= lower:
                alpha = 0
            else:
                alpha = round(255 * (distance - lower) / (upper - lower))
            dst_pixels[x, y] = (r, g, b, alpha)
    background = Image.new("RGB", source.size, (92, 73, 112))
    checker = ImageDraw.Draw(background)
    block = 24
    for y in range(0, source.height, block):
        for x in range(0, source.width, block):
            if (x // block + y // block) % 2:
                checker.rectangle((x, y, x + block - 1, y + block - 1), fill=(128, 111, 145))
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


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


def write_internal_agent_reports(project_dir: Path) -> dict[str, Path]:
    """Write internal-only cost, QA and exception reports under 99_项目状态."""
    paths = project_paths(project_dir)
    manifest = load_manifest(paths) or init_project(project_dir)
    agent = manifest.get("agent", {}) if isinstance(manifest.get("agent"), dict) else {}
    budget = agent.get("budget", {}) if isinstance(agent.get("budget"), dict) else {}
    entries = budget.get("entries", []) if isinstance(budget.get("entries"), list) else []
    provider_totals: dict[str, float] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != "settled":
            continue
        provider = str(entry.get("provider") or "未记录")
        provider_totals[provider] = round(provider_totals.get(provider, 0.0) + float(entry.get("actual_amount", 0.0)), 4)
    cost_json = paths.status / "成本报告.json"
    cost_payload = {
        "currency": budget.get("currency", "CNY"),
        "spent": float(budget.get("spent", 0.0)),
        "reserved": float(budget.get("reserved", 0.0)),
        "soft_limit": float(budget.get("soft_limit", 0.0)),
        "hard_limit": float(budget.get("hard_limit", 0.0)),
        "provider_totals": provider_totals,
        "entries": entries,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_json(cost_json, cost_payload)
    cost_md = paths.status / "成本报告.md"
    cost_lines = [
        "# 成本报告（内部）",
        "",
        f"- 已结算：¥{cost_payload['spent']:.2f}",
        f"- 预留中：¥{cost_payload['reserved']:.2f}",
        f"- 软/硬上限：¥{cost_payload['soft_limit']:.2f} / ¥{cost_payload['hard_limit']:.2f}",
        "",
        "## 供应商汇总",
        *(f"- {provider}：¥{amount:.2f}" for provider, amount in sorted(provider_totals.items())),
        "",
        "## 明细",
        "| 时间 | 标签 | 状态 | 预留 | 实际 | 供应商 | 请求编号 |",
        "| --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    if not provider_totals:
        cost_lines.insert(cost_lines.index("## 明细") - 1, "- 暂无已结算供应商费用")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cost_lines.append(
            f"| {entry.get('time', '')} | {str(entry.get('label', '')).replace('|', '/')} | {entry.get('status', '')} | "
            f"¥{float(entry.get('amount', 0.0)):.2f} | ¥{float(entry.get('actual_amount', 0.0)):.2f} | "
            f"{entry.get('provider', '')} | {entry.get('request_id', '')} |"
        )
    cost_md.write_text("\n".join(cost_lines) + "\n", encoding="utf-8")

    reviews: list[dict[str, Any]] = []
    review_files = sorted((paths.status / "reviews").glob("*_review.json")) if (paths.status / "reviews").exists() else []
    source_review = paths.status / "source_edit" / "source_edit_review.json"
    if source_review.exists():
        review_files.insert(0, source_review)
    for review_path in review_files:
        try:
            payload = json.loads(review_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            reviews.append({"name": review_path.stem, "path": str(review_path), "valid_json": False, "passed": False})
            continue
        reviews.append(
            {
                "name": review_path.stem,
                "path": str(review_path),
                "valid_json": True,
                "approved": bool(payload.get("approved")),
                "score": float(payload.get("score", 0)),
                "critical_errors": payload.get("critical_errors", []),
                "artifact_sha256": payload.get("artifact_sha256", ""),
                "passed": bool(payload.get("approved")) and float(payload.get("score", 0)) >= 85 and not payload.get("critical_errors"),
            }
        )
    qa_files = {
        key: str(value)
        for key, value in manifest.get("qa", {}).items()
        if value and Path(str(value)).exists()
    }
    qa_json = paths.status / "QA汇总.json"
    qa_payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reviews": reviews,
        "machine_qa": qa_files,
        "all_review_scores_pass": bool(reviews) and all(item.get("passed") for item in reviews),
    }
    save_json(qa_json, qa_payload)
    qa_md = paths.status / "QA汇总.md"
    qa_lines = [
        "# QA 汇总（内部）",
        "",
        "| 审核 | 通过 | 分数 | 关键错误 | 文件 |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for item in reviews:
        critical = "；".join(str(value) for value in item.get("critical_errors", []))
        qa_lines.append(
            f"| {item.get('name', '')} | {'✅' if item.get('passed') else '❌'} | {item.get('score', '')} | "
            f"{critical or '无'} | `{item.get('path', '')}` |"
        )
    if not reviews:
        qa_lines.append("| 尚无审核 | ❌ |  |  |  |")
    qa_lines.extend(["", "## 机器 QA", *(f"- {key}：`{value}`" for key, value in sorted(qa_files.items()))])
    qa_md.write_text("\n".join(qa_lines) + "\n", encoding="utf-8")

    stage_records = agent.get("stages", {}) if isinstance(agent.get("stages"), dict) else {}
    exceptions: list[dict[str, Any]] = []
    for name, record in stage_records.items():
        if not isinstance(record, dict):
            continue
        if record.get("status") in {"blocked", "failed", "cancelled", "retrying"} or int(record.get("attempts", 0)) > 1:
            exceptions.append(
                {
                    "stage": name,
                    "status": record.get("status", ""),
                    "attempts": int(record.get("attempts", 0)),
                    "message": record.get("message", ""),
                    "retry_reason": record.get("retry_reason", ""),
                }
            )
    exception_md = paths.status / "异常说明.md"
    exception_lines = [
        "# 异常说明（内部）",
        "",
        f"- 当前状态：{agent.get('status', 'pending')}",
        f"- 阻塞原因：{agent.get('blocked_reason') or '无'}",
        "",
        "## 阶段异常与重试",
    ]
    if exceptions:
        exception_lines.extend(
            f"- `{item['stage']}`：{item['status']}，尝试 {item['attempts']} 次；{item['retry_reason'] or item['message']}"
            for item in exceptions
        )
    else:
        exception_lines.append("- 无")
    external_blockers = agent.get("external_blockers", []) if isinstance(agent.get("external_blockers"), list) else []
    exception_lines.extend(["", "## 外部阻塞"])
    if external_blockers:
        for item in external_blockers:
            if not isinstance(item, dict):
                continue
            exception_lines.append(
                f"- `{item.get('kind', 'external')}`：{item.get('status', 'pending')}；"
                f"{item.get('message', '')}；恢复动作：{item.get('recovery_action', '')}"
            )
    else:
        exception_lines.append("- 无")
    exception_md.write_text("\n".join(exception_lines) + "\n", encoding="utf-8")

    manifest.setdefault("outputs", {}).update(
        {
            "internal_cost_report": str(cost_md),
            "internal_qa_summary": str(qa_md),
            "internal_exception_report": str(exception_md),
        }
    )
    write_manifest(paths, manifest)
    return {
        "cost_md": cost_md,
        "cost_json": cost_json,
        "qa_md": qa_md,
        "qa_json": qa_json,
        "exception_md": exception_md,
    }


def final_delivery(project_dir: Path, *, update_latest_episode: bool = False) -> Path:
    paths = project_paths(project_dir)
    manifest = detect_project_assets(paths.root)
    discover_outputs(paths, manifest)
    qa_steps = (
        (qa_images, "images"),
        (qa_videos, "videos"),
        (qa_release, "release"),
        (qa_product, "product"),
        (qa_publish, "publish"),
    )
    agent_job = bool(manifest.get("agent", {}).get("job_id"))
    native_story_run = (paths.status / "story_run.json").is_file()
    for qa_func, qa_key in qa_steps:
        existing_report = first_existing(manifest.get("qa", {}).get(qa_key))
        if (agent_job or native_story_run) and existing_report is not None:
            # Independent reviews bind the exact QA report bytes.  Both the
            # legacy Agent job and the Codex-native story_run ledger therefore
            # preserve an already-produced report during final delivery;
            # rewriting only checked_at would invalidate a valid bundle.
            continue
        try:
            qa_func(paths.root)  # type: ignore[arg-type]
        except Exception as exc:
            manifest.setdefault("qa_warnings", []).append(f"{qa_func.__name__}: {exc}")
    manifest = load_manifest(paths) or manifest
    discover_outputs(paths, manifest)
    internal_reports = write_internal_agent_reports(paths.root)
    manifest = load_manifest(paths) or manifest
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
    if manifest.get("agent", {}).get("job_id"):
        required_paths.extend([internal_reports["cost_md"], internal_reports["qa_md"], internal_reports["exception_md"]])
    missing_delivery = [path for path in required_paths if not path.exists()]
    review_failures: list[str] = []
    if manifest.get("agent", {}).get("job_id"):
        review_paths = {
            "source_edit_review": paths.status / "source_edit" / "source_edit_review.json",
            "story_images_review": paths.status / "reviews" / "story_images_review_review.json",
            "video_prompt_review": paths.status / "reviews" / "video_prompt_review.json",
            "video_review": paths.status / "reviews" / "video_review_review.json",
            "release_preview": paths.status / "reviews" / "release_preview_review.json",
            "release_video_review": paths.status / "reviews" / "release_video_review_review.json",
            "publish_package_review": paths.status / "reviews" / "publish_package_review_review.json",
            "product_annotation_review": paths.status / "reviews" / "product_annotation_review_review.json",
            "product_package_review": paths.status / "reviews" / "product_package_review_review.json",
        }
        review_artifacts = {
            "source_edit_review": Path(str(manifest.get("outputs", {}).get("source_edit_decisions", ""))),
            "story_images_review": paths.status / "reviews" / "story_images_bundle.json",
            "video_prompt_review": paths.status / "reviews" / "video_prompt_bundle.json",
            "video_review": paths.status / "reviews" / "video_bundle.json",
            "release_preview": paths.status / "reviews" / "release_preview_bundle.json",
            "release_video_review": paths.status / "reviews" / "release_video_bundle.json",
            "publish_package_review": paths.status / "reviews" / "publish_package_bundle.json",
            "product_annotation_review": paths.status / "reviews" / "product_annotation_bundle.json",
            "product_package_review": paths.status / "reviews" / "product_package_bundle.json",
        }
        if not manifest.get("outputs", {}).get("source_edit_decisions"):
            review_paths.pop("source_edit_review", None)
            review_artifacts.pop("source_edit_review", None)
        for name, review_path in review_paths.items():
            try:
                review = json.loads(review_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                review_failures.append(f"{name}: 缺少有效审核 JSON")
                continue
            if not review.get("approved") or float(review.get("score", 0)) < 85 or review.get("critical_errors"):
                review_failures.append(f"{name}: 未达到 85 分无关键错误门槛")
                continue
            artifact = review_artifacts.get(name)
            if artifact is None or not artifact.is_file():
                review_failures.append(f"{name}: 缺少被审核产物或 bundle")
                continue
            if review.get("artifact_sha256") != sha256_file(artifact):
                review_failures.append(f"{name}: 审核哈希与当前 artifact 不匹配")
                continue
            if artifact.name.endswith("_bundle.json") and not review_bundle_current_local(artifact):
                review_failures.append(f"{name}: bundle 中的产物已经变化")
        music_qa_path = paths.status / "qa_music_report.json"
        source_decisions = Path(str(manifest.get("outputs", {}).get("source_edit_decisions", "")))
        if source_decisions.is_file():
            source_qa_path = paths.status / "qa_source_report.json"
            try:
                source_qa = json.loads(source_qa_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                review_failures.append("source_qa: 缺少有效源视频 QA JSON")
            else:
                source_artifacts = source_qa.get("artifacts")
                source_current = isinstance(source_artifacts, dict) and bool(source_artifacts)
                required_source_artifacts = {"edit_decisions"}
                source_inputs = manifest.get("inputs", {}) if isinstance(manifest.get("inputs"), dict) else {}
                if source_inputs.get("greenscreen_video_original"):
                    required_source_artifacts.update({"source_video", "clean_video", "clean_audio"})
                if source_inputs.get("color_lut"):
                    required_source_artifacts.add("color_lut")
                if source_current and not required_source_artifacts.issubset(source_artifacts):
                    source_current = False
                if source_current:
                    for item in source_artifacts.values():
                        if not isinstance(item, dict):
                            source_current = False
                            break
                        artifact_path = Path(str(item.get("path", "")))
                        if not artifact_path.is_file() or item.get("sha256") != sha256_file(artifact_path):
                            source_current = False
                            break
                if not source_qa.get("passed") or not source_current:
                    review_failures.append("source_qa: 未通过或源视频/剪辑/LUT/清洁媒体哈希已变化")
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
        product_qa_path = paths.status / "qa_product_report.json"
        try:
            product_qa = json.loads(product_qa_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            review_failures.append("product_qa: 缺少有效资料包机器 QA JSON")
        else:
            product_artifacts = product_qa.get("artifacts")
            product_current = isinstance(product_artifacts, dict) and bool(product_artifacts)
            if product_current:
                for item in product_artifacts.values():
                    if not isinstance(item, dict):
                        product_current = False
                        break
                    artifact_path = Path(str(item.get("path", "")))
                    if not artifact_path.is_file() or item.get("sha256") != sha256_file(artifact_path):
                        product_current = False
                        break
            if not product_qa.get("passed") or not product_current:
                review_failures.append("product_qa: 未通过或资料包文件哈希已变化")
        publish_qa_path = paths.status / "qa_publish_report.json"
        try:
            publish_qa = json.loads(publish_qa_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            review_failures.append("publish_qa: 缺少有效发布物料机器 QA JSON")
        else:
            publish_artifacts = publish_qa.get("artifacts")
            publish_current = isinstance(publish_artifacts, dict) and bool(publish_artifacts)
            if publish_current:
                for item in publish_artifacts.values():
                    if not isinstance(item, dict):
                        publish_current = False
                        break
                    artifact_path = Path(str(item.get("path", "")))
                    if not artifact_path.is_file() or item.get("sha256") != sha256_file(artifact_path):
                        publish_current = False
                        break
            if not publish_qa.get("passed") or not publish_current:
                review_failures.append("publish_qa: 未通过或发布物料文件哈希已变化")
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


def review_bundle_current_local(bundle: Path) -> bool:
    try:
        payload = json.loads(bundle.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return False
    for item in artifacts:
        if not isinstance(item, dict):
            return False
        target = Path(str(item.get("path", "")))
        if not target.is_file() or item.get("sha256") != sha256_file(target):
            return False
    return True


def qa_release(project_dir: Path) -> Path:
    from release_video import (
        build_tail_frame_probe_commands,
        inspect_tail_image_black_rectangles,
        release_plate_integrity_issues,
        video_window_black_edge_issues,
    )

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
        tail_dir = paths.status / "release_tail_probe" / path.stem
        if tail_dir.exists():
            shutil.rmtree(tail_dir)
        tail_dir.mkdir(parents=True, exist_ok=True)
        probe_commands = build_tail_frame_probe_commands(path, tail_dir, duration, fps=12, window_seconds=2.0)
        for command in probe_commands:
            process = subprocess.run(command, text=True, capture_output=True)
            if process.returncode != 0:
                notes.append("片尾高频抽帧失败")
                break
        tail_frames = sorted(tail_dir.glob("tail_*.jpg"))
        if len(tail_frames) < 2:
            notes.append("片尾高频证据不足")
        else:
            for tail_frame in tail_frames:
                tail_issues = inspect_tail_image_black_rectangles(tail_frame)
                if tail_issues:
                    notes.extend(f"{tail_frame.name}: {issue}" for issue in tail_issues)
                    break
        general_frames: list[Path] = []
        for sample_index, timestamp in enumerate((0.5, duration * 0.25, duration * 0.5, duration * 0.75), start=1):
            frame_path = tail_dir / f"general_{sample_index:02d}_{max(0.0, timestamp):.3f}.jpg"
            process = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    f"{max(0.0, timestamp):.3f}",
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    str(frame_path),
                ],
                text=True,
                capture_output=True,
            )
            if process.returncode == 0 and frame_path.is_file():
                general_frames.append(frame_path)
        scaled_video_box = (
            0,
            round(height * 416 / 1440),
            width,
            round(height * 608 / 1440),
        )
        for general_frame in general_frames:
            general_issues = release_plate_integrity_issues(general_frame, scaled_video_box)
            general_issues.extend(
                video_window_black_edge_issues(
                    general_frame,
                    scaled_video_box,
                    issue_prefix="release_center_video",
                )
            )
            if general_issues:
                notes.extend(f"{general_frame.name}: {issue}" for issue in general_issues)
                break
        rows.append({"index": str(len(rows) + 1), "file": str(path), "status": "warning" if notes else "ok", "notes": f"{duration:.2f}s {'；'.join(notes)}"})
        structured_results.append(
            {
                "label": label,
                "path": str(path),
                "duration_sec": duration,
                **alignment,
                "tail_probe_dir": str(tail_dir),
                "tail_probe_frame_count": len(tail_frames),
                "general_probe_frame_count": len(general_frames),
                "issues": notes,
            }
        )
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
    required_base = (("故事文稿",), ("故事配乐",), ("朗读标注",), ("示范表演",), ("背景图片",))
    required_advanced = (
        ("故事文稿",),
        ("故事配乐",),
        ("朗读标注",),
        ("示范表演",),
        ("背景图片",),
        ("背景视频", "含字幕"),
        ("背景视频", "无字幕"),
        ("故事PPT", "含字幕"),
        ("故事PPT", "无字幕"),
        ("A镜无人物背景视频",),
    )
    artifacts: dict[str, dict[str, str]] = {}
    product_work = paths.status / "product_package_work"
    package_manifest = product_work / "product_package_manifest.json"
    if package_manifest.exists():
        from product_quality import (
            annotation_receipt_issues,
            manuscript_receipt_issues,
            ppt_render_manifest_issues,
            product_content_manifest_issues,
            product_package_manifest_issues,
        )

        deterministic_checks = {
            "product_content": product_content_manifest_issues(product_work / "product_content_manifest.json"),
            "customer_manuscript": manuscript_receipt_issues(product_work / "customer_manuscript_receipt.json"),
            "reading_annotation": annotation_receipt_issues(product_work / "reading_annotation_receipt.json"),
            "ppt_with_subtitles": ppt_render_manifest_issues(product_work / "ppt_with_subtitles_render_manifest.json"),
            "ppt_without_subtitles": ppt_render_manifest_issues(product_work / "ppt_without_subtitles_render_manifest.json"),
            "product_package": product_package_manifest_issues(package_manifest),
        }
        for component, component_issues in deterministic_checks.items():
            for issue in component_issues:
                issues.append(f"- {component}：{issue}")
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
        files = [path for path in directory.iterdir() if path.is_file()]
        names = [path.name for path in files]
        missing_groups = ["+".join(group) for group in required if not any(all(token in name for token in group) for name in names)]
        empty = [path.name for path in files if path.stat().st_size <= 0]
        leaked = [name for name in names if any(token in name for token in ("成本报告", "QA汇总", "异常说明", "agent_morning_report"))]
        notes: list[str] = []
        if missing_groups:
            notes.append("缺少：" + "、".join(missing_groups))
        if empty:
            notes.append("空文件：" + "、".join(empty))
        if leaked:
            notes.append("混入内部报告：" + "、".join(leaked))
        rows.append({"index": str(len(rows) + 1), "file": str(directory), "status": "warning" if notes else "ok", "notes": "；".join(notes) if notes else "文件齐全"})
        if notes:
            issues.append(f"- {label}：{'；'.join(notes)}")
        for path in files:
            artifacts[f"{key}:{path.name}"] = {"path": str(path), "sha256": sha256_file(path)}
    report = paths.status / "qa_product_report.md"
    write_qa_report(report, "资料包结构机器审查", paths.product, rows, issues, expected="基础版/进阶版目录存在且必备文件齐全")
    manifest["qa"]["product"] = str(report)
    report_json = paths.status / "qa_product_report.json"
    save_json(
        report_json,
        {
            "version": 1,
            "passed": not issues,
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "artifacts": artifacts,
            "issues": issues,
        },
    )
    manifest["qa"]["product_json"] = str(report_json)
    write_manifest(paths, manifest)
    return report


def qa_publish(project_dir: Path) -> Path:
    paths = project_paths(project_dir)
    manifest = init_project(paths.root)
    discover_outputs(paths, manifest)
    rows: list[dict[str, str]] = []
    issues: list[str] = []
    retry_files: list[str] = []
    artifacts: dict[str, dict[str, str]] = {}
    value = manifest["outputs"].get("publish_package", "")
    publish_dir = Path(value) if value else paths.publish
    support_files = [
        ("Codex发布物料交接", publish_dir / "publish_package_codex_handoff.md"),
        ("主账号候选帧索引", publish_dir / "frame_candidates" / "main_候选帧索引.jpg"),
        ("宝库号候选帧索引", publish_dir / "frame_candidates" / "library_候选帧索引.jpg"),
        ("主账号三比例封面任务", publish_dir / "main" / "covers" / "main_cover_design_request.md"),
        ("主账号三比例封面参考帧", publish_dir / "main" / "covers" / "reference_3x4.png"),
        ("宝库号三比例封面参考帧", publish_dir / "library" / "covers" / "reference_3x4.png"),
        ("宝库号参考帧选择说明", publish_dir / "library" / "covers" / "reference_choice.md"),
        ("宝库号三比例衍生提示", publish_dir / "library" / "covers" / "cover_derivative_prompts.md"),
    ]
    for label, path in support_files:
        rows.append(
            {
                "index": str(len(rows) + 1),
                "file": str(path),
                "status": "ok" if path.exists() else "support-missing",
                "notes": f"{label}：{'存在' if path.exists() else '缺少支持材料；不单独阻断正式交付'}",
            }
        )

    expected_ratios = {"3x4": 3 / 4, "4x3": 4 / 3, "16x9": 16 / 9}
    account_covers: dict[str, list[Path]] = {}
    for account_label, account_key in (("主账号", "main"), ("宝库号", "library")):
        covers: list[Path] = []
        for ratio, expected_ratio in expected_ratios.items():
            path = publish_dir / account_key / "covers" / f"cover_{ratio}.png"
            covers.append(path)
            notes: list[str] = []
            if not path.is_file():
                notes.append("缺失")
            else:
                try:
                    with Image.open(path) as image:
                        width, height = image.size
                        if min(width, height) < 600:
                            notes.append(f"尺寸过小：{width}x{height}")
                        if abs(width / max(1, height) - expected_ratio) > 0.025:
                            notes.append(f"比例错误：{width}x{height}")
                except Exception:
                    notes.append("无法读取图像")
                artifacts[f"{account_key}:cover_{ratio}"] = {"path": str(path), "sha256": sha256_file(path)}
            if notes:
                issues.append(f"- {account_label}{ratio}封面：{'；'.join(notes)}")
                retry_files.append(str(path.relative_to(publish_dir)))
            rows.append(
                {
                    "index": str(len(rows) + 1),
                    "file": str(path),
                    "status": "warning" if notes else "ok",
                    "notes": "；".join(notes) if notes else f"{ratio} 比例与尺寸通过",
                }
            )
        account_covers[account_key] = covers

    for account_key, covers in account_covers.items():
        if all(path.is_file() for path in covers):
            for left_index in range(len(covers)):
                for right_index in range(left_index + 1, len(covers)):
                    difference = normalized_cover_difference(covers[left_index], covers[right_index])
                    if difference < 2.0:
                        pair = f"{covers[left_index].name}/{covers[right_index].name}"
                        issues.append(f"- {account_key}封面疑似机械缩放同一母版：{pair}，差异 {difference:.2f}")
                        for path in (covers[left_index], covers[right_index]):
                            relative = str(path.relative_to(publish_dir))
                            if relative not in retry_files:
                                retry_files.append(relative)

    handoff = publish_dir / "publish_package_codex_handoff.md"
    required_v1 = manifest.get("agent", {}).get("story_contract", {}).get("policy") == "required_v1"
    lineage_required = required_v1 or (handoff.is_file() and "cover_lineage.json" in handoff.read_text(encoding="utf-8-sig", errors="ignore"))
    if required_v1:
        integrated_receipt = publish_dir / "cover_integrated_generation.json"
        if integrated_receipt.is_file():
            required_issues, required_retries = integrated_cover_issues(
                publish_dir,
                receipt_path=integrated_receipt,
                expected_title=str(manifest.get("story", {}).get("name") or ""),
            )
            issues.extend(required_issues)
            retry_files.extend(required_retries)
            artifacts["cover_integrated_generation.json"] = {"path": str(integrated_receipt), "sha256": sha256_file(integrated_receipt)}
        else:
            compiled_path = paths.status / "contracts" / "consumers" / "cover.compiled.json"
            try:
                compiled = json.loads(compiled_path.read_text(encoding="utf-8"))
                required_issues, required_retries = required_cover_issues(
                    publish_dir,
                    render_manifest_path=publish_dir / "cover_render_manifest.json",
                    lineage_path=publish_dir / "cover_lineage.json",
                    compiled_spec=compiled,
                    expected_title=str(manifest.get("story", {}).get("name") or ""),
                )
            except (OSError, json.JSONDecodeError) as exc:
                required_issues = [f"- required_v1 封面编译投影缺失或损坏：{exc}"]
                required_retries = [f"{account}/covers/cover_{ratio}.png" for account in ("main", "library") for ratio in ("3x4", "4x3", "16x9")]
            issues.extend(required_issues)
            retry_files.extend(required_retries)
            for name in ("cover_lineage.json", "cover_render_manifest.json", "publish_asset_manifest.json"):
                path = publish_dir / name
                if path.is_file():
                    artifacts[name] = {"path": str(path), "sha256": sha256_file(path)}
    elif lineage_required:
        lineage_path = publish_dir / "cover_lineage.json"
        lineage_issues, lineage_retries = validate_cover_lineage(publish_dir, lineage_path)
        issues.extend(lineage_issues)
        retry_files.extend(lineage_retries)
        if lineage_path.is_file():
            artifacts["cover_lineage"] = {"path": str(lineage_path), "sha256": sha256_file(lineage_path)}

    for account_label, account_key in (("主账号", "main"), ("宝库号", "library")):
        copy_path = publish_dir / account_key / "copy.md"
        copy_notes: list[str] = []
        if not copy_path.is_file():
            copy_notes.append("缺失")
        else:
            text = copy_path.read_text(encoding="utf-8-sig", errors="ignore")
            nonempty = [line.strip() for line in text.splitlines() if line.strip()]
            if len(nonempty) < 3 or len(text.strip()) < 20:
                copy_notes.append("文案内容过短，未形成标题/正文/话题结构")
            if "#" not in text and "话题" not in text:
                copy_notes.append("缺少话题建议")
            artifacts[f"{account_key}:copy"] = {"path": str(copy_path), "sha256": sha256_file(copy_path)}
        if copy_notes:
            issues.append(f"- {account_label}发布文案：{'；'.join(copy_notes)}")
            retry_files.append(str(copy_path.relative_to(publish_dir)))
        rows.append(
            {
                "index": str(len(rows) + 1),
                "file": str(copy_path),
                "status": "warning" if copy_notes else "ok",
                "notes": "；".join(copy_notes) if copy_notes else "标题/正文/话题结构通过",
            }
        )
    report = paths.status / "qa_publish_report.md"
    write_qa_report(
        report,
        "发布物料机器审查",
        publish_dir,
        rows,
        issues,
        expected="两账号文案结构完整；六张封面比例/尺寸正确、不是机械缩放，并具有当前有效的母版编辑血缘",
    )
    manifest["qa"]["publish"] = str(report)
    report_json = paths.status / "qa_publish_report.json"
    save_json(
        report_json,
        {
            "version": 1,
            "passed": not issues,
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "artifacts": artifacts,
            "issues": issues,
            "retry_files": sorted(set(retry_files)),
        },
    )
    manifest["qa"]["publish_json"] = str(report_json)
    write_manifest(paths, manifest)
    return report


def apply_fixed_cover_branding(project_dir: Path, contract_spec: Path | None = None) -> Path:
    """Overlay the exact configured brand PNG on all covers and hash the operation."""
    paths = project_paths(project_dir)
    config = load_config()
    brand = config.get("brand_assets", {}) if isinstance(config.get("brand_assets"), dict) else {}
    if contract_spec is not None:
        try:
            compiled = json.loads(contract_spec.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"封面合同编译投影不可读：{contract_spec}") from exc
        if compiled.get("consumer") != "cover":
            raise ValueError("封面合同编译投影 consumer 必须为 cover")
        official = [item for item in compiled.get("official_assets", []) if isinstance(item, dict)]
        logo = first_existing(brand.get("cover_logo"), brand.get("logo")) if official else None
        if official and logo is None:
            raise FileNotFoundError("封面定版缺少可读的固定品牌 Logo；禁止让生图模型伪造图标")
        manifest = load_manifest(paths) or {}
        receipt, _render, _lineage = render_required_covers(
            paths.publish,
            compiled_spec=compiled,
            logo_path=logo,
            story=manifest.get("story", {}),
        )
        asset_manifest = paths.publish / "publish_asset_manifest.json"
        render_payload = json.loads((paths.publish / "cover_render_manifest.json").read_text(encoding="utf-8"))
        save_json(asset_manifest, render_payload)
        return receipt
    logo = first_existing(brand.get("cover_logo"), brand.get("logo"))
    if logo is None:
        raise FileNotFoundError("封面定版缺少可读的固定品牌 Logo；禁止让生图模型伪造图标")
    covers = [
        paths.publish / account / "covers" / f"cover_{ratio}.png"
        for account in ("main", "library")
        for ratio in ("3x4", "4x3", "16x9")
    ]
    missing = [path for path in covers if not path.is_file()]
    if missing:
        raise FileNotFoundError("封面定版缺少文件：" + "、".join(path.name for path in missing))
    receipt = paths.status / "publish_cover_branding.json"
    logo_sha = sha256_file(logo)
    compiled: dict[str, Any] = {}
    binding: dict[str, str] = {}
    if contract_spec is not None:
        try:
            compiled = json.loads(contract_spec.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"封面合同编译投影不可读：{contract_spec}") from exc
        if compiled.get("consumer") != "cover":
            raise ValueError("封面合同编译投影 consumer 必须为 cover")
        binding = {
            field: str(compiled.get(field) or "")
            for field in ("contract_schema_version", "story_contract_sha256", "story_contract_dependency_sha256")
        }
        if not all(binding.values()):
            raise ValueError("封面合同编译投影缺少合同绑定字段")
        official = [item for item in compiled.get("official_assets", []) if isinstance(item, dict)]
        if not any(item.get("sha256") == logo_sha and int(item.get("max_per_frame", 0)) >= 1 for item in official):
            raise ValueError("配置的封面 Logo 不在已审核官方品牌资产清单中")
    try:
        old_receipt = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        old_receipt = {}
    old_outputs = old_receipt.get("covers") if isinstance(old_receipt.get("covers"), dict) else {}
    asset_manifest = paths.publish / "publish_asset_manifest.json"
    manifest_current = not binding
    if binding and asset_manifest.is_file():
        try:
            previous_manifest = json.loads(asset_manifest.read_text(encoding="utf-8"))
            manifest_current = all(previous_manifest.get(field) == value for field, value in binding.items())
        except (OSError, json.JSONDecodeError):
            manifest_current = False
    if manifest_current and old_receipt.get("logo_sha256") == logo_sha and all(old_receipt.get(field) == value for field, value in binding.items()) and all(
        isinstance(old_outputs.get(str(path)), dict)
        and old_outputs[str(path)].get("output_sha256") == sha256_file(path)
        for path in covers
    ):
        return receipt

    with Image.open(logo) as logo_source:
        logo_rgba = logo_source.convert("RGBA")
        logo_bbox = logo_rgba.getbbox()
        if logo_bbox is None:
            raise ValueError("配置的品牌 Logo 全透明")
        logo_rgba = logo_rgba.crop(logo_bbox)
    cover_records: dict[str, dict[str, Any]] = {}
    ratio_names = {"3x4": "3:4", "4x3": "4:3", "16x9": "16:9"}
    variants = [item for item in compiled.get("variants", []) if isinstance(item, dict)]
    for cover in covers:
        input_sha = sha256_file(cover)
        with Image.open(cover) as source:
            canvas = source.convert("RGBA")
        ratio_key = cover.stem.removeprefix("cover_")
        expected_ratio = ratio_names[ratio_key]
        if abs(canvas.width / canvas.height - ({"3:4": 3 / 4, "4:3": 4 / 3, "16:9": 16 / 9}[expected_ratio])) > 0.015:
            raise ValueError(f"封面比例与确定性产物规格不符：{cover.name}")
        variant = next((item for item in variants if item.get("aspect_ratio") == expected_ratio), None)
        regions = {
            str(item.get("role")): item
            for item in ((variant or {}).get("regions", []))
            if isinstance(item, dict) and item.get("role")
        }
        logo_region = regions.get("logo") or regions.get("brand_logo")
        max_width = round(canvas.width * (float(logo_region["width"]) if logo_region else 0.27))
        max_height = round(canvas.height * (float(logo_region["height"]) if logo_region else 0.105))
        scale = min(max_width / logo_rgba.width, max_height / logo_rgba.height)
        logo_resized = logo_rgba.resize(
            (max(1, round(logo_rgba.width * scale)), max(1, round(logo_rgba.height * scale))),
            Image.Resampling.LANCZOS,
        )
        x = round(canvas.width * float(logo_region["x"])) if logo_region else (canvas.width - logo_resized.width) // 2
        y = round(canvas.height * float(logo_region["y"])) if logo_region else round(canvas.height * 0.025)
        if x < 0 or y < 0 or x + logo_resized.width > canvas.width or y + logo_resized.height > canvas.height:
            raise ValueError(f"合同 Logo 区域超出封面画布：{cover.name}")
        canvas.alpha_composite(logo_resized, (x, y))
        temporary = cover.with_name(f".{cover.stem}.branding-{uuid.uuid4().hex}.png")
        canvas.convert("RGB").save(temporary, format="PNG", optimize=True)
        os.replace(temporary, cover)
        cover_records[str(cover)] = {
            "input_sha256": input_sha,
            "output_sha256": sha256_file(cover),
            "logo_box": [x, y, logo_resized.width, logo_resized.height],
            "aspect_ratio": expected_ratio,
            "variant_id": (variant or {}).get("variant_id"),
            "title_safe_region": regions.get("title_safe") or regions.get("title"),
            "person_region": regions.get("person") or regions.get("host"),
            "layout_constraints": compiled.get("layout_rules", []),
        }

    lineage_path = paths.publish / "cover_lineage.json"
    if lineage_path.is_file():
        try:
            lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            lineage = None
        if isinstance(lineage, dict) and isinstance(lineage.get("covers"), list):
            for item in lineage["covers"]:
                if not isinstance(item, dict):
                    continue
                relative = str(item.get("path") or "")
                target = paths.publish / relative
                if target.is_file():
                    item["sha256"] = sha256_file(target)
                    item["brand_asset_sha256"] = logo_sha
                    item["brand_mode"] = "deterministic_exact_overlay"
                parent = item.get("parent")
                parent_path = paths.publish / str(parent) if parent else None
                if parent_path is not None and parent_path.is_file():
                    item["parent_sha256"] = sha256_file(parent_path)
            save_json(lineage_path, lineage)
    save_json(
        receipt,
        {
            "version": 1,
            **binding,
            "logo_path": str(logo),
            "logo_sha256": logo_sha,
            "mode": "deterministic_exact_overlay",
            "covers": cover_records,
        },
    )
    if contract_spec is not None:
        save_json(
            asset_manifest,
            {
                "version": 1,
                "consumer": "cover",
                **binding,
                "compiled_spec": str(contract_spec),
                "compiled_spec_sha256": sha256_file(contract_spec),
                "official_logo_sha256": logo_sha,
                "official_logo_count_per_cover": 1,
                "brand_rules": compiled.get("brand_rules", []),
                "layout_rules": compiled.get("layout_rules", []),
                "covers": cover_records,
            },
        )
    return receipt


def normalized_cover_difference(left: Path, right: Path) -> float:
    with Image.open(left).convert("RGB") as left_image, Image.open(right).convert("RGB") as right_image:
        left_sample = left_image.resize((64, 64), Image.Resampling.BILINEAR)
        right_sample = right_image.resize((64, 64), Image.Resampling.BILINEAR)
        total = 0
        count = 0
        left_pixels = left_sample.get_flattened_data() if hasattr(left_sample, "get_flattened_data") else left_sample.getdata()
        right_pixels = right_sample.get_flattened_data() if hasattr(right_sample, "get_flattened_data") else right_sample.getdata()
        for left_pixel, right_pixel in zip(left_pixels, right_pixels):
            total += sum(abs(int(a) - int(b)) for a, b in zip(left_pixel, right_pixel))
            count += 3
    return total / max(1, count)


def validate_cover_lineage(publish_dir: Path, lineage_path: Path) -> tuple[list[str], list[str]]:
    expected_parents: dict[str, str | None] = {
        "main/covers/cover_4x3.png": None,
        "main/covers/cover_3x4.png": "main/covers/cover_4x3.png",
        "main/covers/cover_16x9.png": "main/covers/cover_4x3.png",
        "library/covers/cover_4x3.png": "main/covers/cover_4x3.png",
        "library/covers/cover_3x4.png": "library/covers/cover_4x3.png",
        "library/covers/cover_16x9.png": "library/covers/cover_4x3.png",
    }
    issues: list[str] = []
    retry_files: list[str] = []
    if not lineage_path.is_file():
        issues.append("- 六张封面缺少 cover_lineage.json，无法证明参考图编辑血缘")
        return issues, sorted(expected_parents)
    try:
        payload = json.loads(lineage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        issues.append("- cover_lineage.json 无法解析")
        return issues, sorted(expected_parents)
    entries = payload.get("covers", []) if isinstance(payload, dict) else []
    if not isinstance(entries, list):
        entries = []
    by_path = {
        str(item.get("path", "")).replace("\\", "/"): item
        for item in entries
        if isinstance(item, dict) and str(item.get("path", "")).strip()
    }
    for relative, expected_parent in expected_parents.items():
        entry = by_path.get(relative)
        cover = publish_dir / relative
        if entry is None:
            issues.append(f"- 封面血缘缺少记录：{relative}")
            retry_files.append(relative)
            continue
        generation_mode = str(entry.get("generation_mode", ""))
        expected_mode = "master" if expected_parent is None else "edit-derived"
        if generation_mode != expected_mode:
            issues.append(f"- 封面血缘模式错误：{relative} 应为 {expected_mode}")
            retry_files.append(relative)
        parent = entry.get("parent") or None
        if parent != expected_parent:
            issues.append(f"- 封面母版关系错误：{relative} 的 parent 应为 {expected_parent or 'null'}")
            retry_files.append(relative)
        references = entry.get("reference_files")
        if not isinstance(references, list) or not any(str(item).strip() for item in references):
            issues.append(f"- 封面缺少生成参考记录：{relative}")
            retry_files.append(relative)
        if cover.is_file() and entry.get("sha256") != sha256_file(cover):
            issues.append(f"- 封面血缘当前哈希失效：{relative}")
            retry_files.append(relative)
        if expected_parent is not None:
            parent_path = publish_dir / expected_parent
            if not parent_path.is_file() or entry.get("parent_sha256") != sha256_file(parent_path):
                issues.append(f"- 封面母版哈希失效：{relative}")
                retry_files.append(relative)
    return issues, sorted(set(retry_files))


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
    canonical_release_outputs = {
        "main_release_video": paths.release / "主账号发布视频.mp4",
        "library_release_video": paths.release / "宝库号发布视频.mp4",
    }
    for key, values in candidates.items():
        canonical = canonical_release_outputs.get(key)
        if canonical is not None and canonical.is_file():
            manifest["outputs"][key] = str(canonical)
            continue
        if key in canonical_release_outputs:
            values = [
                path
                for path in values
                if paths.status not in path.parents and "_release_work" not in path.parts
            ]
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
        if not path.is_absolute():
            repository_asset = ROOT / path
            if repository_asset.exists():
                return repository_asset
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
