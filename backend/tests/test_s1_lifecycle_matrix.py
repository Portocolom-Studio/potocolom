"""Lifecycle routes keep S1 command erasure inside the account boundary."""

import asyncio
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import command_store, db, deletion, sessions as account_sessions
from app.main import app
from app.settings import get_settings
from test_command_store import acknowledge_bound
from test_fleet_protocol6 import (
    FLEET_HEADERS,
    drop_worker_rows,
    fresh_pool,
    protocol6_client,
    root_keys,
)
from test_oauth import (
    ORIGIN as OAUTH_ORIGIN,
    _callback as oauth_callback,
    _fake_provider,
    _link as link_identity,
    _linked_through as link_through_route,
    _make as make_oauth_user,
    accounts as oauth_accounts,
)
from test_v6_sessions import command_ack, open_live_session
from test_totp_flow import _make as make_account_user

__all__ = ["oauth_accounts"]


@contextmanager
def _oauth_protocol_client(*worker_ids: str):
    with TestClient(app, base_url=OAUTH_ORIGIN, headers=FLEET_HEADERS) as client:
        try:
            yield client
        finally:
            client.portal.call(fresh_pool)
            for worker_id in worker_ids:
                client.portal.call(drop_worker_rows, worker_id)


def _auth_cookies(client, token: str, csrf: str) -> None:
    session_cookie, csrf_cookie = account_sessions.cookie_names(get_settings().public_url)
    client.cookies.set(session_cookie, token)
    client.cookies.set(csrf_cookie, csrf)


@pytest.mark.db
@pytest.mark.parametrize("transition", ["disabled", "deletion_pending"])
def test_terminal_account_transition_records_late_ack_without_publication(
    oauth_accounts, transition,
):
    worker_id = f"s1-terminal-{transition}-{uuid.uuid4()}"
    model_id = f"s1-terminal-model-{uuid.uuid4()}"
    with protocol6_client(worker_id) as client:
        user = client.portal.call(make_oauth_user, f"terminal-{uuid.uuid4()}@example.test")
        issued = client.portal.call(account_sessions.mint, user, False)
        _auth_cookies(client, issued.token, issued.csrf)
        worker_ws, worker, _grant, _open, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        try:
            browser_ws.send_json({"type": "update_params", "params": {"prompt": "private"}})
            command = worker_ws.receive_json()
            assert command["type"] == "update_session"

            if transition == "disabled":
                admin = client.portal.call(make_account_user, f"admin-{uuid.uuid4()}@example.test", "admin")
                admin_session = client.portal.call(account_sessions.mint, admin, False)
                _auth_cookies(client, admin_session.token, admin_session.csrf)
                response = client.post(
                    f"/api/v1/users/{user.id}/state",
                    headers={
                        "origin": get_settings().public_url,
                        "x-csrf-token": admin_session.csrf,
                    },
                    json={"state": "disabled"},
                )
            else:
                _auth_cookies(client, issued.token, issued.csrf)
                response = client.delete(
                    "/api/v1/account",
                    headers={
                        "origin": get_settings().public_url,
                        "x-csrf-token": issued.csrf,
                    },
                )
            assert response.status_code == 204

            async def inspect_erased():
                async with db.session_factory() as session:
                    command_row = (await session.execute(
                        text("SELECT state, encrypted_body FROM worker_commands "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                             "AND command_sequence = :sequence"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation,
                         "sequence": command["command_sequence"]},
                    )).mappings().one()
                    account = (await session.execute(
                        text("SELECT state FROM users WHERE id = :user_id"),
                        {"user_id": user.id},
                    )).scalar_one()
                    binding = (await session.execute(
                        text("SELECT revoked_at FROM sessions WHERE token_hash = :token_hash"),
                        {"token_hash": account_sessions.token_hash(issued.token)},
                    )).scalar_one()
                    return command_row, account, binding

            erased, state, revoked_at = client.portal.call(inspect_erased)
            assert erased["state"] == "erased"
            assert erased["encrypted_body"] is None
            assert state == transition
            assert revoked_at is not None

            ack = command_ack(command)
            result = client.portal.call(acknowledge_bound, ack, worker_id, worker.incarnation)
            assert result.recorded is True
            assert result.publication is None

            async def inspect_thin_outcome():
                async with db.session_factory() as session:
                    floor = await session.scalar(
                        text("SELECT acknowledged_floor FROM worker_connections "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation},
                    )
                    retained = await session.scalar(
                        text("SELECT count(*) FROM worker_commands "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                             "AND command_sequence = :sequence"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation,
                         "sequence": command["command_sequence"]},
                    )
                    realtime = (await session.execute(
                        text("SELECT params_revision, effective_params FROM realtime_sessions "
                             "WHERE id = :session_id"),
                        {"session_id": uuid.UUID(command["session_id"])},
                    )).mappings().one()
                    return floor, retained, realtime

            floor, retained, realtime = client.portal.call(inspect_thin_outcome)
            assert floor == command["command_sequence"]
            assert retained == 0
            assert realtime["params_revision"] == 1
            assert realtime["effective_params"]["prompt"] == "house"
        finally:
            browser.__exit__(None, None, None)
            fleet.__exit__(None, None, None)


@pytest.mark.db
def test_oauth_signin_erases_the_planted_session_command(oauth_accounts, monkeypatch):
    worker_id = f"s1-oauth-login-{uuid.uuid4()}"
    model_id = f"s1-oauth-login-model-{uuid.uuid4()}"
    subject = f"oauth-login-{uuid.uuid4()}@example.test"
    _fake_provider(monkeypatch, "google", subject="s1-oauth-login", email=subject)
    with _oauth_protocol_client(worker_id) as client:
        user = client.portal.call(make_oauth_user, subject)
        client.portal.call(link_identity, user.id, "google", "s1-oauth-login")
        planted = client.portal.call(account_sessions.mint, user, False)
        _auth_cookies(client, planted.token, planted.csrf)
        worker_ws, worker, _grant, _open, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        try:
            browser_ws.send_json({"type": "update_params", "params": {"prompt": "private"}})
            command = worker_ws.receive_json()
            assert command["type"] == "update_session"
            state = client.get("/api/v1/auth/redirect/google", follow_redirects=False)
            oauth_state = parse_qs(urlsplit(state.headers["location"]).query)["state"][0]
            response = oauth_callback(client, "google", oauth_state)
            assert response.status_code == 307
            assert client.get(
                "/api/v1/account",
                headers={"authorization": f"Bearer {planted.token}"},
            ).status_code == 401

            async def inspect():
                async with db.session_factory() as session:
                    return (await session.execute(
                        text("SELECT state, encrypted_body FROM worker_commands "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                             "AND command_sequence = :sequence"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation,
                         "sequence": command["command_sequence"]},
                    )).mappings().one()

            row = client.portal.call(inspect)
            assert row["state"] == "erased" and row["encrypted_body"] is None
            ack = command_ack(command)
            result = client.portal.call(acknowledge_bound, ack, worker_id, worker.incarnation)
            assert result.recorded is True and result.publication is None
        finally:
            browser.__exit__(None, None, None)
            fleet.__exit__(None, None, None)


@pytest.mark.db
def test_oauth_link_rotation_erases_other_session_and_keeps_its_own_command(
    oauth_accounts, monkeypatch,
):
    _fake_provider(
        monkeypatch, "github", subject=f"s1-link-{uuid.uuid4()}",
        email=f"s1-link-{uuid.uuid4()}@example.test",
    )
    keep_worker = f"s1-link-keep-{uuid.uuid4()}"
    other_worker = f"s1-link-other-{uuid.uuid4()}"
    with _oauth_protocol_client(keep_worker, other_worker) as client:
        user = client.portal.call(make_oauth_user, f"s1-link-owner-{uuid.uuid4()}@example.test")
        kept = client.portal.call(account_sessions.mint, user, False, True)
        other = client.portal.call(account_sessions.mint, user, False)
        _auth_cookies(client, kept.token, kept.csrf)
        keep_model = f"s1-link-model-{uuid.uuid4()}"
        keep_ws, keep_connection, _grant, _open, keep_browser_ws, keep_browser, keep_fleet = (
            open_live_session(client, keep_worker, keep_model)
        )
        other_model = f"s1-link-other-model-{uuid.uuid4()}"
        try:
            keep_browser_ws.send_json({
                "type": "update_params", "params": {"prompt": "kept"},
            })
            keep_command = keep_ws.receive_json()
            assert keep_command["type"] == "update_session"

            _auth_cookies(client, other.token, other.csrf)
            other_ws, other_connection, _grant, _open, other_browser_ws, other_browser, other_fleet = (
                open_live_session(client, other_worker, other_model)
            )
            other_browser_ws.send_json({
                "type": "update_params", "params": {"prompt": "retired"},
            })
            other_command = other_ws.receive_json()
            assert other_command["type"] == "update_session"

            _auth_cookies(client, kept.token, kept.csrf)
            response = link_through_route(client, "github")
            assert response.status_code == 307
            assert client.get("/api/v1/account").status_code == 200
            assert client.get(
                "/api/v1/account",
                headers={"authorization": f"Bearer {other.token}"},
            ).status_code == 401

            async def command_states():
                async with db.session_factory() as session:
                    rows = (await session.execute(
                        text("SELECT worker_id, command_sequence, state, encrypted_body "
                             "FROM worker_commands WHERE worker_id IN (:keep, :other) "
                             "ORDER BY worker_id, command_sequence"),
                        {"keep": keep_worker, "other": other_worker},
                    )).mappings().all()
                    return {(row["worker_id"], row["command_sequence"]): row for row in rows}

            rows = client.portal.call(command_states)
            kept_row = rows[(keep_worker, keep_command["command_sequence"])]
            retired_row = rows[(other_worker, other_command["command_sequence"])]
            assert kept_row["state"] == "pending"
            assert kept_row["encrypted_body"] is not None
            assert retired_row["state"] == "erased"
            assert retired_row["encrypted_body"] is None
        finally:
            if "other_browser" in locals():
                other_browser.__exit__(None, None, None)
                other_fleet.__exit__(None, None, None)
            keep_browser.__exit__(None, None, None)
            keep_fleet.__exit__(None, None, None)


@pytest.mark.db
def test_due_purge_waits_for_actual_command_preparation(oauth_accounts, monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-purge-prep-{uuid.uuid4()}"
    model_id = f"s1-purge-prep-model-{uuid.uuid4()}"
    entered = asyncio.Event()
    release = asyncio.Event()
    blocker = {}
    original_prepare = command_store.prepare_command_subject

    async def held_prepare(session, command_type, fields, **kwargs):
        if command_type == "update_session":
            blocker["pid"] = await session.scalar(text("SELECT pg_backend_pid()"))
            entered.set()
            await release.wait()
        return await original_prepare(session, command_type, fields, **kwargs)

    with protocol6_client(worker_id) as client:
        user = client.portal.call(make_oauth_user, f"s1-purge-prep-{uuid.uuid4()}@example.test")
        issued = client.portal.call(account_sessions.mint, user, False)
        _auth_cookies(client, issued.token, issued.csrf)
        worker_ws, worker, _grant, _open, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        try:
            monkeypatch.setattr(command_store, "prepare_command_subject", held_prepare)
            browser_ws.send_json({
                "type": "update_params", "params": {"prompt": "held preparation"},
            })
            client.portal.call(asyncio.wait_for, entered.wait(), 5)
            cutoff = datetime.now(timezone.utc)

            async def make_due():
                async with db.session_factory() as session:
                    await session.execute(
                        text("UPDATE users SET state = 'deletion_pending', "
                             "deletion_requested_at = :requested_at WHERE id = :user_id"),
                        {"requested_at": cutoff - timedelta(seconds=1), "user_id": user.id},
                    )
                    await session.commit()

            client.portal.call(make_due)
            purge = client.portal.start_task_soon(deletion._purge, user.id, cutoff)

            async def wait_for_purge_lock():
                for _ in range(250):
                    async with db.session_factory() as session:
                        blocked = await session.scalar(text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity waiter "
                            "WHERE waiter.datname = current_database() "
                            "AND waiter.wait_event_type = 'Lock' "
                            "AND waiter.query ILIKE '%pg_advisory_xact_lock%' "
                            "AND :holder = ANY(pg_blocking_pids(waiter.pid)))"
                        ), {"holder": blocker["pid"]})
                    if blocked:
                        return
                    await asyncio.sleep(0.02)
                raise AssertionError("purge did not wait behind the held preparation")

            client.portal.call(wait_for_purge_lock)
            release.set()
            purge.result(timeout=20)

            async def inspect_purged_commands():
                async with db.session_factory() as session:
                    rows = (await session.execute(
                        text("SELECT state, encrypted_body FROM worker_commands "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation},
                    )).mappings().all()
                    exists = await session.scalar(
                        text("SELECT EXISTS (SELECT 1 FROM users WHERE id = :user_id)"),
                        {"user_id": user.id},
                    )
                    return rows, exists

            rows, exists = client.portal.call(inspect_purged_commands)
            assert not exists
            assert all(row["state"] != "pending" or row["encrypted_body"] is None for row in rows)
            assert all(row["state"] == "erased" or row["encrypted_body"] is None for row in rows)
        finally:
            release.set()
            browser.__exit__(None, None, None)
            fleet.__exit__(None, None, None)
