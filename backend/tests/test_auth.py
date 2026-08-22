import asyncio
import uuid

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from app import db
from app.auth import require_role
from app.main import app
from app.tables import User


async def _set_local_role(role: str) -> None:
    assert db.session_factory is not None
    assert db.local_user_id is not None
    async with db.session_factory() as session:
        user = await session.get(User, db.local_user_id)
        assert user is not None
        user.role = role
        await session.commit()


def _request(method: str = "GET", path: str = "/api/v1/probe") -> Request:
    return Request({
        "type": "http", "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "headers": [], "server": ("testserver", 80), "client": None,
    })


def test_role_tiers_preserve_user_as_member_value():
    viewer = User(email="viewer@example.test", role="viewer")
    member = User(email="member@example.test", role="user")
    admin = User(email="admin@example.test", role="admin")
    member_dependency = require_role("member")
    admin_dependency = require_role("admin")

    request = _request()

    with pytest.raises(HTTPException) as denied:
        asyncio.run(member_dependency(request, viewer))
    assert denied.value.status_code == 403
    assert asyncio.run(member_dependency(request, member)) is member
    assert asyncio.run(member_dependency(request, admin)) is admin
    with pytest.raises(HTTPException) as denied:
        asyncio.run(admin_dependency(request, member))
    assert denied.value.status_code == 403


@pytest.mark.db
def test_viewer_reads_but_cannot_mutate_and_preview_is_admin_only():
    missing_job = uuid.UUID("00000000-0000-0000-0000-000000000000")
    with TestClient(app) as client:
        try:
            client.portal.call(_set_local_role, "viewer")
            assert client.get("/api/v1/generations").status_code == 200
            assert client.get("/api/v1/benchmark/sessions").status_code == 403
            assert client.post(
                f"/api/v1/generations/{missing_job}/star"
            ).status_code == 403
            assert client.get("/api/v1/telemetry/preview").status_code == 403

            client.portal.call(_set_local_role, "user")
            assert client.post(
                f"/api/v1/generations/{missing_job}/star"
            ).status_code == 404
            assert client.get("/api/v1/telemetry/preview").status_code == 403

            client.portal.call(_set_local_role, "admin")
            assert client.post(
                f"/api/v1/generations/{missing_job}/star"
            ).status_code == 404
            assert client.get("/api/v1/telemetry/preview").status_code == 200
        finally:
            client.portal.call(_set_local_role, "admin")


@pytest.mark.db
def test_existing_implicit_local_user_is_promoted_to_admin():
    with TestClient(app) as client:
        assert db.session_factory is not None
        assert db.local_user_id is not None
        client.portal.call(_set_local_role, "user")
        user_id = client.portal.call(db._ensure_local_user, db.session_factory)
        assert user_id == db.local_user_id

        async def read_role() -> str:
            assert db.session_factory is not None
            async with db.session_factory() as session:
                user = await session.get(User, user_id)
                assert user is not None
                return user.role

        assert client.portal.call(read_role) == "admin"
