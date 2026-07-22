import pytest

from worker.upscale import (
    is_direct_weight_source,
    iter_tiles,
    weight_cache_path,
    weight_url,
)


def test_weight_url_maps_factor_to_release_asset():
    base = "https://github.com/xinntao/Real-ESRGAN/releases/download"
    assert weight_url(base, 2).endswith("v0.2.1/RealESRGAN_x2plus.pth")
    assert weight_url(base, 4).endswith("v0.1.0/RealESRGAN_x4plus.pth")


def test_direct_weight_source_uses_the_pth_url_for_every_factor():
    url = (
        "https://github.com/xinntao/Real-ESRGAN/releases/download/"
        "v0.2.5.0/realesr-general-x4v3.pth"
    )
    assert is_direct_weight_source(url)
    assert weight_url(url, 2) == url
    assert weight_url(url, 4) == url
    path = weight_cache_path("/models", "realesrgan-fast", 2, url)
    assert path.name == "realesr-general-x4v3.pth"
    assert weight_cache_path("/models", "realesrgan-fast", 4, url) == path


def test_weight_helpers_reject_unknown_factor():
    with pytest.raises(ValueError, match="unsupported upscale factor"):
        weight_url("https://example.com", 3)
    with pytest.raises(ValueError, match="unsupported upscale factor"):
        weight_cache_path("/models", "realesrgan", 3)


def test_iter_tiles_covers_small_image_as_single_tile():
    assert iter_tiles(64, 48, tile=512, overlap=32) == [(0, 0, 64, 48)]


def test_iter_tiles_covers_large_image_without_gaps():
    width, height = 1024, 768
    tiles = iter_tiles(width, height, tile=512, overlap=32)
    covered = [[False] * width for _ in range(height)]
    for x0, y0, x1, y1 in tiles:
        assert 0 <= x0 < x1 <= width
        assert 0 <= y0 < y1 <= height
        for y in range(y0, y1):
            for x in range(x0, x1):
                covered[y][x] = True
    assert all(all(row) for row in covered)
    assert len(tiles) >= 4


def test_upscale_tiled_reconstructs_borders_without_dark_frame():
    torch = __import__("pytest").importorskip("torch")
    from PIL import Image

    from worker.upscale import upscale_tiled

    class Nearest2x(torch.nn.Module):
        def forward(self, x):
            return torch.nn.functional.interpolate(x, scale_factor=2, mode="nearest")

    white = Image.new("RGB", (700, 520), (255, 255, 255))
    result = upscale_tiled(
        Nearest2x(), white, 2, device="cpu", dtype=torch.float32,
        tile=512, overlap=32,
    )
    assert result.size == (1400, 1040)
    extrema = result.convert("L").getextrema()
    assert extrema == (255, 255), f"border pixels darkened: {extrema}"


def test_upscale_tiled_downsamples_when_factor_below_native_scale():
    torch = __import__("pytest").importorskip("torch")
    from PIL import Image

    from worker.upscale import upscale_tiled

    class Nearest4x(torch.nn.Module):
        def forward(self, x):
            return torch.nn.functional.interpolate(x, scale_factor=4, mode="nearest")

    source = Image.new("RGB", (32, 24), (40, 80, 120))
    result = upscale_tiled(
        Nearest4x(), source, 2, device="cpu", dtype=torch.float32,
        native_scale=4, tile=512, overlap=32,
    )
    assert result.size == (64, 48)


def test_upscale_tiled_rejects_factor_above_native_scale():
    torch = __import__("pytest").importorskip("torch")
    from PIL import Image

    from worker.upscale import upscale_tiled

    class Nearest2x(torch.nn.Module):
        def forward(self, x):
            return torch.nn.functional.interpolate(x, scale_factor=2, mode="nearest")

    with pytest.raises(ValueError, match="exceeds network native scale"):
        upscale_tiled(
            Nearest2x(), Image.new("RGB", (16, 16), (0, 0, 0)), 4,
            device="cpu", dtype=torch.float32, native_scale=2,
        )
