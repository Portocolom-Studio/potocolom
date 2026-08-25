import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import db, jobs
from app.auth import current_user
from app.benchmark import router as benchmark_router
from app.main import app
from app.settings import get_settings
from app.storage import LocalStorage, S3Storage
from app.tables import Asset, User


def _client(user=None):
    if user is not None:
        app.dependency_overrides[current_user] = lambda: user
    return TestClient(app)


@pytest.fixture(autouse=True)
def clear_dependency_overrides():
    yield
    app.dependency_overrides.clear()


def _benchmark_client(user):
    test_app = FastAPI()
    test_app.include_router(benchmark_router)
    test_app.dependency_overrides[current_user] = lambda: user
    return TestClient(test_app)


def _principal(role: str, suffix: str) -> User:
    return User(id=uuid.uuid5(uuid.NAMESPACE_URL, f"endpoint-hardening:{suffix}"),
                email=f"{suffix}@example.test", role=role)


async def _persist_user(role: str, suffix: str) -> User:
    assert db.session_factory is not None
    row = _principal(role, suffix)
    async with db.session_factory() as session:
        session.add(row)
        await session.commit()
    return row


@pytest.mark.db
def test_models_resolve_principal_and_none_mode_keeps_implicit_admin(monkeypatch):
    with _client() as client:
        assert client.get("/api/v1/models").status_code == 200
        async def missing_principal():
            raise HTTPException(status_code=401, detail="authentication required")
        app.dependency_overrides[current_user] = missing_principal
        assert client.get("/api/v1/models").status_code == 401
        app.dependency_overrides.pop(current_user, None)
        get_settings.cache_clear()
        monkeypatch.setenv("AUTH_MODE", "none")
        assert client.get("/api/v1/models").status_code == 200


@pytest.mark.db
@pytest.mark.parametrize("role", ("viewer", "user"))
def test_install_operations_are_not_available_to_non_admin(monkeypatch, role):
    routes = [
        ("/api/v1/studio/gpu", {}),
        ("/api/v1/metrics/gpu/history?from=2026-01-01T00:00:00Z&to=2026-01-01T01:00:00Z", {}),
        ("/api/v1/telemetry/preview", {}),
    ]
    principal = _principal(role, f"endpoint-{role}")
    with _client(principal) as client:
        for route, body in routes:
            response = client.get(route)
            assert response.status_code == 403, (role, route, response.status_code)


@pytest.mark.parametrize("role", ("viewer", "user"))
def test_benchmark_routes_are_not_available_to_non_admin(monkeypatch, role):
    monkeypatch.setenv("BENCHMARK_API", "1")
    get_settings.cache_clear()
    principal = _principal(role, f"benchmark-{role}")
    routes = [
        ("/api/v1/benchmark/models", "get", None),
        ("/api/v1/benchmark/gpu", "get", None),
        ("/api/v1/benchmark/gpu/load", "post", {"model_id": "sd-test"}),
        ("/api/v1/benchmark/gpu/unload", "post", {}),
    ]
    with _benchmark_client(principal) as client:
        for route, method, body in routes:
            response = getattr(client, method)(route, json=body) if body is not None else getattr(client, method)(route)
            assert response.status_code == 403, (role, route, response.status_code)


@pytest.mark.db
@pytest.mark.parametrize("role", ("viewer", "user"))
def test_benchmark_session_routes_are_not_available_to_non_admin(monkeypatch, role):
    monkeypatch.setenv("BENCHMARK_API", "1")
    get_settings.cache_clear()
    principal = _principal(role, f"benchmark-session-{role}")
    with _client(principal) as client:
        assert client.get("/api/v1/benchmark/sessions").status_code == 403
        assert client.get(
            "/api/v1/benchmark/sessions/00000000-0000-0000-0000-000000000000"
        ).status_code == 403


@pytest.mark.db
def test_implicit_admin_can_use_install_operations(monkeypatch):
    monkeypatch.setenv("BENCHMARK_API", "1")
    get_settings.cache_clear()
    try:
        with _client() as client:
            assert client.get("/api/v1/metrics/gpu/history?from=2026-01-01T00:00:00Z&to=2026-01-01T01:00:00Z").status_code == 200
        with _benchmark_client(User(role="admin")) as client:
            assert client.get("/api/v1/benchmark/models").status_code == 200
    finally:
        get_settings.cache_clear()


@pytest.mark.db
def test_storage_key_get_is_retired_but_worker_put_remains_capability_bound(tmp_path, monkeypatch):
    storage = LocalStorage(str(tmp_path), "http://browser", "http://worker")
    key = "user/job-attempt-1.png"
    path = storage.path(key)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"bytes")
    monkeypatch.setattr("app.files.get_storage", lambda: storage)
    with _client() as client:
        assert client.get(f"/api/v1/files/{key}").status_code == 404
        assert client.get(f"/api/v1/files/{key}", headers={"Authorization": "Bearer ordinary"}).status_code == 404
        assert client.put(f"/api/v1/files/{key}", content=b"bytes").status_code == 403
        # A retired route that a generated client still offers is a route that
        # still exists, whatever it answers.
        assert sorted(client.get("/openapi.json").json()
                      ["paths"]["/api/v1/files/{key}"]) == ["put"]


@pytest.mark.db
def test_asset_id_read_is_session_bound_and_owner_scoped(tmp_path, monkeypatch):
    asset_id = uuid.uuid5(uuid.NAMESPACE_URL, "endpoint-hardening:asset")
    storage = LocalStorage(str(tmp_path), "http://browser", "http://worker")
    monkeypatch.setattr("app.files.get_storage", lambda: storage)
    with _client() as client:
        owner = client.portal.call(_persist_user, "user", "asset-owner")
        other = client.portal.call(_persist_user, "viewer", "asset-other")
        admin = client.portal.call(_persist_user, "admin", "asset-admin")
        key = f"{owner.id}/asset.png"
        path = storage.path(key)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"asset-bytes")
        async def create_asset() -> None:
            assert db.session_factory is not None
            async with db.session_factory() as session:
                session.add(Asset(id=asset_id, user_id=owner.id, storage_key=key,
                                  mime="image/png", width=1, height=1))
                await session.commit()
        client.portal.call(create_asset)
        app.dependency_overrides[current_user] = lambda: owner
        owner_response = client.get(f"/api/v1/assets/{asset_id}")
        assert owner_response.status_code == 200
        assert owner_response.content == b"asset-bytes"
        assert key not in owner_response.request.url.path
        app.dependency_overrides[current_user] = lambda: other
        assert client.get(f"/api/v1/assets/{asset_id}").status_code == 404
        app.dependency_overrides[current_user] = lambda: admin
        assert client.get(f"/api/v1/assets/{asset_id}").status_code == 200


@pytest.mark.db
def test_expired_asset_is_not_readable_by_owner_or_admin(tmp_path, monkeypatch):
    asset_id = uuid.uuid5(uuid.NAMESPACE_URL, "endpoint-hardening:expired-asset")
    storage = LocalStorage(str(tmp_path), "http://browser", "http://worker")
    monkeypatch.setattr("app.files.get_storage", lambda: storage)
    with _client() as client:
        owner = client.portal.call(_persist_user, "user", "expired-asset-owner")
        admin = client.portal.call(_persist_user, "admin", "expired-asset-admin")
        key = f"{owner.id}/expired-asset.png"
        path = storage.path(key)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"expired-asset-bytes")

        async def create_asset() -> None:
            assert db.session_factory is not None
            async with db.session_factory() as session:
                session.add(Asset(id=asset_id, user_id=owner.id, storage_key=key,
                                  mime="image/png", width=1, height=1,
                                  expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc)))
                await session.commit()

        client.portal.call(create_asset)
        for principal in (owner, admin):
            app.dependency_overrides[current_user] = lambda principal=principal: principal
            assert client.get(f"/api/v1/assets/{asset_id}").status_code == 404


class _PresignClient:
    def __init__(self):
        self.calls = []

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.calls.append((operation, Params, ExpiresIn))
        return f"https://storage.test/{Params['Key']}?X-Amz-Expires={ExpiresIn}"


@pytest.mark.parametrize(
    ("method", "expected"),
    (("url", 300), ("worker_fetch_url", 900)),
)
def test_storage_url_purposes_have_separate_expiry_contracts(method, expected):
    fake = _PresignClient()
    storage = object.__new__(S3Storage)
    storage.client = fake
    storage.bucket = "assets"
    result = asyncio.run(getattr(storage, method)("user/asset.png"))
    assert parse_qs(urlsplit(result).query)["X-Amz-Expires"] == [str(expected)]
    assert fake.calls[-1][2] == expected


@pytest.mark.db
def test_sensitive_authenticated_responses_are_not_cached():
    with _client() as client:
        responses = [
            client.get("/api/v1/models"),
            client.get("/api/v1/assets/00000000-0000-0000-0000-000000000000"),
            client.get("/api/v1/studio/gpu"),
            client.get("/api/v1/benchmark/sessions"),
        ]
    for response in responses:
        assert response.headers.get("cache-control") == "no-store"


@pytest.mark.db
def test_auth_mode_none_keeps_owner_data_and_install_access():
    with _client() as client:
        assert client.get("/api/v1/generations").status_code == 200
        assert client.get("/api/v1/telemetry/preview").status_code == 200
        assert client.get("/api/v1/models").status_code == 200


def test_worker_input_rejects_non_ascii_capability_token(monkeypatch):
    monkeypatch.setattr(jobs, "time", SimpleNamespace(time=lambda: 1_800_000_000))
    capability = "endpoint-hardening-live-capability"
    expires = 1_800_000_300
    jobs.register_input_capability(capability, "input/source.png", expires)
    try:
        with _client() as client:
            response = client.get(
                "/api/v1/worker-input",
                params={"token": "é", "expires": expires},
            )
        assert response.status_code == 403
    finally:
        jobs.input_capabilities.pop(capability, None)
