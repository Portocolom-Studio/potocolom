import asyncio
import base64
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
    MAX_PNG_CHUNKS,
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
    # The dispatch token rides in the headers the worker echoes (issue #247).
    tokened = asyncio.run(storage.upload_target("u/j.png", "tok"))
    assert tokened.headers == {"Content-Type": "image/png", "X-Upload-Token": "tok"}
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
    def __init__(self, data, pages=None, delete_response=None, delete_responses=None):
        self.data = data
        self.pages = pages or []
        self.deleted: list[dict] = []
        self.delete_calls = 0
        # DeleteObjects answers 200 with an Errors list on partial failure.
        # delete_responses gives one answer per call, so a batch can fail while
        # the batches after it succeed.
        self.delete_response = delete_response or {}
        self.delete_responses = delete_responses

    def get_object(self, **kwargs):
        return {
            "Body": _FakeBody(self.data),
            "ContentLength": len(self.data),
            # Deliberately not the type of the bytes: an uploader declares its
            # own Content-Type on a presigned PUT, so image_info must report
            # what the bytes are, and a fake that agreed with them could not
            # tell the difference.
            "ContentType": "text/plain",
        }

    def get_paginator(self, name):
        assert name == "list_object_versions"
        return _FakePaginator(self.pages)

    def delete_objects(self, **kwargs):
        self.deleted.extend(kwargs["Delete"]["Objects"])
        self.delete_calls += 1
        if self.delete_responses is not None:
            index = min(self.delete_calls - 1, len(self.delete_responses) - 1)
            return dict(self.delete_responses[index])
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
    # If-None-Match is signed as well as sent: the bucket refuses a second
    # write with 412, so a presigned PUT cannot be replayed over an object the
    # API has already verified (issue #249).
    assert target.headers == {"Content-Type": "image/png", "If-None-Match": "*"}
    signed = parse_qs(urlsplit(target.url).query)["X-Amz-SignedHeaders"][0]
    assert "if-none-match" in signed
    thumb_target = asyncio.run(storage.upload_target("u/j-thumb.webp"))
    assert thumb_target.headers == {"Content-Type": "image/webp", "If-None-Match": "*"}
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


def test_s3_image_info_refuses_an_oversized_object(monkeypatch):
    """The local PUT route caps upload size, but a presigned S3 PUT constrains
    bucket, key and content type, never size, so this check is the only bound
    on how many peer-chosen bytes the API process buffers to verify a
    completion. The read goes one byte past the bound because read(amt) may
    return less than amt, and an object exactly at the bound must be admitted
    while one a byte larger must not. The constant is 64 MiB, so the test
    patches it down to the size of an object it builds rather than allocating
    the real bound.
    """
    storage = S3Storage.__new__(S3Storage)
    storage.bucket = "bucket"
    data = png_bytes(5, 7)
    head, iend = data[:-12], data[-12:]
    at_limit = head + _chunk(b"prVt", b"pad") + iend
    one_past = head + _chunk(b"prVt", b"padd") + iend
    monkeypatch.setattr("app.storage.MAX_VERIFY_BYTES", len(at_limit))

    storage.client = _FakeS3Client(at_limit)
    info = asyncio.run(storage.image_info("u/j.png"))
    assert info is not None
    assert info.size == len(at_limit)

    storage.client = _FakeS3Client(one_past)
    assert asyncio.run(storage.image_info("u/j.png")) is None


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


def test_image_info_refuses_a_png_past_the_chunk_cap(tmp_path):
    """A stored object is peer-supplied and the walk's cost is per chunk.

    Bounded only by MAX_VERIFY_BYTES, one object could force millions of chunk
    iterations (a 64 MiB object of empty chunks is about five million), each
    seeding a CRC. MAX_PNG_CHUNKS is far beyond any real encoder, so a file
    past the cap is junk, not an output worth inspecting, and a file exactly at
    it must still be accepted: the guard is a ceiling on abuse, and an
    off-by-one in either direction is a real bug.
    """
    storage = LocalStorage(str(tmp_path), "http://browser", "http://worker")
    sig = b"\x89PNG\r\n\x1a\n"
    idat = zlib.compress(b"".join(b"\0" + b"\0" * (16 * 3) for _ in range(16)))
    filler = _chunk(b"prVt", b"")
    at_cap = (sig + _chunk(b"IHDR", _header(16, 16)) + _chunk(b"IDAT", idat)
              + filler * (MAX_PNG_CHUNKS - 3) + _chunk(b"IEND", b""))
    over_cap = (sig + _chunk(b"IHDR", _header(16, 16)) + _chunk(b"IDAT", idat)
                + filler * (MAX_PNG_CHUNKS - 2) + _chunk(b"IEND", b""))

    (tmp_path / "at-cap.png").write_bytes(at_cap)
    assert asyncio.run(storage.image_info("at-cap.png")) is not None
    (tmp_path / "over-cap.png").write_bytes(over_cap)
    assert asyncio.run(storage.image_info("over-cap.png")) is None


def test_max_image_edge_matches_the_largest_real_output():
    # A factor-4 upscale of the largest shipped generation size. Anything more
    # only moves the memory question to whatever decodes the file later.
    assert MAX_IMAGE_EDGE == 4096


# The real lossless 320x200 WebP from test_jobs, embedded here too so the
# rejections below are mutations of real encoded bytes rather than headers
# built to please the reader.
_WEBP_BYTES = base64.b64decode(
    "UklGRiQAAABXRUJQVlA4TBcAAAAvP8ExAAcQ9Y/+BwAU6f9/iuh/6v+fAQA="
)

# A real extended 320x200 WebP with an alpha channel, ffmpeg output
# (VP8X + ALPH + VP8): the only shape a real encoder here gives a VP8X
# canvas, and the frame inside carries its own 320x200 header.
_WEBP_EXTENDED_BYTES = base64.b64decode(
    "UklGRpIEAABXRUJQVlA4WAoAAAAQAAAAPwEAxwAAQUxQSAACAAABGYBIan/0ASL6nxI4bNtGkuKyrv/e9pkdxxkbB0RMACQ1kiRJ1Kk3I81ggpx4wm95/8AzUgEeUoHeRL94PpPiMOQQpXo0BV8royn4BqKEwRVjJhR8qQX4DjPG4M7CLDqxhzBFOIa/JXgaI8bw8VHFBNeNgcPErHXSFHwD0b2D2+aa9XZsBhTzNrjBMP/BG1uI6MZ2rZqm4BuI7h3cPdesx2Mzp5jHwX2HaT84oaH7Nrbwh7WxFWfIlKEXQ2C+X5romnWfIa6Ye3/fYdoPzmuY243tncXhYJQZwn/wxhYuMqQWc+HvO0z7wekN07uxhQbG5pnuetaKiglOKNpGBjh4THZ3PXCRIb0Y8B2m/eBdGNpoYzt9TDkOTN+Gn4iqCM4ryp8BXICBsWG8tetZKgrDwxTfYS4y+A7zH7yxLe0H5xXlzwA2wKgITijqhv8S+ZPC1DDFd5j7M0YSs0jNPRvbuWPu/ZkwZtj1lPaD84ryZ4D0YBSPzdncLxtIqiI4n2jxHebxUXOKuT9jXDe2zH/Tx77rUTWgeUX5M4AbMO0HJxRVPDbbuV96CFN8h3l74hBiPlFL78a2UAQ/9l2PqgHNK8rfHdKDUTw2J32//FJ0XmPTfZiSW8zbE6d3Y1v2Dh6bXc9aGYYMEBvMPq/Hh4HZYMYY3FmYRQfPJ6Yox1A8BVZQOCBsAgAA8BYAnQEqQAHIAD6RSKFNJaQjIiAoALASCWdu4THVbG65TW/0CmA8fz/90/z/d35AFZu14uTkPfgCAe+2hFNEA5zIPCDXlNrYjorBpuVeTCYZdNVfzP2gTR/F11u/qUBYzLMiUoT3sD7ZO5F33uCuzFarQgjeeo34gOu1iJJV+fIo8SoYESwV2XnL2IQ4iiCDTiVAvI8+AVXCQpeGL9Vq68iq7ZpMvAHw6L9+Svfsh2CK+VbSogaif8nKYP81AZAAAP7b7X6ip5li//roH/66B/+ugf7aLHUQK5GBaGeY1jBB8/zlsKHAJMgi8V6jhupi6e0N1L6cHQx+DRDn4QgWC8Ji3ywdEprD7LhLfGS8q2QVh+YBI56Sfaad3u9ahXlAu384pO9QE2mDo5MJ1URn1RX1tnmWAOBoNglC7TvjeuHJZBp9LQZx0HHrfDchWI9H0s4h1gRLfPCi9WrwQrX3nsQQufyGx4wq0hkBcuqAsz+I8C83DjcfrLW7f9oVqismwoF8ebOkqNIEtYjogSfShoC7J5shYJlXSZaHyelTBWx8C+5GoOHFgkl8RV8vS1SUhxDsBazFUnMz+40CICNzCQFSsqLlwrsC9EtuLtB39+ykQ0OvHR2aoaGXw6QbAeHWn7FkIJwEl/UEcoEoPFBMlIRre9C59fnf7fmizmw0Vv2w9hVl3O2fRXT/oyIS88Msq3Jt0whWGwjnNQZeduLbcc6ECTlEw3D0qBIc+q5A+70nxoOhb3s3w1MhxLGU7N2rFwwaCizjFAAETx90qVLM0O4Rdiva6cjjCWJX+RbzZULLEU89ikXh2kTAAAA="
)


def _webp_chunk(kind, payload):
    padded = payload + (b"\x00" if len(payload) & 1 else b"")
    return kind + struct.pack("<I", len(payload)) + padded


def _webp_container(*chunks):
    body = b"".join(chunks)
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def _webp_payload(blob, kind):
    """Payload of the first chunk of this kind in a real WebP blob."""
    position = 12
    while position + 8 <= len(blob):
        if blob[position:position + 4] == kind:
            length = struct.unpack_from("<I", blob, position + 4)[0]
            return bytes(blob[position + 8:position + 8 + length])
        position += 8 + struct.unpack_from("<I", blob, position + 4)[0]
    raise AssertionError(f"{kind!r} missing from the fixture")


def test_image_info_requires_a_complete_webp(tmp_path):
    """The WebP reader is a bounded chunk walk like the PNG one.

    Reading the RIFF header and the first chunk header let a fabricated VP8X
    prefix pass as a thumbnail no matter what followed it, so the walk must
    demand an image chunk after VP8X, keep every chunk inside the RIFF payload
    and reject a lossless frame that claims an unknown version.
    """
    storage = LocalStorage(str(tmp_path), "http://browser", "http://worker")

    vp8x = _webp_chunk(b"VP8X", b"\x00" * 4 + (319).to_bytes(3, "little")
                       + (199).to_bytes(3, "little"))
    (tmp_path / "vp8x-only.webp").write_bytes(_webp_container(vp8x))
    assert asyncio.run(storage.image_info("vp8x-only.webp")) is None

    # The real file's VP8L chunk declares 23 bytes; a declared 100 runs past
    # the RIFF payload even though all 44 bytes are present.
    overlong = bytearray(_WEBP_BYTES)
    struct.pack_into("<I", overlong, 16, 100)
    (tmp_path / "overlong.webp").write_bytes(overlong)
    assert asyncio.run(storage.image_info("overlong.webp")) is None

    # The top three bits of the fourth byte after the 0x2f signature are the
    # version; the encoder emitted zero there and a nonzero one is a frame
    # this walk does not understand.
    bad_version = bytearray(_WEBP_BYTES)
    bad_version[24] |= 0x80
    (tmp_path / "bad-version.webp").write_bytes(bad_version)
    assert asyncio.run(storage.image_info("bad-version.webp")) is None


def test_image_info_refuses_an_animated_webp(tmp_path):
    """An ANMF chunk is refused, not treated as image data.

    What the fleet uploads is a still thumbnail; accepting an animation would
    mean validating the whole nested frame structure for no use case, and an
    empty frame must not count as the one image a container needs.
    """
    storage = LocalStorage(str(tmp_path), "http://browser", "http://worker")
    vp8x = _webp_chunk(b"VP8X", b"\x00" * 4 + (319).to_bytes(3, "little")
                       + (199).to_bytes(3, "little"))
    (tmp_path / "animated.webp").write_bytes(
        _webp_container(vp8x, _webp_chunk(b"ANMF", b""))
    )
    assert asyncio.run(storage.image_info("animated.webp")) is None


def test_image_info_requires_the_frame_to_match_the_canvas(tmp_path):
    """The VP8X canvas is a claim, not the object.

    The frame carries its own dimensions, and a canvas edited down to 1x1
    over a real 320x200 frame would otherwise be recorded as 1x1 and dodge
    the thumbnail edge check that trusts these numbers.
    """
    storage = LocalStorage(str(tmp_path), "http://browser", "http://worker")
    lying_canvas = bytearray(_WEBP_EXTENDED_BYTES)
    # The canvas is the four flags plus width-1 and height-1, three
    # little-endian bytes each, in the VP8X payload.
    lying_canvas[24:27] = b"\x00\x00\x00"
    lying_canvas[27:30] = b"\x00\x00\x00"
    (tmp_path / "lying-canvas.webp").write_bytes(lying_canvas)
    assert asyncio.run(storage.image_info("lying-canvas.webp")) is None


def test_image_info_refuses_a_second_bitstream(tmp_path):
    """A static WebP carries exactly one frame chunk.

    A second bitstream in an extended container is not a thumbnail the fleet
    can produce, so it is a fabricated file rather than one to keep.
    """
    storage = LocalStorage(str(tmp_path), "http://browser", "http://worker")
    frame = _webp_chunk(b"VP8 ", _webp_payload(_WEBP_EXTENDED_BYTES, b"VP8 "))
    vp8x = _webp_chunk(b"VP8X", _webp_payload(_WEBP_EXTENDED_BYTES, b"VP8X"))
    (tmp_path / "two-frames.webp").write_bytes(
        _webp_container(vp8x, frame, frame)
    )
    assert asyncio.run(storage.image_info("two-frames.webp")) is None


def test_image_info_accepts_a_real_webp_with_xmp(tmp_path):
    """The XMP whitelist entry is the four-byte kind: the trailing space is
    part of the chunk code, so a real XMP chunk must not fall into the
    unknown-chunk branch."""
    storage = LocalStorage(str(tmp_path), "http://browser", "http://worker")
    frame = _webp_chunk(b"VP8 ", _webp_payload(_WEBP_EXTENDED_BYTES, b"VP8 "))
    vp8x = _webp_chunk(b"VP8X", _webp_payload(_WEBP_EXTENDED_BYTES, b"VP8X"))
    xmp = _webp_chunk(b"XMP ", b'<x:xmpmeta xmlns:x="adobe:ns:meta/">')
    (tmp_path / "with-xmp.webp").write_bytes(
        _webp_container(vp8x, frame, xmp)
    )
    info = asyncio.run(storage.image_info("with-xmp.webp"))
    assert (info.width, info.height) == (320, 200)


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


def test_s3_delete_finishes_every_batch_before_reporting_failures():
    """A key past a thousand versions takes several DeleteObjects calls.

    Raising on the first batch that reports an error abandoned every batch and
    every page after it, and the caller only logs a warning, so the versions
    stayed and nothing came back to them (issue #256).
    """
    storage = S3Storage.__new__(S3Storage)
    storage.bucket = "bucket"
    key = "u/j-attempt-1.png"
    first_page = [{"Key": key, "VersionId": f"v{n}"} for n in range(1500)]
    second_page = [{"Key": key, "VersionId": "last"}]
    storage.client = _FakeS3Client(
        b"",
        pages=[{"Versions": first_page}, {"Versions": second_page}],
        # Batch one fails two objects, the rest succeed: the failure must not
        # cost the 501 versions behind it.
        delete_responses=[
            {"Errors": [{"Key": key, "VersionId": "v1", "Code": "InternalError"},
                        {"Key": key, "VersionId": "v2", "Code": "InternalError"}]},
            {},
            {},
        ],
    )

    with pytest.raises(RuntimeError, match="could not delete 2 version"):
        asyncio.run(storage.delete(key))
    # Two batches for the first page, one for the second.
    assert storage.client.delete_calls == 3
    assert len(storage.client.deleted) == 1501
    assert storage.client.deleted[-1] == {"Key": key, "VersionId": "last"}


def test_s3_delete_counts_failures_from_every_batch():
    """The message the caller logs must name the whole shortfall, not the
    first batch's share of it."""
    storage = S3Storage.__new__(S3Storage)
    storage.bucket = "bucket"
    key = "u/j-attempt-1.png"
    storage.client = _FakeS3Client(
        b"",
        pages=[{"Versions": [{"Key": key, "VersionId": f"v{n}"} for n in range(1200)]}],
        delete_responses=[
            {"Errors": [{"Key": key, "VersionId": "v1", "Code": "InternalError"}]},
            {"Errors": [{"Key": key, "VersionId": "v1001", "Code": "AccessDenied"},
                        {"Key": key, "VersionId": "v1002", "Code": "AccessDenied"}]},
        ],
    )

    # Three failures across two batches, reported with the first code seen.
    with pytest.raises(RuntimeError, match="could not delete 3 version\\(s\\).*InternalError"):
        asyncio.run(storage.delete(key))
