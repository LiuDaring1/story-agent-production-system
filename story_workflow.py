from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from analyze_storyboard_pacing import analyze_storyboard_pacing
from story_video_synthesizer.media import probe_duration
from story_video_synthesizer.image_video import (
    enforce_prompt_continuity_contract,
    validate_image_video_jobs,
)
from video_provider_adapter import resolve_video_provider

from story_project import (
    auto_keying,
    create_theme_asset_request,
    detect_project_assets,
    doctor_project,
    first_existing,
    first_image_in,
    final_delivery,
    init_project,
    load_config,
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
    semi_auto_status,
    update_story_info,
    write_manifest,
)


ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="儿童故事图片到成片的统一流程入口")
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

    release_preview = subparsers.add_parser("preview-release-project", help="可选刷新当前发布视频预览帧，不编码完整视频")
    release_preview.add_argument("--project-dir", required=True, type=Path)
    release_preview.add_argument("--variant", choices=["auto", "both", "main", "library"], default="auto")
    release_preview.add_argument("--times", default="1,2,37,92")
    release_preview.add_argument("--person-layouts", default="", help="主账号人物布局候选；留空只预览 keying_preset 当前最终参数，auto 生成候选")

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
    product_preflight.add_argument("--demo-person-crop-mode", choices=["preset", "full-width"], default="full-width")
    product_preflight.add_argument("--demo-person-vertical-align", choices=["center", "bottom"], default="bottom")
    product_preflight.add_argument("--demo-person-crop-bottom-ratio", default=0.0, type=float)

    product_project = subparsers.add_parser("product-package-project", help="从桌面项目自动生成基础版/进阶版资料包")
    product_project.add_argument("--project-dir", required=True, type=Path)
    product_project.add_argument("--annotation-docx", type=Path)
    product_project.add_argument("--annotation-json", type=Path)
    product_project.add_argument("--allow-draft-annotation", action="store_true", default=False)
    product_project.add_argument("--demo-person-crop-mode", choices=["preset", "full-width"], default="full-width")
    product_project.add_argument("--demo-person-vertical-align", choices=["center", "bottom"], default="bottom")
    product_project.add_argument("--demo-person-crop-bottom-ratio", default=0.0, type=float)

    normalize = subparsers.add_parser("normalize-images", help="文生图输出 -> 标准 images 目录")
    normalize.add_argument("--source-dir", required=True, type=Path)
    normalize.add_argument("--output-dir", required=True, type=Path)
    normalize.add_argument("--slug", required=True)
    normalize.add_argument("--count", default=0, type=int)

    pacing = subparsers.add_parser("analyze-pacing", help="分析手动换行分镜是否适合 Grok Video 1.5（1-15 秒，默认 8 秒）图生视频")
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
    timing.add_argument("--max-duration", default=8.0, type=float, help="单镜头推荐默认 8 秒；Grok Video 1.5 支持 1-15 秒")
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

    generate = subparsers.add_parser("generate", help="调用视频 API 生成片段")
    generate.add_argument("--jobs-csv", required=True, type=Path)
    generate.add_argument("--images-dir", required=True, type=Path)
    generate.add_argument("--videos-dir", required=True, type=Path)
    generate.add_argument("--start-scene", default=1, type=int)
    generate.add_argument("--end-scene", default=9999, type=int)
    generate.add_argument("--scenes", default="")
    generate.add_argument("--limit", default=0, type=int)
    generate.add_argument("--dry-run", action="store_true")
    generate.add_argument("--submit-all-first", action="store_true", help="先批量提交待生成任务，再逐个轮询下载")
    generate.add_argument("--max-submit-first", default=20, type=int, help="批量提交模式下一次最多保留多少个已提交未完成任务；0 表示不限制")
    generate.add_argument("--prompt-review-csv", default="", help="提示词确认页导出的 prompt_review_decisions.csv")
    generate.add_argument("--skip-prompt-review", action="store_true", help="跳过图生视频提示词确认闸门")
    generate.add_argument("--provider", default="", help="覆盖 pipeline_config.json 中的图生视频 provider")

    rerun_review = subparsers.add_parser("rerun-review", help="根据审核 CSV 只重跑标记为重做的片段")
    rerun_review.add_argument("--jobs-csv", required=True, type=Path)
    rerun_review.add_argument("--images-dir", required=True, type=Path)
    rerun_review.add_argument("--videos-dir", required=True, type=Path)
    rerun_review.add_argument("--decisions-csv", required=True, type=Path)
    rerun_review.add_argument("--provider", default="", help="覆盖 pipeline_config.json 中的图生视频 provider")

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

    release = subparsers.add_parser("package-release", help="背景成片 -> 主账号/宝库号小红书发布视频")
    release.add_argument("--story-name", required=True)
    release.add_argument("--duration-text", required=True)
    release.add_argument("--bg-video", required=True, type=Path)
    release.add_argument("--output-dir", required=True, type=Path)
    release.add_argument("--variant", choices=["both", "main", "library"], default="both")
    release.add_argument("--bg-image", type=Path)
    release.add_argument("--person-greenscreen", type=Path)
    release.add_argument("--audio-mix", type=Path)
    release.add_argument("--watermark-logo", type=Path)
    release.add_argument("--antipiracy-logo", type=Path)
    release.add_argument("--plate-image", type=Path)
    release.add_argument("--video-box", default="0,416,1080,608")
    release.add_argument("--watermark-width", default=190, type=int)
    release.add_argument("--watermark-opacity", default=0.78, type=float)
    release.add_argument("--watermark-speed", default=1.0, type=float)
    release.add_argument("--frame-image", type=Path)
    release.add_argument("--frame-image-b", type=Path)
    release.add_argument("--story-box", default="170,250,990,557")
    release.add_argument("--b-story-box", default="150,88,1620,911")
    release.add_argument("--b-windows", default="")
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
    release.add_argument("--keyer", choices=["chromakey", "colorkey"], default="chromakey")
    release.add_argument("--keying-preset-json", type=Path)
    release.add_argument("--person-crop", default="")
    release.add_argument("--person-grade", choices=["none", "log-soft", "log-strong"], default="none")
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
    product.add_argument("--demo-person-crop-mode", choices=["preset", "full-width"], default="preset")
    product.add_argument("--demo-person-vertical-align", choices=["center", "bottom"], default="center")
    product.add_argument("--preview-only", action="store_true")
    product.add_argument("--preview-times", default="0.8,1.5,2.5,37,92")
    product.add_argument("--music-volume", default=0.22, type=float)
    product.add_argument("--narration-volume", default=1.0, type=float)

    args = parser.parse_args()
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
        report = qa_videos(args.project_dir, args.videos_dir)
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
        preset = auto_keying(args.project_dir, args.greenscreen)
        print(f"已生成自动抠像参数：{preset}")
    elif args.command == "prepare-release-assets-project":
        outputs = create_theme_asset_request(args.project_dir)
        for label, path in outputs.items():
            print(f"{label}: {path}")
        try:
            preset = auto_keying(args.project_dir)
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
        run_package_release_project(args.project_dir, args.variant)
    elif args.command == "preview-release-project":
        run_package_release_project(args.project_dir, args.variant, preview_times=args.times, preview_person_layouts=args.person_layouts)
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
        run_script("apply_narration_durations.py", *command)
    elif args.command == "generate":
        continuity_errors = validate_image_video_jobs(args.jobs_csv)
        if continuity_errors:
            raise ValueError(
                "视觉连续性合同/任务校验失败，已在付费调用前阻断：" + "；".join(continuity_errors)
            )
        provider = resolve_video_provider(load_config(), ROOT, args.provider)
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
            *provider.runner_args(),
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
        ]
        if args.scenes:
            command.extend(["--scenes", args.scenes])
        if args.limit:
            command.extend(["--limit", str(args.limit)])
        if args.dry_run:
            command.append("--dry-run")
        if args.submit_all_first:
            command.append("--submit-all-first")
            command.extend(["--max-submit-first", str(args.max_submit_first)])
        if args.dry_run:
            run_script(str(provider.runner), *command)
        else:
            run_generate_until_complete(
                command,
                runner=provider.runner,
                jobs_csv=args.jobs_csv,
                videos_dir=args.videos_dir,
                start_scene=args.start_scene,
                end_scene=args.end_scene,
                scenes=args.scenes,
                limit=args.limit,
            )
    elif args.command == "rerun-review":
        provider = resolve_video_provider(load_config(), ROOT, args.provider)
        scenes = reset_redo_scenes(args.jobs_csv, args.videos_dir, args.decisions_csv)
        if not scenes:
            print("审核 CSV 中没有标记为重做的片段。")
            return
        print("需要重跑：" + ", ".join(f"{scene:02d}" for scene in scenes))
        run_script(
            str(provider.runner),
            *provider.runner_args(),
            "--jobs-csv",
            args.jobs_csv,
            "--images-dir",
            args.images_dir,
            "--videos-dir",
            args.videos_dir,
            "--scenes",
            ",".join(str(scene) for scene in scenes),
            "--submit-all-first",
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
            inferred_subtitle_script = Path(args.output_dir).expanduser().parent / "00_输入素材" / "story_source.txt"
            if inferred_subtitle_script.exists():
                subtitle_script = inferred_subtitle_script
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
        if args.whisper_model_dir is not None:
            command.extend(["--whisper-model-dir", args.whisper_model_dir])
        run_script("synthesize.py", *command)
    elif args.command == "package-release":
        command = [
            "--story-name",
            args.story_name,
            "--duration-text",
            args.duration_text,
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
        ]
        for flag, value in (
            ("--bg-image", args.bg_image),
            ("--person-greenscreen", args.person_greenscreen),
            ("--audio-mix", args.audio_mix),
            ("--watermark-logo", args.watermark_logo),
            ("--antipiracy-logo", args.antipiracy_logo),
            ("--plate-image", args.plate_image),
            ("--frame-image", args.frame_image),
            ("--frame-image-b", args.frame_image_b),
            ("--story-logo", args.story_logo),
            ("--subtitle-srt", args.subtitle_srt),
            ("--keying-preset-json", args.keying_preset_json),
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


def annotation_skill_path_from_config() -> Path | None:
    value = str(load_config().get("external_tools", {}).get("story_performance_script_skill", "")).strip()
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def build_abc_scene_windows(duration: float, subtitle_srt: Path | None = None) -> tuple[str, str]:
    """Return B and C windows snapped to speech gaps; A is the default and always ends the video."""
    cue_ends: list[float] = []
    if subtitle_srt is not None and subtitle_srt.exists():
        from release_video import parse_srt

        cue_ends = [end for _start, end, _text in parse_srt(subtitle_srt) if 1.0 < end < duration - 1.0]
    boundaries: list[float] = []
    target = 15.0
    while target < duration - 8.0:
        nearby = [value for value in cue_ends if abs(value - target) <= 5.0 and (not boundaries or value - boundaries[-1] >= 8.0)]
        chosen = min(nearby, key=lambda value: abs(value - target)) if nearby else target
        if not boundaries or chosen - boundaries[-1] >= 8.0:
            boundaries.append(chosen)
        target = chosen + 18.0
    points = [0.0, *boundaries, duration]
    segments = [(points[index], points[index + 1]) for index in range(len(points) - 1) if points[index + 1] - points[index] >= 1.0]
    modes = ["c", "b", "a"]
    assigned = [modes[index % len(modes)] for index in range(len(segments))]
    if assigned:
        assigned[-1] = "a"
    b_windows: list[str] = []
    c_windows: list[str] = []
    for (start, end), mode in zip(segments, assigned):
        value = f"{start:.3f}-{end:.3f}"
        if mode == "b":
            b_windows.append(value)
        elif mode == "c":
            c_windows.append(value)
    return ",".join(b_windows), ",".join(c_windows)


def run_package_release_project(
    project_dir: Path,
    variant: str,
    preview_times: str | None = None,
    preview_person_layouts: str | None = None,
) -> None:
    paths = project_paths(project_dir)
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
        from story_agent_runtime import review_bundle_is_current, review_passes

        preview_bundle = paths.status / "reviews" / "release_preview_bundle.json"
        preview_review = paths.status / "reviews" / "release_preview_review.json"
        try:
            review_payload = json.loads(preview_review.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("全片渲染前必须完成独立发布预览审核") from exc
        if not review_bundle_is_current(preview_bundle) or not review_passes(review_payload, artifact=preview_bundle):
            raise RuntimeError("发布预览审核未通过，或审核后的択像/布局产物已变更，拒绝渲染全片")
    config = load_config()
    story = manifest["story"]
    outputs = manifest["outputs"]
    inputs = manifest["inputs"]
    main_bg_video = first_existing(
        outputs.get("background_video_no_sub"),
        paths.assembly / "story_no_subs_bgm.mp4",
        outputs.get("background_video_with_sub"),
        paths.assembly / "story_sales_subs_bgm.mp4",
        outputs.get("demo_voice_bgm"),
        paths.assembly / "story_demo_voice_bgm.mp4",
    )
    library_bg_video = first_existing(
        outputs.get("background_video_no_sub"),
        paths.assembly / "story_no_subs_bgm.mp4",
        outputs.get("demo_voice_bgm"),
        paths.assembly / "story_demo_voice_bgm.mp4",
        outputs.get("background_video_with_sub"),
        paths.assembly / "story_sales_subs_bgm.mp4",
    )
    if main_bg_video is None and library_bg_video is None:
        raise FileNotFoundError("缺少背景成片：请先完成 ⑪ 合成背景成片。")
    audio_mix = first_existing(outputs.get("demo_voice_bgm"), paths.assembly / "story_demo_voice_bgm.mp4", inputs.get("narration"))
    greenscreen = first_existing(inputs.get("greenscreen_video"))
    theme_dir = paths.release / "theme_assets"
    # 第 12 步只生成一个统一故事框；非严格 QA 只负责从源图导出 A 框。
    try:
        qa_theme_assets(paths, strict=False)
    except Exception:
        pass
    main_plate = first_release_asset(theme_dir / "main_release_plate.png", theme_dir / "release_plate.png", outputs.get("release_plate_image"))
    library_plate = first_release_asset(theme_dir / "library_release_plate.png", outputs.get("library_release_plate_image"))
    bg_image = first_existing(outputs.get("main_background_image"), theme_dir / "main_background_16x9.png")
    frame_a = first_existing(outputs.get("story_frame_a"), theme_dir / "story_frame_a.png")
    frame_b = frame_a
    subtitle_srt = first_existing(
        outputs.get("subtitles_srt"),
        paths.assembly / "story_subtitles.srt",
        outputs.get("sales_subtitles_srt"),
        paths.assembly / "story_sales_subtitles.srt",
    )
    release_defaults = config.get("release_defaults", {})
    brand_assets = config.get("brand_assets", {})
    story_logo = first_existing(brand_assets.get("story_logo"), brand_assets.get("logo"))
    watermark_logo = first_existing(brand_assets.get("watermark_logo"))
    if story_logo is not None and watermark_logo is not None and story_logo.resolve() == watermark_logo.resolve():
        watermark_logo = None
    antipiracy_logo = first_existing(brand_assets.get("antipiracy_logo"), brand_assets.get("logo"))
    keying_preset = first_existing(outputs.get("keying_preset"), paths.release / "keying" / "keying_preset.json")
    if keying_preset is None and greenscreen is not None:
        try:
            keying_preset = auto_keying(paths.root)
        except Exception as exc:
            print(f"[warning] 自动抠像参数暂不可用：{exc}")

    requested_variant = variant
    if requested_variant == "auto":
        requested_variant = "both" if greenscreen and audio_mix and bg_image and main_bg_video else "library"

    missing: list[str] = []
    if requested_variant in {"both", "main"}:
        for label, value in (
            ("主账号底板 main_release_plate.png", main_plate),
            ("主账号 16:9 背景图 main_background_16x9.png", bg_image),
            ("A 景透明故事框 story_frame_a.png", frame_a),
            ("无字幕背景视频 story_no_subs_bgm.mp4", main_bg_video),
            ("人声混音 story_demo_voice_bgm.mp4", audio_mix),
            ("绿幕视频", greenscreen),
        ):
            if value is None:
                missing.append(label)
        if release_defaults.get("b_windows") and frame_a is None:
            missing.append("统一透明故事框 story_frame_a.png")
    if requested_variant in {"both", "library"}:
        if library_bg_video is None:
            missing.append("宝库号有人声有字幕版本 story_demo_voice_bgm.mp4")
        if library_plate is None:
            missing.append("宝库号底板 library_release_plate.png")
    if missing:
        request = create_theme_asset_request(paths.root)["request"]
        raise FileNotFoundError(
            "发布视频缺少必要素材：" + "、".join(missing) + f"。请先点击 ⑫ 生成/打开发布素材任务书，把复制的指令发给 Codex；Codex 生成素材后，再点 ⑬ 接收并体检发布素材：{request}"
        )
    qa_theme_assets(paths, strict=True)

    def resolve_scene_windows(bg_video: Path) -> tuple[str, str]:
        configured_b = str(release_defaults.get("b_windows", "") or "").strip()
        configured_c = str(release_defaults.get("c_windows", "") or "").strip()
        auto_b, auto_c = build_abc_scene_windows(probe_duration(bg_video), subtitle_srt)
        b_windows = configured_b if configured_b and configured_b.lower() != "auto" else auto_b
        c_windows = configured_c if configured_c and configured_c.lower() != "auto" else auto_c
        return b_windows, c_windows

    def release_command(selected_variant: str, bg_video: Path, plate_image: Path | None) -> list[object]:
        command: list[object] = [
            "--story-name",
            story.get("name", ""),
            "--duration-text",
            story.get("duration_text") or "待定",
            "--bg-video",
            bg_video,
            "--output-dir",
            paths.release,
            "--variant",
            selected_variant,
            "--video-box",
            release_defaults.get("video_box", "0,416,1080,608"),
            "--story-box",
            release_defaults.get("story_box", "210,270,910,512"),
            "--story-bleed",
            str(release_defaults.get("story_bleed", 0)),
            "--background-blur",
            str(release_defaults.get("background_blur", 14)),
            "--b-story-box",
            release_defaults.get("b_story_box", "356,180,1209,680"),
            "--crf",
            str(release_defaults.get("crf", 17)),
            "--preset",
            release_defaults.get("preset", "medium"),
            "--output-scale",
            str(release_defaults.get("output_scale", 2)),
            "--person-height",
            str(release_defaults.get("person_height", 900)),
            "--person-x",
            str(release_defaults.get("person_x", 1200)),
            "--person-y",
            str(release_defaults.get("person_y", 105)),
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
            str(release_defaults.get("watermark_width", 190)),
            "--watermark-opacity",
            str(release_defaults.get("watermark_opacity", 0.78)),
            "--watermark-speed",
            str(release_defaults.get("watermark_speed", 1.0)),
            "--tail-seconds",
            str(release_defaults.get("tail_seconds", 3.0)),
            "--tail-notice-text",
            str(release_defaults.get("tail_notice_text", "有需要联系客服，好作品有偿分享！")),
            "--story-logo-width-a",
            str(release_defaults.get("story_logo_width_a", 150)),
            "--story-logo-width-b",
            str(release_defaults.get("story_logo_width_b", 175)),
            "--story-logo-x",
            str(release_defaults.get("story_logo_x", 42)),
            "--story-logo-y",
            str(release_defaults.get("story_logo_y", 44)),
            "--subtitle-font-size",
            str(release_defaults.get("subtitle_font_size", 42)),
            "--subtitle-margin-v",
            str(release_defaults.get("subtitle_margin_v", 72)),
        ]
        optional: list[tuple[str, object | None]] = [
            ("--plate-image", plate_image),
            ("--watermark-logo", watermark_logo),
            ("--antipiracy-logo", antipiracy_logo),
            ("--story-logo", story_logo),
            ("--keying-preset-json", keying_preset),
        ]
        person_crop = str(release_defaults.get("person_crop", "") or "").strip()
        if person_crop:
            command.extend(["--person-crop", person_crop])
        if selected_variant == "main":
            optional.extend(
                [
                    ("--bg-image", bg_image),
                    ("--person-greenscreen", greenscreen),
                    ("--audio-mix", audio_mix),
                    ("--frame-image", frame_a),
                    ("--frame-image-b", frame_b),
                    ("--subtitle-srt", subtitle_srt),
                ]
            )
            b_windows, c_windows = resolve_scene_windows(bg_video)
            if frame_b is not None and b_windows:
                command.extend(["--b-windows", b_windows])
            if c_windows:
                command.extend(["--c-windows", c_windows])
        if selected_variant == "library":
            optional.extend(
                [
                    ("--audio-mix", audio_mix),
                    ("--subtitle-srt", subtitle_srt),
                ]
            )
        if preview_times is not None:
            command.extend(["--preview-dir", preview_dir, "--preview-times", preview_times])
            if selected_variant == "main" and preview_person_layouts:
                command.extend(["--preview-person-layouts", preview_person_layouts])
        for flag, value in optional:
            if value is not None:
                command.extend([flag, value])
        return command

    if requested_variant in {"both", "main"}:
        assert main_bg_video is not None
        run_script("release_video.py", *release_command("main", main_bg_video, main_plate))
    if requested_variant in {"both", "library"}:
        assert library_bg_video is not None
        run_script("release_video.py", *release_command("library", library_bg_video, library_plate))
    if is_preview:
        sheet = render_release_preview_contact_sheet(preview_dir, run_id)
        handoff = write_release_preview_index(preview_dir, sheet, run_id)
        feedback_handoff = write_release_preview_feedback_handoff(preview_dir, sheet, run_id)
        print("", flush=True)
        print(f"本轮预览目录：{preview_dir}", flush=True)
        print(f"总览拼图：{sheet}", flush=True)
        print(f"预览索引：{handoff}", flush=True)
        print(f"反馈给 Codex：{feedback_handoff}", flush=True)
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


def important_preview_images(preview_dir: Path) -> list[Path]:
    names = [
        "preview_contact_sheet.png",
        "main_002s_a_h84.png",
        "main_002s_a_h90.png",
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
    preset_path = project_dir / "04_发布视频" / "keying" / "keying_preset.json"
    index_path = preview_dir / "preview_index.md"
    lines = [
        "# 第 13 步预览反馈给 Codex",
        "",
        "请不要只总结图片。请直接查看本轮发布视频预览，判断主账号和宝库号是否适合进入正式生成；如果主账号人物位置、大小、抠像边缘、故事框、字幕、Logo、背景虚化或画面留白需要调整，请直接修改 keying_preset.json，然后让我回工作台重新点击第 13 步复查。",
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
    preset_path = paths.release / "keying" / "keying_preset.json"
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
        "",
        "## 当前 keying_preset.json",
        "```json",
        json.dumps(preset, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 自动候选布局",
        "- h78: person_height_ratio=0.78, person_x=1280, person_y=252",
        "- h84: person_height_ratio=0.84, person_x=1230, person_y=220",
        "- h90: person_height_ratio=0.90, person_x=1190, person_y=188",
        "- h96: person_height_ratio=0.96, person_x=1160, person_y=154",
        "",
        "## Codex 应执行",
        "1. 先看 preview_contact_sheet.png，再逐张查看候选 A 镜、B 镜和宝库号预览。",
        "2. 选出最终人像布局，必要时微调 person_x/person_y/person_height_ratio。",
        "3. 写回 keying_preset.json；后续普通预览只看最终参数，不再保留候选猜参数。",
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
    manifest = refresh_project_outputs(paths.root)
    story = manifest["story"]
    outputs = manifest["outputs"]
    inputs = manifest["inputs"]
    story_text = first_existing(inputs.get("story_text"))
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
        story.get("age_range", "6-8岁"),
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
) -> None:
    paths = project_paths(project_dir)
    manifest = detect_project_assets(paths.root, extract_audio=True)
    manifest = refresh_project_outputs(paths.root)
    existing_keying = first_existing(manifest["outputs"].get("keying_preset"), paths.release / "keying" / "keying_preset.json")
    if existing_keying is None:
        try:
            auto_keying(paths.root)
        except Exception as exc:
            print(f"[warning] 自动抠像参数暂不可用：{exc}")
        manifest = refresh_project_outputs(paths.root)
    story = manifest["story"]
    inputs = manifest["inputs"]
    outputs = manifest["outputs"]
    story_text = first_existing(inputs.get("story_text"))
    story_document_text = first_existing(outputs.get("consumer_manuscript"), story_text)
    script_lines = first_existing(inputs.get("story_text"), paths.inputs / "story_source.txt")
    narration = first_existing(inputs.get("narration"), inputs.get("extracted_narration"))
    music = first_existing(paths.video_jobs / "music" / f"{story.get('slug')}_background_music.mp3", inputs.get("music"))
    images_dir = paths.video_jobs / "images" if (paths.video_jobs / "images").exists() else paths.images / "images"
    bg_with_sub = first_existing(outputs.get("background_video_with_sub"), paths.assembly / "story_sales_subs_bgm.mp4")
    bg_no_sub = first_existing(outputs.get("background_video_no_sub"), paths.assembly / "story_no_subs_bgm.mp4")
    greenscreen = first_existing(inputs.get("greenscreen_video"))
    keying_preset = first_existing(outputs.get("keying_preset"), paths.release / "keying" / "keying_preset.json")
    demo_bg = first_existing(outputs.get("main_background_image"), paths.release / "theme_assets" / "main_background_16x9.png")
    story_frame_a = first_existing(outputs.get("story_frame_a"), paths.release / "theme_assets" / "story_frame_a.png")
    timings = first_existing(outputs.get("timings_json"), paths.assembly / "timings.json")
    source_subtitles = first_existing(paths.assembly / "story_subtitles.srt")
    if story_text is not None and source_subtitles is not None:
        product_script, product_timings = build_product_text_sources_from_story_source(
            story_text=story_text,
            subtitles_srt=source_subtitles,
            output_dir=paths.status / "product_package_work",
        )
        script_lines = product_script
        timings = product_timings
    config = load_config()
    brand_assets = config.get("brand_assets", {})
    demo_logo = first_existing(brand_assets.get("story_logo"), brand_assets.get("logo"))
    product_defaults = config.get("product_defaults", {})
    if not bool(product_defaults.get("include_demo_logo", False)):
        demo_logo = None
    release_defaults = config.get("release_defaults", {})
    missing = []
    for label, value in (
        ("故事正文", story_document_text),
        ("逐行台词", script_lines),
        ("旁白", narration),
        ("配乐", music),
        ("图片目录", images_dir if images_dir.exists() else None),
        ("含字幕背景视频", bg_with_sub),
        ("无字幕背景视频", bg_no_sub),
        ("绿幕视频", greenscreen),
        ("抠像参数", keying_preset),
        ("主账号 A 镜背景图", demo_bg),
        ("主账号 A 镜故事框", story_frame_a),
        ("timings.json", timings),
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
    annotation_skill_path = (
        annotation_skill_path_from_config()
    )
    if demo_logo is not None:
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
    manifest = refresh_project_outputs(paths.root)
    write_manifest(paths, manifest)
    if not preview_only:
        qa_product(paths.root)


def build_product_text_sources_from_story_source(story_text: Path, subtitles_srt: Path, output_dir: Path) -> tuple[Path, Path]:
    story_lines = [
        line.strip()
        for line in story_text.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
        if line.strip()
    ]
    cues = parse_simple_srt(subtitles_srt)
    output_dir.mkdir(parents=True, exist_ok=True)
    patched_timings: list[dict] = []
    cue_index = 0
    for index, line in enumerate(story_lines, start=1):
        line_key = normalize_story_text_for_alignment(line)
        start: float | None = None
        end: float | None = None
        consumed = ""
        line_cue_start = cue_index
        while cue_index < len(cues) and len(consumed) < len(line_key):
            cue_start, cue_end, cue_text = cues[cue_index]
            if start is None:
                start = cue_start
            end = cue_end
            consumed += normalize_story_text_for_alignment(cue_text)
            cue_index += 1
            if line_key and line_key in consumed:
                break
        if not line_key:
            start = end = 0.0
        elif line_key not in consumed:
            matched = consumed[:48]
            raise ValueError(
                f"故事原文第 {index} 行无法与 story_subtitles.srt 顺序对齐：{line}\n"
                f"已匹配到：{matched}\n"
                "请先修正原文或字幕，资料包不再回退使用画面描述文本。"
            )
        if start is None or end is None:
            if patched_timings:
                start = patched_timings[-1]["source_end"]
                end = start
            else:
                start = end = 0.0
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
                "source_cue_start": line_cue_start + 1,
                "source_cue_end": cue_index,
            }
        )
    script_path = output_dir / "product_script_lines_from_story_source.txt"
    timings_path = output_dir / "product_timings_from_story_source.json"
    script_path.write_text("\n".join(story_lines) + "\n", encoding="utf-8")
    timings_path.write_text(json.dumps(patched_timings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return script_path, timings_path


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
        for key in ["task_id", "video_url", "error", "api_response", "query_response"]:
            if key in row:
                row[key] = ""
        row["status"] = "todo"
        decision = decisions.get(f"{scene:02d}", {})
        row["notes"] = decision.get("notes", row.get("notes", ""))
        row["prompt"] = _apply_review_notes_to_prompt(_review_prompt(row, decision), row["notes"])
        row["review_status"] = "redo"
        row["review_notes"] = row["notes"]

    for key in ["prompt", "review_status", "review_notes"]:
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
    command: list[object],
    *,
    runner: Path,
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
        run_script(str(runner), *command)
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
