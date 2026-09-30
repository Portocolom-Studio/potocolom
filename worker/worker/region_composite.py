from __future__ import annotations

from PIL import Image, ImageChops, ImageFilter

COMPOSITE_DILATION_PX = 8
COMPOSITE_FEATHER_PX = 8
# MAX_COMPOSITE_FRACTION is the fraction of the frame covered by the dilated
# (not yet feathered) change mask: measured 0.0065 for one small stroke or
# erasure and 0.046 for closing a shape in scripts/prototype-region-composite.py.
# It is a calibration knob, not a derived value.
MAX_COMPOSITE_FRACTION = 0.02


def max_channel_difference(
    previous: Image.Image, current: Image.Image
) -> Image.Image:
    if previous.size != current.size:
        raise ValueError("images must have the same size")
    difference = ImageChops.difference(previous.convert("RGB"), current.convert("RGB"))
    channels = difference.split()
    return ImageChops.lighter(
        ImageChops.lighter(channels[0], channels[1]), channels[2]
    )


def sketch_change_mask(previous: Image.Image, current: Image.Image) -> Image.Image:
    changed = max_channel_difference(previous, current)
    return changed.point(lambda value: 255 if value else 0, mode="L")


def feather_change_mask(
    mask: Image.Image, dilation_px: int, feather_px: int
) -> Image.Image:
    if dilation_px < 0 or feather_px < 0:
        raise ValueError("dilation_px and feather_px must be non-negative")
    binary = mask.convert("L").point(lambda value: 255 if value else 0, mode="L")
    if dilation_px:
        # MaxFilter(17) costs about 66 ms at 512 px and the box blur 2.5 ms for
        # the same mask; each pass is capped at radius 8 because Pillow rounds
        # the blur, so one isolated 255 pixel averaged over a box wider than
        # about 21 px rounds to 0 and would vanish.
        remaining = dilation_px
        while remaining:
            radius = min(8, remaining)
            binary = binary.filter(ImageFilter.BoxBlur(radius)).point(
                lambda value: 255 if value else 0, mode="L",
            )
            remaining -= radius
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


def keep_unchanged_pixels(
    previous_sketch: Image.Image,
    previous_image: Image.Image,
    sketch: Image.Image,
    image: Image.Image,
) -> Image.Image:
    """Blend `image` over `previous_image` through the mask of the change.

    Identical sketches are not a special case: their empty mask composites to
    exactly `previous_image`, which is what the session last delivered. Any
    mismatch of the four sizes means nothing can be trusted about them, so the
    new frame goes out whole.
    """
    sizes = {previous_sketch.size, previous_image.size, sketch.size, image.size}
    if len(sizes) != 1:
        return image
    dilated = feather_change_mask(
        sketch_change_mask(previous_sketch, sketch), COMPOSITE_DILATION_PX, 0,
    )
    covered = dilated.histogram()[255] / (dilated.width * dilated.height)
    if covered > MAX_COMPOSITE_FRACTION:
        return image
    # The dilated mask doubles as the feathering input: dilating again would
    # widen the change the coverage was just measured on.
    alpha = feather_change_mask(dilated, 0, COMPOSITE_FEATHER_PX)
    return composite_rgb(previous_image, image, alpha)
