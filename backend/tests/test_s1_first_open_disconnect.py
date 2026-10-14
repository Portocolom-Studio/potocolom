import asyncio
import uuid
from contextlib import suppress

import pytest
from sqlalchemy import text

from app import db, realtime, sessions as account_sessions, worker_authority
from app.settings import get_settings
from test_fleet_protocol6 import (
    hello,
    manifest,
    protocol6_client,
    request_work_grant,
    root_keys,
)
from test_totp_flow import _make as make_account_user, accounts

__all__ = ["accounts"]


async def _start(call):
    return asyncio.create_task(asyncio.to_thread(call))


async def _join(task, seconds=8):
    return await asyncio.wait_for(asyncio.shield(task), seconds)


async def _wait(event, seconds=8):
    await asyncio.wait_for(event.wait(), seconds)


async def _settle(event, seconds=1):
    # Unrepaired cleanup reaches the connection lock before the claim commits;
    # repaired cleanup waits for the claim first, so this point never comes.
    with suppress(TimeoutError):
        await asyncio.wait_for(event.wait(), seconds)


async def _inspect(session_id, worker_id, incarnation):
    async with db.session_factory() as session:
        attempt = (await session.execute(
            text("SELECT state, attempt_id, range_id FROM realtime_session_attempts "
                 "WHERE session_id = :session_id"), {"session_id": session_id}
        )).mappings().one()
        command = (await session.execute(
            text("SELECT state, encrypted_body, key_version FROM worker_commands "
                 "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                 "AND subject_id = :session_id"),
            {"worker_id": worker_id, "incarnation": incarnation,
             "session_id": session_id},
        )).mappings().one()
        connection = await session.scalar(
            text("SELECT lifecycle FROM worker_connections WHERE worker_id = :worker_id "
                 "AND incarnation = :incarnation"),
            {"worker_id": worker_id, "incarnation": incarnation},
        )
        return dict(attempt), dict(command), connection


@pytest.mark.db
def test_disconnect_erases_first_open_committed_after_selection(accounts, monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-first-open-close-{uuid.uuid4()}"
    model_id = f"s1-first-open-close-model-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    claim_paused = asyncio.Event()
    release_claim = asyncio.Event()
    close_waiting = asyncio.Event()
    disconnect_started = asyncio.Event()
    inserted = {}
    sent_after_disconnect = []
    original_execute = db.OwnedSession.execute

    with protocol6_client(worker_id) as client:
        user = client.portal.call(make_account_user, f"first-open-{uuid.uuid4()}@example.test")
        issued = client.portal.call(account_sessions.mint, user, False)
        cookie, csrf = account_sessions.cookie_names(get_settings().public_url)
        client.cookies.set(cookie, issued.token)
        client.cookies.set(csrf, issued.csrf)
        client.portal.call(worker_authority.acquire_scheduler_lease)
        fleet = client.websocket_connect("/api/v1/fleet")
        worker_ws = fleet.__enter__()
        worker_ws.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
        worker_ws.receive_json()
        request_work_grant(worker_ws, worker_id, incarnation)
        worker = realtime.workers[worker_id]
        original_send = worker.ws.send_text

        async def observed_send(data):
            if disconnect_started.is_set():
                sent_after_disconnect.append(data)
            await original_send(data)

        worker.ws.send_text = observed_send

        async def held_execute(session, statement, *args, **kwargs):
            sql = " ".join(str(statement).split())
            if "UPDATE worker_connections SET next_command_sequence = next_command_sequence + 1" in sql:
                async def record_inserted_rows():
                    result = await original_execute(
                        session,
                        text("SELECT session.id, attempt.attempt_id, attempt.range_id, "
                             "command.command_sequence, command.state, command.encrypted_body, "
                             "command.key_version FROM realtime_sessions session "
                             "JOIN realtime_session_attempts attempt ON attempt.session_id = session.id "
                             "JOIN worker_commands command ON command.subject_id = session.id "
                             "WHERE attempt.worker_id = :worker_id AND attempt.incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )
                    inserted.update(dict(result.mappings().one()))

                await record_inserted_rows()
                claim_paused.set()
                await release_claim.wait()
            if "UPDATE worker_connections SET lifecycle = 'ended'" in sql:
                close_waiting.set()
            return await original_execute(session, statement, *args, **kwargs)

        monkeypatch.setattr(db.OwnedSession, "execute", held_execute)
        browser = client.websocket_connect("/api/v1/realtime")
        browser_ws = browser.__enter__()
        browser_ws.send_json({
            "type": "open", "model_id": model_id,
            "params": {"prompt": "first"}, "frame_header": 2,
        })
        try:
            client.portal.call(_wait, claim_paused)
        except BaseException:
            release_claim.set()
            browser.__exit__(None, None, None)
            fleet.__exit__(None, None, None)
            raise
        assert inserted["state"] == "pending"
        assert inserted["encrypted_body"] is not None
        assert inserted["key_version"] is not None

        disconnect_started.set()
        closing = client.portal.call(_start, lambda: fleet.__exit__(None, None, None))
        try:
            client.portal.call(_settle, close_waiting)
            release_claim.set()
            client.portal.call(_join, closing)
            attempt, command, lifecycle = client.portal.call(
                _inspect, inserted["id"], worker_id, incarnation
            )
            assert (
                attempt["state"], command["state"], command["encrypted_body"],
                command["key_version"], lifecycle, sent_after_disconnect,
            ) == ("ended", "erased", None, None, "ended", [])
        finally:
            release_claim.set()
            if not closing.done():
                client.portal.call(_join, closing)
            browser.__exit__(None, None, None)


@pytest.mark.db
def test_disconnect_and_delivery_registration_do_not_deadlock_first_open(accounts, monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-first-open-register-{uuid.uuid4()}"
    model_id = f"s1-first-open-register-model-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    claim_paused = asyncio.Event()
    release_claim = asyncio.Event()
    close_waiting = asyncio.Event()
    connection_locked = asyncio.Event()
    release_close = asyncio.Event()
    registration_locked_attempt = asyncio.Event()
    disconnect_started = asyncio.Event()
    inserted = {}
    sent_after_disconnect = []
    original_execute = db.OwnedSession.execute

    with protocol6_client(worker_id) as client:
        user = client.portal.call(make_account_user, f"first-open-register-{uuid.uuid4()}@example.test")
        issued = client.portal.call(account_sessions.mint, user, False)
        cookie, csrf = account_sessions.cookie_names(get_settings().public_url)
        client.cookies.set(cookie, issued.token)
        client.cookies.set(csrf, issued.csrf)
        client.portal.call(worker_authority.acquire_scheduler_lease)
        fleet = client.websocket_connect("/api/v1/fleet")
        worker_ws = fleet.__enter__()
        worker_ws.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
        worker_ws.receive_json()
        request_work_grant(worker_ws, worker_id, incarnation)
        worker = realtime.workers[worker_id]
        original_send = worker.ws.send_text

        async def observed_send(data):
            if disconnect_started.is_set():
                sent_after_disconnect.append(data)
            await original_send(data)

        worker.ws.send_text = observed_send

        async def held_execute(session, statement, *args, **kwargs):
            sql = " ".join(str(statement).split())
            if "UPDATE worker_connections SET next_command_sequence = next_command_sequence + 1" in sql:
                result = await original_execute(
                    session,
                    text("SELECT session.id, attempt.attempt_id, attempt.range_id, "
                         "command.command_sequence, command.state, command.encrypted_body, "
                         "command.key_version FROM realtime_sessions session "
                         "JOIN realtime_session_attempts attempt ON attempt.session_id = session.id "
                         "JOIN worker_commands command ON command.subject_id = session.id "
                         "WHERE attempt.worker_id = :worker_id AND attempt.incarnation = :incarnation"),
                    {"worker_id": worker_id, "incarnation": incarnation},
                )
                inserted.update(dict(result.mappings().one()))
                claim_paused.set()
                await release_claim.wait()
            if "UPDATE worker_connections SET lifecycle = 'ended'" in sql:
                close_waiting.set()
                result = await original_execute(session, statement, *args, **kwargs)
                connection_locked.set()
                await release_close.wait()
                return result
            result = await original_execute(session, statement, *args, **kwargs)
            if ("FROM realtime_session_attempts WHERE session_id = :subject_id" in sql
                    and "FOR UPDATE" in sql):
                registration_locked_attempt.set()
            return result

        monkeypatch.setattr(db.OwnedSession, "execute", held_execute)
        browser = client.websocket_connect("/api/v1/realtime")
        browser_ws = browser.__enter__()
        browser_ws.send_json({
            "type": "open", "model_id": model_id,
            "params": {"prompt": "first"}, "frame_header": 2,
        })
        try:
            client.portal.call(_wait, claim_paused)
        except BaseException:
            release_claim.set()
            browser.__exit__(None, None, None)
            fleet.__exit__(None, None, None)
            raise
        disconnect_started.set()
        closing = client.portal.call(_start, lambda: fleet.__exit__(None, None, None))
        failure = None
        try:
            client.portal.call(_settle, close_waiting)
            release_claim.set()
            client.portal.call(_wait, connection_locked)
            client.portal.call(_wait, registration_locked_attempt)
            release_close.set()
            try:
                client.portal.call(_join, closing)
            except Exception as error:
                failure = error
            attempt, command, lifecycle = client.portal.call(
                _inspect, inserted["id"], worker_id, incarnation
            )
            assert failure is None, f"fleet cleanup failed during delivery registration: {failure}"
            assert (
                attempt["state"], command["state"], command["encrypted_body"],
                command["key_version"], lifecycle, sent_after_disconnect,
            ) == ("ended", "erased", None, None, "ended", [])
        finally:
            release_claim.set()
            release_close.set()
            if not closing.done():
                try:
                    client.portal.call(_join, closing)
                except Exception:
                    pass
            browser.__exit__(None, None, None)
