from __future__ import annotations

"""Legacy one-off helper for the completed 邯郸学步 regression sample.

The reusable pipeline now generates story-specific release visuals through
story_workflow.py theme-assets / prepare-release-assets-project.
"""

import math
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont


OUT_DIR = Path("/Users/baiyanglin/Desktop/故事剪辑：邯郸学步/04_发布视频/theme_assets")
PREVIEW_DIR = Path("output/handan_theme_assets")

MAIN_PLATE = OUT_DIR / "main_release_plate.png"
LIBRARY_PLATE = OUT_DIR / "library_release_plate.png"
MAIN_BG = OUT_DIR / "main_background_16x9.png"
FRAME_A = OUT_DIR / "story_frame_a.png"
FRAME_B = OUT_DIR / "story_frame_b.png"

SAFE_BOX = (0, 360, 1080, 608)
SAFE_COLOR = (248, 232, 204)


def font(size: int, *, serif: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        [
            "/System/Library/Fonts/Supplemental/Songti.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc",
        ]
        if serif
        else [
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ]
    )
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def lerp(a: int, b: int, t: float) -> int:
    return int(a + (b - a) * t)


def gradient(size: tuple[int, int], top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    w, h = size
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        draw.line((0, y, w, y), fill=tuple(lerp(top[i], bottom[i], t) for i in range(3)))
    return img


def paste_layer(base: Image.Image, layer: Image.Image) -> None:
    base.alpha_composite(layer) if base.mode == "RGBA" else base.paste(layer.convert("RGB"), (0, 0), layer.getchannel("A"))


def add_soft_shadow(img: Image.Image, alpha: Image.Image, offset: tuple[int, int], blur: int, color=(90, 55, 28, 110)) -> None:
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    colored = Image.new("RGBA", img.size, color)
    shadow_alpha = Image.new("L", img.size, 0)
    shadow_alpha.paste(alpha, offset)
    shadow_alpha = shadow_alpha.filter(ImageFilter.GaussianBlur(blur))
    shadow.putalpha(shadow_alpha)
    img.alpha_composite(shadow)


def centered_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    fnt: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    stroke_fill: tuple[int, int, int] | None = None,
    stroke_width: int = 0,
) -> None:
    x1, y1, x2, y2 = box
    bbox = draw.textbbox((0, 0), text, font=fnt, stroke_width=stroke_width)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = x1 + (x2 - x1 - tw) / 2
    y = y1 + (y2 - y1 - th) / 2 - 3
    draw.text((x, y), text, font=fnt, fill=fill, stroke_fill=stroke_fill, stroke_width=stroke_width)


def fit_font(text: str, max_width: int, start_size: int, *, serif: bool = False) -> ImageFont.ImageFont:
    size = start_size
    while size > 12:
        fnt = font(size, serif=serif)
        box = ImageDraw.Draw(Image.new("RGB", (10, 10))).textbbox((0, 0), text, font=fnt, stroke_width=4)
        if box[2] - box[0] <= max_width:
            return fnt
        size -= 2
    return font(size, serif=serif)


def rounded_mask(size: tuple[int, int], rect: tuple[int, int, int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(rect, radius=radius, fill=255)
    return mask


def draw_cloud(draw: ImageDraw.ImageDraw, cx: int, cy: int, scale: float, fill=(255, 250, 236), outline=(230, 194, 146)) -> None:
    parts = [(-58, 8, 46), (-22, -18, 56), (25, -10, 50), (62, 9, 38), (0, 18, 62)]
    for ox, oy, r in parts:
        rr = int(r * scale)
        x = cx + int(ox * scale)
        y = cy + int(oy * scale)
        draw.ellipse((x - rr, y - rr, x + rr, y + rr), fill=fill, outline=outline, width=max(2, int(3 * scale)))


def draw_lantern(draw: ImageDraw.ImageDraw, cx: int, cy: int, scale: float) -> None:
    w, h = int(64 * scale), int(82 * scale)
    draw.line((cx, cy - h // 2 - 36, cx, cy - h // 2), fill=(145, 82, 46), width=max(2, int(4 * scale)))
    draw.rounded_rectangle((cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2), radius=int(22 * scale), fill=(238, 94, 64), outline=(143, 60, 42), width=max(2, int(4 * scale)))
    draw.ellipse((cx - w // 2 + 8, cy - h // 2 + 8, cx + w // 2 - 8, cy + h // 2 - 8), outline=(255, 185, 94), width=max(2, int(4 * scale)))
    draw.rectangle((cx - w // 3, cy - h // 2 - 8, cx + w // 3, cy - h // 2 + 6), fill=(255, 183, 87))
    draw.rectangle((cx - w // 3, cy + h // 2 - 6, cx + w // 3, cy + h // 2 + 8), fill=(255, 183, 87))
    for i in range(3):
        x = cx - int(10 * scale) + i * int(10 * scale)
        draw.line((x, cy + h // 2 + 8, x - int(7 * scale), cy + h // 2 + int(35 * scale)), fill=(143, 60, 42), width=max(1, int(2 * scale)))


def draw_bamboo(draw: ImageDraw.ImageDraw, x: int, y: int, length: int, angle: float, scale: float = 1.0) -> None:
    dx = math.cos(angle) * length
    dy = math.sin(angle) * length
    width = max(8, int(18 * scale))
    draw.line((x, y, x + dx, y + dy), fill=(103, 143, 79), width=width)
    draw.line((x + 4, y - 4, x + dx + 4, y + dy - 4), fill=(157, 190, 104), width=max(3, width // 3))
    for t in [0.2, 0.42, 0.64, 0.84]:
        nx, ny = x + dx * t, y + dy * t
        draw.ellipse((nx - width, ny - width, nx + width, ny + width), outline=(68, 109, 62), width=3)


def draw_footprints(draw: ImageDraw.ImageDraw, x: int, y: int, scale: float, color=(169, 108, 72, 125)) -> None:
    for i in range(4):
        cx = x + i * int(58 * scale)
        cy = y + (i % 2) * int(26 * scale)
        draw.ellipse((cx - 14 * scale, cy - 25 * scale, cx + 17 * scale, cy + 24 * scale), fill=color)
        for j in range(3):
            tx = cx - 22 * scale + j * 18 * scale
            ty = cy - 35 * scale - abs(j - 1) * 4 * scale
            draw.ellipse((tx - 6 * scale, ty - 6 * scale, tx + 6 * scale, ty + 6 * scale), fill=color)


def draw_scroll_panel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], title: str, lines: list[str]) -> None:
    x1, y1, x2, y2 = box
    # Soft outer shadow.
    draw.rounded_rectangle((x1 + 8, y1 + 12, x2 + 8, y2 + 12), radius=36, fill=(134, 83, 45, 70))
    draw.rounded_rectangle((x1, y1, x2, y2), radius=36, fill=(255, 244, 215), outline=(170, 105, 56), width=5)
    draw.rounded_rectangle((x1 + 22, y1 + 18, x2 - 22, y2 - 18), radius=24, outline=(238, 194, 113), width=3)
    # Scroll rollers.
    roll_w = 56
    for rx in (x1 - 18, x2 - roll_w + 18):
        draw.rounded_rectangle((rx, y1 + 18, rx + roll_w, y2 - 18), radius=24, fill=(226, 159, 89), outline=(151, 90, 50), width=4)
        draw.rectangle((rx + roll_w // 2 - 4, y1 + 28, rx + roll_w // 2 + 4, y2 - 28), fill=(244, 195, 126))
    title_font = font(36)
    draw.text((x1 + 96, y1 + 42), title, font=title_font, fill=(116, 67, 42))
    small = font(41)
    icon_x = x1 + 108
    text_x = x1 + 184
    for idx, line in enumerate(lines):
        y = y1 + 106 + idx * 76
        draw_icon(draw, (icon_x, y + 23), idx)
        draw.text((text_x, y), line, font=small, fill=(87, 52, 37))


def draw_icon(draw: ImageDraw.ImageDraw, center: tuple[int, int], idx: int) -> None:
    cx, cy = center
    color = (203, 104, 60)
    fill = (255, 230, 185)
    if idx == 0:
        draw.rounded_rectangle((cx - 24, cy - 20, cx + 24, cy + 20), radius=8, outline=color, width=4, fill=fill)
        draw.polygon([(cx - 12, cy + 8), (cx - 2, cy - 4), (cx + 8, cy + 7), (cx + 17, cy - 6), (cx + 20, cy + 12), (cx - 18, cy + 12)], fill=(119, 160, 102))
        draw.ellipse((cx - 16, cy - 13, cx - 8, cy - 5), fill=(247, 180, 71))
    elif idx == 1:
        draw.rounded_rectangle((cx - 19, cy - 24, cx + 21, cy + 24), radius=6, outline=color, width=4, fill=fill)
        for yy in (-10, 2, 14):
            draw.line((cx - 8, cy + yy, cx + 11, cy + yy), fill=color, width=3)
        draw.line((cx + 25, cy + 18, cx + 43, cy - 9), fill=color, width=6)
        draw.polygon([(cx + 45, cy - 13), (cx + 52, cy - 24), (cx + 47, cy - 8)], fill=(255, 221, 127))
    else:
        draw.rounded_rectangle((cx - 23, cy - 20, cx + 23, cy + 20), radius=7, outline=color, width=4, fill=fill)
        draw.polygon([(cx - 7, cy - 10), (cx - 7, cy + 10), (cx + 11, cy)], fill=color)
        draw.arc((cx + 27, cy - 20, cx + 51, cy + 20), 270, 90, fill=color, width=5)


def draw_release_plate(kind: str) -> Image.Image:
    img = gradient((1080, 1440), (253, 228, 168), (177, 219, 159)).convert("RGBA")
    draw = ImageDraw.Draw(img, "RGBA")

    # Top scenic band, kept above the safe strip.
    draw.ellipse((-130, 205, 360, 520), fill=(108, 165, 98, 130))
    draw.ellipse((730, 205, 1220, 520), fill=(96, 155, 103, 110))
    draw.rectangle((0, 300, 1080, 352), fill=(150, 190, 112, 135))
    for x in (70, 165, 870, 970):
        draw_bamboo(draw, x, 70 + (x % 3) * 20, 210, math.radians(88 + (x % 2) * 8), 0.65)
    draw_lantern(draw, 78, 128, 0.7)
    draw_lantern(draw, 1004, 120, 0.66)
    draw_cloud(draw, 210, 310, 0.48)
    draw_cloud(draw, 872, 308, 0.44)

    # Header plaque.
    plaque = Image.new("RGBA", img.size, (0, 0, 0, 0))
    pdraw = ImageDraw.Draw(plaque, "RGBA")
    mask = rounded_mask(img.size, (176, 34, 904, 122), 36)
    add_soft_shadow(img, mask, (0, 10), 14)
    pdraw.rounded_rectangle((176, 34, 904, 122), radius=36, fill=(227, 151, 79), outline=(132, 82, 48), width=5)
    pdraw.rounded_rectangle((202, 52, 878, 106), radius=24, fill=(255, 207, 124), outline=(255, 239, 178), width=3)
    img.alpha_composite(plaque)

    headline = "绵羊姐姐讲故事" if kind == "main" else "成语故事"
    centered_text(draw, (202, 47, 878, 108), headline, font(44), (115, 59, 36), (255, 250, 224), 1)

    title = "《邯郸学步》"
    title_font = fit_font(title, 960, 104, serif=True)
    centered_text(draw, (44, 132, 1036, 238), title, title_font, (255, 246, 158), (138, 68, 38), 8)
    centered_text(draw, (44, 132, 1036, 238), title, title_font, (255, 246, 158), (255, 255, 220), 2)

    badge_font = font(32)
    for box, text in [((146, 262, 514, 326), "故事时长 1分53秒"), ((566, 262, 934, 326), "适合年龄 3-6岁")]:
        draw.rounded_rectangle(box, radius=26, fill=(255, 250, 218), outline=(203, 117, 63), width=4)
        centered_text(draw, box, text, badge_font, (126, 70, 42))

    # The exact full-width video strip. This is intentionally flat and late in the stack.
    sx, sy, sw, sh = SAFE_BOX
    draw.rectangle((sx, sy, sx + sw, sy + sh), fill=SAFE_COLOR)

    # Bottom band.
    draw.ellipse((-150, 1230, 330, 1530), fill=(103, 157, 94, 125))
    draw.ellipse((820, 1225, 1260, 1510), fill=(104, 162, 101, 115))
    draw_bamboo(draw, 70, 1260, 190, math.radians(-30), 0.55)
    draw_bamboo(draw, 985, 1245, 180, math.radians(214), 0.55)
    draw_footprints(draw, 760, 1380, 0.72)

    panel_title = "本期节目资料包" if kind == "main" else "成语故事发布资料"
    lines = ["背景视频 + PPT + 配乐", "文稿 + 标注", "示范视频"]
    draw_scroll_panel(draw, (78, 1018, 1002, 1372), panel_title, lines)

    return img.convert("RGB")


def draw_background() -> Image.Image:
    img = gradient((1920, 1080), (253, 230, 180), (196, 224, 190)).convert("RGBA")
    draw = ImageDraw.Draw(img, "RGBA")

    # Ancient Handan-inspired market/courtyard, deliberately soft and low-contrast.
    draw.rectangle((0, 650, 1920, 1080), fill=(194, 172, 120, 80))
    for i, x in enumerate(range(-80, 1500, 270)):
        roof_y = 320 + (i % 3) * 22
        draw.polygon([(x, roof_y + 88), (x + 130, roof_y), (x + 300, roof_y + 88)], fill=(149, 91, 62, 105))
        draw.rectangle((x + 24, roof_y + 88, x + 274, roof_y + 220), fill=(214, 166, 101, 70))
        draw.line((x + 18, roof_y + 90, x + 282, roof_y + 90), fill=(118, 70, 50, 110), width=12)
    for x in (80, 210, 350, 500, 645, 802):
        draw_bamboo(draw, x, 760, 430, math.radians(-80 + x % 9), 0.9)
    draw.rectangle((1280, 0, 1920, 1080), fill=(255, 245, 218, 95))

    # Soft city gate silhouette and stone path on the left story-frame side.
    draw.rounded_rectangle((315, 440, 910, 760), radius=40, fill=(133, 97, 70, 75))
    draw.rectangle((405, 520, 820, 760), fill=(104, 79, 59, 80))
    draw.arc((485, 548, 740, 880), 180, 360, fill=(74, 58, 47, 105), width=24)
    draw.polygon([(210, 1080), (610, 710), (1020, 1080)], fill=(216, 190, 142, 95))
    for i in range(12):
        y = 770 + i * 32
        draw.arc((360 - i * 20, y, 875 + i * 20, y + 80), 180, 360, fill=(139, 116, 88, 52), width=3)

    # Scroll and cloud motifs.
    draw_cloud(draw, 230, 210, 0.75, fill=(255, 248, 231, 125), outline=(238, 207, 158, 80))
    draw_cloud(draw, 1050, 180, 0.65, fill=(255, 248, 231, 95), outline=(238, 207, 158, 55))
    draw_lantern(draw, 1120, 255, 0.55)
    draw_lantern(draw, 1430, 235, 0.5)
    draw_footprints(draw, 650, 892, 0.9, color=(142, 91, 66, 80))

    img = img.filter(ImageFilter.GaussianBlur(4.8))
    overlay = Image.new("RGBA", img.size, (255, 243, 216, 0))
    od = ImageDraw.Draw(overlay, "RGBA")
    od.rectangle((0, 0, 1320, 1080), fill=(255, 242, 213, 32))
    for xx in range(1060, 1920):
        t = (xx - 1060) / 860
        alpha = int(28 + 142 * t * t)
        od.line((xx, 0, xx, 1080), fill=(255, 248, 229, alpha))
    img.alpha_composite(overlay)
    return img.convert("RGB")


def wood_texture(size: tuple[int, int], seed: int) -> Image.Image:
    random.seed(seed)
    w, h = size
    tex = gradient(size, (224, 156, 83), (194, 111, 54)).convert("RGBA")
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    for _ in range(44):
        y = random.randint(0, h)
        amp = random.randint(8, 24)
        phase = random.random() * math.tau
        color = random.choice([(126, 73, 45, 34), (255, 219, 145, 48), (151, 86, 50, 28)])
        pts = []
        for x in range(-20, w + 40, 28):
            yy = y + math.sin(x / 82 + phase) * amp + math.sin(x / 31 + phase * 0.7) * amp * 0.18
            pts.append((x, yy))
        draw.line(pts, fill=color, width=random.randint(2, 4))
    for _ in range(18):
        cx, cy = random.randint(30, w - 30), random.randint(20, h - 20)
        rx, ry = random.randint(22, 55), random.randint(9, 22)
        draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), outline=(124, 72, 42, 38), width=3)
        draw.ellipse((cx - rx // 2, cy - ry // 2, cx + rx // 2, cy + ry // 2), outline=(255, 210, 135, 34), width=2)
    tex.alpha_composite(overlay)
    return tex


def ring_mask(size: tuple[int, int], outer: tuple[int, int, int, int], inner: tuple[int, int, int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle(outer, radius=radius, fill=255)
    d.rectangle(inner, fill=0)
    return mask


def draw_frame(path: Path, label: str, box: tuple[int, int, int, int]) -> Image.Image:
    img = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")
    x, y, w, h = box
    pad = 52 if label == "A" else 64
    outer = (x - pad, y - pad, x + w + pad, y + h + pad)
    inner = (x, y, x + w, y + h)
    radius = 34 if label == "A" else 42
    mask = ring_mask(img.size, outer, inner, radius)
    add_soft_shadow(img, mask, (14, 18), 20, color=(66, 43, 28, 135))

    frame_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    texture = wood_texture((outer[2] - outer[0], outer[3] - outer[1]), 24 if label == "A" else 51)
    frame_layer.paste(texture, (outer[0], outer[1]), texture)
    frame_layer.putalpha(mask)
    img.alpha_composite(frame_layer)
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rounded_rectangle(outer, radius=radius, outline=(255, 224, 144, 255), width=10)
    draw.rounded_rectangle((outer[0] + 13, outer[1] + 13, outer[2] - 13, outer[3] - 13), radius=max(8, radius - 10), outline=(130, 78, 45, 205), width=7)
    draw.rectangle(inner, fill=(0, 0, 0, 0))

    # Corner wool/cloud puffs and small Chinese-story details outside the window.
    corner_scale = 0.42 if label == "A" else 0.52
    corners = [
        (outer[0] + 42, outer[1] + 38, 1),
        (outer[2] - 42, outer[1] + 38, -1),
        (outer[0] + 42, outer[3] - 38, 1),
        (outer[2] - 42, outer[3] - 38, -1),
    ]
    for cx, cy, side in corners:
        draw_cloud(draw, cx, cy, corner_scale, fill=(255, 250, 237, 255), outline=(224, 188, 138, 170))
        hx = cx - side * int(52 * corner_scale)
        draw.arc((hx - 26, cy - 24, hx + 26, cy + 24), 80 if side > 0 else 100, 300 if side > 0 else 320, fill=(160, 91, 55, 230), width=8)

    if label == "A":
        draw_lantern(draw, outer[2] + 78, outer[1] + 120, 0.48)
        draw_bamboo(draw, outer[0] - 26, outer[3] + 44, 210, math.radians(-16), 0.62)
        draw_footprints(draw, outer[2] - 240, outer[3] + 52, 0.52, color=(152, 91, 59, 135))
    else:
        draw_lantern(draw, outer[2] + 80, outer[1] + 118, 0.58)
        draw_lantern(draw, outer[0] - 76, outer[1] + 118, 0.54)
        draw_bamboo(draw, outer[0] - 4, outer[3] + 62, 260, math.radians(-12), 0.75)
        draw_bamboo(draw, outer[2] + 4, outer[3] + 54, 245, math.radians(192), 0.72)
        draw_footprints(draw, outer[0] + 170, outer[3] + 62, 0.68, color=(152, 91, 59, 130))

    # Restore the exact video rectangle to full transparency after all decoration.
    clear = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    img.paste(clear, (x, y))
    img.save(path)
    return img


def write_assets(out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "main_release_plate": out_dir / "main_release_plate.png",
        "library_release_plate": out_dir / "library_release_plate.png",
        "main_background_16x9": out_dir / "main_background_16x9.png",
        "story_frame_a": out_dir / "story_frame_a.png",
        "story_frame_b": out_dir / "story_frame_b.png",
    }
    draw_release_plate("main").save(outputs["main_release_plate"])
    draw_release_plate("library").save(outputs["library_release_plate"])
    draw_background().save(outputs["main_background_16x9"])
    draw_frame(outputs["story_frame_a"], "A", (210, 270, 910, 512))
    draw_frame(outputs["story_frame_b"], "B", (356, 180, 1209, 680))
    return outputs


def qa(paths: dict[str, Path]) -> str:
    rows: list[str] = []
    issues: list[str] = []
    for key, path in paths.items():
        with Image.open(path) as im:
            mode = im.mode
            size = im.size
            note = "ok"
            if key.endswith("plate") or "release_plate" in key:
                if size != (1080, 1440):
                    issues.append(f"{path.name}: expected 1080x1440, got {size}")
                strip = im.crop((0, 360, 1080, 968)).convert("RGB")
                diff = ImageChops.difference(strip, Image.new("RGB", strip.size, SAFE_COLOR))
                if diff.getbbox() is not None:
                    issues.append(f"{path.name}: safe video strip is not flat SAFE_COLOR")
                    note = "safe strip issue"
            if key == "main_background_16x9" and size != (1920, 1080):
                issues.append(f"{path.name}: expected 1920x1080, got {size}")
            if key.startswith("story_frame"):
                if size != (1920, 1080):
                    issues.append(f"{path.name}: expected 1920x1080, got {size}")
                if mode != "RGBA":
                    issues.append(f"{path.name}: expected RGBA, got {mode}")
                alpha = im.getchannel("A")
                box = (210, 270, 1120, 782) if key.endswith("_a") else (356, 180, 1565, 860)
                if alpha.crop(box).getbbox() is not None:
                    issues.append(f"{path.name}: video window is not fully transparent")
                    note = "window alpha issue"
                for point in [(0, 0), (1919, 0), (0, 1079), (1919, 1079)]:
                    if alpha.getpixel(point) != 0:
                        issues.append(f"{path.name}: corner {point} is not transparent")
            rows.append(f"- {path.name}: {size[0]}x{size[1]} {mode} {note}")
    report = "\n".join(rows)
    if issues:
        report += "\n\nIssues:\n" + "\n".join(f"- {item}" for item in issues)
    else:
        report += "\n\nAll checks passed."
    return report


def main() -> None:
    preview_paths = write_assets(PREVIEW_DIR)
    final_paths = write_assets(OUT_DIR)
    report = qa(final_paths)
    (OUT_DIR / "theme_assets_qa_report.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"\nSaved previews: {PREVIEW_DIR.resolve()}")


if __name__ == "__main__":
    main()
