from __future__ import annotations

import argparse
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFont
from PIL import ImageDraw


SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass(frozen=True)
class AccountSpec:
    key: str
    display_name: str
    video_path: Path
    selected_frame: int | None


def main() -> None:
    parser = argparse.ArgumentParser(description="生成全自动发布物料包：候选帧、文案和 3:4/4:3/16:9 封面任务")
    parser.add_argument("--story-name", required=True, help="故事名称，不需要书名号")
    parser.add_argument("--episode", required=True, help="主账号 B 站标题使用的集数")
    parser.add_argument("--duration-text", required=True, help="故事时长，例如 3分钟 / 2分40秒")
    parser.add_argument("--story-text", required=True, type=Path, help="完整故事正文 txt/md")
    parser.add_argument("--main-video", required=True, type=Path, help="主账号成片视频")
    parser.add_argument("--library-video", required=True, type=Path, help="宝库号成片视频")
    parser.add_argument("--greenscreen-video", default=None, type=Path, help="主账号真人绿幕原片，用于抽取封面真人动作参考")
    parser.add_argument("--background-video", default=None, type=Path, help="背景故事成片，用于抽取主账号封面故事画面参考")
    parser.add_argument("--output-dir", required=True, type=Path, help="发布包输出目录")
    parser.add_argument("--candidate-count", default=8, type=int, help="每个账号抽取候选帧数量，建议 6-12")
    parser.add_argument("--main-frame", default=None, type=int, help="兼容旧参数：主账号故事画面参考帧编号")
    parser.add_argument("--main-person-frame", default=None, type=int, help="主账号选中的绿幕人物动作参考帧编号")
    parser.add_argument("--main-story-frame", default=None, type=int, help="主账号选中的背景故事画面参考帧编号")
    parser.add_argument("--library-frame", default=None, type=int, help="宝库号选中的候选帧编号，例如 5")
    parser.add_argument("--generate-covers", action="store_true", help="生成封面参考任务；Codex 后续为每个账号产出 3:4、4:3、16:9")
    parser.add_argument("--main-cover-reference", default=None, type=Path, help="主账号横版封面参考帧；优先使用第 13 步确认的预览帧")
    parser.add_argument("--library-cover-reference", default=None, type=Path, help="宝库号横版封面参考帧；应选择能体现故事关键情节的帧，不要用标题页、空景或纯资料展示帧")
    parser.add_argument("--person-reference", default=None, type=Path, help="主账号高清绿幕人物参考帧；建议从原绿幕视频抽帧，不用发布成片截图")
    parser.add_argument("--cover-style", choices=["designed", "screenshot"], default="designed", help="designed=信息型设计封面；screenshot=保留旧的截帧扩展思路")
    parser.add_argument("--story-type", default="童话故事")
    parser.add_argument("--age-range", default="6-8岁")
    parser.add_argument("--roles", default="", help="可选：角色说明，例如 小羊、狐狸、旁白")
    args = parser.parse_args()

    story_text = read_text(args.story_text.expanduser())
    output_dir = args.output_dir.expanduser()
    ensure_dir(output_dir)
    cleanup_deprecated_outputs(output_dir)
    greenscreen_video = args.greenscreen_video.expanduser() if args.greenscreen_video else None
    background_video = args.background_video.expanduser() if args.background_video else None
    main_story_frame = args.main_story_frame if args.main_story_frame is not None else args.main_frame

    accounts = (
        AccountSpec("main", "主账号", args.main_video.expanduser(), args.main_frame),
        AccountSpec("library", "宝库号", args.library_video.expanduser(), args.library_frame),
    )
    for account in accounts:
        if not account.video_path.exists():
            raise FileNotFoundError(f"{account.display_name}视频不存在：{account.video_path}")

    ensure_dir(output_dir / "main")
    ensure_dir(output_dir / "library")

    for account in accounts:
        frames_dir = output_dir / "frame_candidates" / account.key
        extract_candidate_frames(account.video_path, frames_dir, args.candidate_count)
        render_contact_sheet(frames_dir, output_dir / "frame_candidates" / f"{account.key}_候选帧索引.jpg", account.display_name)

    visual_elements = infer_visual_elements(args.story_name, story_text)
    main_cover_reference = args.main_cover_reference.expanduser() if args.main_cover_reference else None
    library_cover_reference = args.library_cover_reference.expanduser() if args.library_cover_reference else None
    write_main_cover_design_package(
        output_dir=output_dir,
        story_name=args.story_name,
        duration_text=args.duration_text,
        story_type=args.story_type,
        age_range=args.age_range,
        story_text=story_text,
        visual_elements=visual_elements,
        greenscreen_video=greenscreen_video,
        background_video=background_video,
        person_reference=args.person_reference.expanduser() if args.person_reference else None,
        cover_reference=main_cover_reference,
        selected_person_frame=args.main_person_frame,
        selected_story_frame=main_story_frame,
        candidate_count=args.candidate_count,
    )

    if args.generate_covers:
        library_selected = library_cover_reference
        library_account = accounts[1]
        if library_selected is None and library_account.selected_frame is not None:
            library_selected = output_dir / "frame_candidates" / "library" / f"candidate_{library_account.selected_frame:02d}.jpg"
        if library_selected is None:
            library_selected = choose_default_library_cover_reference(output_dir / "frame_candidates" / "library", args.candidate_count)
        if not library_selected.exists():
            raise FileNotFoundError(f"宝库号横版封面参考帧不存在：{library_selected}")
        library_covers_dir = output_dir / "library" / "covers"
        ensure_dir(library_covers_dir)
        library_reference = library_covers_dir / "reference_3x4.png"
        copy_image(library_selected, library_reference)
        write_cover_reference_note(
            library_covers_dir / "reference_choice.md",
            account="宝库号",
            reference=library_selected,
            reason="优先选择故事正文中的动作/冲突帧，避免标题页、空书房、开头介绍或纯资料展示帧。未显式传入 --library-frame 时默认从候选帧中取更靠近故事情节的候选 03。",
        )
        write_library_cover_prompts(
            covers_dir=library_covers_dir,
            story_name=args.story_name,
            duration_text=args.duration_text,
            reference_image=library_reference,
            story_type=args.story_type,
            age_range=args.age_range,
            visual_elements=visual_elements,
        )


    write_codex_handoff(
        output_dir=output_dir,
        story_name=args.story_name,
        story_type=args.story_type,
        age_range=args.age_range,
        duration_text=args.duration_text,
        generated_covers=args.generate_covers,
    )
    write_manifest(output_dir, args.story_name, accounts, args.generate_covers)
    print(f"已生成发布包：{output_dir}")


def read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"故事正文不存在：{path}")
    return path.read_text(encoding="utf-8").strip()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def cleanup_deprecated_outputs(output_dir: Path) -> None:
    for relative in ("copy_codex_request.md",):
        path = output_dir / relative
        if path.exists() and path.is_file():
            path.unlink()


def run_command(command: list[str]) -> str:
    result = subprocess.run(command, check=True, text=True, capture_output=True)
    return result.stdout.strip()


def probe_duration(video_path: Path) -> float:
    output = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]
    )
    try:
        return float(output)
    except ValueError as exc:
        raise RuntimeError(f"无法读取视频时长：{video_path}") from exc


def extract_candidate_frames(video_path: Path, output_dir: Path, count: int) -> None:
    ensure_dir(output_dir)
    count = max(1, min(12, count))
    duration = max(1.0, probe_duration(video_path))
    start = min(duration * 0.12, 12.0)
    end = max(start + 1.0, duration * 0.86)
    step = (end - start) / (count + 1)
    for index in range(1, count + 1):
        timestamp = start + step * index
        output_path = output_dir / f"candidate_{index:02d}.jpg"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(output_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def render_contact_sheet(frames_dir: Path, output_path: Path, title: str) -> None:
    frame_paths = sorted(path for path in frames_dir.iterdir() if path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS)
    if not frame_paths:
        return
    thumb_width = 360
    thumb_height = 480
    cols = 4
    rows = math.ceil(len(frame_paths) / cols)
    header_height = 72
    padding = 18
    label_height = 34
    sheet = Image.new(
        "RGB",
        (
            cols * thumb_width + (cols + 1) * padding,
            header_height + rows * (thumb_height + label_height + padding) + padding,
        ),
        (248, 246, 240),
    )
    draw = ImageDraw.Draw(sheet)
    font = load_font(28)
    small_font = load_font(22)
    draw.text((padding, 22), f"{title}候选帧：选中编号后用 --{frames_dir.name}-frame N", fill=(38, 38, 34), font=font)
    for idx, frame_path in enumerate(frame_paths):
        row = idx // cols
        col = idx % cols
        x = padding + col * (thumb_width + padding)
        y = header_height + row * (thumb_height + label_height + padding)
        image = Image.open(frame_path).convert("RGB")
        image.thumbnail((thumb_width, thumb_height))
        tile = Image.new("RGB", (thumb_width, thumb_height), (230, 228, 220))
        tile.paste(image, ((thumb_width - image.width) // 2, (thumb_height - image.height) // 2))
        sheet.paste(tile, (x, y))
        draw.text((x, y + thumb_height + 6), frame_path.stem.replace("candidate_", "候选 "), fill=(58, 58, 52), font=small_font)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, quality=92)


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


def make_3x4_cover(source_path: Path, output_path: Path) -> None:
    image = Image.open(source_path).convert("RGB")
    cropped = crop_to_ratio(image, 3 / 4)
    cropped = cropped.resize((1080, 1440), Image.Resampling.LANCZOS)
    cropped = ImageEnhance.Color(cropped).enhance(1.04)
    cropped = ImageEnhance.Contrast(cropped).enhance(1.05)
    cropped = ImageEnhance.Sharpness(cropped).enhance(1.08)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(output_path)


def crop_to_ratio(image: Image.Image, ratio: float) -> Image.Image:
    width, height = image.size
    current = width / height
    if abs(current - ratio) < 0.01:
        return image.copy()
    if current > ratio:
        new_width = int(height * ratio)
        left = (width - new_width) // 2
        return image.crop((left, 0, left + new_width, height))
    new_height = int(width / ratio)
    top = (height - new_height) // 2
    return image.crop((0, top, width, top + new_height))


def write_library_cover_prompts(
    covers_dir: Path,
    story_name: str,
    duration_text: str,
    reference_image: Path,
    story_type: str,
    age_range: str,
    visual_elements: str,
) -> None:
    prompt = render_library_cover_prompt(
        story_name=story_name,
        duration_text=duration_text,
        reference_image=reference_image,
        story_type=story_type,
        age_range=age_range,
        visual_elements=visual_elements,
    )
    (covers_dir / "cover_derivative_prompts.md").write_text(prompt, encoding="utf-8")


def write_main_cover_design_package(
    output_dir: Path,
    story_name: str,
    duration_text: str,
    story_type: str,
    age_range: str,
    story_text: str,
    visual_elements: str,
    greenscreen_video: Path | None,
    background_video: Path | None,
    person_reference: Path | None,
    cover_reference: Path | None,
    selected_person_frame: int | None,
    selected_story_frame: int | None,
    candidate_count: int,
) -> None:
    covers_dir = output_dir / "main" / "covers"
    ensure_dir(covers_dir)
    person_candidates_dir = covers_dir / "person_candidates"
    story_candidates_dir = covers_dir / "story_frame_candidates"

    if greenscreen_video and greenscreen_video.exists():
        extract_candidate_frames(greenscreen_video, person_candidates_dir, candidate_count)
        render_contact_sheet(person_candidates_dir, covers_dir / "person候选帧索引.jpg", "主账号真人动作")
    elif person_reference and person_reference.exists():
        ensure_dir(person_candidates_dir)
        make_3x4_cover(person_reference, person_candidates_dir / "candidate_01.jpg")
        render_contact_sheet(person_candidates_dir, covers_dir / "person候选帧索引.jpg", "主账号真人动作")

    if background_video and background_video.exists():
        extract_candidate_frames(background_video, story_candidates_dir, candidate_count)
        render_contact_sheet(story_candidates_dir, covers_dir / "story候选帧索引.jpg", "主账号故事画面")

    selected_person = select_candidate(person_candidates_dir, selected_person_frame)
    selected_story = select_candidate(story_candidates_dir, selected_story_frame)
    if selected_person:
        copy_image(selected_person, covers_dir / "selected_person_reference.png")
    if selected_story:
        copy_image(selected_story, covers_dir / "selected_story_reference.png")
    if cover_reference and cover_reference.exists():
        copy_image(cover_reference, covers_dir / "reference_3x4.png")
    elif selected_story:
        copy_image(selected_story, covers_dir / "reference_3x4.png")

    prompt = render_main_cover_design_prompt(
        story_name=story_name,
        duration_text=duration_text,
        story_type=story_type,
        age_range=age_range,
        story_text=story_text,
        visual_elements=visual_elements,
        cover_reference=covers_dir / "reference_3x4.png" if (covers_dir / "reference_3x4.png").exists() else None,
        person_reference=covers_dir / "selected_person_reference.png" if selected_person else None,
        story_reference=covers_dir / "selected_story_reference.png" if selected_story else None,
        person_contact_sheet=covers_dir / "person候选帧索引.jpg" if (covers_dir / "person候选帧索引.jpg").exists() else None,
        story_contact_sheet=covers_dir / "story候选帧索引.jpg" if (covers_dir / "story候选帧索引.jpg").exists() else None,
    )
    (covers_dir / "main_cover_design_request.md").write_text(prompt, encoding="utf-8")
    (covers_dir / "cover_derivative_prompts.md").write_text(
        render_main_derivative_prompt(story_name, duration_text),
        encoding="utf-8",
    )


def select_candidate(candidates_dir: Path, selected_frame: int | None) -> Path | None:
    if selected_frame is None:
        return None
    path = candidates_dir / f"candidate_{selected_frame:02d}.jpg"
    if not path.exists():
        raise FileNotFoundError(f"候选帧不存在：{path}")
    return path


def copy_image(source_path: Path, output_path: Path) -> None:
    image = Image.open(source_path).convert("RGBA")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)


def choose_default_library_cover_reference(candidates_dir: Path, candidate_count: int) -> Path:
    # Candidate 01/02 are often title/intro frames. Prefer the first story-action
    # candidate so library covers do not become generic resource-display art.
    preferred_index = min(max(3, candidate_count // 3), max(1, candidate_count))
    preferred = candidates_dir / f"candidate_{preferred_index:02d}.jpg"
    if preferred.exists():
        return preferred
    candidates = sorted(candidates_dir.glob("candidate_*.jpg"))
    if not candidates:
        raise FileNotFoundError(f"宝库号候选帧目录为空：{candidates_dir}")
    return candidates[min(2, len(candidates) - 1)]


def write_cover_reference_note(output_path: Path, *, account: str, reference: Path, reason: str) -> None:
    output_path.write_text(
        "\n".join(
            [
                f"# {account}横版封面参考帧选择",
                "",
                f"- 参考帧：`{reference}`",
                f"- 选择理由：{reason}",
                "",
                "后续如果这帧不能体现故事关键情节，请显式传入 `--library-frame` 或 `--library-cover-reference` 重新生成发布物料。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def render_library_cover_prompt(
    story_name: str,
    duration_text: str,
    reference_image: Path,
    story_type: str,
    age_range: str,
    visual_elements: str,
) -> str:
    return f"""# 宝库号 3:4 / 4:3 / 16:9 封面提示：基于发布预览帧生成

## 生成方式硬性要求
- 必须回到 Codex 对话中使用 Codex 原生图像生成能力生成最终三张封面。
- 本脚本只允许准备参考图、候选帧和提示词，不允许用 Pillow、HTML/CSS、截图拼接或其他本地脚本合成最终封面。
- 生成后只允许做尺寸标准化、文件复制和轻微压缩，不允许把本地拼贴图冒充最终封面。
- 先生成 `cover_4x3.png` 母版，再把它作为必选参考图，通过图像编辑/扩图衍生 `cover_3x4.png`、`cover_16x9.png`；每种比例重新组织安全区，但保持同一角色、字体体系、色彩和装饰语言。禁止三次互不相关的随机生成，也禁止机械裁切或拉伸。

发布/预览参考帧：`{reference_image}`

宝库号封面逻辑：保留故事、标题和资源信息，以主账号 4:3 母版为参考图移除真人并重排空位，得到统一的 4:3 故事封面。参考帧用于核对本期故事角色、场景氛围和资料包信息；如果只是标题页、空景或纯资料展示帧，必须回候选帧重新选择。

## 三种比例
- 输出文件：`cover_3x4.png`、`cover_4x3.png`、`cover_16x9.png`。
- 画面比例分别为 3:4、4:3、16:9，标题、角色和信息带都必须处于各自安全区。
- 保留《{story_name}》标题、{story_type} 类型、故事时长 {duration_text}、适合年龄 {age_range}。
- 保留资源包信息：背景视频、PPT、配乐、文稿、朗读标注、示范视频。
- 故事视觉元素：{visual_elements}。
- 横版构图要像成熟发布封面，不要只是裁切或拉伸 3:4 参考帧。
- 画面必须是统一场景，资源信息放在同一条底部信息带或自然场景标识里。
- 禁止六宫格、资源卡片矩阵、白色卡片、播放器框、PPT框、木质大相框、内嵌小画面和模板拼贴感。
- 字要大、清楚、无乱码，不要新增无关道具或陌生 logo。
"""


def render_main_cover_design_prompt(
    story_name: str,
    duration_text: str,
    story_type: str,
    age_range: str,
    story_text: str,
    visual_elements: str,
    cover_reference: Path | None,
    person_reference: Path | None,
    story_reference: Path | None,
    person_contact_sheet: Path | None,
    story_contact_sheet: Path | None,
) -> str:
    cover_line = f"- 发布/预览参考帧：`{cover_reference}`。" if cover_reference else "- 优先查看第 13 步发布预览帧，选择画面、人物位置和字幕都稳定的一帧作为横版封面参考。"
    person_line = f"- 已选真人参考图：`{person_reference}`。" if person_reference else "- 先查看 `person候选帧索引.jpg`，选择表情自然、动作打开、身体完整的一帧作为真人参考。"
    story_line = f"- 已选故事画面参考图：`{story_reference}`。" if story_reference else "- 先查看 `story候选帧索引.jpg`，选择最能代表故事冲突/情绪的一帧作为卡通故事参考。"
    person_sheet_line = f"- 真人候选索引：`{person_contact_sheet}`。" if person_contact_sheet else "- 真人候选索引暂缺：请先提供绿幕视频或人物参考帧。"
    story_sheet_line = f"- 故事画面候选索引：`{story_contact_sheet}`。" if story_contact_sheet else "- 故事画面候选索引暂缺：请先提供背景故事成片。"
    return f"""# 主账号三比例封面设计任务

## 生成方式硬性要求
- 必须回到 Codex 对话中使用 Codex 原生图像生成能力生成最终三张封面。
- 先使用真人参考、故事参考和历史版式参考生成 `cover_4x3.png` 母版；再把该母版作为必选参考图，通过图像编辑/扩图衍生 `cover_3x4.png`、`cover_16x9.png`。三种比例都保持真人、故事角色、字体体系、色彩和装饰语言一致，禁止互不相关的随机生成和机械裁切。
- 禁止用 Pillow、HTML/CSS、截图拼接、模板叠字或其他本地脚本合成主账号最终封面。
- 本脚本只负责抽取真人/故事参考、生成任务书和保存最终文件；生成后只允许做尺寸标准化、文件复制和轻微压缩。
- 如果发现最终图像像“真人照片贴在模板上”、像“视频截图贴框”、像资源卡片矩阵或像拼贴模板，必须判定为不合格并重新用原生图像生成。

主账号封面逻辑：以第 13 步已确认的发布预览帧或发布成片截屏为参考先生成 4:3 母版，再通过参考图编辑衍生 3:4、16:9。人物一致性优先，不能把真人绿幕帧重新捏成另一个人。

## 参考素材
{cover_line}
{person_sheet_line}
{story_sheet_line}
{person_line}
{story_line}

## 生成目标
- 输出文件：`cover_3x4.png`、`cover_4x3.png`、`cover_16x9.png`。
- 画面比例分别为 3:4、4:3、16:9；各比例重新组织安全区，但必须继承同一 4:3 母版。
- 主体：真实绵羊姐姐 + 《{story_name}》故事核心场景和角色。
- 真人必须以发布/预览参考帧中的主持人为准，人物一致性优先级高于风格统一：保持同一个人的脸型、五官比例、眼睛形状、鼻子、嘴型、发际线、发髻、肤色和真实照片质感。
- 不要重新捏脸，不要换成另一个漂亮主持人，不要卡通化，不要过度美颜；如果脸不像参考图，整张封面不合格。
- 服装、饰品和麦克风严格保持本期真人参考图，人物可以重新进入故事场景，但身份必须像参考图本人。
- 卡通故事元素来自背景故事画面，和故事强关联，不能使用其他故事的道具。

## 必须写入的信息
- 品牌：绵羊姐姐讲故事。
- 故事类型：{story_type}。
- 标题：《{story_name}》。
- 故事时长：{duration_text}。
- 适合年龄：{age_range}。
- 资料包信息：背景视频、PPT、配乐、文稿、朗读标注、示范视频。

## 画面方向
- 故事视觉元素：{visual_elements}。
- 明亮、显眼、适合小红书/视频号的儿童故事节目包装，具体时代和场景只服从本期故事参考。
- 画面必须是一张统一的本期故事场景封面：人物、标题、故事角色和背景自然在同一个空间里。
- 标题要大，人物和故事画面要自然融合，不要像把截屏硬贴在模板里。
- 沿用历史样例的扁平简洁信息层级：标题与时长/年龄集中成清楚信息块，底部只保留小号适用说明；禁止六宫格、白色资源卡、内嵌小画面、木质大相框或分栏模板。

## 故事文本参考
{first_third(story_text)}
"""


def render_main_derivative_prompt(story_name: str, duration_text: str) -> str:
    return f"""# 主账号三比例封面补充提示：基于发布预览帧生成

## 生成方式硬性要求
- 必须回到 Codex 对话中使用 Codex 原生图像生成能力生成 `cover_3x4.png`、`cover_4x3.png`、`cover_16x9.png`。
- 禁止用 Pillow、HTML/CSS、截图拼接、模板叠字或其他本地脚本合成最终封面。
- 生成后只允许做尺寸标准化、文件复制和轻微压缩。
- 以已经通过的 4:3 母版作为必选参考图进行编辑衍生；每种比例独立组织人物、标题和资料信息安全区，但禁止脱离母版重新随机生成，也禁止机械裁切、拉伸或补边。

封面参考帧：优先使用 `reference_3x4.png`；如果不存在，则从第 13 步发布预览帧中选择主账号画面稳定的一帧。

请先确认 4:3 母版，再以它作为必选参考图编辑衍生另外两种比例。不要重新换人物，不要换故事角色；不要照搬预览里的木质相框、截图框或模板分区。
主账号真人必须保持参考帧中的同一个人，尤其是脸型、五官比例、眼睛、鼻子、嘴型、发际线和真实照片质感；不要在横版生成时换脸。

## 三种比例
- 输出文件：`cover_3x4.png`、`cover_4x3.png`、`cover_16x9.png`。
- 分别适配竖版、横版和宽屏封面安全区。
- 保持参考帧的风格和信息，不要裁掉人物手臂、标题或资料区。
- 画面必须是一张统一场景封面，禁止内嵌框、资源卡片矩阵、截图拼贴和模板叠字感。
"""


def first_third(text: str) -> str:
    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if len(normalized) <= 180:
        return normalized
    target = max(120, len(normalized) // 3)
    target = min(target, 360)
    cut = target
    for marker in ("。", "！", "？", "\n"):
        pos = normalized.find(marker, target)
        if pos != -1 and pos < target + 180:
            cut = pos + 1
            break
    return normalized[:cut].strip()


def infer_visual_elements(story_name: str, text: str) -> str:
    combined = f"{story_name}\n{text}"
    for keyword, elements in (
        ("悬梁刺股", "夜晚书房、房梁绳子、头发悬梁、锥子刺股、竹简、烛光、古代读书人"),
        ("邯郸学步", "赵国街市、古城门、学步的年轻人、行人脚步、青石路"),
        ("自相矛盾", "古代集市、盾牌、长矛、围观人群、摊位"),
        ("夸大其词", "讲述者、夸张表情、听众反应、戏剧化舞台"),
    ):
        if keyword in combined:
            return elements
    return "故事关键角色、主要场景、儿童表演舞台元素"
def write_manifest(output_dir: Path, story_name: str, accounts: tuple[AccountSpec, ...], generated_covers: bool) -> None:
    lines = [
        f"# {story_name} 发布包清单",
        "",
        "## 发布物料范围",
        "- 生成主账号、宝库号两份发布文案：标题、正文、话题建议。",
        "- 两账号分别生成 3:4、4:3、16:9 三张封面，共六张。",
        "- 六张封面必须使用 Codex 原生生图能力，按主账号 4:3 母版 → 主账号衍生 → 宝库号 4:3 → 宝库号衍生的参考图编辑血缘完成。",
        "- `cover_lineage.json`：记录六张封面的母子关系、输入参考和 SHA-256。",
        "- `publish_package_codex_handoff.md`：交给 Codex 原生生图的总任务书",
        "",
        "## 候选帧",
        "- `frame_candidates/main_候选帧索引.jpg`",
        "- `frame_candidates/library_候选帧索引.jpg`",
        "",
        "## 封面",
    ]
    if generated_covers:
        lines.extend(
            [
                "- `library/covers/reference_3x4.png`：宝库号三比例封面的参考帧；应体现故事关键情节",
                "- `library/covers/reference_choice.md`：宝库号参考帧选择说明",
                "- `library/covers/cover_derivative_prompts.md`：基于宝库号参考帧生成三种比例的提示",
                "- `main/covers/reference_3x4.png`：主账号三比例封面的参考帧",
                "- `main/covers/person候选帧索引.jpg`：主账号真人绿幕动作候选",
                "- `main/covers/story候选帧索引.jpg`：主账号故事画面候选",
                "- `main/covers/main_cover_design_request.md`：主账号三比例封面生图任务",
                "- `main/covers/cover_derivative_prompts.md`：基于主账号参考帧生成三种比例的提示",
            ]
        )
    else:
        lines.append("- 尚未生成。确认候选帧后用 `--generate-covers` 再跑一次。")
    lines.extend(["", "## 下一步"])
    if generated_covers:
        lines.extend(
            [
                "1. 宝库号：检查 `library/covers/reference_3x4.png` 和 `reference_choice.md`，确认参考帧体现故事关键情节，再生成 3:4、4:3、16:9 三张封面。",
                "2. 主账号：检查 `main/covers/reference_3x4.png`，再用 `main_cover_design_request.md` 或 `cover_derivative_prompts.md` 生成 3:4、4:3、16:9 三张封面。",
                "3. 检查 `main/copy.md`、`library/copy.md` 均包含标题、正文和话题建议。",
                "4. 六张封面必须按母版血缘做参考图编辑并为各比例重新排版；禁止六次独立随机生成，也禁止用 Pillow、HTML/CSS、截图拼接、模板叠字或其他本地脚本合成最终封面。",
            ]
        )
    else:
        lines.extend(
            [
                "1. 宝库号：打开 `frame_candidates/library_候选帧索引.jpg`，选择能体现故事关键情节或角色动作的横版封面参考帧；不要选标题页、空景、普通书卷景或纯资料展示帧。",
                "2. 主账号：优先使用第 13 步确认过的人物/故事框预览帧作为横版封面参考。",
                "3. 重新运行脚本并传入 `--generate-covers`；如需指定候选帧，再加 `--library-frame`。",
            ]
        )
    (output_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_codex_handoff(
    *,
    output_dir: Path,
    story_name: str,
    story_type: str,
    age_range: str,
    duration_text: str,
    generated_covers: bool,
) -> Path:
    handoff = output_dir / "publish_package_codex_handoff.md"
    project_dir = output_dir.parent
    library_cover_ready = (output_dir / "library" / "covers" / "reference_3x4.png").exists()
    main_cover_ready = (output_dir / "main" / "covers" / "reference_3x4.png").exists()
    main_person_ref_ready = (output_dir / "main" / "covers" / "selected_person_reference.png").exists()
    main_story_ref_ready = (output_dir / "main" / "covers" / "selected_story_reference.png").exists()
    lines = [
        f"# 第 15 步发布物料 Codex 交接：{story_name}",
        "",
        "请继续处理这个故事的全自动发布物料。生成主账号/宝库号两份文案，并为每个账号生成 3:4、4:3、16:9 三张封面，共六张；封面必须使用 Codex 原生生图能力并按母版血缘衍生。",
        "",
        "封面不能用 Pillow、HTML/CSS、截图拼接、模板叠字或本地脚本合成最终图。本地脚本只能准备规则、素材、限制和路径；生成后只允许做尺寸标准化、文件复制和轻微压缩。",
        "",
        "## 项目信息",
        f"- 项目目录：`{project_dir}`",
        f"- 发布物料目录：`{output_dir}`",
        f"- 故事名称：`{story_name}`",
        f"- 故事类型：`{story_type}`",
        f"- 故事时长：`{duration_text}`",
        f"- 适合年龄：`{age_range}`",
        f"- 本次是否带选帧生成封面参考：`{'是' if generated_covers else '否'}`",
        f"- 现有宝库号三比例封面参考帧：`{'已存在' if library_cover_ready else '未生成'}`",
        f"- 现有主账号三比例封面参考帧：`{'已存在' if main_cover_ready else '未生成'}`",
        f"- 现有主账号真人/故事选中参考：`{'已存在' if main_person_ref_ready and main_story_ref_ready else '未完整生成，可不用作横版主参考'}`",
        "",
        "## 已生成文件",
        f"- 主账号候选帧索引：`{output_dir / 'frame_candidates' / 'main_候选帧索引.jpg'}`",
        f"- 宝库号候选帧索引：`{output_dir / 'frame_candidates' / 'library_候选帧索引.jpg'}`",
        f"- 主账号真人候选帧索引：`{output_dir / 'main' / 'covers' / 'person候选帧索引.jpg'}`",
        f"- 主账号故事候选帧索引：`{output_dir / 'main' / 'covers' / 'story候选帧索引.jpg'}`",
        f"- 主账号三比例生图任务：`{output_dir / 'main' / 'covers' / 'main_cover_design_request.md'}`",
        f"- 主账号三比例衍生提示：`{output_dir / 'main' / 'covers' / 'cover_derivative_prompts.md'}`",
        f"- 宝库号三比例衍生提示：`{output_dir / 'library' / 'covers' / 'cover_derivative_prompts.md'}`",
        "",
        "## 执行规则",
        "1. 生成 `main/copy.md`、`library/copy.md`，每份包含标题、正文、话题建议。",
        "2. 先用本期真人、本期故事代表帧和历史已确认版式样例生成主账号 `cover_4x3.png` 母版；样例只提供版式，不得带入旧角色或旧标题。",
        "3. 以主账号 4:3 母版为必选参考图编辑衍生主账号 3:4/16:9；再编辑移除真人得到宝库号 4:3，并由它衍生宝库号 3:4/16:9。禁止六次独立随机生成。",
        "4. 主账号真人一致性优先：生成时尽量保留预览帧中原主持人的脸型、五官比例、发型、服装和姿态。",
        "5. 3:4、4:3、16:9 必须分别适配安全区，不能机械裁切。",
        "6. 版式沿用已确认样例的扁平、简洁信息层级：标题与时长/年龄集中成清楚信息块，底部只保留小号适用说明；不要重新发明复杂框架。",
        "7. 在发布目录写 `cover_lineage.json`，逐张记录 path、parent、parent_sha256、generation_mode、reference_files、sha256；衍生图的 parent 哈希必须等于实际母版。",
        "8. 生成后可用脚本做尺寸标准化和复制落位，但不能用脚本重新画版式、叠文字或拼合最终封面。",
        "",
        "## 目标保存路径",
        f"- 宝库号：`{output_dir / 'library' / 'covers'}` 下 cover_3x4.png、cover_4x3.png、cover_16x9.png",
        f"- 主账号：`{output_dir / 'main' / 'covers'}` 下 cover_3x4.png、cover_4x3.png、cover_16x9.png",
        f"- 文案：`{output_dir / 'main' / 'copy.md'}`、`{output_dir / 'library' / 'copy.md'}`",
        "",
        "## 如果还没有选帧",
        "主账号可优先使用第 13 步已经确认的发布预览帧；宝库号必须看候选帧索引，选择能体现故事关键情节或角色动作的横版封面参考帧，然后回工作台重新运行第 15 步或执行：",
        "",
        "```bash",
        f"python3 story_workflow.py publish-package-project --project-dir \"{project_dir}\" --generate-covers --library-frame 编号",
        "```",
    ]
    if not generated_covers:
        lines.extend(
            [
                "",
                "当前状态：工作台本次只是刷新发布物料任务书，没有重新带选帧生成封面参考。如果现有封面和参考图已经确认，可以直接沿用；如果要替换参考帧，再带 `--generate-covers` 和帧编号重跑。",
            ]
        )
    handoff.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return handoff


if __name__ == "__main__":
    main()
