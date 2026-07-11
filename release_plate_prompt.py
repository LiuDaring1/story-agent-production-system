from __future__ import annotations

import argparse
from pathlib import Path


DEFAULT_VIDEO_BOX = "0,416,1080,608"


def main() -> None:
    parser = argparse.ArgumentParser(description="生成发布底板图的 Codex 生图提示词")
    parser.add_argument("--story-name", required=True)
    parser.add_argument("--duration-text", required=True)
    parser.add_argument("--account-variant", choices=["library", "main"], default="library")
    parser.add_argument("--story-type", default="儿童故事")
    parser.add_argument("--age-range", default="按本期设定")
    parser.add_argument("--theme-elements", default="", help="画面元素偏向，例如：中国风、私塾、竹简、古代学堂")
    parser.add_argument("--style-reference", default="", help="参考底板风格说明，可写参考图路径或文字")
    parser.add_argument("--reference-image", default=None, type=Path, help="底板参考图，用来锁定版式和视觉风格")
    parser.add_argument("--video-box", default=DEFAULT_VIDEO_BOX, help="中间视频窗口 x,y,w,h，基于 1080x1440")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--slug", default="release_plate")
    args = parser.parse_args()

    output_dir = args.output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    reference_image = args.reference_image.expanduser() if args.reference_image else None
    if reference_image is not None and not reference_image.exists():
        raise FileNotFoundError(f"参考图不存在：{reference_image}")
    prompt_path = output_dir / f"{args.slug}_release_plate_prompt.md"
    prompt_path.write_text(
        build_prompt(
            story_name=args.story_name,
            duration_text=args.duration_text,
            account_variant=args.account_variant,
            story_type=args.story_type,
            age_range=args.age_range,
            theme_elements=args.theme_elements,
            style_reference=args.style_reference,
            reference_image=reference_image,
            video_box=args.video_box,
        ),
        encoding="utf-8",
    )
    print(f"已生成底板生图提示词：{prompt_path}")


def build_prompt(
    story_name: str,
    duration_text: str,
    account_variant: str,
    story_type: str,
    age_range: str,
    theme_elements: str,
    style_reference: str,
    reference_image: Path | None,
    video_box: str,
) -> str:
    title = story_name if story_name.startswith("《") else f"《{story_name}》"
    elements_line = theme_elements or "贴合故事主题的儿童友好元素，简洁、有场景感但不杂乱"
    is_main = account_variant == "main"
    account_name = "主账号版（绵羊姐姐讲故事）" if is_main else "宝库号版（绵羊姐姐语言节目宝库）"
    headline = story_type
    bottom_lines = (
        [
            f"完整版时长：{duration_text}",
            f"适合年龄：{age_range}",
            "适用于朗诵比赛、故事表演、少儿口才、技能比拼",
        ]
        if is_main
        else [
            "背景视频 + PPT + 配乐",
            "文稿 + 示范视频 + 朗读标注",
        ]
    )
    bottom_text = "\n".join(f"- 底部信息：{line}" for line in bottom_lines)
    visible_info = (
        f"- 完整版时长：{duration_text}\n"
        f"- 适合年龄：{age_range}\n"
        "- 适用说明：适用于朗诵比赛、故事表演、少儿口才、技能比拼"
        if is_main
        else f"- 故事时长：{duration_text}\n- 适合年龄：{age_range}"
    )
    reference_line = style_reference or (
        "参考主账号简洁节目包装：中间视频居中，顶部只放故事类型和标题，底部只放完整版时长、适合年龄和适用说明"
        if is_main
        else "参考既有宝库号商品发布底板：保留较丰富的资料包海报感，只学习顶部标题区和底部资源信息区；中间画面内容不要学习，必须改成纯色空白横向打穿区域"
    )
    reference_section = ""
    if reference_image is not None:
        reference_section = f"""
## 输入参考图

Reference image:
{reference_image}

这张图是版式和风格参考图。生成底板时只学习它的顶部信息区、底部信息区和整体儿童节目/资源包装质感。主账号要比参考图更简洁，宝库号可保留较丰富的商品资料包感。不要学习参考图中间的故事插画区；中间整条横版区域后期会被真实视频覆盖，必须做成纯色空白占位，不要生成任何故事画面、边框、人物或装饰。
"""
    return f"""# {account_name}发布底板图生图提示词

用途：生成一张 3:4 竖屏发布视频底板图。后续脚本会把真实 16:9 {'主账号横版成片（含人物、框和故事视频）' if is_main else '背景视频'}放到中间区域，所以中间区域不是创作区，必须整条横向打穿、纯色留空，不能生成插画人物、边框或装饰压入这个区域。

请用 Codex 原生图像生成能力生成 1 张图。需要时可连续生成 4 张候选图。若本任务书包含“输入参考图”，生成时请把该图片作为 imagegen 的参考图一起输入。
{reference_section}

## 生图提示词

Use case: ads-marketing
Asset type: 3:4 vertical video release plate / 商品展示底板图
Input images: Reference image is the layout/style anchor when provided. Learn the top title band, bottom scroll information band, and premium children's resource-poster feeling. Ignore the reference image's middle illustration area; replace it with a plain full-width blank video strip.
Primary request:
生成一张精美儿童故事{'主账号节目' if is_main else '资源'}发布视频底板图，最终比例 3:4，适合小红书竖屏视频封装。整体结构必须稳定：顶部和底部高度相等，中间整条 16:9 横版区域精确居中，是“后期视频占位死区”，只允许纯色空白，不参与画面创作。

Exact visible text:
- 顶部分类：{headline}
- 故事名称：{title}
{visible_info}
{bottom_text}

Layout requirements:
- Final aspect ratio: 3:4.
- Canvas target: 1080x1440.
- Reserve a full-width clean 16:9 horizontal blank strip exactly at approximately {video_box}. This is the real video overlay area.
- The strip must be vertically centered in the 3:4 canvas; top packaging area and bottom packaging area must have equal height and equal visual area.
- Because this strip will be covered by real video, do not design it as a framed scene, do not add a small floating frame, and do not draw a story illustration there.
- The middle strip must be one flat solid matte color, preferably dark neutral gray, from left edge to right edge. No gradients, no texture, no picture content.
- Absolutely no title text, duration text, icons, characters, scrolls, curtains, clouds, instruments, people, shields, spears, plants, decorative corners, borders, shadows, or patterns may appear inside this strip.
- Do not let any decorative object cross into the strip from the top, bottom, left, or right. Since the strip is full width, there are no side decoration zones within this Y range.
- Keep all creative visual elements only above the strip or below the strip.
- Keep the top title area and bottom information area readable, clearly separated from the blank middle strip, and never crossing into it.
- {'Main account: keep the design simple and focused; top only has story type and story title, bottom has one compact information panel plus the one-line usage note.' if is_main else 'Library account: keep the richer product-resource poster feeling; bottom area may use a warm illustrated scroll/card/resource panel, and resource text must say 文稿 + 示范视频 + 朗读标注.'}

Story and style direction:
- Story type: {headline}
- Story name: {title}
- Duration: {duration_text}
- Age range: {age_range}
- Theme elements: {elements_line}
- Reference style: {reference_line}

Visual style:
- High-quality 3D cartoon / soft children's illustration style.
- Warm, bright, premium {'program package' if is_main else 'resource-pack poster'} feeling.
- Follow the reference image only for top/bottom packaging logic and overall polish.
- But unlike the reference image, do not generate a story preview picture in the middle; leave the middle as a plain full-width matte strip.
- Rich but clean composition, no dark background, no harsh neon.
- Use story-related decorative elements around the top and bottom areas.
- If using Chinese style elements, keep them cute and child-friendly, not serious historical realism.

Avoid:
- Do not use black bars.
- Do not make a plain UI mockup.
- Do not put watermarks, app UI, playback controls, or phone screenshots in the image.
- Do not put any critical information or decorative material inside the middle video strip.
- Do not make the middle strip narrow, inset, rounded, framed, or surrounded by complex border art.
- Avoid tiny unreadable text.

Important production note:
The exact Chinese text may be corrected later by code if needed. Prioritize the stable 3:4 layout, beautiful illustrated top/bottom areas, and a completely empty full-width 16:9 middle strip.
"""


if __name__ == "__main__":
    main()
