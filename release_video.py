from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from story_video_synthesizer.media import ensure_dir, probe_duration, run_command


FINAL_WIDTH = 1080
FINAL_HEIGHT = 1440
TOP_HEIGHT = 320
CENTER_HEIGHT = 608
BOTTOM_HEIGHT = FINAL_HEIGHT - TOP_HEIGHT - CENTER_HEIGHT
DEFAULT_VIDEO_BOX = (0, 416, 1080, 608)

WIDE_WIDTH = 1920
WIDE_HEIGHT = 1080
STORY_BOX_X = 170
STORY_BOX_Y = 250
STORY_BOX_W = 990
STORY_BOX_H = 557
PERSON_HEIGHT = 940
PERSON_X = 1190
PERSON_Y = 100


@dataclass(frozen=True)
class ReleaseConfig:
    story_name: str
    duration_text: str
    bg_video: Path
    output_dir: Path
    variant: str
    bg_image: Path | None
    person_greenscreen: Path | None
    audio_mix: Path | None
    watermark_logo: Path | None
    antipiracy_logo: Path | None
    plate_image: Path | None
    video_box: tuple[int, int, int, int]
    watermark_width: int
    watermark_opacity: float
    watermark_speed: float
    frame_image: Path | None
    story_box: tuple[int, int, int, int]
    story_bleed: int
    background_blur: int
    frame_image_b: Path | None
    b_story_box: tuple[int, int, int, int]
    b_windows: tuple[tuple[float, float], ...]
    c_windows: tuple[tuple[float, float], ...]
    story_logo: Path | None
    story_logo_width_a: int
    story_logo_width_b: int
    story_logo_x: int
    story_logo_y: int
    subtitle_srt: Path | None
    subtitle_font_size: int
    subtitle_margin_v: int
    mix_bg_audio: bool
    voice_volume: float
    bg_audio_volume: float
    person_height: int
    person_x: int
    person_y: int
    chroma_color: str
    chroma_similarity: float
    chroma_blend: float
    keyer: str
    person_crop: tuple[int, int, int, int] | None
    detected_person_bbox: tuple[int, int, int, int] | None
    person_grade: str
    person_beauty: str
    library_watermark_text: str
    tail_seconds: float
    tail_notice_text: str
    crf: int
    preset: str
    output_scale: int


def main() -> None:
    parser = argparse.ArgumentParser(description="把故事背景视频包装成两个小红书发布版")
    parser.add_argument("--story-name", required=True, help="故事名，例如《猴子捞月》或 猴子捞月")
    parser.add_argument("--duration-text", required=True, help="显示在信息栏里的时长，例如 3分钟 / 2分40秒")
    parser.add_argument("--bg-video", required=True, type=Path, help="已生成的背景故事视频")
    parser.add_argument("--output-dir", required=True, type=Path, help="发布视频输出目录")
    parser.add_argument("--variant", choices=["both", "main", "library"], default="both")
    parser.add_argument("--bg-image", type=Path, help="主账号 16:9 底图")
    parser.add_argument("--person-greenscreen", type=Path, help="主账号绿幕人像视频")
    parser.add_argument("--audio-mix", type=Path, help="已混好的人声 + 音乐音频")
    parser.add_argument("--watermark-logo", type=Path, help="品牌水印 PNG/JPG")
    parser.add_argument("--antipiracy-logo", type=Path, help="宝库号中间视频区域飘动防盗 PNG；不传则使用文字水印")
    parser.add_argument("--plate-image", type=Path, help="AI 生成的 3:4 完整底板图；传入后视频会嵌进 --video-box")
    parser.add_argument("--video-box", default="0,416,1080,608", help="视频嵌入窗口：x,y,w,h，基于 1080x1440")
    parser.add_argument("--watermark-width", default=120, type=int, help="防盗 PNG 在中间视频里的显示宽度")
    parser.add_argument("--watermark-opacity", default=0.62, type=float, help="防盗 PNG 不透明度，0-1")
    parser.add_argument("--watermark-speed", default=0.35, type=float, help="防盗水印移动速度倍率，默认使用缓慢完整移动")
    parser.add_argument("--frame-image", type=Path, help="可选透明 PNG 框模板；不传则使用默认框")
    parser.add_argument("--story-box", default=f"{STORY_BOX_X},{STORY_BOX_Y},{STORY_BOX_W},{STORY_BOX_H}", help="A 画面故事视频窗口：x,y,w,h，基于 1920x1080")
    parser.add_argument("--story-bleed", default=0, type=int, help="故事视频开口遮罩扩展像素；默认由框内开口遮罩控制，不直接铺矩形")
    parser.add_argument("--background-blur", default=14, type=int, help="主账号 16:9 背景虚化半径")
    parser.add_argument("--frame-image-b", type=Path, help="可选 B 画面透明 PNG 大框模板")
    parser.add_argument("--b-story-box", default="150,88,1620,911", help="B 画面故事视频窗口：x,y,w,h，基于 1920x1080")
    parser.add_argument("--b-windows", default="", help="B 画面出现时间段，例如 8-14,38.5-55；留空则不切 B")
    parser.add_argument("--c-windows", default="", help="C 画面（仅人物+主题背景）出现时间段，例如 0-15,52-70")
    parser.add_argument("--story-logo", type=Path, help="叠在故事视频右上角的小台标 PNG")
    parser.add_argument("--story-logo-width-a", default=180, type=int, help="A 画面台标宽度")
    parser.add_argument("--story-logo-width-b", default=210, type=int, help="B 画面台标宽度")
    parser.add_argument("--story-logo-x", default=42, type=int, help="台标固定 X，基于 1920x1080 主画布")
    parser.add_argument("--story-logo-y", default=44, type=int, help="台标固定 Y，基于 1920x1080 主画布")
    parser.add_argument("--subtitle-srt", type=Path, help="独立叠在 16:9 横屏底部的字幕 SRT；故事框内视频应使用无字幕版")
    parser.add_argument("--subtitle-font-size", default=52, type=int)
    parser.add_argument("--subtitle-margin-v", default=72, type=int, help="字幕距 16:9 横屏底部距离")
    parser.add_argument("--mix-bg-audio", action="store_true", help="把 --bg-video 的音频作为配乐，与 --audio-mix 混合")
    parser.add_argument("--voice-volume", default=1.05, type=float, help="--audio-mix 人声音量倍率")
    parser.add_argument("--bg-audio-volume", default=0.28, type=float, help="--bg-video 配乐音量倍率")
    parser.add_argument("--person-height", default=PERSON_HEIGHT, type=int, help="主账号人像缩放后的高度")
    parser.add_argument("--person-x", default=PERSON_X, type=int, help="主账号人像左上角 X")
    parser.add_argument("--person-y", default=PERSON_Y, type=int, help="主账号人像左上角 Y")
    parser.add_argument("--chroma-color", default="0x00FF00", help="绿幕颜色，默认 0x00FF00")
    parser.add_argument("--chroma-similarity", default=0.16, type=float)
    parser.add_argument("--chroma-blend", default=0.08, type=float)
    parser.add_argument("--keyer", choices=["chromakey", "colorkey"], default="chromakey")
    parser.add_argument("--keying-preset-json", type=Path, help="自动抠像生成的 keying_preset.json；传入后覆盖抠像相关参数")
    parser.add_argument("--person-crop", default="", help="可选人像裁剪：x,y,w,h，例如 0,0,1080,1440")
    parser.add_argument("--person-grade", choices=["none", "natural", "log-soft", "log-strong"], default="natural")
    parser.add_argument("--person-beauty", choices=["none", "light"], default="light", help="本地可复现轻度磨皮；不依赖剪映")
    parser.add_argument("--library-watermark-text", default="绵羊姐姐原创故事资源")
    parser.add_argument("--tail-seconds", default=0.0, type=float, help="宝库号结尾模糊提示时长；0 表示按总时长自动估算")
    parser.add_argument("--tail-notice-text", default="有需要联系客服，好作品有偿分享！")
    parser.add_argument("--crf", default=15, type=int)
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--output-scale", default=2, type=int, help="主账号输出倍率：1=1080x1440，2=2160x2880；宝库号固定 1080x1440")
    parser.add_argument("--preview-dir", type=Path, help="只生成发布合成预览帧 PNG，不编码完整视频")
    parser.add_argument("--preview-times", default="1,2,37,92", help="预览帧时间点，秒，用逗号分隔；默认包含开头动作帧以检查手部裁切")
    parser.add_argument("--preview-person-layouts", default="", help="预览人像布局候选；auto 或 height,x,y;label:height,x,y")
    args = parser.parse_args()

    keying = load_keying_preset(args.keying_preset_json.expanduser()) if args.keying_preset_json else {}
    keyer = str(keying.get("keyer", args.keyer))
    if keyer not in {"chromakey", "colorkey"}:
        raise ValueError("keying_preset.json 中 keyer 只能是 chromakey 或 colorkey")
    person_grade = str(keying.get("person_grade", args.person_grade))
    if person_grade not in {"none", "log-soft", "log-strong"}:
        raise ValueError("keying_preset.json 中 person_grade 只能是 none/log-soft/log-strong")
    person_beauty = str(keying.get("person_beauty", args.person_beauty))
    if person_beauty not in {"none", "light"}:
        raise ValueError("keying_preset.json 中 person_beauty 只能是 none/light")
    person_crop_value = args.person_crop
    if not person_crop_value.strip() and keying.get("person_crop") is not None:
        person_crop_value = format_preset_box(keying["person_crop"])
    detected_person_bbox_value = ""
    if keying.get("detected_person_bbox") is not None:
        detected_person_bbox_value = format_preset_box(keying["detected_person_bbox"])
    person_height = args.person_height
    person_x = args.person_x
    person_y = args.person_y
    if keying.get("person_height_ratio") is not None:
        person_height = round(WIDE_HEIGHT * float(keying["person_height_ratio"]))
        bottom_margin = round(float(keying.get("bottom_margin", 0)))
        person_y = WIDE_HEIGHT - person_height - bottom_margin
    if keying.get("person_x") is not None:
        person_x = round(float(keying["person_x"]))
    if keying.get("person_y") is not None:
        person_y = round(float(keying["person_y"]))
    story_box_value = format_preset_box(keying["story_box"]) if keying.get("story_box") is not None else args.story_box
    b_story_box_value = format_preset_box(keying["b_story_box"]) if keying.get("b_story_box") is not None else args.b_story_box
    story_bleed = int(float(keying.get("story_bleed", args.story_bleed)))
    background_blur = int(float(keying.get("background_blur", args.background_blur)))

    config = ReleaseConfig(
        story_name=args.story_name,
        duration_text=args.duration_text,
        bg_video=args.bg_video.expanduser(),
        output_dir=args.output_dir.expanduser(),
        variant=args.variant,
        bg_image=args.bg_image.expanduser() if args.bg_image else None,
        person_greenscreen=args.person_greenscreen.expanduser() if args.person_greenscreen else None,
        audio_mix=args.audio_mix.expanduser() if args.audio_mix else None,
        watermark_logo=args.watermark_logo.expanduser() if args.watermark_logo else None,
        antipiracy_logo=args.antipiracy_logo.expanduser() if args.antipiracy_logo else None,
        plate_image=args.plate_image.expanduser() if args.plate_image else None,
        video_box=parse_video_box(args.video_box),
        watermark_width=args.watermark_width,
        watermark_opacity=max(0.0, min(1.0, args.watermark_opacity)),
        watermark_speed=max(0.1, args.watermark_speed),
        frame_image=args.frame_image.expanduser() if args.frame_image else None,
        story_box=parse_required_box(story_box_value, "--story-box", "250,285,825,464"),
        story_bleed=max(0, story_bleed),
        background_blur=max(0, background_blur),
        frame_image_b=args.frame_image_b.expanduser() if args.frame_image_b else None,
        b_story_box=parse_required_box(b_story_box_value, "--b-story-box", "150,88,1620,911"),
        b_windows=parse_b_windows(args.b_windows),
        c_windows=parse_b_windows(args.c_windows),
        story_logo=args.story_logo.expanduser() if args.story_logo else None,
        story_logo_width_a=max(1, args.story_logo_width_a),
        story_logo_width_b=max(1, args.story_logo_width_b),
        story_logo_x=args.story_logo_x,
        story_logo_y=args.story_logo_y,
        subtitle_srt=args.subtitle_srt.expanduser() if args.subtitle_srt else None,
        subtitle_font_size=max(1, args.subtitle_font_size),
        subtitle_margin_v=max(0, args.subtitle_margin_v),
        mix_bg_audio=args.mix_bg_audio,
        voice_volume=max(0.0, args.voice_volume),
        bg_audio_volume=max(0.0, args.bg_audio_volume),
        person_height=person_height,
        person_x=person_x,
        person_y=person_y,
        chroma_color=str(keying.get("chroma_color", args.chroma_color)),
        chroma_similarity=float(keying.get("chroma_similarity", args.chroma_similarity)),
        chroma_blend=float(keying.get("chroma_blend", args.chroma_blend)),
        keyer=keyer,
        person_crop=parse_optional_box(person_crop_value, "--person-crop"),
        detected_person_bbox=parse_optional_box(detected_person_bbox_value, "detected_person_bbox"),
        person_grade=person_grade,
        person_beauty=person_beauty,
        library_watermark_text=args.library_watermark_text,
        tail_seconds=args.tail_seconds,
        tail_notice_text=args.tail_notice_text,
        crf=args.crf,
        preset=args.preset,
        output_scale=max(1, args.output_scale),
    )
    if args.preview_dir is not None:
        render_release_previews(
            config,
            args.preview_dir.expanduser(),
            parse_preview_times(args.preview_times),
            parse_preview_person_layouts(args.preview_person_layouts, config),
        )
    else:
        package_release_videos(config)


def load_keying_preset(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"keying_preset.json 不存在：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("keying_preset.json 必须是 JSON 对象")
    selected = str(data.get("keying_candidate") or "").strip()
    if selected:
        search_ref = str(data.get("keying_search") or "").strip()
        search_path = Path(search_ref).expanduser() if search_ref else path.with_name("keying_search.json")
        if not search_path.is_absolute():
            search_path = (path.parent / search_path).resolve()
        try:
            search = json.loads(search_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"択像预设缺少可验证的 keying_search 证据：{search_path}") from exc
        candidates = search.get("candidates") if isinstance(search, dict) else None
        candidate_ids = {
            str(item.get("id") or item.get("candidate_id") or "").strip()
            for item in (candidates or [])
            if isinstance(item, dict)
        }
        if selected not in candidate_ids:
            raise ValueError(f"keying_candidate={selected!r} 未出现在 keying_search.candidates 中")
    return data


def format_preset_box(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return ",".join(str(int(float(part))) for part in value)
    raise ValueError("keying_preset.json 中布局框必须是 x,y,w,h")


def parse_video_box(value: str) -> tuple[int, int, int, int]:
    return parse_required_box(value, "--video-box", "0,394,1080,608")


def parse_required_box(value: str, flag: str, example: str) -> tuple[int, int, int, int]:
    parts = [part.strip() for part in value.replace("，", ",").split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(f"{flag} 格式应为 x,y,w,h，例如 {example}")
    try:
        x, y, width, height = (int(float(part)) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{flag} 只能包含数字") from exc
    return x, y, width, height


def parse_optional_box(value: str, flag: str) -> tuple[int, int, int, int] | None:
    if not value.strip():
        return None
    return parse_required_box(value, flag, "0,0,1080,1440")


def parse_b_windows(value: str) -> tuple[tuple[float, float], ...]:
    if not value.strip():
        return ()
    windows: list[tuple[float, float]] = []
    for raw_part in value.replace("，", ",").split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" not in part:
            raise argparse.ArgumentTypeError("--b-windows 格式应为 start-end,start-end，例如 8-14,38.5-55")
        raw_start, raw_end = part.split("-", 1)
        try:
            start = float(raw_start.strip())
            end = float(raw_end.strip())
        except ValueError as exc:
            raise argparse.ArgumentTypeError("--b-windows 只能包含数字时间") from exc
        if start < 0 or end <= start:
            raise argparse.ArgumentTypeError("--b-windows 每段必须满足 0 <= start < end")
        windows.append((start, end))
    return tuple(windows)


def parse_preview_times(value: str) -> list[float]:
    times: list[float] = []
    for raw in value.replace("，", ",").split(","):
        raw = raw.strip()
        if raw:
            times.append(max(0.0, float(raw)))
    return times or [2.0]


def parse_preview_person_layouts(value: str, config: ReleaseConfig) -> list[tuple[str, ReleaseConfig]]:
    value = value.strip()
    if not value:
        return [("current", config)]
    if value.lower() == "auto":
        candidates = [
            ("h78", round(WIDE_HEIGHT * 0.78), 1280, 252),
            ("h84", round(WIDE_HEIGHT * 0.84), 1230, 220),
            ("h90", round(WIDE_HEIGHT * 0.90), 1190, 188),
            ("h96", round(WIDE_HEIGHT * 0.96), 1160, 154),
        ]
    else:
        candidates = []
        for index, raw in enumerate(value.replace("；", ";").split(";"), start=1):
            raw = raw.strip()
            if not raw:
                continue
            label = f"layout{index}"
            if ":" in raw:
                label, raw = raw.split(":", 1)
                label = label.strip() or f"layout{index}"
            parts = [part.strip() for part in raw.replace("，", ",").split(",")]
            if len(parts) != 3:
                raise argparse.ArgumentTypeError("--preview-person-layouts 每项应为 height,x,y 或 label:height,x,y")
            height, x, y = (int(float(part)) for part in parts)
            candidates.append((label, height, x, y))
    layouts: list[tuple[str, ReleaseConfig]] = []
    for label, height, x, y in candidates:
        layouts.append((label, replace(config, person_height=max(1, height), person_x=x, person_y=y)))
    return layouts or [("current", config)]


def args_input_paths(args: list[str]) -> list[str]:
    return [args[index + 1] for index, value in enumerate(args[:-1]) if value == "-i"]


def scaled_box(box: tuple[int, int, int, int], scale: int) -> tuple[int, int, int, int]:
    x, y, width, height = box
    return x * scale, y * scale, width * scale, height * scale


def bleed_box(box: tuple[int, int, int, int], bleed: int, canvas_size: tuple[int, int]) -> tuple[int, int, int, int]:
    x, y, width, height = box
    canvas_w, canvas_h = canvas_size
    x1 = max(0, x - bleed)
    y1 = max(0, y - bleed)
    x2 = min(canvas_w, x + width + bleed)
    y2 = min(canvas_h, y + height + bleed)
    return x1, y1, x2 - x1, y2 - y1


def prepare_story_frame_assets(
    frame_image: Path,
    window: tuple[int, int, int, int],
    frame_output: Path,
    mask_output: Path,
    story_bleed: int = 0,
) -> tuple[Path, Path, tuple[int, int, int, int]]:
    """Prepare a frame layer and a natural aperture mask from the frame alpha."""
    frame = Image.open(frame_image).convert("RGBA")
    if frame.size != (WIDE_WIDTH, WIDE_HEIGHT):
        frame = scale_crop_image(frame, WIDE_WIDTH, WIDE_HEIGHT)
    frame = fit_frame_to_window(frame, window)
    frame.save(frame_output)
    mask = story_aperture_mask(frame, window, story_bleed)
    bbox = mask.getbbox()
    if bbox is None:
        raise ValueError(f"无法从故事框生成视频开口遮罩：{frame_image}")
    mask.save(mask_output)
    x1, y1, x2, y2 = bbox
    return frame_output, mask_output, (x1, y1, x2 - x1, y2 - y1)


def fit_frame_to_window(frame: Image.Image, window: tuple[int, int, int, int]) -> Image.Image:
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


def story_aperture_mask(frame: Image.Image, window: tuple[int, int, int, int], bleed: int = 0) -> Image.Image:
    alpha = frame.convert("RGBA").getchannel("A")
    binary = alpha.point(lambda value: 255 if value <= 12 else 0, mode="L")
    x, y, width, height = window
    seed = find_transparent_seed(binary, (x + width // 2, y + height // 2), window)
    ImageDraw.floodfill(binary, seed, 128, thresh=0)
    mask = binary.point(lambda value: 255 if value == 128 else 0, mode="L")
    if bleed > 0:
        # Small dilation lets story video tuck under antialiased frame edges without
        # reverting to a visible rectangular story layer.
        radius = min(bleed, 24)
        mask = mask.filter(ImageFilter.MaxFilter(radius * 2 + 1))
    return mask


def find_transparent_seed(mask: Image.Image, preferred: tuple[int, int], window: tuple[int, int, int, int]) -> tuple[int, int]:
    px = mask.load()
    w, h = mask.size
    x, y = preferred
    if 0 <= x < w and 0 <= y < h and px[x, y] == 255:
        return x, y
    wx, wy, ww, wh = window
    for radius in range(4, max(ww, wh), 4):
        for sx in range(max(wx, x - radius), min(wx + ww, x + radius + 1), 4):
            for sy in (max(wy, y - radius), min(wy + wh - 1, y + radius)):
                if 0 <= sx < w and 0 <= sy < h and px[sx, sy] == 255:
                    return sx, sy
        for sy in range(max(wy, y - radius), min(wy + wh, y + radius + 1), 4):
            for sx in (max(wx, x - radius), min(wx + ww - 1, x + radius)):
                if 0 <= sx < w and 0 <= sy < h and px[sx, sy] == 255:
                    return sx, sy
    raise ValueError("故事框窗口中心附近没有可用透明开口")


def package_release_videos(config: ReleaseConfig) -> None:
    validate_config(config)
    ensure_dir(config.output_dir)
    work_dir = config.output_dir / "_release_work"
    ensure_dir(work_dir)

    assets = render_static_assets(config, work_dir)
    if config.variant in {"both", "main"}:
        main_wide = work_dir / "main_account_16x9.mp4"
        main_vertical = config.output_dir / "主账号发布视频.mp4"
        render_main_wide(config, assets["frame"], main_wide)
        if config.plate_image is not None:
            render_plate_package(main_wide, config.plate_image, main_vertical, config, output_scale=config.output_scale)
        else:
            render_vertical_package(
                source_video=main_wide,
                top_panel=assets["main_top"],
                bottom_panel=assets["main_bottom"],
                output_path=main_vertical,
                duration=probe_duration(main_wide),
                config=config,
                output_scale=config.output_scale,
            )
        print(f"已生成主账号发布视频：{main_vertical}")

    if config.variant in {"both", "library"}:
        library_output = config.output_dir / "宝库号发布视频.mp4"
        library_window = work_dir / "library_video_window.mp4"
        render_library_window_video(
            source_video=config.bg_video,
            watermark_png=assets["library_watermark"],
            tail_notice_png=assets["tail_notice"],
            output_path=library_window,
            config=config,
        )
        if config.plate_image is not None:
            render_plate_package(library_window, config.plate_image, library_output, config, output_scale=1)
        else:
            render_vertical_package(
                source_video=library_window,
                top_panel=assets["library_top"],
                bottom_panel=assets["library_bottom"],
                output_path=library_output,
                duration=probe_duration(library_window),
                config=config,
                output_scale=1,
            )
        print(f"已生成宝库号发布视频：{library_output}")


def render_release_previews(
    config: ReleaseConfig,
    preview_dir: Path,
    times: list[float],
    person_layouts: list[tuple[str, ReleaseConfig]] | None = None,
) -> None:
    validate_config(config)
    ensure_dir(preview_dir)
    work_dir = preview_dir / "_work"
    ensure_dir(work_dir)
    assets = render_static_assets(config, work_dir)
    if config.variant in {"both", "main"}:
        if config.bg_image is None or config.person_greenscreen is None or config.frame_image is None:
            raise ValueError("主账号预览需要 --bg-image、--person-greenscreen 和 --frame-image")
        layouts = person_layouts or [("current", config)]
        for timestamp in times:
            use_b = any(start <= timestamp <= end for start, end in config.b_windows)
            use_c = any(start <= timestamp <= end for start, end in config.c_windows)
            if use_b:
                output = preview_dir / f"main_{int(round(timestamp)):03d}s_b.png"
                render_main_preview_frame(config, assets["frame"], output, work_dir, timestamp, "b")
                print(f"已生成主账号预览帧：{output}")
                continue
            if use_c:
                output = preview_dir / f"main_{int(round(timestamp)):03d}s_c.png"
                render_main_preview_frame(config, assets["frame"], output, work_dir, timestamp, "c")
                print(f"已生成主账号预览帧：{output}")
                continue
            for label, layout_config in layouts:
                suffix = "" if label == "current" and len(layouts) == 1 else f"_{label}"
                output = preview_dir / f"main_{int(round(timestamp)):03d}s_a{suffix}.png"
                render_main_preview_frame(layout_config, assets["frame"], output, work_dir, timestamp, "a")
                print(f"已生成主账号预览帧：{output}")
    if config.variant in {"both", "library"}:
        for timestamp in times:
            output = preview_dir / f"library_{int(round(timestamp)):03d}s.png"
            render_library_preview_frame(config, assets["library_watermark"], output, work_dir, timestamp)
            print(f"已生成宝库号预览帧：{output}")


def render_main_preview_frame(
    config: ReleaseConfig,
    frame_image: Path,
    output_path: Path,
    work_dir: Path,
    timestamp: float,
    scene: str,
) -> None:
    assert config.bg_image is not None
    assert config.person_greenscreen is not None
    base = scale_crop_image(Image.open(config.bg_image).convert("RGBA"), WIDE_WIDTH, WIDE_HEIGHT)
    if config.background_blur > 0:
        base = base.filter(ImageFilter.GaussianBlur(config.background_blur))
    if scene != "c":
        use_b_scene = scene == "b"
        window = config.b_story_box if use_b_scene else config.story_box
        frame_source = config.frame_image_b if use_b_scene and config.frame_image_b is not None else frame_image
        prepared_frame, mask_path, story_bbox = prepare_story_frame_assets(
            frame_source,
            window,
            work_dir / f"preview_frame_{scene}.png",
            work_dir / f"preview_mask_{scene}.png",
            config.story_bleed,
        )
        story_frame_path = work_dir / f"story_{int(round(timestamp)):03d}.png"
        extract_video_frame(config.bg_video, story_frame_path, timestamp)
        story = scale_crop_image(Image.open(story_frame_path).convert("RGBA"), story_bbox[2], story_bbox[3])
        story_layer = Image.new("RGBA", (WIDE_WIDTH, WIDE_HEIGHT), (0, 0, 0, 0))
        story_layer.alpha_composite(story, (story_bbox[0], story_bbox[1]))
        story_layer.putalpha(Image.open(mask_path).convert("L"))
        base.alpha_composite(story_layer)
        base.alpha_composite(Image.open(prepared_frame).convert("RGBA"))
    if scene != "b":
        person_path = work_dir / f"person_{int(round(timestamp)):03d}.png"
        extract_person_frame(config, config.person_greenscreen, person_path, timestamp)
        person = Image.open(person_path).convert("RGBA")
        if scene == "c":
            person, person_x, person_y = native_person_preview_layout(
                person,
                config.detected_person_bbox if config.person_crop is None else None,
                WIDE_WIDTH,
                WIDE_HEIGHT,
            )
        else:
            if config.detected_person_bbox is not None and config.person_crop is None:
                x, y, width, height = clamp_box(config.detected_person_bbox, person.width, person.height)
                person = person.crop((x, y, x + width, y + height))
            person = person.resize(
                (max(1, round(person.width * config.person_height / max(1, person.height))), config.person_height),
                Image.Resampling.LANCZOS,
            )
            person_x = max(0, min(config.person_x, WIDE_WIDTH - person.width))
            person_y = max(0, min(config.person_y, WIDE_HEIGHT - person.height))
        base.alpha_composite(person, (person_x, person_y))
    if config.watermark_logo is not None:
        logo = Image.open(config.watermark_logo).convert("RGBA")
        logo = ImageOps.contain(logo, (190, 190), method=Image.Resampling.LANCZOS)
        base.alpha_composite(logo, (WIDE_WIDTH - logo.width - 44, 42))
    if config.story_logo is not None:
        logo = Image.open(config.story_logo).convert("RGBA")
        logo_width = config.story_logo_width_b if scene == "b" else config.story_logo_width_a
        logo = logo.resize((logo_width, max(1, round(logo.height * logo_width / max(1, logo.width)))), Image.Resampling.LANCZOS)
        base.alpha_composite(logo, (config.story_logo_x, config.story_logo_y))
    draw_preview_subtitle(base, config, timestamp)
    output = package_preview_frame(base, config.plate_image, config.video_box) if config.plate_image is not None else base
    output.convert("RGB").save(output_path)


def render_library_preview_frame(config: ReleaseConfig, watermark_png: Path, output_path: Path, work_dir: Path, timestamp: float) -> None:
    frame_path = work_dir / f"library_story_{int(round(timestamp)):03d}.png"
    extract_video_frame(config.bg_video, frame_path, timestamp)
    x, y, width, height = config.video_box
    story = scale_crop_image(Image.open(frame_path).convert("RGBA"), width, height)
    watermark = Image.open(watermark_png).convert("RGBA")
    wm_width = max(80, min(config.watermark_width, int(width * 0.28)))
    watermark = watermark.resize((wm_width, max(1, round(watermark.height * wm_width / max(1, watermark.width)))), Image.Resampling.LANCZOS)
    watermark.putalpha(watermark.getchannel("A").point(lambda value: round(value * config.watermark_opacity)))
    story.alpha_composite(watermark, (32, 40))
    story.alpha_composite(watermark, (max(0, width - watermark.width - 32), max(0, height - watermark.height - 40)))
    draw_preview_subtitle(
        story,
        config,
        timestamp,
        canvas_size=(width, height),
        font_size=max(22, round(config.subtitle_font_size * width / WIDE_WIDTH)),
        margin_v=max(22, round(config.subtitle_margin_v * height / WIDE_HEIGHT)),
    )
    canvas = Image.new("RGBA", (FINAL_WIDTH, FINAL_HEIGHT), (0, 0, 0, 255))
    canvas.alpha_composite(story, (x, y))
    if config.plate_image is not None:
        overlay = plate_overlay_image(config.plate_image, config.video_box, FINAL_WIDTH, FINAL_HEIGHT)
        canvas.alpha_composite(overlay)
    canvas.convert("RGB").save(output_path)


def package_preview_frame(wide_frame: Image.Image, plate_image: Path, video_box: tuple[int, int, int, int]) -> Image.Image:
    canvas = Image.new("RGBA", (FINAL_WIDTH, FINAL_HEIGHT), (0, 0, 0, 255))
    x, y, width, height = video_box
    center = scale_crop_image(wide_frame.convert("RGBA"), width, height)
    canvas.alpha_composite(center, (x, y))
    canvas.alpha_composite(plate_overlay_image(plate_image, video_box, FINAL_WIDTH, FINAL_HEIGHT))
    return canvas


def plate_overlay_image(plate_image: Path, video_box: tuple[int, int, int, int], width: int, height: int) -> Image.Image:
    x, y, box_w, box_h = video_box
    overlay = scale_crop_image(Image.open(plate_image).convert("RGBA"), width, height)
    pixels = overlay.load()
    for py in range(y, y + box_h):
        for px in range(x, x + box_w):
            red, green, blue, alpha = pixels[px, py]
            pixels[px, py] = (red, green, blue, 0)
    return overlay


def extract_video_frame(video: Path, output_path: Path, timestamp: float) -> Path:
    run_command(["ffmpeg", "-y", "-ss", f"{timestamp:.3f}", "-i", str(video), "-frames:v", "1", "-update", "1", str(output_path)])
    return output_path


def extract_person_frame(config: ReleaseConfig, video: Path, output_path: Path, timestamp: float) -> Path:
    filters: list[str] = []
    if config.person_crop is not None:
        x, y, width, height = config.person_crop
        filters.append(f"crop={width}:{height}:{x}:{y}")
    source = "[0:v]"
    if filters:
        source = "[person_crop]"
    grade = person_grade_filter(config)
    beauty = person_beauty_filter(config)
    if config.keyer == "chromakey":
        graph = ",".join(filters + ([beauty] if beauty else []) + [f"chromakey={config.chroma_color}:{config.chroma_similarity}:{config.chroma_blend}", "format=rgba"])
        if grade:
            graph += grade
        run_command(["ffmpeg", "-y", "-ss", f"{timestamp:.3f}", "-i", str(video), "-frames:v", "1", "-vf", graph, "-update", "1", str(output_path)])
    else:
        graph_parts: list[str] = []
        if filters:
            graph_parts.append(f"[0:v]{','.join(filters)}[person_crop]")
        beauty_chain = f"{beauty}," if beauty else ""
        graph_parts.extend(
            [
                f"{source}{beauty_chain}format=rgba,split[person_orig][person_keysrc]",
                f"[person_keysrc]colorkey={config.chroma_color}:{config.chroma_similarity}:{config.chroma_blend},alphaextract,erosion,dilation[person_mask]",
                f"[person_orig][person_mask]alphamerge,despill=type=green:mix=0.35{grade}[person_keyed]",
            ]
        )
        run_command(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-filter_complex",
                ";".join(graph_parts),
                "-map",
                "[person_keyed]",
                "-update",
                "1",
                str(output_path),
            ]
        )
    return output_path


def clamp_box(box: tuple[int, int, int, int], canvas_width: int, canvas_height: int) -> tuple[int, int, int, int]:
    x, y, width, height = box
    x = max(0, min(canvas_width - 1, x))
    y = max(0, min(canvas_height - 1, y))
    width = max(1, min(canvas_width - x, width))
    height = max(1, min(canvas_height - y, height))
    return x, y, width, height


def native_person_preview_layout(
    person: Image.Image,
    detected_bbox: tuple[int, int, int, int] | None,
    output_width: int,
    output_height: int,
) -> tuple[Image.Image, int, int]:
    """Reconstruct the original source composition without green edge bands."""
    source_width, source_height = person.size
    scale = min(output_width / source_width, output_height / source_height)
    canvas_x = round((output_width - source_width * scale) / 2)
    canvas_y = round((output_height - source_height * scale) / 2)
    if detected_bbox is None:
        resized = person.resize(
            (max(1, round(source_width * scale)), max(1, round(source_height * scale))),
            Image.Resampling.LANCZOS,
        )
        return resized, canvas_x, canvas_y
    x, y, width, height = clamp_box(detected_bbox, source_width, source_height)
    cropped = person.crop((x, y, x + width, y + height))
    cropped = cropped.resize(
        (max(1, round(width * scale)), max(1, round(height * scale))),
        Image.Resampling.LANCZOS,
    )
    return cropped, canvas_x + round(x * scale), canvas_y + round(y * scale)


def probe_video_size(path: Path) -> tuple[int, int]:
    process = subprocess.run(
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
        text=True,
        capture_output=True,
    )
    if process.returncode != 0:
        raise RuntimeError(f"无法读取人物视频尺寸：{path}: {process.stderr.strip()}")
    width_text, height_text = process.stdout.strip().split("x", 1)
    return int(width_text), int(height_text)


def probe_video_stream_duration(path: Path) -> float | None:
    """Read the duration of the first video stream, excluding any audio tail.

    ``format=duration`` is intentionally not used here: a muxed greenscreen
    file can carry an audio stream that outlives the actual image stream.  A
    missing/invalid duration is returned as ``None`` so callers can retain the
    normal ffmpeg EOF boundary instead of guessing.
    """
    process = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
        capture_output=True,
    )
    if process.returncode != 0:
        return None
    try:
        value = float(process.stdout.strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def probe_video_frame_duration(path: Path) -> float:
    """Return one frame's duration for a video, with a deterministic fallback."""
    process = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate,r_frame_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=0",
            str(path),
        ],
        text=True,
        capture_output=True,
    )
    if process.returncode == 0:
        rates: list[float] = []
        for line in process.stdout.splitlines():
            if "=" not in line:
                continue
            _, raw_rate = line.split("=", 1)
            try:
                if "/" in raw_rate:
                    numerator, denominator = raw_rate.split("/", 1)
                    rate = float(numerator) / float(denominator)
                else:
                    rate = float(raw_rate)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            if rate > 0:
                rates.append(rate)
        if rates:
            return 1.0 / max(rates)
    return 1.0 / 25.0


def person_tail_pad_seconds(
    person_duration: float | None,
    target_duration: float,
    frame_duration: float = 1.0 / 25.0,
) -> float:
    """How long to clone the last valid person frame before the output ends.

    One extra frame is included by default so a stream that ends exactly on a
    frame boundary cannot expose an EOF-generated black frame.  A source that
    is already long enough receives no pad.  The function is pure and is kept
    separate from ffprobe so it can be tested without media files.
    """
    if person_duration is None or target_duration <= 0 or person_duration >= target_duration:
        return 0.0
    safety_frame = max(0.0, float(frame_duration))
    return max(0.0, target_duration - max(0.0, person_duration)) + safety_frame


def native_person_filter_layout(
    source_path: Path,
    detected_bbox: tuple[int, int, int, int] | None,
    output_width: int,
    output_height: int,
) -> tuple[str, int, int]:
    source_width, source_height = probe_video_size(source_path)
    scale = min(output_width / source_width, output_height / source_height)
    canvas_x = round((output_width - source_width * scale) / 2)
    canvas_y = round((output_height - source_height * scale) / 2)
    if detected_bbox is None:
        return f"scale={max(1, round(source_width * scale))}:{max(1, round(source_height * scale))}", canvas_x, canvas_y
    x, y, width, height = clamp_box(detected_bbox, source_width, source_height)
    return (
        f"crop={width}:{height}:{x}:{y},scale={max(1, round(width * scale))}:{max(1, round(height * scale))}",
        canvas_x + round(x * scale),
        canvas_y + round(y * scale),
    )


def draw_preview_subtitle(
    image: Image.Image,
    config: ReleaseConfig,
    timestamp: float,
    canvas_size: tuple[int, int] = (WIDE_WIDTH, WIDE_HEIGHT),
    font_size: int | None = None,
    margin_v: int | None = None,
) -> None:
    if config.subtitle_srt is None or not config.subtitle_srt.exists():
        return
    text = ""
    for start, end, candidate in parse_srt(config.subtitle_srt):
        if start <= timestamp < end:
            text = candidate
            break
    if not text:
        return
    draw = ImageDraw.Draw(image)
    width, height = canvas_size
    font = load_cjk_font(font_size or config.subtitle_font_size)
    subtitle_margin = config.subtitle_margin_v if margin_v is None else margin_v
    stroke_width = 4
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    x = (width - (bbox[2] - bbox[0])) // 2
    y = height - subtitle_margin - (bbox[3] - bbox[1])
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255), stroke_width=stroke_width, stroke_fill=(0, 0, 0, 230))


def validate_config(config: ReleaseConfig) -> None:
    required_paths = [("bg_video", config.bg_video)]
    if config.variant in {"both", "main"}:
        required_paths.extend(
            [
                ("bg_image", config.bg_image),
                ("person_greenscreen", config.person_greenscreen),
                ("audio_mix", config.audio_mix),
            ]
        )
    for label, path in required_paths:
        if path is None:
            raise ValueError(f"缺少必要参数：--{label.replace('_', '-')}")
        if not path.exists():
            raise FileNotFoundError(f"{label} 不存在：{path}")
    for label, path in (
        ("watermark_logo", config.watermark_logo),
        ("antipiracy_logo", config.antipiracy_logo),
        ("plate_image", config.plate_image),
        ("frame_image", config.frame_image),
        ("frame_image_b", config.frame_image_b),
        ("story_logo", config.story_logo),
        ("subtitle_srt", config.subtitle_srt),
    ):
        if path is not None and not path.exists():
            raise FileNotFoundError(f"{label} 不存在：{path}")
    x, y, width, height = config.video_box
    if width <= 0 or height <= 0:
        raise ValueError("--video-box 的宽高必须大于 0")
    if x < 0 or y < 0 or x + width > FINAL_WIDTH or y + height > FINAL_HEIGHT:
        raise ValueError("--video-box 必须落在 1080x1440 画布内")
    if config.person_crop is not None:
        crop_x, crop_y, crop_width, crop_height = config.person_crop
        if min(crop_x, crop_y) < 0 or crop_width <= 0 or crop_height <= 0:
            raise ValueError("--person-crop 的 x/y 不能为负，宽高必须大于 0")
    if config.detected_person_bbox is not None:
        crop_x, crop_y, crop_width, crop_height = config.detected_person_bbox
        if min(crop_x, crop_y) < 0 or crop_width <= 0 or crop_height <= 0:
            raise ValueError("detected_person_bbox 的 x/y 不能为负，宽高必须大于 0")
    if config.person_height <= 0:
        raise ValueError("--person-height 必须大于 0")
    story_x, story_y, story_width, story_height = config.story_box
    if story_width <= 0 or story_height <= 0:
        raise ValueError("--story-box 的宽高必须大于 0")
    if story_x < 0 or story_y < 0 or story_x + story_width > WIDE_WIDTH or story_y + story_height > WIDE_HEIGHT:
        raise ValueError("--story-box 必须落在 1920x1080 主画布内")
    b_x, b_y, b_width, b_height = config.b_story_box
    if b_width <= 0 or b_height <= 0:
        raise ValueError("--b-story-box 的宽高必须大于 0")
    if b_x < 0 or b_y < 0 or b_x + b_width > WIDE_WIDTH or b_y + b_height > WIDE_HEIGHT:
        raise ValueError("--b-story-box 必须落在 1920x1080 主画布内")
    # B 景默认复用 A 景统一故事框；只在需要完全不同框图时传 --frame-image-b。
    validate_release_assets(config)


def _load_rgba_image(source: Image.Image | Path) -> Image.Image:
    """Load an image source without leaking an open file handle."""
    if isinstance(source, Image.Image):
        return source.convert("RGBA").copy()
    with Image.open(source) as image:
        return image.convert("RGBA").copy()


def _visible_alpha_mask(image: Image.Image, threshold: int = 24) -> Image.Image:
    return image.convert("RGBA").getchannel("A").point(lambda value: 255 if value > threshold else 0, mode="L")


def _composite_rgb_for_integrity(image: Image.Image, sample_limit: int = 256) -> Image.Image:
    """Flatten transparent pixels to white before dark-pixel analysis."""
    rgba = image.convert("RGBA")
    if max(rgba.size) > sample_limit:
        scale = sample_limit / max(rgba.size)
        rgba = rgba.resize(
            (max(1, round(rgba.width * scale)), max(1, round(rgba.height * scale))),
            Image.Resampling.BOX,
        )
    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    background.alpha_composite(rgba)
    return background.convert("RGB")


def _rgb_pixel_values(image: Image.Image):
    getter = getattr(image, "get_flattened_data", None)
    return getter() if getter is not None else image.getdata()


def _black_pixel_mask(image: Image.Image, threshold: int = 18) -> Image.Image:
    rgb = _composite_rgb_for_integrity(image)
    return Image.frombytes(
        "L",
        rgb.size,
        bytes(255 if max(pixel) <= threshold else 0 for pixel in _rgb_pixel_values(rgb)),
    )


def _black_components(mask: Image.Image) -> list[tuple[int, int, int, int, int]]:
    """Return connected black components as (area, x, y, width, height)."""
    width, height = mask.size
    pixels = mask.load()
    visited = bytearray(width * height)
    components: list[tuple[int, int, int, int, int]] = []
    for y in range(height):
        for x in range(width):
            index = y * width + x
            if visited[index] or pixels[x, y] == 0:
                continue
            visited[index] = 1
            queue = [(x, y)]
            area = 0
            min_x = max_x = x
            min_y = max_y = y
            while queue:
                current_x, current_y = queue.pop()
                area += 1
                min_x = min(min_x, current_x)
                max_x = max(max_x, current_x)
                min_y = min(min_y, current_y)
                max_y = max(max_y, current_y)
                for next_x, next_y in (
                    (current_x - 1, current_y),
                    (current_x + 1, current_y),
                    (current_x, current_y - 1),
                    (current_x, current_y + 1),
                ):
                    if not (0 <= next_x < width and 0 <= next_y < height):
                        continue
                    next_index = next_y * width + next_x
                    if not visited[next_index] and pixels[next_x, next_y] != 0:
                        visited[next_index] = 1
                        queue.append((next_x, next_y))
            components.append((area, min_x, min_y, max_x - min_x + 1, max_y - min_y + 1))
    return components


def release_plate_integrity_issues(
    source: Image.Image | Path,
    video_box: tuple[int, int, int, int] = DEFAULT_VIDEO_BOX,
    *,
    dark_threshold: int = 18,
    seam_fraction: float = 0.82,
    rectangle_area_fraction: float = 0.05,
) -> list[str]:
    """Detect black seams and large black rectangles in a release plate.

    The check intentionally operates on a downsampled copy, making it a fast
    deterministic gate before any full-video render. Transparent source pixels
    are flattened to white so alpha does not masquerade as a black seam.
    """
    image = _load_rgba_image(source)
    width, height = image.size
    issues: list[str] = []
    x, y, box_width, box_height = video_box
    if box_width <= 0 or box_height <= 0 or x < 0 or y < 0 or x + box_width > width or y + box_height > height:
        issues.append("plate_video_box_invalid: 视频窗口超出底板范围")

    rgb = _composite_rgb_for_integrity(image)
    mask = Image.frombytes(
        "L",
        rgb.size,
        bytes(255 if max(pixel) <= dark_threshold else 0 for pixel in _rgb_pixel_values(rgb)),
    )
    mask_pixels = mask.load()
    sample_width, sample_height = mask.size
    row_fraction = [
        sum(1 for px in range(sample_width) if mask_pixels[px, py]) / max(1, sample_width)
        for py in range(sample_height)
    ]
    column_fraction = [
        sum(1 for py in range(sample_height) if mask_pixels[px, py]) / max(1, sample_height)
        for px in range(sample_width)
    ]
    edge_rows = max(1, round(sample_height * 0.012))
    edge_columns = max(1, round(sample_width * 0.012))
    if any(value >= seam_fraction for value in row_fraction[:edge_rows] + row_fraction[-edge_rows:]):
        issues.append("plate_edge_black_seam: 底板外边缘存在连续黑接缝")
    if any(value >= seam_fraction for value in column_fraction[:edge_columns] + column_fraction[-edge_columns:]):
        issues.append("plate_edge_black_seam: 底板外边缘存在连续黑接缝")

    # Interior rows/columns are partition boundaries.  Downsampling can turn
    # a real 8-12px seam into one sampled pixel, so preserve a one-pixel run as
    # a deterministic failure signal rather than hiding it as anti-aliasing.
    interior_row_run = max(1, round(sample_height * 0.002))
    interior_col_run = max(1, round(sample_width * 0.002))
    if _has_fraction_run(row_fraction, seam_fraction, interior_row_run, edge_rows, sample_height - edge_rows):
        issues.append("plate_partition_black_seam: 底板分区之间存在连续黑接缝")
    if _has_fraction_run(column_fraction, seam_fraction, interior_col_run, edge_columns, sample_width - edge_columns):
        issues.append("plate_partition_black_seam: 底板分区之间存在连续黑接缝")

    for area, component_x, component_y, component_width, component_height in _black_components(_black_pixel_mask(image, dark_threshold)):
        component_area_fraction = area / max(1, sample_width * sample_height)
        bounding_area = component_width * component_height
        rectangularity = area / max(1, bounding_area)
        if (
            component_area_fraction >= rectangle_area_fraction
            and component_width >= sample_width * 0.15
            and component_height >= sample_height * 0.06
            and rectangularity >= 0.62
        ):
            issues.append(
                "plate_large_black_rectangle: "
                f"黑色区域约占底板 {component_area_fraction:.1%} "
                f"（x={component_x}, y={component_y}, w={component_width}, h={component_height}）"
            )
            break
    return issues


def plate_integrity_issues(
    source: Image.Image | Path,
    video_box: tuple[int, int, int, int] = DEFAULT_VIDEO_BOX,
    **kwargs,
) -> list[str]:
    """Backward/short-name wrapper for :func:`release_plate_integrity_issues`."""
    return release_plate_integrity_issues(source, video_box, **kwargs)


def _has_fraction_run(values: list[float], threshold: float, run: int, start: int, end: int) -> bool:
    count = 0
    for index, value in enumerate(values):
        if index < start or index >= end:
            continue
        if value >= threshold:
            count += 1
            if count >= run:
                return True
        else:
            count = 0
    return False


def story_frame_integrity_issues(
    source: Image.Image | Path,
    window: tuple[int, int, int, int],
    *,
    alpha_threshold: int = 24,
) -> list[str]:
    """Check that an A-scene frame has four visible corners and a closed outline."""
    original_mode = source.mode if isinstance(source, Image.Image) else None
    if original_mode is None:
        try:
            with Image.open(source) as source_image:
                original_mode = source_image.mode
        except OSError:
            return ["frame_unreadable: 无法读取故事框图片"]
    image = _load_rgba_image(source)
    issues: list[str] = []
    if original_mode != "RGBA":
        issues.append("frame_not_rgba: 故事框必须是RGBA透明图")
        return issues
    width, height = image.size
    x, y, box_width, box_height = window
    if box_width <= 0 or box_height <= 0 or x < 0 or y < 0 or x + box_width > width or y + box_height > height:
        return ["frame_window_invalid: 故事视频窗口超出画布范围"]
    alpha = _visible_alpha_mask(image, alpha_threshold)
    if alpha.getbbox() is None:
        return ["frame_empty: 故事框没有可见轮廓"]
    pixels = alpha.load()
    outer = max(24, min(120, round(min(box_width, box_height) * 0.18)))
    corners = {
        # Include the boundary pixel so a frame drawn exactly on the story
        # window (rather than outside it) is still treated as a valid corner.
        "top_left": (max(0, x - outer), max(0, y - outer), min(width, x + 1), min(height, y + 1)),
        "top_right": (max(0, x + box_width - 1), max(0, y - outer), min(width, x + box_width + outer), min(height, y + 1)),
        "bottom_left": (max(0, x - outer), max(0, y + box_height - 1), min(width, x + 1), min(height, y + box_height + outer)),
        "bottom_right": (max(0, x + box_width - 1), max(0, y + box_height - 1), min(width, x + box_width + outer), min(height, y + box_height + outer)),
    }
    missing_corners: list[str] = []
    corner_probe_radius = max(8, round(outer * 0.12))
    for label, (x1, y1, x2, y2) in corners.items():
        patch_width = max(1, x2 - x1)
        patch_height = max(1, y2 - y1)
        visible_points = [(px, py) for py in range(y1, y2) for px in range(x1, x2) if pixels[px, py]]
        visible = len(visible_points)
        ratio = visible / max(1, patch_width * patch_height)
        if label.endswith("left"):
            corner_x = x
        else:
            corner_x = x + box_width
        if label.startswith("top"):
            corner_y = y
        else:
            corner_y = y + box_height
        nearest_distance = min(
            (max(abs(px - corner_x), abs(py - corner_y)) for px, py in visible_points),
            default=outer + 1,
        )
        if ratio < 0.012 and nearest_distance > corner_probe_radius:
            missing_corners.append(label)
            issues.append(f"frame_corner_missing:{label}: A镜框{label}角缺失")
    if missing_corners:
        issues.append("frame_contour_open: A镜框四角未形成闭合轮廓")

    side_bands = {
        "top": (max(0, x - outer), max(0, y - outer), min(width, x + box_width + outer), min(height, y + max(1, outer // 2))),
        "bottom": (max(0, x - outer), max(0, y + box_height - max(1, outer // 2)), min(width, x + box_width + outer), min(height, y + box_height + outer)),
        "left": (max(0, x - outer), max(0, y - outer), min(width, x + max(1, outer // 2)), min(height, y + box_height + outer)),
        "right": (max(0, x + box_width - max(1, outer // 2)), max(0, y - outer), min(width, x + box_width + outer), min(height, y + box_height + outer)),
    }
    missing_sides: list[str] = []
    for label, (x1, y1, x2, y2) in side_bands.items():
        band_width = max(1, x2 - x1)
        band_height = max(1, y2 - y1)
        visible = sum(1 for py in range(y1, y2) for px in range(x1, x2) if pixels[px, py])
        if visible / max(1, band_width * band_height) < 0.004:
            missing_sides.append(label)
            issues.append(f"frame_side_missing:{label}: A镜框{label}边缺失")
            continue
        # A side can have enough decoration overall while still containing a
        # conspicuous gap.  Check interior segments separately to enforce a
        # genuinely closed contour rather than just four corner ornaments.
        segment_count = 10
        for segment_index in range(segment_count):
            if label in {"top", "bottom"}:
                segment_x1 = x + (box_width * segment_index) // segment_count
                segment_x2 = x + (box_width * (segment_index + 1)) // segment_count
                segment_box = (segment_x1, y1, max(segment_x1 + 1, segment_x2), y2)
            else:
                segment_y1 = y + (box_height * segment_index) // segment_count
                segment_y2 = y + (box_height * (segment_index + 1)) // segment_count
                segment_box = (x1, segment_y1, x2, max(segment_y1 + 1, segment_y2))
            sx1, sy1, sx2, sy2 = segment_box
            segment_width = max(1, sx2 - sx1)
            segment_height = max(1, sy2 - sy1)
            segment_visible = sum(1 for py in range(sy1, sy2) for px in range(sx1, sx2) if pixels[px, py])
            coordinate_distances: list[int] = []
            if label in {"top", "bottom"}:
                coordinate_range = range(sx1, sx2)
                for coordinate in coordinate_range:
                    distances = [
                        abs(py - (y if label == "top" else y + box_height))
                        for py in range(sy1, sy2)
                        if pixels[coordinate, py]
                    ]
                    coordinate_distances.append(min(distances, default=outer + 1))
            else:
                coordinate_range = range(sy1, sy2)
                for coordinate in coordinate_range:
                    distances = [
                        abs(px - (x if label == "left" else x + box_width))
                        for px in range(sx1, sx2)
                        if pixels[px, coordinate]
                    ]
                    coordinate_distances.append(min(distances, default=outer + 1))
            finite_distances = sorted(value for value in coordinate_distances if value <= outer)
            baseline_distance = finite_distances[len(finite_distances) // 2] if finite_distances else outer
            gap_limit = max(corner_probe_radius * 2, baseline_distance + corner_probe_radius)
            gap_run = 0
            has_gap = False
            for distance in coordinate_distances:
                if distance > gap_limit:
                    gap_run += 1
                    if gap_run >= max(4, len(coordinate_distances) // 20):
                        has_gap = True
                        break
                else:
                    gap_run = 0
            if segment_visible / max(1, segment_width * segment_height) < 0.001 or has_gap:
                issues.append(f"frame_contour_gap:{label}:{segment_index}: A镜框闭合轮廓存在缺口")
                if not any(item.startswith("frame_contour_open:") for item in issues):
                    issues.append("frame_contour_open: A镜框边缘未闭合")
                break
    if missing_sides and not any(item.startswith("frame_contour_open:") for item in issues):
        issues.append("frame_contour_open: A镜框边缘未闭合")
    return issues


def frame_integrity_issues(
    source: Image.Image | Path,
    window: tuple[int, int, int, int],
    **kwargs,
) -> list[str]:
    """Short-name wrapper for A-scene frame integrity checks."""
    return story_frame_integrity_issues(source, window, **kwargs)


def validate_release_assets(config: ReleaseConfig, *, frame_image: Path | None = None) -> None:
    """Fail before rendering when a plate or story frame is structurally bad."""
    issues: list[str] = []
    if config.plate_image is not None:
        issues.extend(release_plate_integrity_issues(config.plate_image, config.video_box))
    candidate_frame = frame_image or config.frame_image
    if candidate_frame is not None:
        issues.extend(story_frame_integrity_issues(candidate_frame, config.story_box))
    if config.frame_image_b is not None and config.b_windows:
        issues.extend(story_frame_integrity_issues(config.frame_image_b, config.b_story_box))
    if issues:
        raise ValueError("发布素材未通过机器完整性检查，已阻止全片渲染：\n" + "\n".join(f"- {issue}" for issue in issues))


def render_static_assets(config: ReleaseConfig, work_dir: Path) -> dict[str, Path]:
    frame = config.frame_image or render_default_frame(work_dir / "default_frame.png")
    # Validate the generated default frame too.  This keeps the same preflight
    # gate for hand-supplied and built-in A-scene frames.
    validate_release_assets(config, frame_image=frame)
    main_top = render_top_panel(
        work_dir / "main_top.png",
        "故事表演",
        config.story_name,
        config.duration_text,
        accent=(67, 143, 62),
    )
    main_bottom = render_bottom_panel(
        work_dir / "main_bottom.png",
        [
            "背景视频 + PPT + 配乐",
            "文稿 + 标注",
            "示范视频",
        ],
        accent=(67, 143, 62),
    )
    library_top = render_top_panel(
        work_dir / "library_top.png",
        "儿童故事",
        config.story_name,
        config.duration_text,
        accent=(214, 88, 70),
    )
    library_bottom = render_bottom_panel(
        work_dir / "library_bottom.png",
        [
            "背景视频 + PPT + 配乐",
            "文稿 + 标注",
            "示范视频",
        ],
        accent=(214, 88, 70),
    )
    library_watermark = config.antipiracy_logo or render_watermark_png(
        work_dir / "library_watermark.png",
        config.library_watermark_text,
    )
    tail_notice = render_tail_notice_png(
        work_dir / "tail_notice.png",
        config.tail_notice_text,
    )
    return {
        "frame": frame,
        "main_top": main_top,
        "main_bottom": main_bottom,
        "library_top": library_top,
        "library_bottom": library_bottom,
        "library_watermark": library_watermark,
        "tail_notice": tail_notice,
    }


def render_main_wide(config: ReleaseConfig, frame_image: Path, output_path: Path) -> None:
    assert config.bg_image is not None
    assert config.person_greenscreen is not None
    assert config.audio_mix is not None
    duration = probe_duration(config.audio_mix)
    # A greenscreen source can be a few frames shorter than the narration.  A
    # raw EOF frame is not safe to composite: ffmpeg may materialize it as an
    # opaque black rectangle before chroma-keying.  Pad the source with a clone
    # of its last *valid* frame and make the person overlay non-repeating as a
    # second defensive boundary.
    person_duration = probe_video_stream_duration(config.person_greenscreen)
    person_frame_duration = probe_video_frame_duration(config.person_greenscreen)
    person_tail_pad = person_tail_pad_seconds(person_duration, duration, person_frame_duration)
    scale = config.output_scale
    wide_width = WIDE_WIDTH * scale
    wide_height = WIDE_HEIGHT * scale
    work_dir = output_path.parent
    frame_a_path, mask_a_path, story_a_bbox = prepare_story_frame_assets(
        frame_image,
        config.story_box,
        work_dir / "story_frame_a_prepared.png",
        work_dir / "story_mask_a.png",
        config.story_bleed,
    )
    frame_b_path = None
    mask_b_path = None
    story_b_bbox = None
    if config.b_windows:
        frame_b_path, mask_b_path, story_b_bbox = prepare_story_frame_assets(
            config.frame_image_b or frame_image,
            config.b_story_box,
            work_dir / "story_frame_b_prepared.png",
            work_dir / "story_mask_b.png",
            config.story_bleed,
        )
    args = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        str(config.bg_image),
        "-stream_loop",
        "-1",
        "-i",
        str(config.bg_video),
        "-i",
        str(config.person_greenscreen),
        "-loop",
        "1",
        "-i",
        str(frame_a_path),
        "-loop",
        "1",
        "-i",
        str(mask_a_path),
    ]
    frame_index = 3
    mask_index = 4
    frame_b_index = None
    mask_b_index = None
    if frame_b_path is not None and mask_b_path is not None:
        frame_b_index = 5
        mask_b_index = 6
        args.extend(["-loop", "1", "-i", str(frame_b_path), "-loop", "1", "-i", str(mask_b_path)])
    watermark_index = None
    if config.watermark_logo is not None:
        watermark_index = len(args_input_paths(args))
        args.extend(["-loop", "1", "-i", str(config.watermark_logo)])
    story_logo_index = None
    if config.story_logo is not None:
        story_logo_index = len(args_input_paths(args))
        args.extend(["-loop", "1", "-i", str(config.story_logo)])
    subtitle_index = None
    if config.subtitle_srt is not None:
        subtitle_index = len(args_input_paths(args))
        subtitle_overlay = output_path.parent / "main_subtitle_overlay.mov"
        render_subtitle_overlay_video(
            srt_path=config.subtitle_srt,
            output_path=subtitle_overlay,
            duration=duration,
            width=wide_width,
            height=wide_height,
            font_size=config.subtitle_font_size * scale,
            margin_v=config.subtitle_margin_v * scale,
            stroke_width=max(4, 4 * scale),
        )
        args.extend(["-i", str(subtitle_overlay)])
    audio_index = len(args_input_paths(args))
    args.extend(["-i", str(config.audio_mix)])

    person_source = "[2:v]"
    if config.person_crop is not None:
        crop_x, crop_y, crop_width, crop_height = config.person_crop
        filters_prefix = []
        if person_tail_pad > 0:
            filters_prefix.append(
                f"[2:v]tpad=stop_mode=clone:stop_duration={person_tail_pad:.6f},"
                f"setpts=PTS-STARTPTS[person_padded]"
            )
            person_source = "[person_padded]"
        filters_prefix.append(
            f"{person_source}crop={crop_width}:{crop_height}:{crop_x}:{crop_y},setsar=1[person_in]"
        )
        person_source = "[person_in]"
    else:
        filters_prefix = []
        if person_tail_pad > 0:
            filters_prefix.append(
                f"[2:v]tpad=stop_mode=clone:stop_duration={person_tail_pad:.6f},"
                f"setpts=PTS-STARTPTS[person_padded]"
            )
            person_source = "[person_padded]"

    has_b = bool(config.b_windows)
    has_c = bool(config.c_windows)
    filters = filters_prefix + [
        f"[0:v]scale={wide_width}:{wide_height}:force_original_aspect_ratio=increase,"
        f"crop={wide_width}:{wide_height},setsar=1,format=rgba[base_src]",
    ]
    if config.background_blur > 0:
        filters.append(f"[base_src]boxblur={config.background_blur}:1[base0]")
    else:
        filters.append("[base_src]null[base0]")
    base_labels = ["base_a"] + (["base_b"] if has_b else []) + (["base_c"] if has_c else [])
    if len(base_labels) > 1:
        filters.append(f"[base0]split={len(base_labels)}" + "".join(f"[{label}]" for label in base_labels))
    else:
        filters.append("[base0]null[base_a]")
    if has_b:
        filters.append("[1:v]split=2[story_src_a][story_src_b]")
        story_a_source = "[story_src_a]"
    else:
        story_a_source = "[1:v]"
    story_x, story_y, story_width, story_height = scaled_box(story_a_bbox, scale)
    filters.extend(
        [
            f"{story_a_source}scale={story_width}:{story_height}:force_original_aspect_ratio=increase,"
            f"crop={story_width}:{story_height},setsar=1,format=rgba[story_rect]",
            f"color=c=0x000000@0.0:s={wide_width}x{wide_height}:d={duration:.3f},format=rgba[story_canvas]",
            f"[story_canvas][story_rect]overlay={story_x}:{story_y}[story_layer]",
            f"[{mask_index}:v]scale={wide_width}:{wide_height},format=gray[story_mask]",
            "[story_layer][story_mask]alphamerge[story_masked]",
            "[base_a][story_masked]overlay=0:0[withstory]",
        ]
    )
    filters.extend(person_key_filters(config, person_source))
    if has_c:
        filters.append("[person_keyed]split=2[person_keyed_a][person_keyed_c]")
    else:
        filters.append("[person_keyed]null[person_keyed_a]")
    a_subject_filter = ""
    if config.detected_person_bbox is not None and config.person_crop is None:
        source_width, source_height = probe_video_size(config.person_greenscreen)
        crop_x, crop_y, crop_width, crop_height = clamp_box(config.detected_person_bbox, source_width, source_height)
        a_subject_filter = f"crop={crop_width}:{crop_height}:{crop_x}:{crop_y},"
    filters.extend(
        [
            f"[{frame_index}:v]scale={wide_width}:{wide_height},setsar=1,format=rgba[frame]",
            "[withstory][frame]overlay=0:0[framed]",
            f"[person_keyed_a]{a_subject_filter}scale=-1:{config.person_height * scale},setsar=1,format=rgba[person]",
            f"[framed][person]overlay=min({config.person_x * scale}\\,W-w):min({config.person_y * scale}\\,H-h):"
            "eof_action=pass:repeatlast=0[withperson]",
        ]
    )
    current = "withperson"
    if watermark_index is not None:
        filters.append(f"[{watermark_index}:v]scale=190:-1,format=rgba[logo]")
        filters.append(f"[{current}][logo]overlay=W-w-44:42[branded]")
        current = "branded"
    if has_b:
        assert story_b_bbox is not None
        b_x, b_y, b_width, b_height = scaled_box(story_b_bbox, scale)
        assert frame_b_index is not None
        assert mask_b_index is not None
        filters.extend(
            [
                f"[story_src_b]scale={b_width}:{b_height}:force_original_aspect_ratio=increase,"
                f"crop={b_width}:{b_height},setsar=1,format=rgba[story_b_rect]",
                f"color=c=0x000000@0.0:s={wide_width}x{wide_height}:d={duration:.3f},format=rgba[story_b_canvas]",
                f"[story_b_canvas][story_b_rect]overlay={b_x}:{b_y}[story_b_layer]",
                f"[{mask_b_index}:v]scale={wide_width}:{wide_height},format=gray[story_b_mask]",
                "[story_b_layer][story_b_mask]alphamerge[story_b_masked]",
                "[base_b][story_b_masked]overlay=0:0[b_story]",
                f"[{frame_b_index}:v]scale={wide_width}:{wide_height},setsar=1,format=rgba[frame_b]",
                "[b_story][frame_b]overlay=0:0[b_framed]",
            ]
        )
        b_current = "b_framed"
        b_expr = "+".join(f"between(T\\,{start:.3f}\\,{end:.3f})" for start, end in config.b_windows)
        filters.append(f"[{current}][{b_current}]blend=all_expr='if({b_expr},B,A)'[ab_scene]")
        current = "ab_scene"
    if has_c:
        c_person_filter, c_person_x, c_person_y = native_person_filter_layout(
            config.person_greenscreen,
            config.detected_person_bbox if config.person_crop is None else None,
            wide_width,
            wide_height,
        )
        filters.extend(
            [
                f"[person_keyed_c]{c_person_filter},setsar=1,format=rgba[person_c]",
                f"[base_c][person_c]overlay={c_person_x}:{c_person_y}[c_person]",
            ]
        )
        c_expr = "+".join(f"between(T\\,{start:.3f}\\,{end:.3f})" for start, end in config.c_windows)
        filters.append(f"[{current}][c_person]blend=all_expr='if({c_expr},B,A)'[abc_scene]")
        current = "abc_scene"
    if story_logo_index is not None:
        filters.append(f"[{story_logo_index}:v]scale={config.story_logo_width_a * scale}:-1,format=rgba[story_logo]")
        filters.append(f"[{current}][story_logo]overlay={config.story_logo_x * scale}:{config.story_logo_y * scale}[with_story_logo]")
        current = "with_story_logo"
    if subtitle_index is not None:
        filters.append(f"[{subtitle_index}:v]format=rgba[subtitle_overlay]")
        filters.append(f"[{current}][subtitle_overlay]overlay=0:0[with_subtitles]")
        current = "with_subtitles"
    if config.mix_bg_audio:
        filters.extend(
            [
                f"[{audio_index}:a]volume={config.voice_volume:.3f},atrim=0:{duration:.3f},"
                f"apad=whole_dur={duration:.3f},asetpts=PTS-STARTPTS[voice]",
                f"[1:a]volume={config.bg_audio_volume:.3f},atrim=0:{duration:.3f},"
                f"apad=whole_dur={duration:.3f},asetpts=PTS-STARTPTS[music]",
                "[voice][music]amix=inputs=2:duration=first:dropout_transition=0,alimiter=limit=0.94[a]",
            ]
        )
    else:
        filters.append(
            f"[{audio_index}:a]atrim=0:{duration:.3f},apad=whole_dur={duration:.3f},asetpts=PTS-STARTPTS[a]"
        )
    args.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            f"[{current}]",
            "-map",
            "[a]",
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            config.preset,
            "-crf",
            str(config.crf),
            "-pix_fmt",
            "yuv420p",
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    run_command(args)


def person_key_filters(config: ReleaseConfig, source_label: str) -> list[str]:
    beauty = person_beauty_filter(config)
    beauty_chain = f"{beauty}," if beauty else ""
    if config.keyer == "chromakey":
        return [
            f"{source_label}{beauty_chain}chromakey={config.chroma_color}:{config.chroma_similarity}:{config.chroma_blend},"
            f"format=rgba{person_grade_filter(config)}[person_keyed]"
        ]
    return [
        f"{source_label}{beauty_chain}format=rgba,split[person_orig][person_keysrc]",
        f"[person_keysrc]colorkey={config.chroma_color}:{config.chroma_similarity}:{config.chroma_blend},"
        "alphaextract,erosion,dilation[person_mask]",
        f"[person_orig][person_mask]alphamerge,despill=type=green:mix=0.35{person_grade_filter(config)}[person_keyed]",
    ]


def person_grade_filter(config: ReleaseConfig) -> str:
    if config.person_grade == "natural":
        return ",eq=contrast=1.05:saturation=1.07:brightness=0.01:gamma=0.99"
    if config.person_grade == "log-soft":
        return ",eq=contrast=1.18:saturation=1.25:brightness=0.03:gamma=0.96"
    if config.person_grade == "log-strong":
        return ",eq=contrast=1.30:saturation=1.35:brightness=0.04:gamma=0.92"
    return ""


def person_beauty_filter(config: ReleaseConfig) -> str:
    if config.person_beauty == "light":
        return "hqdn3d=1.2:1.0:3.0:2.0,unsharp=5:5:0.18:5:5:0.0"
    return ""


def render_subtitle_overlay_video(
    srt_path: Path,
    output_path: Path,
    duration: float,
    width: int,
    height: int,
    font_size: int,
    margin_v: int,
    stroke_width: int,
    fps: int = 25,
) -> Path:
    subtitles = parse_srt(srt_path)
    frames_dir = output_path.parent / "subtitle_frames"
    ensure_dir(frames_dir)
    font = load_cjk_font(font_size)
    total_frames = max(1, int(duration * fps + 0.999))
    active_index = 0

    for frame_no in range(total_frames):
        t = frame_no / fps
        while active_index < len(subtitles) and subtitles[active_index][1] <= t:
            active_index += 1
        text = ""
        if active_index < len(subtitles):
            start, end, candidate = subtitles[active_index]
            if start <= t < end:
                text = candidate

        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        if text:
            draw = ImageDraw.Draw(image)
            bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            x = (width - text_width) // 2
            y = height - margin_v - text_height
            draw.text((x, y), text, font=font, fill=(255, 255, 255, 255), stroke_width=stroke_width, stroke_fill=(0, 0, 0, 230))
        image.save(frames_dir / f"subtitle_{frame_no:05d}.png")

    run_command(
        [
            "ffmpeg",
            "-y",
            "-framerate",
            str(fps),
            "-i",
            str(frames_dir / "subtitle_%05d.png"),
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "qtrle",
            "-pix_fmt",
            "argb",
            str(output_path),
        ]
    )
    return output_path


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    blocks = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip().split("\n\n")
    entries: list[tuple[float, float, str]] = []
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if len(lines) < 2:
            continue
        time_line = lines[1] if lines[0].isdigit() and len(lines) > 1 else lines[0]
        if "-->" not in time_line:
            continue
        start_raw, end_raw = [part.strip() for part in time_line.split("-->", 1)]
        text_lines = lines[2:] if lines[0].isdigit() else lines[1:]
        text = " ".join(text_lines).strip()
        if text:
            entries.append((parse_srt_timestamp(start_raw), parse_srt_timestamp(end_raw), text))
    return entries


def parse_srt_timestamp(value: str) -> float:
    hours_raw, minutes_raw, seconds_raw = value.replace(",", ".").split(":")
    return int(hours_raw) * 3600 + int(minutes_raw) * 60 + float(seconds_raw)


def load_cjk_font(size: int) -> ImageFont.FreeTypeFont:
    for font_path in (
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
    ):
        path = Path(font_path)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default(size=size)


def render_vertical_package(
    source_video: Path,
    top_panel: Path,
    bottom_panel: Path,
    output_path: Path,
    duration: float,
    config: ReleaseConfig,
    tail_notice_png: Path | None = None,
    output_scale: int | None = None,
) -> None:
    scale = max(1, output_scale if output_scale is not None else config.output_scale)
    final_width = FINAL_WIDTH * scale
    final_height = FINAL_HEIGHT * scale
    top_height = TOP_HEIGHT * scale
    center_height = CENTER_HEIGHT * scale
    bottom_height = BOTTOM_HEIGHT * scale
    filters = (
        f"color=c=0xFFF7DF:s={final_width}x{final_height}:d={duration:.3f}[base];"
        f"[0:v]scale={final_width}:{center_height}:force_original_aspect_ratio=decrease,"
        f"pad={final_width}:{center_height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=rgba[center];"
        f"[1:v]scale={final_width}:{top_height},format=rgba[top];"
        f"[2:v]scale={final_width}:{bottom_height},format=rgba[bottom];"
        "[base][top]overlay=0:0[v1];"
        f"[v1][center]overlay=0:{top_height}[v2];"
        f"[v2][bottom]overlay=0:{top_height + center_height}[packaged]"
    )
    command = [
            "ffmpeg",
            "-y",
            "-i",
            str(source_video),
            "-loop",
            "1",
            "-i",
            str(top_panel),
            "-loop",
            "1",
            "-i",
            str(bottom_panel),
    ]
    if tail_notice_png is not None:
        tail_start = max(0.0, duration - min(3.0, resolved_tail_seconds(duration, config.tail_seconds)))
        command.extend(["-loop", "1", "-i", str(tail_notice_png)])
        filters += (
            f";[3:v]scale={int(final_width * 0.82)}:-1,format=rgba[tail_notice];"
            f"[packaged][tail_notice]overlay=x=(W-w)/2:y=(H-h)/2:enable='gte(t,{tail_start:.3f})'[v]"
        )
    else:
        filters += ";[packaged]null[v]"
    command.extend(
        [
            "-filter_complex",
            filters,
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            config.preset,
            "-crf",
            str(config.crf),
            "-pix_fmt",
            "yuv420p",
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
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


def render_library_window_video(
    source_video: Path,
    watermark_png: Path,
    tail_notice_png: Path,
    output_path: Path,
    config: ReleaseConfig,
) -> None:
    duration = probe_duration(source_video)
    _, _, video_width, video_height = config.video_box
    tail_seconds = resolved_tail_seconds(duration, config.tail_seconds)
    tail_start = max(0.0, duration - tail_seconds)
    notice_width = max(240, min(video_width - 120, int(video_width * 0.78)))
    watermark_width = max(80, min(config.watermark_width, int(video_width * 0.28)))
    opacity = max(0.0, min(1.0, config.watermark_opacity))
    speed_x = 70.0 * config.watermark_speed
    speed_y = 42.0 * config.watermark_speed
    args = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_video),
        "-loop",
        "1",
        "-i",
        str(watermark_png),
        "-loop",
        "1",
        "-i",
        str(tail_notice_png),
    ]
    subtitle_index = None
    if config.subtitle_srt is not None:
        subtitle_index = len(args_input_paths(args))
        subtitle_overlay = output_path.parent / "_release_work" / "library_subtitle_overlay.mov"
        render_subtitle_overlay_video(
            srt_path=config.subtitle_srt,
            output_path=subtitle_overlay,
            duration=duration,
            width=video_width,
            height=video_height,
            font_size=max(22, round(config.subtitle_font_size * video_width / WIDE_WIDTH)),
            margin_v=max(22, round(config.subtitle_margin_v * video_height / WIDE_HEIGHT)),
            stroke_width=3,
        )
        args.extend(["-i", str(subtitle_overlay)])
    audio_index = None
    if config.audio_mix is not None:
        audio_index = len(args_input_paths(args))
        args.extend(["-i", str(config.audio_mix)])

    wm1_x, wm1_y, wm2_x, wm2_y = safe_watermark_motion_expressions(speed_x, speed_y)
    filters = [
        f"[0:v]scale={video_width}:{video_height}:force_original_aspect_ratio=increase,"
        f"crop={video_width}:{video_height},setsar=1,format=rgba[base]",
        "[base]split=2[clean][blur_src]",
        "[blur_src]boxblur=18:1[blurred]",
        f"[clean][blurred]overlay=0:0:enable='gte(t,{tail_start:.3f})'[tail]",
        f"[1:v]scale={watermark_width}:-1,format=rgba,colorchannelmixer=aa={opacity:.3f}[wm]",
        "[wm]split=2[wm1][wm2]",
        f"[tail][wm1]overlay=x='{wm1_x}':y='{wm1_y}':enable='lt(t,{tail_start:.3f})'[w1]",
        f"[w1][wm2]overlay=x='{wm2_x}':y='{wm2_y}':enable='lt(t,{tail_start:.3f})'[w2]",
    ]
    current = "w2"
    if subtitle_index is not None:
        filters.extend(
            [
                f"[{subtitle_index}:v]format=rgba[subtitle_overlay]",
                f"[{current}][subtitle_overlay]overlay=0:0[with_subtitles]",
            ]
        )
        current = "with_subtitles"
    filters.extend(
        [
            f"[2:v]scale={notice_width}:-1,format=rgba[notice]",
            f"[{current}][notice]overlay=x=(W-w)/2:y=(H-h)/2:enable='gte(t,{tail_start:.3f})'[v]",
        ]
    )
    args.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[v]",
            "-map",
            f"{audio_index}:a" if audio_index is not None else "0:a?",
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            config.preset,
            "-crf",
            str(config.crf),
            "-pix_fmt",
            "yuv420p",
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    run_command(args)


def render_plate_package(
    source_video: Path,
    plate_image: Path,
    output_path: Path,
    config: ReleaseConfig,
    tail_notice_png: Path | None = None,
    output_scale: int | None = None,
) -> None:
    duration = probe_duration(source_video)
    scale = max(1, output_scale if output_scale is not None else config.output_scale)
    final_width = FINAL_WIDTH * scale
    final_height = FINAL_HEIGHT * scale
    x, y, width, height = scaled_box(config.video_box, scale)
    plate_overlay = output_path.parent / "_release_work" / "plate_overlay_cutout.png"
    create_plate_overlay_cutout(plate_image, plate_overlay, (x, y, width, height), final_width, final_height)
    filters = (
        f"color=c=0x000000:s={final_width}x{final_height}:d={duration:.3f},format=rgba[base];"
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,format=rgba[window];"
        f"[base][window]overlay={x}:{y}[under];"
        f"[1:v]scale={final_width}:{final_height}:force_original_aspect_ratio=increase,"
        f"crop={final_width}:{final_height},setsar=1,format=rgba[plate];"
        f"[under][plate]overlay=0:0[packaged]"
    )
    command = [
            "ffmpeg",
            "-y",
            "-i",
            str(source_video),
            "-loop",
            "1",
            "-i",
            str(plate_overlay),
    ]
    if tail_notice_png is not None:
        tail_start = max(0.0, duration - min(3.0, resolved_tail_seconds(duration, config.tail_seconds)))
        command.extend(["-loop", "1", "-i", str(tail_notice_png)])
        filters += (
            f";[2:v]scale={int(final_width * 0.82)}:-1,format=rgba[tail_notice];"
            f"[packaged][tail_notice]overlay=x=(W-w)/2:y=(H-h)/2:enable='gte(t,{tail_start:.3f})'[v]"
        )
    else:
        filters += ";[packaged]null[v]"
    command.extend(
        [
            "-filter_complex",
            filters,
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            config.preset,
            "-crf",
            str(config.crf),
            "-pix_fmt",
            "yuv420p",
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
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


def create_plate_overlay_cutout(
    plate_image: Path,
    output_path: Path,
    video_box: tuple[int, int, int, int],
    final_width: int = FINAL_WIDTH,
    final_height: int = FINAL_HEIGHT,
) -> Path:
    """Make the generated plate usable as a foreground layer.

    The middle video box is not part of the generated plate design. It is always
    punched fully transparent so any generated detail that wanders into the video
    area cannot cover the real story video.
    """
    x, y, width, height = video_box
    image = Image.open(plate_image).convert("RGBA")
    image = scale_crop_image(image, final_width, final_height)
    pixels = image.load()

    for py in range(y, y + height):
        for px in range(x, x + width):
            red, green, blue, alpha = pixels[px, py]
            pixels[px, py] = (red, green, blue, 0)

    image.save(output_path)
    return output_path


def scale_crop_image(image: Image.Image, width: int, height: int) -> Image.Image:
    source_width, source_height = image.size
    scale = max(width / source_width, height / source_height)
    resized = image.resize((round(source_width * scale), round(source_height * scale)), Image.Resampling.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def render_default_frame(output_path: Path) -> Path:
    image = Image.new("RGBA", (WIDE_WIDTH, WIDE_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    outer = (STORY_BOX_X - 58, STORY_BOX_Y - 58, STORY_BOX_X + STORY_BOX_W + 58, STORY_BOX_Y + STORY_BOX_H + 58)
    inner = (STORY_BOX_X - 6, STORY_BOX_Y - 6, STORY_BOX_X + STORY_BOX_W + 6, STORY_BOX_Y + STORY_BOX_H + 6)
    draw.rounded_rectangle(outer, radius=60, fill=(236, 190, 120, 255), outline=(105, 70, 36, 255), width=8)
    draw.rounded_rectangle(inner, radius=28, fill=(0, 0, 0, 0), outline=(255, 244, 210, 255), width=16)
    for x, y in ((outer[0], outer[1]), (outer[2], outer[1]), (outer[0], outer[3]), (outer[2], outer[3])):
        draw.ellipse((x - 32, y - 32, x + 32, y + 32), fill=(255, 219, 128, 255), outline=(105, 70, 36, 255), width=6)
    title_font = load_font(54)
    draw.text((150, 120), "绵羊姐姐讲故事", font=title_font, fill=(255, 241, 184, 255), stroke_width=4, stroke_fill=(72, 116, 58, 255))
    image.save(output_path)
    return output_path


def render_top_panel(output_path: Path, label: str, story_name: str, duration_text: str, accent: tuple[int, int, int]) -> Path:
    image = Image.new("RGBA", (FINAL_WIDTH, TOP_HEIGHT), (253, 248, 230, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, FINAL_WIDTH, TOP_HEIGHT), fill=(239, 251, 241, 255))
    draw.rounded_rectangle((255, 34, 825, 102), radius=26, fill=(255, 236, 172, 255), outline=(135, 96, 45, 255), width=3)
    draw.text((FINAL_WIDTH / 2, 68), label, font=load_font(52), anchor="mm", fill=(86, 57, 28, 255), stroke_width=2, stroke_fill=(255, 255, 255, 255))
    title = story_name if story_name.startswith("《") else f"《{story_name}》"
    fit_text(draw, title, (FINAL_WIDTH / 2, 185), max_width=930, max_size=92, min_size=58, fill=accent)
    draw.text((FINAL_WIDTH / 2, 282), f"时长：{duration_text}", font=load_font(48), anchor="mm", fill=accent, stroke_width=3, stroke_fill=(255, 248, 210, 255))
    image.save(output_path)
    return output_path


def render_bottom_panel(output_path: Path, lines: list[str], accent: tuple[int, int, int]) -> Path:
    image = Image.new("RGBA", (FINAL_WIDTH, BOTTOM_HEIGHT), (255, 248, 218, 255))
    draw = ImageDraw.Draw(image)
    margin = 54
    draw.rounded_rectangle((margin, 44, FINAL_WIDTH - margin, BOTTOM_HEIGHT - 44), radius=28, fill=(255, 245, 202, 255), outline=(132, 83, 39, 255), width=6)
    draw.line((105, 86, FINAL_WIDTH - 105, 86), fill=(199, 139, 59, 255), width=8)
    font = load_font(54)
    y = 150
    for line in lines:
        draw.text((142, y), "•", font=font, fill=accent, anchor="lm")
        wrapped = wrap_text(draw, line, font, 790)
        for part in wrapped:
            draw.text((190, y), part, font=font, fill=(62, 45, 28, 255), anchor="lm")
            y += 64
        y += 18
    image.save(output_path)
    return output_path


def render_watermark_png(output_path: Path, text: str) -> Path:
    font = load_font(42)
    padding_x = 34
    padding_y = 18
    dummy = Image.new("RGBA", (1, 1))
    bbox = ImageDraw.Draw(dummy).textbbox((0, 0), text, font=font, stroke_width=2)
    width = bbox[2] - bbox[0] + padding_x * 2
    height = bbox[3] - bbox[1] + padding_y * 2
    image = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, width, height), radius=height // 2, fill=(255, 255, 255, 130), outline=(68, 120, 190, 180), width=3)
    draw.text((padding_x, padding_y - bbox[1]), text, font=font, fill=(34, 82, 132, 230), stroke_width=2, stroke_fill=(255, 255, 255, 230))
    image.save(output_path)
    return output_path


def render_tail_notice_png(output_path: Path, text: str) -> Path:
    font = load_font(50)
    dummy = Image.new("RGBA", (1, 1))
    bbox = ImageDraw.Draw(dummy).textbbox((0, 0), text, font=font, stroke_width=2)
    width = bbox[2] - bbox[0] + 72
    height = bbox[3] - bbox[1] + 50
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, width, height), radius=20, fill=(0, 0, 0, 170))
    draw.text((36, 25 - bbox[1]), text, font=font, fill=(255, 255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0, 255))
    image.save(output_path)
    return output_path


def resolved_tail_seconds(duration: float, configured_tail_seconds: float) -> float:
    if configured_tail_seconds > 0:
        return min(duration, configured_tail_seconds)
    if duration <= 1:
        return duration
    return min(duration, max(30.0, min(50.0, duration / 5.0)))


def tail_probe_timestamps(duration: float, fps: int = 12, window_seconds: float = 2.0) -> tuple[float, ...]:
    """Return dense timestamps covering the final window plus a safe final frame."""
    duration = max(0.0, float(duration))
    fps = max(1, int(fps))
    window = max(0.0, min(float(window_seconds), duration))
    if duration <= 0:
        return (0.0,)
    start = max(0.0, duration - window)
    step = 1.0 / fps
    values: list[float] = []
    current = start
    # Keep the last timestamp strictly before EOF.  The final frame command
    # below probes the same safe point explicitly, even when fps rounding would
    # otherwise skip it.
    while current < duration - 1e-9:
        values.append(round(current, 6))
        current += step
    # Leave one frame period before EOF; codecs commonly place their last
    # decodable frame there even when the container duration rounds up.
    final = max(0.0, duration - max(step, 0.001))
    if not values or abs(values[-1] - final) > 1e-6:
        values.append(round(final, 6))
    return tuple(values)


def build_tail_frame_probe_commands(
    video: Path,
    output_dir: Path,
    duration: float,
    *,
    fps: int = 12,
    window_seconds: float = 2.0,
) -> tuple[list[str], list[str]]:
    """Build ffmpeg commands for dense tail sampling and a safe terminal frame.

    The first command samples the complete final two seconds at ``fps``.  The
    second command seeks to a timestamp just before EOF and writes one frame,
    so a QA caller can detect a terminal black block instead of checking only
    ``duration - 0.5``.
    """
    duration = max(0.0, float(duration))
    fps = max(1, int(fps))
    window = max(0.0, min(float(window_seconds), duration))
    start = max(0.0, duration - window)
    safe_final = max(0.0, duration - max(1.0 / fps, 0.001))
    tail_pattern = str(output_dir / "tail_%04d.jpg")
    final_path = str(output_dir / "tail_final.jpg")
    tail_command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(video),
        "-t",
        f"{window:.3f}",
        "-vf",
        f"fps={fps}",
        "-an",
        "-q:v",
        "2",
        tail_pattern,
    ]
    final_command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{safe_final:.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-an",
        "-q:v",
        "2",
        final_path,
    ]
    return tail_command, final_command


def build_tail_probe_commands(
    video: Path,
    output_dir: Path,
    duration: float,
    **kwargs,
) -> tuple[list[str], list[str]]:
    """Short-name wrapper for :func:`build_tail_frame_probe_commands`."""
    return build_tail_frame_probe_commands(video, output_dir, duration, **kwargs)


def inspect_tail_image_black_rectangles(
    source: Image.Image | Path,
    *,
    dark_threshold: int = 18,
    rectangle_area_fraction: float = 0.05,
) -> list[str]:
    """Inspect one tail frame for a large contiguous black rectangle."""
    image = _load_rgba_image(source)
    mask = _black_pixel_mask(image, dark_threshold)
    width, height = mask.size
    for area, x, y, component_width, component_height in _black_components(mask):
        area_fraction = area / max(1, width * height)
        rectangularity = area / max(1, component_width * component_height)
        if (
            area_fraction >= rectangle_area_fraction
            and component_width >= width * 0.15
            and component_height >= height * 0.06
            and rectangularity >= 0.62
        ):
            return [
                "tail_black_rectangle: "
                f"黑色区域约占画面 {area_fraction:.1%}（x={x}, y={y}, w={component_width}, h={component_height}）"
            ]
    return []


def tail_frame_integrity_issues(
    source: Image.Image | Path,
    **kwargs,
) -> list[str]:
    """Short-name wrapper for terminal black-rectangle inspection."""
    return inspect_tail_image_black_rectangles(source, **kwargs)


def safe_watermark_motion_expressions(speed_x: float, speed_y: float, margin: int = 20) -> tuple[str, str, str, str]:
    """Return overlay expressions that keep both moving watermarks fully in frame."""
    span = margin * 2
    x_forward = f"{margin}+mod(t*{speed_x:.3f}\\,max(1\\,W-w-{span}))"
    y_forward = f"{margin}+mod(t*{speed_y:.3f}\\,max(1\\,H-h-{span}))"
    x_reverse = f"W-w-{margin}-mod(t*{speed_x:.3f}\\,max(1\\,W-w-{span}))"
    y_reverse = f"H-h-{margin}-mod(t*{speed_y:.3f}\\,max(1\\,H-h-{span}))"
    return x_forward, y_forward, x_reverse, y_reverse


def font_candidates() -> list[Path]:
    return [
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/STHeiti Light.ttc"),
        Path("/System/Library/Fonts/Supplemental/Songti.ttc"),
        Path("/Library/Fonts/Arial Unicode.ttf"),
    ]


def font_path() -> Path | None:
    return next((path for path in font_candidates() if path.exists()), None)


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = font_path()
    if path is None:
        return ImageFont.load_default()
    return ImageFont.truetype(str(path), size=size)


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[float, float],
    max_width: int,
    max_size: int,
    min_size: int,
    fill: tuple[int, int, int],
) -> None:
    for size in range(max_size, min_size - 1, -2):
        font = load_font(size)
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=4)
        if bbox[2] - bbox[0] <= max_width or size == min_size:
            draw.text(xy, text, font=font, anchor="mm", fill=fill, stroke_width=4, stroke_fill=(255, 247, 189, 255))
            return


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if current and bbox[2] - bbox[0] > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


if __name__ == "__main__":
    main()
