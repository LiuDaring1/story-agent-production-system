from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image, ImageDraw, ImageFont
from analyze_storyboard_pacing import analyze_storyboard_pacing
from story_video_synthesizer.media import probe_duration
from story_video_synthesizer.image_video import (
    enforce_prompt_continuity_contract,
    validate_image_video_jobs,
)
from story_module_registry import (
    build_registry_for_profile,
    export_module_execution_mode,
    export_module_profile,
    resolve_module_execution_mode,
    resolve_module_profile,
)
from story_semantics import SemanticKind, classify_story
from story_contract_consumers import (
    compile_demo_render_spec,
    demo_logo_arguments,
    compile_release_render_spec,
    release_argument_overrides,
)
from story_text import read_trusted_story_text
from artifact_semantic_plan import load_current_artifact_semantic_plan, semantic_plan_path, selected_line_indices
from r2v_retry_policy import evaluate_quality_redos
from presenter_layout import PRESENTER_LAYOUT_POLICY, compile_fixed_anchor, scan_rvm_body_overflow
from story_delivery_policy import release_semantic_subtitle_artifact
from story_timeline import ensure_authoritative_timeline_srt
from release_geometry import CANONICAL_B_STORY_BOX
from demo_quality import demo_render_manifest_issues
from static_ppt_contract import validate_delivery_receipt as validate_static_ppt_delivery_receipt

from story_project import (
    auto_keying,
    configured_keying_backend,
    create_theme_asset_request,
    detect_project_assets,
    doctor_project,
    first_existing,
    first_image_in,
    final_delivery,
    init_project,
    load_config,
    main_package_receipt_issues,
    maybe_update_latest_episode,
    project_paths,
    qa_images,
    qa_music,
    qa_manual_outputs,
    qa_product,
    qa_publish,
    qa_release,
    qa_theme_assets,
    qa_videos,
    register_manual_output,
    refresh_project_outputs,
    save_json,
    semi_auto_status,
    update_story_info,
    write_manifest,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_B_STORY_BOX_TEXT = ",".join(str(value) for value in CANONICAL_B_STORY_BOX)


def customer_manuscript_source(
    confirmed_story_text: Path | None,
    generated_consumer_manuscript: Path | str | None,
    generic_story_text: Path | str | None,
) -> Path | None:
    """Choose a customer document source without feeding old output back in.

    The hash-bound confirmed manuscript is authoritative.  A prior generated
    customer document is only a migration fallback, ahead of the subtitle-like
    generic text source.
    """

    return first_existing(
        confirmed_story_text,
        generated_consumer_manuscript,
        generic_story_text,
    )


def product_demo_audio_source(
    confirmed_program_audio: Path | None,
    manifest_narration: Path | str | None,
    extracted_narration: Path | str | None,
) -> Path | None:
    """Choose the full spoken program before any body-only narration."""

    return first_existing(
        confirmed_program_audio,
        manifest_narration,
        extracted_narration,
    )


def formal_release_audio_arguments(
    release_defaults: dict,
    *,
    strict_contract: bool,
) -> list[str]:
    """Return the public-release narration+music mixing contract.

    The project background videos already carry the reviewed music-only bed.
    Formal release must mix that bed with the hash-bound spoken program; an
    audio stream containing narration alone is not a valid release soundtrack.
    """

    if not strict_contract:
        return []
    return [
        "--mix-bg-audio",
        "--voice-volume",
        str(float(release_defaults.get("voice_volume", 1.0))),
        "--bg-audio-volume",
        str(float(release_defaults.get("bg_audio_volume", 1.0))),
    ]


def static_ppt_inputs_from_delivery_receipt(status_dir: Path) -> dict[str, Path]:
    """Return the exact sealed PPT inputs recorded by the native ledger."""

    receipt_path = story_run_artifact_path(status_dir, "static_ppt_delivery_receipt")
    if receipt_path is None:
        return {}
    receipt = validate_static_ppt_delivery_receipt(receipt_path)
    return {
        "--director-plan": Path(str(receipt["director_plan_path"])),
        "--shot-storyboard-compile-receipt": Path(
            str(receipt["shot_storyboard_compile_receipt_path"])
        ),
        "--static-ppt-plan": Path(str(receipt["ppt_plan_path"])),
        "--static-ppt-with-subtitles": Path(str(receipt["with_subtitles_pptx_path"])),
        "--static-ppt-without-subtitles": Path(str(receipt["without_subtitles_pptx_path"])),
    }


def product_body_subtitles_from_customer_media_receipt(status_dir: Path) -> Path | None:
    """Resolve the exact body-only SRT already audited with customer media."""

    receipt_path = story_run_artifact_path(status_dir, "customer_media_receipt")
    if receipt_path is None:
        return None
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("客户媒体回执无法读取") from exc
    if receipt.get("passed") is not True or receipt.get("critical_errors"):
        raise ValueError("客户媒体回执未通过，不能复用正文 SRT")
    subtitle = receipt.get("subtitle_srt")
    if not isinstance(subtitle, dict):
        raise ValueError("客户媒体回执缺少正文 SRT 绑定")
    path = Path(str(subtitle.get("path") or "")).expanduser()
    expected = str(subtitle.get("sha256") or "").lower()
    if not path.is_file() or len(expected) != 64:
        raise ValueError("客户媒体回执的正文 SRT 不存在")
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError("客户媒体回执的正文 SRT 哈希失效")
    return path


def current_demo_preview_manifest(project_root: Path, manifest: dict) -> Path:
    """Resolve the newest *valid* preview geometry receipt, never a dead path.

    Older repair runs stored this receipt under a run-specific status folder,
    while the workflow later hard-coded one product-work path.  Search the
    bounded status tree only as a migration fallback and validate every
    candidate's hashes before allowing Release to inherit its geometry.
    """

    paths = project_paths(project_root)
    outputs = manifest.get("outputs") if isinstance(manifest.get("outputs"), dict) else {}
    candidates: list[Path] = []
    explicit = Path(str(outputs.get("demo_preview_manifest") or ""))
    if explicit.is_file():
        candidates.append(explicit)
    candidates.extend(
        path
        for path in (
            paths.status / "product_package_work" / "demo_preview_manifest.json",
            paths.product / "product_package_work" / "demo_preview_manifest.json",
            paths.status / "product_assets" / "demo_preview_manifest.json",
        )
        if path.is_file()
    )
    discovered = sorted(
        paths.status.rglob("*demo*manifest*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    candidates.extend(discovered)
    seen: set[Path] = set()
    rejected: list[str] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            rejected.append(f"{candidate}:{exc}")
            continue
        if payload.get("mode") != "preview":
            continue
        issues = demo_render_manifest_issues(candidate, paths.root, require_final=False)
        if not issues:
            return candidate
        rejected.append(f"{candidate}:{','.join(issues)}")
    detail = "；".join(rejected[:4]) or "未找到 preview 模式回执"
    raise FileNotFoundError(f"缺少当前有效的 Demo 预览几何回执：{detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description="儿童故事图片到成片的统一流程入口")
    parser.add_argument(
        "--module-profile",
        default="",
        help="允许列表中的模块适配器配置；子进程会校验并继承该选择",
    )
    parser.add_argument(
        "--module-execution-mode",
        default="",
        help="模块 production/test 执行锁；子进程会校验并继承该选择",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init-project", help="初始化桌面故事项目和 project_manifest.json")
    init.add_argument("--project-dir", required=True, type=Path)
    init.add_argument("--story-name", default="")
    init.add_argument("--slug", default="")
    init.add_argument("--episode", default=None, type=int)

    detect = subparsers.add_parser("detect-assets", help="自动识别故事正文、原声/绿幕视频并更新 manifest")
    detect.add_argument("--project-dir", required=True, type=Path)
    detect.add_argument("--extract-audio", action="store_true", help="没有独立原声时从绿幕视频提取音频")

    setup = subparsers.add_parser("setup-project", help="保存故事信息并识别素材，适合工作台极简入口")
    setup.add_argument("--project-dir", required=True, type=Path)
    setup.add_argument("--story-name", default=None)
    setup.add_argument("--slug", default=None)
    setup.add_argument("--episode", default=None, type=int)
    setup.add_argument("--story-type", default=None)
    setup.add_argument("--image-style", default=None)
    setup.add_argument("--age-range", default=None)
    setup.add_argument("--duration-text", default=None)
    setup.add_argument("--update-latest-episode-on-delivery", choices=["yes", "no"], default=None)
    setup.add_argument("--does-not-count-episode", choices=["yes", "no"], default=None)
    setup.add_argument("--extract-audio", action="store_true", default=True)

    info = subparsers.add_parser("set-story-info", help="设置工作台故事信息")
    info.add_argument("--project-dir", required=True, type=Path)
    info.add_argument("--story-name", default=None)
    info.add_argument("--slug", default=None)
    info.add_argument("--episode", default=None, type=int)
    info.add_argument("--story-type", default=None)
    info.add_argument("--image-style", default=None)
    info.add_argument("--age-range", default=None)
    info.add_argument("--duration-text", default=None)
    info.add_argument("--update-latest-episode-on-delivery", choices=["yes", "no"], default=None)
    info.add_argument("--does-not-count-episode", choices=["yes", "no"], default=None)

    qa_image_cmd = subparsers.add_parser("qa-images", help="机器审查图片目录并生成 QA 报告")
    qa_image_cmd.add_argument("--project-dir", required=True, type=Path)
    qa_image_cmd.add_argument("--image-dir", type=Path)

    qa_video_cmd = subparsers.add_parser("qa-videos", help="机器抽帧审查视频片段并生成 QA 报告")
    qa_video_cmd.add_argument("--project-dir", required=True, type=Path)
    qa_video_cmd.add_argument("--videos-dir", type=Path)
    qa_video_cmd.add_argument("--execution-mode", choices=["production", "test"], default="production")

    qa_music_cmd = subparsers.add_parser("qa-music", help="检查配乐覆盖、响度、削波、静音和分段结构")
    qa_music_cmd.add_argument("--project-dir", required=True, type=Path)
    qa_music_cmd.add_argument("--music", required=True, type=Path)
    qa_music_cmd.add_argument("--narration", required=True, type=Path)
    qa_music_cmd.add_argument("--plan-csv", type=Path)

    qa_release_cmd = subparsers.add_parser("qa-release", help="检查主账号/宝库号发布视频")
    qa_release_cmd.add_argument("--project-dir", required=True, type=Path)

    qa_product_cmd = subparsers.add_parser("qa-product", help="检查基础版/进阶版资料包结构")
    qa_product_cmd.add_argument("--project-dir", required=True, type=Path)

    qa_publish_cmd = subparsers.add_parser("qa-publish", help="检查双账号四平台发布物料")
    qa_publish_cmd.add_argument("--project-dir", required=True, type=Path)

    theme_cmd = subparsers.add_parser("theme-assets", help="生成第 12 步 Codex/imagegen 发布素材任务书")
    theme_cmd.add_argument("--project-dir", required=True, type=Path)

    keying_cmd = subparsers.add_parser("auto-keying", help="自动采样绿幕并生成 keying_preset.json")
    keying_cmd.add_argument("--project-dir", required=True, type=Path)
    keying_cmd.add_argument("--greenscreen", type=Path)
    keying_cmd.add_argument(
        "--backend",
        choices=["auto", "colorkey", "chromakey", "rvm"],
        default="auto",
        help="auto 使用 pipeline_config.release_defaults.preferred_keyer",
    )

    release_assets_cmd = subparsers.add_parser("prepare-release-assets-project", help="生成第 12 步 Codex 发布视觉定版任务书并尝试自动抠像")
    release_assets_cmd.add_argument("--project-dir", required=True, type=Path)

    delivery_cmd = subparsers.add_parser("final-delivery", help="生成总交付清单并可更新 latest_episode")
    delivery_cmd.add_argument("--project-dir", required=True, type=Path)
    delivery_cmd.add_argument("--update-latest-episode", action="store_true")

    doctor_cmd = subparsers.add_parser("doctor-project", help="检查单个故事项目的配置、manifest、输入输出和 QA 状态")
    doctor_cmd.add_argument("--project-dir", required=True, type=Path)

    release_project = subparsers.add_parser("package-release-project", help="从桌面项目自动生成主账号/宝库号发布视频")
    release_project.add_argument("--project-dir", required=True, type=Path)
    release_project.add_argument("--variant", choices=["auto", "both", "main", "library"], default="auto")
    release_project.add_argument("--story-contract-context", type=Path)
    release_project.add_argument(
        "--authorize-binding-repair",
        default="",
        metavar="REASON",
        help="仅用于用户明确授权的规则/技术缺陷修复；必须写明原因，旧成片会自动备份并消耗一次修复额度",
    )

    release_preview = subparsers.add_parser("preview-release-project", help="可选刷新当前发布视频预览帧，不编码完整视频")
    release_preview.add_argument("--project-dir", required=True, type=Path)
    release_preview.add_argument("--variant", choices=["auto", "both", "main", "library"], default="auto")
    release_preview.add_argument("--times", default="1,2,37,92")
    release_preview.add_argument("--person-layouts", default="", help="主账号人物布局候选；留空只预览 keying_preset 当前最终参数，auto 生成候选")
    release_preview.add_argument("--story-contract-context", type=Path)

    layout_handoff = subparsers.add_parser("release-layout-handoff", help="第 12 步后生成 Codex 智能定参候选预览和交接说明")
    layout_handoff.add_argument("--project-dir", required=True, type=Path)
    layout_handoff.add_argument("--times", default="1,2,37,92")
    layout_handoff.add_argument("--person-layouts", default="auto")

    publish_project = subparsers.add_parser("publish-package-project", help="从桌面项目自动生成精简发布物料：候选帧和两张 4:3 封面任务")
    publish_project.add_argument("--project-dir", required=True, type=Path)
    publish_project.add_argument("--generate-covers", action="store_true")
    publish_project.add_argument("--main-frame", type=int, help="兼容旧参数：主账号故事画面参考帧编号")
    publish_project.add_argument("--main-person-frame", type=int)
    publish_project.add_argument("--main-story-frame", type=int)
    publish_project.add_argument("--library-frame", type=int)

    publish_draft_project = subparsers.add_parser("publish-package-draft-project", help="半自动版提前生成精简发布物料草稿任务书")
    publish_draft_project.add_argument("--project-dir", required=True, type=Path)

    semi_status = subparsers.add_parser("semi-auto-status", help="生成半自动状态报告")
    semi_status.add_argument("--project-dir", required=True, type=Path)

    register_output = subparsers.add_parser("register-output", help="登记半自动手工产物")
    register_output.add_argument("--project-dir", required=True, type=Path)
    register_output.add_argument("--kind", required=True)
    register_output.add_argument("--source", required=True, type=Path)

    qa_manual = subparsers.add_parser("qa-manual-outputs", help="检查半自动手工产物是否完整")
    qa_manual.add_argument("--project-dir", required=True, type=Path)

    product_preflight = subparsers.add_parser("product-package-preflight-project", help="第 16 步先生成 Codex 前置审查材料和示范视频预览帧")
    product_preflight.add_argument("--project-dir", required=True, type=Path)
    product_preflight.add_argument("--preview-times", default="0.8,1.5,2.5,37,92")
    product_preflight.add_argument(
        "--demo-person-crop-mode",
        choices=["source-native", "preset", "full-width"],
        default="source-native",
    )
    product_preflight.add_argument("--demo-person-vertical-align", choices=["center", "bottom"], default="bottom")
    product_preflight.add_argument("--demo-person-crop-bottom-ratio", default=0.0, type=float)
    product_preflight.add_argument("--story-contract-context", type=Path)

    product_project = subparsers.add_parser("product-package-project", help="从桌面项目自动生成基础版/进阶版资料包")
    product_project.add_argument("--project-dir", required=True, type=Path)
    product_project.add_argument("--annotation-docx", type=Path)
    product_project.add_argument("--annotation-json", type=Path)
    product_project.add_argument("--allow-draft-annotation", action="store_true", default=False)
    product_project.add_argument(
        "--demo-person-crop-mode",
        choices=["source-native", "preset", "full-width"],
        default="source-native",
    )
    product_project.add_argument("--demo-person-vertical-align", choices=["center", "bottom"], default="bottom")
    product_project.add_argument("--demo-person-crop-bottom-ratio", default=0.0, type=float)
    product_project.add_argument("--story-contract-context", type=Path)

    normalize = subparsers.add_parser("normalize-images", help="文生图输出 -> 标准 images 目录")
    normalize.add_argument("--source-dir", required=True, type=Path)
    normalize.add_argument("--output-dir", required=True, type=Path)
    normalize.add_argument("--slug", required=True)
    normalize.add_argument("--count", default=0, type=int)

    pacing = subparsers.add_parser("analyze-pacing", help="分析手动换行分镜是否适合 Grok Video 1.0（6/10 秒）图生视频")
    pacing.add_argument("--story-file", required=True, type=Path)
    pacing.add_argument("--output-dir", required=True, type=Path)
    pacing.add_argument("--slug", default="story")
    pacing.add_argument("--narration", default=None, type=Path)
    pacing.add_argument("--whisper-model", default="base")
    pacing.add_argument("--language", default="zh")
    pacing.add_argument("--whisper-model-dir", default=None, type=Path)

    prepare = subparsers.add_parser("prepare", help="图片 + 分镜文本 -> 任务清单和审核页")
    prepare.add_argument("--image-dir", required=True, type=Path)
    prepare.add_argument("--storyboard", required=True, type=Path)
    prepare.add_argument("--output-dir", required=True, type=Path)
    prepare.add_argument("--story-contract-context", type=Path)
    prepare.add_argument("--slug", required=True)
    prepare.add_argument("--short-slug", required=True)
    prepare.add_argument("--continuity-contract", type=Path, default=None, help="视觉连续性合同 JSON")
    prepare.add_argument("--storyboard-plan", type=Path, default=None, help="机器可读 storyboard_plan.json")

    timing = subparsers.add_parser("timing", help="根据旁白写入 frames、目标时长，以及可选的自适应整数秒请求")
    timing.add_argument("--jobs-csv", required=True, type=Path)
    timing.add_argument("--narration", required=True, type=Path)
    timing.add_argument("--whisper-model", default="base")
    timing.add_argument("--language", default="zh")
    timing.add_argument("--whisper-model-dir", default=None, type=Path)
    timing.add_argument("--max-duration", default=10.0, type=float, help="单镜头推荐上限 10 秒；Grok Video 1.0 支持 6/10 秒")
    timing.add_argument(
        "--duration-mode",
        choices=["fixed", "adaptive", "adaptive-seconds"],
        default="fixed",
        help="fixed 保持旧版固定时长；adaptive-seconds 按旁白向上取整并夹在供应商整数秒范围",
    )
    timing.add_argument("--adaptive-seconds", action="store_true", help="兼容别名：等同于 --duration-mode adaptive-seconds")
    timing.add_argument(
        "--min-generation-seconds",
        "--generation-min-seconds",
        dest="min_generation_seconds",
        default=1.0,
        type=float,
        help="adaptive-seconds 的供应商最小整数秒（默认 1）",
    )
    timing.add_argument(
        "--max-generation-seconds",
        "--generation-max-seconds",
        dest="max_generation_seconds",
        default=15.0,
        type=float,
        help="adaptive-seconds 的供应商最大整数秒（默认 15）",
    )
    timing.add_argument(
        "--generation-duration-choices",
        default="",
        help="供应商离散秒数选项，例如 6,10；按真实镜头时长选最近值",
    )

    generate = subparsers.add_parser("generate", help="调用视频 API 生成片段")
    generate.add_argument("--jobs-csv", required=True, type=Path)
    generate.add_argument("--images-dir", required=True, type=Path)
    generate.add_argument("--videos-dir", required=True, type=Path)
    generate.add_argument("--start-scene", default=1, type=int)
    generate.add_argument("--end-scene", default=9999, type=int)
    generate.add_argument("--scenes", default="")
    generate.add_argument("--limit", default=0, type=int)
    generate.add_argument("--dry-run", action="store_true")
    generate.add_argument("--max-submit-first", default=20, type=int, help="批量提交模式下一次最多保留多少个已提交未完成任务；0 表示不限制")
    generate.add_argument("--prompt-review-csv", default="", help="提示词确认页导出的 prompt_review_decisions.csv")
    generate.add_argument("--skip-prompt-review", action="store_true", help="跳过图生视频提示词确认闸门")
    generate.add_argument("--provider", default="", help="覆盖 pipeline_config.json 中的图生视频 provider")
    generate.add_argument("--project-dir", default="", type=Path, help="V3.5 合同锁所属项目目录；合同绑定任务必填")
    generate.add_argument("--execution-mode", choices=["production", "test"], default="production")

    rerun_review = subparsers.add_parser("rerun-review", help="根据审核 CSV 只重跑标记为重做的片段")
    rerun_review.add_argument("--jobs-csv", required=True, type=Path)
    rerun_review.add_argument("--images-dir", required=True, type=Path)
    rerun_review.add_argument("--videos-dir", required=True, type=Path)
    rerun_review.add_argument("--decisions-csv", required=True, type=Path)
    rerun_review.add_argument("--provider", default="", help="覆盖 pipeline_config.json 中的图生视频 provider")
    rerun_review.add_argument("--execution-mode", choices=["production", "test"], default="production")

    review = subparsers.add_parser("apply-review", help="根据审核 CSV 整理最终 clips")
    review.add_argument("--jobs-csv", required=True, type=Path)
    review.add_argument("--videos-dir", required=True, type=Path)
    review.add_argument("--output-dir", required=True, type=Path)
    review.add_argument("--decisions-csv", default="")
    review.add_argument("--short-slug", required=True)

    music_request = subparsers.add_parser("music-request", help="故事文本 -> Suno 配乐任务包")
    music_request.add_argument("--story-file", required=True, type=Path)
    music_request.add_argument("--output-dir", required=True, type=Path)
    music_request.add_argument("--slug", required=True)
    music_request.add_argument("--story-title", default="")
    music_request.add_argument("--narration", default=None, type=Path)
    music_request.add_argument("--jobs-csv", default=None, type=Path)
    music_request.add_argument("--skill-path", default=None, type=Path)
    music_request.add_argument("--story-contract-context", default=None, type=Path)

    assemble_music = subparsers.add_parser("assemble-music", help="按分段表拼接 Suno 背景音乐")
    assemble_music.add_argument("--plan-csv", required=True, type=Path)
    assemble_music.add_argument("--clips-dir", required=True, type=Path)
    assemble_music.add_argument("--output", required=True, type=Path)
    assemble_music.add_argument("--fade", default=1.0, type=float)

    assemble = subparsers.add_parser("assemble", help="合成三版成片")
    assemble.add_argument("--video-dir", required=True, type=Path)
    assemble.add_argument("--script", required=True, type=Path)
    assemble.add_argument("--subtitle-script", default=None, type=Path)
    assemble.add_argument("--narration", required=True, type=Path)
    assemble.add_argument("--music", required=True, type=Path)
    assemble.add_argument("--output-dir", required=True, type=Path)
    assemble.add_argument("--whisper-model", default="base")
    assemble.add_argument("--language", default="zh")
    assemble.add_argument("--whisper-model-dir", default=None, type=Path)
    assemble.add_argument("--subtitle-style", choices=["clean", "box"], default="clean")
    assemble.add_argument("--project-dir", type=Path)
    assemble.add_argument("--artifact-semantic-plan", type=Path)

    release = subparsers.add_parser("package-release", help="背景成片 -> 主账号/宝库号小红书发布视频")
    release.add_argument("--story-name", required=True)
    release.add_argument("--duration-text", required=True)
    release.add_argument("--age-text", required=True)
    release.add_argument("--usage-text", default="适用于朗诵比赛、故事表演、少儿口才、技能比拼")
    release.add_argument("--story-type", default="儿童故事")
    release.add_argument("--bg-video", required=True, type=Path)
    release.add_argument("--output-dir", required=True, type=Path)
    release.add_argument("--variant", choices=["both", "main", "library"], default="both")
    release.add_argument("--bg-image", type=Path)
    release.add_argument("--person-greenscreen", type=Path)
    release.add_argument("--audio-mix", type=Path)
    release.add_argument("--watermark-logo", type=Path)
    release.add_argument("--antipiracy-logo", type=Path)
    release.add_argument("--contract-render-spec", required=True, type=Path)
    release.add_argument("--artifact-semantic-plan", required=True, type=Path)
    release.add_argument("--main-top-panel", type=Path)
    release.add_argument("--main-bottom-panel", type=Path)
    release.add_argument("--library-top-panel", type=Path)
    release.add_argument("--library-bottom-panel", type=Path)
    release.add_argument("--main-package-spec", type=Path)
    release.add_argument("--main-package-receipt", type=Path)
    release.add_argument("--approved-preview-geometry", type=Path)
    release.add_argument("--video-box", default="0,416,1080,608")
    release.add_argument("--watermark-width", default=96, type=int)
    release.add_argument("--watermark-opacity", default=0.78, type=float)
    release.add_argument("--watermark-speed", default=0.45, type=float)
    release.add_argument("--frame-image", type=Path)
    release.add_argument("--frame-image-b", type=Path)
    release.add_argument("--story-box", default="170,250,990,557")
    release.add_argument("--b-story-box", default=DEFAULT_B_STORY_BOX_TEXT)
    release.add_argument("--b-windows", default="")
    release.add_argument("--c-windows", default="")
    release.add_argument("--story-logo", type=Path)
    release.add_argument("--story-logo-width-a", default=180, type=int)
    release.add_argument("--story-logo-width-b", default=210, type=int)
    release.add_argument("--story-logo-x", default=42, type=int)
    release.add_argument("--story-logo-y", default=44, type=int)
    release.add_argument("--subtitle-srt", type=Path)
    release.add_argument("--subtitle-font-size", default=42, type=int)
    release.add_argument("--subtitle-margin-v", default=72, type=int)
    release.add_argument("--mix-bg-audio", action="store_true")
    release.add_argument("--voice-volume", default=1.05, type=float)
    release.add_argument("--bg-audio-volume", default=0.28, type=float)
    release.add_argument("--person-height", default=940, type=int)
    release.add_argument("--person-x", default=1190, type=int)
    release.add_argument("--person-y", default=100, type=int)
    release.add_argument("--chroma-color", default="0x00FF00")
    release.add_argument("--chroma-similarity", default=0.16, type=float)
    release.add_argument("--chroma-blend", default=0.08, type=float)
    release.add_argument("--keyer", choices=["chromakey", "colorkey", "rvm"], default="chromakey")
    release.add_argument("--keying-preset-json", type=Path)
    release.add_argument("--demo-render-manifest", type=Path)
    release.add_argument("--person-crop", default="")
    release.add_argument("--person-grade", choices=["none", "natural", "log-soft", "log-strong"], default="none")
    release.add_argument("--library-watermark-text", default="绵羊姐姐原创故事资源")
    release.add_argument("--tail-seconds", default=0.0, type=float)
    release.add_argument("--tail-notice-text", default="有需要联系客服，好作品有偿分享！")
    release.add_argument("--crf", default=19, type=int)
    release.add_argument("--preset", default="medium")
    release.add_argument("--output-scale", default=1, type=int)

    plate_prompt = subparsers.add_parser("plate-prompt", help="故事信息 -> 宝库号底板图生图提示词")
    plate_prompt.add_argument("--story-name", required=True)
    plate_prompt.add_argument("--duration-text", required=True)
    plate_prompt.add_argument("--account-variant", choices=["library", "main"], default="library")
    plate_prompt.add_argument("--story-type", default="儿童故事")
    plate_prompt.add_argument("--theme-elements", default="")
    plate_prompt.add_argument("--style-reference", default="")
    plate_prompt.add_argument("--reference-image", default=None, type=Path)
    plate_prompt.add_argument("--video-box", default="0,416,1080,608")
    plate_prompt.add_argument("--output-dir", required=True, type=Path)
    plate_prompt.add_argument("--slug", default="release_plate")

    publish = subparsers.add_parser("publish-package", help="精简发布物料包：候选帧、封面参考、两张 4:3 封面任务")
    publish.add_argument("--story-name", required=True)
    publish.add_argument("--episode", required=True)
    publish.add_argument("--duration-text", required=True)
    publish.add_argument("--story-text", required=True, type=Path)
    publish.add_argument("--main-video", required=True, type=Path)
    publish.add_argument("--library-video", required=True, type=Path)
    publish.add_argument("--greenscreen-video", default=None, type=Path)
    publish.add_argument("--background-video", default=None, type=Path)
    publish.add_argument("--output-dir", required=True, type=Path)
    publish.add_argument("--candidate-count", default=8, type=int)
    publish.add_argument("--main-frame", default=None, type=int)
    publish.add_argument("--main-person-frame", default=None, type=int)
    publish.add_argument("--main-story-frame", default=None, type=int)
    publish.add_argument("--library-frame", default=None, type=int)
    publish.add_argument("--generate-covers", action="store_true")
    publish.add_argument("--person-reference", default=None, type=Path)
    publish.add_argument("--main-cover-reference", default=None, type=Path)
    publish.add_argument("--library-cover-reference", default=None, type=Path)
    publish.add_argument("--cover-style", choices=["designed", "screenshot"], default="designed")
    publish.add_argument("--story-type", default="童话故事")
    publish.add_argument("--age-range", default="6-8岁")
    publish.add_argument("--roles", default="")

    product = subparsers.add_parser("product-package", help="生成绵羊故事锦囊基础版/进阶版资料包")
    product.add_argument("--story-name", required=True)
    product.add_argument("--slug", default="")
    product.add_argument("--story-text", required=True, type=Path)
    product.add_argument("--script-lines", required=True, type=Path)
    product.add_argument("--narration", required=True, type=Path)
    product.add_argument("--music", required=True, type=Path)
    product.add_argument("--images-dir", required=True, type=Path)
    product.add_argument("--bg-video-with-sub", required=True, type=Path)
    product.add_argument("--bg-video-no-sub", required=True, type=Path)
    product.add_argument("--person-greenscreen", required=True, type=Path)
    product.add_argument("--demo-background-image", type=Path)
    product.add_argument("--story-frame-a-image", type=Path)
    product.add_argument("--demo-logo", type=Path)
    product.add_argument("--demo-logo-width", default=150, type=int)
    product.add_argument("--demo-logo-x", default=24, type=int)
    product.add_argument("--demo-logo-y", default=20, type=int)
    product.add_argument("--annotation-docx", type=Path)
    product.add_argument("--annotation-json", type=Path)
    product.add_argument("--annotation-skill-path", type=Path)
    product.add_argument("--allow-draft-annotation", action="store_true")
    product.add_argument("--keying-preset-json", type=Path)
    product.add_argument("--allow-test-greenscreen", action="store_true")
    product.add_argument("--allow-full-subtitle-bg", action="store_true")
    product.add_argument("--output-root", default="~/Desktop", type=Path)
    product.add_argument("--work-dir", type=Path)
    product.add_argument("--timings-json", type=Path)
    product.add_argument("--allow-even-timings", action="store_true")
    product.add_argument("--whisper-model", default="base")
    product.add_argument("--language", default="zh")
    product.add_argument("--demo-width", default=1920, type=int)
    product.add_argument("--demo-height", default=1080, type=int)
    product.add_argument("--demo-crf", default=20, type=int)
    product.add_argument("--demo-preset", default="veryfast")
    product.add_argument("--demo-person-crop-bottom-ratio", default=0.0, type=float)
    product.add_argument(
        "--demo-person-crop-mode",
        choices=["source-native", "preset", "full-width"],
        default="source-native",
    )
    product.add_argument("--demo-person-vertical-align", choices=["center", "bottom"], default="center")
    product.add_argument("--preview-only", action="store_true")
    product.add_argument("--preview-times", default="0.8,1.5,2.5,37,92")
    product.add_argument("--music-volume", default=0.22, type=float)
    product.add_argument("--narration-volume", default=1.0, type=float)
    product.add_argument("--director-plan", type=Path, help="Codex 原生导演计划；封存静态 PPT 模式必填")
    product.add_argument("--shot-storyboard-compile-receipt", type=Path, help="逐镜故事板同源编译回执")
    product.add_argument("--static-ppt-plan", type=Path, help="由封存故事板编译的静态 PPT 计划")
    product.add_argument("--static-ppt-with-subtitles", type=Path, help="已按计划生成的含字幕静态 PPTX")
    product.add_argument("--static-ppt-without-subtitles", type=Path, help="已按计划生成的无字幕静态 PPTX")

    args = parser.parse_args()
    module_profile = resolve_module_profile(args.module_profile)
    module_execution_mode = resolve_module_execution_mode(
        module_profile, args.module_execution_mode
    )
    export_module_profile(module_profile)
    export_module_execution_mode(module_execution_mode)
    if args.command == "init-project":
        manifest = init_project(args.project_dir, story_name=args.story_name, slug=args.slug, episode=args.episode)
        print(f"已初始化项目：{args.project_dir.expanduser()}")
        print(f"Manifest：{project_paths(args.project_dir).manifest}")
        print(f"本期集数：{manifest['story'].get('episode')}")
    elif args.command == "detect-assets":
        manifest = detect_project_assets(args.project_dir, extract_audio=args.extract_audio)
        print(f"已识别素材并更新：{project_paths(args.project_dir).manifest}")
        for key, value in manifest.get("inputs", {}).items():
            if value:
                print(f"- {key}: {value}")
    elif args.command == "setup-project":
        update_story_info(
            args.project_dir,
            story_name=args.story_name,
            slug=args.slug,
            episode=args.episode,
            story_type=args.story_type,
            image_style=args.image_style,
            age_range=args.age_range,
            duration_text=args.duration_text,
            update_latest_episode_on_delivery=(
                None if args.update_latest_episode_on_delivery is None else args.update_latest_episode_on_delivery == "yes"
            ),
            does_not_count_episode=(
                None if args.does_not_count_episode is None else args.does_not_count_episode == "yes"
            ),
        )
        manifest = detect_project_assets(args.project_dir, extract_audio=args.extract_audio)
        print(f"已保存故事信息并识别素材：{project_paths(args.project_dir).manifest}")
        for key, value in manifest.get("inputs", {}).items():
            if value:
                print(f"- {key}: {value}")
    elif args.command == "set-story-info":
        manifest = update_story_info(
            args.project_dir,
            story_name=args.story_name,
            slug=args.slug,
            episode=args.episode,
            story_type=args.story_type,
            image_style=args.image_style,
            age_range=args.age_range,
            duration_text=args.duration_text,
            update_latest_episode_on_delivery=(
                None if args.update_latest_episode_on_delivery is None else args.update_latest_episode_on_delivery == "yes"
            ),
            does_not_count_episode=(
                None if args.does_not_count_episode is None else args.does_not_count_episode == "yes"
            ),
        )
        print(f"已更新故事信息：{project_paths(args.project_dir).manifest}")
        print(manifest["story"])
    elif args.command == "qa-images":
        report = qa_images(args.project_dir, args.image_dir)
        print(f"已生成图片 QA：{report}")
    elif args.command == "qa-videos":
        report = qa_videos(args.project_dir, args.videos_dir, execution_mode=args.execution_mode)
        print(f"已生成视频 QA：{report}")
    elif args.command == "qa-music":
        report = qa_music(args.project_dir, args.music, args.narration, args.plan_csv)
        print(f"已生成配乐 QA：{report}")
    elif args.command == "qa-release":
        report = qa_release(args.project_dir)
        print(f"已生成发布视频 QA：{report}")
    elif args.command == "qa-product":
        report = qa_product(args.project_dir)
        print(f"已生成资料包 QA：{report}")
    elif args.command == "qa-publish":
        report = qa_publish(args.project_dir)
        print(f"已生成发布物料 QA：{report}")
    elif args.command == "theme-assets":
        outputs = create_theme_asset_request(args.project_dir)
        for label, path in outputs.items():
            print(f"{label}: {path}")
    elif args.command == "auto-keying":
        backend = configured_keying_backend() if args.backend == "auto" else args.backend
        preset = auto_keying(args.project_dir, args.greenscreen, backend=backend)
        print(f"已生成自动抠像参数：{preset}")
    elif args.command == "prepare-release-assets-project":
        outputs = create_theme_asset_request(args.project_dir)
        for label, path in outputs.items():
            print(f"{label}: {path}")
        try:
            preset = auto_keying(args.project_dir, backend=configured_keying_backend())
            print(f"已生成自动抠像参数：{preset}")
        except Exception as exc:
            print(f"[warning] 暂未生成抠像参数：{exc}")
    elif args.command == "final-delivery":
        report = final_delivery(args.project_dir, update_latest_episode=args.update_latest_episode)
        print(f"已生成总交付清单：{report}")
    elif args.command == "doctor-project":
        report = doctor_project(args.project_dir)
        print(f"已生成工程体检报告：{report}")
    elif args.command == "package-release-project":
        run_package_release_project(
            args.project_dir,
            args.variant,
            story_contract_context=args.story_contract_context,
            authorized_binding_repair_reason=args.authorize_binding_repair,
        )
    elif args.command == "preview-release-project":
        run_package_release_project(args.project_dir, args.variant, preview_times=args.times, preview_person_layouts=args.person_layouts, story_contract_context=args.story_contract_context)
    elif args.command == "release-layout-handoff":
        run_release_layout_handoff(args.project_dir, args.times, args.person_layouts)
    elif args.command == "publish-package-project":
        run_publish_package_project(
            args.project_dir,
            args.generate_covers,
            args.main_frame,
            args.library_frame,
            main_person_frame=args.main_person_frame,
            main_story_frame=args.main_story_frame,
        )
    elif args.command == "publish-package-draft-project":
        run_publish_package_draft_project(args.project_dir)
    elif args.command == "semi-auto-status":
        print(semi_auto_status(args.project_dir))
    elif args.command == "register-output":
        manifest = register_manual_output(args.project_dir, args.kind, args.source)
        print(manifest.get("manual_outputs", {}).get(args.kind, ""))
    elif args.command == "qa-manual-outputs":
        print(qa_manual_outputs(args.project_dir))
    elif args.command == "product-package-preflight-project":
        run_product_package_project(
            args.project_dir,
            allow_draft_annotation=False,
            preview_only=True,
            annotation_docx=None,
            annotation_json=None,
            demo_person_crop_mode=args.demo_person_crop_mode,
            demo_person_vertical_align=args.demo_person_vertical_align,
            demo_person_crop_bottom_ratio=args.demo_person_crop_bottom_ratio,
            preview_times=args.preview_times,
            story_contract_context=args.story_contract_context,
        )
    elif args.command == "product-package-project":
        run_product_package_project(
            args.project_dir,
            allow_draft_annotation=args.allow_draft_annotation,
            preview_only=False,
            annotation_docx=args.annotation_docx,
            annotation_json=args.annotation_json,
            demo_person_crop_mode=args.demo_person_crop_mode,
            demo_person_vertical_align=args.demo_person_vertical_align,
            demo_person_crop_bottom_ratio=args.demo_person_crop_bottom_ratio,
            preview_times="",
            story_contract_context=args.story_contract_context,
        )
    elif args.command == "normalize-images":
        command = [
            "--source-dir",
            args.source_dir,
            "--output-dir",
            args.output_dir,
            "--slug",
            args.slug,
        ]
        if args.count:
            command.extend(["--count", str(args.count)])
        run_script("normalize_story_images.py", *command)
    elif args.command == "analyze-pacing":
        analysis = analyze_storyboard_pacing(
            story_text=args.story_file.read_text(encoding="utf-8-sig"),
            output_dir=args.output_dir,
            slug=args.slug,
            narration_path=args.narration,
            whisper_model=args.whisper_model,
            language=args.language,
            whisper_model_dir=args.whisper_model_dir,
        )
        print(f"已生成分镜节奏分析：{analysis.report_path}")
        print(f"已生成建议换行草稿：{analysis.draft_path}")
        print(analysis.summary)
    elif args.command == "prepare":
        command = [
            "--image-dir",
            args.image_dir,
            "--storyboard",
            args.storyboard,
            "--output-dir",
            args.output_dir,
            "--slug",
            args.slug,
            "--short-slug",
            args.short_slug,
        ]
        if args.continuity_contract is not None:
            command.extend(["--continuity-contract", args.continuity_contract])
        if args.storyboard_plan is not None:
            command.extend(["--storyboard-plan", args.storyboard_plan])
        if args.story_contract_context is not None:
            command.extend(["--story-contract-context", args.story_contract_context])
        run_script("prepare_image_video_jobs.py", *command)
    elif args.command == "timing":
        command = [
            "--jobs-csv",
            args.jobs_csv,
            "--narration",
            args.narration,
            "--whisper-model",
            args.whisper_model,
            "--language",
            args.language,
            "--max-duration",
            str(args.max_duration),
        ]
        if args.whisper_model_dir is not None:
            command.extend(["--whisper-model-dir", args.whisper_model_dir])
        if args.adaptive_seconds:
            command.extend(["--duration-mode", "adaptive-seconds"])
        elif args.duration_mode != "fixed":
            command.extend(["--duration-mode", args.duration_mode])
        if args.adaptive_seconds or args.duration_mode != "fixed":
            command.extend(
                [
                    "--min-generation-seconds",
                    str(args.min_generation_seconds),
                    "--max-generation-seconds",
                    str(args.max_generation_seconds),
                ]
            )
        if args.generation_duration_choices.strip():
            command.extend(["--generation-duration-choices", args.generation_duration_choices])
        run_script("apply_narration_durations.py", *command)
    elif args.command == "generate":
        continuity_errors = validate_image_video_jobs(args.jobs_csv)
        if continuity_errors:
            raise ValueError(
                "视觉连续性合同/任务校验失败，已在付费调用前阻断：" + "；".join(continuity_errors)
            )
        # Production provider selection and invocation now pass through the
        # module Port registry; the concrete adapter still delegates to the
        # unchanged runner and provider configuration.
        provider = build_registry_for_profile(
            module_profile,
            load_config(),
            ROOT,
            video_provider_override=args.provider,
            execution_mode=module_execution_mode,
        ).video_generator()
        images_dir = resolve_generate_images_dir(args.jobs_csv, args.images_dir)
        if not args.skip_prompt_review and not args.dry_run:
            decisions_csv = Path(args.prompt_review_csv).expanduser() if args.prompt_review_csv else args.jobs_csv.expanduser().parent / "prompt_review_decisions.csv"
            apply_prompt_review_confirmation(
                args.jobs_csv,
                decisions_csv,
                start_scene=args.start_scene,
                end_scene=args.end_scene,
                scenes=args.scenes,
                limit=args.limit,
            )
        command = [
            "--jobs-csv",
            args.jobs_csv,
            "--images-dir",
            images_dir,
            "--videos-dir",
            args.videos_dir,
            "--start-scene",
            str(args.start_scene),
            "--end-scene",
            str(args.end_scene),
            "--execution-mode",
            args.execution_mode,
        ]
        if args.project_dir:
            command.extend(["--project-dir", str(args.project_dir)])
        if args.scenes:
            command.extend(["--scenes", args.scenes])
        if args.limit:
            command.extend(["--limit", str(args.limit)])
        if args.dry_run:
            command.append("--dry-run")
        command.extend(["--max-submit-first", str(args.max_submit_first)])
        if args.dry_run:
            provider.invoke_batch(command, executor=run_module_command)
        else:
            run_generate_until_complete(
                lambda: provider.invoke_batch(command, executor=run_module_command),
                jobs_csv=args.jobs_csv,
                videos_dir=args.videos_dir,
                start_scene=args.start_scene,
                end_scene=args.end_scene,
                scenes=args.scenes,
                limit=args.limit,
            )
    elif args.command == "rerun-review":
        provider = build_registry_for_profile(
            module_profile,
            load_config(),
            ROOT,
            video_provider_override=args.provider,
            execution_mode=module_execution_mode,
        ).video_generator()
        scenes = reset_redo_scenes(args.jobs_csv, args.videos_dir, args.decisions_csv)
        if not scenes:
            print("审核 CSV 中没有标记为重做的片段。")
            return
        print("需要重跑：" + ", ".join(f"{scene:02d}" for scene in scenes))
        provider.invoke_batch(
            [
                "--jobs-csv",
                args.jobs_csv,
                "--images-dir",
                args.images_dir,
                "--videos-dir",
                args.videos_dir,
                "--scenes",
                ",".join(str(scene) for scene in scenes),
                "--execution-mode",
                args.execution_mode,
            ],
            executor=run_module_command,
        )
    elif args.command == "apply-review":
        command = [
            "--jobs-csv",
            args.jobs_csv,
            "--videos-dir",
            args.videos_dir,
            "--output-dir",
            args.output_dir,
            "--short-slug",
            args.short_slug,
        ]
        if args.decisions_csv:
            command.extend(["--decisions-csv", args.decisions_csv])
        run_script("apply_review_decisions.py", *command)
    elif args.command == "music-request":
        command = [
            "--story-file",
            args.story_file,
            "--output-dir",
            args.output_dir,
            "--slug",
            args.slug,
            "--story-title",
            args.story_title,
        ]
        skill_path = args.skill_path or Path(str(load_config().get("external_tools", {}).get("suno_story_score_skill", ""))).expanduser()
        if skill_path:
            command.extend(["--skill-path", skill_path])
        if args.narration:
            command.extend(["--narration", args.narration])
        if args.jobs_csv:
            command.extend(["--jobs-csv", args.jobs_csv])
        if args.story_contract_context:
            command.extend(["--story-contract-context", args.story_contract_context])
        run_script("prepare_suno_music_request.py", *command)
    elif args.command == "assemble-music":
        run_script(
            "assemble_suno_music.py",
            "--plan-csv",
            args.plan_csv,
            "--clips-dir",
            args.clips_dir,
            "--output",
            args.output,
            "--fade",
            str(args.fade),
        )
    elif args.command == "assemble":
        subtitle_script = args.subtitle_script
        if subtitle_script is None:
            project_root = Path(args.output_dir).expanduser().parent
            subtitle_script = confirmed_subtitle_from_story_run(project_root / "99_项目状态")
            if subtitle_script is None:
                # Compatibility is intentionally limited to projects that do
                # not yet have the native ledger. Once story_run.json exists,
                # an absent or stale subtitle TXT is a hard error rather than
                # permission to reuse story_source.txt or invent new wrapping.
                inferred = project_root / "00_输入素材" / "story_source.txt"
                if inferred.exists():
                    subtitle_script = inferred
        command = [
            "--video-dir",
            args.video_dir,
            "--script",
            args.script,
            "--narration",
            args.narration,
            "--music",
            args.music,
            "--output-dir",
            args.output_dir,
            "--whisper-model",
            args.whisper_model,
            "--language",
            args.language,
            "--subtitle-style",
            args.subtitle_style,
            "--keep-workdir",
        ]
        if subtitle_script is not None:
            command.extend(["--subtitle-script", subtitle_script])
        if args.project_dir is not None:
            command.extend(["--project-dir", args.project_dir])
        if args.artifact_semantic_plan is not None:
            command.extend(["--artifact-semantic-plan", args.artifact_semantic_plan])
        if args.whisper_model_dir is not None:
            command.extend(["--whisper-model-dir", args.whisper_model_dir])
        run_script("synthesize.py", *command)
    elif args.command == "package-release":
        command = [
            "--story-name",
            args.story_name,
            "--duration-text",
            args.duration_text,
            "--age-text",
            args.age_text,
            "--usage-text",
            args.usage_text,
            "--story-type",
            args.story_type,
            "--bg-video",
            args.bg_video,
            "--output-dir",
            args.output_dir,
            "--variant",
            args.variant,
            "--chroma-color",
            args.chroma_color,
            "--chroma-similarity",
            str(args.chroma_similarity),
            "--chroma-blend",
            str(args.chroma_blend),
            "--keyer",
            args.keyer,
            "--person-crop",
            args.person_crop,
            "--person-grade",
            args.person_grade,
            "--library-watermark-text",
            args.library_watermark_text,
            "--tail-seconds",
            str(args.tail_seconds),
            "--tail-notice-text",
            args.tail_notice_text,
            "--video-box",
            args.video_box,
            "--watermark-width",
            str(args.watermark_width),
            "--watermark-opacity",
            str(args.watermark_opacity),
            "--watermark-speed",
            str(args.watermark_speed),
            "--crf",
            str(args.crf),
            "--preset",
            args.preset,
            "--person-height",
            str(args.person_height),
            "--person-x",
            str(args.person_x),
            "--person-y",
            str(args.person_y),
            "--contract-render-spec",
            args.contract_render_spec,
            "--artifact-semantic-plan",
            args.artifact_semantic_plan,
        ]
        for flag, value in (
            ("--bg-image", args.bg_image),
            ("--person-greenscreen", args.person_greenscreen),
            ("--audio-mix", args.audio_mix),
            ("--watermark-logo", args.watermark_logo),
            ("--antipiracy-logo", args.antipiracy_logo),
            ("--frame-image", args.frame_image),
            ("--frame-image-b", args.frame_image_b),
            ("--story-logo", args.story_logo),
            ("--subtitle-srt", args.subtitle_srt),
            ("--keying-preset-json", args.keying_preset_json),
            ("--demo-render-manifest", args.demo_render_manifest),
            ("--main-top-panel", args.main_top_panel),
            ("--main-bottom-panel", args.main_bottom_panel),
            ("--library-top-panel", args.library_top_panel),
            ("--library-bottom-panel", args.library_bottom_panel),
            ("--main-package-spec", args.main_package_spec),
            ("--main-package-receipt", args.main_package_receipt),
            ("--approved-preview-geometry", args.approved_preview_geometry),
        ):
            if value is not None:
                command.extend([flag, value])
        if args.mix_bg_audio:
            command.append("--mix-bg-audio")
        command.extend(
            [
                "--story-box",
                args.story_box,
                "--b-story-box",
                args.b_story_box,
                "--b-windows",
                args.b_windows,
                "--c-windows",
                args.c_windows,
                "--story-logo-width-a",
                str(args.story_logo_width_a),
                "--story-logo-width-b",
                str(args.story_logo_width_b),
                "--story-logo-x",
                str(args.story_logo_x),
                "--story-logo-y",
                str(args.story_logo_y),
                "--subtitle-font-size",
                str(args.subtitle_font_size),
                "--subtitle-margin-v",
                str(args.subtitle_margin_v),
                "--voice-volume",
                str(args.voice_volume),
                "--bg-audio-volume",
                str(args.bg_audio_volume),
                "--output-scale",
                str(args.output_scale),
            ]
        )
        run_script("release_video.py", *command)
    elif args.command == "plate-prompt":
        run_script(
            "release_plate_prompt.py",
            "--story-name",
            args.story_name,
            "--duration-text",
            args.duration_text,
            "--account-variant",
            args.account_variant,
            "--story-type",
            args.story_type,
            "--theme-elements",
            args.theme_elements,
            "--style-reference",
            args.style_reference,
            "--video-box",
            args.video_box,
            "--output-dir",
            args.output_dir,
            "--slug",
            args.slug,
            *([] if args.reference_image is None else ["--reference-image", args.reference_image]),
        )
    elif args.command == "publish-package":
        command = [
            "--story-name",
            args.story_name,
            "--episode",
            args.episode,
            "--duration-text",
            args.duration_text,
            "--story-text",
            args.story_text,
            "--main-video",
            args.main_video,
            "--library-video",
            args.library_video,
            "--output-dir",
            args.output_dir,
            "--candidate-count",
            str(args.candidate_count),
            "--story-type",
            args.story_type,
            "--age-range",
            args.age_range,
            "--roles",
            args.roles,
            "--cover-style",
            args.cover_style,
        ]
        if args.person_reference is not None:
            command.extend(["--person-reference", args.person_reference])
        if args.main_cover_reference is not None:
            command.extend(["--main-cover-reference", args.main_cover_reference])
        if args.library_cover_reference is not None:
            command.extend(["--library-cover-reference", args.library_cover_reference])
        if args.greenscreen_video is not None:
            command.extend(["--greenscreen-video", args.greenscreen_video])
        if args.background_video is not None:
            command.extend(["--background-video", args.background_video])
        if args.main_frame is not None:
            command.extend(["--main-frame", str(args.main_frame)])
        if args.main_person_frame is not None:
            command.extend(["--main-person-frame", str(args.main_person_frame)])
        if args.main_story_frame is not None:
            command.extend(["--main-story-frame", str(args.main_story_frame)])
        if args.library_frame is not None:
            command.extend(["--library-frame", str(args.library_frame)])
        if args.generate_covers:
            command.append("--generate-covers")
        run_script("publish_package.py", *command)
    elif args.command == "product-package":
        command = [
            "--story-name",
            args.story_name,
            "--story-text",
            args.story_text,
            "--script-lines",
            args.script_lines,
            "--narration",
            args.narration,
            "--music",
            args.music,
            "--images-dir",
            args.images_dir,
            "--bg-video-with-sub",
            args.bg_video_with_sub,
            "--bg-video-no-sub",
            args.bg_video_no_sub,
            "--person-greenscreen",
            args.person_greenscreen,
            "--output-root",
            args.output_root,
            "--whisper-model",
            args.whisper_model,
            "--language",
            args.language,
            "--demo-width",
            str(args.demo_width),
            "--demo-height",
            str(args.demo_height),
            "--demo-crf",
            str(args.demo_crf),
            "--demo-preset",
            args.demo_preset,
            "--demo-person-crop-bottom-ratio",
            str(args.demo_person_crop_bottom_ratio),
            "--demo-person-crop-mode",
            args.demo_person_crop_mode,
            "--demo-person-vertical-align",
            args.demo_person_vertical_align,
            "--music-volume",
            str(args.music_volume),
            "--narration-volume",
            str(args.narration_volume),
        ]
        if args.slug:
            command.extend(["--slug", args.slug])
        if args.keying_preset_json is not None:
            command.extend(["--keying-preset-json", args.keying_preset_json])
        if args.demo_background_image is not None:
            command.extend(["--demo-background-image", args.demo_background_image])
        if args.story_frame_a_image is not None:
            command.extend(["--story-frame-a-image", args.story_frame_a_image])
        if args.demo_logo is not None:
            command.extend(["--demo-logo", args.demo_logo])
            command.extend(["--demo-logo-width", str(args.demo_logo_width)])
            command.extend(["--demo-logo-x", str(args.demo_logo_x)])
            command.extend(["--demo-logo-y", str(args.demo_logo_y)])
        if args.annotation_docx is not None:
            command.extend(["--annotation-docx", args.annotation_docx])
        if args.annotation_json is not None:
            command.extend(["--annotation-json", args.annotation_json])
        annotation_skill_path = args.annotation_skill_path or annotation_skill_path_from_config()
        if annotation_skill_path is not None:
            command.extend(["--annotation-skill-path", annotation_skill_path])
        if args.allow_draft_annotation:
            command.append("--allow-draft-annotation")
        if args.allow_test_greenscreen:
            command.append("--allow-test-greenscreen")
        if args.allow_full_subtitle_bg:
            command.append("--allow-full-subtitle-bg")
        if args.work_dir is not None:
            command.extend(["--work-dir", args.work_dir])
        if args.timings_json is not None:
            command.extend(["--timings-json", args.timings_json])
        if args.allow_even_timings:
            command.append("--allow-even-timings")
        if args.preview_only:
            command.append("--preview-only")
            command.extend(["--preview-times", args.preview_times])
        for option, value in (
            ("--director-plan", args.director_plan),
            ("--shot-storyboard-compile-receipt", args.shot_storyboard_compile_receipt),
            ("--static-ppt-plan", args.static_ppt_plan),
            ("--static-ppt-with-subtitles", args.static_ppt_with_subtitles),
            ("--static-ppt-without-subtitles", args.static_ppt_without_subtitles),
        ):
            if value is not None:
                command.extend([option, value])
        run_script("product_package.py", *command)


def run_script(script_name: str, *args) -> None:
    script = Path(script_name).expanduser()
    if not script.is_absolute():
        script = ROOT / script
    command = [sys.executable, str(script)] + [str(arg) for arg in args]
    print("运行：", " ".join(command), flush=True)
    result = subprocess.run(command)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def run_module_command(command: Sequence[str]) -> None:
    """Execute an adapter-owned command without knowing its runner or provider."""

    normalized = [str(part) for part in command]
    print("运行模块：", " ".join(normalized), flush=True)
    result = subprocess.run(normalized)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def annotation_skill_path_from_config() -> Path | None:
    value = str(load_config().get("external_tools", {}).get("story_performance_script_skill", "")).strip()
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def build_abc_scene_windows(duration: float, subtitle_srt: Path | None = None) -> tuple[str, str]:
    """Return B/C windows snapped to speech gaps.

    A remains the default story layout.  When the audited subtitle stream ends
    several seconds before the program, the subtitle-free host moral/outro is
    assigned to C so a drifting presenter cannot be stranded at the edge of
    the A composition.
    """
    cue_ends: list[float] = []
    final_cue_end: float | None = None
    if subtitle_srt is not None and subtitle_srt.exists():
        from release_video import parse_srt

        all_cue_ends = [end for _start, end, _text in parse_srt(subtitle_srt)]
        final_cue_end = max(all_cue_ends) if all_cue_ends else None
        cue_ends = [end for end in all_cue_ends if 1.0 < end < duration - 1.0]
    boundaries: list[float] = []
    target = 15.0
    while target < duration - 8.0:
        nearby = [value for value in cue_ends if abs(value - target) <= 5.0 and (not boundaries or value - boundaries[-1] >= 8.0)]
        chosen = min(nearby, key=lambda value: abs(value - target)) if nearby else target
        if not boundaries or chosen - boundaries[-1] >= 8.0:
            boundaries.append(chosen)
        target = chosen + 18.0
    # Boundary snapping omits the final second; ending detection must still
    # inspect the complete subtitle stream, including its last cue.
    tail_c_start = final_cue_end if final_cue_end is not None and duration - final_cue_end >= 4.0 else None
    if tail_c_start is not None and all(abs(tail_c_start - value) >= 0.05 for value in boundaries):
        boundaries.append(tail_c_start)
        boundaries.sort()
    points = [0.0, *boundaries, duration]
    segments = [(points[index], points[index + 1]) for index in range(len(points) - 1) if points[index + 1] - points[index] >= 1.0]
    modes = ["c", "b", "a"]
    assigned = [modes[index % len(modes)] for index in range(len(segments))]
    if assigned:
        assigned[-1] = "c" if tail_c_start is not None else "a"
    b_windows: list[str] = []
    c_windows: list[str] = []
    for (start, end), mode in zip(segments, assigned):
        value = f"{start:.3f}-{end:.3f}"
        if mode == "b":
            b_windows.append(value)
        elif mode == "c":
            c_windows.append(value)
    return ",".join(b_windows), ",".join(c_windows)


def preview_times_with_b_coverage(value: str, b_windows: str, c_windows: str = "") -> str:
    """Add one deterministic review frame inside every B/C interval."""
    raw_times = [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
    times = [max(0.0, float(part)) for part in raw_times]
    occupied_filenames = {int(round(item)) for item in times}
    for raw_window in ",".join(part for part in (b_windows, c_windows) if part).replace("，", ",").split(","):
        raw_window = raw_window.strip()
        if not raw_window:
            continue
        raw_start, raw_end = raw_window.split("-", 1)
        start, end = float(raw_start), float(raw_end)
        midpoint = (start + end) / 2
        if int(round(midpoint)) not in occupied_filenames:
            times.append(midpoint)
            occupied_filenames.add(int(round(midpoint)))
    return ",".join(f"{item:.3f}" for item in times)


def preview_times_with_keying_coverage(value: str, preset_path: Path | None) -> str:
    """Include the real neutral anchor and large-gesture frames in short preview."""

    times = [max(0.0, float(part.strip())) for part in value.replace("，", ",").split(",") if part.strip()]
    if preset_path is not None and preset_path.is_file():
        try:
            preset = json.loads(preset_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            preset = {}
        for field in ("presenter_initial_anchor_frame_seconds", "presenter_gesture_review_frame_seconds"):
            raw = preset.get(field)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                value_seconds = max(0.0, float(raw))
                if int(round(value_seconds)) not in {int(round(item)) for item in times}:
                    times.append(value_seconds)
    return ",".join(f"{item:.3f}" for item in times)


def preview_times_with_library_tail_coverage(value: str, duration: float, tail_seconds: float) -> str:
    """Include samples immediately before and inside the expected sales tail."""

    from release_video import resolved_tail_seconds

    times = [max(0.0, float(part.strip())) for part in value.replace("，", ",").split(",") if part.strip()]
    tail_duration = resolved_tail_seconds(duration, tail_seconds)
    tail_start = max(0.0, duration - tail_duration)
    candidates = (
        max(0.0, tail_start - 0.5),
        min(max(0.0, duration - 0.04), tail_start + min(1.0, max(0.2, tail_duration / 4))),
        max(0.0, duration - 0.2),
    )
    for candidate in candidates:
        if int(round(candidate)) not in {int(round(item)) for item in times}:
            times.append(candidate)
    return ",".join(f"{item:.3f}" for item in times)


def require_preferred_keyer(preset_path: Path, preferred_keyer: str) -> None:
    """Refuse a production/preview fallback from the configured keyer."""

    preferred = str(preferred_keyer or "").strip()
    if not preferred:
        return
    payload = json.loads(preset_path.read_text(encoding="utf-8"))
    actual = str(payload.get("keyer") or "").strip()
    if actual != preferred:
        raise RuntimeError(
            "当前抠像 preset 与配置的 preferred_keyer 不一致，拒绝静默降级进入预览或正式交付："
            f"preferred={preferred} actual={actual}。"
            f"请重新运行 auto-keying --backend {preferred}。"
        )


def load_release_person_layout(
    path: Path,
    keying_preset: Path | None,
    *,
    story_box: str = "210,270,910,512",
) -> dict[str, int]:
    """Load and hard-check the source-native, center-aligned A-shot anchor."""

    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "story-release-person-layout/v1":
        raise ValueError("release_layout.json schema_version 无效")
    if payload.get("dynamic_repositioning") is not False:
        raise ValueError("release_layout.json 必须禁止动态跟随移位")
    if payload.get("person_crop") not in (None, "", []):
        raise ValueError("release_layout.json 不得通过裁切解决大手势布局")
    keying_payload: dict[str, object] = {}
    if keying_preset is not None:
        expected = str(payload.get("keying_preset_sha256") or "")
        actual = hashlib.sha256(keying_preset.read_bytes()).hexdigest()
        if expected != actual:
            raise ValueError("release_layout.json 未绑定当前 keying_preset.json")
        keying_payload = json.loads(keying_preset.read_text(encoding="utf-8"))
    result: dict[str, int] = {}
    for field in ("person_height", "person_x", "person_y"):
        raw = payload.get(field)
        if isinstance(raw, bool):
            raise ValueError(f"release_layout.json {field} 必须为整数")
        value = int(raw)
        if field == "person_height" and value <= 0:
            raise ValueError("release_layout.json person_height 必须大于 0")
        result[field] = value
    if keying_payload:
        policy = str(keying_payload.get("person_layout_policy") or "")
        if policy != PRESENTER_LAYOUT_POLICY:
            raise ValueError("keying_preset.json 缺少 source-native 固定中轴布局策略")
        source_height = int(keying_payload.get("rvm_input_height") or 1080)
        if result["person_height"] != source_height:
            raise ValueError("release_layout.json 禁止缩放人物：person_height 必须等于 RVM 原始高度")
        if result["person_y"] != 0:
            raise ValueError("release_layout.json 禁止改变人物原始纵向位置：person_y 必须为 0")
        initial_bbox = keying_payload.get("presenter_initial_subject_bbox")
        if not isinstance(initial_bbox, list) or len(initial_bbox) != 4:
            raise ValueError("keying_preset.json 缺少开场中性帧人物框，无法计算固定中轴")
        try:
            story_x, _story_y, story_width, _story_height = (
                int(float(part.strip())) for part in str(story_box).replace("，", ",").split(",")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("story_box 必须为 x,y,width,height") from exc
        story_right = story_x + story_width
        if not 0 < story_right < 1920:
            raise ValueError("story_box 未形成有效的右侧空白矩形")
        anchor = compile_fixed_anchor(
            [],
            active_windows=[],
            initial_subject_bbox=[int(value) for value in initial_bbox],
            right_blank_rect=[story_right, 0, 1920 - story_right, source_height],
            canvas_width=1920,
        )
        expected_x = int(anchor["anchor_x"])
        if result["person_x"] != expected_x:
            raise ValueError(
                "release_layout.json 人物中轴未对齐右侧空白矩形中轴："
                f"person_x={result['person_x']} expected={expected_x}"
            )
    return result


def release_encode_guard_action(
    previous: dict[str, Any],
    *,
    binding_fingerprint: str,
    output_is_current: bool,
    max_technical_repairs: int = 1,
    binding_repair_authorized: bool = False,
) -> str:
    """Choose reuse/start/technical-repair without reopening aesthetic loops."""

    if not previous:
        return "start"
    same_binding = previous.get("binding_fingerprint") == binding_fingerprint
    if (
        same_binding
        and output_is_current
        and previous.get("status") in {"completed", "adopted_existing"}
    ):
        return "reuse"
    repairs = int(previous.get("technical_repair_count") or 0)
    if not same_binding and binding_repair_authorized and repairs < max_technical_repairs:
        return "authorized_binding_repair"
    if not same_binding:
        return "block_binding_change"
    if repairs < max_technical_repairs and previous.get("status") in {
        "running", "running_technical_repair", "running_authorized_binding_repair", "failed_or_interrupted",
        "completed", "adopted_existing",
    }:
        return "technical_repair"
    return "block_repair_limit"


def run_package_release_project(
    project_dir: Path,
    variant: str,
    preview_times: str | None = None,
    preview_person_layouts: str | None = None,
    story_contract_context: Path | None = None,
    authorized_binding_repair_reason: str = "",
) -> None:
    paths = project_paths(project_dir)
    require_native_run_inputs(paths.status)
    is_preview = preview_times is not None
    preview_dir = paths.status / "release_preview_frames"
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") if is_preview else ""
    if is_preview:
        reset_release_preview_dir(preview_dir)
    manifest = detect_project_assets(paths.root, extract_audio=True)
    manifest = refresh_project_outputs(paths.root)
    if not is_preview:
        # Full renders are expensive and irreversible enough that a reviewed
        # preview, bound to the exact preset/search/evidence hashes, is a hard
        # prerequisite even when this legacy CLI is invoked directly.
        from story_evidence import review_bundle_is_current, review_passes

        preview_bundle = paths.status / "reviews" / "release_preview_bundle.json"
        preview_review = paths.status / "reviews" / "release_preview_review.json"
        try:
            review_payload = json.loads(preview_review.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("全片渲染前必须完成独立发布预览审核") from exc
        if not review_bundle_is_current(preview_bundle) or not review_passes(review_payload, artifact=preview_bundle):
            raise RuntimeError("发布预览审核未通过，或审核后的択像/布局产物已变更，拒绝渲染全片")
        if story_contract_context is not None:
            from keying_quality import keying_preset_lock_issues

            preset_path = paths.release / "keying" / "keying_preset.json"
            lock_issues = keying_preset_lock_issues(preset_path)
            if lock_issues:
                raise RuntimeError("抠像 preset 未绑定当前机器 QA 与独立审核，拒绝渲染全片：" + "；".join(lock_issues))
    config = load_config()
    story = manifest["story"]
    outputs = manifest["outputs"]
    inputs = manifest["inputs"]
    age_range = str(story.get("age_range") or "").strip()
    manual_overrides = story.get("manual_overrides") if isinstance(story.get("manual_overrides"), dict) else {}
    if story_contract_context is not None and (not age_range or manual_overrides.get("age_range") is not True):
        raise ValueError("age_range_user_input_required: 请先由用户指定适合年龄，再生成发布包装")
    no_sub_bg_video = first_existing(
        outputs.get("background_video_no_sub"),
        paths.assembly / "story_no_subs_bgm.mp4",
    )
    if story_contract_context is not None:
        # Required-v1 composites exactly one subtitle layer.  A burned-in
        # fallback plus subtitle_srt produced the double subtitles seen in the
        # first real short test.
        main_bg_video = no_sub_bg_video
        library_bg_video = no_sub_bg_video
    else:
        main_bg_video = no_sub_bg_video or first_existing(
            outputs.get("background_video_with_sub"),
            paths.assembly / "story_sales_subs_bgm.mp4",
            outputs.get("demo_voice_bgm"),
            paths.assembly / "story_demo_voice_bgm.mp4",
        )
        library_bg_video = no_sub_bg_video or first_existing(
            outputs.get("demo_voice_bgm"),
            paths.assembly / "story_demo_voice_bgm.mp4",
            outputs.get("background_video_with_sub"),
            paths.assembly / "story_sales_subs_bgm.mp4",
        )
    if main_bg_video is None and library_bg_video is None:
        raise FileNotFoundError("缺少背景成片：请先完成 ⑪ 合成背景成片。")
    # Release is a full spoken program (opening + body + closing).  The
    # manifest narration field may intentionally contain only the story body;
    # using it as the render clock truncated this project's 179.86 s program
    # to 155.32 s and made terminal-frame QA impossible.
    audio_mix = first_existing(
        confirmed_audio_from_story_run(paths.status),
        outputs.get("demo_voice_bgm"),
        paths.assembly / "story_demo_voice_bgm.mp4",
        inputs.get("narration"),
    )
    greenscreen = first_existing(inputs.get("greenscreen_video"))
    theme_dir = paths.release / "theme_assets"
    # 第 12 步只生成一个统一故事框；非严格 QA 只负责从源图导出 A 框。
    try:
        qa_theme_assets(paths, strict=False)
    except Exception:
        pass
    main_plate = first_release_asset(theme_dir / "main_release_plate.png", theme_dir / "release_plate.png", outputs.get("release_plate_image"))
    library_plate = first_release_asset(theme_dir / "library_release_plate.png", outputs.get("library_release_plate_image"))
    main_top_panel = first_existing(theme_dir / "main_release_plate_top.png")
    main_bottom_panel = first_existing(theme_dir / "main_release_plate_bottom.png")
    library_top_panel = first_existing(theme_dir / "library_release_plate_top.png")
    library_bottom_panel = first_existing(theme_dir / "library_release_plate_bottom.png")
    main_package_spec = first_existing(outputs.get("main_package_spec"), theme_dir / "main_package_spec.json")
    main_package_receipt = first_existing(theme_dir / "main_package_generation_receipt.json")
    bg_image = first_existing(outputs.get("main_background_image"), theme_dir / "main_background_16x9.png")
    frame_a = first_existing(outputs.get("story_frame_a"), theme_dir / "story_frame_a.png")
    frame_b = frame_a
    confirmed_spoken_srt = ensure_confirmed_spoken_timeline_srt(paths.status, paths.assembly)
    authoritative_timeline_receipt = story_run_artifact_path(
        paths.status,
        "authoritative_timeline_receipt",
    )
    subtitle_srt = first_existing(
        # A reviewed short-cue full program SRT is the strongest release
        # source.  Body-only sales subtitles belong to customer background
        # deliverables, not the public release videos.
        paths.assembly / "story_full_subtitles.srt",
        paths.assembly / "story_semantic_timeline.srt",
        confirmed_spoken_srt,
        outputs.get("subtitles_srt"),
        paths.assembly / "story_subtitles.srt",
        outputs.get("sales_subtitles_srt"),
        paths.assembly / "story_sales_subtitles.srt",
    )
    release_defaults = config.get("release_defaults", {})
    release_contract_spec: Path | None = None
    release_contract_payload: dict | None = None
    release_contract_args: dict[str, object] = {}
    release_semantic_plan: dict | None = None
    if story_contract_context is not None:
        release_contract_spec = compile_release_render_spec(
            story_contract_context,
            paths.status / "contracts" / "consumers" / "release_video.compiled.json",
        )
        release_contract_payload = json.loads(
            release_contract_spec.read_text(encoding="utf-8")
        )
        release_contract_args = release_argument_overrides(
            release_contract_payload, "main"
        )
        # Required-v1 uses the reviewed ImageGen-native top/bottom panels.
        # Whole-canvas plates remain legacy-only because they can hide or
        # distort the real center video aperture.
        main_plate = None
        library_plate = None
        release_semantic_plan = load_current_artifact_semantic_plan(paths.root)
    brand_assets = config.get("brand_assets", {})
    story_logo = first_existing(brand_assets.get("story_logo"), brand_assets.get("logo"))
    watermark_logo = first_existing(brand_assets.get("watermark_logo"))
    if story_logo is not None and watermark_logo is not None and story_logo.resolve() == watermark_logo.resolve():
        watermark_logo = None
    antipiracy_logo = first_existing(brand_assets.get("antipiracy_logo"), brand_assets.get("logo"))
    # Main and library are separate output videos.  The same reviewed program
    # Logo may therefore be the fixed main mark and the moving library
    # anti-piracy mark without ever appearing twice in one frame.
    if release_contract_payload is not None:
        story_logo, watermark_logo, antipiracy_logo = contract_release_brand_paths(
            release_contract_payload,
            story_logo,
            watermark_logo,
            antipiracy_logo,
        )
    keying_preset = first_existing(outputs.get("keying_preset"), paths.release / "keying" / "keying_preset.json")
    if keying_preset is None and greenscreen is not None:
        try:
            keying_preset = auto_keying(paths.root, backend=configured_keying_backend())
        except Exception as exc:
            print(f"[warning] 自动抠像参数暂不可用：{exc}")
    if keying_preset is not None:
        require_preferred_keyer(keying_preset, str(release_defaults.get("preferred_keyer") or ""))
    layout_story_box = release_contract_args.get("story_box") or release_defaults.get(
        "story_box", "210,270,910,512"
    )
    if isinstance(layout_story_box, (list, tuple)):
        layout_story_box = ",".join(str(value) for value in layout_story_box)
    release_person_layout = load_release_person_layout(
        paths.release / "release_layout.json",
        keying_preset,
        story_box=str(layout_story_box),
    )

    requested_variant = variant
    if requested_variant == "auto":
        requested_variant = "both" if greenscreen and audio_mix and bg_image and main_bg_video else "library"

    missing: list[str] = []
    if requested_variant in {"both", "main"}:
        for label, value in (
            ("主账号 16:9 背景图 main_background_16x9.png", bg_image),
            ("A 景透明故事框 story_frame_a.png", frame_a),
            ("无字幕背景视频 story_no_subs_bgm.mp4", main_bg_video),
            ("完整口播音频", audio_mix),
            ("绿幕视频", greenscreen),
        ):
            if value is None:
                missing.append(label)
        for label, value in (
            ("主账号 ImageGen 顶部包装板 main_release_plate_top.png", main_top_panel),
            ("主账号 ImageGen 底部包装板 main_release_plate_bottom.png", main_bottom_panel),
            ("主账号包装参考合同 main_package_spec.json", main_package_spec),
            ("主账号包装生成回执 main_package_generation_receipt.json", main_package_receipt),
        ):
            if story_contract_context is not None and value is None:
                missing.append(label)
        if release_defaults.get("b_windows") and frame_a is None:
            missing.append("统一透明故事框 story_frame_a.png")
    if requested_variant in {"both", "library"}:
        if antipiracy_logo is None:
            missing.append("宝库号审核通过的移动防盗 Logo antipiracy_logo")
        if library_bg_video is None:
            missing.append("宝库号无字幕背景视频 story_no_subs_bgm.mp4")
        if library_plate is None and story_contract_context is None:
            missing.append("宝库号底板 library_release_plate.png")
        if story_contract_context is not None:
            if library_top_panel is None:
                missing.append("宝库号 ImageGen 顶部包装板 library_release_plate_top.png")
            if library_bottom_panel is None:
                missing.append("宝库号 ImageGen 底部包装板 library_release_plate_bottom.png")
    if story_contract_context is not None:
        missing.extend(f"主账号包装链路 {issue}" for issue in main_package_receipt_issues(paths))
    if missing:
        request = create_theme_asset_request(paths.root)["request"]
        raise FileNotFoundError(
            "发布视频缺少必要素材：" + "、".join(missing) + f"。请先点击 ⑫ 生成/打开发布素材任务书，把复制的指令发给 Codex；Codex 生成素材后，再点 ⑬ 接收并体检发布素材：{request}"
        )
    qa_theme_assets(paths, strict=story_contract_context is None)

    severe_presenter_windows: list[tuple[float, float]] = []
    if requested_variant in {"both", "main"} and keying_preset is not None and release_person_layout:
        keying_payload = json.loads(keying_preset.read_text(encoding="utf-8"))
        if str(keying_payload.get("keyer") or "") == "rvm":
            foreground = Path(str(keying_payload.get("rvm_foreground_video") or "")).expanduser()
            overflow = scan_rvm_body_overflow(
                foreground,
                fixed_anchor_x=int(release_person_layout["person_x"]),
                canvas_width=1920,
                source_width=int(keying_payload.get("rvm_input_width") or 1920),
                report_path=paths.status / "release_preview_frames" / "presenter_body_overflow_report.json",
            )
            severe_presenter_windows = [
                (float(window[0]), float(window[1]))
                for window in overflow.get("severe_windows", [])
                if isinstance(window, list) and len(window) == 2
            ]

    def merge_scene_windows(base: str, additions: list[tuple[float, float]]) -> str:
        windows: list[tuple[float, float]] = []
        for raw in base.replace("，", ",").split(","):
            part = raw.strip()
            if not part:
                continue
            raw_start, raw_end = part.split("-", 1)
            windows.append((float(raw_start), float(raw_end)))
        windows.extend(additions)
        if not windows:
            return ""
        merged: list[list[float]] = []
        for start, end in sorted(windows):
            if merged and start <= merged[-1][1] + 0.05:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return ",".join(f"{start:.3f}-{end:.3f}" for start, end in merged)

    def resolve_scene_windows(bg_video: Path) -> tuple[str, str]:
        configured_b = str(release_defaults.get("b_windows", "") or "").strip()
        configured_c = str(release_defaults.get("c_windows", "") or "").strip()
        auto_b, auto_c = build_abc_scene_windows(probe_duration(bg_video), subtitle_srt)
        b_windows = configured_b if configured_b and configured_b.lower() != "auto" else auto_b
        # Severe torso overflow is resolved only by substituting the existing
        # B scene. The approved A presenter transform remains untouched. Hand
        # and forearm overflow was removed by the body-core scanner upstream.
        b_windows = merge_scene_windows(b_windows, severe_presenter_windows)
        c_windows = configured_c if configured_c and configured_c.lower() != "auto" else auto_c
        return b_windows, c_windows

    def release_command(selected_variant: str, bg_video: Path, plate_image: Path | None) -> list[object]:
        effective = dict(release_defaults)
        if selected_variant == "main":
            effective.update({key: value for key, value in release_contract_args.items() if key != "safe_regions"})
            effective.update(release_person_layout)
        release_subtitle_srt = subtitle_srt
        if release_semantic_plan is not None and subtitle_srt is not None:
            # Both account variants are public release videos.  Their subtitle
            # layer covers the complete spoken program: presenter opening,
            # narrative body and closing moral.  The body-only
            # background_subtitles policy belongs exclusively to the customer
            # background-video asset in 06_资料包.
            artifact = release_semantic_subtitle_artifact(selected_variant)
            release_subtitle_srt = compile_release_subtitle_srt(
                subtitle_srt,
                release_semantic_plan,
                artifact,
                paths.status / "release_semantics" / f"{selected_variant}_{artifact}.srt",
                semantic_source=(
                    paths.root
                    / str(release_semantic_plan["semantic_source"]["project_relative_path"])
                ),
            )
        # tail_seconds=0 means the library renderer's automatic sales-protect
        # window (normally about the final 30–50 seconds).  It must not be
        # replaced by the often one-second post-speech silence; doing so makes
        # a technically present but commercially ineffective tail.
        effective_tail_seconds = float(release_defaults.get("tail_seconds", 0.0))
        command: list[object] = [
            "--story-name",
            story.get("name", ""),
            "--duration-text",
            story.get("duration_text") or "待定",
            "--age-text",
            age_range,
            "--bg-video",
            bg_video,
            "--output-dir",
            paths.release,
            "--variant",
            selected_variant,
            "--video-box",
            effective.get("video_box", "0,416,1080,608"),
            "--story-box",
            effective.get("story_box", "210,270,910,512"),
            "--story-bleed",
            str(release_defaults.get("story_bleed", 0)),
            "--background-blur",
            str(release_defaults.get("background_blur", 14)),
            "--b-story-box",
            release_defaults.get("b_story_box", DEFAULT_B_STORY_BOX_TEXT),
            "--crf",
            str(release_defaults.get("crf", 17)),
            "--preset",
            release_defaults.get("preset", "medium"),
            "--output-scale",
            str(release_defaults.get("output_scale", 1)),
            "--person-height",
            str(effective.get("person_height", 900)),
            "--person-x",
            str(effective.get("person_x", 1200)),
            "--person-y",
            str(effective.get("person_y", 105)),
            "--keyer",
            release_defaults.get("keyer", "colorkey"),
            "--chroma-color",
            release_defaults.get("chroma_color", "0x00FF00"),
            "--chroma-similarity",
            str(release_defaults.get("chroma_similarity", 0.14)),
            "--chroma-blend",
            str(release_defaults.get("chroma_blend", 0.02)),
            "--person-grade",
            release_defaults.get("person_grade", "log-soft"),
            "--watermark-width",
            str(release_defaults.get("watermark_width", 96)),
            "--watermark-opacity",
            str(release_defaults.get("watermark_opacity", 0.78)),
            "--watermark-speed",
            str(release_defaults.get("watermark_speed", 0.45)),
            "--tail-seconds",
            str(effective_tail_seconds),
            "--tail-notice-text",
            str(release_defaults.get("tail_notice_text", "有需要联系客服，好作品有偿分享！")),
            "--story-logo-width-a",
            str(effective.get("story_logo_width_a", 150)),
            "--story-logo-width-b",
            str(release_defaults.get("story_logo_width_b", 175)),
            "--story-logo-x",
            str(effective.get("story_logo_x", 42)),
            "--story-logo-y",
            str(effective.get("story_logo_y", 44)),
            "--subtitle-font-size",
            str(release_defaults.get("subtitle_font_size", 42)),
            "--subtitle-margin-v",
            str(effective.get("subtitle_margin_v", 72)),
        ]
        command.extend(
            formal_release_audio_arguments(
                release_defaults,
                strict_contract=story_contract_context is not None,
            )
        )
        if release_contract_spec is not None:
            # Both short preview and formal Release inherit the same reviewed
            # real-material Demo geometry. The full customer Demo remains an
            # independent downstream artifact and no longer gates Release.
            demo_preview_manifest = current_demo_preview_manifest(paths.root, manifest)
            command.extend([
                "--contract-render-spec", release_contract_spec,
                "--artifact-semantic-plan", semantic_plan_path(paths.root),
                "--demo-render-manifest", demo_preview_manifest,
            ])
            if not is_preview:
                command.extend([
                    "--approved-preview-geometry",
                    paths.status / "release_preview_frames" / f"release_geometry_manifest_{selected_variant}.json",
                ])
        optional: list[tuple[str, object | None]] = [
            ("--plate-image", plate_image),
            ("--main-top-panel", main_top_panel),
            ("--main-bottom-panel", main_bottom_panel),
            ("--library-top-panel", library_top_panel),
            ("--library-bottom-panel", library_bottom_panel),
            ("--main-package-spec", main_package_spec),
            ("--main-package-receipt", main_package_receipt),
            ("--keying-preset-json", keying_preset),
        ]
        person_crop = str(release_defaults.get("person_crop", "") or "").strip()
        if person_crop:
            command.extend(["--person-crop", person_crop])
        if selected_variant == "main":
            optional.extend(
                [
                    ("--watermark-logo", watermark_logo),
                    ("--story-logo", story_logo),
                    ("--bg-image", bg_image),
                    ("--person-greenscreen", greenscreen),
                    ("--audio-mix", audio_mix),
                    ("--frame-image", frame_a),
                    ("--frame-image-b", frame_b),
                    ("--subtitle-srt", release_subtitle_srt),
                ]
            )
            b_windows, c_windows = resolve_scene_windows(bg_video)
            if b_windows:
                command.extend(["--b-windows", b_windows])
            if c_windows:
                command.extend(["--c-windows", c_windows])
        if selected_variant == "library":
            optional.extend(
                [
                    # Library uses two counter-moving copies of the approved
                    # anti-piracy bitmap, rendered by release_video.  Do not
                    # additionally pass the fixed main-account story_logo.
                    ("--antipiracy-logo", antipiracy_logo),
                    ("--audio-mix", audio_mix),
                    ("--subtitle-srt", release_subtitle_srt),
                ]
            )
        if preview_times is not None:
            effective_preview_times = preview_times_with_keying_coverage(preview_times, keying_preset)
            if selected_variant == "main":
                b_windows, c_windows = resolve_scene_windows(bg_video)
                effective_preview_times = preview_times_with_b_coverage(effective_preview_times, b_windows, c_windows)
            else:
                effective_preview_times = preview_times_with_library_tail_coverage(
                    effective_preview_times,
                    probe_duration(bg_video),
                    effective_tail_seconds,
                )
            command.extend(["--preview-dir", preview_dir, "--preview-times", effective_preview_times])
            if selected_variant == "main" and preview_person_layouts:
                command.extend(["--preview-person-layouts", preview_person_layouts])
        for flag, value in optional:
            if value is not None:
                command.extend([flag, value])
        return command

    def sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def run_release_variant(selected_variant: str, bg_video: Path, plate_image: Path | None) -> None:
        command = release_command(selected_variant, bg_video, plate_image)
        if is_preview:
            run_script("release_video.py", *command)
            return
        output = paths.release / ("主账号发布视频.mp4" if selected_variant == "main" else "宝库号发布视频.mp4")
        file_bindings: dict[str, str] = {}
        for value in command:
            candidate = Path(str(value)).expanduser()
            if candidate.is_file():
                file_bindings[str(candidate.resolve())] = sha256_file(candidate)
        fingerprint = hashlib.sha256(json.dumps({
            "variant": selected_variant,
            "command": [str(value) for value in command],
            "input_artifact_hashes": file_bindings,
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        guard_dir = paths.status / "release_encode_guards"
        guard_dir.mkdir(parents=True, exist_ok=True)
        guard = guard_dir / f"{selected_variant}.json"
        try:
            previous = json.loads(guard.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
        recorded_sha = str(previous.get("output_sha256") or "")
        output_is_current = bool(
            output.is_file()
            and output.stat().st_size > 0
            and recorded_sha
            and recorded_sha == sha256_file(output)
        )
        guard_action = release_encode_guard_action(
            previous,
            binding_fingerprint=fingerprint,
            output_is_current=output_is_current,
            binding_repair_authorized=bool(authorized_binding_repair_reason.strip()),
        )
        if guard_action == "reuse":
            print(f"复用已绑定的 {selected_variant} 正式发布视频：{output}", flush=True)
            return
        if guard_action.startswith("block_"):
            detail = (
                "正式预演绑定已变化，禁止把审美改动伪装成技术修复"
                if guard_action == "block_binding_change"
                else "同一绑定的一次技术修复额度已用完"
            )
            raise RuntimeError(
                f"max_full_resolution_encodes_reached:{selected_variant}; {detail}；"
                "请保留当前成片、执行局部修复或由用户接受当前版本。"
            )
        if output.is_file() and not previous:
            save_json(guard, {
                "schema_version": "story-release-encode-guard/v1",
                "variant": selected_variant,
                "status": "adopted_existing",
                "attempt_count": 1,
                "binding_fingerprint": fingerprint,
                "input_artifact_hashes": file_bindings,
                "output": str(output),
                "output_sha256": sha256_file(output),
                "note": "保护性接管实施前已有成片，未覆盖重编码。",
            })
            print(f"保留并接管已有 {selected_variant} 正式发布视频：{output}", flush=True)
            return
        authorized_binding_repair = guard_action == "authorized_binding_repair"
        technical_repair = guard_action in {"technical_repair", "authorized_binding_repair"}
        technical_repair_count = int(previous.get("technical_repair_count") or 0) + int(technical_repair)
        repair_authorization: dict[str, Any] = {}
        if authorized_binding_repair:
            previous_status = str(previous.get("status") or "")
            incomplete_previous = previous_status in {
                "running",
                "running_technical_repair",
                "running_authorized_binding_repair",
                "failed_or_interrupted",
            }
            if not output_is_current and not incomplete_previous:
                raise RuntimeError(
                    f"authorized_binding_repair_requires_current_output:{selected_variant}; "
                    "旧成片与门禁 SHA 不一致，不能建立可追溯备份。"
                )
            backup_dir = paths.status / "release_encode_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = None
            backup_sha = ""
            backup_kind = "none"
            if output.is_file() and output.stat().st_size > 0:
                actual_output_sha = sha256_file(output)
                backup_kind = "verified_previous_output" if output_is_current else "interrupted_partial_output"
                backup = backup_dir / f"{selected_variant}_{actual_output_sha[:12]}_{backup_kind}.mp4"
                if not backup.is_file():
                    shutil.copy2(output, backup)
                backup_sha = sha256_file(backup)
                if backup_sha != actual_output_sha:
                    raise RuntimeError(f"authorized_binding_repair_backup_sha_mismatch:{selected_variant}")
            repair_authorization = {
                "repair_authorization": {
                    "reason": authorized_binding_repair_reason.strip(),
                    "authorized_at": datetime.now().isoformat(timespec="seconds"),
                    "previous_binding_fingerprint": str(previous.get("binding_fingerprint") or ""),
                    "previous_output_sha256": recorded_sha,
                    "previous_status": previous_status,
                    "backup_kind": backup_kind,
                    "backup": str(backup) if backup is not None else "",
                    "backup_sha256": backup_sha,
                }
            }
        started_at = datetime.now().isoformat(timespec="seconds")
        save_json(guard, {
            "schema_version": "story-release-encode-guard/v1",
            "variant": selected_variant,
            "status": (
                "running_authorized_binding_repair"
                if authorized_binding_repair
                else "running_technical_repair" if technical_repair else "running"
            ),
            "attempt_count": 1,
            "technical_repair_count": technical_repair_count,
            "max_technical_repairs": 1,
            "max_full_resolution_encodes": 1,
            "binding_fingerprint": fingerprint,
            "input_artifact_hashes": file_bindings,
            "output": str(output),
            "started_at": started_at,
            **repair_authorization,
        })
        try:
            run_script("release_video.py", *command)
        except BaseException as exc:
            save_json(guard, {
                "schema_version": "story-release-encode-guard/v1",
                "variant": selected_variant,
                "status": "failed_or_interrupted",
                "attempt_count": 1,
                "technical_repair_count": technical_repair_count,
                "max_technical_repairs": 1,
                "max_full_resolution_encodes": 1,
                "binding_fingerprint": fingerprint,
                "input_artifact_hashes": file_bindings,
                "output": str(output),
                "started_at": started_at,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "error": f"{type(exc).__name__}: {exc}",
                **repair_authorization,
            })
            raise
        if not output.is_file() or output.stat().st_size <= 0:
            raise RuntimeError(f"正式编码返回但未产生有效输出：{output}")
        save_json(guard, {
            "schema_version": "story-release-encode-guard/v1",
            "variant": selected_variant,
            "status": "completed",
            "attempt_count": 1,
            "technical_repair_count": technical_repair_count,
            "max_technical_repairs": 1,
            "max_full_resolution_encodes": 1,
            "binding_fingerprint": fingerprint,
            "input_artifact_hashes": file_bindings,
            "output": str(output),
            "output_sha256": sha256_file(output),
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            **repair_authorization,
        })

    if requested_variant in {"both", "main"}:
        assert main_bg_video is not None
        run_release_variant("main", main_bg_video, main_plate)
    if requested_variant in {"both", "library"}:
        assert library_bg_video is not None
        run_release_variant("library", library_bg_video, library_plate)
    if is_preview:
        sheet = render_release_preview_contact_sheet(preview_dir, run_id)
        handoff = write_release_preview_index(preview_dir, sheet, run_id)
        feedback_handoff = write_release_preview_feedback_handoff(preview_dir, sheet, run_id)
        review_bundle = write_release_preview_review_bundle(
            preview_dir,
            paths.status / "reviews" / "release_preview_bundle.json",
        )
        print("", flush=True)
        print(f"本轮预览目录：{preview_dir}", flush=True)
        print(f"总览拼图：{sheet}", flush=True)
        print(f"预览索引：{handoff}", flush=True)
        print(f"反馈给 Codex：{feedback_handoff}", flush=True)
        print(f"独立审核哈希包：{review_bundle}", flush=True)
        for path in important_preview_images(preview_dir):
            print(f"重点预览：{path}", flush=True)
        return
    manifest = refresh_project_outputs(paths.root)
    write_manifest(paths, manifest)
    qa_release(paths.root)


def reset_release_preview_dir(preview_dir: Path) -> None:
    if preview_dir.exists():
        shutil.rmtree(preview_dir)
    preview_dir.mkdir(parents=True, exist_ok=True)


def write_release_preview_review_bundle(preview_dir: Path, output: Path) -> Path:
    """Bind every current preview artifact before independent review.

    Formal Release already fails closed when this bundle or its review is
    absent or stale. Creating the bundle during preview generation closes the
    former handoff gap where operators had to assemble it manually.
    """

    from story_evidence import review_bundle_is_current, write_review_bundle

    artifacts = sorted(path for path in preview_dir.rglob("*") if path.is_file())
    if not artifacts:
        raise RuntimeError("发布预览为空，无法生成独立审核哈希包")
    write_review_bundle(output, artifacts)
    if not review_bundle_is_current(output):
        raise RuntimeError("发布预览独立审核哈希包生成后未通过当前性校验")
    return output


def important_preview_images(preview_dir: Path) -> list[Path]:
    names = [
        "preview_contact_sheet.png",
        "main_002s_a_native_anchor.png",
        "main_002s_a_native_right.png",
        "main_037s_b.png",
        "library_002s.png",
        "library_037s.png",
    ]
    paths = [preview_dir / name for name in names if (preview_dir / name).exists()]
    if paths:
        return paths
    return sorted(path for path in preview_dir.glob("*.png") if path.name != "preview_contact_sheet.png")[:8]


def render_release_preview_contact_sheet(preview_dir: Path, run_id: str) -> Path:
    images = sorted(path for path in preview_dir.glob("*.png") if path.name != "preview_contact_sheet.png")
    if not images:
        raise FileNotFoundError(f"预览目录没有 PNG：{preview_dir}")
    thumb_w, thumb_h = 270, 360
    cols = 3
    rows = (len(images) + cols - 1) // cols
    title_h = 54
    label_h = 34
    padding = 18
    sheet = Image.new("RGB", (cols * thumb_w + (cols + 1) * padding, title_h + rows * (thumb_h + label_h + padding) + padding), "white")
    draw = ImageDraw.Draw(sheet)
    font = load_contact_font(20)
    small_font = load_contact_font(15)
    draw.text((padding, 14), f"发布视频预览总览 {run_id}", fill=(25, 25, 25), font=font)
    for index, path in enumerate(images):
        row = index // cols
        col = index % cols
        x = padding + col * (thumb_w + padding)
        y = title_h + row * (thumb_h + label_h + padding)
        with Image.open(path).convert("RGB") as image:
            image.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
            tile = Image.new("RGB", (thumb_w, thumb_h), (244, 244, 244))
            tile.paste(image, ((thumb_w - image.width) // 2, (thumb_h - image.height) // 2))
        sheet.paste(tile, (x, y))
        draw.text((x, y + thumb_h + 8), path.name, fill=(30, 30, 30), font=small_font)
    output = preview_dir / "preview_contact_sheet.png"
    sheet.save(output)
    return output


def load_contact_font(size: int):
    for candidate in (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ):
        path = Path(candidate)
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except Exception:
                pass
    return ImageFont.load_default()


def write_release_preview_index(preview_dir: Path, sheet: Path, run_id: str) -> Path:
    images = sorted(path for path in preview_dir.glob("*.png"))
    lines = [
        "# 发布视频预览索引",
        "",
        f"- 本轮时间：{run_id}",
        f"- 总览拼图：{sheet}",
        f"- 预览目录：{preview_dir}",
        "",
        "## 图片列表",
    ]
    lines.extend(f"- {path.name}" for path in images)
    output = preview_dir / "preview_index.md"
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


def write_release_preview_feedback_handoff(preview_dir: Path, sheet: Path, run_id: str) -> Path:
    images = sorted(path for path in preview_dir.glob("*.png") if path.name != "preview_contact_sheet.png")
    status_dir = preview_dir.parent
    project_dir = status_dir.parent
    preset_path = project_dir / "04_发布视频" / "release_layout.json"
    index_path = preview_dir / "preview_index.md"
    lines = [
        "# 第 13 步预览反馈给 Codex",
        "",
        "请不要只总结图片。请直接查看本轮发布视频预览，判断主账号和宝库号是否适合进入正式生成；如果主账号人物位置、大小或留白需要调整，请修改 release_layout.json并重新预览。抠像边缘问题必须回到 RVM/keying 审核链，不得用裁切或布局移位遮盖。",
        "",
        "## 本轮文件",
        f"- 项目目录：`{project_dir}`",
        f"- 总览拼图：`{sheet}`",
        f"- 预览索引：`{index_path}`",
        f"- 参数写回：`{preset_path}`",
        f"- 本轮时间：`{run_id}`",
        "",
        "## 重点检查",
        "- 主账号：人物大小是否够清楚，位置是否自然，右侧/左侧是否空得难看。",
        "- 主账号：A/B 镜故事框是否挡字幕，故事画面是否被框裁得别扭。",
        "- 主账号：绿幕抠像边缘、手部、头发、裙摆是否干净；开头动作帧也要看。",
        "- 宝库号：中间故事视频是否填满安全区，标题和底部资料文字是否完整清楚。",
        "- 两个账号：Logo、水印、字幕和底部资料区不要互相打架。",
        "",
        "## 用户反馈区",
        "我对当前预览的直观反馈是：",
        "",
        "- ",
        "",
        "## 单张预览",
    ]
    lines.extend(f"- `{path}`" for path in images)
    output = preview_dir / "release_preview_feedback_to_codex.md"
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


def run_release_layout_handoff(project_dir: Path, times: str, person_layouts: str) -> None:
    run_package_release_project(project_dir, "both", preview_times=times, preview_person_layouts=person_layouts)
    paths = project_paths(project_dir)
    preview_dir = paths.status / "release_preview_frames"
    keying_preset_path = paths.release / "keying" / "keying_preset.json"
    preset_path = paths.release / "release_layout.json"
    preset = {}
    if preset_path.exists():
        preset = json.loads(preset_path.read_text(encoding="utf-8"))
    handoff = paths.status / "release_layout_codex_handoff.md"
    lines = [
        "# 第 13 步 Codex 智能定参交接",
        "",
        "请 Codex 读取本轮候选预览图，判断主持人人像大小、位置、故事框遮挡、字幕、Logo、背景虚化是否协调。",
        "",
        f"- 项目目录：{paths.root}",
        f"- 候选预览目录：{preview_dir}",
        f"- 总览拼图：{preview_dir / 'preview_contact_sheet.png'}",
        f"- 参数写回：{preset_path}",
        f"- 抠像绑定：{keying_preset_path}",
        "",
        "## 当前 release_layout.json",
        "```json",
        json.dumps(preset, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 自动候选布局",
        "- native_left/native_anchor/native_right：全部保持 1920×1080 抠像层 1:1，仅比较水平锚点",
        "- detected_person_bbox 只作测量，禁止用作裁切框",
        "- A 镜使用开场中性帧确定唯一固定 X；后续大手势保持人物层位置不变",
        "",
        "## Codex 应执行",
        "1. 先看 preview_contact_sheet.png，再逐张查看候选 A 镜、B 镜和宝库号预览。",
        "2. 以开头中性帧人物中轴对齐右侧空白矩形中心，只计算一次固定 person_x。",
        "3. 在 release_layout.json 中使用 person_height=1080、person_crop=null、person_y=0 和一个固定 person_x；整段沿用同一位置，并绑定当前 keying_preset SHA-256。",
        "4. 再运行 preview-release-project 确认最终预览，确认后才点击工作台 ⑭ 正式编码。",
    ]
    handoff.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已生成 Codex 定参交接：{handoff}", flush=True)


def run_publish_package_project(
    project_dir: Path,
    generate_covers: bool,
    main_frame: int | None,
    library_frame: int | None,
    *,
    main_person_frame: int | None = None,
    main_story_frame: int | None = None,
) -> None:
    paths = project_paths(project_dir)
    require_native_run_inputs(paths.status)
    manifest = refresh_project_outputs(paths.root)
    story = manifest["story"]
    outputs = manifest["outputs"]
    inputs = manifest["inputs"]
    # New native runs bind the immutable confirmed TXT/MD in story_run.json.
    # Prefer that hash-checked source over a customer DOCX discovered at the
    # project root; publish_package.py intentionally consumes plain text.
    story_text = confirmed_text_from_story_run(paths.status) or first_existing(inputs.get("story_text"))
    main_video = first_existing(outputs.get("main_release_video"), paths.release / "主账号发布视频.mp4")
    library_video = first_existing(outputs.get("library_release_video"), paths.release / "宝库号发布视频.mp4")
    if story_text is None:
        raise FileNotFoundError("缺少故事正文，请先放入桌面项目或在工作台保存故事文本。")
    if main_video is None or library_video is None:
        raise FileNotFoundError("缺少主账号/宝库号发布视频，请先运行 package-release-project。")
    person_reference = first_existing(outputs.get("person_reference"), paths.release / "person_reference.jpg")
    greenscreen_video = first_existing(inputs.get("greenscreen_video"))
    background_video = first_existing(outputs.get("background_video_no_sub"), outputs.get("demo_voice_bgm"), outputs.get("background_video_with_sub"))
    main_cover_reference = first_existing(
        paths.status / "release_preview_frames" / "main_002s_a.png",
        paths.status / "release_preview_frames" / "main_001s_a.png",
    )
    command = [
        "--story-name",
        story.get("name", ""),
        "--episode",
        str(story.get("episode", "")),
        "--duration-text",
        story.get("duration_text") or "待定",
        "--story-text",
        story_text,
        "--main-video",
        main_video,
        "--library-video",
        library_video,
        "--output-dir",
        paths.publish,
        "--story-type",
        story.get("story_type", "童话故事"),
        "--age-range",
        story.get("age_range", ""),
    ]
    if person_reference is not None:
        command.extend(["--person-reference", person_reference])
    if greenscreen_video is not None:
        command.extend(["--greenscreen-video", greenscreen_video])
    if background_video is not None:
        command.extend(["--background-video", background_video])
    if main_cover_reference is not None:
        command.extend(["--main-cover-reference", main_cover_reference])
    if main_frame is not None:
        command.extend(["--main-frame", str(main_frame)])
    if main_person_frame is not None:
        command.extend(["--main-person-frame", str(main_person_frame)])
    if main_story_frame is not None:
        command.extend(["--main-story-frame", str(main_story_frame)])
    if library_frame is not None:
        command.extend(["--library-frame", str(library_frame)])
    if generate_covers:
        command.append("--generate-covers")
    run_script("publish_package.py", *command)
    manifest = refresh_project_outputs(paths.root)
    manifest["outputs"]["publish_package"] = str(paths.publish)
    write_manifest(paths, manifest)
    qa_publish(paths.root)


def run_publish_package_draft_project(project_dir: Path) -> None:
    from publish_package import choose_default_library_cover_reference, extract_candidate_frames, render_contact_sheet

    paths = project_paths(project_dir)
    manifest = refresh_project_outputs(paths.root)
    story = manifest["story"]
    inputs = manifest["inputs"]
    outputs = manifest["outputs"]
    publish_dir = paths.publish
    main_covers = publish_dir / "main" / "covers"
    library_covers = publish_dir / "library" / "covers"
    main_covers.mkdir(parents=True, exist_ok=True)
    library_covers.mkdir(parents=True, exist_ok=True)

    for relative in (
        "copy_codex_request.md",
        "main/copy.md",
        "library/copy.md",
        "main/covers/cover_16x9.png",
        "library/covers/cover_16x9.png",
    ):
        path = publish_dir / relative
        if path.exists() and path.is_file():
            path.unlink()

    story_name = story.get("name") or paths.root.name.replace("故事剪辑：", "")
    duration_text = story.get("duration_text") or "待定"
    story_type = story.get("story_type") or "童话故事"
    age_range = story.get("age_range") or "6-8岁"
    story_text = first_existing(inputs.get("story_text"), paths.inputs / "story_source.txt")
    main_video = first_existing(outputs.get("main_release_video"), paths.release / f"主账号发布：{story_name}.mp4", paths.release / "主账号发布视频.mp4")
    library_video = first_existing(outputs.get("library_release_video"), paths.release / f"宝库号发布：{story_name}.mp4", paths.release / "宝库号发布视频.mp4")
    person_reference = first_existing(outputs.get("person_reference"), paths.release / "person_reference.jpg")
    greenscreen_video = first_existing(inputs.get("greenscreen_video"))
    background_video = first_existing(outputs.get("background_video_no_sub"), outputs.get("background_video_with_sub"), outputs.get("demo_voice_bgm"))
    story_images = sorted((paths.images / "images").glob("story_scene_*.png"))

    publish_frame_dir = publish_dir / "frame_candidates"
    main_candidate_sheet = publish_frame_dir / "main_候选帧索引.jpg"
    library_candidate_sheet = publish_frame_dir / "library_候选帧索引.jpg"
    if main_video is not None:
        extract_candidate_frames(main_video, publish_frame_dir / "main", 8)
        render_contact_sheet(publish_frame_dir / "main", main_candidate_sheet, "主账号发布视频")
    if library_video is not None:
        extract_candidate_frames(library_video, publish_frame_dir / "library", 8)
        render_contact_sheet(publish_frame_dir / "library", library_candidate_sheet, "宝库号发布视频")

    main_story_candidates = main_covers / "story_frame_candidates"
    main_story_sheet = main_covers / "story候选帧索引.jpg"
    if background_video is not None:
        extract_candidate_frames(background_video, main_story_candidates, 8)
        render_contact_sheet(main_story_candidates, main_story_sheet, "故事横屏镜头")
    elif story_images:
        main_story_candidates.mkdir(parents=True, exist_ok=True)
        for index, image in enumerate(story_images[:8], start=1):
            with Image.open(image).convert("RGB") as source:
                source.save(main_story_candidates / f"candidate_{index:02d}.jpg", quality=94)
        render_contact_sheet(main_story_candidates, main_story_sheet, "故事横屏镜头")

    person_sheet = main_covers / "person候选帧索引.jpg"
    if greenscreen_video is not None:
        person_candidates = main_covers / "person_candidates"
        extract_candidate_frames(greenscreen_video, person_candidates, 8)
        render_contact_sheet(person_candidates, person_sheet, "主账号真人动作")
    elif person_reference is not None:
        person_candidates = main_covers / "person_candidates"
        person_candidates.mkdir(parents=True, exist_ok=True)
        with Image.open(person_reference).convert("RGB") as source:
            source.save(person_candidates / "candidate_01.jpg", quality=94)
        render_contact_sheet(person_candidates, person_sheet, "主账号真人动作")

    main_story_reference = first_existing(main_covers / "reference_3x4.png", main_covers / "selected_story_reference.png")
    if main_story_reference is None and main_story_candidates.exists():
        preferred_story = choose_default_library_cover_reference(main_story_candidates, 8)
        shutil.copyfile(preferred_story, main_covers / "selected_story_reference.png")
        shutil.copyfile(preferred_story, main_covers / "reference_3x4.png")
        main_story_reference = main_covers / "reference_3x4.png"
    if person_reference is not None:
        shutil.copyfile(person_reference, main_covers / "selected_person_reference.png")

    library_reference = first_existing(library_covers / "reference_3x4.png")
    if library_reference is None and (publish_frame_dir / "library").exists():
        preferred_library = choose_default_library_cover_reference(publish_frame_dir / "library", 8)
        shutil.copyfile(preferred_library, library_covers / "reference_3x4.png")
        library_reference = library_covers / "reference_3x4.png"
        (library_covers / "reference_choice.md").write_text(
            f"# 宝库号封面参考帧\n\n- 参考帧：`{preferred_library}`\n- 选择原则：从宝库号发布视频候选帧中优先选故事正文的动作/冲突/角色互动帧，而不是标题页、底板页或纯资料展示页。\n",
            encoding="utf-8",
        )

    main_prompt = f"""# 主账号 4:3 封面提示

## 生成方式硬性要求
- 必须使用 Codex 原生图像生成能力生成最终 `cover_4x3.png`。
- 禁止用 Pillow、HTML/CSS、截图拼接、模板叠字或本地脚本合成最终封面。
- 不生成发布文案，不生成 `cover_16x9.png`，不生成 `cover_3x4.png`。
- 生成后只允许做尺寸标准化、文件复制和轻微压缩。

## 参考素材
- 故事横屏候选索引：`{main_story_sheet if main_story_sheet.exists() else '未生成'}`
- 选中的故事横屏参考帧：`{main_story_reference or '未找到；请从故事横屏候选索引中选一帧能代表故事的 16:9 镜头'}`
- 人物候选索引：`{person_sheet if person_sheet.exists() else '未生成'}`
- 选中的人物参考帧：`{person_reference or '未找到'}`
- 主账号发布视频候选索引：`{main_candidate_sheet if main_candidate_sheet.exists() else '未生成'}`

## 4:3 输出
- 保存路径：`{main_covers / 'cover_4x3.png'}`
- 标题：《{story_name}》
- 故事类型：{story_type}
- 时长：{duration_text}
- 适合年龄：{age_range}
- 主账号封面偏故事展示和表演感，真人一致性优先。
- 主持人应尽量保持参考帧的脸型、五官比例、发型、服装和麦克风。
- 画面以选中的 16:9 故事镜头为核心参考，继承故事角色、动作、场景光线和情绪；在此基础上加入真人主持人。
- 标题、故事类型、时长、适合年龄和资料包信息只是文字信息，不引用任何底板图、背景图、木质故事框或发布模板。
- 禁止出现木质大相框、内嵌小画面、白色信息卡、分栏排版、六宫格资源卡、截图边框或模板拼贴感。
"""
    library_prompt = f"""# 宝库号 4:3 封面提示

## 生成方式硬性要求
- 必须使用 Codex 原生图像生成能力生成最终 `cover_4x3.png`。
- 禁止用 Pillow、HTML/CSS、截图拼接、模板叠字或本地脚本合成最终封面。
- 不生成发布文案，不生成 `cover_16x9.png`，不生成 `cover_3x4.png`。
- 生成后只允许做尺寸标准化、文件复制和轻微压缩。

## 参考素材
- 宝库号发布视频候选索引：`{library_candidate_sheet if library_candidate_sheet.exists() else '未生成'}`
- 选中的宝库号横屏参考帧：`{library_reference or '未找到；请从宝库号发布视频候选索引中选一帧能代表故事的 16:9 镜头'}`
- 故事横屏候选索引：`{main_story_sheet if main_story_sheet.exists() else '未生成'}`

## 4:3 输出
- 保存路径：`{library_covers / 'cover_4x3.png'}`
- 标题：《{story_name}》
- 故事类型：{story_type}
- 时长：{duration_text}
- 适合年龄：{age_range}
- 宝库号封面偏资源陈列和购买决策，不出现真人。
- 清楚表达资源内容：背景视频、PPT、配乐、文稿、朗读标注、示范视频。
- 画面以选中的宝库号 16:9 横屏参考帧为核心参考，继承故事角色、动作、场景光线和情绪。
- 标题、故事类型、时长、适合年龄和 6 项资源内容只是文字信息，不引用宝库号底板图、发布底图、故事框或其他包装模板。
- 资源信息可以集中在底部横条或少量图标中，但不要做六宫格资源卡矩阵。
- 禁止木质大相框、内嵌小画面、白色卡片、播放器框、PPT框和模板拼贴感。
"""
    (main_covers / "cover_derivative_prompts.md").write_text(main_prompt, encoding="utf-8")
    (library_covers / "cover_derivative_prompts.md").write_text(library_prompt, encoding="utf-8")

    handoff_lines = [
        f"# 发布物料草稿任务书：{story_name}",
        "",
        "这是半自动版提前生成的精简发布物料任务书。它不要求主账号发布视频和宝库号发布视频已经完成；有最终视频后可重新抽帧修正参考。",
        "",
        "## 已知项目",
        f"- 项目目录：`{paths.root}`",
        f"- 故事原文：`{story_text or '未找到'}`",
        f"- 主账号发布视频：`{main_video or '未找到'}`",
        f"- 宝库号发布视频：`{library_video or '未找到'}`",
        f"- 背景故事成片：`{background_video or '未找到'}`",
        f"- 人物参考帧：`{person_reference or '未找到'}`",
        f"- 故事横屏候选索引：`{main_story_sheet if main_story_sheet.exists() else '未生成'}`",
        f"- 宝库号候选索引：`{library_candidate_sheet if library_candidate_sheet.exists() else '未生成'}`",
        f"- 主账号故事参考帧：`{main_story_reference or '未找到'}`",
        f"- 宝库号参考帧：`{library_reference or '未找到'}`",
        "",
        "## 交给 Codex 的任务",
        "1. 不生成发布文案，不创建或覆盖 `main/copy.md`、`library/copy.md`。",
        "2. 主账号只生成一张 4:3 封面，保存到 `main/covers/cover_4x3.png`。",
        "3. 宝库号只生成一张 4:3 封面，保存到 `library/covers/cover_4x3.png`。",
        "4. 3:4 封面不单独做，发布站上传视频后从视频中选帧。",
        "5. 16:9 封面不生成。",
        "6. 两张 4:3 封面必须使用 Codex 原生生图能力生成；禁止用 Pillow、HTML/CSS、截图拼接、模板叠字或本地脚本合成最终封面。",
        "7. 参考图只使用故事横屏镜头、发布视频候选帧和人物参考帧；不要引用 `theme_assets` 里的背景底图、发布底板或故事框。",
        "",
        "## 目标路径",
        f"- 主账号 4:3：`{main_covers / 'cover_4x3.png'}`",
        f"- 宝库号 4:3：`{library_covers / 'cover_4x3.png'}`",
        "",
        "## 参考提示",
        f"- 主账号 4:3 封面提示：`{main_covers / 'cover_derivative_prompts.md'}`",
        f"- 宝库号 4:3 封面提示：`{library_covers / 'cover_derivative_prompts.md'}`",
    ]
    (publish_dir / "publish_package_codex_handoff.md").write_text("\n".join(handoff_lines) + "\n", encoding="utf-8")

    manifest = refresh_project_outputs(paths.root)
    manifest["outputs"]["publish_package"] = str(paths.publish)
    write_manifest(paths, manifest)
    print(f"已生成精简发布物料草稿任务书：{publish_dir / 'publish_package_codex_handoff.md'}")


def run_product_package_project(
    project_dir: Path,
    *,
    allow_draft_annotation: bool,
    preview_only: bool,
    annotation_docx: Path | None,
    annotation_json: Path | None,
    demo_person_crop_mode: str,
    demo_person_vertical_align: str,
    demo_person_crop_bottom_ratio: float,
    preview_times: str,
    story_contract_context: Path | None = None,
) -> None:
    paths = project_paths(project_dir)
    require_native_run_inputs(paths.status)
    manifest = detect_project_assets(paths.root, extract_audio=True)
    manifest = refresh_project_outputs(paths.root)
    existing_keying = first_existing(manifest["outputs"].get("keying_preset"), paths.release / "keying" / "keying_preset.json")
    if existing_keying is None:
        try:
            auto_keying(paths.root, backend=configured_keying_backend())
        except Exception as exc:
            print(f"[warning] 自动抠像参数暂不可用：{exc}")
        manifest = refresh_project_outputs(paths.root)
    story = manifest["story"]
    inputs = manifest["inputs"]
    outputs = manifest["outputs"]
    story_text = first_existing(inputs.get("story_text"))
    confirmed_story_text = confirmed_text_from_story_run(paths.status)
    # The project manifest's generic story_text may be the punctuation-free
    # subtitle TXT.  Prefer the hash-bound confirmed manuscript for the
    # customer Word document so neither timing rows nor an old generated
    # consumer manuscript can become the source of a corrected package.
    story_document_text = customer_manuscript_source(
        confirmed_story_text,
        outputs.get("consumer_manuscript"),
        story_text,
    )
    script_lines = first_existing(confirmed_story_text, inputs.get("story_text"), paths.inputs / "story_source.txt")
    narration = product_demo_audio_source(
        confirmed_audio_from_story_run(paths.status),
        inputs.get("narration"),
        inputs.get("extracted_narration"),
    )
    music = first_existing(
        story_run_artifact_path(paths.status, "final_background_music"),
        paths.video_jobs / "music" / f"{story.get('slug')}_background_music.mp3",
        inputs.get("music"),
    )
    sealed_images_dir = sealed_storyboard_images_dir(paths.status)
    images_dir = first_existing(
        sealed_images_dir,
        paths.root / "02_图片素材" / "shot_storyboards",
        paths.images / "shot_storyboards",
        paths.video_jobs / "images",
        paths.images / "images",
    )
    # Customer packages use the body-only sales subtitle projection.  The
    # generic background_video_with_sub may also contain opening/closing
    # presenter text and is therefore only a fallback.
    bg_with_sub = first_existing(paths.assembly / "story_sales_subs_bgm.mp4", outputs.get("background_video_with_sub"))
    bg_no_sub = first_existing(outputs.get("background_video_no_sub"), paths.assembly / "story_no_subs_bgm.mp4")
    greenscreen = first_existing(inputs.get("greenscreen_video"))
    keying_preset = first_existing(outputs.get("keying_preset"), paths.release / "keying" / "keying_preset.json")
    demo_bg = first_existing(outputs.get("main_background_image"), paths.release / "theme_assets" / "main_background_16x9.png")
    story_frame_a = first_existing(outputs.get("story_frame_a"), paths.release / "theme_assets" / "story_frame_a.png")
    timings = first_existing(
        outputs.get("timings_json"),
        paths.assembly / "timings.json",
        paths.status / "preflight" / "confirmed_line_timings.json",
    )
    confirmed_spoken_srt = ensure_confirmed_spoken_timeline_srt(paths.status, paths.assembly)
    body_subtitles_srt = first_existing(
        product_body_subtitles_from_customer_media_receipt(paths.status),
        outputs.get("sales_subtitles_srt"),
        paths.assembly / "story_sales_subtitles.srt",
        paths.assembly / "story_subtitles.srt",
    )
    full_subtitles_srt = first_existing(
        paths.status / "release_semantics" / "main_demo_subtitles.srt",
        paths.assembly / "story_full_subtitles.srt",
        paths.assembly / "subtitles" / "全片字幕_62行.srt",
    )
    source_subtitles = first_existing(
        paths.assembly / "story_semantic_timeline.srt",
        confirmed_spoken_srt,
        paths.assembly / "story_subtitles.srt",
    )
    authoritative_timeline_receipt = story_run_artifact_path(
        paths.status,
        "authoritative_timeline_receipt",
    )
    # The customer manuscript keeps natural reading paragraphs, while PPTs
    # must stay one-to-one with the generated storyboard images.  Prepared
    # projects therefore use the audited storyboard text for presentation
    # captions/timings without changing the document or annotation source.
    storyboard_text = first_existing(
        inputs.get("storyboard_text"),
        confirmed_story_text,
        paths.inputs / f"{story.get('slug')}_storyboard_text.txt",
        story_text,
    )
    if storyboard_text is not None and source_subtitles is not None:
        product_script, product_timings = build_product_text_sources_from_story_source(
            story_text=storyboard_text,
            subtitles_srt=source_subtitles,
            output_dir=paths.status / "product_package_work",
        )
        script_lines = product_script
        timings = product_timings
    config = load_config()
    brand_assets = config.get("brand_assets", {})
    demo_logo = first_existing(brand_assets.get("story_logo"), brand_assets.get("logo"))
    product_defaults = config.get("product_defaults", {})
    if story_contract_context is None and not bool(product_defaults.get("include_demo_logo", False)):
        demo_logo = None
    release_defaults = config.get("release_defaults", {})
    missing = []
    for label, value in (
        ("故事正文", story_document_text),
        ("逐行台词", script_lines),
        ("旁白", narration),
        ("配乐", music),
        ("图片目录", images_dir if images_dir is not None and images_dir.exists() else None),
        ("含字幕背景视频", bg_with_sub),
        ("无字幕背景视频", bg_no_sub),
        ("绿幕视频", greenscreen),
        ("抠像参数", keying_preset),
        ("主账号 A 镜背景图", demo_bg),
        ("主账号 A 镜故事框", story_frame_a),
        ("timings.json", timings),
        ("权威时间轴回执", authoritative_timeline_receipt),
    ):
        if value is None:
            missing.append(label)
    if missing:
        raise FileNotFoundError("资料包缺少必要输入：" + "、".join(missing))
    command = [
        "--story-name",
        story.get("name", ""),
        "--slug",
        story.get("slug", ""),
        "--story-text",
        story_document_text,
        "--script-lines",
        script_lines,
        "--narration",
        narration,
        "--music",
        music,
        "--images-dir",
        images_dir,
        "--bg-video-with-sub",
        bg_with_sub,
        "--bg-video-no-sub",
        bg_no_sub,
        "--person-greenscreen",
        greenscreen,
        "--demo-background-image",
        demo_bg,
        "--story-frame-a-image",
        story_frame_a,
        "--keying-preset-json",
        keying_preset,
        "--timings-json",
        timings,
        "--output-root",
        paths.product,
        "--work-dir",
        paths.status / "product_package_work",
        "--authoritative-timeline-receipt",
        authoritative_timeline_receipt,
        "--demo-person-crop-bottom-ratio",
        str(demo_person_crop_bottom_ratio),
        "--demo-person-crop-mode",
        demo_person_crop_mode,
        "--demo-person-vertical-align",
        demo_person_vertical_align,
        "--demo-logo-width",
        str(release_defaults.get("story_logo_width_a", 150)),
        "--demo-logo-x",
        str(release_defaults.get("story_logo_x", 42)),
        "--demo-logo-y",
        str(release_defaults.get("story_logo_y", 44)),
    ]
    receipted_static_ppt_inputs = static_ppt_inputs_from_delivery_receipt(paths.status)
    static_ppt_inputs = {
        "--director-plan": first_existing(
            receipted_static_ppt_inputs.get("--director-plan"),
            story_run_artifact_path(paths.status, "master_director_plan"),
            paths.root / "01_导演计划" / "master_director_plan.json",
            paths.status / "director" / "story_r2v_plan_draft.json",
            paths.status / "director" / "story_r2v_plan.json",
        ),
        "--shot-storyboard-compile-receipt": first_existing(
            receipted_static_ppt_inputs.get("--shot-storyboard-compile-receipt"),
            story_run_artifact_path(paths.status, "shot_storyboard_compile_receipt"),
            paths.root / "01_导演计划" / "shot_storyboard_compile_receipt.json",
            paths.status / "storyboards" / "shot_storyboard_compile_receipt.json"
        ),
        "--static-ppt-plan": first_existing(
            receipted_static_ppt_inputs.get("--static-ppt-plan"),
            story_run_artifact_path(paths.status, "static_ppt_plan"),
            paths.root / "01_导演计划" / "static_ppt_plan.json",
            paths.status / "product" / "static_ppt_plan.json",
        ),
        "--static-ppt-with-subtitles": first_existing(
            receipted_static_ppt_inputs.get("--static-ppt-with-subtitles"),
            paths.product / "build" / f"{story.get('name')}_静态故事PPT_含字幕.pptx",
            paths.root / "05_产品资料" / "构建中" / f"{story.get('name')}_静态故事PPT_含字幕.pptx",
            paths.product / "构建中" / f"{story.get('name')}_静态故事PPT_含字幕.pptx"
        ),
        "--static-ppt-without-subtitles": first_existing(
            receipted_static_ppt_inputs.get("--static-ppt-without-subtitles"),
            paths.product / "build" / f"{story.get('name')}_静态故事PPT_无字幕.pptx",
            paths.root / "05_产品资料" / "构建中" / f"{story.get('name')}_静态故事PPT_无字幕.pptx",
            paths.product / "构建中" / f"{story.get('name')}_静态故事PPT_无字幕.pptx"
        ),
    }
    if all(static_ppt_inputs.values()):
        if body_subtitles_srt is None:
            raise FileNotFoundError("静态 PPT 完整时间轴校验缺少正文 SRT")
        validate_static_ppt_full_timeline(
            Path(static_ppt_inputs["--static-ppt-plan"]),
            body_subtitles_srt,
            probe_duration(bg_no_sub),
        )
        for option, value in static_ppt_inputs.items():
            command.extend([option, value])
        command.extend(["--background-subtitle-srt", body_subtitles_srt])
    elif (paths.status / "story_run.json").is_file() or any(static_ppt_inputs.values()):
        missing_static = [option for option, value in static_ppt_inputs.items() if value is None]
        raise FileNotFoundError(
            "静态 PPT 输入不完整，禁止回退旧目录或重拼页：" + "、".join(missing_static)
        )
    # Exact reviewed subtitle artifacts are authoritative for both native and
    # contract-backed projects.  Never make the safe full/body projections
    # conditional on an optional contract-context CLI argument.
    if full_subtitles_srt is not None:
        command.extend(["--demo-subtitle-srt", full_subtitles_srt])
    if story_contract_context is not None:
        command.extend(["--project-dir", paths.root])
        # A current static-PPT delivery receipt is the authoritative native
        # branch and product_package intentionally rejects mixing it with the
        # older artifact-semantic-plan PPT selector.  Only projects without a
        # complete sealed PPT set use that legacy selector.
        if not all(static_ppt_inputs.values()):
            load_current_artifact_semantic_plan(paths.root)
            command.extend([
                "--artifact-semantic-plan", semantic_plan_path(paths.root),
            ])
        release_context = paths.status / "contracts" / "consumers" / "release_video.json"
        demo_brand_spec_path = compile_demo_render_spec(
            release_context,
            paths.status / "contracts" / "consumers" / "demo.compiled.json",
            official_logo_path=demo_logo,
        )
        demo_brand_spec = json.loads(demo_brand_spec_path.read_text(encoding="utf-8"))
        logo_args = demo_logo_arguments(demo_brand_spec, 1920, 1080)
        command.extend(["--demo-brand-spec", demo_brand_spec_path])
        if logo_args["logo_path"] is not None:
            command.extend([
                "--demo-logo", logo_args["logo_path"],
                "--demo-logo-width", str(logo_args["logo_width"]),
                "--demo-logo-x", str(logo_args["logo_x"]),
                "--demo-logo-y", str(logo_args["logo_y"]),
            ])
    annotation_skill_path = (
        annotation_skill_path_from_config()
    )
    if demo_logo is not None and story_contract_context is None:
        command.extend(["--demo-logo", demo_logo])
    if preview_only:
        command.append("--preview-only")
        if preview_times:
            command.extend(["--preview-times", preview_times])
    if annotation_docx is not None:
        command.extend(["--annotation-docx", annotation_docx])
    if annotation_json is not None:
        command.extend(["--annotation-json", annotation_json])
    if annotation_skill_path is not None:
        command.extend(["--annotation-skill-path", annotation_skill_path])
    if allow_draft_annotation:
        command.append("--allow-draft-annotation")
    run_script("product_package.py", *command)
    manifest = bind_generated_product_outputs(paths.root, str(story.get("name") or ""))
    if not preview_only:
        qa_product(paths.root)


def bind_generated_product_outputs(project_dir: Path, story_name: str) -> dict[str, Any]:
    """Bind QA to the package directories produced by the current command.

    Generic discovery deliberately preserves existing in-project paths.  That
    is useful during passive scans, but it previously meant a successful
    formal package rebuild could still leave QA pointing at an older package
    tree.  The producer knows its exact output locations, so it must bind them
    explicitly before QA runs.
    """

    paths = project_paths(project_dir)
    base = paths.product / f"绵羊故事锦囊：{story_name}（基础版）"
    advanced = paths.product / f"绵羊故事锦囊：{story_name}（进阶版）"
    missing = [str(path) for path in (base, advanced) if not path.is_dir()]
    if missing:
        raise FileNotFoundError("正式资料包生成后缺少目标目录：" + "、".join(missing))
    manifest = refresh_project_outputs(paths.root)
    manifest["outputs"]["product_base"] = str(base)
    manifest["outputs"]["product_advanced"] = str(advanced)
    write_manifest(paths, manifest)
    return manifest


def require_native_run_inputs(status_dir: Path) -> None:
    """Fail closed before current producers can discover historical fallbacks."""
    from story_run import load_run
    from story_requirements import _current_binding
    run_file = status_dir / "story_run.json"
    if not run_file.is_file():
        raise ValueError("正式生产缺少 story_run.json；历史读取不自动转为生产")
    ledger = load_run(run_file)
    for role in ("confirmed_text", "subtitle_txt", "greenscreen_video", "audio"):
        record = ledger["inputs"].get(role)
        if not isinstance(record, dict):
            raise ValueError(f"正式生产缺少当前输入：{role}")
        _current_binding(record, f"当前运行输入 {role}")


def confirmed_text_from_story_run(status_dir: Path) -> Path | None:
    """Return the hash-bound confirmed text from the native story ledger."""

    run_file = status_dir / "story_run.json"
    try:
        payload = json.loads(run_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    record = payload.get("inputs", {}).get("confirmed_text", {})
    if not isinstance(record, dict):
        return None
    path = Path(str(record.get("path") or "")).expanduser()
    expected = str(record.get("sha256") or "").lower()
    if path.suffix.lower() not in {".txt", ".md", ".docx"} or not path.is_file() or len(expected) != 64:
        return None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path if digest == expected else None


def confirmed_audio_from_story_run(status_dir: Path) -> Path | None:
    """Return the hash-bound full-program audio from the native story ledger."""

    run_file = status_dir / "story_run.json"
    try:
        payload = json.loads(run_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    record = payload.get("inputs", {}).get("audio", {})
    if not isinstance(record, dict):
        return None
    path = Path(str(record.get("path") or "")).expanduser()
    expected = str(record.get("sha256") or "").lower()
    if path.suffix.lower() not in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}:
        return None
    if not path.is_file() or len(expected) != 64:
        return None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path if digest == expected else None


def confirmed_subtitle_from_story_run(status_dir: Path) -> Path | None:
    """Return the exact user TXT bound into the native ledger.

    A native ledger makes this input mandatory. Missing, replaced, or changed
    TXT files fail closed so downstream consumers cannot silently switch back
    to the story manuscript and create their own punctuation or line breaks.
    """

    run_file = status_dir / "story_run.json"
    if not run_file.is_file():
        return None
    try:
        payload = json.loads(run_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("story_run.json 无法解析，不能确认字幕 TXT") from exc
    record = payload.get("inputs", {}).get("subtitle_txt")
    if not isinstance(record, dict):
        raise ValueError("story_run.json 缺少 subtitle_txt；禁止系统另做字幕")
    path = Path(str(record.get("path") or "")).expanduser()
    expected = str(record.get("sha256") or "").lower()
    if path.suffix.lower() != ".txt" or not path.is_file() or len(expected) != 64:
        raise ValueError("账本中的字幕 TXT 不存在或记录无效")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise ValueError("字幕 TXT 已发生哈希漂移；必须重新确认后登记")
    return path


def story_run_artifact_path(status_dir: Path, artifact_id: str) -> Path | None:
    """Return one hash-bound artifact recorded by the native story ledger."""

    run_file = status_dir / "story_run.json"
    try:
        payload = json.loads(run_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    record = payload.get("artifacts", {}).get(artifact_id, {})
    if not isinstance(record, dict):
        return None
    path = Path(str(record.get("path") or "")).expanduser()
    expected = str(record.get("sha256") or "").lower()
    if not path.is_file() or len(expected) != 64:
        return None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path if digest == expected else None


def sealed_storyboard_images_dir(status_dir: Path) -> Path | None:
    """Return the common directory of the current sealed storyboard images."""

    manifest_path = first_existing(
        story_run_artifact_path(status_dir, "storyboard_manifest_sealed"),
        status_dir / "storyboards" / "storyboard_manifest_sealed.json",
    )
    if manifest_path is None:
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entries = payload.get("entries")
    if payload.get("status") != "sealed" or not isinstance(entries, list) or not entries:
        return None
    images: list[Path] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        path = Path(str(entry.get("image_path") or "")).expanduser()
        expected = str(entry.get("image_sha256") or "").lower()
        if not path.is_file() or len(expected) != 64:
            return None
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            return None
        images.append(path)
    parents = {path.parent.resolve() for path in images}
    return next(iter(parents)) if len(parents) == 1 else None


def build_product_text_sources_from_story_source(story_text: Path, subtitles_srt: Path, output_dir: Path) -> tuple[Path, Path]:
    story_lines = [
        line.strip()
        for line in read_trusted_story_text(story_text).splitlines()
        if line.strip()
    ]
    cues = parse_simple_srt(subtitles_srt)
    story_lines = _trim_story_source_opening_absent_from_subtitles(story_lines, cues)
    story_lines = _trim_story_source_closing_absent_from_subtitles(story_lines, cues)
    output_dir.mkdir(parents=True, exist_ok=True)
    patched_timings: list[dict] = []
    subtitle_start = _find_story_subtitle_start(cues, story_lines)
    subtitle_stream, stream_segments = _build_story_subtitle_stream(cues, subtitle_start)
    stream_offset = 0
    for index, line in enumerate(story_lines, start=1):
        line_key = normalize_story_text_for_alignment(line)
        if not line_key:
            start = end = 0.0
            source_cue_start = subtitle_start + 1
            source_cue_end = subtitle_start
        else:
            if not subtitle_stream.startswith(line_key, stream_offset):
                matched = subtitle_stream[stream_offset : stream_offset + 48]
                raise ValueError(
                    f"故事原文第 {index} 行无法与 story_subtitles.srt 顺序对齐：{line}\n"
                    f"已匹配到：{matched}\n"
                    "请先修正原文或字幕，资料包不再回退使用画面描述文本。"
                )
            line_start_offset = stream_offset
            line_end_offset = stream_offset + len(line_key) - 1
            start_segment = _stream_segment_at_offset(stream_segments, line_start_offset)
            end_segment = _stream_segment_at_offset(stream_segments, line_end_offset)
            if start_segment is None or end_segment is None:
                matched = subtitle_stream[stream_offset : stream_offset + 48]
                raise ValueError(
                    f"故事原文第 {index} 行无法与 story_subtitles.srt 顺序对齐：{line}\n"
                    f"已匹配到：{matched}\n"
                    "请先修正原文或字幕，资料包不再回退使用画面描述文本。"
                )
            start = start_segment[3]
            end = end_segment[4]
            source_cue_start = start_segment[2] + 1
            source_cue_end = end_segment[2] + 1
            stream_offset += len(line_key)
        duration = max(0.0, end - start)
        patched_timings.append(
            {
                "index": index,
                "line": line,
                "source_start": round(start, 3),
                "source_end": round(end, 3),
                "duration": round(duration, 3),
                "timeline_start": round(start, 3),
                "timeline_end": round(end, 3),
                "source_cue_start": source_cue_start,
                "source_cue_end": source_cue_end,
            }
        )
    script_path = output_dir / "product_script_lines_from_story_source.txt"
    timings_path = output_dir / "product_timings_from_story_source.json"
    script_path.write_text("\n".join(story_lines) + "\n", encoding="utf-8")
    timings_path.write_text(json.dumps(patched_timings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return script_path, timings_path


def validate_static_ppt_full_timeline(
    plan_path: Path,
    body_subtitles_srt: Path,
    full_duration: float,
    *,
    tolerance: float = 0.15,
) -> None:
    """Block a body-only SRT from replacing TITLE/MORAL PPT timing slots."""

    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    slides = payload.get("slides")
    if not isinstance(slides, list) or len(slides) < 3:
        raise ValueError("静态 PPT 计划缺少 TITLE/正文/MORAL 完整结构")
    if str(slides[0].get("shot_id")) != "TITLE" or str(slides[-1].get("shot_id")) != "MORAL":
        raise ValueError("静态 PPT 必须以 TITLE 开始并以 MORAL 结束")
    cues = parse_simple_srt(body_subtitles_srt)
    if not cues:
        raise ValueError("正文 SRT 为空，无法校验 PPT 首尾独立时段")
    title_duration = float(slides[0].get("duration_seconds") or 0)
    moral_duration = float(slides[-1].get("duration_seconds") or 0)
    total_duration = sum(float(slide.get("duration_seconds") or 0) for slide in slides)
    expected_title = float(cues[0][0])
    expected_moral = float(full_duration) - float(cues[-1][1])
    if expected_title <= 0 or expected_moral <= 0:
        raise ValueError("正文 SRT 未给 TITLE/MORAL 留出独立音频时段")
    if abs(title_duration - expected_title) > tolerance:
        raise ValueError(
            f"PPT TITLE 时长未覆盖主持人开场：actual={title_duration:.3f} expected={expected_title:.3f}"
        )
    if abs(moral_duration - expected_moral) > tolerance:
        raise ValueError(
            f"PPT MORAL 时长未覆盖结尾寓意：actual={moral_duration:.3f} expected={expected_moral:.3f}"
        )
    if abs(total_duration - float(full_duration)) > tolerance:
        raise ValueError(
            f"PPT 总时长未覆盖完整音频：actual={total_duration:.3f} expected={full_duration:.3f}"
        )


def _trim_story_source_opening_absent_from_subtitles(
    story_lines: list[str],
    cues: list[tuple[float, float, str]],
) -> list[str]:
    """Drop only a semantic opening prefix already handled by a title slot.

    The native ledger's confirmed text may include a spoken title while the
    assembled body subtitle SRT intentionally starts after the independent
    title interval.  Product PPT timing consumes the body SRT, so it must use
    the matching body suffix without treating the confirmed title as corrupt.
    """

    if not story_lines or not cues:
        return story_lines
    semantics = classify_story(story_lines)
    opening_kinds = {
        SemanticKind.TITLE,
        SemanticKind.HOST_INTRO,
        SemanticKind.STORY_ANNOUNCEMENT,
    }
    semantic_prefix_end = 0
    for line_index in range(len(story_lines)):
        if semantics.kind_at(line_index + 1) not in opening_kinds:
            break
        semantic_prefix_end = line_index + 1
    subtitle_stream = "".join(normalize_story_text_for_alignment(cue[2]) for cue in cues)
    matching_boundaries: list[int] = []
    for boundary in range(semantic_prefix_end + 1):
        # Only the first retained line is needed to prove the opening
        # boundary.  Requiring the *entire* remaining source to equal the SRT
        # made a legitimate omitted host intro impossible to trim whenever a
        # later sales-only/title/moral line differed.  The main alignment loop
        # remains strict and will still reject any missing middle body line.
        candidate = (
            normalize_story_text_for_alignment(story_lines[boundary])
            if boundary < len(story_lines)
            else ""
        )
        if candidate and subtitle_stream.startswith(candidate):
            matching_boundaries.append(boundary)
    return story_lines[max(matching_boundaries):] if matching_boundaries else story_lines


def _trim_story_source_closing_absent_from_subtitles(
    story_lines: list[str],
    cues: list[tuple[float, float, str]],
) -> list[str]:
    """Trim only an explicit audience-addressed moral after the body SRT.

    Sales subtitles intentionally stop before the independent moral card.  A
    missing middle body line must still fail; trimming is allowed only after
    the subtitle stream has been consumed exactly and the remaining suffix
    begins with the generic ``这个故事告诉我们`` moral framing (optionally
    preceded by ``小朋友们``).
    """

    subtitle_stream = "".join(normalize_story_text_for_alignment(cue[2]) for cue in cues)
    if not story_lines or not subtitle_stream:
        return story_lines
    offset = 0
    for index, line in enumerate(story_lines):
        key = normalize_story_text_for_alignment(line)
        if not key or not subtitle_stream.startswith(key, offset):
            return story_lines
        offset += len(key)
        if offset != len(subtitle_stream):
            continue
        suffix = [
            normalize_story_text_for_alignment(item)
            for item in story_lines[index + 1 :]
            if normalize_story_text_for_alignment(item)
        ]
        if suffix and suffix[0] in {"小朋友", "小朋友们"}:
            suffix = suffix[1:]
        if suffix and suffix[0].startswith("这个故事告诉我们"):
            return story_lines[: index + 1]
        return story_lines
    return story_lines


def _find_story_subtitle_start(
    cues: list[tuple[float, float, str]], story_lines: list[str]
) -> int:
    """Find the first subtitle cue that can contain the supplied story text.

    ``story_text`` is normally the semantic body/moral view while
    ``story_subtitles.srt`` can still contain the spoken title and presenter
    framing.  The shared semantic classifier is used only to bound the
    skippable prefix; after that boundary alignment is a strict contiguous
    match.  Trying every prefix boundary also preserves legacy callers whose
    story source already includes a title or opening line.
    """

    if not cues:
        return 0
    story_stream = "".join(normalize_story_text_for_alignment(line) for line in story_lines)
    semantics = classify_story([cue[2] for cue in cues])
    opening_kinds = {
        SemanticKind.TITLE,
        SemanticKind.HOST_INTRO,
        SemanticKind.STORY_ANNOUNCEMENT,
    }
    semantic_prefix_end = 0
    for cue_index in range(len(cues)):
        if semantics.kind_at(cue_index + 1) not in opening_kinds:
            break
        semantic_prefix_end = cue_index + 1

    matching_boundaries = []
    for boundary in range(semantic_prefix_end + 1):
        candidate_stream = "".join(
            normalize_story_text_for_alignment(cue[2]) for cue in cues[boundary:]
        )
        if candidate_stream.startswith(story_stream):
            matching_boundaries.append(boundary)
    if matching_boundaries:
        # Prefer the furthest semantic opening boundary.  This avoids treating
        # a spoken title that happens to equal the first body line as part of
        # the body, while boundary 0 still preserves direct/legacy alignment.
        return max(matching_boundaries)
    return semantic_prefix_end


def _build_story_subtitle_stream(
    cues: list[tuple[float, float, str]], start: int
) -> tuple[str, list[tuple[int, int, int, float, float]]]:
    """Return normalized subtitle text and cue spans for timing projection."""

    stream_parts: list[str] = []
    segments: list[tuple[int, int, int, float, float]] = []
    offset = 0
    for cue_index in range(start, len(cues)):
        cue_start, cue_end, cue_text = cues[cue_index]
        normalized = normalize_story_text_for_alignment(cue_text)
        if not normalized:
            continue
        stream_parts.append(normalized)
        next_offset = offset + len(normalized)
        segments.append((offset, next_offset, cue_index, cue_start, cue_end))
        offset = next_offset
    return "".join(stream_parts), segments


def _stream_segment_at_offset(
    segments: list[tuple[int, int, int, float, float]], offset: int
) -> tuple[int, int, int, float, float] | None:
    for segment in segments:
        if segment[0] <= offset < segment[1]:
            return segment
    return None


def normalize_story_text_for_alignment(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text).lower()


def parse_simple_srt(path: Path) -> list[tuple[float, float, str]]:
    text = path.read_text(encoding="utf-8-sig", errors="ignore")
    cues: list[tuple[float, float, str]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        raw_start, raw_end = lines[1].split("-->", 1)
        cue_text = "，".join(lines[2:]).strip()
        if cue_text:
            cues.append((parse_srt_timestamp(raw_start.strip()), parse_srt_timestamp(raw_end.strip()), cue_text))
    return cues


def ensure_confirmed_spoken_timeline_srt(status_dir: Path, assembly_dir: Path) -> Path | None:
    """Return a full spoken subtitle timeline, deriving it from audio anchors.

    The body-only customer background SRT is never accepted as a substitute.
    Native synthesis writes ``story_semantic_timeline.srt`` directly.  A
    preflight-aligned project may instead expose ``confirmed_line_timings``;
    this function deterministically serializes those already-reviewed anchors
    without running ASR or changing any confirmed text.
    """

    return ensure_authoritative_timeline_srt(status_dir, assembly_dir)


def compile_release_subtitle_srt(
    source: Path,
    plan: dict,
    artifact: str,
    target: Path,
    *,
    semantic_source: Path | None = None,
) -> Path:
    """Project reviewed source-line semantics onto an existing SRT timeline.

    Semantic plans are deliberately compiled against stable story-source lines,
    while display subtitles may split each source line into several short cues.
    The equal-count path remains exact and backwards compatible.  When cue
    counts differ, the current hash-bound semantic source is aligned to the
    normalized subtitle text and whole cues inherit the decision of the source
    line(s) they cover.
    """
    text = source.read_text(encoding="utf-8-sig", errors="strict")
    blocks = [block for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]
    lines: list[str] = []
    valid_blocks: list[str] = []
    for block in blocks:
        parts = [line.strip() for line in block.splitlines() if line.strip()]
        if len(parts) >= 3 and "-->" in parts[1]:
            valid_blocks.append(block.strip())
            lines.append("，".join(parts[2:]).strip())
    expected_count = int(plan["semantic_source"]["line_count"])
    if len(lines) == expected_count:
        indices = selected_line_indices(lines, plan, artifact)
    else:
        if semantic_source is None:
            raise ValueError(
                "semantic artifact source line count mismatch: "
                f"expected {expected_count}, got {len(lines)}; "
                "semantic_source is required to project split subtitle cues"
            )
        semantic_source = semantic_source.resolve()
        if not semantic_source.is_file():
            raise ValueError(f"semantic source missing: {semantic_source}")
        recorded_sha256 = str(plan["semantic_source"].get("sha256") or "")
        actual_sha256 = hashlib.sha256(semantic_source.read_bytes()).hexdigest()
        if recorded_sha256 and actual_sha256 != recorded_sha256:
            raise ValueError("semantic source SHA-256 does not match the current plan")
        semantic_lines = [
            line.strip()
            for line in semantic_source.read_text(
                encoding="utf-8-sig", errors="strict"
            ).splitlines()
            if line.strip()
        ]
        if len(semantic_lines) != expected_count:
            raise ValueError(
                "semantic source line count does not match the current plan: "
                f"expected {expected_count}, got {len(semantic_lines)}"
            )
        selected_source_indices = selected_line_indices(
            semantic_lines, plan, artifact
        )
        indices = project_semantic_lines_to_subtitle_cues(
            semantic_lines,
            lines,
            selected_source_indices,
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    selected = [valid_blocks[index] for index in indices]
    target.write_text("\n\n".join(selected) + ("\n" if selected else ""), encoding="utf-8")
    return target


def contract_release_brand_paths(
    spec: dict,
    story_logo: Path | None,
    watermark_logo: Path | None,
    antipiracy_logo: Path | None,
) -> tuple[Path | None, Path | None, Path | None]:
    """Keep only the reviewed official mark for each release placement.

    ``official_assets`` is the only allowlist for deterministic release
    branding.  The release renderer validates a configured story logo against
    that allowlist.  Main rendering consumes ``story_logo`` as its one fixed
    mark; library rendering consumes ``antipiracy_logo`` as its one moving
    mark.  They may intentionally be the same official PNG because the two
    renderers never place both roles in one output frame.
    """

    official_assets = [
        item for item in spec.get("official_assets", []) if isinstance(item, dict)
    ]
    reviewed_story_logo = story_logo if official_assets else None
    reviewed_antipiracy_logo = antipiracy_logo if official_assets else None
    return reviewed_story_logo, None, reviewed_antipiracy_logo


def project_semantic_lines_to_subtitle_cues(
    semantic_lines: Sequence[str],
    subtitle_lines: Sequence[str],
    selected_source_indices: Sequence[int],
) -> list[int]:
    """Map source-line selections onto shorter subtitle cues without guessing.

    A cue crossing a selected/unselected source boundary is rejected because
    keeping or dropping it would silently change the locked semantic policy.
    """

    normalized_source = [
        normalize_story_text_for_alignment(line) for line in semantic_lines
    ]
    normalized_cues = [
        normalize_story_text_for_alignment(line) for line in subtitle_lines
    ]
    if any(not line for line in normalized_source):
        raise ValueError("semantic source contains an empty normalized line")
    if any(not line for line in normalized_cues):
        raise ValueError("subtitle SRT contains an empty normalized cue")

    source_stream = "".join(normalized_source)
    cue_stream = "".join(normalized_cues)
    occurrences = [
        offset
        for offset in range(0, len(cue_stream) - len(source_stream) + 1)
        if cue_stream.startswith(source_stream, offset)
    ]
    if len(occurrences) != 1:
        raise ValueError(
            "semantic source cannot be uniquely aligned to subtitle cues: "
            f"matches={len(occurrences)}"
        )
    source_offset = occurrences[0]

    source_spans: list[tuple[int, int, int]] = []
    cursor = source_offset
    for source_index, text in enumerate(normalized_source):
        end = cursor + len(text)
        source_spans.append((cursor, end, source_index))
        cursor = end

    selected_set = set(int(index) for index in selected_source_indices)
    cue_indices: list[int] = []
    cue_spans: list[tuple[int, int]] = []
    cue_cursor = 0
    for cue_index, cue_text in enumerate(normalized_cues):
        cue_end = cue_cursor + len(cue_text)
        cue_spans.append((cue_cursor, cue_end))
        overlapping = {
            source_index
            for start, end, source_index in source_spans
            if cue_cursor < end and cue_end > start
        }
        if overlapping:
            selected_overlap = overlapping & selected_set
            if selected_overlap and selected_overlap != overlapping:
                raise ValueError(
                    "subtitle cue crosses a selected/unselected semantic line "
                    f"boundary: cue={cue_index + 1}"
                )
            if selected_overlap:
                cue_indices.append(cue_index)
        cue_cursor = cue_end

    covered = {
        source_index
        for cue_index in cue_indices
        for start, end, source_index in source_spans
        if cue_spans[cue_index][0] < end and cue_spans[cue_index][1] > start
    }
    if not selected_set.issubset(covered):
        missing = sorted(selected_set - covered)
        raise ValueError(f"selected semantic lines have no subtitle cues: {missing}")
    return cue_indices


def parse_srt_timestamp(value: str) -> float:
    match = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", value)
    if not match:
        raise ValueError(f"无法解析 SRT 时间：{value}")
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def reset_redo_scenes(jobs_csv: Path, videos_dir: Path, decisions_csv: Path) -> list[int]:
    jobs_csv = jobs_csv.expanduser()
    videos_dir = videos_dir.expanduser()
    decisions_csv = decisions_csv.expanduser()
    with decisions_csv.open(encoding="utf-8-sig", newline="") as file:
        decisions = {
            row["scene"].zfill(2): row
            for row in csv.DictReader(file)
            if row.get("scene", "").strip()
        }
    redo_scenes = sorted(int(scene) for scene, row in decisions.items() if row.get("review_status") == "redo")
    if not redo_scenes:
        return []

    with jobs_csv.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    policy_rows = []
    for row in rows:
        if not row.get("retry_policy_version", "").strip():
            continue
        current = dict(row)
        video_name = row.get("target_video_filename", f"{int(row['scene']):02d}.mp4")
        video_path = videos_dir / video_name
        if video_path.is_file():
            current["current_artifact_sha256"] = hashlib.sha256(video_path.read_bytes()).hexdigest()
        policy_rows.append(current)
    approvals = evaluate_quality_redos(policy_rows, decisions) if policy_rows else {}
    if policy_rows:
        redo_scenes = sorted(int(scene) for scene in approvals)
        if not redo_scenes:
            return []

    backup_dir = videos_dir / ("_review_redo_backup_" + time.strftime("%Y%m%d_%H%M%S"))
    backup_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        scene = int(row["scene"])
        if scene not in redo_scenes:
            continue
        video_name = row.get("target_video_filename", f"{scene:02d}.mp4")
        video_path = videos_dir / video_name
        if video_path.exists():
            shutil.move(str(video_path), str(backup_dir / video_path.name))
        for key in ["task_id", "client_business_id", "provider_failure_confirmed", "video_url", "error", "api_response", "query_response"]:
            if key in row:
                row[key] = ""
        row["status"] = "todo"
        decision = decisions.get(f"{scene:02d}", {})
        approval = approvals.get(f"{scene:02d}")
        if approval is not None:
            row["quality_retry_count"] = str(approval.next_retry_count)
            row["quality_version"] = str(approval.next_retry_count + 1)
            row["provider_attempt"] = str(int(row.get("provider_attempt") or "0") + 1)
            row["retry_defect_code"] = approval.defect_code
            row["retry_evidence"] = approval.evidence
            row["retry_root_cause"] = approval.root_cause
            row["retry_strategy"] = approval.retry_strategy
            row["retry_defect_severity"] = approval.severity
            row["v3_escalation_approved"] = (
                "true" if approval.v3_escalation_approved else "false"
            )
            row["batch_retry_calibration_status"] = approval.batch_calibration_status
            row["batch_retry_calibration_notes"] = approval.batch_calibration_notes
        row["notes"] = decision.get("notes", row.get("notes", ""))
        if approval is not None:
            # Never replace the compiler-owned prompt. The provider runner
            # appends this scoped repair after the locked director facts.
            row["provider_retry_prompt"] = (
                str(decision.get("prompt") or "").strip() or approval.retry_strategy
            )
        else:
            reviewed_prompt = _review_prompt(row, decision)
            if row.get("continuity_state") or row.get("visual_continuity_state"):
                reviewed_prompt = reviewed_prompt.split("视觉连续性硬约束（机器可读）：", 1)[0].rstrip(" ；;。")
            reviewed_prompt = _apply_review_notes_to_prompt(reviewed_prompt, row["notes"])
            row["prompt"] = enforce_prompt_continuity_contract(reviewed_prompt, row)
        row["review_status"] = "redo"
        row["review_notes"] = row["notes"]

    for key in [
        "prompt", "notes", "review_status", "review_notes", "quality_retry_count", "quality_version",
        "retry_defect_code", "retry_evidence", "retry_root_cause", "retry_strategy",
        "retry_defect_severity", "v3_escalation_approved", "provider_retry_prompt",
        "batch_retry_calibration_status", "batch_retry_calibration_notes",
    ]:
        if key not in fieldnames:
            fieldnames.append(key)
    with jobs_csv.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"旧视频备份：{backup_dir}")
    return redo_scenes


def resolve_generate_images_dir(jobs_csv: Path, images_dir: Path) -> Path:
    """Prefer the prepared job image folder when CSV filenames do not exist in the UI-selected folder."""
    jobs_csv = jobs_csv.expanduser()
    images_dir = images_dir.expanduser()
    try:
        with jobs_csv.open(encoding="utf-8-sig", newline="") as file:
            first_row = next(csv.DictReader(file), None)
    except FileNotFoundError:
        return images_dir
    image_filename = (first_row or {}).get("image_filename", "").strip()
    if not image_filename:
        return images_dir
    if (images_dir / image_filename).exists():
        return images_dir
    prepared_images_dir = jobs_csv.parent / "images"
    if (prepared_images_dir / image_filename).exists():
        print(f"图片目录已自动切换为任务图片副本：{prepared_images_dir}")
        return prepared_images_dir
    return images_dir


def run_generate_until_complete(
    invoke: Callable[[], None],
    *,
    jobs_csv: Path,
    videos_dir: Path,
    start_scene: int,
    end_scene: int,
    scenes: str,
    limit: int,
) -> None:
    max_rounds = 50
    for round_index in range(1, max_rounds + 1):
        pending_before = count_pending_generate_rows(
            jobs_csv,
            videos_dir,
            start_scene=start_scene,
            end_scene=end_scene,
            scenes=scenes,
            limit=limit,
        )
        if pending_before == 0:
            print("所有选中图生视频任务都已完成。")
            return
        if round_index > 1:
            print(f"继续自动接续图生视频：第 {round_index} 轮，剩余 {pending_before} 条。")
        invoke()
        pending_after = count_pending_generate_rows(
            jobs_csv,
            videos_dir,
            start_scene=start_scene,
            end_scene=end_scene,
            scenes=scenes,
            limit=limit,
        )
        if pending_after == 0:
            print("所有选中图生视频任务都已完成。")
            return
        if pending_after == pending_before:
            raise RuntimeError(f"图生视频自动接续没有减少待处理任务，剩余 {pending_after} 条，请查看日志。")
    raise RuntimeError(f"图生视频自动接续超过 {max_rounds} 轮仍未完成，请查看日志。")


def count_pending_generate_rows(
    jobs_csv: Path,
    videos_dir: Path,
    *,
    start_scene: int,
    end_scene: int,
    scenes: str,
    limit: int,
) -> int:
    jobs_csv = jobs_csv.expanduser()
    videos_dir = videos_dir.expanduser()
    with jobs_csv.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    selected_scene_numbers = parse_scene_filter_for_prompt_review(scenes)
    selected_rows = [
        row
        for row in rows
        if (
            int(row["scene"]) in selected_scene_numbers
            if selected_scene_numbers
            else start_scene <= int(row["scene"]) <= end_scene
        )
    ]
    if limit:
        selected_rows = selected_rows[:limit]
    pending = 0
    for row in selected_rows:
        video_path = videos_dir / row.get("target_video_filename", "")
        if video_path.exists() and video_path.stat().st_size > 0:
            continue
        if row.get("status", "") in {"downloaded", "approved"}:
            continue
        pending += 1
    return pending


def apply_prompt_review_confirmation(
    jobs_csv: Path,
    decisions_csv: Path,
    *,
    start_scene: int = 1,
    end_scene: int = 9999,
    scenes: str = "",
    limit: int = 0,
) -> None:
    """Apply pre-generation prompt edits and require explicit approval.

    This is a cost-control gate: image-to-video API calls should only happen
    after the user has reviewed the motion prompts and exported the prompt
    review CSV from the preview page.
    """
    jobs_csv = jobs_csv.expanduser()
    decisions_csv = decisions_csv.expanduser()
    continuity_errors = validate_image_video_jobs(jobs_csv)
    if continuity_errors:
        raise ValueError(
            "视觉连续性合同/任务校验失败，已停止调用视频 API。" + "；".join(continuity_errors)
        )
    if not decisions_csv.exists():
        raise FileNotFoundError(
            "缺少图生视频提示词确认 CSV，已停止调用视频 API。\n"
            f"请先打开审核页，检查/修改提示词后点击“导出提示词确认CSV”，再重试。\n"
            f"期望文件：{decisions_csv}"
        )

    with jobs_csv.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    with decisions_csv.open(encoding="utf-8-sig", newline="") as file:
        decisions = {
            row["scene"].zfill(2): row
            for row in csv.DictReader(file)
            if row.get("scene", "").strip()
        }

    selected_scene_numbers = parse_scene_filter_for_prompt_review(scenes)
    selected_rows = [
        row
        for row in rows
        if (
            int(row["scene"]) in selected_scene_numbers
            if selected_scene_numbers
            else start_scene <= int(row["scene"]) <= end_scene
        )
    ]
    if limit:
        selected_rows = selected_rows[:limit]

    missing: list[str] = []
    mismatched: list[str] = []
    not_approved: list[str] = []
    for row in selected_rows:
        scene = row["scene"].zfill(2)
        decision = decisions.get(scene)
        if decision is None:
            missing.append(scene)
            continue
        decision_image = (decision.get("image_filename") or "").strip()
        decision_story = (decision.get("story_text") or "").strip()
        if not decision_image and not decision_story:
            mismatched.append(f"{scene}:确认CSV缺少图片名/分镜文本")
            continue
        if decision_image and decision_image != (row.get("image_filename") or "").strip():
            mismatched.append(f"{scene}:图片名不匹配")
            continue
        if decision_story and decision_story != (row.get("story_text") or "").strip():
            mismatched.append(f"{scene}:分镜文本不匹配")
            continue
        status = (decision.get("review_status") or "").strip()
        if status != "approved":
            not_approved.append(f"{scene}:{status or '空'}")
            continue
        prompt = (decision.get("prompt") or "").strip()
        if prompt:
            row["prompt"] = enforce_prompt_continuity_contract(prompt, row)
        else:
            row["prompt"] = enforce_prompt_continuity_contract(row.get("prompt", ""), row)
        row["prompt_review_status"] = "approved"
        row["prompt_review_notes"] = (decision.get("notes") or "").strip()

    problems: list[str] = []
    if missing:
        problems.append("缺少确认记录：" + "、".join(missing))
    if mismatched:
        problems.append("确认记录与当前任务不匹配：" + "、".join(mismatched))
    if not_approved:
        problems.append("尚未标为通过：" + "、".join(not_approved))
    if problems:
        raise ValueError(
            "图生视频提示词尚未全部确认，已停止调用视频 API。\n"
            + "\n".join(problems)
            + "\n请在审核页确认这些镜头并重新导出 prompt_review_decisions.csv。"
        )

    for key in ["prompt", "prompt_review_status", "prompt_review_notes"]:
        if key not in fieldnames:
            fieldnames.append(key)
    with jobs_csv.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"已应用图生视频提示词确认：{len(selected_rows)} 条，来源：{decisions_csv}")


def parse_scene_filter_for_prompt_review(value: str) -> set[int]:
    scenes: set[int] = set()
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        scenes.add(int(part))
    return scenes


def first_release_asset(*values: str | Path | None) -> Path | None:
    for value in values:
        if not value:
            continue
        path = Path(value).expanduser()
        if not path.exists():
            continue
        if "placeholder" in path.stem.lower():
            continue
        return path
    return None


def _apply_review_notes_to_prompt(prompt: str, notes: str) -> str:
    base_prompt = re.sub(r"\s*\[审核备注\].*$", "", prompt).strip()
    note = notes.strip()
    if not note:
        return base_prompt

    if any(keyword in note for keyword in ["不要出现任何人物", "没有人物", "不要有人物", "无人物"]):
        base_prompt = base_prompt.replace(
            "角色做自然轻微的眨眼和呼吸动作",
            "镜头轻轻推进当前画面中的牌匾和风景，画面中不要出现任何人物",
        )
        extra = "画面中不要出现任何人物、角色、路人、动物或新增道具，只保留当前参考图里的牌匾和环境。"
    elif any(keyword in note for keyword in ["足球", "毽", "球类", "道具"]):
        extra = "画面中不要出现足球、毽子、球类或其他新增道具，只保留原图中的角色和场景。"
    elif any(keyword in note for keyword in ["走路", "走着", "脚步", "步态"]):
        extra = "只表现自然走路动作，不要踢球、跳跃、追逐或加入其他物体。"
    else:
        extra = note

    return f"{base_prompt} [审核备注] {extra}"


def _review_prompt(row: dict[str, str], decision: dict[str, str]) -> str:
    prompt = (decision.get("prompt") or "").strip()
    return prompt or row.get("prompt", "")


if __name__ == "__main__":
    main()
