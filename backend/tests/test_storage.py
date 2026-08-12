import asyncio
import struct
import zlib
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.files import MAX_UPLOAD_BYTES
from app.main import app
from app.settings import Settings
from app.storage import (
    MAX_IMAGE_EDGE,
    MAX_VERIFY_BYTES,
    LocalStorage,
    S3Storage,
    get_storage,
)

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
        # botocore's StreamingBody takes an amt and returns at most that many,
        # not exactly that many. Hand back one byte at a time so a caller that
        # assumes a single read fills its buffer is caught here.
        if amt is None:
            data, self.data = self.data, b""
            return data
        data, self.data = self.data[:1], self.data[1:]
        return data[:amt]

    def close(self):
        self.closed = True


class _FakePaginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kwargs):
        prefix = kwargs.get("Prefix", "")
        for page in self.pages:
            yield {
                group: [e for e in page.get(group, []) if e["Key"].startswith(prefix)]
                for group in ("Versions", "DeleteMarkers")
            }


class _FakeS3Client:
    def __init__(self, data, pages=None, delete_response=None):
        self.data = data
        self.pages = pages or []
        self.deleted: list[dict] = []
        # DeleteObjects answers 200 with an Errors list on partial failure.
        self.delete_response = delete_response or {}

    def get_object(self, **kwargs):
        return {
            "Body": _FakeBody(self.data),
            "ContentLength": len(self.data),
            "ContentType": "image/png",
        }

    def get_paginator(self, name):
        assert name == "list_object_versions"
        return _FakePaginator(self.pages)

    def delete_objects(self, **kwargs):
        self.deleted.extend(kwargs["Delete"]["Objects"])
        return dict(self.delete_response)


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


def test_image_info_does_not_decompress_and_bounds_dimensions(tmp_path):
    """The validator reads structure, never pixels.

    It decompressed once, to prove the image decoded, and that was twice a
    denial of service: unbounded it turned 400 KB into 831 MB, and bounded by
    the declared dimensions it granted an 80 GB ceiling to a 65 KB object
    claiming 100000x100000. Deciding how much memory to spend from a number the
    peer supplied cannot be made safe, so a bomb is now simply bytes we never
    expand, and the defence is a fixed dimension cap plus the size cap.
    """
    storage = LocalStorage(str(tmp_path), "http://browser", "http://worker")
    bomb = zlib.compress(b"\x00" * (64 * 1024 * 1024), 9)

    # Honest dimensions: accepted, and cost nothing because nothing inflates.
    (tmp_path / "bomb.png").write_bytes(_png(16, 16, bomb))
    info = asyncio.run(storage.image_info("bomb.png"))
    assert (info.width, info.height) == (16, 16)

    # The bypass: a ceiling derived from the header is a ceiling the peer sets.
    (tmp_path / "huge.png").write_bytes(_png(100000, 100000, bomb))
    assert asyncio.run(storage.image_info("huge.png")) is None

    (tmp_path / "empty.png").write_bytes(_png(16, 0, bomb))
    assert asyncio.run(storage.image_info("empty.png")) is None

    rows = b"".join(b"\0" + b"\0" * (16 * 3) for _ in range(16))
    (tmp_path / "real.png").write_bytes(_png(16, 16, zlib.compress(rows)))
    info = asyncio.run(storage.image_info("real.png"))
    assert (info.width, info.height) == (16, 16)


def test_image_info_requires_a_complete_object(tmp_path):
    # Truncation is the likeliest real failure, and the trailer is fixed width
    # so checking it costs nothing.
    storage = LocalStorage(str(tmp_path), "http://browser", "http://worker")
    rows = b"".join(b"\0" + b"\0" * (16 * 3) for _ in range(16))
    complete = _png(16, 16, zlib.compress(rows))
    (tmp_path / "cut.png").write_bytes(complete[:-12])
    assert asyncio.run(storage.image_info("cut.png")) is None
    (tmp_path / "not-png.png").write_bytes(b"GIF89a" + complete[6:])
    assert asyncio.run(storage.image_info("not-png.png")) is None
    assert asyncio.run(storage.image_info("absent.png")) is None


def test_image_info_refuses_an_oversized_object(tmp_path, monkeypatch):
    # The local PUT route caps uploads, but a presigned S3 PUT carries no size
    # condition, so the inspection itself has to refuse to buffer the object.
    storage = LocalStorage(str(tmp_path), "http://browser", "http://worker")
    rows = b"".join(b"\0" + b"\0" * (16 * 3) for _ in range(16))
    (tmp_path / "big.png").write_bytes(_png(16, 16, zlib.compress(rows)))
    monkeypatch.setattr("app.storage.MAX_VERIFY_BYTES", 8)
    assert asyncio.run(storage.image_info("big.png")) is None
    assert MAX_VERIFY_BYTES > 1024 * 1024


def _chunk(kind, body):
    return (struct.pack(">I", len(body)) + kind + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff))


def _header(width, height, depth=8, colour=2, interlace=0):
    return struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0, interlace)


def test_image_info_requires_a_structurally_complete_png(tmp_path):
    """Checking the header and the last twelve bytes is not structure.

    An IHDR followed by arbitrary bytes and a trailer-shaped tail passed that,
    so junk was persisted as a successful archival image/png. The walk checks
    every chunk boundary and CRC without decompressing anything.
    """
    storage = LocalStorage(str(tmp_path), "http://browser", "http://worker")
    sig = b"\x89PNG\r\n\x1a\n"
    idat = zlib.compress(b"".join(b"\0" + b"\0" * (16 * 3) for _ in range(16)))
    good = sig + _chunk(b"IHDR", _header(16, 16)) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")

    bad = {
        "junk-between": sig + _chunk(b"IHDR", _header(16, 16)) + b"\x00" * 40
                        + _chunk(b"IEND", b""),
        "no-idat": sig + _chunk(b"IHDR", _header(16, 16)) + _chunk(b"IEND", b""),
        "repeat-header": sig + _chunk(b"IHDR", _header(16, 16))
                         + _chunk(b"IHDR", _header(16, 16)) + _chunk(b"IDAT", idat)
                         + _chunk(b"IEND", b""),
        "illegal-depth": sig + _chunk(b"IHDR", _header(16, 16, depth=3))
                         + _chunk(b"IDAT", idat) + _chunk(b"IEND", b""),
        "trailing-bytes": good + b"appended",
        # Same length and kind, one byte of the body changed: only the CRC
        # tells you the object is damaged.
        "corrupt-crc": good[:len(sig) + 8 + 13 + 4 + 8] + bytes([good[len(sig) + 8 + 13 + 4 + 8] ^ 0xff])
                       + good[len(sig) + 8 + 13 + 4 + 9:],
        "over-edge": sig + _chunk(b"IHDR", _header(MAX_IMAGE_EDGE + 1, 16))
                     + _chunk(b"IDAT", idat) + _chunk(b"IEND", b""),
    }
    for name, data in bad.items():
        (tmp_path / f"{name}.png").write_bytes(data)
        assert asyncio.run(storage.image_info(f"{name}.png")) is None, name

    for name, data in {"good": good,
                       "at-edge": sig + _chunk(b"IHDR", _header(MAX_IMAGE_EDGE, 16))
                                  + _chunk(b"IDAT", idat) + _chunk(b"IEND", b""),
                       "interlaced": sig + _chunk(b"IHDR", _header(16, 16, interlace=1))
                                     + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")}.items():
        (tmp_path / f"{name}.png").write_bytes(data)
        assert asyncio.run(storage.image_info(f"{name}.png")) is not None, name


def test_max_image_edge_matches_the_largest_real_output():
    # A factor-4 upscale of the largest shipped generation size. Anything more
    # only moves the memory question to whatever decodes the file later.
    assert MAX_IMAGE_EDGE == 4096


def test_s3_delete_removes_every_version_of_exactly_that_key():
    """A delete marker hides the object and keeps billing for the bytes.

    The terminal paths in jobs.py delete to reclaim storage from a peer that
    can upload whatever it likes, so a plain delete_object is not enough on the
    cloud bucket, which has versioning on.
    """
    storage = S3Storage.__new__(S3Storage)
    storage.bucket = "bucket"
    key = "u/j-attempt-1.png"
    storage.client = _FakeS3Client(b"", pages=[
        {"Versions": [{"Key": key, "VersionId": "v1"},
                      {"Key": key, "VersionId": "v2"},
                      # Same prefix, different object: attempt 10, not attempt 1.
                      {"Key": "u/j-attempt-10.png", "VersionId": "other"}],
         "DeleteMarkers": [{"Key": key, "VersionId": "marker"}]},
        {"Versions": [{"Key": key, "VersionId": "v3"}]},
    ])

    asyncio.run(storage.delete(key))
    assert storage.client.deleted == [
        {"Key": key, "VersionId": "v1"},
        {"Key": key, "VersionId": "v2"},
        {"Key": key, "VersionId": "marker"},
        {"Key": key, "VersionId": "v3"},
    ]


def test_s3_delete_is_quiet_on_an_unversioned_bucket():
    # An unversioned bucket reports VersionId "null"; the same path works.
    storage = S3Storage.__new__(S3Storage)
    storage.bucket = "bucket"
    storage.client = _FakeS3Client(b"", pages=[
        {"Versions": [{"Key": "u/j.png", "VersionId": "null"}]},
    ])
    asyncio.run(storage.delete("u/j.png"))
    assert storage.client.deleted == [{"Key": "u/j.png", "VersionId": "null"}]

    empty = S3Storage.__new__(S3Storage)
    empty.bucket = "bucket"
    empty.client = _FakeS3Client(b"", pages=[{}])
    asyncio.run(empty.delete("u/absent.png"))
    assert empty.client.deleted == []


def test_s3_delete_raises_when_a_version_could_not_be_removed():
    """DeleteObjects answers 200 with an Errors list on partial failure.

    Quiet suppresses the successes, not the failures. Discarding the response
    reports a reclaim that did not happen, and the caller then logs nothing.
    """
    storage = S3Storage.__new__(S3Storage)
    storage.bucket = "bucket"
    key = "u/j-attempt-1.png"
    storage.client = _FakeS3Client(
        b"",
        pages=[{"Versions": [{"Key": key, "VersionId": "v1"}]}],
        delete_response={"Errors": [{"Key": key, "VersionId": "v1",
                                     "Code": "AccessDenied",
                                     "Message": "Access Denied"}]},
    )
    with pytest.raises(RuntimeError, match="AccessDenied"):
        asyncio.run(storage.delete(key))
