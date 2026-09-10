"""Lossless subtitle layer cropping; full-frame coordinates and cue ordering survive."""
from pathlib import Path
from PIL import Image


def crop_subtitle_layers(paths, *, width, height):
    """Crop only transparent padding, preserving 4:2:0 chroma phase and two-pixel guard.

    Non-RGBA, empty or unexpected-size images fall back to the original layer.
    The original subtitle files remain untouched and are rendered from current cues.
    """
    layers = []
    for path in paths:
        path = Path(path)
        with Image.open(path) as image:
            box = image.getchannel('A').getbbox() if image.mode == 'RGBA' else None
            if image.size != (width, height) or box is None:
                layers.append((path, 0, 0))
                continue
            x, y, right, bottom = box
            box = (max(0, x//2*2-2), max(0, y//2*2-2),
                   min(width, (right+3)//2*2), min(height, (bottom+3)//2*2))
            output = path.with_name(path.stem + '_cropped.png')
            image.crop(box).save(output)
            layers.append((output, box[0], box[1]))
    return layers


def cropped_overlay_chain(cues, layers, first_image_input=1):
    if len(cues) != len(layers):
        raise ValueError('Subtitle cue/layer ordering mismatch')
    filters = []
    for offset, (cue, (_, x, y)) in enumerate(zip(cues, layers)):
        current = '[0:v]' if offset == 0 else f'[v{offset-1}]'
        output = '[v]' if offset == len(cues)-1 else f'[v{offset}]'
        filters.append(f"{current}[{first_image_input+offset}:v]overlay={x}:{y}:enable='between(t,{cue.start:.3f},{cue.end:.3f})'{output}")
    return ';'.join(filters)
