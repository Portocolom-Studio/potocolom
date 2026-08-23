import pytest
from PIL import Image

from worker.region_composite import (
    composite_rgb,
    feather_change_mask,
    max_channel_difference,
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
