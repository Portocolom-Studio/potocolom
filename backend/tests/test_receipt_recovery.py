"""Restart recovery for protocol 6 jobs with committed worker report receipts."""

import asyncio
import hashlib
import time
import uuid

import pytest
from sqlalchemy import text
from starlette.testclient import TestClient

from app import db, jobs
from app.main import app
from app.protocol6 import canonical_bytes, validate_message
from test_command_store import job_done_from_dispatch, png_bytes, put_upload
from test_fleet_protocol6 import (
    hello,
    manifest,
    protocol6_client,
    request_work_grant,
    root_keys,
)
from test_jobs import FLEET_HEADERS, fleet_hello
from test_worker_reports import (
    checkpoint_from_command,
    dispatch_acknowledged,
    receive_report_ack,
)

@pytest.fixture(autouse=True)
def park_dispatch_loop(monkeypatch):
    async def parked():
        return None

    monkeypatch.setattr(jobs, "dispatch_step", parked)


def worker_authority_acquire():
    from app import worker_authority

    return worker_authority.acquire_scheduler_lease()


def job_failed_from_dispatch(command: dict, grant_nonce: str) -> dict:
    message = {
        "type": "job_failed",
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
        "gpu_ms": 500,
        "duration_ms": 800,
        "images": 0,
        "physical_complete": True,
        "reason": "boom",
        "failure_code": "generation_failed",
    }
    source = {key: value for key, value in message.items() if key != "report_hash"}
    message["report_hash"] = hashlib.sha256(canonical_bytes(source)).hexdigest()
    validate_message(
        message,
        expected_worker_id=command["worker_id"],
        expected_incarnation=uuid.UUID(command["incarnation"]),
    )
    return message


async def queue_contains_job(job_id: uuid.UUID) -> bool:
    heap = getattr(jobs.queues, "_heaps", {}).get(jobs.JOB_QUEUE, [])
    return any(hint.id == str(job_id) for hint in heap)


async def wait_for_attempt_state(job_id: uuid.UUID, state: str) -> None:
    for _ in range(300):
        async with db.session_factory() as session:
            row = await session.scalar(
                text("SELECT state FROM job_attempts WHERE job_id = :id"),
                {"id": job_id},
            )
        if row == state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"attempt for {job_id} never reached state {state}")


async def remove_queue_hint(job_id: uuid.UUID) -> None:
    heap = getattr(jobs.queues, "_heaps", {}).get(jobs.JOB_QUEUE)
    if not heap:
        return
    jobs.queues._heaps[jobs.JOB_QUEUE] = [
        hint for hint in heap if hint.id != str(job_id)
    ]


async def await_blob_cleanup() -> None:
    for _ in range(500):
        pending = list(jobs._blob_cleanup_tasks)
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0.01)
    raise AssertionError("blob cleanup tasks did not finish")


async def restart_recover(job_id: uuid.UUID) -> None:
    jobs.inflight.pop(job_id, None)
    jobs.live_progress.pop(job_id, None)
    jobs.last_progress_at.pop(job_id, None)
    await jobs.recover()
    await await_blob_cleanup()


def v6_crash_after_receipt(
    client,
    monkeypatch,
    worker_id: str,
    *,
    attempt_terminal_state: str,
    before_report,
    send_report,
) -> tuple[uuid.UUID, dict]:
    root_keys(monkeypatch)
    incarnation = uuid.uuid4()
    model_id = f"recover-model-{uuid.uuid4()}"

    async def noop(_worker, _control):
        return None

    monkeypatch.setattr(jobs, "on_worker_message", noop)

    client.portal.call(worker_authority_acquire)
    with client.websocket_connect("/api/v1/fleet") as worker:
        worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
        worker.receive_json()
        grant = request_work_grant(worker, worker_id, incarnation)

        job_id, command = dispatch_acknowledged(
            client, worker, worker_id, incarnation, model_id
        )
        client.portal.call(remove_queue_hint, job_id)
        before_report(client, command)
        send_report(worker, command, grant)
        client.portal.call(wait_for_attempt_state, job_id, attempt_terminal_state)
        client.portal.call(restart_recover, job_id)
        return job_id, command


@pytest.mark.db
def test_recover_receipted_job_done_succeeds(monkeypatch):
    uploaded = png_bytes(320, 240)
    digest = hashlib.sha256(uploaded).hexdigest()

    def before_report(client, command):
        assert put_upload(client, command["upload"], uploaded).status_code == 200

    def send_report(worker, command, grant):
        worker.send_json(job_done_from_dispatch(command, grant["grant_nonce"]))
        receive_report_ack(worker, command["worker_id"], uuid.UUID(command["incarnation"]))

    worker_id = f"done-{uuid.uuid4()}"
    with protocol6_client(worker_id) as client:
        job_id, _command = v6_crash_after_receipt(
            client,
            monkeypatch,
            worker_id,
            attempt_terminal_state="completed",
            before_report=before_report,
            send_report=send_report,
        )

        job = client.get(f"/api/v1/generations/{job_id}").json()
        assert job["state"] == "succeeded"
        assert job["attempt"] == 1
        assert job["gpu_ms"] == 1200
        assert client.portal.call(queue_contains_job, job_id) is False

        async def asset_rows():
            async with db.session_factory() as session:
                return list((await session.execute(
                    text("SELECT mime FROM assets WHERE job_id = :id ORDER BY mime"),
                    {"id": job_id},
                )).scalars().all())

        assert client.portal.call(asset_rows) == ["image/png"]

        asset = job["assets"][0]
        body = client.get(asset["url"]).content
        assert hashlib.sha256(body).hexdigest() == digest

        async def user_id_for_job():
            async with db.session_factory() as session:
                return await session.scalar(
                    text("SELECT user_id FROM jobs WHERE id = :id"),
                    {"id": job_id},
                )

        user_id = client.portal.call(user_id_for_job)
        dispatch_key, _ = jobs.dispatch_keys_for_attempt(user_id, job_id, 1)
        storage = jobs.get_storage()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and storage.path(dispatch_key).exists():
            time.sleep(0.02)
        assert not storage.path(dispatch_key).exists()


@pytest.mark.db
def test_recover_receipted_job_failed(monkeypatch):
    def send_report(worker, command, grant):
        worker.send_json(job_failed_from_dispatch(command, grant["grant_nonce"]))
        receive_report_ack(worker, command["worker_id"], uuid.UUID(command["incarnation"]))

    worker_id = f"done-{uuid.uuid4()}"
    with protocol6_client(worker_id) as client:
        job_id, _command = v6_crash_after_receipt(
            client,
            monkeypatch,
            worker_id,
            attempt_terminal_state="failed",
            before_report=lambda _client, _command: None,
            send_report=send_report,
        )

        job = client.get(f"/api/v1/generations/{job_id}").json()
        assert job["state"] == "failed"
        assert job["attempt"] == 1
        assert client.portal.call(queue_contains_job, job_id) is False


@pytest.mark.db
def test_recover_receipted_job_done_missing_output_fails(monkeypatch):
    def send_report(worker, command, grant):
        worker.send_json(job_done_from_dispatch(command, grant["grant_nonce"]))
        receive_report_ack(worker, command["worker_id"], uuid.UUID(command["incarnation"]))

    worker_id = f"done-{uuid.uuid4()}"
    with protocol6_client(worker_id) as client:
        job_id, _command = v6_crash_after_receipt(
            client,
            monkeypatch,
            worker_id,
            attempt_terminal_state="completed",
            before_report=lambda _client, _command: None,
            send_report=send_report,
        )

        job = client.get(f"/api/v1/generations/{job_id}").json()
        assert job["state"] == "failed"
        assert job["failure_reason"] == "worker output was missing or invalid"
        assert job["attempt"] == 1
        assert client.portal.call(queue_contains_job, job_id) is False


@pytest.mark.db
def test_recover_checkpoint_only_requeues(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"ckpt-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"ckpt-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority_acquire)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)

            job_id, command = dispatch_acknowledged(
                client, worker, worker_id, incarnation, model_id
            )
            client.portal.call(remove_queue_hint, job_id)
            worker.send_json(checkpoint_from_command(command, 1, gpu_ms=100))
            receive_report_ack(worker, worker_id, incarnation)

            client.portal.call(restart_recover, job_id)

        job = client.get(f"/api/v1/generations/{job_id}").json()
        assert job["state"] == "queued"
        assert job["attempt"] == 2
        assert client.portal.call(queue_contains_job, job_id) is True


@pytest.mark.db
def test_recover_protocol5_running_requeues():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-p5-recover")
            created = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "running"}},
            )
            assert created.status_code == 202
            job_id = uuid.UUID(created.json()["job_id"])
            assert client.portal.call(jobs.dispatch, job_id) is True
            dispatch = worker.receive_json()
            assert dispatch["job_id"] == str(job_id)
            client.portal.call(remove_queue_hint, job_id)

        client.portal.call(restart_recover, job_id)
        job = client.get(f"/api/v1/generations/{job_id}").json()
        assert job["state"] == "queued"
        assert job["attempt"] == 2
        assert client.portal.call(queue_contains_job, job_id) is True


@pytest.mark.db
def test_recover_without_receipt_requeues(monkeypatch):
    """A completed attempt whose receipt row was removed must not be settled."""
    root_keys(monkeypatch)
    worker_id = f"noreceipt-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"noreceipt-model-{uuid.uuid4()}"

    async def noop(_worker, _control):
        return None

    monkeypatch.setattr(jobs, "on_worker_message", noop)

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority_acquire)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            grant = request_work_grant(worker, worker_id, incarnation)

            job_id, command = dispatch_acknowledged(
                client, worker, worker_id, incarnation, model_id
            )
            client.portal.call(remove_queue_hint, job_id)
            put_upload(client, command["upload"], png_bytes())
            worker.send_json(job_done_from_dispatch(command, grant["grant_nonce"]))
            receive_report_ack(worker, worker_id, incarnation)
            client.portal.call(wait_for_attempt_state, job_id, "completed")

            async def drop_receipt():
                async with db.session_factory() as session:
                    await session.execute(
                        text("DELETE FROM worker_report_receipts WHERE range_id = :range_id"),
                        {"range_id": uuid.UUID(command["work_budget"]["range_id"])},
                    )
                    await session.commit()

            client.portal.call(drop_receipt)
            client.portal.call(restart_recover, job_id)

        job = client.get(f"/api/v1/generations/{job_id}").json()
        assert job["state"] == "queued"
        assert job["attempt"] == 2
        assert client.portal.call(queue_contains_job, job_id) is True


@pytest.mark.db
def test_recover_falls_back_to_a_retry_when_storage_fails(monkeypatch):
    """Startup must complete even when storage errors during receipt
    recovery: the job gets the retry it got before receipts existed."""
    from app.storage import get_storage

    async def broken(_key):
        raise OSError("storage unavailable")

    def break_storage(_client, _command):
        monkeypatch.setattr(get_storage(), "image_info", broken)

    def send_report(worker, command, grant):
        worker.send_json(job_done_from_dispatch(command, grant["grant_nonce"]))
        receive_report_ack(worker, command["worker_id"], uuid.UUID(command["incarnation"]))

    worker_id = f"done-{uuid.uuid4()}"
    with protocol6_client(worker_id) as client:
        job_id, _command = v6_crash_after_receipt(
            client,
            monkeypatch,
            worker_id,
            attempt_terminal_state="completed",
            before_report=break_storage,
            send_report=send_report,
        )

        job = client.get(f"/api/v1/generations/{job_id}").json()
        assert job["state"] == "queued"
        assert job["attempt"] == 2
