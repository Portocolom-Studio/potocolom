import asyncio

import pytest
from fastapi.testclient import TestClient

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
    target = asyncio.run(storage.upload_target("u/j.webp"))
    assert target.url == "http://worker/api/v1/files/u/j.webp"
    assert asyncio.run(storage.url("u/j.webp")) == "http://browser/api/v1/files/u/j.webp"


def test_s3_storage_presigns_offline():
    storage = S3Storage(Settings(storage_backend="s3",
                                 storage_s3_endpoint="http://localhost:9100",
                                 storage_s3_access_key="key",
                                 storage_s3_secret_key="secret"))
    target = asyncio.run(storage.upload_target("u/j.webp"))
    assert target.url.startswith("http://localhost:9100/")
    assert "u/j.webp" in target.url
    assert "X-Amz-Signature" in target.url
    assert target.headers == {"Content-Type": "image/webp"}
    # Browser-facing image URL (SPA <img src>), not the worker upload target.
    view = asyncio.run(storage.url("u/j.webp"))
    assert view.startswith("http://localhost:9100/")
    assert "u/j.webp" in view
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
    assert client.get("/api/v1/files/u1/missing.webp").status_code == 404


def test_upload_requires_inflight_job():
    assert client.put("/api/v1/files/u1/j1.webp", content=b"image-bytes").status_code == 403
