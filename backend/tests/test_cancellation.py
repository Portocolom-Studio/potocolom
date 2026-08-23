"""Stopping work that is already running.

PostgreSQL is the authority and the worker is told afterwards, so a worker
that never hears changes nothing about whether the job is cancelled. What it
produces after that is discarded, and the GPU time it spent is still charged.
"""

import time
import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import db, jobs
from app.main import app
from app.tables import Asset, Job, UsageEvent
from tests.test_jobs import (
    FLEET_HEADERS, fleet_hello, png_bytes, poll_until, put_upload,
)


@contextmanager
def _held_in_the_queue(client, worker_id):
    """A worker connected, so the model is known, and dispatch paused, so the
    job stays where it was created."""
    with client.websocket_connect("/api/v1/fleet") as worker:
        fleet_hello(worker, worker_id)
        jobs.bump_dispatch_epoch()
        jobs.wait_dispatch_idle()
        try:
            yield worker
        finally:
            jobs.resume_dispatch()


def _create(client, prompt="a house on a hill"):
    created = client.post("/api/v1/generations",
                          json={"model_id": "sd-test", "params": {"prompt": prompt}})
    assert created.status_code == 202
    return created.json()["job_id"]


async def _job(job_id) -> Job:
    async with db.session_factory() as session:
        return await session.get(Job, uuid.UUID(str(job_id)))


async def _assets(job_id) -> list[Asset]:
    async with db.session_factory() as session:
        return list((await session.execute(
            select(Asset).where(Asset.job_id == uuid.UUID(str(job_id))))).scalars().all())


async def _charges(model_id="sd-test") -> int:
    async with db.session_factory() as session:
        return int(await session.scalar(
            select(func.count()).select_from(UsageEvent)
            .where(UsageEvent.model_id == model_id, UsageEvent.kind == "job")) or 0)


def _wait_dispatch(worker):
    dispatch = worker.receive_json()
    assert dispatch["type"] == "dispatch_job"
    return dispatch


@pytest.mark.db
def test_a_queued_job_can_be_called_off_before_any_worker_sees_it():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        with _held_in_the_queue(client, "w-queued"):
            job_id = _create(client)
            assert client.get(f"/api/v1/generations/{job_id}").json()["state"] == "queued"
            assert client.post(f"/api/v1/generations/{job_id}/cancel").status_code == 204
            assert client.get(f"/api/v1/generations/{job_id}").json()["state"] == "cancelled"


@pytest.mark.db
def test_cancelling_twice_says_the_same_thing():
    """The answer to "stop it" and to "it already stopped" is the same to
    whoever asked."""
    with TestClient(app, headers=FLEET_HEADERS) as client:
        with _held_in_the_queue(client, "w-twice"):
            job_id = _create(client)
            assert client.post(f"/api/v1/generations/{job_id}/cancel").status_code == 204
            assert client.post(f"/api/v1/generations/{job_id}/cancel").status_code == 204
            assert client.get(f"/api/v1/generations/{job_id}").json()["state"] == "cancelled"


@pytest.mark.db
def test_a_running_job_is_cancelled_here_first_and_the_worker_told_after():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-cancel")
            job_id = _create(client)
            dispatch = _wait_dispatch(worker)
            poll_until(client, job_id, "running")

            assert client.post(f"/api/v1/generations/{job_id}/cancel").status_code == 204
            # The row first: whatever the worker does next, this is decided.
            assert client.get(f"/api/v1/generations/{job_id}").json()["state"] == "cancelled"
            told = worker.receive_json()
    assert told["type"] == "cancel_job"
    assert told["job_id"] == job_id
    assert told["dispatch_token"] == dispatch["dispatch_token"]


@pytest.mark.db
def test_what_a_cancelled_job_uploads_afterwards_is_discarded():
    """A worker on an older protocol never hears the cancellation and
    finishes. Nothing it produced may reach the library."""
    with TestClient(app, headers=FLEET_HEADERS) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-late")
            job_id = _create(client)
            dispatch = _wait_dispatch(worker)
            poll_until(client, job_id, "running")
            assert client.post(f"/api/v1/generations/{job_id}/cancel").status_code == 204
            assert worker.receive_json()["type"] == "cancel_job"

            worker.send_json({"type": "job_done", "job_id": job_id,
                              "dispatch_token": dispatch["dispatch_token"],
                              "gpu_ms": 1200, "width": 64, "height": 64})
            time.sleep(0.3)
            assert client.get(f"/api/v1/generations/{job_id}").json()["state"] == "cancelled"
            assert client.portal.call(_assets, job_id) == []


@pytest.mark.db
def test_the_gpu_time_a_cancelled_job_spent_is_still_charged():
    """The output is discarded and the time is not: the GPU ran for however
    long it ran before the cancellation reached it."""
    with TestClient(app, headers=FLEET_HEADERS) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-charge")
            before = client.portal.call(_charges)
            job_id = _create(client)
            dispatch = _wait_dispatch(worker)
            poll_until(client, job_id, "running")
            assert client.post(f"/api/v1/generations/{job_id}/cancel").status_code == 204
            assert worker.receive_json()["type"] == "cancel_job"

            worker.send_json({"type": "job_cancelled", "job_id": job_id,
                              "dispatch_token": dispatch["dispatch_token"],
                              "gpu_ms": 850})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if client.portal.call(_charges) > before:
                    break
                time.sleep(0.05)
            charged = client.portal.call(_job, job_id)
    assert charged.gpu_ms == 850
    assert charged.state == "cancelled"


@pytest.mark.db
def test_a_cancelled_job_gives_its_slot_back():
    """Otherwise the fleet loses a slot for every cancellation until the
    worker reconnects."""
    with TestClient(app, headers=FLEET_HEADERS) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-slot")
            job_id = _create(client)
            dispatch = _wait_dispatch(worker)
            poll_until(client, job_id, "running")
            assert client.post(f"/api/v1/generations/{job_id}/cancel").status_code == 204
            assert worker.receive_json()["type"] == "cancel_job"
            worker.send_json({"type": "job_cancelled", "job_id": job_id,
                              "dispatch_token": dispatch["dispatch_token"], "gpu_ms": 10})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if uuid.UUID(job_id) not in jobs.inflight:
                    break
                time.sleep(0.05)
    assert uuid.UUID(job_id) not in jobs.inflight


@pytest.mark.db
def test_a_finished_job_cannot_be_cancelled_after_the_fact():
    """Cancelling a succeeded job would take an asset out of somebody's
    library by changing a state word."""
    with TestClient(app, headers=FLEET_HEADERS) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-done")
            job_id = _create(client)
            dispatch = _wait_dispatch(worker)
            assert put_upload(client, dispatch["upload"],
                              png_bytes(64, 64)).status_code == 200
            worker.send_json({"type": "job_done", "job_id": job_id,
                              "dispatch_token": dispatch["dispatch_token"],
                              "gpu_ms": 40, "width": 64, "height": 64})
            poll_until(client, job_id, "succeeded")
            assert client.post(f"/api/v1/generations/{job_id}/cancel").status_code == 204
            assert client.get(f"/api/v1/generations/{job_id}").json()["state"] == "succeeded"


@pytest.mark.db
def test_only_the_owner_or_an_administrator_can_call_off_a_job():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        with _held_in_the_queue(client, "w-twice"):
            job_id = _create(client)
        assert client.post(f"/api/v1/generations/{uuid.uuid4()}/cancel").status_code == 404
        assert client.get(f"/api/v1/generations/{job_id}").json()["state"] == "queued"
