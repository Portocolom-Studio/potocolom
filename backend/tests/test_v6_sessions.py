"""Protocol 6 realtime sessions on local fleet sockets."""

import asyncio
import hashlib
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect

from app import command_store, db, jobs, realtime, worker_authority
from app.main import app
from app.protocol6 import canonical_bytes, decode_control, validate_message
from test_fleet_protocol6 import (
    FLEET_HEADERS,
    hello,
    manifest,
    protocol6_client,
    request_work_grant,
    root_keys,
)
from test_realtime import expect, canvas_frame, generated_frame


@pytest.fixture(autouse=True)
def park_dispatch_loop(monkeypatch):
    async def parked():
        return None

    monkeypatch.setattr(jobs, "dispatch_step", parked)


def command_ack(command: dict) -> dict:
    fields = {
        key: command[key]
        for key in (
            "worker_id", "incarnation", "region", "owner_epoch", "lease_id",
            "lease_expires_at", "grant_nonce", "command_sequence", "command_id",
            "body_hash",
        )
    }
    for key in ("session_id", "attempt_id", "control_generation", "job_id", "dispatch_sequence"):
        if key in command:
            fields[key] = command[key]
    return {**fields, "type": "command_ack", "status": "accepted"}


def session_ready_message(worker: realtime.Worker, open_cmd: dict,
                          grant_nonce: uuid.UUID | str) -> dict:
    message = {
        "type": "session_ready",
        "worker_id": worker.id,
        "incarnation": str(worker.incarnation),
        "region": worker_authority.REGION,
        "lease_id": str(worker.lease_id),
        "owner_epoch": worker.owner_epoch,
        "grant_nonce": str(grant_nonce),
        "lease_expires_at": worker.lease_expires_at,
        "session_id": open_cmd["session_id"],
        "attempt_id": open_cmd["attempt_id"],
        "control_generation": open_cmd["control_generation"],
        "params_revision": 1,
    }
    validate_message(
        message,
        expected_worker_id=worker.id,
        expected_incarnation=worker.incarnation,
    )
    return message


def session_closed_message(worker: realtime.Worker, open_cmd: dict,
                           grant_nonce: uuid.UUID | str,
                           report_sequence: int = 1) -> dict:
    message = {
        "type": "session_closed",
        "worker_id": worker.id,
        "incarnation": str(worker.incarnation),
        "region": worker_authority.REGION,
        "lease_id": str(worker.lease_id),
        "owner_epoch": worker.owner_epoch,
        "grant_nonce": str(grant_nonce),
        "lease_expires_at": worker.lease_expires_at,
        "session_id": open_cmd["session_id"],
        "attempt_id": open_cmd["attempt_id"],
        "control_generation": open_cmd["control_generation"],
        "range_id": open_cmd["work_budget"]["range_id"],
        "report_sequence": report_sequence,
        "gpu_ms": 100,
        "duration_ms": 200,
        "frames": 1,
        "physical_complete": True,
        "category": "other",
    }
    source = {key: value for key, value in message.items() if key != "report_hash"}
    message["report_hash"] = hashlib.sha256(canonical_bytes(source)).hexdigest()
    validate_message(
        message,
        expected_worker_id=worker.id,
        expected_incarnation=worker.incarnation,
    )
    return message


def open_live_session(client, worker_id: str, model_id: str):
    incarnation = uuid.uuid4()
    client.portal.call(worker_authority.acquire_scheduler_lease)
    fleet = client.websocket_connect("/api/v1/fleet")
    ws = fleet.__enter__()
    ws.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
    ws.receive_json()
    grant = request_work_grant(ws, worker_id, incarnation)
    worker = realtime.workers[worker_id]
    browser = client.websocket_connect("/api/v1/realtime")
    browser_ws = browser.__enter__()
    browser_ws.send_json({
        "type": "open",
        "model_id": model_id,
        "params": {"prompt": "house"},
        "frame_header": 2,
    })
    open_cmd = decode_control(ws.receive_text().encode("utf-8"))
    validate_message(
        open_cmd,
        expected_worker_id=worker_id,
        expected_incarnation=worker.incarnation,
    )
    assert open_cmd["type"] == "open_session"
    ws.send_json(command_ack(open_cmd))
    ws.send_json(session_ready_message(worker, open_cmd, grant["grant_nonce"]))
    expect(browser_ws, "ready")
    return ws, worker, grant, open_cmd, browser_ws, browser, fleet


@pytest.mark.db
def test_v6_session_open_ready_and_durable_rows(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"v6-session-{uuid.uuid4()}"
    model_id = f"v6-rt-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        incarnation = uuid.uuid4()
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as ws:
            ws.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            ws.receive_json()
            grant = request_work_grant(ws, worker_id, incarnation)
            worker = realtime.workers[worker_id]
            with client.websocket_connect("/api/v1/realtime") as browser_ws:
                browser_ws.send_json({
                    "type": "open",
                    "model_id": model_id,
                    "params": {"prompt": "house"},
                })
                open_cmd = decode_control(ws.receive_text().encode("utf-8"))
                validate_message(
                    open_cmd,
                    expected_worker_id=worker_id,
                    expected_incarnation=worker.incarnation,
                )
                assert open_cmd["type"] == "open_session"
                session_id = uuid.UUID(open_cmd["session_id"])

                async def read_rows():
                    async with db.session_factory() as session:
                        sess = (await session.execute(
                            text("SELECT state FROM realtime_sessions WHERE id = :id"),
                            {"id": session_id},
                        )).mappings().one()
                        attempt = (await session.execute(
                            text(
                                "SELECT state FROM realtime_session_attempts "
                                "WHERE session_id = :id"
                            ),
                            {"id": session_id},
                        )).mappings().one()
                        range_row = (await session.execute(
                            text(
                                "SELECT kind FROM worker_physical_ranges WHERE range_id = :id"
                            ),
                            {"id": uuid.UUID(open_cmd["work_budget"]["range_id"])},
                        )).mappings().one()
                        return sess, attempt, range_row

                sess, attempt, range_row = client.portal.call(read_rows)
                assert sess["state"] == "assigning"
                assert attempt["state"] == "opening"
                assert range_row["kind"] == "session"

                ws.send_json(command_ack(open_cmd))
                ws.send_json(session_ready_message(worker, open_cmd, grant["grant_nonce"]))
                ready = expect(browser_ws, "ready")
                assert ready["session_id"] == str(session_id)

                async def attempt_running():
                    async with db.session_factory() as session:
                        return (await session.execute(
                            text("SELECT state FROM realtime_session_attempts "
                                 "WHERE session_id = :id"),
                            {"id": session_id},
                        )).scalar_one()

                assert client.portal.call(attempt_running) == "running"


@pytest.mark.db
def test_v6_session_ready_after_grant_nonce_rotation(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"v6-grant-{uuid.uuid4()}"
    model_id = f"v6-rt-grant-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        incarnation = uuid.uuid4()
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as ws:
            ws.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            ws.receive_json()
            request_work_grant(ws, worker_id, incarnation)
            worker = realtime.workers[worker_id]
            with client.websocket_connect("/api/v1/realtime") as browser_ws:
                browser_ws.send_json({
                    "type": "open",
                    "model_id": model_id,
                    "params": {"prompt": "rotate"},
                })
                open_cmd = decode_control(ws.receive_text().encode("utf-8"))
                ws.send_json(command_ack(open_cmd))
                rotated = request_work_grant(ws, worker_id, incarnation)
                ws.send_json(session_ready_message(worker, open_cmd, rotated["grant_nonce"]))
                expect(browser_ws, "ready")


@pytest.mark.db
def test_v6_session_frames_relay_both_ways(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"v6-frames-{uuid.uuid4()}"
    model_id = f"v6-rt-frames-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        ws, worker, _grant, open_cmd, browser_ws, browser_ctx, fleet_ctx = open_live_session(
            client, worker_id, model_id
        )
        try:
            session_id = uuid.UUID(open_cmd["session_id"])
            payload = b"canvas-bytes"
            browser_ws.send_bytes(canvas_frame(session_id, payload, revision=1))
            worker_frame = ws.receive_bytes()
            assert worker_frame.endswith(payload)
            ws.send_bytes(generated_frame(session_id, b"out", revision=1))
            browser_frame = browser_ws.receive_bytes()
            assert browser_frame.endswith(b"out")
        finally:
            browser_ctx.__exit__(None, None, None)
            fleet_ctx.__exit__(None, None, None)


@pytest.mark.db
def test_v6_session_params_update_revision(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"v6-params-{uuid.uuid4()}"
    model_id = f"v6-rt-params-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        ws, worker, grant, open_cmd, browser_ws, browser_ctx, fleet_ctx = open_live_session(
            client, worker_id, model_id
        )
        try:
            browser_ws.send_json({
                "type": "update_params",
                "params": {"prompt": "second"},
            })
            updated = decode_control(ws.receive_text().encode("utf-8"))
            assert updated["type"] == "update_session"
            assert updated["params_revision"] == 2
            ws.send_json(command_ack(updated))
            expect(browser_ws, "params_updated")
        finally:
            browser_ctx.__exit__(None, None, None)
            fleet_ctx.__exit__(None, None, None)


async def _close_state(worker_id: str, incarnation: uuid.UUID, session_id: uuid.UUID):
    async with db.session_factory() as session:
        sess_state = await session.scalar(
            text("SELECT state FROM realtime_sessions WHERE id = :id"), {"id": session_id},
        )
        attempt_state = await session.scalar(
            text("SELECT state FROM realtime_session_attempts WHERE session_id = :id"),
            {"id": session_id},
        )
        connection_closed = await session.scalar(
            text("SELECT closed_at IS NOT NULL FROM worker_connections "
                 "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
            {"worker_id": worker_id, "incarnation": incarnation},
        )
        pending = await session.scalar(
            text("SELECT count(*) FROM worker_commands WHERE subject_id = :id "
                 "AND state = 'pending'"),
            {"id": session_id},
        )
    return sess_state, attempt_state, connection_closed, pending


def _wait_close_state(client, worker_id, incarnation, session_id, done):
    async def poll():
        for _ in range(300):
            state = await _close_state(worker_id, incarnation, session_id)
            if done(state):
                return state
            await asyncio.sleep(0.01)
        return state

    return client.portal.call(poll)


def _close_browser(browser_ws) -> None:
    browser_ws.send_json({"type": "close"})
    with pytest.raises(WebSocketDisconnect) as closed:
        browser_ws.receive_json()
    assert closed.value.code == 1000


@pytest.mark.db
def test_v6_live_close_sends_close_session_and_keeps_the_worker(monkeypatch):
    """An ordinary canvas close is a close_session the worker answers with
    session_closed: the incarnation is not fenced and its other work is kept."""
    root_keys(monkeypatch)
    monkeypatch.setattr(realtime, "CLOSE_RECEIPT_TIMEOUT", 0.3)
    worker_id = f"v6-close-{uuid.uuid4()}"
    model_id = f"v6-rt-close-{uuid.uuid4()}"
    lost = []
    real_lost = jobs.on_worker_lost
    monkeypatch.setattr(jobs, "on_worker_lost",
                        lambda worker: (lost.append(worker.id), real_lost(worker))[1])

    with protocol6_client(worker_id) as client:
        ws, worker, grant, open_cmd, browser_ws, browser_ctx, fleet_ctx = open_live_session(
            client, worker_id, model_id
        )
        try:
            session_id = uuid.UUID(open_cmd["session_id"])
            _close_browser(browser_ws)
            state = _wait_close_state(client, worker_id, worker.incarnation, session_id,
                                      lambda s: s[0] != "live")
            assert state[0] == "ending"
            assert state[2] is False
            close_cmd = decode_control(ws.receive_text().encode("utf-8"))
            assert close_cmd["type"] == "close_session"
            assert close_cmd["attempt_id"] == open_cmd["attempt_id"]
            assert close_cmd["control_generation"] == open_cmd["control_generation"]

            ws.send_json(command_ack(close_cmd))
            ws.send_json(session_closed_message(worker, open_cmd, grant["grant_nonce"]))
            report_ack = ws.receive_json()
            assert report_ack["type"] == "checkpoint_ack"
            assert report_ack["status"] == "accepted"
            state = _wait_close_state(client, worker_id, worker.incarnation, session_id,
                                      lambda s: s[0] == "ended")
            assert state == ("ended", "ended", False, 0)
            assert realtime.workers.get(worker_id) is worker
            # The receipt deadline passing must leave an answered close alone.
            client.portal.call(asyncio.sleep, 0.6)
            assert lost == []
            # The next routine renewal is where a fenced incarnation is cut off.
            request_work_grant(ws, worker_id, worker.incarnation)
            assert realtime.workers.get(worker_id) is worker
        finally:
            browser_ctx.__exit__(None, None, None)
            fleet_ctx.__exit__(None, None, None)


@pytest.mark.db
def test_v6_live_close_falls_back_to_the_fence_when_close_is_refused(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"v6-close-refused-{uuid.uuid4()}"
    model_id = f"v6-rt-close-refused-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        ws, worker, _grant, open_cmd, browser_ws, browser_ctx, fleet_ctx = open_live_session(
            client, worker_id, model_id
        )
        try:
            session_id = uuid.UUID(open_cmd["session_id"])
            refuse_command(monkeypatch, "close_session")
            _close_browser(browser_ws)
            state = _wait_close_state(client, worker_id, worker.incarnation, session_id,
                                      lambda s: s[0] == "ended" and s[2])
            assert state == ("ended", "ended", True, 0)
            assert worker_id not in realtime.workers
        finally:
            browser_ctx.__exit__(None, None, None)
            fleet_ctx.__exit__(None, None, None)


@pytest.mark.db
def test_v6_live_close_that_does_not_land_in_time_falls_back_to_the_fence(monkeypatch):
    """The close committed (the row is ending) but its send never returned:
    the timeout falls back to erasing that close and fencing the worker."""
    root_keys(monkeypatch)
    monkeypatch.setattr(realtime, "CLOSE_TIMEOUT", 0.3)
    worker_id = f"v6-close-stuck-{uuid.uuid4()}"
    model_id = f"v6-rt-close-stuck-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        ws, worker, _grant, open_cmd, browser_ws, browser_ctx, fleet_ctx = open_live_session(
            client, worker_id, model_id
        )
        stuck = asyncio.Event()

        async def never_returns(*args, **kwargs):
            await stuck.wait()

        try:
            session_id = uuid.UUID(open_cmd["session_id"])
            monkeypatch.setattr(worker.ws, "send_text", never_returns)
            _close_browser(browser_ws)
            state = _wait_close_state(client, worker_id, worker.incarnation, session_id,
                                      lambda s: s[0] == "ended" and s[2])
            assert state == ("ended", "ended", True, 0)
            assert worker_id not in realtime.workers
        finally:
            client.portal.call(stuck.set)
            browser_ctx.__exit__(None, None, None)
            fleet_ctx.__exit__(None, None, None)


@pytest.mark.db
def test_v6_live_close_never_delivered_is_fenced_at_the_receipt_deadline(monkeypatch):
    """A committed close whose delivery is refused (an expired browser binding,
    say) cannot be answered; the receipt deadline erases it and fences."""
    root_keys(monkeypatch)
    monkeypatch.setattr(realtime, "CLOSE_RECEIPT_TIMEOUT", 0.3)
    worker_id = f"v6-close-undelivered-{uuid.uuid4()}"
    model_id = f"v6-rt-close-undelivered-{uuid.uuid4()}"
    real_register = command_store.register_delivery

    async def refuse_close(command, **kwargs):
        if json.loads(command.body)["type"] == "close_session":
            return False
        return await real_register(command, **kwargs)

    with protocol6_client(worker_id) as client:
        ws, worker, _grant, open_cmd, browser_ws, browser_ctx, fleet_ctx = open_live_session(
            client, worker_id, model_id
        )
        try:
            session_id = uuid.UUID(open_cmd["session_id"])
            monkeypatch.setattr(command_store, "register_delivery", refuse_close)
            _close_browser(browser_ws)
            state = _wait_close_state(client, worker_id, worker.incarnation, session_id,
                                      lambda s: s[0] == "ended" and s[2])
            assert state == ("ended", "ended", True, 0)
            assert worker_id not in realtime.workers
        finally:
            browser_ctx.__exit__(None, None, None)
            fleet_ctx.__exit__(None, None, None)


@pytest.mark.db
def test_v6_live_close_with_a_pending_update_fences(monkeypatch):
    """A pending update carries a parameter body whose outcome is unproven, so
    the close erases it and fences rather than leave it behind."""
    root_keys(monkeypatch)
    worker_id = f"v6-close-pending-{uuid.uuid4()}"
    model_id = f"v6-rt-close-pending-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        ws, worker, _grant, open_cmd, browser_ws, browser_ctx, fleet_ctx = open_live_session(
            client, worker_id, model_id
        )
        try:
            session_id = uuid.UUID(open_cmd["session_id"])
            browser_ws.send_json({"type": "update_params", "params": {"prompt": "second"}})
            update_cmd = decode_control(ws.receive_text().encode("utf-8"))
            assert update_cmd["type"] == "update_session"
            _close_browser(browser_ws)
            state = _wait_close_state(client, worker_id, worker.incarnation, session_id,
                                      lambda s: s[0] == "ended" and s[2])
            assert state == ("ended", "ended", True, 0)
            assert worker_id not in realtime.workers
        finally:
            browser_ctx.__exit__(None, None, None)
            fleet_ctx.__exit__(None, None, None)


@pytest.mark.db
def test_v6_live_close_without_a_receipt_ends_when_the_worker_goes(monkeypatch):
    """A worker that takes the close and never reports session_closed leaves
    the row ending; losing that worker must still end the session."""
    root_keys(monkeypatch)
    worker_id = f"v6-close-silent-{uuid.uuid4()}"
    model_id = f"v6-rt-close-silent-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        ws, worker, _grant, open_cmd, browser_ws, browser_ctx, fleet_ctx = open_live_session(
            client, worker_id, model_id
        )
        incarnation = worker.incarnation
        fleet_open = True
        try:
            session_id = uuid.UUID(open_cmd["session_id"])
            _close_browser(browser_ws)
            # Read the socket only once the row says a close was committed, so a
            # regression fails here instead of blocking on a frame never sent.
            state = _wait_close_state(client, worker_id, incarnation, session_id,
                                      lambda s: s[0] != "live")
            assert state[0] == "ending"
            close_cmd = decode_control(ws.receive_text().encode("utf-8"))
            assert close_cmd["type"] == "close_session"
            ws.send_json(command_ack(close_cmd))
            fleet_ctx.__exit__(None, None, None)
            fleet_open = False
            state = _wait_close_state(client, worker_id, incarnation, session_id,
                                      lambda s: s[0] == "ended" and s[1] == "ended")
            assert state[:2] == ("ended", "ended")
            assert state[3] == 0
        finally:
            browser_ctx.__exit__(None, None, None)
            if fleet_open:
                fleet_ctx.__exit__(None, None, None)


@pytest.mark.db
def test_v6_worker_without_grant_is_not_picked_for_sessions(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"v6-nogrant-{uuid.uuid4()}"
    model_id = f"v6-rt-nogrant-{uuid.uuid4()}"
    incarnation = uuid.uuid4()

    with TestClient(app, headers=FLEET_HEADERS) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as ws:
            ws.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            ws.receive_json()
            worker = realtime.workers[worker_id]
            assert realtime.takes_work(worker) is False
            assert worker.has_current_work_grant is False
            assert realtime.takes_sessions(worker) is False
            assert realtime.pick_worker(model_id) is None


def refuse_command(monkeypatch, refused_type: str) -> None:
    real_commit = command_store.commit_command

    async def commit(*args, **kwargs):
        if args[2] == refused_type:
            raise command_store.CommandRefused("worker has no current work grant")
        return await real_commit(*args, **kwargs)

    monkeypatch.setattr(command_store, "commit_command", commit)


@pytest.mark.db
def test_a_refused_open_session_frees_the_slot_and_keeps_the_browser(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"v6-refopen-{uuid.uuid4()}"
    model_id = f"v6-rt-refopen-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        incarnation = uuid.uuid4()
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as ws:
            ws.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            ws.receive_json()
            request_work_grant(ws, worker_id, incarnation)
            worker = realtime.workers[worker_id]
            refuse_command(monkeypatch, "open_session")
            with client.websocket_connect("/api/v1/realtime") as browser_ws:
                browser_ws.send_json({
                    "type": "open",
                    "model_id": model_id,
                    "params": {"prompt": "house"},
                })
                assert browser_ws.receive_json()["type"] == "queued"
                assert worker.slots_in_use == 0


@pytest.mark.db
def test_v6_new_session_after_terminal_close_uses_new_incarnation(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"v6-reopen-{uuid.uuid4()}"
    model_id = f"v6-rt-reopen-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        _ws, worker, _grant, _open_cmd, browser_ws, browser_ctx, fleet_ctx = open_live_session(
            client, worker_id, model_id
        )
        old_incarnation = worker.incarnation
        try:
            browser_ws.send_json({"type": "close"})
            with pytest.raises(WebSocketDisconnect):
                browser_ws.receive_json()
        finally:
            browser_ctx.__exit__(None, None, None)
        fleet_ctx.__exit__(None, None, None)
        assert worker_id not in realtime.workers

        _ws2, worker2, _grant2, open_cmd2, _browser2, browser_ctx2, fleet_ctx2 = (
            open_live_session(client, worker_id, model_id)
        )
        try:
            assert worker2.incarnation != old_incarnation
            assert open_cmd2["type"] == "open_session"
        finally:
            browser_ctx2.__exit__(None, None, None)
            fleet_ctx2.__exit__(None, None, None)


@pytest.mark.db
def test_v6_release_during_open_session_commit_ends_attempt(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"v6-relcommit-{uuid.uuid4()}"
    model_id = f"v6-rt-relcommit-{uuid.uuid4()}"
    real_commit = command_store.commit_command

    async def commit_then_release(*args, **kwargs):
        result = await real_commit(*args, **kwargs)
        if args[2] == "open_session":
            session_id = uuid.UUID(args[3]["session_id"])
            session = realtime.sessions.get(session_id)
            if session is not None:
                await realtime.release(session)
        return result

    monkeypatch.setattr(command_store, "commit_command", commit_then_release)

    with protocol6_client(worker_id) as client:
        incarnation = uuid.uuid4()
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as ws:
            ws.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            ws.receive_json()
            request_work_grant(ws, worker_id, incarnation)
            with client.websocket_connect("/api/v1/realtime") as browser_ws:
                browser_ws.send_json({
                    "type": "open",
                    "model_id": model_id,
                    "params": {"prompt": "house"},
                })
                queued = browser_ws.receive_json()
                assert queued["type"] == "queued"
                live_sessions = [
                    session for session in realtime.sessions.values()
                    if session.model_id == model_id
                ]
                assert len(live_sessions) == 1
                session_id = live_sessions[0].id

                async def read_attempt():
                    async with db.session_factory() as session:
                        return await session.scalar(
                            text(
                                "SELECT state FROM realtime_session_attempts "
                                "WHERE session_id = :id"
                            ),
                            {"id": session_id},
                        )

                assert client.portal.call(read_attempt) == "ended"
                assert realtime.sessions[session_id].state == "queued"


@pytest.mark.db
def test_v6_accept_session_ready_refuses_ended_session(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"v6-lateready-{uuid.uuid4()}"
    model_id = f"v6-rt-lateready-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        incarnation = uuid.uuid4()
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as ws:
            ws.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            ws.receive_json()
            grant = request_work_grant(ws, worker_id, incarnation)
            worker = realtime.workers[worker_id]
            with client.websocket_connect("/api/v1/realtime") as browser_ws:
                browser_ws.send_json({
                    "type": "open",
                    "model_id": model_id,
                    "params": {"prompt": "house"},
                })
                open_cmd = decode_control(ws.receive_text().encode("utf-8"))
                ws.send_json(command_ack(open_cmd))
                session_id = uuid.UUID(open_cmd["session_id"])

                async def end_session_row():
                    async with db.session_factory() as session:
                        async with session.begin():
                            await session.execute(
                                text(
                                    "UPDATE realtime_sessions SET state = 'ended', "
                                    "ended_at = clock_timestamp() WHERE id = :id"
                                ),
                                {"id": session_id},
                            )

                client.portal.call(end_session_row)
                ready_msg = session_ready_message(worker, open_cmd, grant["grant_nonce"])
                accepted = client.portal.call(
                    worker_authority.accept_session_ready,
                    worker_id,
                    worker.incarnation,
                    ready_msg,
                )
                assert accepted is False


@pytest.mark.db
def test_v6_worker_disconnect_ends_running_session_attempt(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"v6-disc-{uuid.uuid4()}"
    model_id = f"v6-rt-disc-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        ws, worker, grant, open_cmd, browser_ws, browser_ctx, fleet_ctx = open_live_session(
            client, worker_id, model_id
        )
        try:
            session_id = uuid.UUID(open_cmd["session_id"])

            async def read_attempt():
                async with db.session_factory() as session:
                    return await session.scalar(
                        text(
                            "SELECT state FROM realtime_session_attempts "
                            "WHERE session_id = :id"
                        ),
                        {"id": session_id},
                    )

            assert client.portal.call(read_attempt) == "running"
            client.portal.call(worker_authority.close_worker, worker_id, worker.incarnation)
            assert client.portal.call(read_attempt) == "ended"
        finally:
            browser_ctx.__exit__(None, None, None)
            fleet_ctx.__exit__(None, None, None)


@pytest.mark.db
def test_pick_worker_for_model_skips_protocol6_workers(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"v6-bench-{uuid.uuid4()}"
    model_id = f"v6-rt-bench-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        incarnation = uuid.uuid4()
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as ws:
            ws.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            ws.receive_json()
            request_work_grant(ws, worker_id, incarnation)
            assert realtime.pick_worker(model_id) is not None
            assert realtime.pick_worker_for_model(model_id) is None


@pytest.mark.db
def test_a_refused_update_session_does_not_close_the_browser(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"v6-refupd-{uuid.uuid4()}"
    model_id = f"v6-rt-refupd-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        ws, worker, grant, open_cmd, browser_ws, browser_ctx, fleet_ctx = open_live_session(
            client, worker_id, model_id
        )
        try:
            refuse_command(monkeypatch, "update_session")
            browser_ws.send_json({"type": "update_params", "params": {"prompt": "second"}})
            session = realtime.sessions[uuid.UUID(open_cmd["session_id"])]
            for _ in range(500):
                if session.desired_params["prompt"] == "second":
                    break
                client.portal.call(asyncio.sleep, 0.01)
            assert session.desired_params["prompt"] == "second"
            assert session.desired_revision == 1
        finally:
            browser_ctx.__exit__(None, None, None)
            fleet_ctx.__exit__(None, None, None)


@pytest.mark.db
def test_a_refused_v6_session_fences_its_attempt_without_a_stop_command(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"v6-refused-{uuid.uuid4()}"
    model_id = f"v6-rt-refused-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        incarnation = uuid.uuid4()
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as ws:
            ws.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            ws.receive_json()
            grant = request_work_grant(ws, worker_id, incarnation)
            with client.websocket_connect("/api/v1/realtime") as browser_ws:
                browser_ws.send_json({"type": "open", "model_id": model_id,
                                      "params": {"prompt": "house"}, "frame_header": 2})
                open_cmd = decode_control(ws.receive_text().encode("utf-8"))
                ws.send_json(command_ack(open_cmd))
                refused = {
                    key: open_cmd[key]
                    for key in ("worker_id", "incarnation", "region", "owner_epoch",
                                "lease_id", "lease_expires_at", "session_id",
                                "attempt_id", "control_generation")
                }
                refused.update({"type": "session_refused", "code": "model_unavailable",
                                "grant_nonce": grant["grant_nonce"]})
                validate_message(refused)
                ws.send_json(refused)
                with pytest.raises(WebSocketDisconnect):
                    ws.receive_text()

                async def attempt_state():
                    async with db.session_factory() as session:
                        state = await session.scalar(
                            text("SELECT state FROM realtime_session_attempts "
                                 "WHERE attempt_id = :attempt_id"),
                            {"attempt_id": uuid.UUID(open_cmd["attempt_id"])},
                        )
                        next_sequence = await session.scalar(
                            text("SELECT next_command_sequence FROM worker_connections "
                                 "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                            {"worker_id": worker_id, "incarnation": incarnation},
                        )
                        closed = await session.scalar(
                            text("SELECT closed_at IS NOT NULL FROM worker_connections "
                                 "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                            {"worker_id": worker_id, "incarnation": incarnation},
                        )
                        return state, next_sequence, closed

                for _ in range(200):
                    if client.portal.call(attempt_state)[0] == "ended":
                        break
                    client.portal.call(asyncio.sleep, 0.01)
                assert client.portal.call(attempt_state) == ("ended", 2, True)
