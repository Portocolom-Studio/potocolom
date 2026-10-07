"""Durable protocol 6 worker report receipts and physical work ranges."""

import asyncio
import hashlib
import json
import uuid

import pytest
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect

from app import db, jobs, worker_authority
from app.protocol6 import canonical_bytes, decode_control, validate_message
from test_command_store import (
    command_ack_fields,
    job_done_from_dispatch,
    png_bytes,
    put_upload,
)
from test_fleet_protocol6 import (
    hello,
    manifest,
    protocol6_client,
    request_work_grant,
    root_keys,
    seed_queued_job,
)


@pytest.fixture(autouse=True)
def park_dispatch_loop(monkeypatch):
    async def parked():
        return None

    monkeypatch.setattr(jobs, "dispatch_step", parked)


def checkpoint_from_command(command: dict, report_sequence: int, gpu_ms: int,
                          duration_ms: int = 1000, images: int = 0) -> dict:
    message = {
        "type": "checkpoint",
        "worker_id": command["worker_id"],
        "incarnation": command["incarnation"],
        "region": command["region"],
        "owner_epoch": command["owner_epoch"],
        "job_id": command["job_id"],
        "dispatch_sequence": command["dispatch_sequence"],
        "range_id": command["work_budget"]["range_id"],
        "report_sequence": report_sequence,
        "gpu_ms": gpu_ms,
        "duration_ms": duration_ms,
        "images": images,
    }
    source = {key: value for key, value in message.items() if key != "report_hash"}
    message["report_hash"] = hashlib.sha256(canonical_bytes(source)).hexdigest()
    validate_message(
        message,
        expected_worker_id=command["worker_id"],
        expected_incarnation=uuid.UUID(command["incarnation"]),
    )
    return message


def dispatch_acknowledged(client, worker, worker_id, incarnation, model_id):
    job_id = client.portal.call(seed_queued_job, model_id)
    assert client.portal.call(jobs.dispatch, job_id) is True
    command = worker.receive_json()
    ack = {**command_ack_fields(command), "type": "command_ack", "status": "accepted"}
    worker.send_json(ack)
    return job_id, command


def receive_report_ack(worker, worker_id: str, incarnation: uuid.UUID) -> dict:
    ack = decode_control(worker.receive_text().encode("utf-8"))
    validate_message(ack, expected_worker_id=worker_id, expected_incarnation=incarnation)
    assert ack["type"] == "checkpoint_ack"
    return ack


@pytest.mark.db
def test_dispatch_creates_worker_physical_range(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"range-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"range-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)

            job_id, command = dispatch_acknowledged(
                client, worker, worker_id, incarnation, model_id
            )

            async def read_range():
                async with db.session_factory() as session:
                    return (await session.execute(
                        text(
                            "SELECT kind, state, range_id, dispatch_sequence, subject_id "
                            "FROM worker_physical_ranges WHERE worker_id = :worker_id"
                        ),
                        {"worker_id": worker_id},
                    )).mappings().one()

            row = client.portal.call(read_range)
            assert row["kind"] == "job"
            assert row["state"] == "outstanding"
            assert row["range_id"] == uuid.UUID(command["work_budget"]["range_id"])
            assert row["dispatch_sequence"] == command["dispatch_sequence"]
            assert row["subject_id"] == job_id


@pytest.mark.db
def test_job_done_receipts_and_completes_attempt(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"receipt-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"receipt-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            grant = request_work_grant(worker, worker_id, incarnation)

            job_id, command = dispatch_acknowledged(
                client, worker, worker_id, incarnation, model_id
            )
            put = put_upload(client, command["upload"], png_bytes())
            assert put.status_code == 200

            done = job_done_from_dispatch(command, grant["grant_nonce"])
            worker.send_json(done)
            ack = receive_report_ack(worker, worker_id, incarnation)
            assert ack["status"] == "accepted"
            assert ack["gpu_ms"] == done["gpu_ms"]
            assert ack["images"] == done["images"]

            async def read_rows():
                async with db.session_factory() as session:
                    attempt = (await session.execute(
                        text(
                            "SELECT state, gpu_ms, duration_ms, frames, report_sequence "
                            "FROM job_attempts WHERE job_id = :id"
                        ),
                        {"id": job_id},
                    )).mappings().one()
                    receipts = await session.scalar(
                        text("SELECT count(*) FROM worker_report_receipts "
                             "WHERE range_id = :range_id"),
                        {"range_id": uuid.UUID(command["work_budget"]["range_id"])},
                    )
                    job_state = await session.scalar(
                        text("SELECT state FROM jobs WHERE id = :id"),
                        {"id": job_id},
                    )
                    return attempt, receipts, job_state

            for _ in range(200):
                attempt, receipts, job_state = client.portal.call(read_rows)
                if job_state == "succeeded":
                    break
                client.portal.call(asyncio.sleep, 0.01)
            else:
                raise AssertionError("job never reached succeeded")

            assert attempt["state"] == "completed"
            assert attempt["gpu_ms"] == done["gpu_ms"]
            assert attempt["duration_ms"] == done["duration_ms"]
            assert attempt["frames"] == done["images"]
            assert attempt["report_sequence"] == done["report_sequence"]
            assert receipts == 1


@pytest.mark.db
def test_job_done_replay_and_hash_mismatch(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"replay-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"replay-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            grant = request_work_grant(worker, worker_id, incarnation)

            job_id, command = dispatch_acknowledged(
                client, worker, worker_id, incarnation, model_id
            )
            put = put_upload(client, command["upload"], png_bytes())
            assert put.status_code == 200

            done = job_done_from_dispatch(command, grant["grant_nonce"])
            worker.send_json(done)
            first_ack = worker.receive_text()

            worker.send_json(done)
            second_ack = worker.receive_text()
            assert first_ack == second_ack

            async def receipt_count():
                async with db.session_factory() as session:
                    return await session.scalar(
                        text("SELECT count(*) FROM worker_report_receipts "
                             "WHERE range_id = :range_id"),
                        {"range_id": uuid.UUID(command["work_budget"]["range_id"])},
                    )

            assert client.portal.call(receipt_count) == 1

            changed = {**done, "gpu_ms": done["gpu_ms"] + 1}
            source = {key: value for key, value in changed.items() if key != "report_hash"}
            changed["report_hash"] = hashlib.sha256(canonical_bytes(source)).hexdigest()
            validate_message(
                changed,
                expected_worker_id=worker_id,
                expected_incarnation=incarnation,
            )
            worker.send_json(changed)
            with pytest.raises(WebSocketDisconnect) as closed:
                worker.receive_text()
            assert closed.value.code == 4000

            assert client.portal.call(receipt_count) == 1

            async def stored_hash():
                async with db.session_factory() as session:
                    return await session.scalar(
                        text(
                            "SELECT report_hash FROM worker_report_receipts "
                            "WHERE range_id = :range_id AND report_sequence = :sequence"
                        ),
                        {
                            "range_id": uuid.UUID(command["work_budget"]["range_id"]),
                            "sequence": 1,
                        },
                    )

            assert client.portal.call(stored_hash) == json.loads(first_ack)["report_hash"]


@pytest.mark.db
def test_job_done_wrong_dispatch_token_refused(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"token-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"token-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            grant = request_work_grant(worker, worker_id, incarnation)

            job_id, command = dispatch_acknowledged(
                client, worker, worker_id, incarnation, model_id
            )
            put = put_upload(client, command["upload"], png_bytes())
            assert put.status_code == 200

            done = job_done_from_dispatch(command, grant["grant_nonce"])
            done["dispatch_token"] = "not-the-token"
            source = {key: value for key, value in done.items() if key != "report_hash"}
            done["report_hash"] = hashlib.sha256(canonical_bytes(source)).hexdigest()
            validate_message(
                done,
                expected_worker_id=worker_id,
                expected_incarnation=incarnation,
            )
            worker.send_json(done)
            with pytest.raises(WebSocketDisconnect) as closed:
                worker.receive_text()
            assert closed.value.code == 4000

            async def read_rows():
                async with db.session_factory() as session:
                    attempt = (await session.execute(
                        text("SELECT state FROM job_attempts WHERE job_id = :id"),
                        {"id": job_id},
                    )).mappings().one()
                    receipts = await session.scalar(
                        text("SELECT count(*) FROM worker_report_receipts "
                             "WHERE range_id = :range_id"),
                        {"range_id": uuid.UUID(command["work_budget"]["range_id"])},
                    )
                    job_state = await session.scalar(
                        text("SELECT state FROM jobs WHERE id = :id"),
                        {"id": job_id},
                    )
                    return attempt["state"], receipts, job_state

            attempt_state, receipts, job_state = client.portal.call(read_rows)
            assert receipts == 0
            assert attempt_state == "dispatching"
            assert job_state == "running"


@pytest.mark.db
def test_checkpoint_keeps_range_maxima(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"max-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"max-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)

            job_id, command = dispatch_acknowledged(
                client, worker, worker_id, incarnation, model_id
            )

            high = checkpoint_from_command(command, 1, gpu_ms=9000, duration_ms=8000, images=3)
            worker.send_json(high)
            receive_report_ack(worker, worker_id, incarnation)

            low = checkpoint_from_command(command, 2, gpu_ms=100, duration_ms=50, images=1)
            worker.send_json(low)
            ack = receive_report_ack(worker, worker_id, incarnation)
            assert ack["gpu_ms"] == 9000
            assert ack["images"] == 3
            assert ack["duration_ms"] == 8000

            async def read_range():
                async with db.session_factory() as session:
                    return (await session.execute(
                        text(
                            "SELECT gpu_ms, frames, duration_ms FROM worker_physical_ranges "
                            "WHERE range_id = :range_id"
                        ),
                        {"range_id": uuid.UUID(command["work_budget"]["range_id"])},
                    )).mappings().one()

            row = client.portal.call(read_range)
            assert row["gpu_ms"] == 9000
            assert row["frames"] == 3
            assert row["duration_ms"] == 8000


@pytest.mark.db
def test_stale_report_sequence_gets_refused_ack(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"floor-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"floor-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)

            job_id, command = dispatch_acknowledged(
                client, worker, worker_id, incarnation, model_id
            )

            async def raise_floor():
                async with db.session_factory() as session:
                    await session.execute(
                        text(
                            "UPDATE worker_physical_ranges SET report_floor = 5 "
                            "WHERE range_id = :range_id"
                        ),
                        {"range_id": uuid.UUID(command["work_budget"]["range_id"])},
                    )
                    await session.commit()

            client.portal.call(raise_floor)

            stale = checkpoint_from_command(command, 3, gpu_ms=10)
            worker.send_json(stale)
            ack = receive_report_ack(worker, worker_id, incarnation)
            assert ack["status"] == "refused"
            assert ack["code"] == "stale_report"

            async def receipt_count():
                async with db.session_factory() as session:
                    return await session.scalar(
                        text("SELECT count(*) FROM worker_report_receipts "
                             "WHERE range_id = :range_id"),
                        {"range_id": uuid.UUID(command["work_budget"]["range_id"])},
                    )

            assert client.portal.call(receipt_count) == 0


@pytest.mark.db
def test_a_refused_stale_job_done_does_not_finish_the_job(monkeypatch):
    """A terminal report below the range's floor is refused, and the refusal
    must stop it from reaching the job handlers too."""
    root_keys(monkeypatch)
    worker_id = f"floor-done-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"floor-done-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            grant = request_work_grant(worker, worker_id, incarnation)

            job_id, command = dispatch_acknowledged(
                client, worker, worker_id, incarnation, model_id
            )
            put_upload(client, command["upload"], png_bytes())

            async def raise_floor():
                async with db.session_factory() as session:
                    await session.execute(
                        text("UPDATE worker_physical_ranges SET report_floor = 5 "
                             "WHERE range_id = :range_id"),
                        {"range_id": uuid.UUID(command["work_budget"]["range_id"])},
                    )
                    await session.commit()

            client.portal.call(raise_floor)
            worker.send_json(job_done_from_dispatch(command, grant["grant_nonce"]))
            ack = receive_report_ack(worker, worker_id, incarnation)
            assert ack["status"] == "refused"
            # The handler takes messages one at a time: once this later
            # report is acknowledged, the stale one is fully handled.
            worker.send_json(checkpoint_from_command(command, 6, gpu_ms=10))
            assert receive_report_ack(worker, worker_id, incarnation)["status"] == "accepted"
            assert client.get(f"/api/v1/generations/{job_id}").json()["state"] == "running"
