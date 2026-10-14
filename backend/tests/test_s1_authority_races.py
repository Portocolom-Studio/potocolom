"""Two real-PostgreSQL races from the S1 repair round (issue #717).

A browser binding that expires while delivery waits on a held journal row must
not register or send, and the terminal-report/obsolete-ACK interleaving must
not invert journal and lease locks. Both tests run the actual application
transactions against the test database; neither mocks a lock. The delivery
entry used here is the single production path every trigger funnels into, with
the real read, the real registration and the real socket.
"""

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from contextlib import ExitStack

import asyncpg
import pytest
from sqlalchemy import select, text

from app import command_store, db, jobs, realtime, sessions as account_sessions, worker_authority
from app.protocol6 import canonical_bytes, validate_message
from app.settings import get_settings
from app.tables import User
from test_command_store import acknowledge_bound, command_ack_fields
from test_fleet_protocol6 import (
    hello,
    manifest,
    protocol6_client,
    request_work_grant,
    root_keys,
    seed_queued_job,
)
from test_receipt_recovery import job_failed_from_dispatch
from test_totp_flow import accounts
from test_v6_sessions import open_live_session

__all__ = ["accounts"]

_EXPIRY = "2.5 seconds"


def _marker(worker_id: str, incarnation: uuid.UUID):
    return realtime._sent_command_sequences.get((worker_id, incarnation))


async def _hold_journal_row(worker_id: str, incarnation: uuid.UUID, sequence: int,
                            held: asyncio.Event, release: asyncio.Event,
                            pid: dict | None = None) -> None:
    connection = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        if pid is not None:
            pid["backend"] = await connection.fetchval("SELECT pg_backend_pid()")
        async with connection.transaction():
            await connection.execute(
                "SELECT command_sequence FROM worker_commands "
                "WHERE worker_id = $1 AND incarnation = $2 AND command_sequence = $3 "
                "FOR UPDATE",
                worker_id, incarnation, sequence,
            )
            held.set()
            await release.wait()
    finally:
        await connection.close()


def _join_portal_tasks(client, release_events, tasks, timeout: float = 15) -> None:
    preserve_primary = sys.exc_info()[0] is not None
    errors = []
    for event in release_events:
        try:
            client.portal.call(event.set)
        except BaseException as error:
            errors.append(error)
    for task in tasks:
        try:
            task.result(timeout=timeout)
        except BaseException as error:
            errors.append(error)
    if errors and not preserve_primary:
        raise errors[0]


async def _run_delivery(worker, outcome: dict, done: asyncio.Event) -> None:
    try:
        outcome["result"] = await realtime._deliver_pending_worker_command(worker)
    except BaseException as error:
        outcome["error"] = repr(error)
    finally:
        done.set()


async def _wait_peer_lock_wait(started: asyncio.Event | None, timeout: float,
                               done: asyncio.Event | None = None) -> bool:
    """True once another backend waits on our journal or lease rows, or the
    watched transaction finished on its own."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if done is not None and done.is_set():
            return True
        if started is None or started.is_set():
            async with db.session_factory() as session:
                waiting = await session.scalar(text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                    "AND wait_event_type = 'Lock' "
                    "AND (query ILIKE 'SELECT%worker_commands%' "
                    "OR query ILIKE 'SELECT%scheduler_leases%')"
                ))
                if waiting:
                    return True
        await asyncio.sleep(0.02)
    return False


async def _wait_holder_blocked(holder: dict, timeout: float) -> bool:
    """True once a backend waits on a lock this test's own journal holder owns.

    pg_blocking_pids pins the observation to the holder's backend, so an
    unrelated lock wait in the same database can never satisfy the witness.
    """
    deadline = time.monotonic() + timeout
    backend = holder.get("backend")
    while backend is not None and time.monotonic() < deadline:
        async with db.session_factory() as session:
            waiting = await session.scalar(text(
                "SELECT count(*) FROM pg_stat_activity a "
                "WHERE a.datname = current_database() AND a.pid <> pg_backend_pid() "
                "AND a.wait_event_type = 'Lock' "
                "AND :holder = ANY(pg_blocking_pids(a.pid))"
            ), {"holder": backend})
            if waiting:
                return True
        await asyncio.sleep(0.02)
    return False


async def _binding_live(auth_session_id: uuid.UUID, column: str) -> bool:
    if column not in {"idle_expires_at", "absolute_expires_at"}:
        raise AssertionError(f"unexpected expiry column {column}")
    async with db.session_factory() as session:
        return bool(await session.scalar(text(
            f"SELECT {column} > clock_timestamp() FROM sessions WHERE id = :id"
        ), {"id": auth_session_id}))


async def _wait_binding_expired(auth_session_id: uuid.UUID, column: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not await _binding_live(auth_session_id, column):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the binding never expired while the journal wait was held")


async def _set_expiry(auth_session_id: uuid.UUID, column: str) -> None:
    if column not in {"idle_expires_at", "absolute_expires_at"}:
        raise AssertionError(f"unexpected expiry column {column}")
    async with db.session_factory() as session:
        await session.execute(text(
            f"UPDATE sessions SET {column} = clock_timestamp() + interval '{_EXPIRY}' "
            "WHERE id = :id"
        ), {"id": auth_session_id})
        await session.commit()


async def _authority_live(worker_id: str, incarnation: uuid.UUID) -> bool:
    async with db.session_factory() as session:
        row = (await session.execute(text(
            "SELECT lease.expires_at > clock_timestamp() AS lease_live, "
            "worker.grant_expires_at > clock_timestamp() AS grant_live "
            "FROM worker_connections worker JOIN scheduler_leases lease "
            "ON lease.region = worker.region AND lease.owner_epoch = worker.owner_epoch "
            "AND lease.lease_id = worker.lease_id "
            "WHERE worker.worker_id = :worker_id AND worker.incarnation = :incarnation"
        ), {"worker_id": worker_id, "incarnation": incarnation})).mappings().one_or_none()
    return row is not None and bool(row["lease_live"]) and bool(row["grant_live"])


async def _auth_binding(session_id: uuid.UUID) -> uuid.UUID | None:
    async with db.session_factory() as session:
        return await session.scalar(text(
            "SELECT auth_session_id FROM realtime_sessions WHERE id = :id"
        ), {"id": session_id})


async def _wait_events(*events: asyncio.Event) -> None:
    while not all(event.is_set() for event in events):
        await asyncio.sleep(0.02)


async def _wait_within(seconds: float, *events: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(_wait_events(*events), seconds)
    except TimeoutError:
        raise AssertionError(
            f"the application transactions did not finish within {seconds}s"
        ) from None


def _mint_local_binding():
    async def mint_binding():
        async with db.session_factory() as session:
            user = (await session.execute(
                select(User).where(User.email == db.LOCAL_USER_EMAIL)
            )).scalar_one()
        issued = await account_sessions.mint(user, remember_me=False)
        resolved = await account_sessions.resolve(issued.token)
        return issued, resolved.session.id

    return mint_binding


def _bound_update_session(client, worker_id: str, model_id: str):
    """Open a live protocol 6 session on the local account's browser binding
    and leave one unacknowledged update as the pending journal head."""
    issued, auth_session_id = client.portal.call(_mint_local_binding())
    session_cookie, csrf_cookie = account_sessions.cookie_names(get_settings().public_url)
    client.cookies.set(session_cookie, issued.token)
    client.cookies.set(csrf_cookie, issued.csrf)
    worker_ws, worker, _grant, open_command, browser_ws, browser, fleet = (
        open_live_session(client, worker_id, model_id)
    )
    with ExitStack() as cleanup:
        cleanup.callback(fleet.__exit__, None, None, None)
        cleanup.callback(browser.__exit__, None, None, None)
        browser_ws.send_json({"type": "update_params", "params": {"prompt": "pending"}})
        first_text = worker_ws.receive_text()
        update = json.loads(first_text)
        assert update["type"] == "update_session"
        bound = client.portal.call(_auth_binding, uuid.UUID(open_command["session_id"]))
        assert bound == auth_session_id, "the live session must carry the browser binding"
        return (issued, auth_session_id, worker_ws, worker, update, first_text,
                browser_ws, cleanup.pop_all())


def _deliver_while_row_held(client, worker_ws, worker, sequence: int,
                            auth_session_id: uuid.UUID | None, expiry_column):
    """Hold the pending journal row with a second transaction, expire the
    binding first, renew the grant while the row is held, then run the real
    delivery and collect its outcome.

    The renewal happens under the row so no early unheld retry can reach the
    one-second throttle before the delivery this test observes. Both the
    delivery and the holder's backend are joined before returning, on success
    and on failure."""
    worker_id, incarnation = worker.id, worker.incarnation
    held = asyncio.Event()
    release = asyncio.Event()
    done = asyncio.Event()
    outcome = {}
    holder: dict = {}
    holder_task = client.portal.start_task_soon(
        _hold_journal_row, worker_id, incarnation, sequence, held, release, holder)
    started_tasks = [holder_task]
    marker_before = _marker(worker_id, incarnation)
    try:
        client.portal.call(asyncio.wait_for, held.wait(), 10)
        if expiry_column is not None:
            client.portal.call(_set_expiry, auth_session_id, expiry_column)
        request_work_grant(worker_ws, worker_id, incarnation)
        delivery_task = client.portal.start_task_soon(_run_delivery, worker, outcome, done)
        started_tasks.append(delivery_task)
        blocked = client.portal.call(_wait_holder_blocked, holder, 1.5)
        assert blocked, "delivery never reached the journal-row wait; outcome=" + repr(outcome)
        if expiry_column is not None:
            assert client.portal.call(_binding_live, auth_session_id, expiry_column), (
                "the binding must still be live when the journal wait begins")
            client.portal.call(_wait_binding_expired, auth_session_id, expiry_column)
        assert client.portal.call(_authority_live, worker_id, incarnation), (
            "the scheduler lease and the grant must stay valid through the wait")
        client.portal.call(release.set)
        client.portal.call(asyncio.wait_for, done.wait(), 15)
    finally:
        _join_portal_tasks(client, [release], started_tasks)
    return marker_before, _marker(worker_id, incarnation), dict(outcome)


@pytest.mark.db
@pytest.mark.parametrize("expiry_column", ["idle_expires_at", "absolute_expires_at"])
def test_browser_binding_expiring_during_the_journal_wait_never_registers_or_sends(
        accounts, expiry_column):
    worker_id = f"s1-expiry-{uuid.uuid4()}"
    model_id = f"s1-expiry-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        _issued, auth_session_id, worker_ws, worker, update, _first_text, browser_ws, cleanup = (
            _bound_update_session(client, worker_id, model_id)
        )
        try:
            time.sleep(1.5)  # the one-second retry throttle on the pending head
            marker_before, marker_after, outcome = _deliver_while_row_held(
                client, worker_ws, worker, update["command_sequence"],
                auth_session_id, expiry_column)
            assert "error" not in outcome, f"delivery failed instead of refusing: {outcome}"
            assert marker_after == marker_before, (
                "an expired browser binding still registered and sent the command: "
                f"{marker_before} -> {marker_after}"
            )
        finally:
            cleanup.close()


@pytest.mark.db
def test_live_binding_registers_and_sends_the_exact_retry_during_the_journal_wait(
        accounts):
    worker_id = f"s1-expiry-live-{uuid.uuid4()}"
    model_id = f"s1-expiry-live-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        (_issued, _auth, worker_ws, worker, update, first_text,
         browser_ws, cleanup) = _bound_update_session(client, worker_id, model_id)
        try:
            time.sleep(1.5)  # the one-second retry throttle on the pending head
            marker_before, marker_after, outcome = _deliver_while_row_held(
                client, worker_ws, worker, update["command_sequence"], _auth, None)
            assert "error" not in outcome, f"delivery failed: {outcome}"
            assert marker_after != marker_before, (
                "a still-live binding must register and send the pending head")
            retry_text = worker_ws.receive_text()
            assert retry_text == first_text, "the retry must carry the exact original bytes"
        finally:
            cleanup.close()


async def _count_sequence(worker_id: str, incarnation: uuid.UUID, sequence: int) -> int:
    async with db.session_factory() as session:
        return await session.scalar(text(
            "SELECT count(*) FROM worker_commands WHERE worker_id = :worker_id "
            "AND incarnation = :incarnation AND command_sequence = :sequence"
        ), {"worker_id": worker_id, "incarnation": incarnation, "sequence": sequence})


async def _race_outcomes(worker_id: str, incarnation: uuid.UUID,
                          job_id: uuid.UUID, command_sequence: int,
                          dispatch_sequence: int,
                          range_id: uuid.UUID, compacted_sequence: int):
    async with db.session_factory() as session:
        receipt = await session.scalar(text(
            "SELECT count(*) FROM worker_report_receipts WHERE range_id = :range_id"
        ), {"range_id": range_id})
        row = (await session.execute(text(
            "SELECT state, encrypted_body FROM worker_commands WHERE worker_id = :worker_id "
            "AND incarnation = :incarnation AND command_sequence = :sequence"
        ), {"worker_id": worker_id, "incarnation": incarnation,
            "sequence": command_sequence})).mappings().one_or_none()
        compacted = await session.scalar(text(
            "SELECT count(*) FROM worker_commands WHERE worker_id = :worker_id "
            "AND incarnation = :incarnation AND command_sequence = :sequence"
        ), {"worker_id": worker_id, "incarnation": incarnation,
            "sequence": compacted_sequence})
        floor = await session.scalar(text(
            "SELECT acknowledged_floor FROM worker_connections WHERE worker_id = :worker_id "
            "AND incarnation = :incarnation"
        ), {"worker_id": worker_id, "incarnation": incarnation})
        attempt_report = await session.scalar(text(
            "SELECT report_sequence FROM job_attempts WHERE job_id = :job_id "
            "AND dispatch_sequence = :dispatch_sequence"
        ), {"job_id": job_id, "dispatch_sequence": dispatch_sequence})
        return receipt, row, compacted, floor, attempt_report


@pytest.mark.db
def test_terminal_report_and_obsolete_ack_finish_in_one_lock_order(monkeypatch):
    """Account A's terminal report without physical completion holds the lease
    and connection before its erasure; the NULL-owner obsolete ACK holds the
    incarnation journal before it asks for that lease. PostgreSQL must never
    abort either valid transaction, and the receipt, erasure and ACK outcomes
    must survive."""
    root_keys(monkeypatch)
    worker_id = f"s1-race-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"s1-race-model-{uuid.uuid4()}"

    async def park_dispatch():
        return None

    monkeypatch.setattr(jobs, "dispatch_step", park_dispatch)

    a_ready = asyncio.Event()
    release_a = asyncio.Event()
    report_done = asyncio.Event()
    ack_done = asyncio.Event()
    ack_started = asyncio.Event()
    results: dict = {}

    original_erase = command_store.erase_subject_commands

    async def gated_erase(*args, **kwargs):
        a_ready.set()
        await release_a.wait()
        return await original_erase(*args, **kwargs)

    monkeypatch.setattr(command_store, "erase_subject_commands", gated_erase)

    async def run_report(report):
        try:
            results["report"] = await worker_authority.commit_worker_report(
                worker_id, incarnation, report)
        except BaseException as error:
            results["report_error"] = repr(error)
        finally:
            report_done.set()

    async def run_obsolete_ack(ack):
        ack_started.set()
        try:
            results["ack"] = await command_store.acknowledge(
                ack,
                expected_worker_id=worker_id,
                expected_incarnation=incarnation,
                transport_owner_id=worker_authority.TRANSPORT_OWNER_ID,
            )
        except BaseException as error:
            results["ack_error"] = repr(error)
        finally:
            ack_done.set()

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker_ws:
            worker_ws.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker_ws.receive_json()
            grant = request_work_grant(worker_ws, worker_id, incarnation)

            job_b = client.portal.call(seed_queued_job, model_id)
            assert client.portal.call(jobs.dispatch, job_b) is True
            command_b = worker_ws.receive_json()
            first_ack = {**command_ack_fields(command_b),
                         "type": "command_ack", "status": "accepted"}
            recorded = client.portal.call(acknowledge_bound, first_ack, worker_id, incarnation)
            assert recorded.recorded is True
            compacted_sequence = command_b["command_sequence"]
            assert client.portal.call(
                _count_sequence, worker_id, incarnation, compacted_sequence
            ) == 0, "the acknowledged head must be compacted to a NULL-owner obsolete ACK"

            job_a = client.portal.call(seed_queued_job, model_id)
            assert client.portal.call(jobs.dispatch, job_a) is True
            command_a = worker_ws.receive_json()
            report = job_failed_from_dispatch(command_a, grant["grant_nonce"])
            report["physical_complete"] = False
            source = {key: value for key, value in report.items() if key != "report_hash"}
            report["report_hash"] = hashlib.sha256(canonical_bytes(source)).hexdigest()
            validate_message(report, expected_worker_id=worker_id,
                             expected_incarnation=incarnation)
            obsolete_ack = {**command_ack_fields(command_b),
                            "type": "command_ack", "status": "accepted"}

            report_task = None
            ack_task = None
            try:
                report_task = client.portal.start_task_soon(run_report, report)
                client.portal.call(asyncio.wait_for, a_ready.wait(), 10)
                ack_task = client.portal.start_task_soon(run_obsolete_ack, obsolete_ack)
                settled = client.portal.call(_wait_peer_lock_wait, ack_started, 5, ack_done)
                assert settled, (
                    "the ACK neither reached a lock wait nor finished: " + repr(results))
                client.portal.call(release_a.set)
                client.portal.call(_wait_within, 20, report_done, ack_done)
            finally:
                _join_portal_tasks(
                    client, [release_a],
                    [task for task in (report_task, ack_task) if task is not None],
                    timeout=25,
                )
            assert "report_error" not in results, (
                "the lock inversion aborted a valid terminal report transaction: "
                + results.get("report_error", ""))
            assert "ack_error" not in results, (
                "the lock inversion aborted a valid ACK transaction: "
                + results.get("ack_error", ""))

            report_ack = json.loads(results["report"])
            assert report_ack["type"] == "checkpoint_ack"
            assert report_ack["status"] == "accepted"
            assert results["ack"].obsolete is True and results["ack"].recorded is False

            receipt, row, compacted, floor, attempt_report = client.portal.call(
                _race_outcomes, worker_id, incarnation, job_a,
                command_a["command_sequence"],
                command_a["dispatch_sequence"],
                uuid.UUID(command_a["work_budget"]["range_id"]),
                compacted_sequence,
            )
            assert receipt == 1, "the terminal report must commit its receipt"
            assert row is not None and row["state"] == "erased" \
                and row["encrypted_body"] is None, (
                    "the terminal erasure must erase the pending dispatch body")
            assert compacted == 0, "the acknowledged head must stay compacted"
            assert floor == compacted_sequence, (
                "the floor must still block on the unresolved erased head")
            assert attempt_report == report["report_sequence"], (
                "the report measurement must be preserved")
