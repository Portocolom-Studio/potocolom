"""Closed protocol 6 ACK identity and retained-outcome checks."""

import asyncio
import contextlib
import copy
import functools
import uuid

import pytest
from sqlalchemy import text

from app import command_store, db, jobs, worker_authority
from app import realtime
from app import sessions as account_sessions
from app.settings import get_settings
from test_command_store import acknowledge_bound, command_ack_fields
from test_fleet_protocol6 import (
    hello,
    manifest,
    protocol6_client,
    request_work_grant,
    root_keys,
    seed_queued_job,
)
from test_totp_flow import _make as make_account_user, accounts
from test_v6_sessions import command_ack, open_live_session

__all__ = ["accounts"]


def _alter(field, value):
    def change(ack):
        ack[field] = value
    return change


def _remove(field):
    def change(ack):
        del ack[field]
    return change


async def read_journal_command(worker_id, incarnation, sequence):
    """The store's own read of one committed row: real key ring, journal AAD."""
    async with db.session_factory() as session:
        row = (await session.execute(
            text("SELECT command_id, body_hash, state, encrypted_body FROM worker_commands "
                 "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                 "AND command_sequence = :sequence"),
            {"worker_id": worker_id, "incarnation": incarnation, "sequence": sequence},
        )).mappings().one()
    plaintext = command_store.get_key_ring().decrypt(
        "worker-command", row["encrypted_body"],
        command_store._aad(worker_id, incarnation, sequence,
                           row["command_id"], row["body_hash"]),
    )
    return row, command_store.decode_control(plaintext)


@pytest.mark.db
@pytest.mark.parametrize(
    ("name", "change", "invalid_shape"),
    [
        ("worker", _alter("worker_id", "another-worker"), True),
        ("incarnation", _alter("incarnation", str(uuid.uuid4())), True),
        ("region", _alter("region", "another-region"), False),
        ("owner_epoch", _alter("owner_epoch", 99), False),
        ("lease_id", _alter("lease_id", str(uuid.uuid4())), False),
        ("lease_expiry", _alter("lease_expires_at", "2099-01-01T00:00:00Z"), False),
        ("grant_nonce", _alter("grant_nonce", str(uuid.uuid4())), False),
        ("sequence", _alter("command_sequence", 99), False),
        ("command_id", _alter("command_id", str(uuid.uuid4())), False),
        ("body_hash", _alter("body_hash", "0" * 64), False),
        ("job_id", _alter("job_id", str(uuid.uuid4())), False),
        ("dispatch_sequence", _alter("dispatch_sequence", 99), False),
        ("wrapper", _alter("type", "session_ready"), True),
        ("extra_field", _alter("unexpected", True), True),
        ("missing_expiry", _remove("lease_expires_at"), True),
        ("accepted_with_code", _alter("code", "invalid_params"), True),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_wrong_ack_identity_or_shape_leaves_command_pending(
    monkeypatch, name, change, invalid_shape,
):
    root_keys(monkeypatch)
    worker_id = f"s1-ack-matrix-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"s1-ack-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)
            job_id = client.portal.call(seed_queued_job, model_id)
            delivery = client.portal.start_task_soon(jobs.dispatch, job_id)
            command = worker.receive_json()
            assert delivery.result() is True
            ack = {
                **command_ack_fields(command),
                "type": "command_ack",
                "status": "accepted",
            }
            change(ack)
            if invalid_shape:
                with pytest.raises(command_store.CommandRefused):
                    client.portal.call(acknowledge_bound, ack, worker_id, incarnation)
            else:
                result = client.portal.call(acknowledge_bound, ack, worker_id, incarnation)
                assert result.recorded is False

            async def inspect_pending():
                async with db.session_factory() as session:
                    row = (await session.execute(
                        text("SELECT state, encrypted_body, body_hash, command_id "
                             "FROM worker_commands WHERE worker_id = :worker_id "
                             "AND incarnation = :incarnation "
                             "AND command_sequence = :sequence"),
                        {"worker_id": worker_id, "incarnation": incarnation,
                         "sequence": command["command_sequence"]},
                    )).mappings().one()
                    floor = await session.scalar(
                        text("SELECT acknowledged_floor FROM worker_connections "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )
                    return row, floor

            row, floor = client.portal.call(inspect_pending)
            assert row["state"] == "pending"
            assert row["encrypted_body"] is not None
            assert row["body_hash"] == command["body_hash"]
            assert str(row["command_id"]) == command["command_id"]
            assert floor < command["command_sequence"], name


@pytest.mark.db
def test_exact_duplicate_is_retained_but_conflicting_outcome_is_refused(monkeypatch):
    async def parked():
        return

    monkeypatch.setattr(jobs, "dispatch_step", parked)
    root_keys(monkeypatch)
    worker_id = f"s1-ack-outcome-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"s1-ack-outcome-model-{uuid.uuid4()}"
    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            greeting = hello(worker_id, [manifest(model_id)], incarnation=incarnation)
            greeting["realtime_slots"] = 2
            worker.send_json(greeting)
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)
            first_job = client.portal.call(seed_queued_job, model_id)
            second_job = client.portal.call(seed_queued_job, model_id)
            assert client.portal.call(jobs.dispatch, first_job) is True
            first = worker.receive_json()
            second_delivery = client.portal.start_task_soon(jobs.dispatch, second_job)
            assert second_delivery.result(timeout=10) is True

            head = client.portal.call(command_store.read_exact_pending, worker_id, incarnation)
            assert head is not None
            assert head.sequence == first["command_sequence"]
            assert str(head.command_id) == first["command_id"]

            second_row, second_command = client.portal.call(
                read_journal_command, worker_id, incarnation,
                first["command_sequence"] + 1,
            )
            assert second_row["state"] == "pending"
            assert second_command["command_sequence"] > first["command_sequence"]

            first_row, first_command = client.portal.call(
                read_journal_command, worker_id, incarnation, first["command_sequence"],
            )
            assert first_row["state"] == "pending"
            assert str(first_row["command_id"]) == first["command_id"]
            assert first_row["body_hash"] == first["body_hash"]
            assert first_command["command_sequence"] == first["command_sequence"]

            ack = {
                **command_ack_fields(second_command),
                "type": "command_ack",
                "status": "accepted",
            }
            first_result = client.portal.call(
                acknowledge_bound, ack, worker_id, incarnation,
            )
            assert first_result.recorded is True
            repeated = client.portal.call(
                acknowledge_bound, copy.deepcopy(ack), worker_id, incarnation,
            )
            assert repeated.recorded is True
            conflict = {**ack, "status": "refused", "code": "invalid_params"}
            refused = client.portal.call(acknowledge_bound, conflict, worker_id, incarnation)
            assert refused.recorded is False

            async def inspect_retained():
                async with db.session_factory() as session:
                    row = (await session.execute(
                        text("SELECT state, ack_status, ack_body FROM worker_commands "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                             "AND command_sequence = :sequence"),
                        {"worker_id": worker_id, "incarnation": incarnation,
                         "sequence": second_command["command_sequence"]},
                    )).mappings().one()
                    floor = await session.scalar(
                        text("SELECT acknowledged_floor FROM worker_connections "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )
                    return row, floor

            row, floor = client.portal.call(inspect_retained)
            assert row["state"] == "acknowledged"
            assert row["ack_status"] == "accepted"
            assert bytes(row["ack_body"]).decode() == command_store.canonical_bytes(ack).decode()
            assert floor < second_command["command_sequence"]
            first_ack = {
                **command_ack_fields(first),
                "type": "command_ack",
                "status": "accepted",
            }
            final = client.portal.call(acknowledge_bound, first_ack, worker_id, incarnation)
            assert final.recorded is True

            async def inspect_compaction():
                async with db.session_factory() as session:
                    floor = await session.scalar(
                        text("SELECT acknowledged_floor FROM worker_connections "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )
                    retained = await session.scalar(
                        text("SELECT count(*) FROM worker_commands "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )
                    return floor, retained

            assert client.portal.call(inspect_compaction) == (second_command["command_sequence"], 0)


@pytest.mark.db
def test_higher_same_worker_command_waits_behind_a_held_journal_head(monkeypatch):
    async def parked():
        return None

    monkeypatch.setattr(jobs, "dispatch_step", parked)
    root_keys(monkeypatch)
    worker_id = f"s1-ack-held-head-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"s1-ack-held-model-{uuid.uuid4()}"
    entered = asyncio.Event()
    release = asyncio.Event()
    sent = []
    reserve_calls = []
    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            greeting = hello(worker_id, [manifest(model_id)], incarnation=incarnation)
            greeting["realtime_slots"] = 2
            worker.send_json(greeting)
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)
            server_worker = realtime.workers[worker_id]
            original_send = server_worker.ws.send_text
            original_reserve = realtime._reserve_worker_delivery

            async def hold_first(message):
                sent.append(message)
                if len(sent) == 1:
                    entered.set()
                    await release.wait()
                await original_send(message)

            def record_reserve(target, *, prepare=None):
                reservation = original_reserve(target, prepare=prepare)
                reserve_calls.append(reservation)
                return reservation

            server_worker.ws.send_text = hold_first
            realtime._reserve_worker_delivery = record_reserve
            first_task = None
            second_task = None
            try:
                first_job = client.portal.call(seed_queued_job, model_id)
                second_job = client.portal.call(seed_queued_job, model_id)
                first_task = client.portal.start_task_soon(jobs.dispatch, first_job)
                client.portal.call(asyncio.wait_for, entered.wait(), 5)
                second_task = client.portal.start_task_soon(jobs.dispatch, second_job)

                async def second_reserved():
                    for _ in range(250):
                        if len(reserve_calls) >= 2:
                            return
                        await asyncio.sleep(0.02)
                    raise AssertionError("the second dispatch never joined the held delivery")

                client.portal.call(second_reserved)
                first_delivery = reserve_calls[0][0]
                assert reserve_calls[0][1] is True
                assert reserve_calls[1][0] is first_delivery
                assert reserve_calls[1][1] is False

                async def journal_rows():
                    async with db.session_factory() as session:
                        return await session.scalar(
                            text("SELECT count(*) FROM worker_commands "
                                 "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                            {"worker_id": worker_id, "incarnation": incarnation},
                        )

                assert client.portal.call(journal_rows) == 1
                assert len(sent) == 1
                client.portal.call(release.set)
                first = worker.receive_json()
                assert first["type"] == "dispatch_job"
                assert first["command_sequence"] == 1

                async def second_row_committed():
                    for _ in range(250):
                        if await journal_rows() == 2:
                            return
                        await asyncio.sleep(0.02)
                    raise AssertionError("the later job did not commit behind the held head")

                client.portal.call(second_row_committed)
                second_row, second_command = client.portal.call(
                    read_journal_command, worker_id, incarnation, 2,
                )
                assert second_row["state"] == "pending"
                assert second_command["command_sequence"] == 2
                assert len(sent) == 1
                worker.send_json({
                    **command_ack_fields(first),
                    "type": "command_ack",
                    "status": "accepted",
                })
                second = worker.receive_json()
                assert second["type"] == "dispatch_job"
                assert second["command_sequence"] == 2
                assert second["job_id"] == str(second_job)
                assert first_task.result(timeout=10) is True
                assert second_task.result(timeout=10) is True
            finally:
                client.portal.call(release.set)
                for task in (first_task, second_task):
                    if task is not None:
                        with contextlib.suppress(Exception):
                            task.result(timeout=10)
                realtime._reserve_worker_delivery = original_reserve


@pytest.mark.db
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("session_id", str(uuid.uuid4())),
        ("attempt_id", str(uuid.uuid4())),
        ("control_generation", 99),
    ],
)
def test_wrong_realtime_subject_ack_is_ignored(accounts, field, replacement):
    worker_id = f"s1-ack-session-{uuid.uuid4()}"
    model_id = f"s1-ack-session-model-{uuid.uuid4()}"
    with protocol6_client(worker_id) as client:
        user = client.portal.call(make_account_user, f"s1-ack-session-{uuid.uuid4()}@example.test")
        issued = client.portal.call(account_sessions.mint, user, False)
        session_cookie, csrf_cookie = account_sessions.cookie_names(get_settings().public_url)
        client.cookies.set(session_cookie, issued.token)
        client.cookies.set(csrf_cookie, issued.csrf)
        worker_ws, worker, _grant, _open, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        try:
            browser_ws.send_json({
                "type": "update_params", "params": {"prompt": "subject"},
            })
            command = worker_ws.receive_json()
            assert command["type"] == "update_session"
            ack = command_ack(command)
            ack[field] = replacement
            result = client.portal.call(
                acknowledge_bound, ack, worker_id, worker.incarnation,
            )
            assert result.recorded is False

            async def inspect_pending():
                async with db.session_factory() as session:
                    return (await session.execute(
                        text("SELECT state, encrypted_body FROM worker_commands "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                             "AND command_sequence = :sequence"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation,
                         "sequence": command["command_sequence"]},
                    )).mappings().one()

            row = client.portal.call(inspect_pending)
            assert row["state"] == "pending"
            assert row["encrypted_body"] is not None
        finally:
            browser.__exit__(None, None, None)
            fleet.__exit__(None, None, None)


@pytest.mark.db
def test_ack_from_stale_transport_owner_is_refused(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-ack-transport-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"s1-ack-transport-model-{uuid.uuid4()}"
    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)
            job_id = client.portal.call(seed_queued_job, model_id)
            delivery = client.portal.start_task_soon(jobs.dispatch, job_id)
            command = worker.receive_json()
            assert delivery.result() is True
            ack = {
                **command_ack_fields(command),
                "type": "command_ack",
                "status": "accepted",
            }
            stale_owner = functools.partial(
                command_store.acknowledge,
                ack,
                expected_worker_id=worker_id,
                expected_incarnation=incarnation,
                transport_owner_id=uuid.uuid4(),
            )
            with pytest.raises(command_store.CommandRefused):
                client.portal.call(stale_owner)
