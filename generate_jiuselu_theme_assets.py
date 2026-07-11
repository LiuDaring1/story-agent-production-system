from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageStat


GEN_DIR = Path("/Users/baiyanglin/.codex/generated_images/019e7d03-9946-7ff1-9d18-64f2d65370f0")
OUT = Path("/Users/baiyanglin/Desktop/故事剪辑：九色鹿/04_发布视频/theme_assets")

MAIN_TOP_SRC = GEN_DIR / "ig_021964c38612230b016a1be89df34c8197882a3b3351d44ae7.png"
MAIN_BOTTOM_SRC = GEN_DIR / "ig_021964c38612230b016a1bea100a8481978d6ce213625e517b.png"
LIB_TOP_SRC = GEN_DIR / "ig_021964c38612230b016a1bea4deabc8197af33387c35d1f24d.png"
LIB_BOTTOM_SRC = GEN_DIR / "ig_021964c38612230b016a1bea806f948197bb5efb63393845ea.png"
BG_SRC = GEN_DIR / "ig_021964c38612230b016a1beac83c5081979303e3da9d9d2d11.png"
FRAME_SRC = GEN_DIR / "ig_021964c38612230b016a1beb68ed8c8197b2d5a79870c2f73f.png"

SAFE_COLOR = (252, 244, 220)
MAGENTA = (255, 0, 255)


def cover_resize(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    target_w, target_h = size
    scale = max(target_w / im.width, target_h / im.height)
    resized = im.resize((round(im.width * scale), round(im.height * scale)), Image.Resampling.LANCZOS)
    left = (resized.width - target_w) // 2
    top = (resized.height - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def save_rgb(src: Path, dst: Path, size: tuple[int, int]) -> None:
    im = Image.open(src).convert("RGB")
    cover_resize(im, size).save(dst)


def make_plate(top: Path, bottom: Path, dst: Path) -> None:
    plate = Image.new("RGB", (1080, 1440), SAFE_COLOR)
    plate.paste(Image.open(top).convert("RGB"), (0, 0))
    plate.paste(Image.open(bottom).convert("RGB"), (0, 968))
    plate.save(dst)


def frame_alpha(src: Path) -> Image.Image:
    im = Image.open(src).convert("RGBA")
    pix = im.load()
    alpha = Image.new("L", im.size, 0)
    ap = alpha.load()
    for y in range(im.height):
        for x in range(im.width):
            r, g, b, a = pix[x, y]
            # The generated source uses a flat bright-magenta key. Keep antialiased
            # edge pixels with a soft alpha instead of a hard jagged cut.
            dist = abs(r - 255) + abs(g - 0) + abs(b - 255)
            if r > 220 and b > 180 and g < 90:
                ap[x, y] = 0 if dist < 90 else min(255, dist * 3)
            else:
                ap[x, y] = a
    alpha = alpha.filter(ImageFilter.GaussianBlur(0.25))
    im.putalpha(alpha)
    return im


def make_story_frame() -> None:
    raw = frame_alpha(FRAME_SRC)
    bbox = raw.getchannel("A").getbbox()
    if bbox is None:
        raise RuntimeError("story frame source has no visible frame")
    cropped = raw.crop(bbox)

    # Fit the generated left-side frame so its opening wraps the A window
    # (210,270,910,512) and its four sides sit close enough for pipeline QA.
    target_outer = (112, 86, 1224, 906)
    target_size = (target_outer[2] - target_outer[0], target_outer[3] - target_outer[1])
    fitted = cropped.resize(target_size, Image.Resampling.LANCZOS)

    alpha_canvas = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    alpha_canvas.alpha_composite(fitted, (target_outer[0], target_outer[1]))

    source_canvas = Image.new("RGB", (1920, 1080), MAGENTA)
    source_canvas.paste(fitted.convert("RGB"), (target_outer[0], target_outer[1]), fitted.getchannel("A"))
    source_canvas.save(OUT / "story_frame_source.png")
    alpha_canvas.save(OUT / "story_frame_a.png")


def qa_report() -> str:
    rows: list[str] = []
    issues: list[str] = []
    expected = {
        "main_release_plate_top.png": (1080, 360),
        "main_release_plate_bottom.png": (1080, 472),
        "main_release_plate.png": (1080, 1440),
        "library_release_plate_top.png": (1080, 360),
        "library_release_plate_bottom.png": (1080, 472),
        "library_release_plate.png": (1080, 1440),
        "main_background_16x9.png": (1920, 1080),
        "story_frame_source.png": (1920, 1080),
        "story_frame_a.png": (1920, 1080),
    }
    for name, size in expected.items():
        path = OUT / name
        with Image.open(path) as im:
            note = "ok"
            if im.size != size:
                issues.append(f"{name}: expected {size}, got {im.size}")
                note = "bad size"
            if name.endswith("_release_plate.png"):
                strip = im.convert("RGB").crop((0, 360, 1080, 968)).resize((108, 61))
                if max(ImageStat.Stat(strip).stddev) > 1:
                    issues.append(f"{name}: middle video safe area is not flat")
                    note = "safe strip issue"
                diff = ImageChops.difference(strip, Image.new("RGB", strip.size, SAFE_COLOR))
                if diff.getbbox() is not None:
                    note = "safe strip flat"
            if name == "story_frame_a.png":
                if im.mode != "RGBA":
                    issues.append(f"{name}: expected RGBA")
                alpha = im.getchannel("A")
                if alpha.getpixel((0, 0)) > 8 or alpha.getpixel((1919, 1079)) > 8:
                    issues.append(f"{name}: transparent corners failed")
                center = alpha.crop((356, 352, 974, 700))
                if center.getbbox() is not None:
                    issues.append(f"{name}: inner opening has visible pixels")
            rows.append(f"- {name}: {im.size[0]}x{im.size[1]} {im.mode} {note}")
    rows.append("")
    rows.append("All checks passed." if not issues else "Issues:\n" + "\n".join(f"- {i}" for i in issues))
    return "\n".join(rows)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    save_rgb(MAIN_TOP_SRC, OUT / "main_release_plate_top.png", (1080, 360))
    save_rgb(MAIN_BOTTOM_SRC, OUT / "main_release_plate_bottom.png", (1080, 472))
    save_rgb(LIB_TOP_SRC, OUT / "library_release_plate_top.png", (1080, 360))
    save_rgb(LIB_BOTTOM_SRC, OUT / "library_release_plate_bottom.png", (1080, 472))
    save_rgb(BG_SRC, OUT / "main_background_16x9.png", (1920, 1080))
    make_plate(OUT / "main_release_plate_top.png", OUT / "main_release_plate_bottom.png", OUT / "main_release_plate.png")
    make_plate(OUT / "library_release_plate_top.png", OUT / "library_release_plate_bottom.png", OUT / "library_release_plate.png")
    make_story_frame()
    report = qa_report()
    (OUT / "theme_assets_qa_report.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
