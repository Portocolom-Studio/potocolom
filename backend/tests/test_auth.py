import asyncio
import uuid

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from app import db
from app.auth import require
from app.capabilities import Capability
from app.main import app
from app.settings import get_settings
from app.tables import User
from tests.test_invitations import _account, _sign_in, accounts

__all__ = ["accounts"]


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


@pytest.mark.parametrize(
    ("role", "capability", "allowed"),
    (
        ("viewer", Capability.GENERATE, False),
        ("viewer", Capability.EDIT_OWN_WORK, False),
        ("viewer", Capability.VIEW_OWN_USAGE, False),
        ("viewer", Capability.ADMINISTER, False),
        ("user", Capability.GENERATE, True),
        ("user", Capability.EDIT_OWN_WORK, True),
        ("user", Capability.VIEW_OWN_USAGE, True),
        ("user", Capability.ADMINISTER, False),
        ("admin", Capability.GENERATE, True),
        ("admin", Capability.EDIT_OWN_WORK, True),
        ("admin", Capability.VIEW_OWN_USAGE, True),
        ("admin", Capability.ADMINISTER, True),
    ),
)
def test_a_role_holds_only_its_capability_bundle(role, capability, allowed):
    user = User(email=f"{role}@example.test", role=role)
    dependency = require(capability)
    if allowed:
        assert asyncio.run(dependency(_request(), user)) is user
        return
    with pytest.raises(HTTPException) as denied:
        asyncio.run(dependency(_request(), user))
    assert denied.value.status_code == 403
    assert denied.value.detail == "insufficient role"


def test_an_unknown_role_holds_no_capability():
    stranger = User(email="stranger@example.test", role="billing")
    request = _request()
    for capability in Capability:
        dependency = require(capability)
        with pytest.raises(HTTPException) as denied:
            asyncio.run(dependency(request, stranger))
        assert denied.value.status_code == 403
        assert denied.value.detail == "insufficient role"


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
        user_id = client.portal.call(db._ensure_local_user, db.session_factory, "none")
        assert user_id == db.local_user_id

        async def read_role() -> str:
            assert db.session_factory is not None
            async with db.session_factory() as session:
                user = await session.get(User, user_id)
                assert user is not None
                return user.role

        assert client.portal.call(read_role) == "admin"


# The star route answers 404 for a job that does not exist, so a 404 shows that
# the request got past the origin check and reached the route.
_MISSING_STAR = f"/api/v1/generations/{uuid.UUID(int=0)}/star"


@pytest.mark.db
def test_none_mode_refuses_an_unsafe_request_from_a_foreign_origin():
    """AUTH_MODE=none answers every request as the administrator, so a page the
    operator visits must not reach a state-changing route through the browser.
    Accounts mode already refuses this (test_session_policy.py)."""
    with TestClient(app) as client:
        foreign = client.post(_MISSING_STAR, headers={"Origin": "https://evil.example"})
        assert foreign.status_code == 403
        assert foreign.json()["detail"] == "origin not allowed"


@pytest.mark.db
def test_none_mode_refuses_an_unsafe_request_with_no_origin():
    with TestClient(app) as client:
        client.headers.pop("origin", None)
        absent = client.post(_MISSING_STAR)
        assert absent.status_code == 403
        assert absent.json()["detail"] == "origin not allowed"


@pytest.mark.db
@pytest.mark.parametrize(
    ("role", "state", "expected"),
    (
        ("viewer", "active", []),
        ("viewer", "suspended", []),
        ("user", "active", ["edit_own_work", "generate", "view_own_usage"]),
        ("user", "suspended", ["view_own_usage"]),
        ("admin", "active", ["administer", "edit_own_work", "generate", "view_own_usage"]),
        ("admin", "suspended", ["administer", "view_own_usage"]),
    ),
)
def test_account_reports_effective_capabilities(accounts, role, state, expected):
    """The studio reads this list. A suspended account keeps reads and loses writes."""
    email = f"{role}-{state}@example.com"
    _account(accounts, email, role=role, state=state)
    with TestClient(app) as client:
        _sign_in(client, email)
        assert client.get("/api/v1/account").json()["capabilities"] == expected


@pytest.mark.db
@pytest.mark.parametrize(
    ("role", "method", "path", "body", "detail"),
    (
        ("viewer", "get", "/api/v1/usage/me", None, "insufficient role"),
        ("user", "get", "/api/v1/studio/gpu", None, "insufficient role"),
        ("viewer", "get", "/api/v1/telemetry/preview", None, "insufficient role"),
        ("viewer", "post", "/api/v1/shares",
         {"asset_id": "00000000-0000-0000-0000-000000000000", "days": 1},
         "insufficient role"),
        ("user", "post", "/api/v1/invitations",
         {"email": "invitee@example.com", "role": "user"}, "insufficient role"),
    ),
)
def test_a_missing_capability_answers_insufficient_role(accounts, role, method, path, body, detail):
    """The detail is the capability refusal, not an origin, CSRF or suspension answer."""
    email = f"gate-{role}-{method}-{path.rsplit('/', 1)[-1]}@example.com"
    _account(accounts, email, role=role)
    with TestClient(app) as client:
        headers = _sign_in(client, email)
        response = client.request(method, path, headers=headers, json=body)
        assert response.status_code == 403
        assert response.json()["detail"] == detail


@pytest.mark.db
def test_a_refused_administer_attempt_is_audited_at_high_severity(accounts):
    _account(accounts, "bundle-climber@example.com", role="user")
    with TestClient(app) as client:
        headers = _sign_in(client, "bundle-climber@example.com")
        refused = client.post(
            "/api/v1/invitations", headers=headers,
            json={"email": "invitee@example.com", "role": "user"},
        )
        assert refused.status_code == 403
        assert refused.json()["detail"] == "insufficient role"

        async def rows():
            from app import audit
            return await audit.search()

        denied = [
            row for row in client.portal.call(rows)
            if row["action"] == "POST /api/v1/invitations"
        ]
    assert len(denied) == 1
    assert denied[0]["severity"] == "high"


@pytest.mark.db
def test_own_usage_and_a_share_of_a_missing_asset_pass_their_capabilities(accounts):
    email = "member-gate@example.com"
    _account(accounts, email, role="user")
    with TestClient(app) as client:
        headers = _sign_in(client, email)
        usage = client.get("/api/v1/usage/me")
        assert usage.status_code == 200
        missing = client.post(
            "/api/v1/shares", headers=headers,
            json={"asset_id": "00000000-0000-0000-0000-000000000000", "days": 1},
        )
        assert missing.status_code == 404
        assert missing.json()["detail"] == "no such asset"


@pytest.mark.db
def test_none_mode_accepts_the_allowed_origin_and_a_safe_request_without_one():
    allowed = get_settings().public_url
    with TestClient(app) as client:
        assert client.post(_MISSING_STAR, headers={"Origin": allowed}).status_code == 404

        client.headers.pop("origin", None)
        assert client.get("/api/v1/generations").status_code == 200
