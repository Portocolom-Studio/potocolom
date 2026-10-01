"""The one banner every SPA shows: set by an administrator, read by everyone.

A single row, so every test clears it itself (the `accounts` fixture's own
cleanup does not touch status_banner, the same way test_account_states.py
clears the tables its own tests touch).
"""

import pytest
from app import db
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from tests.test_totp_flow import ORIGIN, _csrf, _login, _make, accounts

__all__ = ["accounts"]


def _admin(client, email="banneradmin@example.com"):
    client.portal.call(_make, email, "admin")
    assert _login(client, email).status_code == 204


def _set(client, **body):
    return client.put("/api/v1/admin/banner", headers=_csrf(client), json=body)


def _clear(client):
    return client.delete("/api/v1/admin/banner", headers=_csrf(client))


async def _clear_row() -> None:
    async with db.session_factory() as session:
        await session.execute(text("DELETE FROM status_banner"))
        await session.commit()


@pytest.mark.db
def test_config_carries_the_banner_when_set_and_null_when_clear(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        _admin(client)
        assert client.get("/api/v1/config").json()["banner"] is None

        assert _set(client, kind="high_demand",
                    message_key="app.banner.high_demand").status_code == 204
        body = client.get("/api/v1/config").json()["banner"]
        assert body == {"kind": "high_demand", "message_key": "app.banner.high_demand",
                        "custom_text": None}

        assert _clear(client).status_code == 204
        assert client.get("/api/v1/config").json()["banner"] is None
        client.portal.call(_clear_row)


@pytest.mark.db
def test_put_rejects_a_kind_outside_the_allowed_set(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        _admin(client)
        assert _set(client, kind="outage", custom_text="down").status_code == 422
        client.portal.call(_clear_row)


@pytest.mark.db
def test_put_rejects_a_message_key_that_does_not_match_its_kind(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        _admin(client)
        assert _set(client, kind="high_demand",
                    message_key="app.banner.maintenance").status_code == 422
        client.portal.call(_clear_row)


@pytest.mark.db
def test_put_rejects_custom_text_over_280_characters(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        _admin(client)
        assert _set(client, kind="degraded", custom_text="x" * 281).status_code == 422
        client.portal.call(_clear_row)


@pytest.mark.db
def test_put_rejects_neither_a_message_key_nor_custom_text(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        _admin(client)
        assert _set(client, kind="maintenance").status_code == 422
        client.portal.call(_clear_row)


@pytest.mark.db
def test_custom_text_is_accepted_without_a_message_key(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        _admin(client)
        assert _set(client, kind="maintenance", custom_text="Back soon.").status_code == 204
        body = client.get("/api/v1/config").json()["banner"]
        assert body == {"kind": "maintenance", "message_key": None, "custom_text": "Back soon."}
        client.portal.call(_clear_row)


@pytest.mark.db
def test_a_viewer_and_a_member_cannot_set_or_clear_the_banner(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "viewer13@example.com", "viewer")
        assert _login(client, "viewer13@example.com").status_code == 204
        assert _set(client, kind="high_demand",
                    message_key="app.banner.high_demand").status_code == 403
        assert _clear(client).status_code == 403

    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "member13@example.com", "user")
        assert _login(client, "member13@example.com").status_code == 204
        assert _set(client, kind="high_demand",
                    message_key="app.banner.high_demand").status_code == 403
        assert _clear(client).status_code == 403


@pytest.mark.db
def test_delete_clears_a_banner_that_was_never_set(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        _admin(client)
        assert _clear(client).status_code == 204


@pytest.mark.db
def test_set_and_clear_are_audited(accounts):
    from app.tables import AuditEvent

    async def events(action: str) -> list[AuditEvent]:
        async with db.session_factory() as session:
            return list((await session.execute(
                select(AuditEvent).where(AuditEvent.action == action)
            )).scalars().all())

    with TestClient(app, base_url=ORIGIN) as client:
        _admin(client)
        assert _set(client, kind="degraded", custom_text="Slower than usual.").status_code == 204
        assert _clear(client).status_code == 204

        set_events = client.portal.call(events, "PUT /api/v1/admin/banner")
        clear_events = client.portal.call(events, "DELETE /api/v1/admin/banner")
    assert len(set_events) == 1
    assert len(clear_events) == 1


@pytest.mark.db
def test_setting_the_banner_invalidates_the_cache_immediately(accounts):
    """The config route would otherwise serve the old value, set or cleared,
    for up to the 5 second TTL: a reader who just changed it should not see
    their own write lag behind."""
    with TestClient(app, base_url=ORIGIN) as client:
        _admin(client)
        assert client.get("/api/v1/config").json()["banner"] is None
        assert _set(client, kind="high_demand",
                    message_key="app.banner.high_demand").status_code == 204
        # No sleep: if the cache were not invalidated on write, this would
        # still read the pre-write None for up to CACHE_TTL seconds.
        assert client.get("/api/v1/config").json()["banner"] is not None
        assert _clear(client).status_code == 204
        assert client.get("/api/v1/config").json()["banner"] is None
