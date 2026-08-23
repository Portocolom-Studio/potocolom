from __future__ import annotations

from PIL import Image, ImageChops, ImageFilter


def sketch_change_mask(previous: Image.Image, current: Image.Image) -> Image.Image:
    if previous.size != current.size:
        raise ValueError("images must have the same size")
    previous_rgb = previous.convert("RGB")
    current_rgb = current.convert("RGB")
    difference = ImageChops.difference(previous_rgb, current_rgb)
    channels = difference.split()
    changed = ImageChops.lighter(
        ImageChops.lighter(channels[0], channels[1]), channels[2]
    )
    return changed.point(lambda value: 255 if value else 0, mode="L")


def feather_change_mask(
    mask: Image.Image, dilation_px: int, feather_px: int
) -> Image.Image:
    if dilation_px < 0 or feather_px < 0:
        raise ValueError("dilation_px and feather_px must be non-negative")
    binary = mask.convert("L").point(lambda value: 255 if value else 0, mode="L")
    if dilation_px:
        binary = binary.filter(ImageFilter.MaxFilter(dilation_px * 2 + 1))
    if feather_px:
        return binary.filter(ImageFilter.GaussianBlur(feather_px))
    return binary


def composite_rgb(
    previous: Image.Image, current: Image.Image, alpha: Image.Image
) -> Image.Image:
    if previous.size != current.size or previous.size != alpha.size:
        raise ValueError("images and alpha must have the same size")
    previous_rgb = previous.convert("RGB")
    current_rgb = current.convert("RGB")
    alpha_l = alpha.convert("L")
    return Image.composite(current_rgb, previous_rgb, alpha_l)
