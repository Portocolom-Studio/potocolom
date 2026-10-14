import asyncio
import os
import threading
import time
import uuid

import asyncpg
import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app import db, jobs, main as main_module, sessions as account_sessions, worker_authority
from app import realtime
from app.account_lock import budget_lock
from app.settings import get_settings
from app.tables import User
from test_fleet_protocol6 import (
    hello,
    manifest,
    protocol6_client,
    request_work_grant,
    root_keys,
    seed_queued_job,
)
from test_totp_flow import accounts
from test_v6_sessions import open_live_session

__all__ = ["accounts"]


async def _hold_account(user_id):
    connection = await asyncpg.connect(os.environ["DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    await connection.execute(
        "SELECT pg_advisory_xact_lock($1)", budget_lock(user_id)
    )
    return connection, transaction


async def _release_account(holder):
    if holder is not None:
        connection, _transaction = holder
        if connection.is_in_transaction():
            await connection.execute("ROLLBACK")
        await connection.close()


def _start_request(call):
    done = threading.Event()
    outcome = {}

    def run():
        try:
            outcome["response"] = call()
        except BaseException as error:
            outcome["error"] = error
        finally:
            done.set()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, done, outcome


async def _wait_event(event, seconds):
    await asyncio.wait_for(event.wait(), seconds)


async def _wait_http(done, seconds):
    return await asyncio.wait_for(asyncio.to_thread(done.wait), seconds)


async def _wait_ended(session_id, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        async with db.session_factory() as session:
            state = await session.scalar(
                text("SELECT state FROM realtime_sessions WHERE id = :id"),
                {"id": session_id},
            )
            attempt = await session.scalar(
                text("SELECT state FROM realtime_session_attempts WHERE session_id = :id"),
                {"id": session_id},
            )
        if state == "ended" and attempt == "ended":
            return
        await asyncio.sleep(0.05)
    raise AssertionError("realtime session and attempt did not end")


@pytest.mark.db
def test_job_cancel_uses_bounded_account_gate(accounts, monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-cancel-gate-{uuid.uuid4()}"
    model_id = f"s1-cancel-gate-model-{uuid.uuid4()}"

    async def park_dispatch():
        return None

    monkeypatch.setattr(jobs, "dispatch_step", park_dispatch)
    with protocol6_client(worker_id) as client:
        async def current_user():
            async with db.session_factory() as session:
                return (await session.execute(
                    select(User).where(User.id == db.local_user_id)
                )).scalar_one()

        user = client.portal.call(current_user)
        issued = client.portal.call(account_sessions.mint, user, False)
        cookie, csrf = account_sessions.cookie_names(get_settings().public_url)
        client.cookies.set(cookie, issued.token)
        client.cookies.set(csrf, issued.csrf)
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            incarnation = uuid.uuid4()
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)
            job_id = client.portal.call(seed_queued_job, model_id)
            assert client.portal.call(jobs.dispatch, job_id) is True
            dispatch = worker.receive_json()
            assert dispatch["type"] == "dispatch_job"

            async def read_state():
                async with db.session_factory() as session:
                    job = (await session.execute(
                        text("SELECT state FROM jobs WHERE id = :id"), {"id": job_id}
                    )).scalar_one()
                    rows = (await session.execute(
                        text("SELECT kind, command_sequence, state, encrypted_body, key_version "
                             "FROM worker_commands WHERE worker_id = :worker_id "
                             "AND incarnation = :incarnation ORDER BY command_sequence"),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )).mappings().all()
                    return job, [dict(row) for row in rows]

            before = client.portal.call(read_state)
            holder = client.portal.call(_hold_account, db.local_user_id)
            started = time.monotonic()
            request = _start_request(lambda: client.post(
                f"/api/v1/generations/{job_id}/cancel",
                headers={"origin": get_settings().public_url,
                         "x-csrf-token": issued.csrf},
            ))
            timed_out = False
            try:
                thread, done, outcome = request
                try:
                    client.portal.call(_wait_http, done, 8)
                except TimeoutError:
                    timed_out = True
                response = outcome.get("response") if done.is_set() else None
                assert client.portal.call(read_state) == before
            finally:
                client.portal.call(_release_account, holder)
                thread.join(8)
                assert not thread.is_alive(), "cancel request thread did not finish after gate release"
            assert not timed_out, "cancel request waited beyond the 8 second outer bound"
            assert time.monotonic() - started <= 6
            if "error" in outcome:
                raise outcome["error"]
            assert response.status_code == 503
            retry = client.post(
                f"/api/v1/generations/{job_id}/cancel",
                headers={"origin": get_settings().public_url,
                         "x-csrf-token": issued.csrf},
            )
            assert retry.status_code == 204
            final_job, final_rows = client.portal.call(read_state)
            assert final_job == "cancelled"
            assert len(final_rows) == 1
            assert final_rows[0]["kind"] == "dispatch_job"
            assert final_rows[0]["command_sequence"] == dispatch["command_sequence"]
            assert final_rows[0]["state"] == "erased"
            assert final_rows[0]["encrypted_body"] is None
            assert final_rows[0]["key_version"] is None


@pytest.mark.db
def test_revoked_realtime_cleanup_uses_bounded_account_gate(accounts, monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-revoke-gate-{uuid.uuid4()}"
    model_id = f"s1-revoke-gate-model-{uuid.uuid4()}"
    with protocol6_client(worker_id) as client:
        async def find_user():
            async with db.session_factory() as session:
                return (await session.execute(
                    select(User).where(User.id == db.local_user_id)
                )).scalar_one()

        user = client.portal.call(find_user)
        issued = client.portal.call(account_sessions.mint, user, False)
        cookie, csrf = account_sessions.cookie_names(get_settings().public_url)
        client.cookies.set(cookie, issued.token)
        client.cookies.set(csrf, issued.csrf)
        worker_ws, worker, _grant, open_command, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        resolved = client.portal.call(account_sessions.resolve, issued.token)
        auth_session_id = resolved.session.id
        cleanup_started = asyncio.Event()
        holder = []
        original_end = worker_authority.end_realtime_session

        async def held_end(session_id, generation, browser_owner_id):
            held = await _hold_account(db.local_user_id)
            holder.append(held)
            cleanup_started.set()
            return await original_end(session_id, generation, browser_owner_id)

        monkeypatch.setattr(worker_authority, "end_realtime_session", held_end)
        started = time.monotonic()
        request = _start_request(lambda: client.post(
            "/api/v1/auth/logout",
            headers={"origin": get_settings().public_url,
                     "x-csrf-token": issued.csrf},
        ))
        thread, done, outcome = request
        timed_out = False
        try:
            client.portal.call(_wait_event, cleanup_started, 8)
            try:
                client.portal.call(
                    _wait_http, done, max(0, 8 - (time.monotonic() - started))
                )
            except TimeoutError:
                timed_out = True
            response = outcome.get("response") if done.is_set() else None

            async def inspect_revocation():
                async with db.session_factory() as session:
                    revoked = await session.scalar(
                        text("SELECT revoked_at IS NOT NULL FROM sessions WHERE id = :id"),
                        {"id": auth_session_id},
                    )
                    bindings = await session.scalar(
                        text("SELECT count(*) FROM worker_commands WHERE account_user_id = :user_id "
                             "AND kind IN ('open_session', 'update_session', 'close_session') "
                             "AND state = 'pending' AND encrypted_body IS NOT NULL"),
                        {"user_id": db.local_user_id},
                    )
                    return revoked, bindings

            revoked, bindings = client.portal.call(inspect_revocation)
        finally:
            for held in holder:
                client.portal.call(_release_account, held)
            thread.join(8)
            assert not thread.is_alive(), "logout request thread did not finish after gate release"
            browser.__exit__(None, None, None)
            fleet.__exit__(None, None, None)
        assert revoked is True
        assert bindings == 0
        assert not timed_out, "logout cleanup waited beyond the 8 second outer bound"
        assert time.monotonic() - started <= 6
        if "error" in outcome:
            raise outcome["error"]
        assert response.status_code == 204


@pytest.mark.db
def test_refused_realtime_end_completes_after_the_gate_frees(accounts, monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-revoke-owned-end-{uuid.uuid4()}"
    model_id = f"s1-revoke-owned-end-model-{uuid.uuid4()}"
    with protocol6_client(worker_id) as client:
        async def find_user():
            async with db.session_factory() as session:
                return (await session.execute(
                    select(User).where(User.id == db.local_user_id)
                )).scalar_one()

        user = client.portal.call(find_user)
        issued = client.portal.call(account_sessions.mint, user, False)
        cookie, csrf = account_sessions.cookie_names(get_settings().public_url)
        client.cookies.set(cookie, issued.token)
        client.cookies.set(csrf, issued.csrf)
        worker_ws, worker, _grant, open_command, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        resolved = client.portal.call(account_sessions.resolve, issued.token)
        auth_session_id = resolved.session.id
        realtime_session_id = uuid.UUID(open_command["session_id"])
        end_entered = asyncio.Event()
        holders = []
        end_finished = []
        fence_calls = []
        original_end = worker_authority.end_realtime_session
        original_fence = realtime.close_fenced_worker

        async def held_end(session_id, generation, browser_owner_id):
            holder = await _hold_account(db.local_user_id)
            holders.append(holder)
            end_entered.set()
            result = await original_end(session_id, generation, browser_owner_id)
            end_finished.append(result)
            return result

        async def observed_fence(target_id, incarnation):
            fence_calls.append((target_id, incarnation))
            return await original_fence(target_id, incarnation)

        monkeypatch.setattr(worker_authority, "end_realtime_session", held_end)
        monkeypatch.setattr(realtime, "close_fenced_worker", observed_fence)
        started = time.monotonic()
        thread, done, outcome = _start_request(lambda: client.post(
            "/api/v1/auth/logout",
            headers={"origin": get_settings().public_url,
                     "x-csrf-token": issued.csrf},
        ))
        try:
            client.portal.call(_wait_event, end_entered, 8)
            client.portal.call(_wait_http, done, 6)
            assert outcome.get("response") is not None
            assert outcome["response"].status_code == 204
            assert time.monotonic() - started < 6
            for holder in holders:
                client.portal.call(_release_account, holder)
            client.portal.call(_wait_ended, realtime_session_id, 8)
            async def revoked_state():
                async with db.session_factory() as session:
                    return await session.scalar(
                        text("SELECT revoked_at IS NOT NULL FROM sessions WHERE id = :id"),
                        {"id": auth_session_id},
                    )

            assert client.portal.call(revoked_state) is True
            assert fence_calls == [(worker_id, worker.incarnation)]
            assert end_finished == [True]
            assert worker_id not in realtime.workers
        finally:
            for holder in holders:
                client.portal.call(_release_account, holder)
            thread.join(8)
            assert not thread.is_alive(), "logout request did not finish"
            browser.__exit__(None, None, None)
            fleet.__exit__(None, None, None)
        if "error" in outcome:
            raise outcome["error"]


@pytest.mark.db
def test_shutdown_joins_a_pending_realtime_end(accounts, monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-revoke-shutdown-end-{uuid.uuid4()}"
    model_id = f"s1-revoke-shutdown-end-model-{uuid.uuid4()}"
    events = []
    shutdown_drain_started = threading.Event()
    with protocol6_client(worker_id) as client:
        async def find_user():
            async with db.session_factory() as session:
                return (await session.execute(
                    select(User).where(User.id == db.local_user_id)
                )).scalar_one()

        user = client.portal.call(find_user)
        issued = client.portal.call(account_sessions.mint, user, False)
        cookie, csrf = account_sessions.cookie_names(get_settings().public_url)
        client.cookies.set(cookie, issued.token)
        client.cookies.set(csrf, issued.csrf)
        worker_ws, worker, _grant, open_command, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        realtime_session_id = uuid.UUID(open_command["session_id"])
        end_entered = asyncio.Event()
        holders = []
        original_end = worker_authority.end_realtime_session
        original_drain = main_module.drain_worker_deliveries

        async def held_end(session_id, generation, browser_owner_id):
            assert session_id == realtime_session_id
            holder = await _hold_account(db.local_user_id)
            holders.append(holder)
            end_entered.set()
            try:
                result = await original_end(session_id, generation, browser_owner_id)
            except HTTPException:
                events.append("end-refused")
                raise
            async with db.session_factory() as session:
                state = await session.scalar(
                    text("SELECT state FROM realtime_sessions WHERE id = :id"),
                    {"id": session_id},
                )
            assert state == "ended"
            events.append("end-returned")
            return result

        async def observed_drain():
            shutdown_drain_started.set()
            return await original_drain()

        monkeypatch.setattr(worker_authority, "end_realtime_session", held_end)
        monkeypatch.setattr(main_module, "drain_worker_deliveries", observed_drain)
        thread, done, outcome = _start_request(lambda: client.post(
            "/api/v1/auth/logout",
            headers={"origin": get_settings().public_url,
                     "x-csrf-token": issued.csrf},
        ))
        releaser = threading.Thread(target=lambda: (
            shutdown_drain_started.wait(8),
            time.sleep(0.1),
            client.portal.call(_release_account, holders[0]) if holders else None,
        ))
        try:
            client.portal.call(_wait_event, end_entered, 8)
            client.portal.call(_wait_http, done, 6)
            assert outcome["response"].status_code == 204
            releaser.start()
            thread.join(8)
            assert not thread.is_alive()
        finally:
            if releaser.ident is None:
                releaser.start()
            if thread.is_alive():
                thread.join(8)
            try:
                browser.__exit__(None, None, None)
            finally:
                fleet.__exit__(None, None, None)
        if "error" in outcome:
            raise outcome["error"]
    releaser.join(8)
    assert not releaser.is_alive()
    # held_end queries the session row after the real end returns, which needs
    # the live engine: shutdown let the pending end finish before disposal.
    assert "end-returned" in events
