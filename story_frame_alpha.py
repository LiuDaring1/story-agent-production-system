"""Inspect actual alpha samples, without keying, erosion or geometry changes."""
from PIL import Image

def validate_native_frame_alpha(image):
    if image.mode not in {'RGBA', 'LA'} and 'transparency' not in image.info:
        raise ValueError('Frame has no actual alpha channel')
    rgba = image.convert('RGBA')
    alpha = rgba.getchannel('A')
    low, high = alpha.getextrema()
    if low != 0 or high == 0:
        raise ValueError('Frame must contain actual transparent and visible pixels')
    width, height = image.size
    for point in ((0, 0), (width-1, 0), (0, height-1), (width-1,height-1), (width//2,height//2)):
        if alpha.getpixel(point) != 0:
            raise ValueError('Frame exterior and aperture must be actually transparent')
    # A central opening must be enclosed; transparent corners cannot connect to it.
    from PIL import ImageDraw
    mask = alpha.point(lambda a: 255 if a == 0 else 0)
    ImageDraw.floodfill(mask, (width//2, height//2), 128)
    if any(mask.getpixel(p) == 128 for p in ((0,0),(width-1,0),(0,height-1),(width-1,height-1))):
        raise ValueError('Frame transparent aperture is not enclosed')
    return {'actual_alpha': True, 'transparent_pixels': alpha.histogram()[0],
            'semitransparent_pixels': sum(alpha.histogram()[1:255]),
            'opaque_pixels': alpha.histogram()[255]}
