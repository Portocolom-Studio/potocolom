"""The job dispatch and history flow (#16), driven with a fake worker over the
real fleet WebSocket. Real inference is the worker's side (worker/tests)."""

import asyncio
import time
import uuid
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.realtime import PROTOCOL_VERSION
from app.tables import Job, Model

MANIFEST = {
    "id": "sd-test",
    "name": "SD Test",
    "capabilities": ["text_to_image"],
    "parameters": {
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
        "required": ["prompt"],
    },
    "min_vram_gb": 0,
}


def fleet_hello(ws, worker_id):
    ws.send_json({"type": "hello", "protocol_version": PROTOCOL_VERSION,
                  "worker_id": worker_id, "models": [MANIFEST], "realtime_slots": 1})
    assert ws.receive_json()["type"] == "registered"


def poll_until(client, job_id, state, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/generations/{job_id}").json()
        if job["state"] == state:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached {state}")


@pytest.mark.db
def test_generation_end_to_end():
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-jobs")

            models = client.get("/api/v1/models").json()
            assert any(m["id"] == "sd-test" for m in models)

            created = client.post("/api/v1/generations",
                                  json={"model_id": "sd-test",
                                        "params": {"prompt": "a lighthouse"}})
            assert created.status_code == 202
            job_id = created.json()["job_id"]

            dispatch = worker.receive_json()
            assert dispatch["type"] == "dispatch_job"
            assert dispatch["job_id"] == job_id
            assert dispatch["params"] == {"prompt": "a lighthouse"}

            upload_path = urlsplit(dispatch["upload"]["url"]).path
            assert client.put(upload_path, content=b"webp-bytes").status_code == 200

            worker.send_json({"type": "job_progress", "job_id": job_id, "progress": 0.5})
            worker.send_json({"type": "job_done", "job_id": job_id,
                              "gpu_ms": 1234, "width": 512, "height": 512})

            job = poll_until(client, job_id, "succeeded")
            assert job["gpu_ms"] == 1234
            asset = job["assets"][0]
            assert asset["width"] == 512
            assert client.get(urlsplit(asset["url"]).path).content == b"webp-bytes"

            history = client.get("/api/v1/generations").json()
            assert any(entry["id"] == job_id for entry in history)

            # The event stream replays the terminal state and ends.
            events = client.get(f"/api/v1/generations/{job_id}/events")
            assert "succeeded" in events.text


@pytest.mark.db
def test_unknown_model_and_invalid_params():
    with TestClient(app) as client:
        missing = client.post("/api/v1/generations", json={"model_id": "nope", "params": {}})
        assert missing.status_code == 404
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-validate")
            invalid = client.post("/api/v1/generations",
                                  json={"model_id": "sd-test", "params": {}})
            assert invalid.status_code == 422  # prompt is required by the manifest schema


@pytest.mark.db
def test_worker_loss_requeues_once():
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-dies")
            job_id = client.post("/api/v1/generations",
                                 json={"model_id": "sd-test",
                                       "params": {"prompt": "retry me"}}).json()["job_id"]
            assert worker.receive_json()["job_id"] == job_id
        # The worker died mid job: the job gets its one retry on the next worker.
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-heals")
            redispatch = worker.receive_json()
            assert redispatch["job_id"] == job_id
            worker.send_json({"type": "job_failed", "job_id": job_id, "reason": "boom"})
            poll_until(client, job_id, "failed")


async def _seed_recover_jobs() -> tuple[uuid.UUID, uuid.UUID]:
    assert db.local_user_id is not None
    assert db.session_factory is not None
    queued_id = uuid.uuid4()
    running_id = uuid.uuid4()
    async with db.session_factory() as session:
        if await session.get(Model, "sd-test") is None:
            session.add(Model(
                id="sd-test",
                name="SD Test",
                capabilities=["text_to_image"],
                parameters_schema=MANIFEST["parameters"],
                min_vram_gb=0,
            ))
        session.add(Job(
            id=queued_id,
            user_id=db.local_user_id,
            model_id="sd-test",
            params={"prompt": "queued"},
            state="queued",
            attempt=1,
        ))
        session.add(Job(
            id=running_id,
            user_id=db.local_user_id,
            model_id="sd-test",
            params={"prompt": "running"},
            state="running",
            attempt=1,
        ))
        await session.commit()
    return queued_id, running_id


def poll_until_attempt(client, job_id, attempt: int, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/generations/{job_id}").json()
        if job.get("attempt") == attempt:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached attempt {attempt}")


@pytest.mark.db
def test_stalled_job_requeues_once(monkeypatch):
    monkeypatch.setenv("JOB_STALL_SECONDS", "0.05")
    from app.settings import get_settings
    get_settings.cache_clear()
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-stall")
            job_id = client.post("/api/v1/generations",
                                 json={"model_id": "sd-test",
                                       "params": {"prompt": "stall"}}).json()["job_id"]
            assert worker.receive_json()["job_id"] == job_id
            # Worker stays connected but sends no progress; stall requeues once
            # and the retry is dispatched back to the same connected worker.
            poll_until_attempt(client, job_id, 2, timeout=3.0)
            redispatch = worker.receive_json()
            assert redispatch["type"] == "dispatch_job"
            assert redispatch["job_id"] == job_id


@pytest.mark.db
def test_recover_requeues_running_and_dispatches_queued():
    async def prepare() -> tuple[uuid.UUID, uuid.UUID]:
        if not await db.connect():
            pytest.skip("database unavailable")
        ids = await _seed_recover_jobs()
        await db.dispose()
        return ids

    queued_id, running_id = asyncio.run(prepare())

    with TestClient(app) as client:
        requeued = client.get(f"/api/v1/generations/{running_id}").json()
        assert requeued["state"] == "queued"

        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-recover")

            first = worker.receive_json()
            assert first["type"] == "dispatch_job"
            first_id = first["job_id"]

            upload_path = urlsplit(first["upload"]["url"]).path
            assert client.put(upload_path, content=b"webp-bytes").status_code == 200
            worker.send_json({"type": "job_done", "job_id": first_id,
                              "gpu_ms": 1, "width": 512, "height": 512})
            poll_until(client, first_id, "succeeded")

            second = worker.receive_json()
            assert second["type"] == "dispatch_job"
            second_id = second["job_id"]
            assert {first_id, second_id} == {str(queued_id), str(running_id)}
