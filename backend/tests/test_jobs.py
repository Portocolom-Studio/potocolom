"""The job dispatch and history flow (#16), driven with a fake worker over the
real fleet WebSocket. Real inference is the worker's side (worker/tests)."""

import time
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.realtime import PROTOCOL_VERSION

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
