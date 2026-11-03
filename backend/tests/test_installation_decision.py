"""The installation decides its own auth mode (issue #792).

A new install starts unclaimed. Every API route then answers 503 setup_required
until someone spends the setup link and chooses an administrator account or no
accounts. A none install can turn accounts on later from its own administrator.
"""

import asyncio
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from starlette.websockets import WebSocketDisconnect

from app import auth_mode, db, enable, keyring, realtime
from app.main import app
from app.settings import Settings, get_settings
from app.tables import AuthIdentity, AuthToken, InstallationAuthState
from tests.test_realtime import FLEET_HEADERS, answer_ready, expect, hello

ROOT_KEYS = "1:" + "A" * 43 + "="
ORIGIN = "http://localhost:8000"
EMAIL = "owner@example.com"
PASSWORD = "a-long-enough-first-admin-password"
BACKEND = Path(__file__).resolve().parents[1]

# Protected routes, so the gate's 503 is checked on each shape of route: a read,
# a read with an account behind it, a read of an admin surface, and a write.
PROTECTED = [
    ("GET", "/api/v1/models"),
    ("GET", "/api/v1/generations"),
    ("GET", "/api/v1/telemetry/preview"),
    ("GET", "/api/v1/account"),
    ("POST", "/api/v1/auth/login"),
]

# (persisted mode, AUTH_MODE override) -> what startup does. A missing row is
# the same as unclaimed. "refused" means startup stops and names AUTH_MODE.
OUTCOMES = {
    (None, None): "unclaimed",
    (None, "none"): "none",
    (None, "accounts"): "accounts",
    ("unclaimed", None): "unclaimed",
    ("unclaimed", "none"): "none",
    ("unclaimed", "accounts"): "accounts",
    ("none", None): "none",
    ("none", "none"): "none",
    ("none", "accounts"): "refused",
    ("accounts", None): "accounts",
    ("accounts", "none"): "refused",
    ("accounts", "accounts"): "accounts",
}


async def _wipe() -> None:
    """Back to a database with no decision and no people in it."""
    async with db.session_factory() as session:
        await session.execute(text("UPDATE jobs SET source_asset_id = NULL"))
        for table in ("auth_tokens", "auth_identities", "sessions", "audit_events",
                      "assets", "jobs", "installation_auth_state"):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.execute(text("DELETE FROM users"))
        await session.commit()


async def _set_row(mode: str | None) -> None:
    async with db.session_factory() as session:
        await session.execute(text("DELETE FROM installation_auth_state"))
        if mode is not None:
            await session.execute(
                text("INSERT INTO installation_auth_state (id, auth_mode, root_key_version) "
                     "VALUES (1, :mode, :version)"),
                {"mode": mode, "version": 1 if mode == "accounts" else None})
        await session.commit()


async def _row() -> str | None:
    async with db.session_factory() as session:
        state = await session.get(InstallationAuthState, 1)
        return state.auth_mode if state is not None else None


async def _identities() -> list[AuthIdentity]:
    async with db.session_factory() as session:
        return list((await session.execute(select(AuthIdentity))).scalars().all())


@pytest.fixture
def decision(portal_runner, monkeypatch):
    """A database with no decision yet. The test's environment chooses the mode."""
    monkeypatch.setenv("ROOT_KEYS", ROOT_KEYS)
    monkeypatch.setenv("PUBLIC_URL", ORIGIN)
    monkeypatch.delenv("AUTH_MODE", raising=False)
    get_settings.cache_clear()
    keyring.get_key_ring.cache_clear()
    assert portal_runner(db.connect(serving=False)) is True
    portal_runner(_wipe())
    try:
        yield portal_runner
    finally:
        if db.session_factory is None:
            portal_runner(db.connect(serving=False))
        portal_runner(_wipe())
        portal_runner(db.dispose())
        auth_mode.clear()
        get_settings.cache_clear()
        keyring.get_key_ring.cache_clear()


def _serve(decision, **kwargs):
    """Connects the way a starting API does, on a fresh engine."""
    decision(db.dispose())
    return decision(db.connect(**kwargs))


def _slow_hashing(monkeypatch) -> None:
    """Every password hash takes a third of a second, inside the claim's
    transaction, so two claims overlap for certain. Without SETUP_LOCK both pass
    the identity check before either commits, and both adopt the one row."""
    real = enable.hash_password

    def slow(password):
        time.sleep(0.3)
        return real(password)

    monkeypatch.setattr(enable, "hash_password", slow)


async def _plant_link(token: str) -> None:
    """A second live link, which minting would never leave outstanding beside
    the first. The races below need two, because one link is serialized by its own row."""
    async with db.session_factory() as session:
        async with session.begin():
            session.add(AuthToken(
                purpose="setup",
                token_hash=enable._token_hash(token),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            ))


def _choose(client, token, **body):
    return client.post("/api/v1/auth/setup", json={"token": token, **body})


# ---------------------------------------------------------------- startup


@pytest.mark.db
@pytest.mark.parametrize(("persisted", "override"), list(OUTCOMES))
def test_startup_starts_or_refuses_as_the_matrix_says(decision, monkeypatch, persisted, override):
    decision(_set_row(persisted))
    if override is not None:
        monkeypatch.setenv("AUTH_MODE", override)
        get_settings.cache_clear()
    expected = OUTCOMES[(persisted, override)]
    if expected == "refused":
        with pytest.raises(RuntimeError, match="AUTH_MODE"):
            _serve(decision)
        return
    assert _serve(decision) is True
    assert auth_mode.current() == expected
    assert auth_mode.setup_required() is (expected == "unclaimed")
    if expected != "unclaimed":
        assert decision(_row()) == expected


@pytest.mark.db
def test_an_override_is_recorded_on_an_unclaimed_install(decision, monkeypatch):
    """The override decides an install that has no decision yet, so the row says so."""
    decision(_set_row("unclaimed"))
    monkeypatch.setenv("AUTH_MODE", "none")
    get_settings.cache_clear()
    assert _serve(decision) is True
    assert decision(_row()) == "none"


# ---------------------------------------------------------------- the gate


@pytest.mark.db
def test_an_unclaimed_install_answers_setup_required_outside_the_setup_surface(decision):
    with TestClient(app, base_url=ORIGIN) as client:
        assert auth_mode.current() == "unclaimed"
        for method, path in PROTECTED:
            response = client.request(method, path, json={} if method == "POST" else None)
            assert response.status_code == 503, path
            # The body names the state and nothing about the data behind the route.
            assert response.json() == {"detail": "setup_required"}, path

        config = client.get("/api/v1/config").json()
        assert config["setup_required"] is True
        assert config["auth_methods"] == []

        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/ready").status_code == 200
        # The setup route is reached: a wrong link gets the link answer, not the gate's.
        assert _choose(client, "not-a-real-link", mode="none").status_code == 403

        # Workers can connect while setup is pending.
        with client.websocket_connect("/api/v1/fleet", headers=FLEET_HEADERS) as worker:
            worker.send_json(hello())
            expect(worker, "registered")

        # A browser canvas is refused before accept, which is HTTP 403 on the wire.
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/realtime") as canvas:
                canvas.receive_json()


# ---------------------------------------------------------------- the choice


@pytest.mark.db
def test_the_setup_link_can_choose_an_administrator_account(decision):
    with TestClient(app, base_url=ORIGIN) as client:
        token = client.portal.call(enable.mint_setup_token)
        response = _choose(client, token, mode="accounts", email=EMAIL, password=PASSWORD)
        assert response.status_code == 204
        assert any(header.startswith("potocolom_session=")
                   for header in response.headers.get_list("set-cookie"))
        assert auth_mode.current() == "accounts"
        assert client.portal.call(_row) == "accounts"
        assert client.get("/api/v1/config").json()["auth_methods"] == ["password"]
        client.cookies.clear()
        assert client.get("/api/v1/models").status_code == 401


@pytest.mark.db
def test_the_setup_link_can_choose_no_accounts(decision):
    with TestClient(app, base_url=ORIGIN) as client:
        token = client.portal.call(enable.mint_setup_token)
        response = _choose(client, token, mode="none")
        assert response.status_code == 204
        # No accounts means no session: there is nobody to sign in.
        assert "set-cookie" not in response.headers
        assert auth_mode.current() == "none"
        assert client.portal.call(_row) == "none"
        assert client.portal.call(_identities) == []
        config = client.get("/api/v1/config").json()
        assert config["setup_required"] is False
        assert config["auth_methods"] == []
        # The implicit administrator answers, as it does in none mode.
        assert client.get("/api/v1/models").status_code == 200


@pytest.mark.db
def test_a_wrong_or_expired_link_gets_the_same_refusal_for_no_accounts(decision):
    async def age(token: str) -> None:
        async with db.session_factory() as session:
            await session.execute(text("UPDATE auth_tokens SET expires_at = now() - interval '1 minute'"))
            await session.commit()

    with TestClient(app, base_url=ORIGIN) as client:
        token = client.portal.call(enable.mint_setup_token)
        client.portal.call(age, token)
        expired = _choose(client, token, mode="none")
        wrong = _choose(client, "not-a-real-link", mode="none")
        assert expired.status_code == wrong.status_code == 403
        assert expired.json() == wrong.json()
        # Neither refusal decided anything: the install is still undecided.
        assert client.portal.call(_row) in {None, "unclaimed"}
        assert auth_mode.current() == "unclaimed"


@pytest.mark.db
def test_only_one_of_two_concurrent_no_accounts_choices_wins(decision):
    with TestClient(app, base_url=ORIGIN) as client:
        token = client.portal.call(enable.mint_setup_token)

        async def race():
            return await asyncio.gather(
                enable.decline_accounts(token),
                enable.decline_accounts(token),
                return_exceptions=True,
            )

        results = client.portal.call(race)
        won = [result for result in results if result is None]
        lost = [result for result in results if isinstance(result, HTTPException)]
        assert len(won) == 1
        assert len(lost) == 1 and lost[0].status_code == 403
        assert client.portal.call(_row) == "none"


@pytest.mark.db
def test_two_live_links_cannot_both_claim_the_installation(decision, monkeypatch):
    """Two different links claimed at once. Each link's own row serializes only
    its own claim, so SETUP_LOCK is what keeps the second from adopting too."""
    assert _serve(decision) is True
    first = decision(enable.mint_setup_token())
    second = "a-second-live-link-for-the-race"
    decision(_plant_link(second))
    _slow_hashing(monkeypatch)

    async def race():
        return await asyncio.gather(
            enable.claim(first, "first@example.com", PASSWORD),
            enable.claim(second, "second@example.com", PASSWORD),
            return_exceptions=True,
        )

    results = decision(race())
    won = [result for result in results if not isinstance(result, Exception)]
    assert len(won) == 1
    # The loser is refused by the claimed answer, not by the unique index that
    # would also stop a second password identity from committing.
    (loser,) = [result for result in results if isinstance(result, Exception)]
    assert isinstance(loser, HTTPException) and loser.status_code == 409
    assert len(decision(_identities())) == 1


@pytest.mark.db
def test_a_live_link_cannot_switch_a_claimed_install_to_none_beside_a_claim(decision, monkeypatch):
    """A claim and a no-accounts choice race on two live links. Exactly one wins,
    and the recorded mode agrees with the winner."""
    assert _serve(decision) is True
    first = decision(enable.mint_setup_token())
    second = "a-second-live-link-for-the-mode-race"
    decision(_plant_link(second))
    _slow_hashing(monkeypatch)

    async def race():
        return await asyncio.gather(
            enable.claim(first, EMAIL, PASSWORD),
            enable.decline_accounts(second),
            return_exceptions=True,
        )

    results = decision(race())
    won = [result for result in results if not isinstance(result, Exception)]
    assert len(won) == 1
    (loser,) = [result for result in results if isinstance(result, Exception)]
    assert isinstance(loser, HTTPException) and loser.status_code == 409
    mode = decision(_row())
    if isinstance(results[0], Exception):
        assert mode == "none" and decision(_identities()) == []
    else:
        assert mode == "accounts" and len(decision(_identities())) == 1


@pytest.mark.db
def test_a_stale_unclaimed_view_cannot_switch_an_accounts_install_to_none(decision):
    """Another process turned accounts on while this one still says unclaimed.
    The refused choice must not flip the database back, and must not spend the link."""
    assert _serve(decision) is True
    token = decision(enable.mint_setup_token())
    decision(db.enable_accounts_mode(db.session_factory))
    with pytest.raises(HTTPException) as refused:
        decision(enable.decline_accounts(token))
    assert refused.value.status_code == 409
    assert decision(_row()) == "accounts"
    # The refused choice left the link unspent, so the administrator can still claim.
    adopted = decision(enable.claim(token, EMAIL, PASSWORD))
    assert adopted is not None


@pytest.mark.db
@pytest.mark.parametrize("mode", ["unclaimed", "none"])
def test_missing_root_keys_rolls_back_setup_or_enable(decision, monkeypatch, mode):
    decision(_set_row(mode))
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as client:
        token = client.portal.call(enable.mint_setup_token) if mode == "unclaimed" else None
        monkeypatch.setenv("ROOT_KEYS", "")
        get_settings.cache_clear()
        keyring.get_key_ring.cache_clear()
        body = {"email": EMAIL, "password": PASSWORD}
        if token:
            response = _choose(client, token, mode="accounts", **body)
        else:
            response = client.post("/api/v1/auth/enable", json=body, headers={"origin": ORIGIN})
        assert response.status_code == 503
        assert response.json()["detail"] == "root_keys_missing"
        assert "install" in response.json()["message"]
        assert "scripts/ensure-env.sh" in response.json()["message"]
        assert "recreate the API container" in response.json()["message"]
        assert client.portal.call(_row) == mode
        assert client.portal.call(_identities) == []
        assert auth_mode.current() == mode
        if token:
            monkeypatch.setenv("ROOT_KEYS", ROOT_KEYS)
            get_settings.cache_clear()
            keyring.get_key_ring.cache_clear()
            assert _choose(client, token, mode="accounts", **body).status_code == 204


# ---------------------------------------------------------------- runtime enable


@pytest.mark.db
def test_a_none_install_turns_accounts_on_without_a_restart(decision):
    decision(_set_row("none"))
    body = {"email": EMAIL, "password": PASSWORD}
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/v1/models").status_code == 200
        assert client.get("/api/v1/account").status_code == 404  # account routes stay off in none

        refused = client.post("/api/v1/auth/enable", json=body,
                              headers={"origin": "https://evil.example"})
        assert refused.status_code == 403
        assert client.portal.call(_row) == "none"

        response = client.post("/api/v1/auth/enable", json=body, headers={"origin": ORIGIN})
        assert response.status_code == 204
        assert any(header.startswith("potocolom_session=")
                   for header in response.headers.get_list("set-cookie"))
        assert auth_mode.current() == "accounts"
        assert client.portal.call(_row) == "accounts"

        # The very next request needs a session, with no restart in between.
        client.cookies.clear()
        assert client.get("/api/v1/models").status_code == 401
        # And the switch is one way: a second enable finds accounts on and stops.
        assert client.post("/api/v1/auth/enable", json=body,
                           headers={"origin": ORIGIN}).status_code == 404


@pytest.mark.db
def test_a_canvas_open_when_accounts_turn_on_is_closed(decision):
    decision(_set_row("none"))
    with TestClient(app, base_url=ORIGIN) as client:
        with client.websocket_connect("/api/v1/fleet", headers=FLEET_HEADERS) as worker:
            worker.send_json(hello(worker_id="w-enable"))
            expect(worker, "registered")
            with client.websocket_connect("/api/v1/realtime") as canvas:
                canvas.send_json({"type": "open", "model_id": "sd-sim",
                                  "params": {"prompt": "a red house on a hill"}})
                opened = expect(worker, "open_session")
                answer_ready(worker, opened)
                expect(canvas, "ready")

                response = client.post("/api/v1/auth/enable",
                                       json={"email": EMAIL, "password": PASSWORD},
                                       headers={"origin": ORIGIN})
                assert response.status_code == 204
                closed = canvas.receive_json()
    assert closed["type"] == "error"
    assert closed["code"] == 4401


@pytest.mark.db
def test_a_switch_with_no_override_survives_a_restart(decision):
    """The default path: a decided none install, switched at runtime, then started
    again with the same environment. The restart must come up in accounts."""
    decision(_set_row("none"))
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.post("/api/v1/auth/enable",
                               json={"email": EMAIL, "password": PASSWORD},
                               headers={"origin": ORIGIN})
        assert response.status_code == 204
    with TestClient(app, base_url=ORIGIN) as client:
        assert auth_mode.current() == "accounts"
        assert client.get("/api/v1/config").json()["auth_methods"] == ["password"]
        client.cookies.clear()
        assert client.get("/api/v1/models").status_code == 401


@pytest.mark.db
def test_a_tokenless_switch_is_refused_once_accounts_were_turned_on_offline(decision):
    """make auth-enable ran in another process. The database says accounts, this
    process still says none, and a request with no setup link must not adopt the
    implicit administrator."""
    decision(_set_row("none"))
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(db.enable_accounts_mode, db.session_factory)
        response = client.post("/api/v1/auth/enable",
                               json={"email": EMAIL, "password": PASSWORD},
                               headers={"origin": ORIGIN})
        assert response.status_code == 409
        assert "set-cookie" not in response.headers
        assert client.portal.call(_identities) == []


@pytest.mark.db
def test_a_switch_refuses_an_explicit_none_override_and_names_the_variable(decision, monkeypatch):
    """An override of none would refuse the next start, so the switch is refused
    before it writes anything."""
    decision(_set_row("none"))
    monkeypatch.setenv("AUTH_MODE", "none")
    get_settings.cache_clear()
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.post("/api/v1/auth/enable",
                               json={"email": EMAIL, "password": PASSWORD},
                               headers={"origin": ORIGIN})
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "AUTH_MODE" in detail and "deploy/compose/.env" in detail
        assert client.portal.call(_row) == "none"
        assert auth_mode.current() == "none"


@pytest.mark.db
def test_a_socket_that_withholds_open_across_the_switch_is_refused(decision):
    """A handshake made in none mode, with its open sent after the switch, must
    not register: it has no session and no authority past the switch."""
    decision(_set_row("none"))
    with TestClient(app, base_url=ORIGIN) as client:
        # A worker must be registered, or the model is unknown and the open is
        # refused before the registration this test is about.
        with client.websocket_connect("/api/v1/fleet", headers=FLEET_HEADERS) as worker:
            worker.send_json(hello(worker_id="w-withhold"))
            expect(worker, "registered")
            with client.websocket_connect("/api/v1/realtime") as canvas:
                response = client.post("/api/v1/auth/enable",
                                       json={"email": EMAIL, "password": PASSWORD},
                                       headers={"origin": ORIGIN})
                assert response.status_code == 204
                canvas.send_json({"type": "open", "model_id": "sd-sim",
                                  "params": {"prompt": "a red house on a hill"}})
                refused = canvas.receive_json()
    assert refused["type"] == "error"
    assert refused["code"] == 4401
    assert realtime.sessions == {}


@pytest.mark.db
def test_a_none_install_mints_no_setup_link(decision, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "none")
    get_settings.cache_clear()
    assert _serve(decision) is True
    with pytest.raises(enable.AlreadyClaimed, match="without accounts"):
        decision(enable.mint_setup_token())


# ---------------------------------------------------------------- the offline link


@pytest.mark.db
def test_setup_link_prints_the_bare_link_for_an_unclaimed_install(decision):
    decision(_set_row("unclaimed"))
    result = subprocess.run(
        [sys.executable, "-m", "app.operator", "setup-link"],
        cwd=BACKEND, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PUBLIC_URL": ORIGIN},
    )
    assert result.returncode == 0, result.stderr
    assert re.fullmatch(rf"{re.escape(ORIGIN)}/setup#[A-Za-z0-9_-]{{43}}", result.stdout.strip())


@pytest.mark.db
def test_setup_link_is_refused_for_a_none_install(decision):
    decision(_set_row("none"))
    result = subprocess.run(
        [sys.executable, "-m", "app.operator", "setup-link"],
        cwd=BACKEND, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PUBLIC_URL": ORIGIN},
    )
    # Status 3 is the installer's "already chose" signal, not a failure.
    assert result.returncode == 3
    assert "without accounts" in result.stderr
    assert result.stdout.strip() == ""


# ---------------------------------------------------------------- settings


def test_a_blank_auth_mode_means_follow_the_database(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "")
    assert Settings().auth_mode is None


def test_the_override_is_only_none_or_accounts(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "unclaimed")
    with pytest.raises(ValueError):
        Settings()


def test_oauth_offered_ignores_the_mode_so_a_runtime_switch_cannot_skip_the_check():
    settings = Settings(oauth_providers="google, github, facebook",
                        google_client_id="g", google_client_secret="gs")
    assert settings.oauth_offered == ["google"]


def test_sign_in_methods_follow_the_loaded_mode():
    from app.auth_mode import sign_in_methods

    settings = Settings(oauth_providers="google",
                        google_client_id="g", google_client_secret="gs")
    try:
        for mode in (auth_mode.UNCLAIMED, auth_mode.NONE):
            auth_mode.load(mode)
            assert sign_in_methods(settings) == []
        auth_mode.load(auth_mode.ACCOUNTS)
        assert sign_in_methods(settings) == ["password", "google"]
    finally:
        auth_mode.clear()
