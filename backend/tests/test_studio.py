from starlette.testclient import TestClient
from starlette.websockets import WebSocketState

from app import realtime
from app.main import app
from app.manifests import Manifest

client = TestClient(app)


def test_studio_gpu_requires_worker():
    realtime.workers.clear()
    response = client.get("/api/v1/studio/gpu")
    assert response.status_code == 503


def test_pick_any_worker_prunes_disconnected_sockets():
    from unittest.mock import MagicMock

    disconnected = MagicMock()
    disconnected.client_state = WebSocketState.DISCONNECTED
    live = MagicMock()
    live.client_state = WebSocketState.CONNECTED
    manifest = Manifest(id="sd-test", name="SD Test", capabilities=["text_to_image"],
                        parameters={})
    dead = realtime.Worker(id="w-dead", ws=disconnected, manifests=[manifest],
                           realtime_slots=1)
    ok = realtime.Worker(id="w-live", ws=live, manifests=[manifest], realtime_slots=1)
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers["w-dead"] = dead
        realtime.workers["w-live"] = ok
        assert realtime.pick_any_worker() is ok
        assert "w-dead" not in realtime.workers
    finally:
        realtime.workers.clear()
        realtime.workers.update(saved)


def test_studio_gpu_returns_worker_snapshot(monkeypatch):
    async def fake_gpu_command(worker, command, timeout=15.0):
        return {
            "loaded_models": ["sd-sim"],
            "gpu": {
                "device": "cpu",
                "available": False,
            },
        }

    monkeypatch.setattr("app.studio.gpu_command", fake_gpu_command)
    monkeypatch.setattr("app.studio.pick_any_worker", lambda: object())

    response = client.get("/api/v1/studio/gpu")
    assert response.status_code == 200
    body = response.json()
    assert body["loaded_models"] == ["sd-sim"]
    assert body["gpu"]["device"] == "cpu"
