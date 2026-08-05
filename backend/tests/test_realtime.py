import asyncio
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app import db, realtime
from app.main import app
from app.manifests import Manifest
from app.realtime import (
    CANVAS_FRAME,
    GENERATED_FRAME,
    MIN_SUPPORTED_VERSION,
    PROTOCOL_VERSION,
    origin_allowed,
)
from app.settings import get_settings
from app.tables import UsageEvent

client = TestClient(app)


def manifest(model_id="sd-sim") -> dict:
    return {"id": model_id, "name": model_id, "capabilities": ["realtime"], "parameters": {}}


class FakeHeaders:
    """Stands in for a WebSocket when only the Origin header matters.

    Uses the real Headers type: case-insensitive lookup is its behaviour and a
    plain dict would not prove origin_allowed relies on it correctly.
    """

    def __init__(self, origin):
        raw = [] if origin is None else [(b"origin", origin.encode())]
        self.headers = Headers(raw=raw)


class FakeSocket:
    """Minimal WebSocket stand-in for driving realtime helpers on one loop."""

    def __init__(self):
        self.sent = []
        self.close_code = None

    async def send_json(self, message):
        self.sent.append(message)

    async def close(self, code=1000):
        self.close_code = code


def hello(version=PROTOCOL_VERSION, worker_id="w-test", models=("sd-sim",), slots=1):
    return {
        "type": "hello",
        "protocol_version": version,
        "worker_id": worker_id,
        "models": [manifest(m) for m in models],
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


@pytest.mark.db
def test_closed_session_persists_usage_event():
    with TestClient(app) as db_client:
        with db_client.websocket_connect("/api/v1/fleet") as worker_ws:
            worker_ws.send_json(hello(worker_id="w-usage"))
            assert worker_ws.receive_json()["type"] == "registered"
            with db_client.websocket_connect("/api/v1/realtime") as browser_ws:
                browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
                opened = worker_ws.receive_json()
                worker_ws.send_json({
                    "type": "session_ready",
                    "session_id": opened["session_id"],
                })
                assert browser_ws.receive_json()["type"] == "ready"
                browser_ws.send_json({"type": "close"})
            closed = worker_ws.receive_json()
            assert closed["type"] == "close_session"
            worker_ws.send_json({
                "type": "session_closed",
                "session_id": opened["session_id"],
                "frames": 3,
                "gpu_ms": 90,
                "duration_ms": 2000,
                "category": "other",
            })

            async def persisted() -> bool:
                assert db.session_factory is not None
                async with db.session_factory() as session:
                    row = (
                        await session.execute(
                            select(UsageEvent).where(
                                UsageEvent.kind == "realtime",
                                UsageEvent.model_id == "sd-sim",
                            ).order_by(UsageEvent.created_at.desc()).limit(1)
                        )
                    ).scalar_one_or_none()
                    return row is not None and row.frames == 3 and row.action == "draw"

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not asyncio.run(persisted()):
                time.sleep(0.05)
            assert asyncio.run(persisted())


@pytest.mark.db
def test_closing_session_is_removed_when_worker_is_lost():
    with TestClient(app) as db_client:
        with db_client.websocket_connect("/api/v1/fleet") as worker_ws:
            worker_ws.send_json(hello(worker_id="w-closing"))
            assert worker_ws.receive_json()["type"] == "registered"
            with db_client.websocket_connect("/api/v1/realtime") as browser_ws:
                browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
                opened = worker_ws.receive_json()
                worker_ws.send_json({
                    "type": "session_ready",
                    "session_id": opened["session_id"],
                })
                assert browser_ws.receive_json()["type"] == "ready"
                browser_ws.send_json({"type": "close"})
            assert worker_ws.receive_json()["type"] == "close_session"
            session_id = uuid.UUID(opened["session_id"])
            assert session_id in realtime.closing_sessions

        assert session_id not in realtime.closing_sessions


def test_malformed_hello_closes_with_protocol_violation():
    with client.websocket_connect("/api/v1/fleet") as ws:
        ws.send_text("not json at all")
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        assert closed.value.code == 4000


def test_hello_missing_fields_closes_with_protocol_violation():
    with client.websocket_connect("/api/v1/fleet") as ws:
        ws.send_json({"type": "hello"})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        assert closed.value.code == 4000


def test_hello_wrong_types_close_with_protocol_violation():
    with client.websocket_connect("/api/v1/fleet") as ws:
        ws.send_json(hello(version="1"))  # string, not int
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        assert closed.value.code == 4000


def test_frame_for_another_session_closes_with_protocol_violation():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-crossframe"))
        assert worker_ws.receive_json()["type"] == "registered"
        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
            opened = worker_ws.receive_json()
            worker_ws.send_json({"type": "session_ready", "session_id": opened["session_id"]})
            assert browser_ws.receive_json()["type"] == "ready"

            foreign = bytes([CANVAS_FRAME]) + uuid.uuid4().bytes + b"not-my-session"
            browser_ws.send_bytes(foreign)
            with pytest.raises(WebSocketDisconnect) as closed:
                browser_ws.receive_bytes()
            assert closed.value.code == 4000


def test_short_binary_frame_closes_browser_with_protocol_violation():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-shortframe"))
        assert worker_ws.receive_json()["type"] == "registered"
        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
            opened = worker_ws.receive_json()
            worker_ws.send_json({"type": "session_ready", "session_id": opened["session_id"]})
            assert browser_ws.receive_json()["type"] == "ready"

            browser_ws.send_bytes(b"\x01tiny")
            with pytest.raises(WebSocketDisconnect) as closed:
                browser_ws.receive_bytes()
            assert closed.value.code == 4000


def test_heartbeat_does_not_free_committed_slots():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-heartbeat", slots=1))
        assert worker_ws.receive_json()["type"] == "registered"
        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
            opened = worker_ws.receive_json()
            worker_ws.send_json({"type": "session_ready", "session_id": opened["session_id"]})
            assert browser_ws.receive_json()["type"] == "ready"

            # A stale self-report must not undo the server-side accounting.
            worker_ws.send_json({"type": "heartbeat", "slots_in_use": 0})

            with client.websocket_connect("/api/v1/realtime") as second_ws:
                second_ws.send_json({"type": "open", "model_id": "sd-sim"})
                refusal = second_ws.receive_json()
                assert refusal["type"] == "error"
                assert refusal["code"] == 4003


def test_assign_timeout_releases_the_slot(monkeypatch):
    monkeypatch.setattr(realtime, "SESSION_READY_TIMEOUT", 0.1)
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-silent", slots=1))
        assert worker_ws.receive_json()["type"] == "registered"
        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
            assert worker_ws.receive_json()["type"] == "open_session"
            # The worker never answers session_ready.
            refusal = browser_ws.receive_json()
            assert refusal["type"] == "error"
            assert refusal["code"] == 4003
        assert realtime.workers["w-silent"].slots_in_use == 0


def test_reassign_moves_the_session_with_correct_accounting():
    # TestClient runs each WebSocket connection on its own event loop, so the
    # cross-connection recovery flow cannot be driven through it; the CI
    # simulation covers that end to end over real TCP. This drives reassign()
    # directly on one loop and pins message order and slot accounting.
    async def scenario():
        browser = FakeSocket()
        replacement_ws = FakeSocket()
        replacement = realtime.Worker(id="w-replacement", ws=replacement_ws,
                                      manifests=[Manifest.model_validate(manifest())],
                                      realtime_slots=1)
        realtime.workers[replacement.id] = replacement
        session = realtime.Session(id=uuid.uuid4(), model_id="sd-sim", browser=browser)
        realtime.sessions[session.id] = session
        try:
            task = asyncio.create_task(realtime.reassign(session))
            await asyncio.sleep(0.01)  # interrupted sent, open_session in flight
            assert replacement_ws.sent[0]["type"] == "open_session"
            assert replacement_ws.sent[0]["session_id"] == str(session.id)
            session.ready.set()  # what the fleet handler does on session_ready
            await task
            assert [m["type"] for m in browser.sent] == ["interrupted", "resumed"]
            assert session.worker is replacement
            assert replacement.slots_in_use == 1
            assert browser.close_code is None  # the session survived
        finally:
            realtime.workers.pop(replacement.id, None)
            realtime.sessions.pop(session.id, None)

    asyncio.run(scenario())


def test_reaper_closes_silent_workers():
    stale = realtime.Worker(id="w-stale", ws=FakeSocket(), manifests=[], realtime_slots=1,
                            last_seen=time.monotonic() - realtime.WORKER_DEAD_SECONDS - 1)
    fresh = realtime.Worker(id="w-fresh", ws=FakeSocket(), manifests=[], realtime_slots=1)
    realtime.workers.update({stale.id: stale, fresh.id: fresh})
    try:
        asyncio.run(realtime.reap_once())
        assert stale.ws.close_code is not None
        assert fresh.ws.close_code is None
    finally:
        realtime.workers.pop("w-stale", None)
        realtime.workers.pop("w-fresh", None)


def test_origin_allowed_only_for_no_origin_and_configured_origins():
    # Browsers always send Origin and cannot forge it; workers send none.
    settings = get_settings()
    assert origin_allowed(FakeHeaders(None))
    assert origin_allowed(FakeHeaders(settings.public_url))
    assert origin_allowed(FakeHeaders(settings.public_url + "/"))
    assert not origin_allowed(FakeHeaders("https://evil.example"))
    assert not origin_allowed(FakeHeaders("http://localhost:5173"))


def test_origin_allowed_honours_the_configured_extra_origins(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "allowed_origins", "http://localhost:5173, https://ok.example")
    assert origin_allowed(FakeHeaders("http://localhost:5173"))
    assert origin_allowed(FakeHeaders("https://ok.example"))
    assert not origin_allowed(FakeHeaders("https://evil.example"))


def test_foreign_origin_cannot_register_a_worker():
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/v1/fleet", headers={"origin": "https://evil.example"},
        ) as ws:
            ws.send_json(hello(worker_id="w-evil"))
            ws.receive_json()
    assert "w-evil" not in realtime.workers
