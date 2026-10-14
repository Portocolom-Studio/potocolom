"""Protocol 6 durable job commands: dispatch, ack, cancel, compaction."""

import asyncio
import hashlib
import struct
import uuid
import zlib

import pytest
from sqlalchemy import text
from starlette.testclient import TestClient

from app import command_store, db, jobs, worker_authority
from app.main import app
from app.protocol6 import canonical_bytes, decode_control, validate_message
from app.tables import Job, Model
from test_jobs import put_upload
from test_fleet_protocol6 import (
    FLEET_HEADERS,
    hello,
    manifest,
    protocol6_client,
    request_work_grant,
    root_keys,
    seed_queued_job,
)


@pytest.fixture(autouse=True)
def park_dispatch_loop(monkeypatch):
    """Every test here dispatches by hand; the lifespan's own dispatch loop
    would otherwise race it for the same queued jobs and slots."""
    async def parked():
        return None

    monkeypatch.setattr(jobs, "dispatch_step", parked)


def png_bytes(width=512, height=512):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\0" + b"\0" * (width * 3) for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def command_ack_fields(command: dict) -> dict:
    return {
        key: command[key]
        for key in (
            "worker_id", "incarnation", "region", "owner_epoch", "lease_id",
            "lease_expires_at", "grant_nonce", "command_sequence", "command_id",
            "body_hash", "job_id", "dispatch_sequence",
        )
        if key in command
    }


async def acknowledge_bound(message, worker_id, incarnation):
    return await command_store.acknowledge(
        message,
        expected_worker_id=worker_id,
        expected_incarnation=incarnation,
        transport_owner_id=worker_authority.TRANSPORT_OWNER_ID,
    )


def job_done_from_dispatch(command: dict, grant_nonce: str) -> dict:
    message = {
        "type": "job_done",
        "worker_id": command["worker_id"],
        "incarnation": command["incarnation"],
        "region": command["region"],
        "lease_id": command["lease_id"],
        "owner_epoch": command["owner_epoch"],
        "grant_nonce": grant_nonce,
        "lease_expires_at": command["lease_expires_at"],
        "job_id": command["job_id"],
        "dispatch_sequence": command["dispatch_sequence"],
        "dispatch_token": command["dispatch_token"],
        "report_sequence": 1,
        "range_id": command["work_budget"]["range_id"],
        "gpu_ms": 1200,
        "duration_ms": 2400,
        "images": 1,
        "physical_complete": True,
        "width": 512,
        "height": 512,
        "category": "other",
    }
    source = {key: value for key, value in message.items() if key != "report_hash"}
    message["report_hash"] = hashlib.sha256(canonical_bytes(source)).hexdigest()
    validate_message(
        message,
        expected_worker_id=command["worker_id"],
        expected_incarnation=uuid.UUID(command["incarnation"]),
    )
    return message


@pytest.mark.db
def test_protocol6_job_dispatch_is_durable_and_encrypted(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"job-claim-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"job-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            assert worker.receive_json()["ready"] is False
            request_work_grant(worker, worker_id, incarnation)

            job_id = client.portal.call(seed_queued_job, model_id)
            dispatch = client.portal.start_task_soon(jobs.dispatch, job_id)
            command = worker.receive_json()
            validate_message(command, expected_worker_id=worker_id,
                             expected_incarnation=incarnation)
            assert command["type"] == "dispatch_job"
            assert command["dispatch_sequence"] == 1

            async def read_rows():
                async with db.session_factory() as session:
                    job = (await session.execute(
                        text("SELECT state, current_dispatch_sequence FROM jobs WHERE id = :id"),
                        {"id": job_id},
                    )).mappings().one()
                    attempt = (await session.execute(
                        text("SELECT state FROM job_attempts WHERE job_id = :id"),
                        {"id": job_id},
                    )).mappings().one()
                    cmd = (await session.execute(
                        text(
                            "SELECT state, encrypted_body FROM worker_commands "
                            "WHERE worker_id = :worker_id AND incarnation = :incarnation"
                        ),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )).mappings().one()
                    return job, attempt, cmd

            job_row, attempt_row, cmd_row = client.portal.call(read_rows)
            assert job_row["state"] == "running"
            assert job_row["current_dispatch_sequence"] == 1
            assert attempt_row["state"] == "dispatching"
            assert cmd_row["state"] == "pending"
            assert cmd_row["encrypted_body"] is not None
            pending = client.portal.call(command_store.read_exact_pending, worker_id, incarnation)
            assert pending is not None
            assert decode_control(pending.body) == command
            assert b"dispatch_job" not in cmd_row["encrypted_body"]
            assert dispatch.result() is True


@pytest.mark.db
def test_protocol6_command_ack_delivers_the_next_job(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"job-ack-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"job-ack-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)

            job_one = client.portal.call(seed_queued_job, model_id)
            job_two = client.portal.call(seed_queued_job, model_id)
            first = client.portal.call(jobs.dispatch, job_one)
            assert first is True
            command = worker.receive_json()
            ack = {**command_ack_fields(command), "type": "command_ack", "status": "accepted"}
            recorded = client.portal.call(acknowledge_bound, ack, worker_id, incarnation)
            assert recorded.recorded is True
            dispatched = client.portal.start_task_soon(jobs.dispatch, job_two)
            second = worker.receive_json()
            assert dispatched.result() is True
            assert second["type"] == "dispatch_job"
            assert second["job_id"] == str(job_two)
            assert second["command_sequence"] > command["command_sequence"]


@pytest.mark.db
def test_protocol6_job_done_completes_the_job(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"job-done-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"job-done-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            grant = request_work_grant(worker, worker_id, incarnation)

            job_id = client.portal.call(seed_queued_job, model_id)
            assert client.portal.call(jobs.dispatch, job_id) is True
            command = worker.receive_json()
            ack = {**command_ack_fields(command), "type": "command_ack", "status": "accepted"}

            upload = command["upload"]
            put = put_upload(client, upload, png_bytes())
            assert put.status_code == 200

            def missing_key_ring():
                raise command_store.KeyRingError("key is unavailable")

            monkeypatch.setattr(command_store, "get_key_ring", missing_key_ring)
            done = job_done_from_dispatch(command, grant["grant_nonce"])
            worker.send_json(done)
            report_ack = worker.receive_json()
            assert report_ack["type"] == "checkpoint_ack"
            assert report_ack["status"] == "accepted"

            async def retired_head():
                async with db.session_factory() as session:
                    row = (await session.execute(
                        text(
                            "SELECT state, encrypted_body FROM worker_commands "
                            "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                            "AND command_sequence = :sequence"
                        ),
                        {"worker_id": worker_id, "incarnation": incarnation,
                         "sequence": command["command_sequence"]},
                    )).mappings().one()
                    connection = (await session.execute(
                        text(
                            "SELECT closed_at FROM worker_connections "
                            "WHERE worker_id = :worker_id AND incarnation = :incarnation"
                        ),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )).scalar_one()
                    return row["state"], row["encrypted_body"], connection

            assert client.portal.call(retired_head) == ("erased", None, None)
            worker.send_json(ack)

            async def wait_consumed():
                for _ in range(200):
                    async with db.session_factory() as session:
                        floor = await session.scalar(
                            text(
                                "SELECT acknowledged_floor FROM worker_connections "
                                "WHERE worker_id = :worker_id AND incarnation = :incarnation"
                            ),
                            {"worker_id": worker_id, "incarnation": incarnation},
                        )
                    if floor >= command["command_sequence"]:
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("late dispatch ACK did not consume the erased head")

            client.portal.call(wait_consumed)

            async def wait_succeeded():
                for _ in range(200):
                    async with db.session_factory() as session:
                        state = await session.scalar(
                            text("SELECT state FROM jobs WHERE id = :id"),
                            {"id": job_id},
                        )
                    if state == "succeeded":
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("job never reached succeeded")

            client.portal.call(wait_succeeded)


@pytest.mark.db
def test_protocol6_cancel_job_is_durable(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"job-cancel-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"job-cancel-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)

            job_id = client.portal.call(seed_queued_job, model_id)
            assert client.portal.call(jobs.dispatch, job_id) is True
            command = worker.receive_json()

            cancelled = client.post(f"/api/v1/generations/{job_id}/cancel")
            assert cancelled.status_code == 204

            async def inspect_cancel_erasure():
                async with db.session_factory() as session:
                    row = (await session.execute(
                        text(
                            "SELECT state, encrypted_body, key_version, complete_bytes "
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

            erased, connection = client.portal.call(inspect_cancel_erasure)
            assert erased["state"] == "erased"
            assert erased["encrypted_body"] is None
            assert erased["key_version"] is None
            assert erased["complete_bytes"] == 0
            assert connection["acknowledged_floor"] == 0
            assert connection["closed_at"] is not None
            ack = {**command_ack_fields(command), "type": "command_ack", "status": "accepted"}
            result = client.portal.call(acknowledge_bound, ack, worker_id, incarnation)
            assert result.recorded is True
            assert result.publication is None


@pytest.mark.db
def test_protocol6_worker_without_grant_gets_no_job(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"job-nogrant-{uuid.uuid4()}"
    model_id = f"job-nogrant-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)]))
            worker.receive_json()
            job_id = client.portal.call(seed_queued_job, model_id)
            assert jobs.pick_job_worker(model_id) is None
            assert client.portal.call(jobs.dispatch, job_id) is False
            state = client.get(f"/api/v1/generations/{job_id}").json()["state"]
            assert state == "queued"


@pytest.mark.db
def test_protocol6_acknowledged_commands_compact(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"journal-compact-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    grant_nonce = uuid.uuid4()

    with TestClient(app, headers=FLEET_HEADERS) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)

        async def seed_worker():
            async with db.session_factory() as session:
                lease = (await session.execute(
                    text(
                        "SELECT owner_epoch, lease_id, expires_at FROM scheduler_leases "
                        "WHERE region = :region"
                    ),
                    {"region": worker_authority.REGION},
                )).mappings().one()
                await session.execute(
                    text(
                        "INSERT INTO worker_connections "
                        "(worker_id, incarnation, transport_owner_id, region, owner_epoch, "
                        "lease_id, lease_expires_at, grant_nonce, grant_expires_at, grant_ready, "
                        "protocol_version, realtime_slots, capabilities, manifests, device, "
                        "memory_mode, execution_envelope, envelope_revision, lifecycle, "
                        "last_seen_at, expires_at, next_command_sequence, acknowledged_floor) "
                        "VALUES (:worker_id, :incarnation, :transport_owner_id, :region, "
                        ":owner_epoch, :lease_id, :lease_expires_at, :grant_nonce, "
                        "clock_timestamp() + interval '10 seconds', true, 6, 1, '[]'::jsonb, "
                        "'[]'::jsonb, 'cpu-test', 'offload', '{}'::jsonb, 1, 'ready', "
                        "clock_timestamp(), clock_timestamp() + interval '90 seconds', 1, 0)"
                    ),
                    {
                        "worker_id": worker_id,
                        "incarnation": incarnation,
                        "transport_owner_id": worker_authority.TRANSPORT_OWNER_ID,
                        "region": worker_authority.REGION,
                        "owner_epoch": lease["owner_epoch"],
                        "lease_id": lease["lease_id"],
                        "lease_expires_at": lease["expires_at"],
                        "grant_nonce": grant_nonce,
                    },
                )
                await session.commit()

        client.portal.call(seed_worker)
        retained_limit = 3
        monkeypatch.setattr(command_store, "MAX_RETAINED_COMMANDS", retained_limit)
        dispatch_token = "ascii-cancel-token"

        async def commit_cancel() -> None:
            job_id = uuid.uuid4()
            async with db.session_factory() as session:
                model = await session.get(Model, "compact-model")
                if model is None:
                    session.add(Model(
                        id="compact-model", name="compact", capabilities=["text_to_image"],
                        parameters_schema={"type": "object"}, min_vram_gb=0,
                    ))
                    await session.flush()
                session.add(Job(
                    id=job_id, user_id=db.local_user_id, model_id="compact-model",
                    params={"prompt": "x"}, state="cancelled", attempt=1,
                    current_dispatch_sequence=1,
                ))
                await session.execute(
                    text(
                        "INSERT INTO job_attempts "
                        "(job_id, dispatch_sequence, command_id, worker_id, incarnation, "
                        "owner_epoch, state, dispatch_token_hash, range_id) "
                        "VALUES (:job_id, 1, :command_id, :worker_id, :incarnation, "
                        "(SELECT owner_epoch FROM worker_connections WHERE worker_id = :worker_id "
                        "AND incarnation = :incarnation), 'dispatching', :token_hash, :range_id)"
                    ),
                    {
                        "job_id": job_id,
                        "command_id": uuid.uuid4(),
                        "worker_id": worker_id,
                        "incarnation": incarnation,
                        "token_hash": hashlib.sha256(dispatch_token.encode()).hexdigest(),
                        "range_id": uuid.uuid4(),
                    },
                )
                await session.commit()
            await command_store.commit_command(
                worker_id,
                incarnation,
                "cancel_job",
                {
                    "job_id": str(job_id),
                    "dispatch_sequence": 1,
                    "dispatch_token": dispatch_token,
                },
                grant_nonce=grant_nonce,
                account_user_id=db.local_user_id,
            )

        async def acknowledge_pending() -> None:
            pending = await command_store.read_exact_pending(worker_id, incarnation)
            assert pending is not None
            decoded = decode_control(pending.body)
            ack = {**command_ack_fields(decoded), "type": "command_ack", "status": "accepted"}
            assert (await acknowledge_bound(ack, worker_id, incarnation)).recorded

        async def run_compaction():
            for _ in range(5):
                await commit_cancel()
                await acknowledge_pending()

        async def count_rows():
            async with db.session_factory() as session:
                return await session.scalar(
                    text(
                        "SELECT count(*) FROM worker_commands "
                        "WHERE worker_id = :worker_id AND incarnation = :incarnation"
                    ),
                    {"worker_id": worker_id, "incarnation": incarnation},
                )

        client.portal.call(run_compaction)
        retained = client.portal.call(count_rows)
        assert retained <= retained_limit


@pytest.mark.db
def test_protocol6_an_ack_that_does_not_match_its_command_is_ignored(monkeypatch):
    """An ACK must name the exact command it acknowledges. One that names
    another job's dispatch leaves the command pending, so a worker cannot
    clear work it was never sent."""
    root_keys(monkeypatch)
    worker_id = f"job-badack-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"job-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            assert worker.receive_json()["ready"] is False
            request_work_grant(worker, worker_id, incarnation)

            job_id = client.portal.call(seed_queued_job, model_id)
            client.portal.start_task_soon(jobs.dispatch, job_id)
            command = worker.receive_json()
            ack = {**command_ack_fields(command), "type": "command_ack", "status": "accepted"}
            wrong = {**ack, "job_id": str(uuid.uuid4())}
            validate_message(wrong)

            assert not client.portal.call(
                acknowledge_bound, wrong, worker_id, incarnation
            ).recorded
            assert client.portal.call(
                command_store.read_exact_pending, worker_id, incarnation) is not None
            assert client.portal.call(
                acknowledge_bound, ack, worker_id, incarnation
            ).recorded
            assert client.portal.call(
                command_store.read_exact_pending, worker_id, incarnation) is None


@pytest.mark.db
def test_protocol6_a_cancel_landing_right_after_the_claim_reaches_the_worker(monkeypatch):
    """A cancel that wins before first delivery erases the undispatched body."""
    root_keys(monkeypatch)
    worker_id = f"job-gap-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"job-gap-model-{uuid.uuid4()}"
    real_commit = command_store.commit_command

    async def commit_then_cancel(*args, **kwargs):
        command = await real_commit(*args, **kwargs)
        if args[2] == "dispatch_job":
            await jobs.cancel(command.subject_id)
        return command

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)

            job_id = client.portal.call(seed_queued_job, model_id)
            monkeypatch.setattr(command_store, "commit_command", commit_then_cancel)
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
            with pytest.raises(command_store.CommandRefused):
                client.portal.call(command_store.read_exact_pending, worker_id, incarnation)


@pytest.mark.db
def test_protocol6_a_pending_command_replays_after_the_grant_renews(monkeypatch):
    """Replay sends the exact stored bytes, so a newer grant nonce on the
    connection must not make the pending command unreadable."""
    root_keys(monkeypatch)
    worker_id = f"job-replay-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"job-replay-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)

            job_id = client.portal.call(seed_queued_job, model_id)
            assert client.portal.call(jobs.dispatch, job_id) is True
            command = worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)

            pending = client.portal.call(
                command_store.read_exact_pending, worker_id, incarnation)
            assert pending is not None
            assert decode_control(pending.body) == command

            async def register_replay():
                return await command_store.register_delivery(
                    pending,
                    session_factory=db.session_factory,
                    worker_id=worker_id,
                    incarnation=incarnation,
                    transport_owner_id=worker_authority.TRANSPORT_OWNER_ID,
                    register=lambda: True,
                )

            assert client.portal.call(register_replay)


@pytest.mark.db
def test_protocol6_a_claim_that_rolled_back_frees_the_slot_and_requeues(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"job-error-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"job-error-model-{uuid.uuid4()}"

    async def broken(*_args, **_kwargs):
        raise RuntimeError("database went away")


    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)

            job_id = client.portal.call(seed_queued_job, model_id)
            monkeypatch.setattr(command_store, "commit_command", broken)
            # As the dispatch pass does: the hint leaves the queue first.
            popped = client.portal.call(jobs.queues.pop, jobs.JOB_QUEUE)
            assert popped is not None and popped.id == str(job_id)
            assert client.portal.call(jobs.dispatch, job_id) is True
            from app import realtime
            assert job_id not in jobs.inflight
            assert realtime.workers[worker_id].jobs_in_flight == 0
            # No attempt row, so the claim rolled back: requeue_or_fail on the
            # still-queued row puts its hint back.
            assert job_id in jobs.lost_jobs
            jobs.lost_jobs.remove(job_id)
            client.portal.call(jobs.requeue_or_fail, job_id, "test")
            heap = jobs.queues._heaps.get(jobs.JOB_QUEUE, [])
            assert [hint.id for hint in heap].count(str(job_id)) == 1


@pytest.mark.db
def test_protocol6_a_claim_that_committed_before_its_error_is_still_delivered(monkeypatch):
    """The error surfaces after COMMIT. The claim is durable, so the job keeps
    its entry and the worker gets the pending command; it is not retried."""
    root_keys(monkeypatch)
    worker_id = f"job-late-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"job-late-model-{uuid.uuid4()}"
    real_commit = command_store.commit_command

    async def commit_then_fail(*args, **kwargs):
        await real_commit(*args, **kwargs)
        raise RuntimeError("connection reset after COMMIT")

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)

            job_id = client.portal.call(seed_queued_job, model_id)
            client.portal.call(jobs.queues.pop, jobs.JOB_QUEUE)
            monkeypatch.setattr(command_store, "commit_command", commit_then_fail)
            assert client.portal.call(jobs.dispatch, job_id) is True
            command = worker.receive_json()
            assert command["type"] == "dispatch_job"
            assert command["job_id"] == str(job_id)
            assert job_id in jobs.inflight
            assert jobs.inflight[job_id].dispatch_sequence == command["dispatch_sequence"]
            assert job_id not in jobs.lost_jobs


@pytest.mark.db
def test_protocol6_a_claim_whose_outcome_cannot_be_read_keeps_its_entry(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"job-unknown-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"job-unknown-model-{uuid.uuid4()}"

    async def broken(*_args, **_kwargs):
        raise RuntimeError("database went away")

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)

            job_id = client.portal.call(seed_queued_job, model_id)
            client.portal.call(jobs.queues.pop, jobs.JOB_QUEUE)
            monkeypatch.setattr(command_store, "commit_command", broken)
            monkeypatch.setattr(jobs, "_claim_committed", broken)
            assert client.portal.call(jobs.dispatch, job_id) is True
            # The stall sweep owns it now: entry and slot stay.
            assert job_id in jobs.inflight
            assert job_id not in jobs.lost_jobs
            jobs.inflight.pop(job_id)
