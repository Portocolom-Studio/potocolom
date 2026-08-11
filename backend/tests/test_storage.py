import asyncio
import struct
import zlib
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.files import MAX_UPLOAD_BYTES
from app.main import app
from app.settings import Settings
from app.storage import MAX_VERIFY_BYTES, LocalStorage, S3Storage, get_storage

client = TestClient(app)


def png_bytes(width=3, height=2):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\0" + b"\0" * (width * 3) for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def test_local_storage_rejects_escaping_keys(tmp_path):
    storage = LocalStorage(str(tmp_path), "http://browser", "http://worker")
    with pytest.raises(ValueError):
        storage.path("../escape.webp")


def test_local_storage_urls(tmp_path):
    storage = LocalStorage(str(tmp_path), "http://browser/", "http://worker/")
    target = asyncio.run(storage.upload_target("u/j.png"))
    assert target.url == "http://worker/api/v1/files/u/j.png"
    assert target.headers == {"Content-Type": "image/png"}
    thumb_target = asyncio.run(storage.upload_target("u/j-thumb.webp"))
    assert thumb_target.headers == {"Content-Type": "image/webp"}
    assert asyncio.run(storage.url("u/j.webp")) == "http://browser/api/v1/files/u/j.webp"
    download_url = asyncio.run(
        storage.url("u/j.png", download_name="potocolom-20260729-142530-castle.png")
    )
    assert parse_qs(urlsplit(download_url).query) == {
        "download": ["potocolom-20260729-142530-castle.png"],
    }


def test_local_storage_reads_uploaded_image_info(tmp_path):
    storage = LocalStorage(str(tmp_path), "http://browser", "http://worker")
    path = storage.path("u/j.png")
    path.parent.mkdir(parents=True)
    data = png_bytes()
    path.write_bytes(data)

    info = asyncio.run(storage.image_info("u/j.png"))
    assert info is not None
    assert (info.width, info.height, info.size, info.content_type) == (
        3, 2, len(data), "image/png",
    )
    assert asyncio.run(storage.image_info("u/missing.png")) is None
    path.write_bytes(b"not an image")
    assert asyncio.run(storage.image_info("u/j.png")) is None
    oversized = bytearray(data)
    struct.pack_into(">I", oversized, 16, 2**31)
    struct.pack_into(">I", oversized, 29, zlib.crc32(oversized[12:29]) & 0xffffffff)
    path.write_bytes(oversized)
    assert asyncio.run(storage.image_info("u/j.png")) is None


class _FakeBody:
    def __init__(self, data):
        self.data = data
        self.closed = False

    def read(self, amt=None):
        # botocore's StreamingBody takes an amt; image_info passes one to bound
        # what a worker can make the API buffer.
        return self.data if amt is None else self.data[:amt]

    def close(self):
        self.closed = True


class _FakeS3Client:
    def __init__(self, data):
        self.data = data

    def get_object(self, **kwargs):
        return {
            "Body": _FakeBody(self.data),
            "ContentLength": len(self.data),
            "ContentType": "image/png",
        }


def test_s3_storage_reads_uploaded_image_info():
    storage = S3Storage.__new__(S3Storage)
    storage.bucket = "bucket"
    data = png_bytes(5, 7)
    storage.client = _FakeS3Client(data)

    info = asyncio.run(storage.image_info("u/j.png"))
    assert info is not None
    assert (info.width, info.height, info.size, info.content_type) == (
        5, 7, len(data), "image/png",
    )


def test_s3_storage_presigns_offline():
    storage = S3Storage(Settings(storage_backend="s3",
                                 storage_s3_endpoint="http://localhost:9100",
                                 storage_s3_access_key="key",
                                 storage_s3_secret_key="secret"))
    target = asyncio.run(storage.upload_target("u/j.png"))
    assert target.url.startswith("http://localhost:9100/")
    assert "u/j.png" in target.url
    assert "X-Amz-Signature" in target.url
    assert target.headers == {"Content-Type": "image/png"}
    thumb_target = asyncio.run(storage.upload_target("u/j-thumb.webp"))
    assert thumb_target.headers == {"Content-Type": "image/webp"}
    # Browser-facing image URL (SPA <img src>), not the worker upload target.
    view = asyncio.run(storage.url("u/j.png"))
    assert view.startswith("http://localhost:9100/")
    assert "u/j.png" in view
    assert "X-Amz-Signature" in view
    download = asyncio.run(
        storage.url("u/j.png", download_name="potocolom-20260729-142530-castle.png")
    )
    assert parse_qs(urlsplit(download).query)["response-content-disposition"] == [
        'attachment; filename="potocolom-20260729-142530-castle.png"',
    ]


def test_storage_rejects_unsafe_download_name(tmp_path):
    storage = LocalStorage(str(tmp_path), "http://browser", "http://worker")
    with pytest.raises(ValueError, match="unsafe download filename"):
        asyncio.run(storage.url("u/j.png", download_name='safe.png"\r\nX-Evil: injected'))


def test_files_get_after_direct_write():
    storage = get_storage()
    assert isinstance(storage, LocalStorage)
    path = storage.path("u1/j1.webp")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"image-bytes")

    response = client.get("/api/v1/files/u1/j1.webp")
    assert response.status_code == 200
    assert response.content == b"image-bytes"
    assert response.headers["content-type"] == "image/webp"
    assert "content-disposition" not in response.headers
    unsafe_response = client.get(
        "/api/v1/files/u1/j1.webp",
        params={"download": 'safe.webp"\r\nX-Evil: injected'},
    )
    assert unsafe_response.status_code == 400
    png_path = storage.path("u1/j2.png")
    png_path.write_bytes(b"png-bytes")
    png_response = client.get("/api/v1/files/u1/j2.png")
    assert png_response.status_code == 200
    assert png_response.content == b"png-bytes"
    assert png_response.headers["content-type"] == "image/png"
    assert client.get("/api/v1/files/u1/missing.webp").status_code == 404


def test_upload_requires_inflight_job():
    assert client.put("/api/v1/files/u1/j1.webp", content=b"image-bytes").status_code == 403


def test_upload_ceiling_admits_a_lossless_upscale_master():
    """The largest master the fleet can produce is a 4x upscale of a 1024 px
    image, and PNG is lossless, so the ceiling has to clear that size even when
    the detail does not compress. Measured: a real 1024 px generation reaches
    about 19 MB re-encoded at 4096 px, and incompressible content there is
    50 MB. The 20 MB ceiling this route shipped with would have started
    refusing upscale uploads once masters stopped being WebP (issue #125).
    """
    pixels = (1024 * 4) ** 2
    assert MAX_UPLOAD_BYTES >= pixels * 3  # raw RGB, which PNG does not exceed in practice


def _png(width, height, idat):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def test_image_info_refuses_a_decompression_bomb(tmp_path):
    """Validating the output must not be a way to exhaust the API.

    The IDAT of a structurally valid PNG can be a zip bomb: 400 KB on the wire
    expanded to 831 MB in process before this was bounded, and the parser
    reported it as a well-formed 16x16 image. The ceiling comes from the
    declared dimensions, so a genuine image of that size still passes.
    """
    storage = LocalStorage(str(tmp_path), "http://browser", "http://worker")
    bomb = zlib.compress(b"\x00" * (64 * 1024 * 1024), 9)
    (tmp_path / "bomb.png").write_bytes(_png(16, 16, bomb))
    assert len(_png(16, 16, bomb)) < 100_000
    assert asyncio.run(storage.image_info("bomb.png")) is None

    rows = b"".join(b"\0" + b"\0" * (16 * 3) for _ in range(16))
    (tmp_path / "real.png").write_bytes(_png(16, 16, zlib.compress(rows)))
    info = asyncio.run(storage.image_info("real.png"))
    assert (info.width, info.height) == (16, 16)


def test_image_info_refuses_an_oversized_object(tmp_path, monkeypatch):
    # The local PUT route caps uploads, but a presigned S3 PUT carries no size
    # condition, so the inspection itself has to refuse to buffer the object.
    storage = LocalStorage(str(tmp_path), "http://browser", "http://worker")
    rows = b"".join(b"\0" + b"\0" * (16 * 3) for _ in range(16))
    (tmp_path / "big.png").write_bytes(_png(16, 16, zlib.compress(rows)))
    monkeypatch.setattr("app.storage.MAX_VERIFY_BYTES", 8)
    assert asyncio.run(storage.image_info("big.png")) is None
    assert MAX_VERIFY_BYTES > 1024 * 1024
