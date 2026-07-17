from worker.upscale import iter_tiles, weight_url


def test_weight_url_maps_factor_to_release_asset():
    base = "https://github.com/xinntao/Real-ESRGAN/releases/download"
    assert weight_url(base, 2).endswith("v0.2.1/RealESRGAN_x2plus.pth")
    assert weight_url(base, 4).endswith("v0.1.0/RealESRGAN_x4plus.pth")


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
