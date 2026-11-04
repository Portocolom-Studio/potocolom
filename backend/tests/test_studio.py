from starlette.testclient import TestClient
from starlette.websockets import WebSocketState

from app import realtime
from app.auth import current_user
from app.main import app
from app.manifests import Manifest
from app.tables import User

client = TestClient(app)


def test_studio_gpu_requires_worker():
    realtime.workers.clear()
    response = client.get("/api/v1/studio/gpu")
    assert response.status_code == 503


def test_studio_gpu_answers_503_when_the_worker_socket_is_dead(monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    class DeadSocket:
        async def send_json(self, *args, **kwargs):
            raise WebSocketDisconnect()

    dead = realtime.Worker(id="w-dead-send", ws=DeadSocket(), manifests=[], realtime_slots=1)
    monkeypatch.setattr("app.studio.pick_any_worker", lambda: dead)
    monkeypatch.setitem(
        app.dependency_overrides,
        current_user,
        lambda: User(email="studio@example.test", role="admin"),
    )
    # A socket that dies between pick and send is the same "no worker" as
    # none connected, so it is 503, not a 500 for an ordinary disconnect
    # (issue #497).
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

    monkeypatch.setitem(
        app.dependency_overrides,
        current_user,
        lambda: User(email="studio@example.test", role="admin"),
    )
    response = client.get("/api/v1/studio/gpu")
    assert response.status_code == 200
    body = response.json()
    assert body["loaded_models"] == ["sd-sim"]
    assert body["gpu"]["device"] == "cpu"
