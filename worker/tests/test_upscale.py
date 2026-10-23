import pytest

from worker.upscale import (
    ensure_weights,
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


def test_concurrent_weight_downloads_use_private_temporary_files(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    gate = Barrier(2)

    class Response:
        def __init__(self, content):
            self.content = content
            self.reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            if self.reads == 0:
                self.reads += 1
                gate.wait(timeout=3)
                return self.content
            return b""

    contents = iter((b"complete writer one", b"complete writer two"))
    monkeypatch.setattr(
        "worker.upscale.urllib.request.urlopen",
        lambda *_args, **_kwargs: Response(next(contents)),
    )

    def download():
        return ensure_weights("https://example.test/weights.pth", str(tmp_path), "model", 2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: download(), range(2)))

    assert results[0] == results[1]
    assert results[0].read_bytes() in {
        b"complete writer one", b"complete writer two",
    }
    assert not list(results[0].parent.glob("*.tmp"))


def test_completed_download_does_not_remove_another_writers_temporary_file(tmp_path, monkeypatch):
    import io

    source = "https://example.test/weights.pth"
    target = weight_cache_path(str(tmp_path), "model", 2, source)
    target.parent.mkdir(parents=True)
    other = target.parent / f"{target.name}.other-writer.tmp"
    other.write_bytes(b"another writer is still downloading")
    monkeypatch.setattr(
        "worker.upscale.urllib.request.urlopen",
        lambda *_args, **_kwargs: io.BytesIO(b"complete weights"),
    )

    assert ensure_weights(source, str(tmp_path), "model", 2).read_bytes() == b"complete weights"
    assert other.read_bytes() == b"another writer is still downloading"


def test_interrupted_weight_download_leaves_no_partial_cache_file(tmp_path, monkeypatch):
    class InterruptedResponse:
        def __init__(self):
            self.reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            self.reads += 1
            if self.reads == 1:
                return b"partial"
            raise OSError("connection interrupted")

    monkeypatch.setattr(
        "worker.upscale.urllib.request.urlopen",
        lambda *_args, **_kwargs: InterruptedResponse(),
    )
    path = weight_cache_path(str(tmp_path), "model", 2, "https://example.test/weights.pth")

    with pytest.raises(OSError, match="connection interrupted"):
        ensure_weights("https://example.test/weights.pth", str(tmp_path), "model", 2)

    assert not path.exists()
    assert not list(path.parent.glob("*.tmp"))


def test_failed_weight_download_preserves_existing_cached_bytes(tmp_path, monkeypatch):
    source = "https://example.test/weights.pth"
    path = weight_cache_path(str(tmp_path), "model", 2, source)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing complete weights")
    monkeypatch.setattr(
        "worker.upscale.urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("download started")),
    )

    assert ensure_weights(source, str(tmp_path), "model", 2) == path
    assert path.read_bytes() == b"existing complete weights"


def test_readers_cannot_observe_partial_final_weight_file(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    partial_written = Event()
    finish_download = Event()

    class Response:
        def __init__(self):
            self.reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            self.reads += 1
            if self.reads == 1:
                return b"complete"
            if self.reads == 2:
                partial_written.set()
                assert finish_download.wait(timeout=3)
            return b""

    monkeypatch.setattr(
        "worker.upscale.urllib.request.urlopen", lambda *_args, **_kwargs: Response(),
    )
    source = "https://example.test/weights.pth"
    path = weight_cache_path(str(tmp_path), "model", 2, source)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ensure_weights, source, str(tmp_path), "model", 2)
        assert partial_written.wait(timeout=3)
        assert not path.exists()
        finish_download.set()
        assert future.result(timeout=3).read_bytes() == b"complete"


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


def test_upscale_tiled_stops_between_tiles_when_cancelled():
    torch = __import__("pytest").importorskip("torch")
    from PIL import Image

    from worker.engine import Cancelled
    from worker.upscale import upscale_tiled

    class CountingNearest2x(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def forward(self, x):
            self.calls += 1
            return torch.nn.functional.interpolate(x, scale_factor=2, mode="nearest")

    model = CountingNearest2x()
    source = Image.new("RGB", (1024, 512), (10, 20, 30))
    with pytest.raises(Cancelled):
        upscale_tiled(
            model, source, 2, device="cpu", dtype=torch.float32,
            tile=512, overlap=32, cancelled=lambda: model.calls >= 1,
        )
    # One tile ran and the rest did not: a tile holds the GPU until it returns.
    assert model.calls == 1


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
