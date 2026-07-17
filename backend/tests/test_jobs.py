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
    "capabilities": ["text_to_image", "image_to_image"],
    "parameters": {
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
        "required": ["prompt"],
    },
    "min_vram_gb": 0,
}

MANIFEST_T2I_ONLY = {
    **MANIFEST,
    "id": "sd-t2i",
    "capabilities": ["text_to_image"],
}

MANIFEST_WITH_RT = {
    **MANIFEST,
    "capabilities": ["text_to_image", "image_to_image", "realtime"],
}


def fleet_hello(ws, worker_id, manifest=MANIFEST):
    ws.send_json({"type": "hello", "protocol_version": PROTOCOL_VERSION,
                  "worker_id": worker_id, "models": [manifest], "realtime_slots": 1})
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
            thumb_path = urlsplit(dispatch["thumb_upload"]["url"]).path
            assert client.put(thumb_path, content=b"thumb-bytes").status_code == 200

            worker.send_json({"type": "job_progress", "job_id": job_id, "progress": 0.5})
            worker.send_json({"type": "job_done", "job_id": job_id,
                              "gpu_ms": 1234, "width": 512, "height": 512,
                              "has_thumbnail": True})

            job = poll_until(client, job_id, "succeeded")
            assert job["gpu_ms"] == 1234
            asset = job["assets"][0]
            assert asset["width"] == 512
            assert asset["thumbnail_url"] is not None
            assert client.get(urlsplit(asset["url"]).path).content == b"webp-bytes"
            assert client.get(urlsplit(asset["thumbnail_url"]).path).content == b"thumb-bytes"

            history = client.get("/api/v1/generations").json()
            assert any(entry["id"] == job_id for entry in history)

            # The event stream replays the terminal state and ends.
            events = client.get(f"/api/v1/generations/{job_id}/events")
            assert "succeeded" in events.text


@pytest.mark.db
def test_benchmark_only_model_hidden_without_benchmark_api():
    bench_manifest = {**MANIFEST, "id": "bench-only", "benchmark_only": True}
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-bench", manifest=bench_manifest)
            missing = client.post("/api/v1/generations",
                                  json={"model_id": "bench-only",
                                        "params": {"prompt": "hidden"}})
            assert missing.status_code == 404


@pytest.mark.db
def test_benchmark_only_model_allowed_when_benchmark_api_enabled(monkeypatch):
    monkeypatch.setenv("BENCHMARK_API", "1")
    from app.settings import get_settings

    get_settings.cache_clear()
    bench_manifest = {**MANIFEST, "id": "bench-only", "benchmark_only": True}
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-bench", manifest=bench_manifest)
            created = client.post("/api/v1/generations",
                                  json={"model_id": "bench-only",
                                        "params": {"prompt": "benchmark"}})
            assert created.status_code == 202


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
    try:
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
    finally:
        monkeypatch.delenv("JOB_STALL_SECONDS", raising=False)
        get_settings.cache_clear()


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
            thumb_path = urlsplit(first["thumb_upload"]["url"]).path
            assert client.put(thumb_path, content=b"thumb-bytes").status_code == 200
            worker.send_json({"type": "job_done", "job_id": first_id,
                              "gpu_ms": 1, "width": 512, "height": 512,
                              "has_thumbnail": True})
            poll_until(client, first_id, "succeeded")

            second = worker.receive_json()
            assert second["type"] == "dispatch_job"
            second_id = second["job_id"]
            assert {first_id, second_id} == {str(queued_id), str(running_id)}


@pytest.mark.db
def test_img2img_dispatch_includes_input_url():
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-i2i")

            created = client.post("/api/v1/generations",
                                  json={"model_id": "sd-test",
                                        "params": {"prompt": "a lighthouse"}})
            assert created.status_code == 202
            source_job_id = created.json()["job_id"]

            dispatch = worker.receive_json()
            upload_path = urlsplit(dispatch["upload"]["url"]).path
            assert client.put(upload_path, content=b"source-webp").status_code == 200
            worker.send_json({"type": "job_done", "job_id": source_job_id,
                              "gpu_ms": 100, "width": 512, "height": 512})
            source_job = poll_until(client, source_job_id, "succeeded")
            source_asset_id = source_job["assets"][0]["id"]

            edit = client.post("/api/v1/generations",
                               json={"model_id": "sd-test",
                                     "params": {"prompt": "a red lighthouse"},
                                     "source_asset_id": source_asset_id})
            assert edit.status_code == 202
            edit_job_id = edit.json()["job_id"]

            i2i_dispatch = worker.receive_json()
            assert i2i_dispatch["type"] == "dispatch_job"
            assert i2i_dispatch["job_id"] == edit_job_id
            assert "input" in i2i_dispatch
            input_path = urlsplit(i2i_dispatch["input"]["url"]).path
            assert client.get(input_path).content == b"source-webp"

            assert client.put(urlsplit(i2i_dispatch["upload"]["url"]).path,
                              content=b"edited-webp").status_code == 200
            worker.send_json({"type": "job_done", "job_id": edit_job_id,
                              "gpu_ms": 200, "width": 512, "height": 512})
            edit_job = poll_until(client, edit_job_id, "succeeded")
            assert edit_job["assets"][0]["url"].endswith(".webp")


@pytest.mark.db
def test_img2img_rejects_model_without_capability():
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-i2i-cap", MANIFEST_T2I_ONLY)

            created = client.post("/api/v1/generations",
                                  json={"model_id": "sd-t2i",
                                        "params": {"prompt": "seed"}})
            job_id = created.json()["job_id"]
            dispatch = worker.receive_json()
            upload_path = urlsplit(dispatch["upload"]["url"]).path
            assert client.put(upload_path, content=b"source").status_code == 200
            worker.send_json({"type": "job_done", "job_id": job_id,
                              "gpu_ms": 1, "width": 512, "height": 512})
            source_job = poll_until(client, job_id, "succeeded")
            source_asset_id = source_job["assets"][0]["id"]

            rejected = client.post("/api/v1/generations",
                                   json={"model_id": "sd-t2i",
                                         "params": {"prompt": "edit"},
                                         "source_asset_id": source_asset_id})
            assert rejected.status_code == 422
            detail = rejected.json()["detail"]
            assert "image_to_image" in detail or "upscale" in detail


@pytest.mark.db
def test_upscale_dispatch_includes_input_url():
    upscale_manifest = {
        "id": "realesrgan",
        "name": "Real-ESRGAN",
        "capabilities": ["upscale"],
        "parameters": {
            "type": "object",
            "properties": {"factor": {"type": "integer", "enum": [2, 4], "default": 2}},
            "required": ["factor"],
        },
        "min_vram_gb": 4,
    }
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-upscale-seed")
            created = client.post("/api/v1/generations",
                                  json={"model_id": "sd-test",
                                        "params": {"prompt": "seed"}})
            source_job_id = created.json()["job_id"]
            dispatch = worker.receive_json()
            assert client.put(urlsplit(dispatch["upload"]["url"]).path,
                              content=b"source-webp").status_code == 200
            worker.send_json({"type": "job_done", "job_id": source_job_id,
                              "gpu_ms": 50, "width": 512, "height": 512})
            source_asset_id = poll_until(client, source_job_id, "succeeded")["assets"][0]["id"]

        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json({
                "type": "hello",
                "protocol_version": PROTOCOL_VERSION,
                "worker_id": "w-upscale",
                "models": [MANIFEST, upscale_manifest],
                "realtime_slots": 1,
            })
            assert worker.receive_json()["type"] == "registered"

            upscale = client.post("/api/v1/generations",
                                  json={"model_id": "realesrgan",
                                        "params": {"factor": 2},
                                        "source_asset_id": source_asset_id})
            assert upscale.status_code == 202
            upscale_job_id = upscale.json()["job_id"]

            up_dispatch = worker.receive_json()
            assert up_dispatch["type"] == "dispatch_job"
            assert up_dispatch["job_id"] == upscale_job_id
            assert up_dispatch["params"] == {"factor": 2}
            assert "input" in up_dispatch
            assert client.get(urlsplit(up_dispatch["input"]["url"]).path).content == b"source-webp"

            assert client.put(urlsplit(up_dispatch["upload"]["url"]).path,
                              content=b"upscaled-webp").status_code == 200
            worker.send_json({"type": "job_done", "job_id": upscale_job_id,
                              "gpu_ms": 400, "width": 1024, "height": 1024})
            done = poll_until(client, upscale_job_id, "succeeded")
            assert done["gpu_ms"] == 400
            assert done["assets"][0]["width"] == 1024


@pytest.mark.db
def test_upscale_rejects_without_source_asset():
    upscale_manifest = {
        "id": "realesrgan",
        "name": "Real-ESRGAN",
        "capabilities": ["upscale"],
        "parameters": {
            "type": "object",
            "properties": {"factor": {"type": "integer", "enum": [2, 4], "default": 2}},
            "required": ["factor"],
        },
        "min_vram_gb": 4,
    }
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-upscale-nosrc", upscale_manifest)
            rejected = client.post("/api/v1/generations",
                                   json={"model_id": "realesrgan",
                                         "params": {"factor": 2}})
            assert rejected.status_code == 422
            assert "source_asset_id" in rejected.json()["detail"]


@pytest.mark.db
def test_upscale_mixed_capabilities_rejected_at_hello():
    from starlette.websockets import WebSocketDisconnect

    bad = {
        "id": "bad-upscale",
        "name": "Bad",
        "capabilities": ["upscale", "text_to_image"],
        "parameters": {},
        "min_vram_gb": 0,
    }
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json({
                "type": "hello",
                "protocol_version": PROTOCOL_VERSION,
                "worker_id": "w-bad-upscale",
                "models": [bad],
                "realtime_slots": 0,
            })
            with pytest.raises(WebSocketDisconnect) as closed:
                worker.receive_json()
            assert closed.value.code == 4000


@pytest.mark.db
def test_job_phase_timings_persisted():
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-phases")

            created = client.post("/api/v1/generations",
                                  json={"model_id": "sd-test",
                                        "params": {"prompt": "timing"}})
            job_id = created.json()["job_id"]
            dispatch = worker.receive_json()
            upload_path = urlsplit(dispatch["upload"]["url"]).path
            assert client.put(upload_path, content=b"webp-bytes").status_code == 200

            worker.send_json({"type": "job_done", "job_id": job_id,
                              "gpu_ms": 900, "input_fetch_ms": 50,
                              "load_ms": 1200, "postprocess_ms": 80,
                              "width": 512, "height": 512})

            job = poll_until(client, job_id, "succeeded")
            assert job["gpu_ms"] == 900
            assert job["input_fetch_ms"] == 50
            assert job["load_ms"] == 1200
            assert job["postprocess_ms"] == 80
            assert job["dispatched_at"] is not None
            assert job["finished_at"] is not None


@pytest.mark.db
def test_job_failure_reason_persisted():
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-fail-reason")

            created = client.post("/api/v1/generations",
                                  json={"model_id": "sd-test",
                                        "params": {"prompt": "fail"}})
            job_id = created.json()["job_id"]
            worker.receive_json()
            worker.send_json({"type": "job_failed", "job_id": job_id,
                              "reason": "CUDA OOM"})

            job = poll_until(client, job_id, "failed")
            assert job["failure_reason"] == "CUDA OOM"
            assert job["finished_at"] is not None


def _post_generation(client, prompt: str) -> str:
    created = client.post("/api/v1/generations",
                          json={"model_id": "sd-test", "params": {"prompt": prompt}})
    assert created.status_code == 202
    return created.json()["job_id"]


def _stall_safe(monkeypatch) -> None:
    monkeypatch.setenv("JOB_STALL_SECONDS", "600")
    from app.settings import get_settings
    get_settings.cache_clear()


def _wait_for_dispatch(worker, expected: set[str], timeout=5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = worker.receive_json()
        if msg["type"] == "dispatch_job" and msg["job_id"] in expected:
            return msg
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for dispatch_job in {expected}")


def _finish_job(client, worker, dispatch: dict) -> None:
    upload_path = urlsplit(dispatch["upload"]["url"]).path
    assert client.put(upload_path, content=b"webp-bytes").status_code == 200
    worker.send_json({"type": "job_done", "job_id": dispatch["job_id"],
                      "gpu_ms": 1, "width": 512, "height": 512})
    poll_until(client, dispatch["job_id"], "succeeded")


@pytest.mark.db
def test_dispatch_depth_two_before_job_done(monkeypatch):
    """Depth 2: API dispatches a second job while the first is still uploading."""
    _stall_safe(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-depth2")

            first_id = _post_generation(client, "one")
            second_id = _post_generation(client, "two")
            expected = {first_id, second_id}
            time.sleep(0.35)

            first = _wait_for_dispatch(worker, expected)
            second = _wait_for_dispatch(worker, expected - {first["job_id"]})
            assert {first["job_id"], second["job_id"]} == expected

            # Leave no running jobs for the next test.
            _finish_job(client, worker, first)
            _finish_job(client, worker, second)


@pytest.mark.db
def test_dispatch_depth_blocks_third_until_slot_frees(monkeypatch):
    _stall_safe(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-depth-cap")

            id_a = _post_generation(client, "a")
            id_b = _post_generation(client, "b")
            expected = {id_a, id_b}
            time.sleep(0.35)
            d1 = _wait_for_dispatch(worker, expected)
            d2 = _wait_for_dispatch(worker, expected - {d1["job_id"]})
            worker.send_json({"type": "job_progress", "job_id": d1["job_id"], "progress": 0.5})
            worker.send_json({"type": "job_progress", "job_id": d2["job_id"], "progress": 0.5})

            third_id = _post_generation(client, "c")
            time.sleep(0.35)
            assert client.get(f"/api/v1/generations/{third_id}").json()["state"] == "queued"

            _finish_job(client, worker, d1)

            time.sleep(0.35)
            d3 = _wait_for_dispatch(worker, {third_id})
            assert d3["job_id"] == third_id

            _finish_job(client, worker, d2)
            _finish_job(client, worker, d3)


def test_pick_job_worker_prefers_least_loaded():
    from unittest.mock import MagicMock

    from app import jobs, realtime
    from app.manifests import Manifest

    manifest = Manifest(id="sd-test", name="SD Test", capabilities=["text_to_image"],
                        parameters={})
    busy = realtime.Worker(id="w-busy", ws=MagicMock(), manifests=[manifest],
                           realtime_slots=1, jobs_in_flight=1)
    idle = realtime.Worker(id="w-idle", ws=MagicMock(), manifests=[manifest],
                           realtime_slots=1, jobs_in_flight=0)
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers["w-busy"] = busy
        realtime.workers["w-idle"] = idle
        assert jobs.pick_job_worker("sd-test") is idle
    finally:
        realtime.workers.clear()
        # Skip disconnected TestClient sockets; restoring them leaves zombies
        # that make studio/gpu fail with ClosedResourceError instead of 503.
        from starlette.websockets import WebSocketState

        for worker_id, worker in saved.items():
            state = getattr(worker.ws, "client_state", None)
            if state is None or state == WebSocketState.CONNECTED:
                realtime.workers[worker_id] = worker


def test_job_dispatch_depth_one_while_realtime_live():
    from unittest.mock import MagicMock

    from app import jobs, realtime
    from app.manifests import Manifest

    manifest = Manifest(id="sd-test", name="SD Test", capabilities=["text_to_image"],
                        parameters={})
    drawing = realtime.Worker(id="w-drawing", ws=MagicMock(), manifests=[manifest],
                              realtime_slots=1, slots_in_use=1, jobs_in_flight=1)
    idle = realtime.Worker(id="w-idle", ws=MagicMock(), manifests=[manifest],
                           realtime_slots=1, jobs_in_flight=0)
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers["w-drawing"] = drawing
        realtime.workers["w-idle"] = idle
        assert jobs.job_dispatch_depth(drawing) == 1
        assert jobs.pick_job_worker("sd-test") is idle
    finally:
        realtime.workers.clear()
        from starlette.websockets import WebSocketState

        for worker_id, worker in saved.items():
            state = getattr(worker.ws, "client_state", None)
            if state is None or state == WebSocketState.CONNECTED:
                realtime.workers[worker_id] = worker


@pytest.mark.db
def test_dispatch_depth_one_while_realtime_session_open(monkeypatch):
    """Sessions-first: depth drops to 1 while a drawing slot is live."""
    _stall_safe(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-session-jobs", manifest=MANIFEST_WITH_RT)

            with client.websocket_connect("/api/v1/realtime") as browser:
                browser.send_json({"type": "open", "model_id": "sd-test",
                                   "params": {"prompt": "live drawing"}})
                opened = worker.receive_json()
                assert opened["type"] == "open_session"
                worker.send_json({"type": "session_ready", "session_id": opened["session_id"]})
                assert browser.receive_json()["type"] == "ready"

                first_id = _post_generation(client, "during-session-1")
                second_id = _post_generation(client, "during-session-2")
                time.sleep(0.35)
                first = _wait_for_dispatch(worker, {first_id})
                assert client.get(f"/api/v1/generations/{second_id}").json()["state"] == "queued"
                _finish_job(client, worker, first)
