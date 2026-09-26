"""A NUL or a lone surrogate must be refused at the edge, not reach the
database or a strict .encode() (issue #494)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from unittest.mock import MagicMock

from app import db, realtime
from app.keyring import get_key_ring
from app.main import app
from app.manifests import Manifest, json_finite, storable_text
from app.realtime import Worker
from app.settings import get_settings
from app.tables import Job

PASSWORD = "a-long-enough-account-password"
ORIGIN = "https://studio.example.com"
ROOT_KEYS = "1:" + "A" * 43 + "="

MANIFEST = {
    "id": "sd-test",
    "name": "SD Test",
    "capabilities": ["text_to_image"],
    "parameters": {
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
        "required": ["prompt"],
    },
    "min_vram_gb": 0,
    "prompt_token_limit": 77,
}


@pytest.fixture(autouse=True)
def clear_dependency_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def accounts(portal_runner, monkeypatch):
    """An install that has enabled accounts, in the mode the login tests need."""
    monkeypatch.setenv("ROOT_KEYS", ROOT_KEYS)
    monkeypatch.setenv("PUBLIC_URL", ORIGIN)
    get_settings.cache_clear()
    get_key_ring.cache_clear()
    assert portal_runner(db.connect()) is True
    portal_runner(db.enable_accounts_mode(db.session_factory))
    portal_runner(db.dispose())
    monkeypatch.setenv("AUTH_MODE", "accounts")
    get_settings.cache_clear()
    assert portal_runner(db.connect()) is True
    original = db.local_user_id

    async def clear() -> None:
        async with db.session_factory() as session:
            for table in ("recovery_codes", "auth_factors", "auth_tokens", "sessions",
                          "auth_identities", "audit_events", "mail_outbox", "oauth_flows",
                          "installation_auth_state"):
                await session.execute(text(f"DELETE FROM {table}"))
            await session.execute(text("DELETE FROM users WHERE id <> :id"), {"id": original})
            await session.execute(
                text("UPDATE users SET email = :local, role = 'admin', state = 'active' "
                     "WHERE id = :id"),
                {"local": db.LOCAL_USER_EMAIL, "id": original})
            await session.commit()

    try:
        yield portal_runner
    finally:
        if db.session_factory is None:
            portal_runner(db.connect())
        portal_runner(clear())
        portal_runner(db.dispose())
        get_settings.cache_clear()
        get_key_ring.cache_clear()


def _client():
    return TestClient(app)


def test_storable_text_refuses_nul_and_lone_surrogates():
    assert storable_text("\x00") is False
    assert storable_text("a\x00b") is False
    assert storable_text("\ud800") is False
    assert storable_text("x\udfffy") is False
    assert storable_text("") is True
    assert storable_text("plain") is True
    assert storable_text("\x01") is True
    assert storable_text("\x7f") is True
    assert storable_text("\u00e9") is True
    assert storable_text("\U0001f600") is True


def test_json_finite_refuses_unstorable_text():
    assert json_finite({"prompt": "a\x00"}) is False
    assert json_finite({"a\x00": 1}) is False
    assert json_finite(["\ud800"]) is False
    assert json_finite({"prompt": "fine", "n": 1.5}) is True


@pytest.mark.db
def test_generations_search_with_nul_answers_422():
    with _client() as client:
        assert client.get("/api/v1/generations", params={"q": "\x00"}).status_code == 422


@pytest.mark.db
def test_login_with_unstorable_text_answers_422(accounts):
    with _client() as client:
        assert client.post(
            "/api/v1/auth/login",
            headers={"Origin": ORIGIN},
            json={"email": "a\x00@example.com", "password": PASSWORD},
        ).status_code == 422
        assert client.post(
            "/api/v1/auth/login",
            headers={"Origin": ORIGIN},
            json={"email": "a@example.com", "password": "x\x00" + PASSWORD},
        ).status_code == 422
        assert client.post(
            "/api/v1/auth/login",
            headers={"Origin": ORIGIN},
            content='{"email":"a@example.com","password":"\\ud800"}',
        ).status_code == 422


@pytest.mark.db
def test_generation_with_nul_prompt_answers_422_and_creates_no_row():
    async def job_count() -> int:
        assert db.session_factory is not None
        async with db.session_factory() as session:
            return len((await session.execute(select(Job.id))).scalars().all())

    with _client() as client:
        before = client.portal.call(job_count)
        worker = Worker(id="w-unstorable", ws=MagicMock(),
                        manifests=[Manifest(**MANIFEST)], realtime_slots=1)
        realtime.workers[worker.id] = worker
        try:
            response = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "a\x00b"}},
            )
        finally:
            realtime.workers.clear()
        assert response.status_code == 422
        assert client.portal.call(job_count) == before