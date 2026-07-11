from pathlib import Path
from PIL import Image


OUT = Path("/Users/baiyanglin/Desktop/故事剪辑：孟母三迁/04_发布视频/theme_assets")

SRC = {
    "main_top": Path("/Users/baiyanglin/.codex/generated_images/019e5f9f-a19b-7891-8d70-6d3b25398d08/ig_0fdc42e266bc50cd016a14631b56a8819b8c30c7a9f3a99afb.png"),
    "main_bottom": Path("/Users/baiyanglin/.codex/generated_images/019e5f9f-a19b-7891-8d70-6d3b25398d08/ig_0fdc42e266bc50cd016a1465e8cfb4819ba8ec8a9530e8941e.png"),
    "library_top": Path("/Users/baiyanglin/.codex/generated_images/019e5f9f-a19b-7891-8d70-6d3b25398d08/ig_0fdc42e266bc50cd016a14637c73b0819b8a361c56afeb43ac.png"),
    "library_bottom": Path("/Users/baiyanglin/.codex/generated_images/019e5f9f-a19b-7891-8d70-6d3b25398d08/ig_0fdc42e266bc50cd016a146622cbec819bbb9a0ac16b06dae7.png"),
    "background": Path("/Users/baiyanglin/.codex/generated_images/019e5f9f-a19b-7891-8d70-6d3b25398d08/ig_0fdc42e266bc50cd016a1463e09234819ba03e97e63342b06f.png"),
    "frame": Path("/Users/baiyanglin/.codex/generated_images/019e5f9f-a19b-7891-8d70-6d3b25398d08/ig_0fdc42e266bc50cd016a1464208b78819b99f6f7ac3507f3b1.png"),
}


def cover_resize(im, size):
    im = im.convert("RGBA")
    target_w, target_h = size
    scale = max(target_w / im.width, target_h / im.height)
    resized = im.resize((round(im.width * scale), round(im.height * scale)), Image.Resampling.LANCZOS)
    left = (resized.width - target_w) // 2
    top = (resized.height - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def save_rgb(im, path):
    im.convert("RGB").save(path, "PNG", optimize=True)


def make_plate(top, bottom, out):
    plate = Image.new("RGB", (1080, 1440), (250, 240, 214))
    plate.paste(Image.open(top).convert("RGB"), (0, 0))
    # Pure blank video safe zone: x=0, y=360, w=1080, h=608.
    plate.paste(Image.new("RGB", (1080, 608), (250, 240, 214)), (0, 360))
    plate.paste(Image.open(bottom).convert("RGB"), (0, 968))
    plate.save(out, "PNG", optimize=True)


def chroma_to_alpha(im, key=(255, 0, 255), tolerance=34):
    im = im.convert("RGBA")
    px = im.load()
    kr, kg, kb = key
    for y in range(im.height):
        for x in range(im.width):
            r, g, b, a = px[x, y]
            dist = abs(r - kr) + abs(g - kg) + abs(b - kb)
            if dist <= tolerance:
                px[x, y] = (r, g, b, 0)
            elif dist <= tolerance * 3:
                alpha = int(255 * (dist - tolerance) / (tolerance * 2))
                px[x, y] = (r, g, b, max(0, min(255, alpha)))
    return im


def make_frame_source():
    src = Image.open(SRC["frame"]).convert("RGBA")
    keyed = chroma_to_alpha(src, tolerance=44)
    bbox = keyed.getbbox()
    frame = keyed.crop(bbox)

    # Place the generated frame around the requested A-scene window.
    target_outer = (185, 230, 1180, 835)
    target_w = target_outer[2] - target_outer[0]
    target_h = target_outer[3] - target_outer[1]
    frame.thumbnail((target_w, target_h), Image.Resampling.LANCZOS)
    x = target_outer[0] + (target_w - frame.width) // 2
    y = target_outer[1] + (target_h - frame.height) // 2

    source = Image.new("RGB", (1920, 1080), (255, 0, 255))
    source_rgba = source.convert("RGBA")
    source_rgba.alpha_composite(frame, (x, y))
    save_rgb(source_rgba, OUT / "story_frame_source.png")

    transparent = chroma_to_alpha(source_rgba, tolerance=38)
    transparent.save(OUT / "story_frame_a.png", "PNG", optimize=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)

    save_rgb(cover_resize(Image.open(SRC["main_top"]), (1080, 360)), OUT / "main_release_plate_top.png")
    save_rgb(cover_resize(Image.open(SRC["main_bottom"]), (1080, 472)), OUT / "main_release_plate_bottom.png")
    save_rgb(cover_resize(Image.open(SRC["library_top"]), (1080, 360)), OUT / "library_release_plate_top.png")
    save_rgb(cover_resize(Image.open(SRC["library_bottom"]), (1080, 472)), OUT / "library_release_plate_bottom.png")
    save_rgb(cover_resize(Image.open(SRC["background"]), (1920, 1080)), OUT / "main_background_16x9.png")

    make_plate(OUT / "main_release_plate_top.png", OUT / "main_release_plate_bottom.png", OUT / "main_release_plate.png")
    make_plate(OUT / "library_release_plate_top.png", OUT / "library_release_plate_bottom.png", OUT / "library_release_plate.png")
    make_frame_source()

    for name in [
        "main_release_plate.png",
        "main_release_plate_top.png",
        "main_release_plate_bottom.png",
        "library_release_plate.png",
        "library_release_plate_top.png",
        "library_release_plate_bottom.png",
        "main_background_16x9.png",
        "story_frame_source.png",
        "story_frame_a.png",
    ]:
        im = Image.open(OUT / name)
        print(f"{name}: {im.size} {im.mode}")


if __name__ == "__main__":
    main()
