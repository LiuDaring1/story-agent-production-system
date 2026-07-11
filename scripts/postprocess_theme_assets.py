from pathlib import Path

from PIL import Image


GEN_DIR = Path("/Users/baiyanglin/.codex/generated_images/019e818d-ef9c-79f2-9d98-36b522d25958")
OUT_DIR = Path("/Users/baiyanglin/Desktop/故事剪辑：煮酒论英雄/04_发布视频/theme_assets")

SOURCES = {
    "main_top": GEN_DIR / "ig_000c425d7eae04f1016a1d121573c48190af0e6b3067a9374d.png",
    "main_bottom": GEN_DIR / "ig_000c425d7eae04f1016a1d124406488190b3b04a6c6ccb1d56.png",
    "library_top": GEN_DIR / "ig_000c425d7eae04f1016a1d127d2c908190a23c67df43fb9332.png",
    "library_bottom": GEN_DIR / "ig_000c425d7eae04f1016a1d12aee6688190a393514640d2a87c.png",
    "background": GEN_DIR / "ig_000c425d7eae04f1016a1d12e9124c819082150c609316e868.png",
    "frame": GEN_DIR / "ig_000c425d7eae04f1016a1d1321e0148190a425d4630b916a2a.png",
}


def cover_resize(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    target_w, target_h = size
    src_w, src_h = im.size
    scale = max(target_w / src_w, target_h / src_h)
    new_size = (round(src_w * scale), round(src_h * scale))
    resized = im.resize(new_size, Image.Resampling.LANCZOS)
    left = (resized.width - target_w) // 2
    top = (resized.height - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def make_plate(top_path: Path, bottom_path: Path, top_out: Path, bottom_out: Path, final_out: Path) -> None:
    top = cover_resize(Image.open(top_path).convert("RGB"), (1080, 360))
    bottom = cover_resize(Image.open(bottom_path).convert("RGB"), (1080, 472))
    top.save(top_out)
    bottom.save(bottom_out)

    blank_color = (244, 224, 183)
    plate = Image.new("RGB", (1080, 1440), blank_color)
    plate.paste(top, (0, 0))
    plate.paste(bottom, (0, 968))
    plate.save(final_out)


def make_transparent_frame(source_path: Path, source_out: Path, alpha_out: Path) -> None:
    source = cover_resize(Image.open(source_path).convert("RGB"), (1920, 1080))
    source.save(source_out)

    rgba = source.convert("RGBA")
    px = rgba.load()
    for y in range(rgba.height):
        for x in range(rgba.width):
            r, g, b, a = px[x, y]
            if r > 210 and b > 210 and g < 80:
                px[x, y] = (r, g, b, 0)
            elif r > 185 and b > 185 and g < 110:
                # Feather antialiased magenta edges without cutting into the frame.
                alpha = int(max(0, min(255, (120 - g) * 2.2)))
                px[x, y] = (r, g, b, min(a, alpha))
    rgba.save(alpha_out)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    make_plate(
        SOURCES["main_top"],
        SOURCES["main_bottom"],
        OUT_DIR / "main_release_plate_top.png",
        OUT_DIR / "main_release_plate_bottom.png",
        OUT_DIR / "main_release_plate.png",
    )
    make_plate(
        SOURCES["library_top"],
        SOURCES["library_bottom"],
        OUT_DIR / "library_release_plate_top.png",
        OUT_DIR / "library_release_plate_bottom.png",
        OUT_DIR / "library_release_plate.png",
    )

    background = cover_resize(Image.open(SOURCES["background"]).convert("RGB"), (1920, 1080))
    background.save(OUT_DIR / "main_background_16x9.png")

    make_transparent_frame(
        SOURCES["frame"],
        OUT_DIR / "story_frame_source.png",
        OUT_DIR / "story_frame_a.png",
    )


if __name__ == "__main__":
    main()
