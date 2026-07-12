import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.settings import Settings
from app.storage import LocalStorage, S3Storage

client = TestClient(app)


def test_local_storage_rejects_escaping_keys(tmp_path):
    storage = LocalStorage(str(tmp_path), "http://api")
    with pytest.raises(ValueError):
        storage.path("../escape.webp")


def test_local_storage_urls(tmp_path):
    storage = LocalStorage(str(tmp_path), "http://api/")
    target = asyncio.run(storage.upload_target("u/j.webp"))
    assert target.url == "http://api/api/v1/files/u/j.webp"
    assert asyncio.run(storage.url("u/j.webp")) == "http://api/api/v1/files/u/j.webp"


def test_s3_storage_presigns_offline():
    storage = S3Storage(Settings(storage_backend="s3",
                                 storage_s3_endpoint="http://localhost:9100",
                                 storage_s3_access_key="key",
                                 storage_s3_secret_key="secret"))
    target = asyncio.run(storage.upload_target("u/j.webp"))
    assert "u/j.webp" in target.url
    assert "X-Amz-Signature" in target.url


def test_files_roundtrip_through_the_api():
    # STORAGE_LOCAL_PATH points at a temporary directory (conftest.py).
    assert client.put("/api/v1/files/u1/j1.webp", content=b"image-bytes").status_code == 200
    response = client.get("/api/v1/files/u1/j1.webp")
    assert response.status_code == 200
    assert response.content == b"image-bytes"
    assert client.get("/api/v1/files/u1/missing.webp").status_code == 404
