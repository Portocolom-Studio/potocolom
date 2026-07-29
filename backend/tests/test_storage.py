import asyncio

import pytest
from fastapi.testclient import TestClient

from app.files import MAX_UPLOAD_BYTES
from app.main import app
from app.settings import Settings
from app.storage import LocalStorage, S3Storage, get_storage

client = TestClient(app)


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
