import pytest
from PIL import Image, ImageDraw, ImageFilter

from worker.region_composite import (
    COMPOSITE_DILATION_PX,
    COMPOSITE_FEATHER_PX,
    MAX_COMPOSITE_FRACTION,
    composite_rgb,
    feather_change_mask,
    keep_unchanged_pixels,
    max_channel_difference,
    selection_alpha,
    sketch_change_mask,
)


def test_max_channel_difference_returns_all_zero_for_identical_images():
    image = Image.new("RGB", (3, 2), (12, 34, 56))

    result = max_channel_difference(image, image.copy())

    assert result.mode == "L"
    assert result.size == image.size
    assert set(result.getdata()) == {0}


def test_max_channel_difference_returns_single_channel_delta():
    previous = Image.new("RGB", (3, 2), (10, 20, 30))
    current = previous.copy()
    current.putpixel((1, 1), (10, 77, 30))

    result = max_channel_difference(previous, current)

    assert result.getpixel((1, 1)) == 57
    assert sum(pixel != 0 for pixel in result.getdata()) == 1


def test_max_channel_difference_rejects_size_mismatch():
    with pytest.raises(ValueError):
        max_channel_difference(Image.new("RGB", (2, 2)), Image.new("RGB", (3, 2)))


def test_sketch_change_mask_returns_all_zero_for_identical_images():
    image = Image.new("RGB", (3, 2), (12, 34, 56))

    result = sketch_change_mask(image, image.copy())

    assert result.mode == "L"
    assert result.size == image.size
    assert set(result.getdata()) == {0}


def test_sketch_change_mask_marks_one_changed_pixel():
    previous = Image.new("RGB", (3, 3), (0, 0, 0))
    current = previous.copy()
    current.putpixel((1, 2), (255, 255, 255))

    result = sketch_change_mask(previous, current)

    assert result.getpixel((1, 2)) == 255
    assert sum(pixel != 0 for pixel in result.getdata()) == 1


def test_sketch_change_mask_rejects_size_mismatch():
    with pytest.raises(ValueError):
        sketch_change_mask(Image.new("RGB", (2, 2)), Image.new("RGB", (3, 2)))


def test_feather_change_mask_dilation_expands_one_pixel_mark():
    mask = Image.new("L", (5, 5), 0)
    mask.putpixel((2, 2), 255)

    result = feather_change_mask(mask, dilation_px=1, feather_px=0)

    assert all(result.getpixel((x, y)) == 255 for x in range(1, 4) for y in range(1, 4))
    assert result.getpixel((0, 0)) == 0
    assert sum(pixel == 255 for pixel in result.getdata()) == 9


def test_feather_change_mask_feathering_creates_intermediate_edge_values():
    mask = Image.new("L", (5, 5), 0)
    mask.putpixel((2, 2), 255)

    result = feather_change_mask(mask, dilation_px=0, feather_px=1)

    assert any(0 < pixel < 255 for pixel in result.getdata())


def test_composite_rgb_uses_previous_and_current_at_alpha_extremes():
    previous = Image.new("RGB", (2, 1), (10, 20, 30))
    current = Image.new("RGB", (2, 1), (200, 210, 220))

    result = composite_rgb(
        previous,
        current,
        Image.new("L", (2, 1), 0),
    )
    current_result = composite_rgb(
        previous,
        current,
        Image.new("L", (2, 1), 255),
    )

    assert result.getpixel((0, 0)) == (10, 20, 30)
    assert current_result.getpixel((0, 0)) == (200, 210, 220)


def test_composite_rgb_blends_intermediate_alpha():
    previous = Image.new("RGB", (1, 1), (0, 100, 200))
    current = Image.new("RGB", (1, 1), (200, 200, 0))

    result = composite_rgb(previous, current, Image.new("L", (1, 1), 128))

    pixel = result.getpixel((0, 0))
    assert result.mode == "RGB"
    assert all(0 < channel < 200 for channel in pixel)
    assert pixel == (100, 150, 100)


def test_composite_rgb_rejects_size_mismatch():
    previous = Image.new("RGB", (2, 2))
    current = Image.new("RGB", (2, 2))

    with pytest.raises(ValueError):
        composite_rgb(previous, Image.new("RGB", (3, 2)), Image.new("L", (2, 2)))
    with pytest.raises(ValueError):
        composite_rgb(previous, current, Image.new("L", (3, 2)))


@pytest.mark.parametrize("dilation_px, feather_px", [(-1, 0), (0, -1), (-1, -1)])
def test_feather_change_mask_rejects_negative_parameters(dilation_px, feather_px):
    with pytest.raises(ValueError):
        feather_change_mask(Image.new("L", (2, 2)), dilation_px, feather_px)


@pytest.mark.parametrize("dilation_px", [8, 16])
@pytest.mark.parametrize("position", [(32, 32), (0, 0), (0, 32), (63, 63)])
def test_feather_change_mask_box_dilation_matches_max_filter(dilation_px, position):
    """The reference is the MaxFilter the box blur replaced, computed here
    rather than restated: the dilation must be the same square, including at
    the image edge, where a clipped box and a rank filter could disagree."""
    mask = Image.new("L", (64, 64), 0)
    mask.putpixel(position, 255)
    reference = mask.filter(ImageFilter.MaxFilter(dilation_px * 2 + 1))

    result = feather_change_mask(mask, dilation_px=dilation_px, feather_px=0)

    assert result.tobytes() == reference.tobytes()


def _square_change_sketch(size, side):
    sketch = Image.new("RGB", size, (0, 0, 0))
    start = (size[0] - side) // 2
    ImageDraw.Draw(sketch).rectangle(
        [start, start, start + side - 1, start + side - 1],
        fill=(255, 255, 255),
    )
    return sketch


def test_keep_unchanged_pixels_keeps_far_pixels_and_takes_the_change():
    size = (512, 512)
    previous_sketch = Image.new("RGB", size, (0, 0, 0))
    sketch = _square_change_sketch(size, 48)
    previous_image = Image.new("RGB", size, (10, 20, 30))
    image = Image.new("RGB", size, (200, 100, 50))

    result = keep_unchanged_pixels(previous_sketch, previous_image, sketch, image)

    outside = 4 * COMPOSITE_FEATHER_PX
    assert result.getpixel((outside, outside)) == (10, 20, 30)
    assert result.getpixel((500, 500)) == (10, 20, 30)
    assert result.getpixel((256, 256)) == (200, 100, 50)


def test_keep_unchanged_pixels_sends_the_frame_above_the_fraction():
    size = (512, 512)
    previous_sketch = Image.new("RGB", size, (0, 0, 0))
    sketch = _square_change_sketch(size, 120)
    previous_image = Image.new("RGB", size, (10, 20, 30))
    image = Image.new("RGB", size, (200, 100, 50))
    dilated_side = 120 + 2 * COMPOSITE_DILATION_PX
    assert dilated_side * dilated_side / (size[0] * size[1]) > MAX_COMPOSITE_FRACTION

    result = keep_unchanged_pixels(previous_sketch, previous_image, sketch, image)

    assert result is image


def test_keep_unchanged_pixels_identical_sketches_return_the_previous_frame():
    size = (512, 512)
    sketch = _square_change_sketch(size, 48)
    previous_image = Image.new("RGB", size, (10, 20, 30))
    image = Image.new("RGB", size, (200, 100, 50))

    result = keep_unchanged_pixels(sketch, previous_image, sketch.copy(), image)

    assert result.tobytes() == previous_image.tobytes()


def test_keep_unchanged_pixels_returns_the_frame_on_a_size_mismatch():
    size = (512, 512)
    previous_sketch = Image.new("RGB", size, (0, 0, 0))
    sketch = _square_change_sketch(size, 48)
    previous_image = Image.new("RGB", size, (10, 20, 30))
    image = Image.new("RGB", size, (200, 100, 50))

    result = keep_unchanged_pixels(
        previous_sketch, previous_image, Image.new("RGB", (256, 256)), image,
    )

    assert result is image
    assert keep_unchanged_pixels(
        previous_sketch, Image.new("RGB", (256, 256)), sketch, image,
    ) is image


def test_selection_alpha_marks_the_inside_of_a_polygon_and_nothing_outside():
    mask = {"polygons": [[
        [0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75],
    ]]}

    alpha = selection_alpha(mask, (200, 200))

    assert alpha.mode == "L"
    assert alpha.size == (200, 200)
    # The inward feather cannot put a value outside the polygon: darker()
    # clamps every pixel it never filled to zero.
    assert alpha.getpixel((5, 5)) == 0
    assert alpha.getpixel((195, 195)) == 0
    assert alpha.getpixel((100, 30)) == 0
    assert alpha.getpixel((100, 100)) == 255


def test_selection_alpha_unions_the_polygons():
    mask = {"polygons": [
        [[0.1, 0.1], [0.4, 0.1], [0.4, 0.4], [0.1, 0.4]],
        [[0.6, 0.6], [0.9, 0.6], [0.9, 0.9], [0.6, 0.9]],
    ]}

    alpha = selection_alpha(mask, (200, 200))

    assert alpha.getpixel((50, 50)) == 255
    assert alpha.getpixel((150, 150)) == 255
    # Between the two polygons nothing is selected.
    assert alpha.getpixel((100, 100)) == 0


def test_selection_alpha_scales_the_normalized_coordinates_with_the_size():
    mask = {"polygons": [[
        [0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75],
    ]]}

    square = selection_alpha(mask, (200, 200))
    wide = selection_alpha(mask, (400, 200))

    assert square.getpixel((100, 100)) == 255
    assert wide.getpixel((200, 100)) == 255
    # The same pixel is inside the selection on the square frame and outside
    # it on the wide one, because x is scaled by the width.
    assert square.getpixel((55, 100)) > 0
    assert wide.getpixel((55, 100)) == 0
