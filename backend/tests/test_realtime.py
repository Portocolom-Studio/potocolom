import uuid

from fastapi.testclient import TestClient

from app.main import app
from app.realtime import CANVAS_FRAME, GENERATED_FRAME, MIN_SUPPORTED_VERSION, PROTOCOL_VERSION

client = TestClient(app)


def hello(version=PROTOCOL_VERSION, worker_id="w-test", models=("sd-sim",), slots=1):
    return {
        "type": "hello",
        "protocol_version": version,
        "worker_id": worker_id,
        "models": list(models),
        "realtime_slots": slots,
    }


def test_version_gate_rejects_older_than_n_minus_1():
    with client.websocket_connect("/api/v1/fleet") as ws:
        ws.send_json(hello(version=MIN_SUPPORTED_VERSION - 1))
        response = ws.receive_json()
        assert response["type"] == "rejected"
        assert response["min_supported_version"] == MIN_SUPPORTED_VERSION


def test_version_gate_accepts_n_minus_1():
    with client.websocket_connect("/api/v1/fleet") as ws:
        ws.send_json(hello(version=MIN_SUPPORTED_VERSION))
        assert ws.receive_json()["type"] == "registered"


def test_unknown_model_is_refused():
    with client.websocket_connect("/api/v1/realtime") as ws:
        ws.send_json({"type": "open", "model_id": "does-not-exist"})
        response = ws.receive_json()
        assert response["type"] == "error"
        assert response["code"] == 4004


def test_session_and_frame_relay_both_directions():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello())
        assert worker_ws.receive_json()["type"] == "registered"

        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim"})

            opened = worker_ws.receive_json()
            assert opened["type"] == "open_session"
            worker_ws.send_json({"type": "session_ready", "session_id": opened["session_id"]})

            ready = browser_ws.receive_json()
            assert ready["type"] == "ready"
            session = uuid.UUID(ready["session_id"])

            canvas = bytes([CANVAS_FRAME]) + session.bytes + b"canvas-payload"
            browser_ws.send_bytes(canvas)
            assert worker_ws.receive_bytes() == canvas

            generated = bytes([GENERATED_FRAME]) + session.bytes + b"generated-payload"
            worker_ws.send_bytes(generated)
            assert browser_ws.receive_bytes() == generated
