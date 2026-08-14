from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn as docx_qn
from docx.shared import Inches, Pt, RGBColor
from lxml import etree
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
from pptx import Presentation
from pptx.dml.color import RGBColor as PptRGBColor
from pptx.enum.text import PP_ALIGN
from pptx.oxml.ns import qn as pptx_qn
from pptx.enum.text import MSO_ANCHOR
from pptx.util import Emu, Pt as PptPt

from story_video_synthesizer.align import LineTiming, align_evenly, read_script_lines
from story_contract_consumers import BINDING_FIELDS, semantic_line_indices, write_json_atomic
from artifact_semantic_plan import load_current_artifact_semantic_plan, plan_binding, selected_line_indices
from demo_quality import load_demo_brand_spec, write_demo_render_manifest
from keying_quality import blurred_background_issues, file_sha256, keying_preset_lock_issues
from story_video_synthesizer.image_video import sorted_image_files
from story_video_synthesizer.media import ensure_dir, probe_duration, run_command
from story_video_synthesizer.subtitles import write_srt
from story_semantics import SemanticKind, classify_story, lines_for_output


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
PPT_W = Emu(12192000)
PPT_H = Emu(6858000)
PPT_COVER_SEC = 3.0
PPT_AUDIO_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/audio"
PPT_MEDIA_REL = "http://schemas.microsoft.com/office/2007/relationships/media"
PPT_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
PPT_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
PPT_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
PPT_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PPT_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PPT_P14_NS = "http://schemas.microsoft.com/office/powerpoint/2010/main"
PPT_P14_EXT_URI = "{DAA4B4D4-6D71-4841-9C94-3DE7FCFB9230}"
DELIVERY_CJK_FONT = "Arial Unicode MS"
PPT_SUBTITLE_MAX_LINES = 2
PPT_SUBTITLE_MAX_HEIGHT_RATIO = 0.16


@dataclass(frozen=True)
class KeyingPreset:
    keyer: str = "colorkey"
    chroma_color: str = "0x00FF00"
    chroma_similarity: float = 0.10
    chroma_blend: float = 0.0
    person_crop: tuple[int, int, int, int] | None = None
    detected_person_bbox: tuple[int, int, int, int] | None = None
    person_grade: str = "none"
    person_beauty: str = "light"
    person_height_ratio: float = 0.96
    person_x: int | None = None
    person_y: int | None = None
    bottom_margin: int = 0


def artifact_semantic_product_selections(
    script_lines: list[str], semantic_plan: dict[str, Any]
) -> dict[str, list[int]]:
    """Compile the minimal deterministic product projection used below."""

    return {
        "ppt": selected_line_indices(script_lines, semantic_plan, "ppt"),
        "customer_manuscript": selected_line_indices(script_lines, semantic_plan, "customer_manuscript"),
        "reading_annotation": selected_line_indices(script_lines, semantic_plan, "reading_annotation"),
        "demo_subtitles": selected_line_indices(script_lines, semantic_plan, "demo_subtitles"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="生成绵羊故事锦囊基础版/进阶版资料包")
    parser.add_argument("--story-name", required=True)
    parser.add_argument("--slug", default="", help="图片文件名中的故事 slug；留空则自然排序图片")
    parser.add_argument("--story-text", required=True, type=Path, help="故事正文 txt/md/docx")
    parser.add_argument("--script-lines", required=True, type=Path, help="逐行台词 txt")
    parser.add_argument("--narration", required=True, type=Path, help="完整旁白录音")
    parser.add_argument("--music", required=True, type=Path, help="故事配乐 mp3/m4a/wav")
    parser.add_argument("--images-dir", required=True, type=Path, help="镜头图片目录")
    parser.add_argument("--bg-video-with-sub", required=True, type=Path, help="规范背景视频：含字幕")
    parser.add_argument("--bg-video-no-sub", required=True, type=Path, help="规范背景视频：无字幕")
    parser.add_argument("--person-greenscreen", required=True, type=Path, help="绿幕示范表演素材")
    parser.add_argument("--demo-background-image", type=Path, help="示范视频背景图；第 16 步应传主账号 A 镜 16:9 无框背景图")
    parser.add_argument("--story-frame-a-image", type=Path, help="主账号 A 镜故事框 PNG；用于进阶版额外生成无人物 A 镜视频")
    parser.add_argument("--demo-logo", type=Path, help="示范视频左上角品牌 Logo PNG")
    parser.add_argument("--demo-logo-width", default=150, type=int)
    parser.add_argument("--demo-logo-x", default=24, type=int)
    parser.add_argument("--demo-logo-y", default=20, type=int)
    parser.add_argument("--demo-brand-spec", type=Path, help="审核合同编译出的 Demo 官方 Logo 与布局规格")
    parser.add_argument("--annotation-docx", type=Path, help="人工/模型精修后的朗读标注文档；传入后直接使用，不走自动草稿")
    parser.add_argument("--annotation-json", type=Path, help="按 story-performance-script.skill 精修后的朗读标注 JSON")
    parser.add_argument("--annotation-skill-path", default=Path.home() / "Downloads" / "story-performance-script.skill", type=Path)
    parser.add_argument("--allow-draft-annotation", action="store_true", help="允许使用规则草稿生成朗读标注；默认禁止，避免产出不可用假成品")
    parser.add_argument("--keying-preset-json", type=Path, help="已确认的绿幕抠像参数 JSON")
    parser.add_argument("--allow-test-greenscreen", action="store_true", help="允许使用文件名含 test/测试 的绿幕素材")
    parser.add_argument("--allow-full-subtitle-bg", action="store_true", help="允许含开头/结尾字幕的背景视频进入资料包")
    parser.add_argument("--output-root", default="~/Desktop", type=Path)
    parser.add_argument("--work-dir", type=Path, help="中间文件目录；默认 output/product_package_work/<slug>")
    parser.add_argument("--timings-json", type=Path, help="已有 timings.json；正式资料包必须提供，用于校准示范字幕和 PPT 翻页")
    parser.add_argument("--allow-even-timings", action="store_true", help="允许无 timings.json 时按旁白总时长平均分配；仅限内部预览")
    parser.add_argument("--whisper-model", default="base", help="保留接口；正式资料包请先用 timing 流程生成 timings.json")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--demo-width", default=1920, type=int)
    parser.add_argument("--demo-height", default=1080, type=int)
    parser.add_argument("--demo-crf", default=15, type=int)
    parser.add_argument("--demo-preset", default="medium")
    parser.add_argument("--demo-person-crop-bottom-ratio", default=0.0, type=float, help="示范视频专用：从绿幕人物裁切框底部额外收掉的比例，用于清理发布框原本遮住的地面横条")
    parser.add_argument("--demo-person-crop-mode", choices=["source-native", "preset", "full-width"], default="source-native", help="示范视频默认保留拍摄原构图；只有人工确认后才使用裁切框")
    parser.add_argument("--demo-person-vertical-align", choices=["center", "bottom"], default="center")
    parser.add_argument(
        "--demo-background-brightness",
        default=1.0,
        type=float,
        help="示范视频背景亮度；默认 1.0（只模糊，不压暗），仅在人工确认需要时调整",
    )
    parser.add_argument("--preview-only", action="store_true", help="只生成第 16 步示范视频预览帧和 Codex 交接说明，不渲染完整资料包")
    parser.add_argument("--preview-times", default="0.8,1.5,2.5,37,92", help="示范视频预览抽帧时间点，秒")
    parser.add_argument("--music-volume", default=0.22, type=float)
    parser.add_argument("--narration-volume", default=1.0, type=float)
    parser.add_argument("--semantic-contract-spec", type=Path, help="已审核合同编译出的资料包内容选择规格")
    parser.add_argument("--project-dir", type=Path, help="required_v1 项目根目录，用于重新验证语义计划")
    parser.add_argument("--artifact-semantic-plan", type=Path, help="逐产物语义呈现计划")
    args = parser.parse_args()

    build_product_package(args)


def build_product_package(args: argparse.Namespace) -> None:
    story_name = args.story_name.strip()
    if not story_name:
        raise ValueError("--story-name 不能为空")

    story_text_path = args.story_text.expanduser()
    script_path = args.script_lines.expanduser()
    narration_path = args.narration.expanduser()
    music_path = args.music.expanduser()
    images_dir = args.images_dir.expanduser()
    bg_with_sub = args.bg_video_with_sub.expanduser()
    bg_no_sub = args.bg_video_no_sub.expanduser()
    person_path = args.person_greenscreen.expanduser()
    demo_logo = args.demo_logo.expanduser() if args.demo_logo else None
    demo_brand_spec_path = args.demo_brand_spec.expanduser() if args.demo_brand_spec else None
    annotation_skill_path = args.annotation_skill_path.expanduser()
    output_root = args.output_root.expanduser()
    work_dir = (
        args.work_dir.expanduser()
        if args.work_dir
        else Path("output") / "product_package_work" / (args.slug or sanitize_filename(story_name))
    )
    background_brightness = max(0.0, float(getattr(args, "demo_background_brightness", 1.0)))

    for label, path in (
        ("故事正文", story_text_path),
        ("逐行台词", script_path),
        ("旁白录音", narration_path),
        ("故事配乐", music_path),
        ("镜头图片目录", images_dir),
        ("含字幕背景视频", bg_with_sub),
        ("无字幕背景视频", bg_no_sub),
        ("绿幕素材", person_path),
    ):
        if not path.exists():
            raise FileNotFoundError(f"{label}不存在：{path}")
    if demo_logo is not None and not demo_logo.exists():
        raise FileNotFoundError(f"示范视频 Logo 不存在：{demo_logo}")
    validate_background_videos(bg_with_sub, bg_no_sub, allow_full_subtitle_bg=args.allow_full_subtitle_bg)
    validate_person_video_choice(person_path, allow_test=args.allow_test_greenscreen)

    ensure_dir(work_dir)
    assets_dir = work_dir / "assets"
    ensure_dir(assets_dir)

    script_lines = read_script_lines(script_path)
    public_script_lines = [clean_public_story_text(line) for line in script_lines]
    images = sorted_image_files(images_dir, slug=args.slug or None)
    if not images:
        raise ValueError(f"镜头图片目录为空：{images_dir}")
    timings = load_or_build_timings(
        args.timings_json.expanduser() if args.timings_json else None,
        script_lines,
        narration_path,
        allow_even=args.allow_even_timings,
    )
    semantic_spec = load_product_semantic_spec(args.semantic_contract_spec) if args.semantic_contract_spec else None
    semantic_plan = None
    if args.artifact_semantic_plan:
        if args.project_dir is None:
            raise ValueError("--artifact-semantic-plan requires --project-dir")
        semantic_plan = load_current_artifact_semantic_plan(args.project_dir)
    demo_brand_spec: dict[str, Any] | None = None
    if semantic_plan is not None:
        if demo_brand_spec_path is None or demo_logo is None:
            raise ValueError("required_v1 Demo 必须提供审核合同编译出的 --demo-brand-spec 和唯一官方 --demo-logo")
        demo_brand_spec, logo_args = load_demo_brand_spec(demo_brand_spec_path)
        if demo_logo.resolve() != Path(logo_args["logo_path"]).resolve():
            raise ValueError("Demo Logo 与审核合同编译规格不一致")
        if (args.demo_logo_width, args.demo_logo_x, args.demo_logo_y) != (
            logo_args["logo_width"], logo_args["logo_x"], logo_args["logo_y"]
        ):
            raise ValueError("Demo Logo 的确定性坐标/宽度与审核布局规格不一致")
    semantic_plan_selections = (
        artifact_semantic_product_selections(script_lines, semantic_plan)
        if semantic_plan is not None else None
    )

    def selected(artifact: str) -> tuple[list[str], list[Path], list[LineTiming], list[int]]:
        if semantic_plan is not None:
            key = "demo_subtitles" if artifact == "demo" else artifact
            indices = list(semantic_plan_selections[key])
        else:
            indices = semantic_line_indices(script_lines, semantic_spec, artifact) if semantic_spec else list(range(len(script_lines)))
        lines = [public_script_lines[index] for index in indices]
        selected_images = [images[index] for index in indices]
        selected_timings = [
            LineTiming(position + 1, public_script_lines[index], timings[index].source_start, timings[index].source_end,
                       timings[index].duration, timings[index].timeline_start, timings[index].timeline_end)
            for position, index in enumerate(indices)
        ]
        return lines, selected_images, selected_timings, indices

    ppt_lines, ppt_images, ppt_timings, ppt_indices = selected("ppt")
    manuscript_lines, _mi, _mt, manuscript_indices = selected("customer_manuscript")
    annotation_lines, _ai, _at, annotation_indices = selected("reading_annotation")
    demo_lines, _di, demo_timings, demo_indices = selected("demo")
    if semantic_plan is not None:
        write_json_atomic(
            work_dir / "artifact_semantic_plan_product_manifest.json",
            {
                "version": 1,
                "consumer": "product_package",
                **plan_binding(args.artifact_semantic_plan, semantic_plan),
                "source_line_count": len(script_lines),
                "selections": {
                    "ppt": ppt_indices,
                    "customer_manuscript": manuscript_indices,
                    "reading_annotation": annotation_indices,
                    "demo_subtitles": demo_indices,
                },
            },
        )
    elif semantic_spec is not None:
        write_json_atomic(
            work_dir / "product_semantic_selection_manifest.json",
            {
                "version": 1,
                "consumer": "product_package",
                **{field: str(semantic_spec[field]) for field in BINDING_FIELDS},
                "source_line_count": len(script_lines),
                "selections": {
                    "ppt": ppt_indices,
                    "customer_manuscript": manuscript_indices,
                    "reading_annotation": annotation_indices,
                    "demo": demo_indices,
                },
            },
        )
    public_srt_path = work_dir / "story_subtitles_public.srt"
    demo_srt_path = work_dir / "story_subtitles_demo.srt"
    write_srt(ppt_timings, public_srt_path)
    write_srt(demo_timings, demo_srt_path)

    story_docx = assets_dir / f"故事文稿：{story_name}.docx"
    annotation_docx = assets_dir / f"朗读标注：{story_name}.docx"
    ppt_with_sub = assets_dir / f"故事PPT：{story_name}（含字幕）.pptx"
    ppt_no_sub = assets_dir / f"故事PPT：{story_name}（无字幕）.pptx"
    demo_video = assets_dir / f"示范表演：{story_name}.mp4"
    a_only_video = assets_dir / f"A镜无人物背景视频：{story_name}.mp4"

    demo_background = args.demo_background_image.expanduser() if args.demo_background_image else None
    if demo_background is not None and not demo_background.exists():
        raise FileNotFoundError(f"示范视频背景图不存在：{demo_background}")
    story_frame_a = args.story_frame_a_image.expanduser() if args.story_frame_a_image else None
    if story_frame_a is not None and not story_frame_a.exists():
        raise FileNotFoundError(f"A 镜故事框不存在：{story_frame_a}")
    if args.keying_preset_json is None:
        preview_dir = work_dir / "keying_preview"
        generate_keying_preview(
            person_video=person_path,
            background_image=demo_background,
            output_dir=preview_dir,
            width=args.demo_width,
            height=args.demo_height,
            background_brightness=background_brightness,
        )
        if demo_background is None:
            render_background_candidate_sheet(images, work_dir / "demo_background_candidates.jpg")
            print(f"- 背景候选：{work_dir / 'demo_background_candidates.jpg'}")
        print("已生成抠像预览，首次绿幕素材需要先确认参数再渲染整段示范视频。")
        print(f"- 抠像预览：{preview_dir / '示范表演_抠像预览.jpg'}")
        print(f"- 推荐参数：{preview_dir / 'keying_preset_suggested.json'}")
        print("确认后重新运行，并传入 --keying-preset-json。")
        return

    preset_path = args.keying_preset_json.expanduser()
    if semantic_plan is not None:
        lock_issues = keying_preset_lock_issues(preset_path)
        if lock_issues:
            raise RuntimeError("抠像 preset 未绑定当前机器 QA 与独立审核，拒绝渲染 Demo：" + "；".join(lock_issues))
    preset = load_keying_preset(preset_path)
    source_greenscreen_sha256 = file_sha256(person_path)
    if demo_background is None:
        render_background_candidate_sheet(images, work_dir / "demo_background_candidates.jpg")
        raise RuntimeError(
            "示范视频必须显式传入主账号 A 镜 16:9 无框背景图 --demo-background-image，"
            f"已生成候选索引供选择：{work_dir / 'demo_background_candidates.jpg'}"
        )
    crop_bottom_ratio = max(0.0, min(0.2, args.demo_person_crop_bottom_ratio))
    if args.preview_only:
        preview_dir = work_dir / "demo_preview"
        preview_paths = render_demo_preview_frames(
            person_video=person_path,
            background_image=demo_background,
            subtitles=demo_srt_path,
            output_dir=preview_dir,
            preset=preset,
            width=args.demo_width,
            height=args.demo_height,
            crop_bottom_ratio=crop_bottom_ratio,
            crop_mode=args.demo_person_crop_mode,
            vertical_align=args.demo_person_vertical_align,
            times=parse_preview_times(args.preview_times),
            logo_path=demo_logo,
            logo_width=max(1, args.demo_logo_width),
            logo_x=max(0, args.demo_logo_x),
            logo_y=max(0, args.demo_logo_y),
            background_brightness=background_brightness,
        )
        request_path = work_dir / "朗读标注_需精修.md"
        write_annotation_request(story_name, annotation_lines, request_path, annotation_skill_path)
        handoff = work_dir / "第16步资料包_Codex前置审查.md"
        write_product_preflight_handoff(
            story_name=story_name,
            output_path=handoff,
            preview_paths=preview_paths,
            annotation_request=request_path,
            demo_background=demo_background,
            keying_preset=args.keying_preset_json.expanduser(),
            demo_logo=demo_logo,
            annotation_skill_path=annotation_skill_path,
            crop_bottom_ratio=crop_bottom_ratio,
            crop_mode=args.demo_person_crop_mode,
            vertical_align=args.demo_person_vertical_align,
        )
        if semantic_plan is not None and demo_brand_spec is not None and demo_brand_spec_path is not None:
            if file_sha256(person_path) != source_greenscreen_sha256:
                raise RuntimeError("原始绿幕素材在 Demo 预览过程中发生变化，已停止")
            write_demo_render_manifest(
                work_dir / "demo_preview_manifest.json",
                project_dir=args.project_dir,
                semantic_plan=semantic_plan,
                demo_brand_spec_path=demo_brand_spec_path,
                demo_brand_spec=demo_brand_spec,
                keying_preset_path=preset_path,
                source_greenscreen=person_path,
                output_artifacts=[*preview_paths, preview_dir / "demo_background_machine_qa.json"],
                preview=True,
            )
        print(f"已生成第 16 步前置审查材料：{handoff}")
        print(f"示范视频预览帧目录：{preview_dir}")
        print(f"朗读标注精修请求：{request_path}")
        return

    manuscript_text = (
        "\n".join(manuscript_lines)
        if semantic_spec is not None or semantic_plan is not None
        else clean_public_story_text(read_text_document(story_text_path))
    )
    render_story_docx(story_name, manuscript_text, story_docx)
    if args.annotation_docx is not None:
        annotation_source = args.annotation_docx.expanduser()
        if not annotation_source.exists():
            raise FileNotFoundError(f"朗读标注来源文档不存在：{annotation_source}")
        shutil.copy2(annotation_source, annotation_docx)
    elif args.annotation_json is not None:
        annotation_source = args.annotation_json.expanduser()
        if not annotation_source.exists():
            raise FileNotFoundError(f"朗读标注 JSON 不存在：{annotation_source}")
        blocks = load_annotation_blocks(annotation_source)
        validate_annotation_coverage(blocks, annotation_lines)
        render_annotation_blocks_docx(story_name, blocks, annotation_docx)
    elif args.allow_draft_annotation:
        print("[warning] 当前使用规则草稿生成朗读标注，仅用于内部预览；正式资料包请传 --annotation-json 或 --annotation-docx。")
        render_annotation_docx(story_name, annotation_lines, annotation_docx)
    else:
        request_path = work_dir / "朗读标注_需精修.md"
        write_annotation_request(story_name, annotation_lines, request_path, annotation_skill_path)
        raise RuntimeError(
            "已停止：朗读标注必须先精修，不能再自动套模板生成。"
            f"请按 story-performance-script.skill 精修后传入 --annotation-json 或 --annotation-docx：{request_path}"
        )
    narration_duration = probe_duration(narration_path)
    build_story_ppt(
        story_name,
        ppt_images,
        ppt_lines,
        ppt_timings,
        music_path,
        ppt_with_sub,
        with_subtitles=True,
        total_duration=narration_duration,
    )
    build_story_ppt(
        story_name,
        ppt_images,
        ppt_lines,
        ppt_timings,
        music_path,
        ppt_no_sub,
        with_subtitles=False,
        total_duration=narration_duration,
    )

    render_demo_video(
        person_video=person_path,
        background_image=demo_background,
        narration=narration_path,
        music=music_path,
        subtitles=demo_srt_path,
        output_path=demo_video,
        preset=preset,
        width=args.demo_width,
        height=args.demo_height,
        crf=args.demo_crf,
        x264_preset=args.demo_preset,
        music_volume=args.music_volume,
        narration_volume=args.narration_volume,
        crop_bottom_ratio=crop_bottom_ratio,
        crop_mode=args.demo_person_crop_mode,
        vertical_align=args.demo_person_vertical_align,
        logo_path=demo_logo,
        logo_width=max(1, args.demo_logo_width),
        logo_x=max(0, args.demo_logo_x),
        logo_y=max(0, args.demo_logo_y),
        background_brightness=background_brightness,
    )
    if file_sha256(person_path) != source_greenscreen_sha256:
        raise RuntimeError("原始绿幕素材在 Demo 渲染过程中发生变化，已停止")
    if semantic_plan is not None and demo_brand_spec is not None and demo_brand_spec_path is not None:
        write_demo_render_manifest(
            work_dir / "demo_render_manifest.json",
            project_dir=args.project_dir,
            semantic_plan=semantic_plan,
            demo_brand_spec_path=demo_brand_spec_path,
            demo_brand_spec=demo_brand_spec,
            keying_preset_path=preset_path,
            source_greenscreen=person_path,
            output_artifacts=[demo_video, demo_video.parent / "_demo_work" / "demo_background_machine_qa.json"],
            preview=False,
        )
    if story_frame_a is not None:
        render_a_only_background_video(
            bg_video=bg_no_sub,
            background_image=demo_background,
            frame_image=story_frame_a,
            output_path=a_only_video,
            keying_preset=args.keying_preset_json.expanduser(),
            width=args.demo_width,
            height=args.demo_height,
            crf=args.demo_crf,
            x264_preset=args.demo_preset,
        )

    create_package_dirs(
        story_name=story_name,
        output_root=output_root,
        story_docx=story_docx,
        music=music_path,
        annotation_docx=annotation_docx,
        demo_video=demo_video,
        background_image=demo_background,
        bg_with_sub=bg_with_sub,
        bg_no_sub=bg_no_sub,
        ppt_with_sub=ppt_with_sub,
        ppt_no_sub=ppt_no_sub,
        a_only_video=a_only_video if a_only_video.exists() else None,
    )


def load_product_semantic_spec(path: Path) -> dict:
    payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if payload.get("consumer") != "product_package" or not all(str(payload.get(field) or "") for field in BINDING_FIELDS):
        raise ValueError("资料包语义合同规格无效或缺少绑定字段")
    if not isinstance(payload.get("mappings"), list):
        raise ValueError("资料包语义合同规格缺少 mappings")
    return payload


def validate_background_videos(with_sub: Path, no_sub: Path, allow_full_subtitle_bg: bool = False) -> None:
    if with_sub.resolve() == no_sub.resolve():
        raise ValueError("含字幕和无字幕背景视频不能是同一个文件")
    for label, path in (("含字幕背景视频", with_sub), ("无字幕背景视频", no_sub)):
        duration = probe_duration(path)
        if duration <= 0:
            raise ValueError(f"{label}时长异常：{path}")
    if not allow_full_subtitle_bg:
        reject_full_subtitle_background(with_sub)


def reject_full_subtitle_background(path: Path) -> None:
    lower_name = path.name.lower()
    likely_full = lower_name in {"story_subs_bgm.mp4", "story_subtitled_silent.mp4"} or "full_sub" in lower_name
    if likely_full:
        sibling = path.with_name("story_sales_subs_bgm.mp4")
        hint = f"；检测到可能可用的正文字幕版：{sibling}" if sibling.exists() else ""
        raise ValueError(
            f"含字幕背景视频看起来是包含开头/结尾字幕的完整字幕版：{path}{hint}。"
            "资料包应传入只保留故事正文字幕的背景视频；确认无问题时可加 --allow-full-subtitle-bg 覆盖。"
        )

    srt_candidates = [path.with_suffix(".srt"), path.with_name(path.stem.replace("subs_bgm", "subtitles") + ".srt")]
    if "sales" in path.stem:
        srt_candidates.append(path.with_name("story_sales_subtitles.srt"))
    elif path.stem == "story_subs_bgm":
        srt_candidates.append(path.with_name("story_subtitles.srt"))
    for srt in srt_candidates:
        if not srt.exists():
            continue
        subtitle_lines = _read_srt_cue_texts(srt)
        if not subtitle_lines:
            continue
        semantics = classify_story(subtitle_lines)
        # Keep the shared output policy as the source of truth for which
        # semantic kinds are customer-facing.  Moral lines are intentionally
        # retained here because consumer_manuscript includes the closing
        # teaching interaction, even when the sales policy trims it.
        selected_kinds = {
            kind
            for line in lines_for_output(semantics, "background_subtitles")
            if (kind := semantics.kind_at(line.line_number)) is not None
        }
        allowed_kinds = selected_kinds | {SemanticKind.MORAL}
        blocked_kinds = {
            SemanticKind.TITLE,
            SemanticKind.HOST_INTRO,
            SemanticKind.STORY_ANNOUNCEMENT,
            SemanticKind.OUTRO,
        } | (set(SemanticKind) - allowed_kinds)
        for line in semantics.lines:
            kind = semantics.kind_at(line.line_number)
            redacted_host_intro = bool(
                re.match(r"^(?:大家好\s*[，,、]?\s*)?我是_{2,}\s*[。！？!?]?$", line.text.strip())
            )
            if kind not in blocked_kinds and not redacted_host_intro:
                continue
            semantic_label = kind.value if kind is not None else SemanticKind.HOST_INTRO.value
            raise ValueError(
                f"含字幕背景视频对应字幕文件含开头/结尾通用性风险文本：{srt}。"
                f"检测到语义区间 {semantic_label}（第 {line.line_number} 行：{line.text}）。"
                "请改用正文字幕版背景视频，或确认后加 --allow-full-subtitle-bg。"
            )


def _read_srt_cue_texts(path: Path) -> list[str]:
    """Read cue text in source order for semantic background validation."""

    text = path.read_text(encoding="utf-8-sig", errors="ignore")
    lines: list[str] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        cue_lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(cue_lines) < 3 or "-->" not in cue_lines[1]:
            continue
        cue_text = "，".join(cue_lines[2:]).strip()
        if cue_text:
            lines.append(cue_text)
    return lines


def validate_person_video_choice(path: Path, allow_test: bool) -> None:
    if allow_test:
        return
    risky_tokens = ("test", "测试", "raw", "原始")
    if not any(token in path.name.lower() for token in risky_tokens):
        return
    alternatives = [
        item
        for item in path.parent.iterdir()
        if item.is_file()
        and item.suffix.lower() in {".mp4", ".mov", ".m4v"}
        and item.resolve() != path.resolve()
        and not any(token in item.name.lower() for token in risky_tokens)
        and "发布视频" not in item.name
    ]
    if alternatives:
        choices = "\n".join(f"- {item}" for item in alternatives[:8])
        raise ValueError(
            f"当前绿幕素材文件名像测试/原始素材：{path}\n"
            "示范视频应使用已经调色、美颜、完整的绿幕视频。请确认并传入正确文件；候选如下：\n"
            f"{choices}\n如确实要用当前素材，请加 --allow-test-greenscreen。"
        )


def read_text_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8-sig").strip()
    if suffix == ".docx":
        from story_video_synthesizer.image_video import read_docx_text

        return read_docx_text(path).strip()
    raise ValueError(f"不支持的故事正文格式：{path.suffix}")


def load_or_build_timings(path: Path | None, script_lines: list[str], narration: Path, allow_even: bool = False) -> list[LineTiming]:
    if path and path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        timings: list[LineTiming] = []
        for index, item in enumerate(data, start=1):
            line = str(item.get("line") or (script_lines[index - 1] if index <= len(script_lines) else ""))
            source_start = float(item.get("source_start", item.get("start", item.get("timeline_start", 0))))
            source_end = float(item.get("source_end", item.get("end", source_start + item.get("duration", 3.0))))
            timeline_start = float(item.get("timeline_start", source_start))
            timeline_end = float(item.get("timeline_end", source_end))
            duration = float(item.get("duration", source_end - source_start))
            timings.append(LineTiming(index, line, source_start, source_end, duration, timeline_start, timeline_end))
        if len(timings) != len(script_lines):
            print(f"[warning] timings 条数 {len(timings)} 与台词行数 {len(script_lines)} 不一致，PPT 将按可用条目生成。")
        return timings
    if not allow_even:
        raise ValueError(
            "正式资料包必须传入 --timings-json，以便示范视频字幕和 PPT 翻页严格按原声速度校准。"
            "可先运行 story_workflow.py timing 生成 timings.json；仅内部预览可加 --allow-even-timings。"
        )
    print("[warning] 未传 --timings-json，正在按旁白总时长平均分配时间；该结果只适合内部预览，不应用于正式资料包。")
    return align_evenly(script_lines, narration)


def render_story_docx(story_name: str, story_text: str, output_path: Path) -> None:
    story_text = re.sub(rf"^\s*故事文稿[：:]\s*{re.escape(story_name)}\s*", "", story_text, count=1)
    story_text = re.sub(rf"^\s*{re.escape(story_name)}\s*(?:\r?\n)+", "", story_text, count=1)
    doc = Document()
    section = doc.sections[0]
    section.top_margin = Inches(0.75)
    section.bottom_margin = Inches(0.75)
    section.left_margin = Inches(0.8)
    section.right_margin = Inches(0.8)
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(f"《{story_name}》")
    set_run_font(run, DELIVERY_CJK_FONT, 22, "1A1A1A", bold=True)
    for paragraph_text in split_story_paragraphs(story_text):
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.first_line_indent = Pt(22)
        paragraph.paragraph_format.line_spacing = 1.25
        run = paragraph.add_run(paragraph_text)
        set_run_font(run, "微软雅黑", 11, "1A1A1A")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path)


@dataclass(frozen=True)
class AnnotationBlock:
    title: str
    marked_text: str
    emotion: str
    notes: list[str]


def render_annotation_docx(story_name: str, lines: list[str], output_path: Path) -> None:
    render_annotation_blocks_docx(story_name, build_annotation_blocks(lines), output_path)


def render_annotation_blocks_docx(story_name: str, blocks: list[AnnotationBlock], output_path: Path) -> None:
    validate_annotation_blocks(blocks)
    doc = Document()
    section = doc.sections[0]
    section.orientation = WD_ORIENT.PORTRAIT
    section.top_margin = Inches(0.7)
    section.bottom_margin = Inches(0.7)
    section.left_margin = Inches(0.8)
    section.right_margin = Inches(0.8)

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(f"《{story_name}》表演化批注脚本")
    set_run_font(run, "微软雅黑", 22, "1A3A5C", bold=True)
    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub_run = subtitle.add_run("适用人群：家长 · 幼师 · 儿童戏剧指导老师")
    set_run_font(sub_run, "微软雅黑", 10, "888888")

    legend = doc.add_table(rows=1, cols=1)
    prevent_table_row_split(legend.rows[0])
    legend_cell = legend.cell(0, 0)
    set_cell_shading(legend_cell, "D4E6F1")
    set_cell_border(legend_cell, "000000", 8)
    p = legend_cell.paragraphs[0]
    p.add_run("符号说明\n")
    set_run_font(p.runs[-1], "微软雅黑", 12, "1A3A5C", bold=True)
    add_markup_runs(p, "**加粗红色字体**  重读/强调词 —— 朗读时放慢加重，与肢体动作同步卡点。\n /  短停顿（约0.5秒）：句中换气或小节奏感。\n //  长停顿（1-2秒）：制造悬念，为情感爆发蓄力。")

    for index, block in enumerate(blocks, start=1):
        doc.add_paragraph("")
        table = doc.add_table(rows=3, cols=1)
        table.autofit = True
        for row in table.rows:
            prevent_table_row_split(row)
        title_cell, text_cell, note_cell = table.cell(0, 0), table.cell(1, 0), table.cell(2, 0)
        for cell in (title_cell, text_cell, note_cell):
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            set_cell_border(cell, "AED6F1", 6)
        set_cell_shading(title_cell, "D4E6F1")
        set_cell_shading(note_cell, "EAF4FB")

        p = title_cell.paragraphs[0]
        run = p.add_run(f"【段落 {index}】  {block.title}")
        set_run_font(run, "微软雅黑", 11, "1A3A5C", bold=True)

        p = text_cell.paragraphs[0]
        p.paragraph_format.line_spacing = 1.35
        add_markup_runs(p, block.marked_text)

        p = note_cell.paragraphs[0]
        p.paragraph_format.line_spacing = 1.35
        emotion_run = p.add_run("情绪：")
        set_run_font(emotion_run, "仿宋", 10, "1A3A5C", bold=True)
        note_run = p.add_run(block.emotion + "\n")
        set_run_font(note_run, "仿宋", 10, "1A3A5C")
        for note_index, note in enumerate(block.notes):
            body_run = p.add_run(note)
            set_run_font(body_run, "仿宋", 10, "1A3A5C")
            if note_index != len(block.notes) - 1:
                br = p.add_run("\n")
                set_run_font(br, "仿宋", 10, "1A3A5C")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path)


def prevent_table_row_split(row: Any) -> None:
    """Keep a single annotation row together when Word paginates the table."""

    tr_properties = row._tr.get_or_add_trPr()
    if tr_properties.find(docx_qn("w:cantSplit")) is None:
        tr_properties.append(OxmlElement("w:cantSplit"))


def load_annotation_blocks(path: Path) -> list[AnnotationBlock]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("blocks", [])
    if not isinstance(data, list):
        raise ValueError("朗读标注 JSON 必须是数组，或包含 blocks 数组")
    blocks: list[AnnotationBlock] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"朗读标注 JSON 第 {index} 项不是对象")
        title = str(item.get("title", "")).strip()
        marked_text = str(item.get("marked_text", item.get("text", ""))).strip()
        emotion = str(item.get("emotion", "")).strip()
        notes_value = item.get("notes", [])
        if isinstance(notes_value, str):
            notes = [part.strip() for part in re.split(r"\n\s*\n|\n", notes_value) if part.strip()]
        elif isinstance(notes_value, list):
            notes = [str(part).strip() for part in notes_value if str(part).strip()]
        else:
            notes = []
        if not title or not marked_text or not emotion or not notes:
            raise ValueError(f"朗读标注 JSON 第 {index} 项缺少 title/marked_text/emotion/notes")
        blocks.append(AnnotationBlock(title=title, marked_text=marked_text, emotion=emotion, notes=notes))
    validate_annotation_blocks(blocks)
    return blocks


def write_annotation_request(story_name: str, lines: list[str], output_path: Path, skill_path: Path) -> None:
    payload = "\n".join(f"{index}. {line}" for index, line in enumerate(lines, start=1))
    template = {
        "title": "段落标题",
        "marked_text": "原文，使用 **重音**、/、// 标记",
        "emotion": "情绪基调",
        "notes": ["自然段批注，必须引用本段触发词，例如「矛」或「盾」。"],
    }
    text = f"""# 《{story_name}》朗读标注精修请求

请严格按 `{skill_path}` 生成朗读标注内容。

硬性要求：
- 不要套话，不要写“开头要说清楚”“先交代人物情境”这类通用句。
- 批注像有经验的幼儿园故事老师当面说话：温和、短句、先给角色心情或画面，再给声音和动作；禁止“内容重点、节奏落点、情绪层次”等分析术语。
- 每段 2-4 句，按场景/角色/情绪转折拆分。
- marked_text 必须逐字覆盖下方已经清洁的故事台词；主持人自我介绍整句删除，除此之外不得润色、增删、改代词或改句尾。
- 红字只标真正需要重读的内容词、角色/道具首次出现、关键动作、矛盾转折、道理关键词；不得整句连红。
- 批注必须基于文本本身，写清楚为什么这样读，如何配合语气、停顿、表情或动作。
- 输出 JSON 数组，每项字段为 title、marked_text、emotion、notes。

JSON 单项格式示例：
```json
{json.dumps(template, ensure_ascii=False, indent=2)}
```

故事台词：
{payload}
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")


def build_story_ppt(
    story_name: str,
    images: list[Path],
    script_lines: list[str],
    timings: list[LineTiming],
    music_path: Path,
    output_path: Path,
    with_subtitles: bool,
    total_duration: float | None = None,
) -> None:
    prs = Presentation()
    prs.slide_width = PPT_W
    prs.slide_height = PPT_H

    if not script_lines or not timings or not images:
        raise ValueError("PPT 没有可用的图片/台词/时间轴")
    if len(images) != len(script_lines):
        raise ValueError(
            f"PPT 镜头数量不一致：图片 {len(images)} 张，原文分镜 {len(script_lines)} 行。"
            "正式资料包不能复用或吞掉镜头，请先补齐分镜图片/图生视频，或回到原文断行重新分镜。"
        )
    if len(timings) != len(script_lines):
        raise ValueError(
            f"PPT 时间轴数量不一致：timings {len(timings)} 条，原文分镜 {len(script_lines)} 行。"
            "正式资料包必须用 story_source.txt 逐行对齐 story_subtitles.srt 后生成时间轴。"
        )
    n = len(script_lines)
    slide_durations = ppt_slide_durations(timings[:n], total_duration)
    for idx in range(n):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        fill_ppt_image(slide, images[idx], prs)
        if with_subtitles:
            add_ppt_subtitle(slide, script_lines[idx], prs)
        set_ppt_advance(slide, slide_durations[idx])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(output_path)
    embed_ppt_bgm(output_path, music_path)


def ppt_slide_durations(timings: list[LineTiming], total_duration: float | None = None) -> list[float]:
    durations: list[float] = []
    for idx, timing in enumerate(timings):
        if idx + 1 < len(timings):
            end = timings[idx + 1].source_start
        elif total_duration is not None:
            end = max(float(total_duration), timing.source_end)
        else:
            end = timing.source_end
        durations.append(max(0.5, end - timing.source_start))
    return durations


def generate_keying_preview(
    person_video: Path,
    background_image: Path | None,
    output_dir: Path,
    width: int,
    height: int,
    background_brightness: float = 1.0,
) -> None:
    ensure_dir(output_dir)
    duration = probe_duration(person_video)
    timestamps = [min(duration * ratio, max(0.0, duration - 0.1)) for ratio in (0.12, 0.50, 0.82)]
    raw_frames: list[Path] = []
    for idx, ts in enumerate(timestamps, start=1):
        frame = output_dir / f"raw_frame_{idx:02d}.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{ts:.3f}", "-i", str(person_video), "-frames:v", "1", "-q:v", "2", str(frame)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        raw_frames.append(frame)

    suggested = suggest_keying_preset(person_video, raw_frames[len(raw_frames) // 2])
    (output_dir / "keying_preset_suggested.json").write_text(
        json.dumps(
            {
                "keyer": suggested.keyer,
                "chroma_color": suggested.chroma_color,
                "chroma_similarity": suggested.chroma_similarity,
                "chroma_blend": suggested.chroma_blend,
                "person_crop": suggested.person_crop,
                "person_grade": suggested.person_grade,
                "person_height_ratio": suggested.person_height_ratio,
                "bottom_margin": suggested.bottom_margin,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    keyed_frames: list[Path] = []
    for idx, frame in enumerate(raw_frames, start=1):
        out = output_dir / f"keyed_preview_{idx:02d}.png"
        render_keyed_preview_frame(
            frame,
            background_image,
            out,
            suggested,
            width,
            height,
            background_brightness=background_brightness,
        )
        keyed_frames.append(out)
    render_preview_sheet(raw_frames, keyed_frames, output_dir / "示范表演_抠像预览.jpg")


def suggest_keying_preset(person_video: Path, sample_frame: Path) -> KeyingPreset:
    image = Image.open(sample_frame).convert("RGB")
    width, height = image.size
    key_color = sample_green_color(image)
    if height >= width * 1.45:
        crop_w = width
        crop_h = int(width * 4 / 3)
        crop_x = 0
        crop_y = 0
        return KeyingPreset(
            keyer="colorkey",
            chroma_color=key_color,
            chroma_similarity=0.12,
            chroma_blend=0.015,
            person_crop=(crop_x, crop_y, crop_w, crop_h),
            person_grade="log-soft",
            person_height_ratio=1.30,
            bottom_margin=0,
        )
    return KeyingPreset(
        keyer="colorkey",
        chroma_color=key_color,
        chroma_similarity=0.14,
        chroma_blend=0.035,
        person_crop=None,
        person_grade="log-soft",
        person_height_ratio=0.96,
        bottom_margin=0,
    )


def sample_green_color(image: Image.Image) -> str:
    width, height = image.size
    points = [
        (int(width * x), int(height * y))
        for x in (0.08, 0.25, 0.50, 0.75, 0.92)
        for y in (0.08, 0.18, 0.36, 0.55)
    ]
    greens: list[tuple[int, int, int]] = []
    for x, y in points:
        r, g, b = image.getpixel((min(width - 1, x), min(height - 1, y)))
        if g > r * 1.25 and g > b * 1.12:
            greens.append((r, g, b))
    if not greens:
        return "0x00FF00"
    greens.sort(key=lambda rgb: rgb[1])
    r, g, b = greens[len(greens) // 2]
    return f"0x{r:02X}{g:02X}{b:02X}"


def render_keyed_preview_frame(
    source_frame: Path,
    background_image: Path | None,
    output_path: Path,
    preset: KeyingPreset,
    width: int,
    height: int,
    background_brightness: float = 1.0,
) -> None:
    background = output_path.parent / "preview_background.png"
    if background_image is None:
        make_neutral_background(background, width, height)
    else:
        make_blurred_background(background_image, background, width, height, brightness=background_brightness)
    crop_filter = ""
    if preset.person_crop is not None:
        x, y, w, h = preset.person_crop
        crop_filter = f"crop={w}:{h}:{x}:{y},"
    key_filter = keying_filter_chain("[1:v]", preset, crop_filter)
    native_filter, native_x, native_y = source_native_person_layout(source_frame, preset, width, height)
    filters = [
        f"[0:v]scale={width}:{height},setsar=1,format=rgba[bg]",
        key_filter,
        f"[person_keyed]{native_filter},setsar=1,format=rgba[person]",
        f"[bg][person]overlay={native_x}:{native_y}[v]",
    ]
    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(background),
            "-i",
            str(source_frame),
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[v]",
            "-frames:v",
            "1",
            str(output_path),
        ]
    )


def render_demo_video(
    person_video: Path,
    background_image: Path,
    narration: Path,
    music: Path,
    subtitles: Path,
    output_path: Path,
    preset: KeyingPreset,
    width: int,
    height: int,
    crf: int,
    x264_preset: str,
    music_volume: float,
    narration_volume: float,
    crop_bottom_ratio: float,
    crop_mode: str = "preset",
    vertical_align: str = "center",
    logo_path: Path | None = None,
    logo_width: int = 150,
    logo_x: int = 24,
    logo_y: int = 20,
    background_brightness: float = 1.0,
) -> None:
    duration = probe_duration(narration)
    work_dir = output_path.parent / "_demo_work"
    ensure_dir(work_dir)
    background = work_dir / "demo_background.png"
    make_blurred_background(background_image, background, width, height, brightness=background_brightness)
    validate_demo_blurred_background(background)
    subtitle_overlay = work_dir / "demo_subtitle_overlay.mov"
    render_subtitle_overlay(subtitles, subtitle_overlay, duration, width, height)

    crop_filter = demo_crop_filter(person_video, preset, crop_bottom_ratio, crop_mode)
    key_filter = keying_filter_chain("[1:v]", preset, crop_filter)
    if preserve_native_composition(preset, crop_mode):
        person_filter, person_x, person_y = source_native_person_layout(person_video, preset, width, height)
    else:
        demo_person_height = min(int(height * preset.person_height_ratio), height)
        person_filter = f"scale=-1:{demo_person_height}"
        person_x = "(W-w)/2"
        person_y = "H-h" if vertical_align == "bottom" else "(H-h)/2"
    filters = [
        f"[0:v]scale={width}:{height},setsar=1,format=rgba[bg]",
        key_filter,
        f"[person_keyed]{person_filter},setsar=1,format=rgba[person]",
        f"[bg][person]overlay={person_x}:{person_y}[withperson]",
        "[2:v]format=rgba[subtitles]",
        "[withperson][subtitles]overlay=0:0[v]",
        f"[3:a]volume={narration_volume:.3f},atrim=0:{duration:.3f},asetpts=PTS-STARTPTS[voice]",
        f"[4:a]volume={music_volume:.3f},atrim=0:{duration:.3f},apad=whole_dur={duration:.3f},asetpts=PTS-STARTPTS[music]",
        "[voice][music]amix=inputs=2:duration=first:dropout_transition=0,alimiter=limit=0.94[a]",
    ]
    command = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        str(background),
        "-stream_loop",
        "-1",
        "-i",
        str(person_video),
        "-i",
        str(subtitle_overlay),
        "-i",
        str(narration),
        "-i",
        str(music),
    ]
    video_label = "v"
    if logo_path is not None:
        command.extend(["-loop", "1", "-i", str(logo_path)])
        filters.append(f"[5:v]scale={logo_width}:-1,format=rgba[demo_logo]")
        filters.append(f"[v][demo_logo]overlay={logo_x}:{logo_y}[vlogo]")
        video_label = "vlogo"
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            f"[{video_label}]",
            "-map",
            "[a]",
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            x264_preset,
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    run_command(command)


def render_demo_preview_frames(
    person_video: Path,
    background_image: Path,
    subtitles: Path,
    output_dir: Path,
    preset: KeyingPreset,
    width: int,
    height: int,
    crop_bottom_ratio: float,
    crop_mode: str,
    vertical_align: str,
    times: list[float],
    logo_path: Path | None,
    logo_width: int,
    logo_x: int,
    logo_y: int,
    background_brightness: float = 1.0,
) -> list[Path]:
    ensure_dir(output_dir)
    duration = probe_duration(person_video)
    background = output_dir / "demo_background.png"
    make_blurred_background(background_image, background, width, height, brightness=background_brightness)
    validate_demo_blurred_background(background)
    subtitle_overlay = output_dir / "demo_subtitle_overlay.mov"
    render_subtitle_overlay(subtitles, subtitle_overlay, duration, width, height)
    output_paths: list[Path] = []
    for index, ts in enumerate(times, start=1):
        timestamp = min(max(0.0, ts), max(0.0, duration - 0.1))
        output_path = output_dir / f"demo_preview_{index:02d}_{timestamp:.1f}s.png"
        render_demo_preview_frame(
            person_video,
            background,
            subtitle_overlay,
            output_path,
            preset,
            width,
            height,
            crop_bottom_ratio,
            crop_mode,
            vertical_align,
            timestamp,
            logo_path,
            logo_width,
            logo_x,
            logo_y,
        )
        output_paths.append(output_path)
    return output_paths


def render_demo_preview_frame(
    person_video: Path,
    background: Path,
    subtitle_overlay: Path,
    output_path: Path,
    preset: KeyingPreset,
    width: int,
    height: int,
    crop_bottom_ratio: float,
    crop_mode: str,
    vertical_align: str,
    timestamp: float,
    logo_path: Path | None,
    logo_width: int,
    logo_x: int,
    logo_y: int,
    background_brightness: float = 1.0,
) -> None:
    crop_filter = demo_crop_filter(person_video, preset, crop_bottom_ratio, crop_mode)
    # Input-level seeking keeps source timestamps on some QuickTime files.  The
    # single-frame background starts at PTS 0, so framesync can otherwise emit
    # only the background while silently dropping the sought person frame.
    # Normalise both sought video inputs before keying/overlaying them.
    key_filter = keying_filter_chain("[person_source]", preset, crop_filter)
    if preserve_native_composition(preset, crop_mode):
        person_filter, person_x, person_y = source_native_person_layout(person_video, preset, width, height)
    else:
        demo_person_height = min(int(height * preset.person_height_ratio), height)
        person_filter = f"scale=-1:{demo_person_height}"
        person_x = "(W-w)/2"
        person_y = "H-h" if vertical_align == "bottom" else "(H-h)/2"
    filters = [
        f"[0:v]scale={width}:{height},setsar=1,format=rgba[bg]",
        "[1:v]setpts=PTS-STARTPTS[person_source]",
        key_filter,
        f"[person_keyed]{person_filter},setsar=1,format=rgba[person]",
        f"[bg][person]overlay={person_x}:{person_y}[withperson]",
        "[2:v]setpts=PTS-STARTPTS,format=rgba[subtitles]",
        "[withperson][subtitles]overlay=0:0[v]",
    ]
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(background),
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(person_video),
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(subtitle_overlay),
    ]
    video_label = "v"
    if logo_path is not None:
        command.extend(["-loop", "1", "-i", str(logo_path)])
        filters.append(f"[3:v]scale={logo_width}:-1,format=rgba[demo_logo]")
        filters.append(f"[v][demo_logo]overlay={logo_x}:{logo_y}[vlogo]")
        video_label = "vlogo"
    command.extend(["-filter_complex", ";".join(filters), "-map", f"[{video_label}]", "-frames:v", "1", str(output_path)])
    run_command(command)


def demo_crop_filter(person_video: Path, preset: KeyingPreset, crop_bottom_ratio: float, crop_mode: str) -> str:
    if crop_mode == "source-native":
        return ""
    if preset.person_crop is None:
        return ""
    x, y, w, h = preset.person_crop
    if crop_mode == "full-width":
        video_width, video_height = probe_video_size(person_video)
        x = 0
        w = video_width
        h = min(h, max(1, video_height - y))
    if crop_bottom_ratio > 0:
        h = max(1, int(round(h * (1 - crop_bottom_ratio))))
    return f"crop={w}:{h}:{x}:{y},"


def preserve_native_composition(preset: KeyingPreset, crop_mode: str) -> bool:
    """Whether demo rendering should keep the source frame scale and placement.

    A crop mode without an actual human-confirmed crop is equivalent to the
    default native mode.  This guard prevents ``full-width`` (or legacy
    ``preset``) plus a null crop from falling through to a second, arbitrary
    person-height scale such as the historical 0.84 multiplier.
    """
    return crop_mode == "source-native" or preset.person_crop is None


def compute_source_native_layout(
    source_size: tuple[int, int],
    output_size: tuple[int, int],
    detected_person_bbox: tuple[int, int, int, int] | None = None,
) -> tuple[str, int, int]:
    """Return the ffmpeg transform and overlay position for native composition.

    The calculation is deliberately pure: callers can probe media dimensions
    outside this function and tests can verify that a 16:9 source rendered to a
    16:9 canvas stays at 1.0 scale.  ``detected_person_bbox`` only trims the
    transparent green-screen perimeter; it does *not* resize the performer
    relative to the original frame.
    """
    source_width, source_height = (int(source_size[0]), int(source_size[1]))
    output_width, output_height = (int(output_size[0]), int(output_size[1]))
    if source_width <= 0 or source_height <= 0:
        raise ValueError("source_size 必须是正数宽高")
    if output_width <= 0 or output_height <= 0:
        raise ValueError("output_size 必须是正数宽高")
    scale = min(output_width / source_width, output_height / source_height)
    rendered_width = max(1, round(source_width * scale))
    rendered_height = max(1, round(source_height * scale))
    canvas_x = round((output_width - rendered_width) / 2)
    canvas_y = round((output_height - rendered_height) / 2)
    if detected_person_bbox is None:
        return f"scale={rendered_width}:{rendered_height}", canvas_x, canvas_y

    x, y, width, height = (int(value) for value in detected_person_bbox)
    x = max(0, min(source_width - 1, x))
    y = max(0, min(source_height - 1, y))
    width = max(1, min(source_width - x, width))
    height = max(1, min(source_height - y, height))
    target_width = max(1, round(width * scale))
    target_height = max(1, round(height * scale))
    return (
        f"crop={width}:{height}:{x}:{y},scale={target_width}:{target_height}",
        canvas_x + round(x * scale),
        canvas_y + round(y * scale),
    )


def source_native_person_layout(
    person_media: Path,
    preset: KeyingPreset,
    output_width: int,
    output_height: int,
) -> tuple[str, int, int]:
    """Preserve source composition while excluding green-screen edge bands."""
    source_size = probe_video_size(person_media)
    return compute_source_native_layout(
        source_size,
        (output_width, output_height),
        preset.detected_person_bbox,
    )


def probe_video_size(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    width_text, height_text = result.stdout.strip().split("x", 1)
    return int(width_text), int(height_text)


def parse_preview_times(value: str) -> list[float]:
    times: list[float] = []
    for part in value.replace("，", ",").split(","):
        part = part.strip()
        if part:
            times.append(float(part))
    return times or [1.0, 2.0, 37.0, 92.0]


def write_product_preflight_handoff(
    story_name: str,
    output_path: Path,
    preview_paths: list[Path],
    annotation_request: Path,
    demo_background: Path,
    keying_preset: Path,
    demo_logo: Path | None,
    annotation_skill_path: Path,
    crop_bottom_ratio: float,
    crop_mode: str,
    vertical_align: str,
) -> None:
    preview_text = "\n".join(f"- {path}" for path in preview_paths)
    text = f"""# 第 16 步资料包 Codex 前置审查：{story_name}

本步骤只生成需要智能判断的材料，不渲染完整资料包。

## 需要 Codex 判断的部分

1. 示范视频：请查看下列预览帧，判断人物手部、底部边缘、字幕、左上角 Logo 是否协调。
2. 朗读标注：请按 `{annotation_skill_path}` 从头精修，输出 `annotation.json` 或最终 `.docx`。

## 本地直接生成的部分

故事文稿、PPT、背景视频、配乐复制和资料包目录结构不需要智能判断。等示范视频参数与朗读标注确认后，再运行正式资料包生成命令即可。

字幕口径：
- 示范视频使用原始台词字幕，保留“我是绵羊姐姐”。
- PPT、故事文稿、朗读标注等对外售卖资料使用脱敏文本，不能出现“绵羊姐姐”。

## 示范视频预览帧

{preview_text}

当前参数：
- 背景图：{demo_background}
- 抠像参数：{keying_preset}
- Logo：{demo_logo or "未传入"}
- crop_mode：{crop_mode}
- crop_bottom_ratio：{crop_bottom_ratio:.3f}
- vertical_align：{vertical_align}

审查重点：
- 开场和手势最大帧，两只手必须完整，不要被左右裁掉。
- 人物底部不能露出横线，也不要和画面底边形成奇怪空隙。
- Logo 放在左上角安全区，不压人物和字幕。
- 字幕正常、不过长、不挤出画面。

## 朗读标注请求

{annotation_request}

正式生成资料包时，必须传入精修后的 `--annotation-json` 或 `--annotation-docx`。不要再使用自动草稿标注。

如果需要调整示范视频参数，请在同目录写入 `demo_params.json`，工作台第 17 步会自动读取。示例：

```json
{{
  "demo_person_crop_mode": "full-width",
  "demo_person_vertical_align": "bottom",
  "demo_person_crop_bottom_ratio": 0.055
}}
```
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")


def keying_filter_chain(source: str, preset: KeyingPreset, crop_filter: str = "") -> str:
    grade = person_grade_filter(preset.person_grade)
    beauty = person_beauty_filter(preset.person_beauty)
    beauty_chain = f"{beauty}," if beauty else ""
    if preset.keyer == "chromakey":
        return (
            f"{source}{crop_filter}{beauty_chain}chromakey={preset.chroma_color}:{preset.chroma_similarity}:{preset.chroma_blend},"
            f"format=rgba{grade}[person_keyed]"
        )
    return (
        f"{source}{crop_filter}{beauty_chain}format=rgba,split[person_orig][person_keysrc];"
        f"[person_keysrc]colorkey={preset.chroma_color}:{preset.chroma_similarity}:{preset.chroma_blend},"
        "alphaextract,erosion,dilation[person_mask];"
        f"[person_orig][person_mask]alphamerge,despill=type=green:mix=0.35{grade}[person_keyed]"
    )


def person_grade_filter(value: str) -> str:
    if value == "natural":
        return ",eq=contrast=1.05:saturation=1.07:brightness=0.01:gamma=0.99"
    if value == "log-soft":
        return ",eq=contrast=1.18:saturation=1.25:brightness=0.03:gamma=0.96"
    if value == "log-strong":
        return ",eq=contrast=1.30:saturation=1.35:brightness=0.04:gamma=0.92"
    return ""


def person_beauty_filter(value: str) -> str:
    if value == "light":
        return "hqdn3d=1.2:1.0:3.0:2.0,unsharp=5:5:0.18:5:5:0.0"
    return ""


def render_subtitle_overlay(srt_path: Path, output_path: Path, duration: float, width: int, height: int) -> None:
    from release_video import render_subtitle_overlay_video

    render_subtitle_overlay_video(
        srt_path=srt_path,
        output_path=output_path,
        duration=duration,
        width=width,
        height=height,
        font_size=max(34, height // 24),
        margin_v=max(48, height // 18),
        stroke_width=max(3, height // 270),
        fps=25,
    )


def make_blurred_background(
    source: Path,
    output: Path,
    width: int,
    height: int,
    *,
    brightness: float = 1.0,
    contrast: float = 1.0,
) -> None:
    """Prepare a blurred background without silently changing its exposure.

    ``brightness`` and ``contrast`` remain explicit knobs for a deliberate
    visual decision, but both default to 1.0 so blur alone does not darken a
    customer's selected background plate.
    """
    image = Image.open(source).convert("RGB")
    image = crop_image_to_ratio(image, width / height).resize((width, height), Image.Resampling.LANCZOS)
    image = image.filter(ImageFilter.GaussianBlur(radius=12))
    if brightness != 1.0:
        image = ImageEnhance.Brightness(image).enhance(max(0.0, float(brightness)))
    if contrast != 1.0:
        image = ImageEnhance.Contrast(image).enhance(max(0.0, float(contrast)))
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)


def validate_demo_blurred_background(path: Path) -> Path:
    with Image.open(path) as image:
        issues = blurred_background_issues(image)
    report = path.with_name("demo_background_machine_qa.json")
    write_json_atomic(
        report,
        {
            "version": 1,
            "background_path": str(path),
            "background_sha256": file_sha256(path),
            "passed": not issues,
            "critical_errors": issues,
            "review_note": "机器检测仅阻断明显低频矩形拼接/亮度色块；最终融合感仍由独立视觉审核判断。",
        },
    )
    if issues:
        raise RuntimeError("Demo 模糊背景存在明显矩形拼接异常：" + "；".join(issues))
    return report


def make_neutral_background(output: Path, width: int, height: int) -> None:
    image = Image.new("RGB", (width, height), (178, 184, 190))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        value = int(150 + 34 * (1 - y / max(1, height)))
        draw.line((0, y, width, y), fill=(value, value + 4, value + 8))
    image = image.filter(ImageFilter.GaussianBlur(radius=8))
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)


def render_background_candidate_sheet(images: list[Path], output_path: Path) -> None:
    thumbs = images[:24]
    tile_w, tile_h = 320, 180
    cols = 4
    padding = 16
    label_h = 32
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new(
        "RGB",
        (padding + cols * (tile_w + padding), padding + rows * (tile_h + label_h + padding)),
        (246, 244, 238),
    )
    draw = ImageDraw.Draw(sheet)
    font = load_font(20)
    for idx, path in enumerate(thumbs, start=1):
        row = (idx - 1) // cols
        col = (idx - 1) % cols
        x = padding + col * (tile_w + padding)
        y = padding + row * (tile_h + label_h + padding)
        image = Image.open(path).convert("RGB")
        image.thumbnail((tile_w, tile_h))
        tile = Image.new("RGB", (tile_w, tile_h), (220, 218, 210))
        tile.paste(image, ((tile_w - image.width) // 2, (tile_h - image.height) // 2))
        sheet.paste(tile, (x, y))
        draw.text((x, y + tile_h + 6), f"{idx:02d}  {path.name}", fill=(32, 32, 32), font=font)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, quality=92)


def render_a_only_background_video(
    bg_video: Path,
    background_image: Path,
    frame_image: Path,
    output_path: Path,
    keying_preset: Path,
    width: int,
    height: int,
    crf: int,
    x264_preset: str,
) -> None:
    """Render the main-account A-shot layout without host, subtitles, or logos.

    This is intentionally A-shot only for customer compositing: it keeps the
    blurred background plate and story window for the whole video, leaving the
    host side empty so buyers can add their own presenter later.
    """
    from release_video import (
        STORY_BOX_H,
        STORY_BOX_W,
        STORY_BOX_X,
        STORY_BOX_Y,
        WIDE_HEIGHT,
        WIDE_WIDTH,
        format_preset_box,
        parse_required_box,
        prepare_story_frame_assets,
    )

    data = json.loads(keying_preset.read_text(encoding="utf-8"))
    story_box_value = format_preset_box(data["story_box"]) if data.get("story_box") is not None else f"{STORY_BOX_X},{STORY_BOX_Y},{STORY_BOX_W},{STORY_BOX_H}"
    story_box = parse_required_box(story_box_value, "--story-box", f"{STORY_BOX_X},{STORY_BOX_Y},{STORY_BOX_W},{STORY_BOX_H}")
    story_bleed = max(0, int(float(data.get("story_bleed", 0))))
    background_blur = max(0, int(float(data.get("background_blur", 14))))
    duration = probe_duration(bg_video)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame_prepared, mask_path, story_bbox = prepare_story_frame_assets(
        frame_image,
        story_box,
        output_path.parent / "a_only_story_frame_prepared.png",
        output_path.parent / "a_only_story_mask.png",
        story_bleed,
    )
    story_x, story_y, story_width, story_height = story_bbox
    filters = [
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,format=rgba[base_src]",
    ]
    if background_blur > 0:
        filters.append(f"[base_src]boxblur={background_blur}:1[base]")
    else:
        filters.append("[base_src]null[base]")
    filters.extend(
        [
            f"[1:v]scale={story_width}:{story_height}:force_original_aspect_ratio=increase,crop={story_width}:{story_height},setsar=1,format=rgba[story_rect]",
            f"color=c=0x000000@0.0:s={WIDE_WIDTH}x{WIDE_HEIGHT}:d={duration:.3f},format=rgba[story_canvas]",
            f"[story_canvas][story_rect]overlay={story_x}:{story_y}[story_layer]",
            f"[3:v]scale={WIDE_WIDTH}:{WIDE_HEIGHT},format=gray[story_mask]",
            "[story_layer][story_mask]alphamerge[story_masked]",
            "[base][story_masked]overlay=0:0[withstory]",
            f"[2:v]scale={WIDE_WIDTH}:{WIDE_HEIGHT},setsar=1,format=rgba[frame]",
            "[withstory][frame]overlay=0:0,format=yuv420p[v]",
        ]
    )
    run_command(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(background_image),
            "-stream_loop",
            "-1",
            "-i",
            str(bg_video),
            "-loop",
            "1",
            "-i",
            str(frame_prepared),
            "-loop",
            "1",
            "-i",
            str(mask_path),
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[v]",
            "-map",
            "1:a?",
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            str(crf),
            "-preset",
            x264_preset,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


def create_package_dirs(
    story_name: str,
    output_root: Path,
    story_docx: Path,
    music: Path,
    annotation_docx: Path,
    demo_video: Path,
    background_image: Path,
    bg_with_sub: Path,
    bg_no_sub: Path,
    ppt_with_sub: Path,
    ppt_no_sub: Path,
    a_only_video: Path | None = None,
) -> None:
    base_dir = output_root / f"绵羊故事锦囊：{story_name}（基础版）"
    advanced_dir = output_root / f"绵羊故事锦囊：{story_name}（进阶版）"
    for directory in (base_dir, advanced_dir):
        if directory.exists():
            backup = directory.with_name(f"{directory.name}_旧版_{time.strftime('%Y%m%d_%H%M%S')}")
            shutil.move(str(directory), str(backup))
        directory.mkdir(parents=True, exist_ok=True)

    base_items = [
        (story_docx, f"故事文稿：{story_name}.docx"),
        (music, f"故事配乐：{story_name}{music.suffix.lower()}"),
        (annotation_docx, f"朗读标注：{story_name}.docx"),
        (demo_video, f"示范表演：{story_name}.mp4"),
        (background_image, f"背景图片：{story_name}{background_image.suffix.lower()}"),
    ]
    advanced_items = base_items + [
        (bg_with_sub, f"背景视频：{story_name}（含字幕）.mp4"),
        (bg_no_sub, f"背景视频：{story_name}（无字幕）.mp4"),
        (ppt_with_sub, f"故事PPT：{story_name}（含字幕）.pptx"),
        (ppt_no_sub, f"故事PPT：{story_name}（无字幕）.pptx"),
    ]
    if a_only_video is not None:
        advanced_items.append((a_only_video, f"A镜无人物背景视频：{story_name}.mp4"))
    for source, filename in base_items:
        shutil.copy2(source, base_dir / filename)
    for source, filename in advanced_items:
        shutil.copy2(source, advanced_dir / filename)
    print(f"已生成基础版：{base_dir}")
    print(f"已生成进阶版：{advanced_dir}")


def load_keying_preset(path: Path) -> KeyingPreset:
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_keying_preset_selection(path, data)
    crop = data.get("person_crop")
    if isinstance(crop, str) and crop.strip():
        crop = tuple(int(float(part.strip())) for part in crop.replace("，", ",").split(","))
    elif isinstance(crop, list):
        crop = tuple(int(value) for value in crop)
    else:
        crop = None
    if crop is not None and len(crop) != 4:
        raise ValueError("person_crop 必须是 x,y,w,h")
    detected_bbox = data.get("detected_person_bbox")
    if isinstance(detected_bbox, str) and detected_bbox.strip():
        detected_bbox = tuple(int(float(part.strip())) for part in detected_bbox.replace("，", ",").split(","))
    elif isinstance(detected_bbox, list):
        detected_bbox = tuple(int(value) for value in detected_bbox)
    else:
        detected_bbox = None
    if detected_bbox is not None and len(detected_bbox) != 4:
        raise ValueError("detected_person_bbox 必须是 x,y,w,h")
    keyer = str(data.get("keyer", "colorkey"))
    if keyer not in {"colorkey", "chromakey"}:
        raise ValueError("keyer 只能是 colorkey 或 chromakey")
    return KeyingPreset(
        keyer=keyer,
        chroma_color=str(data.get("chroma_color", "0x00FF00")),
        chroma_similarity=float(data.get("chroma_similarity", 0.10)),
        chroma_blend=float(data.get("chroma_blend", 0.0)),
        person_crop=crop,  # type: ignore[arg-type]
        detected_person_bbox=detected_bbox,  # type: ignore[arg-type]
        person_grade=str(data.get("person_grade", "none")),
        person_beauty=str(data.get("person_beauty", "light")),
        person_height_ratio=float(data.get("person_height_ratio", 0.96)),
        person_x=int(round(float(data["person_x"]))) if data.get("person_x") is not None else None,
        person_y=int(round(float(data["person_y"]))) if data.get("person_y") is not None else None,
        bottom_margin=int(data.get("bottom_margin", 0)),
    )


def validate_keying_preset_selection(preset_path: Path, data: dict[str, Any]) -> None:
    """Require a declared keying candidate to be present in its search record."""
    selected = data.get("keying_candidate")
    if not selected:
        return
    search_ref = data.get("keying_search")
    if not search_ref:
        raise ValueError(
            f"抠像预设声明了 keying_candidate={selected!r}，但缺少 keying_search 证据路径：{preset_path}"
        )
    search_path = Path(str(search_ref)).expanduser()
    if not search_path.is_absolute():
        relative_to_preset = (preset_path.parent / search_path).resolve()
        relative_to_cwd = search_path.resolve()
        search_path = relative_to_preset if relative_to_preset.exists() else relative_to_cwd
    if not search_path.exists():
        raise ValueError(f"抠像搜索证据不存在：{search_path}")
    try:
        payload = json.loads(search_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"抠像搜索证据无法读取：{search_path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"抠像搜索证据必须是 JSON 对象：{search_path}")
    candidate_ids = {
        str(item.get("id"))
        for item in payload.get("candidates", [])
        if isinstance(item, dict) and item.get("id")
    }
    if str(selected) not in candidate_ids:
        raise ValueError(
            f"keying_preset 选择了未出现在 keying_search candidates 中的候选：{selected!r}；"
            f"证据文件：{search_path}"
        )


def split_story_paragraphs(text: str) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n|\n", text) if part.strip()]
    if paragraphs:
        return paragraphs
    return [text.strip()] if text.strip() else []


def clean_public_story_text(text: str) -> str:
    # 对外文稿和朗读标注都不保留主持人自我介绍。过去这里只把姓名
    # 替换成下划线，导致独立审核要求“整句删除”而打包器仍要求逐字
    # 覆盖占位句，形成无法通过的互斥门槛。
    text = re.sub(
        r"(?:大家好\s*[，,。！？!?]?\s*)?我是\s*绵羊姐姐(?:姐姐)?\s*[。！？!?]?\s*",
        "",
        text,
    )
    text = text.replace("绵羊姐姐", "____")
    return text.strip()


def build_annotation_blocks(lines: list[str]) -> list[AnnotationBlock]:
    groups = group_annotation_lines(lines)
    blocks: list[AnnotationBlock] = []
    for index, group in enumerate(groups, start=1):
        text = " ".join(group)
        marked_text, keywords = mark_text_for_performance(text)
        blocks.append(
            AnnotationBlock(
                title=infer_segment_title(text, index),
                marked_text=marked_text,
                emotion=infer_emotion(text),
                notes=build_annotation_notes(text, keywords),
            )
        )
    return blocks


def group_annotation_lines(lines: list[str]) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        target_max = 92 if current else 120
        should_flush = current and (current_len + len(stripped) > target_max or len(current) >= 3)
        if should_flush:
            groups.append(current)
            current = []
            current_len = 0
        current.append(stripped)
        current_len += len(stripped)
        if stripped.endswith(("！", "？", "。")) and current_len >= 70:
            groups.append(current)
            current = []
            current_len = 0
    if current:
        groups.append(current)
    return groups


def mark_text_for_performance(text: str) -> tuple[str, list[str]]:
    pieces = [part for part in re.split(r"([，。！？；：、,.!?;:])", text) if part]
    result: list[str] = []
    all_keywords: list[str] = []
    for part in pieces:
        if re.fullmatch(r"[，。！？；：、,.!?;:]", part):
            result.append(part)
            result.append(" // " if part in "。！？!?" else " / ")
            continue
        marked, keywords = mark_clause(part)
        result.append(marked)
        all_keywords.extend(keywords)
    marked_text = "".join(result)
    return re.sub(r"\s+([，。！？；：、,.!?;:])", r"\1", marked_text).strip(" /"), unique_preserve_order(all_keywords)


def mark_clause(clause: str) -> tuple[str, list[str]]:
    candidates = performance_keyword_candidates(clause)
    selected: list[str] = []
    selected_spans: list[tuple[int, int]] = []
    clean_length = len(re.sub(r"\s+", "", clause))
    max_phrases = 2 if clean_length < 12 else (4 if clean_length < 30 else 6)
    red_budget = max(4, min(18, round(clean_length * 0.32)))
    for candidate in candidates:
        if len(selected) >= max_phrases:
            break
        pos = clause.find(candidate)
        if pos < 0:
            continue
        span = (pos, pos + len(candidate))
        if any(not (span[1] + 1 < old[0] or span[0] > old[1] + 1) for old in selected_spans):
            continue
        if sum(len(item) for item in selected) + len(candidate) > red_budget:
            continue
        if any(candidate in item or item in candidate for item in selected):
            continue
        selected.append(candidate)
        selected_spans.append(span)
    marked = clause
    for keyword in selected:
        marked = re.sub(re.escape(keyword), f"**{keyword}**", marked, count=1)
    return marked, selected


def performance_keyword_candidates(clause: str) -> list[str]:
    clean = re.sub(r"\s+", "", clause)
    candidates: list[str] = []
    # 重音应跟随动作、反差、情绪和叙事转折，而不是绑定某几个样例故事。
    generic_beats = (
        "凶猛", "横行霸道", "害怕", "勇敢", "聪明", "生气", "得意", "惊讶", "害羞", "紧张", "委屈",
        "鼓起勇气", "硬着头皮", "毫不畏惧", "主动帮助", "连连点头", "答应", "投降", "再也不",
        "扬起", "举起", "放下", "拿起", "踩下去", "躲开", "爬上", "爬下", "冲过去", "逃走",
        "狠狠地", "悄悄地", "飞快地", "灵活地", "突然", "忽然", "一下子", "嗖的一下",
        "根本", "特别", "非常", "一定", "真正", "最", "没有", "不能", "不会", "却", "竟然",
        "看不见", "找不到", "记住了", "改掉了", "欺负", "帮助", "好痛", "好痒",
        "啊呜", "哎哟", "哎呀", "天哪", "哈哈大笑", "哭笑不得", "得意洋洋", "灰溜溜",
    )
    candidates.extend(term for term in generic_beats if term in clean)
    candidates.extend(re.findall(r"[一二三四五六七八九十百千万两0-9]+[个名只把件句秒年天]?", clean))
    candidates.extend(re.findall(r"[\u4e00-\u9fff]{1,4}(?:大象|蚂蚁|狐狸|乌鸦|老虎|狮子|小朋友|先生|大王|将军|孩子|老人)", clean))
    candidates.extend(re.findall(r"(?:根本|特别|非常|一定|真正|最|没有|不能|不会|却|竟然|突然|忽然)[\u4e00-\u9fff]{0,4}", clean))
    candidates.extend(re.findall(r"[\u4e00-\u9fff]{0,4}(?:举起|放下|拿起|扬起|踩下|躲开|爬上|爬下|冲去|逃走|愣住|大笑|投降|答应|帮助|欺负)", clean))
    candidates.extend(re.findall(r"[\u4e00-\u9fff]{2,6}(?:热热闹闹|气势十足|摇头晃脑|结结巴巴)", clean))
    stopwords = {
        "大家好",
        "今天这个",
        "这个故事",
        "生活里",
        "请听",
        "接着",
        "这时",
        "他说",
        "我是",
    }
    single_important: set[str] = set()
    filtered = [
        item
        for item in candidates
        if item
        and len(item) <= 6
        and (len(item) > 1 or item in single_important)
        and item not in stopwords
        and item not in {"一", "一个"}
        and not item.startswith("我是")
    ]
    return sorted(unique_preserve_order(filtered), key=lambda item: keyword_priority(item), reverse=True)


def keyword_priority(keyword: str) -> tuple[int, int]:
    priority = 0
    if any(token in keyword for token in ("大象", "蚂蚁", "狐狸", "乌鸦", "老虎", "狮子", "先生", "大王", "将军", "孩子")):
        priority += 4
    if any(token in keyword for token in ("根本", "最", "没有", "不会", "不能", "却", "一定", "真正", "再也不")):
        priority += 4
    if any(token in keyword for token in ("扬起", "举起", "踩", "躲", "爬", "愣住", "逃走", "大笑", "投降", "帮助", "欺负")):
        priority += 3
    if any(token in keyword for token in ("勇敢", "聪明", "害怕", "生气", "惊讶", "得意", "好痛", "好痒", "啊呜", "哎哟")):
        priority += 3
    if re.search(r"[一二三四五六七八九十百千万两0-9]", keyword):
        priority += 2
    return priority, min(len(keyword), 6)


def unique_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def validate_annotation_blocks(blocks: list[AnnotationBlock]) -> None:
    generic_bad = (
        "开头要说清楚",
        "先把人物和情境交代稳",
        "内容重点",
        "节奏落点",
        "情绪层次",
        "完成收束",
    )
    for index, block in enumerate(blocks, start=1):
        plain = re.sub(r"[* /\n]", "", block.marked_text)
        emphasized = "".join(re.findall(r"\*\*(.*?)\*\*", block.marked_text))
        if plain and len(emphasized) / len(plain) > 0.36:
            raise ValueError(f"朗读标注第 {index} 段重音过密，请减少红字。")
        groups = re.findall(r"\*\*(.*?)\*\*", block.marked_text)
        if len(plain) >= 60 and (len(groups) < 5 or len(emphasized) / len(plain) < 0.08):
            raise ValueError(f"朗读标注第 {index} 段重音不足；请补齐动作、反差、情绪和转折重音。")
        if any(len(item) > 8 for item in groups):
            raise ValueError(f"朗读标注第 {index} 段存在过长连续重音。")
        notes_text = "\n".join(block.notes)
        if any(bad in notes_text for bad in generic_bad):
            raise ValueError(f"朗读标注第 {index} 段解析出现套话，请改写为基于文本的具体指导。")


def validate_annotation_coverage(blocks: list[AnnotationBlock], source_lines: list[str]) -> None:
    def normalized(value: str) -> str:
        return re.sub(r"\s+", "", value.replace("**", "").replace("/", ""))

    expected = normalized("".join(source_lines))
    actual = normalized("".join(block.marked_text for block in blocks))
    if actual != expected:
        raise ValueError("朗读标注 marked_text 未逐字覆盖客户正文，存在增删、改写、代词或句尾差异。")


def infer_segment_title(line: str, index: int) -> str:
    clean = re.sub(r"[，。！？；：、,.!?;:\"“”‘’《》]", " ", line).strip()
    title = clean[:10].strip() or f"情节 {index}"
    return title


def infer_emotion(line: str) -> str:
    if any(char in line for char in "！？!?"):
        return "情绪鲜明，语气有起伏"
    if any(word in line for word in ("悄悄", "忽然", "突然", "没想到")):
        return "带一点悬念，节奏先收后放"
    if any(word in line for word in ("高兴", "开心", "笑")):
        return "明亮轻快，带笑意"
    return "叙事平稳，清楚推进情节"


def build_annotation_notes(text: str, keywords: list[str]) -> list[str]:
    focus = keywords[:4] or [infer_segment_title(text, 1)]
    notes: list[str] = []
    first = focus[0]
    if any(token in first for token in ("王", "先生", "人", "队伍", "乐师", "小朋友")):
        notes.append(
            f"「{first}」是人物或群体信息，要读得清楚、稳一点；第一次出现时可以配合视线转向或站姿变化，让听众立刻知道故事焦点换到了谁身上。"
        )
    elif any(token in first for token in ("根本", "没有", "不能", "不会", "却", "最")):
        notes.append(
            f"「{first}」承担转折或判断，要稍微放慢并压实声音；不要只提高音量，而是让听众听出这里出现了矛盾、漏洞或反差。"
        )
    else:
        notes.append(
            f"「{first}」是本段的内容重心，读到这里要比前后词更有分量；可以用眼神停一下，帮助听众抓住这一句最重要的信息。"
        )
    if len(focus) > 1:
        second = focus[1]
        if re.search(r"[一二三四五六七八九十百千万两0-9]", second):
            notes.append(f"「{second}」是数量信息，要读得完整、略作强调，帮助孩子感受到规模、程度或条件的变化。")
        elif any(token in second for token in ("举起", "拿起", "放下", "吹嘘", "跳舞", "逃走", "大笑", "愣住", "刺")):
            notes.append(f"「{second}」适合配合动作演出来，声音和动作同时落点；动作不要太碎，做清楚一个就够。")
        else:
            notes.append(f"「{second}」可以作为第二个节奏落点，读完后留半拍，让前后意思分开，避免整段平铺直叙。")
    if "：" in text or "“" in text or '"' in text:
        notes.append("遇到角色说话时，要先用停顿把旁白和台词分开，再换成角色的语气；台词可以更鲜明，但不要喊成一条直线。")
    elif "？" in text or "?" in text:
        notes.append("问句结尾要自然上扬，但前面的关键词先落稳；这样疑问才像真正抛给对方，而不是普通陈述句。")
    return notes


def add_markup_runs(paragraph, text: str) -> None:
    i = 0
    while i < len(text):
        if text.startswith("**", i):
            end = text.find("**", i + 2)
            if end != -1:
                run = paragraph.add_run(text[i + 2 : end])
                set_run_font(run, "微软雅黑", 11, "C0392B", bold=True)
                i = end + 2
                continue
        if text.startswith("//", i):
            run = paragraph.add_run(" // ")
            set_run_font(run, "微软雅黑", 9, "AAAAAA")
            i += 2
            continue
        if text[i] == "/":
            run = paragraph.add_run(" / ")
            set_run_font(run, "微软雅黑", 9, "AAAAAA")
            i += 1
            continue
        j = i + 1
        while j < len(text) and not text.startswith("**", j) and text[j] != "/":
            j += 1
        run = paragraph.add_run(text[i:j])
        set_run_font(run, "微软雅黑", 11, "1A1A1A")
        i = j


def set_run_font(run, font_name: str, size: int, color: str, bold: bool = False) -> None:
    # The delivery machine does not reliably resolve Microsoft YaHei/FangSong
    # through headless LibreOffice. Use a font that is actually installed and
    # declare it for every OOXML script slot so Chinese text cannot disappear.
    resolved_font = DELIVERY_CJK_FONT if font_name in {"微软雅黑", "仿宋"} else font_name
    run.font.name = resolved_font
    for slot in ("ascii", "hAnsi", "eastAsia", "cs"):
        run._element.rPr.rFonts.set(docx_qn(f"w:{slot}"), resolved_font)
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor.from_string(color)
    run.bold = bold


def set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shading = OxmlElement("w:shd")
    shading.set(docx_qn("w:fill"), fill)
    tc_pr.append(shading)


def set_cell_border(cell, color: str, size: int) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    borders = tc_pr.first_child_found_in("w:tcBorders")
    if borders is None:
        borders = OxmlElement("w:tcBorders")
        tc_pr.append(borders)
    for edge in ("top", "left", "bottom", "right"):
        tag = f"w:{edge}"
        element = borders.find(docx_qn(tag))
        if element is None:
            element = OxmlElement(tag)
            borders.append(element)
        element.set(docx_qn("w:val"), "single")
        element.set(docx_qn("w:sz"), str(size))
        element.set(docx_qn("w:color"), color)


def fill_ppt_image(slide, image_path: Path, prs: Presentation) -> None:
    slide.shapes.add_picture(str(image_path), 0, 0, prs.slide_width, prs.slide_height)


def overlay_title(slide, story_name: str, prs: Presentation) -> None:
    box = slide.shapes.add_textbox(0, int(prs.slide_height * 0.36), prs.slide_width, int(prs.slide_height * 0.24))
    tf = box.text_frame
    tf.clear()
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    run = p.add_run()
    run.text = story_name
    run.font.name = DELIVERY_CJK_FONT
    run.font.size = PptPt(58)
    run.font.bold = True
    run.font.color.rgb = PptRGBColor(255, 224, 86)


def set_ppt_advance(slide, seconds: float) -> None:
    el = slide._element
    transition = el.find(pptx_qn("p:transition"))
    if transition is None:
        transition = etree.SubElement(el, pptx_qn("p:transition"))
    transition.set("advClick", "0")
    transition.set("advTm", str(int(seconds * 1000)))
    if transition.find(pptx_qn("p:fade")) is None:
        etree.SubElement(transition, pptx_qn("p:fade"))


def add_ppt_subtitle(slide, text: str, prs: Presentation) -> None:
    clean, font_size = fit_ppt_subtitle_text(text)
    line_count = min(PPT_SUBTITLE_MAX_LINES, clean.count("\n") + 1)
    height_ratio = 0.09 if line_count == 1 else 0.14
    height = min(int(prs.slide_height * PPT_SUBTITLE_MAX_HEIGHT_RATIO), int(prs.slide_height * height_ratio))
    height = max(1, height)
    box = slide.shapes.add_textbox(0, prs.slide_height - height, prs.slide_width, height)
    box.fill.solid()
    box.fill.fore_color.rgb = PptRGBColor(0, 0, 0)
    tf = box.text_frame
    tf.clear()
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    run = p.add_run()
    run.text = clean
    run.font.name = DELIVERY_CJK_FONT
    run.font.size = PptPt(font_size)
    run.font.bold = True
    run.font.color.rgb = PptRGBColor(255, 255, 255)


def fit_ppt_subtitle_text(text: str) -> tuple[str, int]:
    clean = clean_ppt_subtitle_text(text)
    if not clean:
        return "", 28
    if len(clean) <= 22:
        return clean, 28

    # Keep the subtitle to two balanced lines.  Chinese text has no reliable
    # whitespace boundaries, so prefer punctuation/spaces near the midpoint,
    # then fall back to a character split.  A very long sentence still stays
    # within the bounded two-line box and gets a verifiably smaller font.
    midpoint = len(clean) // 2
    split_at = min(
        range(1, len(clean)),
        key=lambda idx: abs(idx - midpoint)
        + (0 if clean[idx - 1] in " ，,；;：:、 " or clean[idx] in " ，,；;：:、 " else 4),
    )
    first = clean[:split_at].strip()
    second = clean[split_at:].strip()
    if not first or not second:
        first, second = clean[:midpoint], clean[midpoint:]
    longest_line = max(len(first), len(second))
    font_size = max(16, min(28, int(round(28 * 22 / max(22, longest_line)))))
    return f"{first}\n{second}", font_size


def clean_ppt_subtitle_text(text: str) -> str:
    clean = clean_public_story_text(text).strip()
    clean = re.sub(r"[，。！？；：、,.!?;:“”\"'‘’《》〈〉（）()【】\[\]———…·]", " ", clean)
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip()


def embed_ppt_bgm(pptx_path: Path, audio_path: Path) -> None:
    ext = audio_path.suffix.lower()
    mime = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4"}.get(ext, "audio/mpeg")
    audio_entry = f"ppt/media/bgm{ext}"
    slide_path = "ppt/slides/slide1.xml"
    slide_rels = "ppt/slides/_rels/slide1.xml.rels"

    with zipfile.ZipFile(pptx_path, "r") as zin:
        files = {item.filename: zin.read(item.filename) for item in zin.infolist()}
    if slide_path not in files:
        raise ValueError(f"PPTX 结构异常：缺少 {slide_path}")

    files[audio_entry] = audio_path.read_bytes()
    ct_tree = etree.fromstring(files["[Content_Types].xml"])
    existing_exts = {element.get("Extension") for element in ct_tree.findall(f"{{{PPT_CT_NS}}}Default")}
    ext_str = ext.lstrip(".")
    if ext_str not in existing_exts:
        element = etree.SubElement(ct_tree, f"{{{PPT_CT_NS}}}Default")
        element.set("Extension", ext_str)
        element.set("ContentType", mime)
        files["[Content_Types].xml"] = etree.tostring(
            ct_tree, xml_declaration=True, encoding="UTF-8", standalone=True
        )

    rels_bytes = files.get(
        slide_rels,
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
    )
    rels_tree = etree.fromstring(rels_bytes)
    used_ids = {element.get("Id") for element in rels_tree}

    def next_rid() -> str:
        i = 1
        while f"rId{i}" in used_ids:
            i += 1
        used_ids.add(f"rId{i}")
        return f"rId{i}"

    rid_audio = next_rid()
    rid_media = next_rid()
    for rid, rel_type in ((rid_audio, PPT_AUDIO_REL), (rid_media, PPT_MEDIA_REL)):
        rel = etree.SubElement(rels_tree, f"{{{PPT_RELS_NS}}}Relationship")
        rel.set("Id", rid)
        rel.set("Type", rel_type)
        rel.set("Target", f"../media/bgm{ext}")
    files[slide_rels] = etree.tostring(rels_tree, xml_declaration=True, encoding="UTF-8", standalone=True)

    slide_xml = files[slide_path].decode("utf-8")
    all_ids = [int(match) for match in re.findall(r'\bid="(\d+)"', slide_xml)]
    shape_id = (max(all_ids) if all_ids else 0) + 1
    audio_pic = (
        f'<p:pic xmlns:p="{PPT_P_NS}" xmlns:a="{PPT_A_NS}" xmlns:r="{PPT_R_NS}" xmlns:p14="{PPT_P14_NS}">'
        f'<p:nvPicPr><p:cNvPr id="{shape_id}" name="bgm">'
        f'<a:hlinkClick r:id="" action="ppaction://media"/></p:cNvPr><p:cNvPicPr/>'
        f'<p:nvPr><a:audioFile r:link="{rid_audio}"/><p:extLst><p:ext uri="{PPT_P14_EXT_URI}">'
        f'<p14:media r:embed="{rid_media}"/></p:ext></p:extLst></p:nvPr></p:nvPicPr>'
        f'<p:blipFill><a:blip/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
        f'<p:spPr><a:xfrm><a:off x="-500" y="-500"/><a:ext cx="500" cy="500"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>'
    )
    timing_xml = (
        f'<p:timing xmlns:p="{PPT_P_NS}"><p:tnLst><p:par>'
        f'<p:cTn id="1" dur="indefinite" restart="whenNotActive" nodeType="tmRoot"><p:childTnLst>'
        f'<p:seq concurrent="1" nextAc="seek"><p:cTn id="2" dur="indefinite" nodeType="mainSeq">'
        f'<p:childTnLst><p:par><p:cTn id="3" fill="hold"><p:stCondLst><p:cond delay="indefinite"/>'
        f'<p:cond evt="onBegin" delay="0"><p:tn val="2"/></p:cond></p:stCondLst><p:childTnLst><p:par>'
        f'<p:cTn id="4" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst><p:par>'
        f'<p:cTn id="5" presetID="1" presetClass="mediacall" presetSubtype="0" fill="hold" nodeType="afterEffect">'
        f'<p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst><p:cmd type="call" cmd="playFrom(0.0)">'
        f'<p:cBhvr additive="base"><p:cTn id="6" dur="1" fill="hold"/><p:tgtEl><p:spTgt spid="{shape_id}"/>'
        f'</p:tgtEl></p:cBhvr></p:cmd></p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn></p:par>'
        f'</p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn>'
        f'<p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:prevCondLst>'
        f'<p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:nextCondLst>'
        f'</p:seq><p:audio><p:cMediaNode numSld="999" showWhenStopped="0">'
        f'<p:cTn id="7" repeatCount="indefinite" fill="hold" display="1"><p:stCondLst><p:cond delay="indefinite"/>'
        f'</p:stCondLst><p:endCondLst><p:cond evt="onStopAudio"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond>'
        f'</p:endCondLst></p:cTn><p:tgtEl><p:spTgt spid="{shape_id}"/></p:tgtEl></p:cMediaNode></p:audio>'
        f'</p:childTnLst></p:cTn></p:par></p:tnLst><p:bldLst><p:bldP spid="{shape_id}" grpId="0"/></p:bldLst></p:timing>'
    )
    close_sptree = "</p:spTree>"
    idx = slide_xml.rfind(close_sptree)
    if idx == -1:
        raise ValueError("slide1.xml 结构异常：未找到 </p:spTree>")
    slide_xml = slide_xml[:idx] + audio_pic + close_sptree + slide_xml[idx + len(close_sptree) :]
    slide_xml = re.sub(r"<p:timing[\s\S]*?</p:timing>", "", slide_xml)
    close_sld = "</p:sld>"
    idx = slide_xml.rfind(close_sld)
    if idx != -1:
        slide_xml = slide_xml[:idx] + timing_xml + close_sld + slide_xml[idx + len(close_sld) :]
    files[slide_path] = slide_xml.encode("utf-8")

    tmp_path = str(pptx_path) + ".tmp"
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in files.items():
            zout.writestr(name, data)
    os.replace(tmp_path, str(pptx_path))


def render_preview_sheet(raw_frames: list[Path], keyed_frames: list[Path], output_path: Path) -> None:
    tile_w, tile_h = 420, 236
    padding = 18
    label_h = 34
    sheet = Image.new("RGB", (padding * 3 + tile_w * 2, padding * 4 + (tile_h + label_h) * len(raw_frames)), (246, 244, 238))
    draw = ImageDraw.Draw(sheet)
    font = load_font(22)
    for row, (raw, keyed) in enumerate(zip(raw_frames, keyed_frames)):
        y = padding + row * (tile_h + label_h + padding)
        for col, (label, path) in enumerate((("原始帧", raw), ("抠像预览", keyed))):
            x = padding + col * (tile_w + padding)
            image = Image.open(path).convert("RGB")
            image.thumbnail((tile_w, tile_h))
            tile = Image.new("RGB", (tile_w, tile_h), (220, 218, 210))
            tile.paste(image, ((tile_w - image.width) // 2, (tile_h - image.height) // 2))
            sheet.paste(tile, (x, y))
            draw.text((x, y + tile_h + 6), f"{label} {row + 1}", fill=(32, 32, 32), font=font)
    sheet.save(output_path, quality=92)


def crop_image_to_ratio(image: Image.Image, ratio: float) -> Image.Image:
    current = image.width / image.height
    if current > ratio:
        new_width = int(image.height * ratio)
        left = (image.width - new_width) // 2
        return image.crop((left, 0, left + new_width, image.height))
    new_height = int(image.width / ratio)
    top = (image.height - new_height) // 2
    return image.crop((0, top, image.width, top + new_height))


def sanitize_filename(value: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\\s]+", "-", value).strip("-") or "story"


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for font_path in ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Light.ttc"):
        path = Path(font_path)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
