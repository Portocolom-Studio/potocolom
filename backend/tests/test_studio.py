import uuid
from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient
from starlette.websockets import WebSocketState

from app import realtime
from app.auth import current_user
from app.main import app
from app.manifests import Manifest
from app.tables import User

client = TestClient(app)


def test_studio_gpu_requires_worker(monkeypatch):
    monkeypatch.setattr(realtime, "workers", {})
    monkeypatch.setitem(app.dependency_overrides, current_user,
                        lambda: User(email="studio@example.test", role="admin"))
    response = client.get("/api/v1/studio/gpu")
    assert response.status_code == 503
    assert response.json()["detail"] == "no worker connected"


def test_studio_gpu_reports_connected_protocol_6_worker(monkeypatch):
    socket = MagicMock(client_state=WebSocketState.CONNECTED)
    socket.send_json = AsyncMock()
    worker = realtime.Worker(id="v6", ws=socket, manifests=[], realtime_slots=1,
                             protocol_version=6, incarnation=uuid.uuid4())
    dead_socket = MagicMock(client_state=WebSocketState.DISCONNECTED)
    dead = realtime.Worker(id="dead", ws=dead_socket, manifests=[], realtime_slots=1,
                           protocol_version=6, incarnation=uuid.uuid4())
    monkeypatch.setattr(realtime, "workers", {"dead": dead, "v6": worker})
    monkeypatch.setitem(app.dependency_overrides, current_user,
                        lambda: User(email="studio@example.test", role="admin"))

    response = client.get("/api/v1/studio/gpu")

    assert response.status_code == 200
    assert response.json() == {"loaded_models": [], "gpu": None}
    socket.send_json.assert_not_awaited()
    assert "dead" not in realtime.workers


def test_studio_gpu_prunes_disconnected_protocol_6_worker(monkeypatch):
    socket = MagicMock(client_state=WebSocketState.DISCONNECTED)
    worker = realtime.Worker(id="dead", ws=socket, manifests=[], realtime_slots=1,
                             protocol_version=6, incarnation=uuid.uuid4())
    monkeypatch.setattr(realtime, "workers", {"dead": worker})
    monkeypatch.setitem(app.dependency_overrides, current_user,
                        lambda: User(email="studio@example.test", role="admin"))

    response = client.get("/api/v1/studio/gpu")

    assert response.status_code == 503
    assert response.json()["detail"] == "no worker connected"
    assert not realtime.workers


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
