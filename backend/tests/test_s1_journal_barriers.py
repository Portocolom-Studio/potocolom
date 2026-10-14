"""S1 command-store guards: erased heads, thin ACKs, sequence sentinels,
and the retained-capacity counters."""

import asyncio
import hashlib
import uuid

import pytest
from sqlalchemy import text

from app import command_store, db, jobs, worker_authority
from app.account_lock import hold_the_account
from app.keyring import KeyRing
from app.protocol6 import canonical_bytes, decode_control, preflight_worker_control
from app.settings import get_settings
from app.tables import User
from test_command_store import acknowledge_bound, command_ack_fields, park_dispatch_loop
from test_fleet_protocol6 import (
    hello,
    manifest,
    protocol6_client,
    request_work_grant,
    root_keys,
    seed_queued_job,
)
from test_v6_sessions import open_live_session

__all__ = ["park_dispatch_loop"]



@pytest.mark.db
def test_protocol6_erased_command_head_blocks_replay_until_exact_late_ack(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"journal-erasure-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"journal-erasure-model-{uuid.uuid4()}"
    committed = []
    original_commit = command_store.commit_command

    async def capture_commit(*args, **kwargs):
        command = await original_commit(*args, **kwargs)
        committed.append(command)
        return command

    monkeypatch.setattr(command_store, "commit_command", capture_commit)
    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)
            job_id = client.portal.call(seed_queued_job, model_id)
            dispatch = client.portal.start_task_soon(jobs.dispatch, job_id)
            command = worker.receive_json()
            assert dispatch.result() is True
            assert committed[0].body == b""
            assert committed[0].account_user_id == db.local_user_id
            assert committed[0].subject_id == job_id
            assert committed[0].subject_sequence == command["dispatch_sequence"]
            assert len(committed[0].ack_identity_hash) == 64

            async def erase_dispatch():
                async with db.session_factory() as session:
                    async with session.begin():
                        await hold_the_account(session, db.local_user_id, wait=True)
                        count = await command_store.erase_subject_commands(
                            session,
                            db.local_user_id,
                            kind="dispatch_job",
                            subject_id=job_id,
                            subject_sequence=command["dispatch_sequence"],
                        )
                        await session.execute(
                            text("DELETE FROM jobs WHERE id = :job_id"),
                            {"job_id": job_id},
                        )
                        await session.execute(
                            text("DELETE FROM users WHERE id = :user_id"),
                            {"user_id": db.local_user_id},
                        )
                        return count

            assert client.portal.call(erase_dispatch) == 1

            async def inspect_erasure():
                async with db.session_factory() as session:
                    row = (await session.execute(
                        text(
                            "SELECT state, encrypted_body, complete_bytes, ack_identity_hash "
                            "FROM worker_commands WHERE worker_id = :worker_id "
                            "AND incarnation = :incarnation AND command_sequence = 1"
                        ), {"worker_id": worker_id, "incarnation": incarnation},
                    )).mappings().one()
                    connection = (await session.execute(
                        text(
                            "SELECT acknowledged_floor, closed_at FROM worker_connections "
                            "WHERE worker_id = :worker_id AND incarnation = :incarnation"
                        ), {"worker_id": worker_id, "incarnation": incarnation},
                    )).mappings().one()
                    return row, connection

            row, connection = client.portal.call(inspect_erasure)
            assert row["state"] == "erased"
            assert row["encrypted_body"] is None
            assert row["complete_bytes"] == 0
            assert row["ack_identity_hash"] == committed[0].ack_identity_hash
            assert connection["acknowledged_floor"] == 0
            assert connection["closed_at"] is not None
            with pytest.raises(command_store.CommandRefused):
                client.portal.call(
                    command_store.read_exact_pending, worker_id, incarnation
                )

            ack = {**command_ack_fields(command), "type": "command_ack", "status": "accepted"}
            def missing_key():
                raise command_store.KeyRingError("test key is unavailable")

            monkeypatch.setattr(command_store, "get_key_ring", missing_key)
            result = client.portal.call(acknowledge_bound, ack, worker_id, incarnation)
            assert result.recorded is True
            assert result.publication is None

            async def inspect_floor():
                async with db.session_factory() as session:
                    floor = await session.scalar(
                        text(
                            "SELECT acknowledged_floor FROM worker_connections "
                            "WHERE worker_id = :worker_id AND incarnation = :incarnation"
                        ), {"worker_id": worker_id, "incarnation": incarnation},
                    )
                    rows = await session.scalar(
                        text(
                            "SELECT count(*) FROM worker_commands WHERE worker_id = :worker_id "
                            "AND incarnation = :incarnation"
                        ), {"worker_id": worker_id, "incarnation": incarnation},
                    )
                    return floor, rows

            assert client.portal.call(inspect_floor) == (1, 0)


@pytest.mark.db
def test_exact_ack_without_the_command_key_is_thin_and_fences_worker(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"journal-keyloss-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"journal-keyloss-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)
            job_id = client.portal.call(seed_queued_job, model_id)
            dispatch = client.portal.start_task_soon(jobs.dispatch, job_id)
            command = worker.receive_json()
            assert dispatch.result() is True

            monkeypatch.setattr(
                command_store, "get_key_ring",
                lambda: KeyRing([(32767, b"k" * 32)]),
            )
            ack = {**command_ack_fields(command), "type": "command_ack", "status": "accepted"}
            result = client.portal.call(acknowledge_bound, ack, worker_id, incarnation)
            assert result.recorded is True
            assert result.publication is None

            async def inspect_rows():
                async with db.session_factory() as session:
                    retained = await session.scalar(
                        text(
                            "SELECT count(*) FROM worker_commands WHERE worker_id = :worker_id "
                            "AND incarnation = :incarnation"
                        ), {"worker_id": worker_id, "incarnation": incarnation},
                    )
                    connection = (await session.execute(
                        text(
                            "SELECT acknowledged_floor, closed_at FROM worker_connections "
                            "WHERE worker_id = :worker_id AND incarnation = :incarnation"
                        ), {"worker_id": worker_id, "incarnation": incarnation},
                    )).mappings().one()
                    return retained, connection

            retained, connection = client.portal.call(inspect_rows)
            assert retained == 0
            assert connection["acknowledged_floor"] == 1
            assert connection["closed_at"] is not None


@pytest.mark.db
def test_account_erasure_uses_original_range_owner_after_job_remap(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"journal-owner-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"journal-owner-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)
            job_id = client.portal.call(seed_queued_job, model_id)
            assert client.portal.call(jobs.dispatch, job_id) is True
            command = worker.receive_json()
            ack = {**command_ack_fields(command), "type": "command_ack", "status": "accepted"}
            assert client.portal.call(acknowledge_bound, ack, worker_id, incarnation).recorded

            new_owner_id = uuid.uuid4()
            async def remap_job():
                async with db.session_factory() as session:
                    async with session.begin():
                        session.add(User(
                            id=new_owner_id, email=f"{new_owner_id}@remap.invalid",
                            role="user", state="active",
                        ))
                        await session.flush()
                        await session.execute(
                            text("UPDATE jobs SET user_id = :new_owner WHERE id = :job_id"),
                            {"new_owner": new_owner_id, "job_id": job_id},
                        )

            client.portal.call(remap_job)

            async def erase_with_range_owner(owner_id):
                async with db.session_factory() as session:
                    async with session.begin():
                        await hold_the_account(session, db.local_user_id, wait=True)
                        await session.execute(
                            text("UPDATE worker_physical_ranges SET account_user_id = :owner "
                                 "WHERE subject_id = :job_id"),
                            {"owner": owner_id, "job_id": job_id},
                        )
                        erased = await command_store.erase_account_commands(
                            session, {db.local_user_id},
                        )
                        closed = await session.scalar(
                            text("SELECT closed_at FROM worker_connections "
                                 "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                            {"worker_id": worker_id, "incarnation": incarnation},
                        )
                        return erased, closed

            erased, closed = client.portal.call(erase_with_range_owner, None)
            assert erased == 0 and closed is None
            erased, closed = client.portal.call(erase_with_range_owner, db.local_user_id)
            assert erased == 0 and closed is not None


def test_registered_delivery_joins_session_close_through_cancellation():
    async def run():
        close_started = asyncio.Event()
        allow_close = asyncio.Event()

        class Session(db.OwnedSession):
            async def close(self):
                close_started.set()
                await allow_close.wait()

        session = Session()

        async def owner():
            async with session:
                pass

        task = asyncio.create_task(owner())
        await close_started.wait()
        task.cancel()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_pending_read_joins_session_close_through_cancellation(monkeypatch):
    async def run():
        close_started = asyncio.Event()
        allow_close = asyncio.Event()

        class Result:
            def mappings(self):
                return self

            def one_or_none(self):
                return None

        class Session(db.OwnedSession):
            async def execute(self, *_args, **_kwargs):
                return Result()

            async def close(self):
                close_started.set()
                await allow_close.wait()

        session = Session()
        monkeypatch.setattr(db, "session_factory", lambda: session)
        task = asyncio.create_task(
            command_store.read_exact_pending("worker", uuid.uuid4())
        )
        await close_started.wait()
        task.cancel()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_command_parameter_small_and_ack_size_boundaries(monkeypatch):
    assert command_store.MAX_PARAMETER_COMMAND_BYTES == 1024 * 1024
    assert command_store.MAX_SMALL_COMMAND_BYTES == 16 * 1024
    assert command_store.MAX_PENDING_BYTES == 4 * 1024 * 1024
    assert command_store.command_size_limit(
        "dispatch_job", command_store.MAX_PARAMETER_COMMAND_BYTES,
        command_store.MAX_SMALL_COMMAND_BYTES,
    ) == command_store.MAX_PARAMETER_COMMAND_BYTES
    assert command_store.command_size_limit(
        "close_session", command_store.MAX_PARAMETER_COMMAND_BYTES,
        command_store.MAX_SMALL_COMMAND_BYTES,
    ) == command_store.MAX_SMALL_COMMAND_BYTES

    worker_id = "size-boundary-worker"
    incarnation = uuid.uuid4()
    common = {
        "worker_id": worker_id, "incarnation": str(incarnation),
        "region": "size-boundary", "owner_epoch": 1,
        "lease_id": str(uuid.uuid4()), "lease_expires_at": "2026-12-31T00:00:00Z",
        "grant_nonce": str(uuid.uuid4()), "command_sequence": 1,
        "command_id": str(uuid.uuid4()),
    }

    def sized_command(kind, payload_size):
        message = {
            **common, "type": kind, "dispatch_sequence": 1,
            "dispatch_token": "token", "job_id": str(uuid.uuid4()),
            "model_id": "size-model", "params": {"payload": "x" * payload_size},
            "upload": {}, "work_budget": {},
        }
        message["body_hash"] = hashlib.sha256(canonical_bytes(message)).hexdigest()
        return message

    base = sized_command("dispatch_job", 0)
    exact_payload_size = command_store.MAX_PARAMETER_COMMAND_BYTES - len(
        preflight_worker_control(base)
    )
    exact = sized_command("dispatch_job", exact_payload_size)
    assert len(preflight_worker_control(exact)) == command_store.MAX_PARAMETER_COMMAND_BYTES
    with pytest.raises(ValueError):
        preflight_worker_control(sized_command("dispatch_job", exact_payload_size + 1))

    small = {
        **common, "type": "close_session", "session_id": str(uuid.uuid4()),
        "attempt_id": str(uuid.uuid4()), "control_generation": 1,
        "body_hash": "0" * 64,
    }
    small["region"] = ""
    small["region"] = "x" * (command_store.MAX_SMALL_COMMAND_BYTES - len(canonical_bytes(small)))
    small["body_hash"] = hashlib.sha256(canonical_bytes({
        key: value for key, value in small.items() if key != "body_hash"
    })).hexdigest()
    assert len(preflight_worker_control(small)) == command_store.MAX_SMALL_COMMAND_BYTES
    small["region"] += "x"
    small["body_hash"] = hashlib.sha256(canonical_bytes({
        key: value for key, value in small.items() if key != "body_hash"
    })).hexdigest()
    with pytest.raises(ValueError):
        preflight_worker_control(small)

    ack = {
        "type": "command_ack", **common, "body_hash": "a" * 64,
        "job_id": str(uuid.uuid4()), "dispatch_sequence": 1, "status": "accepted",
        "region": "x" * command_store.MAX_SMALL_COMMAND_BYTES,
    }
    monkeypatch.setattr(db, "session_factory", object())

    async def reject_large_ack():
        with pytest.raises(command_store.CommandRefused):
            await command_store.acknowledge(
                ack, expected_worker_id=worker_id, expected_incarnation=incarnation,
                transport_owner_id=uuid.uuid4(),
            )

    asyncio.run(reject_large_ack())

    ack["region"] = "size-boundary"
    ack["body_hash"] = "a" * 64
    ack["region"] = ""
    ack["region"] = "x" * (command_store.MAX_SMALL_COMMAND_BYTES - len(canonical_bytes(ack)))
    assert len(canonical_bytes(ack)) == command_store.MAX_SMALL_COMMAND_BYTES

    def reached_session_factory():
        raise AssertionError("ACK size validation passed")

    monkeypatch.setattr(db, "session_factory", reached_session_factory)

    async def accept_exact_ack_size():
        with pytest.raises(AssertionError, match="ACK size validation passed"):
            await command_store.acknowledge(
                ack, expected_worker_id=worker_id, expected_incarnation=incarnation,
                transport_owner_id=uuid.uuid4(),
            )

    asyncio.run(accept_exact_ack_size())


@pytest.mark.db
def test_accounts_job_handoff_needs_active_owner_not_browser_session(monkeypatch, portal_runner):
    root_keys(monkeypatch)
    monkeypatch.setenv("AUTH_MODE", "none")
    get_settings.cache_clear()
    assert portal_runner(db.connect()) is True
    worker_id = f"job-accounts-handoff-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"job-accounts-handoff-model-{uuid.uuid4()}"
    job_id = portal_runner(seed_queued_job(model_id))
    portal_runner(db.enable_accounts_mode(db.session_factory))
    portal_runner(db.dispose())
    monkeypatch.setenv("AUTH_MODE", "accounts")
    get_settings.cache_clear()

    try:
        with protocol6_client(worker_id) as client:
            client.portal.call(worker_authority.acquire_scheduler_lease)
            with client.websocket_connect("/api/v1/fleet") as worker:
                worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
                worker.receive_json()
                request_work_grant(worker, worker_id, incarnation)
                dispatch = client.portal.start_task_soon(jobs.dispatch, job_id)
                command = worker.receive_json()
                assert dispatch.result() is True
                assert command["type"] == "dispatch_job"
                assert uuid.UUID(command["job_id"]) == job_id
    finally:
        async def clear_install_mode():
            if db.session_factory is None:
                await db.connect(serving=False)
            async with db.session_factory() as session:
                await session.execute(text("DELETE FROM installation_auth_state"))
                await session.commit()
            await db.dispose()

        portal_runner(clear_install_mode())
        get_settings.cache_clear()


@pytest.mark.db
@pytest.mark.parametrize("exhausted_counter", ["command", "dispatch"])
def test_signed63_sentinel_refuses_before_job_claim(monkeypatch, exhausted_counter):
    root_keys(monkeypatch)
    worker_id = f"sequence-limit-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"sequence-limit-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)
            job_id = client.portal.call(seed_queued_job, model_id)

            async def exhaust_counter():
                async with db.session_factory() as session:
                    async with session.begin():
                        if exhausted_counter == "command":
                            await session.execute(
                                text(
                                    "UPDATE worker_connections SET next_command_sequence = :limit "
                                    "WHERE worker_id = :worker_id AND incarnation = :incarnation"
                                ),
                                {"limit": command_store.MAX_SEQUENCE,
                                 "worker_id": worker_id, "incarnation": incarnation},
                            )
                        else:
                            await session.execute(
                                text("UPDATE jobs SET current_dispatch_sequence = :previous "
                                     "WHERE id = :job_id"),
                                {"previous": command_store.MAX_SEQUENCE - 1, "job_id": job_id},
                            )

            client.portal.call(exhaust_counter)
            assert client.portal.call(jobs.dispatch, job_id) is False

            async def inspect_unclaimed():
                async with db.session_factory() as session:
                    job = (await session.execute(
                        text("SELECT state, current_dispatch_sequence FROM jobs WHERE id = :id"),
                        {"id": job_id},
                    )).mappings().one()
                    attempts = await session.scalar(
                        text("SELECT count(*) FROM job_attempts WHERE job_id = :id"),
                        {"id": job_id},
                    )
                    ranges = await session.scalar(
                        text("SELECT count(*) FROM worker_physical_ranges WHERE subject_id = :id"),
                        {"id": job_id},
                    )
                    worker_row = (await session.execute(
                        text("SELECT next_command_sequence FROM worker_connections "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )).mappings().one()
                    return job, attempts, ranges, worker_row

            job, attempts, ranges, worker_row = client.portal.call(inspect_unclaimed)
            assert job["state"] == "queued"
            assert attempts == 0 and ranges == 0
            assert worker_row["next_command_sequence"] == (
                command_store.MAX_SEQUENCE if exhausted_counter == "command" else 1
            )


@pytest.mark.db
def test_desired_revision_sentinel_refuses_before_mutation(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"desired-limit-{uuid.uuid4()}"
    model_id = f"desired-limit-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        worker_ws, worker, grant, opened, browser_ws, browser_ctx, fleet_ctx = (
            open_live_session(client, worker_id, model_id)
        )
        try:
            session_id = uuid.UUID(opened["session_id"])
            attempt_id = uuid.UUID(opened["attempt_id"])
            generation = opened["control_generation"]
            desired_revision = command_store.MAX_SEQUENCE - 1

            async def seed_sentinel_revision():
                async with db.session_factory() as session:
                    async with session.begin():
                        await session.execute(
                            text("UPDATE realtime_sessions SET desired_revision = :revision "
                                 "WHERE id = :session_id"),
                            {"revision": desired_revision, "session_id": session_id},
                        )
                        entity = (await session.execute(
                            text("SELECT browser_owner_id FROM realtime_sessions WHERE id = :id"),
                            {"id": session_id},
                        )).mappings().one()
                        return entity["browser_owner_id"]

            browser_owner_id = client.portal.call(seed_sentinel_revision)
            params = {"prompt": "sentinel"}
            fields = {
                "session_id": str(session_id), "attempt_id": str(attempt_id),
                "control_generation": generation,
                "params_revision": command_store.MAX_SEQUENCE,
                "params": params, "model_id": model_id,
            }
            context = {
                "auth_session_id": None, "browser_owner_id": browser_owner_id,
                "attempt_id": attempt_id, "control_generation": generation,
                "model_id": model_id, "base_desired_revision": desired_revision,
                "desired_revision": command_store.MAX_SEQUENCE, "params": params,
            }

            async def refuse_sentinel_update():
                with pytest.raises(command_store.CommandRefused):
                    await command_store.commit_command(
                        worker_id, uuid.UUID(opened["incarnation"]), "update_session", fields,
                        grant_nonce=uuid.UUID(grant["grant_nonce"]),
                        account_user_id=db.local_user_id, session_context=context,
                    )
                async with db.session_factory() as session:
                    state = (await session.execute(
                        text("SELECT desired_revision, desired_params, params_revision "
                             "FROM realtime_sessions WHERE id = :id"),
                        {"id": session_id},
                    )).mappings().one()
                    rows = await session.scalar(
                        text("SELECT count(*) FROM worker_commands WHERE worker_id = :worker_id "
                             "AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": uuid.UUID(opened["incarnation"])},
                    )
                    return state, rows

            state, rows = client.portal.call(refuse_sentinel_update)
            assert state["desired_revision"] == desired_revision
            assert state["desired_params"] != params
            assert state["params_revision"] == 1
            assert rows == 0
        finally:
            browser_ctx.__exit__(None, None, None)
            fleet_ctx.__exit__(None, None, None)


@pytest.mark.db
def test_retained_capacity_counts_erased_row_below_acknowledged_floor(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"retained-limit-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"retained-limit-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)
            job_id = client.portal.call(seed_queued_job, model_id)
            async def seed_erased_history():
                async with db.session_factory() as session:
                    async with session.begin():
                        await session.execute(
                            text("UPDATE worker_connections SET acknowledged_floor = 1, "
                                 "next_command_sequence = 257 WHERE worker_id = :worker_id "
                                 "AND incarnation = :incarnation"),
                            {"worker_id": worker_id, "incarnation": incarnation},
                        )
                        await session.execute(
                            text(
                                "INSERT INTO worker_commands "
                                "(worker_id, incarnation, command_sequence, command_id, kind, "
                                "body_hash, complete_bytes, state) VALUES (:worker_id, "
                                ":incarnation, :sequence, :command_id, 'dispatch_job', :hash, 0, 'erased')"
                            ),
                            [
                                {"worker_id": worker_id, "incarnation": incarnation,
                                 "sequence": sequence, "command_id": uuid.uuid4(),
                                 "hash": "0" * 64}
                                for sequence in range(1, 257)
                            ],
                        )

            client.portal.call(seed_erased_history)
            assert client.portal.call(jobs.dispatch, job_id) is False

            async def inspect_not_claimed():
                async with db.session_factory() as session:
                    state = await session.scalar(
                        text("SELECT state FROM jobs WHERE id = :id"), {"id": job_id},
                    )
                    attempt_count = await session.scalar(
                        text("SELECT count(*) FROM job_attempts WHERE job_id = :id"),
                        {"id": job_id},
                    )
                    retained_count = await session.scalar(
                        text("SELECT count(*) FROM worker_commands WHERE worker_id = :worker_id "
                             "AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )
                    return state, attempt_count, retained_count

            assert client.portal.call(inspect_not_claimed) == ("queued", 0, 256)


@pytest.mark.db
def test_pending_byte_capacity_counts_all_retained_pending_commands(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"pending-bytes-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"pending-bytes-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)
            job_id = client.portal.call(seed_queued_job, model_id)

            async def seed_full_pending_budget():
                async with db.session_factory() as session:
                    async with session.begin():
                        for sequence in range(1, 5):
                            await session.execute(
                                text(
                                    "INSERT INTO worker_commands "
                                    "(worker_id, incarnation, command_sequence, command_id, kind, "
                                    "body_hash, key_version, encrypted_body, complete_bytes, state, "
                                    "account_user_id, subject_id, subject_sequence, ack_identity_hash) "
                                    "VALUES (:worker_id, :incarnation, :sequence, :command_id, "
                                    "'dispatch_job', :hash, 1, :body, :size, 'pending', :owner, "
                                    ":subject, 1, :identity_hash)"
                                ),
                                {"worker_id": worker_id, "incarnation": incarnation,
                                 "sequence": sequence, "command_id": uuid.uuid4(),
                                 "hash": f"{sequence:064x}", "body": b"x", "size": 1024 * 1024,
                                 "owner": db.local_user_id, "subject": uuid.uuid4(),
                                 "identity_hash": f"{sequence:064x}"},
                            )
                        await session.execute(
                            text("UPDATE worker_connections SET next_command_sequence = 5 "
                                 "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                            {"worker_id": worker_id, "incarnation": incarnation},
                        )

            client.portal.call(seed_full_pending_budget)
            assert client.portal.call(jobs.dispatch, job_id) is False

            async def inspect_full_budget():
                async with db.session_factory() as session:
                    state = await session.scalar(
                        text("SELECT state FROM jobs WHERE id = :id"), {"id": job_id},
                    )
                    command_count = await session.scalar(
                        text("SELECT count(*) FROM worker_commands WHERE worker_id = :worker_id "
                             "AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )
                    attempts = await session.scalar(
                        text("SELECT count(*) FROM job_attempts WHERE job_id = :id"),
                        {"id": job_id},
                    )
                    return state, command_count, attempts

            assert client.portal.call(inspect_full_budget) == ("queued", 4, 0)


@pytest.mark.db
def test_protocol6_a_cancel_erases_the_dispatch_before_a_claim_error_surfaces(monkeypatch):
    """A cancel erases the claim before an uncertain commit can hand it off."""
    root_keys(monkeypatch)
    worker_id = f"job-order-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"job-order-model-{uuid.uuid4()}"
    real_commit = command_store.commit_command

    async def commit_cancel_then_fail(*args, **kwargs):
        command = await real_commit(*args, **kwargs)
        if args[2] == "dispatch_job":
            await jobs.cancel(command.subject_id)
            raise RuntimeError("connection reset after COMMIT")
        return command

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)

            job_id = client.portal.call(seed_queued_job, model_id)
            client.portal.call(jobs.queues.pop, jobs.JOB_QUEUE)
            monkeypatch.setattr(command_store, "commit_command", commit_cancel_then_fail)
            assert client.portal.call(jobs.dispatch, job_id) is True

            async def inspect_cancelled_claim():
                async with db.session_factory() as session:
                    row = (await session.execute(
                        text(
                            "SELECT state, encrypted_body, complete_bytes FROM worker_commands "
                            "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                            "AND command_sequence = 1"
                        ), {"worker_id": worker_id, "incarnation": incarnation},
                    )).mappings().one()
                    connection = (await session.execute(
                        text(
                            "SELECT acknowledged_floor, closed_at FROM worker_connections "
                            "WHERE worker_id = :worker_id AND incarnation = :incarnation"
                        ), {"worker_id": worker_id, "incarnation": incarnation},
                    )).mappings().one()
                    return row, connection

            erased, connection = client.portal.call(inspect_cancelled_claim)
            assert erased["state"] == "erased"
            assert erased["encrypted_body"] is None
            assert erased["complete_bytes"] == 0
            assert connection["acknowledged_floor"] == 0
            assert connection["closed_at"] is not None


@pytest.mark.db
@pytest.mark.parametrize("head", ["missing", "contiguous"])
def test_missing_gap_freezes_the_floor_and_still_refuses_the_read(monkeypatch, head):
    """An exact ACK on an erased command records a thin known outcome, and a
    removed head keeps the floor frozen and the read refused."""
    root_keys(monkeypatch)
    worker_id = f"journal-gap-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"journal-gap-model-{uuid.uuid4()}"
    committed = []
    original_preflight = command_store.preflight_worker_control

    def capture_command(message, **kwargs):
        committed.append(dict(message))
        return original_preflight(message, **kwargs)

    monkeypatch.setattr(command_store, "preflight_worker_control", capture_command)
    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)
            job_one = client.portal.call(seed_queued_job, model_id)
            job_two = client.portal.call(seed_queued_job, model_id)
            assert client.portal.call(jobs.dispatch, job_one) is True
            command_one = worker.receive_json()
            assert command_one["type"] == "dispatch_job"
            assert client.portal.call(jobs.dispatch, job_two) is True
            command_two = committed[1]
            assert command_one == committed[0]
            assert command_two["job_id"] == str(job_two)
            assert command_two["command_sequence"] == 2

            async def inspect_journal():
                async with db.session_factory() as session:
                    rows = (await session.execute(
                        text(
                            "SELECT command_sequence, state, encrypted_body, key_version, "
                            "complete_bytes, command_id, body_hash, subject_id, subject_sequence, "
                            "ack_identity_hash, ack_status, ack_body "
                            "FROM worker_commands WHERE worker_id = :worker_id "
                            "AND incarnation = :incarnation ORDER BY command_sequence"
                        ),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )).mappings().all()
                    connection = (await session.execute(
                        text(
                            "SELECT acknowledged_floor, next_command_sequence, closed_at "
                            "FROM worker_connections WHERE worker_id = :worker_id "
                            "AND incarnation = :incarnation"
                        ),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )).mappings().one()
                    return rows, connection

            rows, connection = client.portal.call(inspect_journal)
            assert [row["command_sequence"] for row in rows] == [1, 2]
            assert [row["state"] for row in rows] == ["pending", "pending"]
            assert connection["acknowledged_floor"] == 0
            assert connection["next_command_sequence"] == 3
            original = dict(rows[1])

            async def drop_head_and_erase_second():
                async with db.session_factory() as session:
                    async with session.begin():
                        if head == "missing":
                            await session.execute(
                                text("DELETE FROM worker_commands WHERE worker_id = :worker_id "
                                     "AND incarnation = :incarnation AND command_sequence = 1"),
                                {"worker_id": worker_id, "incarnation": incarnation},
                            )
                        await session.execute(
                            text("UPDATE worker_commands SET state = 'erased', "
                                 "encrypted_body = NULL, key_version = NULL, complete_bytes = 0 "
                                 "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                                 "AND command_sequence = 2"),
                            {"worker_id": worker_id, "incarnation": incarnation},
                        )

            client.portal.call(drop_head_and_erase_second)
            rows, connection = client.portal.call(inspect_journal)
            assert connection["acknowledged_floor"] == 0
            assert [row["command_sequence"] for row in rows] == (
                [2] if head == "missing" else [1, 2]
            )
            erased = rows[-1]
            identity = ("command_id", "body_hash", "subject_id", "subject_sequence",
                        "ack_identity_hash")
            assert erased["state"] == "erased"
            assert erased["encrypted_body"] is None
            assert erased["key_version"] is None
            assert erased["complete_bytes"] == 0
            assert {field: erased[field] for field in identity} == {
                field: original[field] for field in identity
            }

            if head == "contiguous":
                pending = client.portal.call(
                    command_store.read_exact_pending, worker_id, incarnation)
                assert pending is not None
                assert decode_control(pending.body) == command_one
                ack_one = {**command_ack_fields(command_one), "type": "command_ack",
                           "status": "accepted"}
                assert client.portal.call(
                    acknowledge_bound, ack_one, worker_id, incarnation).recorded

            ack_two = {**command_ack_fields(command_two), "type": "command_ack",
                       "status": "accepted"}
            result = client.portal.call(acknowledge_bound, ack_two, worker_id, incarnation)
            assert result.recorded is True
            assert result.publication is None

            rows, connection = client.portal.call(inspect_journal)
            if head == "missing":
                assert connection["acknowledged_floor"] == 0
                assert connection["closed_at"] is None
                assert len(rows) == 1
                assert rows[0]["state"] == "acknowledged"
                assert rows[0]["ack_status"] == "accepted"
                assert rows[0]["ack_body"] is not None
                assert rows[0]["encrypted_body"] is None
                assert rows[0]["key_version"] is None
                assert rows[0]["complete_bytes"] == 0
                assert {field: rows[0][field] for field in identity} == {
                    field: original[field] for field in identity
                }
                with pytest.raises(command_store.CommandRefused, match="unresolved gap"):
                    client.portal.call(
                        command_store.read_exact_pending, worker_id, incarnation)
            else:
                assert connection["acknowledged_floor"] == 2
                assert rows == []
