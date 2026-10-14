"""PostgreSQL transaction races accepted for S1 issue 717."""

import asyncio
import json
import time
import uuid
from contextlib import ExitStack

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import command_store, db, jobs, realtime, sessions as account_sessions
from test_fleet_protocol6 import (
    hello, manifest, protocol6_client, request_work_grant, root_keys,
    seed_queued_job,
)
from test_s1_authority_races import _bound_update_session, _mint_local_binding
from test_totp_flow import accounts
from test_v6_sessions import command_ack

__all__ = ["accounts"]


def _wait_event(client, event: asyncio.Event, timeout: float = 10) -> None:
    async def wait():
        await asyncio.wait_for(event.wait(), timeout)

    client.portal.call(wait)


def _finish_tasks(client, tasks, release_events=()) -> None:
    for event in release_events:
        client.portal.call(event.set)
    for task in tasks:
        task.result(timeout=20)


async def _read_command(worker_id, incarnation, sequence):
    async with db.session_factory() as session:
        return (await session.execute(text(
            "SELECT state, encrypted_body, ack_body FROM worker_commands "
            "WHERE worker_id = :worker_id AND incarnation = :incarnation "
            "AND command_sequence = :sequence"
        ), {"worker_id": worker_id, "incarnation": incarnation,
            "sequence": sequence})).mappings().one_or_none()


async def _worker_floor(worker_id, incarnation):
    async with db.session_factory() as session:
        return await session.scalar(text(
            "SELECT acknowledged_floor FROM worker_connections "
            "WHERE worker_id = :worker_id AND incarnation = :incarnation"
        ), {"worker_id": worker_id, "incarnation": incarnation})


async def _realtime_state(session_id):
    async with db.session_factory() as session:
        return (await session.execute(text(
            "SELECT state, params_revision, effective_params FROM realtime_sessions "
            "WHERE id = :id"
        ), {"id": session_id})).mappings().one()


async def _dispatched_job(job_id):
    """The durable rows a completed jobs.dispatch leaves behind: the running
    job, its pending dispatch command and the attempt that owns it."""
    async with db.session_factory() as session:
        job = (await session.execute(text(
            "SELECT state, model_id, params, attempt, dispatched_at, "
            "current_dispatch_sequence FROM jobs WHERE id = :id"
        ), {"id": job_id})).mappings().one()
        command = (await session.execute(text(
            "SELECT kind, subject_id, subject_sequence, state, body_hash, "
            "encrypted_body IS NOT NULL AS has_body FROM worker_commands "
            "WHERE kind = 'dispatch_job' AND subject_id = :id "
            "ORDER BY command_sequence DESC LIMIT 1"
        ), {"id": job_id})).mappings().one()
        attempt = (await session.execute(text(
            "SELECT worker_id, incarnation, dispatch_sequence, state "
            "FROM job_attempts WHERE job_id = :id "
            "ORDER BY dispatch_sequence DESC LIMIT 1"
        ), {"id": job_id})).mappings().one()
    return {"job": dict(job), "command": dict(command), "attempt": dict(attempt)}


async def _acknowledge(worker, ack):
    return await command_store.acknowledge(
        ack, expected_worker_id=worker.id,
        expected_incarnation=worker.incarnation,
        transport_owner_id=worker.transport_owner_id,
    )


async def _wait_for_blocker(holder_pid: int, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with db.session_factory() as session:
            count = await session.scalar(text(
                "SELECT count(*) FROM pg_stat_activity a "
                "WHERE a.datname = current_database() AND a.pid <> pg_backend_pid() "
                "AND a.wait_event_type = 'Lock' "
                "AND :holder = ANY(pg_blocking_pids(a.pid)) "
                "AND a.query ILIKE '%pg_advisory_xact_lock%'"
            ), {"holder": holder_pid})
        if count:
            return True
        await asyncio.sleep(0.02)
    return False


def _setup(client, worker_id, model_id):
    return _bound_update_session(client, worker_id, model_id)


@pytest.mark.db
def test_ack_retry_after_lost_commit_response_is_exact_and_durable(accounts, monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-ack-lost-{uuid.uuid4()}"
    model_id = f"s1-ack-model-{uuid.uuid4()}"
    with protocol6_client(worker_id) as client, ExitStack() as stack:
        issued, auth_id, worker_ws, worker, update, _, browser_ws, cleanup = (
            _setup(client, worker_id, model_id)
        )
        stack.callback(cleanup.close)
        ack = command_ack(update)
        original = command_store.acknowledge
        first = {}

        async def lose_response_after_commit(*args, **kwargs):
            result = await original(*args, **kwargs)
            if not first.get("lost"):
                first["result"] = result
                first["lost"] = True
                raise ConnectionError("injected after committed ACK")
            return result

        monkeypatch.setattr(command_store, "acknowledge", lose_response_after_commit)
        with pytest.raises(ConnectionError, match="after committed ACK"):
            client.portal.call(_acknowledge, worker, ack)
        retry = client.portal.call(
            _acknowledge, worker, ack
        )
        state = client.portal.call(_realtime_state, uuid.UUID(update["session_id"]))
        assert first["result"].recorded is True
        assert first["result"].publication["params"] == update["params"]
        assert retry.recorded is False and retry.obsolete is True
        assert retry.publication is None
        assert client.portal.call(_worker_floor, worker.id, worker.incarnation) == int(
            update["command_sequence"]
        )
        assert state["params_revision"] == update["params_revision"]
        assert state["effective_params"] == update["params"]
        assert client.portal.call(_read_command, worker.id, worker.incarnation,
                                  update["command_sequence"]) is None


@pytest.mark.db
def test_erasure_before_real_delivery_registration_has_no_handoff_or_socket_io(
    accounts, monkeypatch,
):
    root_keys(monkeypatch)
    worker_id = f"s1-erasure-first-{uuid.uuid4()}"
    model_id = f"s1-erasure-model-{uuid.uuid4()}"
    with protocol6_client(worker_id) as client, ExitStack() as stack:
        _, auth_id, worker_ws, worker, update, _, browser_ws, cleanup = (
            _setup(client, worker_id, model_id)
        )
        stack.callback(cleanup.close)
        marker_before = realtime._sent_command_sequences.get((worker.id, worker.incarnation))
        entered = asyncio.Event()
        release = asyncio.Event()
        registered = []
        sends = []
        original_read = command_store.read_exact_pending
        original_register = command_store.register_delivery
        original_send = worker.ws.send_text

        async def held_real_read(*args, **kwargs):
            command = await original_read(*args, **kwargs)
            entered.set()
            await release.wait()
            return command

        async def observed_real_register(*args, **kwargs):
            result = await original_register(*args, **kwargs)
            registered.append(result)
            return result

        async def observed_send(*args, **kwargs):
            sends.append(args[0] if args else None)
            return await original_send(*args, **kwargs)

        monkeypatch.setattr(command_store, "read_exact_pending", held_real_read)
        monkeypatch.setattr(command_store, "register_delivery", observed_real_register)
        monkeypatch.setattr(worker.ws, "send_text", observed_send)
        delivery = None
        revocation = None
        try:
            delivery = client.portal.start_task_soon(
                realtime._deliver_pending_worker_command, worker
            )
            _wait_event(client, entered)
            revocation = client.portal.start_task_soon(account_sessions.revoke, auth_id)
            revocation.result(timeout=20)
            client.portal.call(release.set)
            assert delivery.result(timeout=20) is None
            row = client.portal.call(_read_command, worker.id, worker.incarnation,
                                     update["command_sequence"])
            assert row["state"] == "erased" and row["encrypted_body"] is None
            assert registered == [False] or registered == []
            assert sends == []
            assert worker.delivery_registered is False
            assert worker.delivery_send_task is None
            assert client.portal.call(_worker_floor, worker.id, worker.incarnation) == 1
            assert realtime._sent_command_sequences.get((worker.id, worker.incarnation)) == (
                marker_before
            )
            state = client.portal.call(_realtime_state, uuid.UUID(update["session_id"]))
            assert state["params_revision"] == 1
        finally:
            _finish_tasks(client, [task for task in (delivery, revocation) if task], [release])


@pytest.mark.db
def test_real_registration_keeps_held_send_owned_until_fenced_send_returns(
    accounts, monkeypatch,
):
    root_keys(monkeypatch)
    worker_id = f"s1-handoff-first-{uuid.uuid4()}"
    model_id = f"s1-handoff-model-{uuid.uuid4()}"
    with protocol6_client(worker_id) as client, ExitStack() as stack:
        _, auth_id, worker_ws, worker, update, _, browser_ws, cleanup = (
            _setup(client, worker_id, model_id)
        )
        stack.callback(cleanup.close)
        registered = asyncio.Event()
        send_started = asyncio.Event()
        release_send = asyncio.Event()
        close_finished = asyncio.Event()
        registration_results = []
        send_messages = []
        original_register = command_store.register_delivery
        original_send = worker.ws.send_text
        original_close = account_sessions.close_sockets

        async def observed_register(*args, **kwargs):
            result = await original_register(*args, **kwargs)
            registration_results.append(result)
            if result:
                registered.set()
            return result

        async def held_real_send(*args, **kwargs):
            result = await original_send(*args, **kwargs)
            send_messages.append(args[0] if args else None)
            send_started.set()
            await release_send.wait()
            return result

        async def observed_close(*args, **kwargs):
            try:
                return await original_close(*args, **kwargs)
            finally:
                close_finished.set()

        monkeypatch.setattr(command_store, "register_delivery", observed_register)
        monkeypatch.setattr(worker.ws, "send_text", held_real_send)
        monkeypatch.setattr(account_sessions, "close_sockets", observed_close)
        delivery = revoke = None
        try:
            client.portal.call(asyncio.sleep, 1.05)
            delivery = client.portal.start_task_soon(
                realtime._deliver_pending_worker_command, worker
            )
            _wait_event(client, send_started)
            assert client.portal.call(_read_command, worker.id, worker.incarnation,
                                      update["command_sequence"])["state"] == "pending"
            assert registration_results == [True] and registered.is_set()
            owner = worker.delivery_task
            send_task = worker.delivery_send_task
            assert owner is not None and send_task is not None
            assert realtime._worker_delivery_owners.get(owner) is worker
            assert json.loads(send_messages[0]) == update
            revoke = client.portal.start_task_soon(account_sessions.revoke, auth_id)
            _wait_event(client, close_finished)
            assert not delivery.done()
            assert worker.delivery_task is owner
            assert worker.delivery_send_task is send_task and not send_task.done()
            assert worker.delivery_admission_open is False
            assert len(send_messages) == 1
            client.portal.call(release_send.set)
            delivery.result(timeout=20)
            revoke.result(timeout=20)
            row = client.portal.call(_read_command, worker.id, worker.incarnation,
                                     update["command_sequence"])
            assert row["state"] == "erased" and row["encrypted_body"] is None
            assert worker.delivery_task is None and worker.delivery_send_task is None
            assert registration_results == [True]
            assert len(send_messages) == 1
        finally:
            _finish_tasks(client, [task for task in (delivery, revoke) if task],
                          [release_send])


@pytest.mark.db
def test_revoke_erasure_and_auth_change_are_invisible_until_same_commit(
    accounts, monkeypatch,
):
    root_keys(monkeypatch)
    worker_id = f"s1-atomic-erasure-{uuid.uuid4()}"
    model_id = f"s1-atomic-model-{uuid.uuid4()}"
    other_model_id = f"s1-atomic-job-model-{uuid.uuid4()}"
    with protocol6_client(worker_id) as client, ExitStack() as stack:
        _, auth_id, worker_ws, worker, update, _, browser_ws, cleanup = (
            _setup(client, worker_id, model_id)
        )
        stack.callback(cleanup.close)
        _, retained_id = client.portal.call(_mint_local_binding())
        async def park_dispatch():
            return None

        monkeypatch.setattr(jobs, "dispatch_step", park_dispatch)
        other_worker_id = f"s1-atomic-job-worker-{uuid.uuid4()}"
        other_incarnation = uuid.uuid4()
        other_fleet = client.websocket_connect("/api/v1/fleet")
        other_ws = stack.enter_context(other_fleet)
        other_ws.send_json(hello(
            other_worker_id, [manifest(other_model_id)], incarnation=other_incarnation
        ))
        other_ws.receive_json()
        request_work_grant(other_ws, other_worker_id, other_incarnation)
        job_id = client.portal.call(seed_queued_job, other_model_id)
        assert client.portal.call(jobs.dispatch, job_id) is True
        dispatched = other_ws.receive_json()
        assert dispatched["type"] == "dispatch_job"
        assert dispatched["job_id"] == str(job_id)
        assert dispatched["model_id"] == other_model_id
        assert dispatched["params"] == {"prompt": "queued"}
        preserved = client.portal.call(_dispatched_job, job_id)
        job_row = preserved["job"]
        command_row = preserved["command"]
        attempt_row = preserved["attempt"]
        assert job_row["state"] == "running"
        assert job_row["model_id"] == other_model_id
        assert job_row["params"] == {"prompt": "queued"}
        assert job_row["current_dispatch_sequence"] == dispatched["dispatch_sequence"]
        assert command_row["kind"] == "dispatch_job"
        assert str(command_row["subject_id"]) == str(job_id)
        assert command_row["subject_sequence"] == dispatched["dispatch_sequence"]
        assert command_row["state"] == "pending"
        assert command_row["has_body"] is True
        assert command_row["body_hash"] == dispatched["body_hash"]
        assert str(attempt_row["worker_id"]) == other_worker_id
        assert str(attempt_row["incarnation"]) == str(other_incarnation)
        assert attempt_row["dispatch_sequence"] == dispatched["dispatch_sequence"]
        entered = asyncio.Event()
        release = asyncio.Event()
        original_execute = AsyncSession.execute

        async def pause_after_revoke_write(session, statement, *args, **kwargs):
            result = await original_execute(session, statement, *args, **kwargs)
            if "UPDATE sessions SET revoked_at" in str(statement):
                entered.set()
                await release.wait()
            return result

        monkeypatch.setattr(AsyncSession, "execute", pause_after_revoke_write)
        revoke = None
        try:
            revoke = client.portal.start_task_soon(account_sessions.revoke, auth_id)
            _wait_event(client, entered)
            before = client.portal.call(_read_command, worker.id, worker.incarnation,
                                       update["command_sequence"])
            async def auth_revoked():
                async with db.session_factory() as session:
                    return await session.scalar(text(
                        "SELECT revoked_at IS NOT NULL FROM sessions WHERE id = :id"
                    ), {"id": auth_id})

            assert client.portal.call(auth_revoked) is False
            assert before["state"] == "pending" and before["encrypted_body"] is not None
            client.portal.call(release.set)
            revoke.result(timeout=20)
            after = client.portal.call(_read_command, worker.id, worker.incarnation,
                                      update["command_sequence"])
            assert client.portal.call(auth_revoked) is True
            assert after["state"] == "erased" and after["encrypted_body"] is None
            assert client.portal.call(_worker_floor, worker.id, worker.incarnation) == 1
            async def preserved_rows():
                async with db.session_factory() as session:
                    retained_revoked = await session.scalar(text(
                        "SELECT revoked_at IS NOT NULL FROM sessions WHERE id = :id"
                    ), {"id": retained_id})
                    job_state = await session.scalar(text(
                        "SELECT state FROM jobs WHERE id = :id"
                    ), {"id": job_id})
                    return retained_revoked, job_state

            assert client.portal.call(preserved_rows) == (False, "running")
            assert client.portal.call(_dispatched_job, job_id) == preserved
        finally:
            _finish_tasks(client, [revoke] if revoke else [], [release])


@pytest.mark.db
def test_ack_commit_wins_before_erasure_and_revoke_preserves_ack_outcome(
    accounts, monkeypatch,
):
    root_keys(monkeypatch)
    worker_id = f"s1-ack-first-{uuid.uuid4()}"
    model_id = f"s1-ack-first-model-{uuid.uuid4()}"
    with protocol6_client(worker_id) as client, ExitStack() as stack:
        _, auth_id, worker_ws, worker, update, _, browser_ws, cleanup = (
            _setup(client, worker_id, model_id)
        )
        stack.callback(cleanup.close)
        ack = command_ack(update)
        entered = asyncio.Event()
        release = asyncio.Event()
        holder_pid = {}
        original_execute = AsyncSession.execute

        async def pause_ack_before_commit(session, statement, *args, **kwargs):
            result = await original_execute(session, statement, *args, **kwargs)
            sql = str(statement)
            if ("UPDATE worker_commands SET state = 'acknowledged'" in sql
                    and not entered.is_set()):
                holder_pid["pid"] = await session.scalar(text("SELECT pg_backend_pid()"))
                entered.set()
                await release.wait()
            return result

        monkeypatch.setattr(AsyncSession, "execute", pause_ack_before_commit)
        ack_task = revoke_task = None
        try:
            ack_task = client.portal.start_task_soon(_acknowledge, worker, ack)
            _wait_event(client, entered)
            revoke_task = client.portal.start_task_soon(account_sessions.revoke, auth_id)
            assert client.portal.call(_wait_for_blocker, holder_pid["pid"])
            client.portal.call(release.set)
            result = ack_task.result(timeout=20)
            revoke_task.result(timeout=20)
            assert result.recorded is True and result.publication["params"] == update["params"]
            assert client.portal.call(_worker_floor, worker.id, worker.incarnation) == int(
                update["command_sequence"]
            )
            assert client.portal.call(_read_command, worker.id, worker.incarnation,
                                      update["command_sequence"]) is None
            state = client.portal.call(_realtime_state, uuid.UUID(update["session_id"]))
            assert state["params_revision"] == update["params_revision"]
            assert state["effective_params"] == update["params"]
            assert state["state"] == "ended"
        finally:
            _finish_tasks(client, [task for task in (ack_task, revoke_task) if task], [release])
