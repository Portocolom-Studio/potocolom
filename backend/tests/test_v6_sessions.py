"""Protocol 6 realtime sessions on local fleet sockets."""

import asyncio
import hashlib
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

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
        ws, worker, grant, open_cmd, browser_ws, browser_ctx, fleet_ctx = open_live_session(
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
            expect(browser_ws, "params_updated")
            updated = decode_control(ws.receive_text().encode("utf-8"))
            assert updated["type"] == "update_session"
            assert updated["params_revision"] == 2
        finally:
            browser_ctx.__exit__(None, None, None)
            fleet_ctx.__exit__(None, None, None)


@pytest.mark.db
def test_v6_session_close_receipt_and_ended_state(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"v6-close-{uuid.uuid4()}"
    model_id = f"v6-rt-close-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        ws, worker, grant, open_cmd, browser_ws, browser_ctx, fleet_ctx = open_live_session(
            client, worker_id, model_id
        )
        try:
            browser_ws.send_json({"type": "close"})
            close_cmd = decode_control(ws.receive_text().encode("utf-8"))
            assert close_cmd["type"] == "close_session"
            ws.send_json(command_ack(close_cmd))
            closed = session_closed_message(worker, open_cmd, grant["grant_nonce"])
            ws.send_text(json.dumps(closed, separators=(",", ":")))
            ack = decode_control(ws.receive_text().encode("utf-8"))
            assert ack["type"] == "checkpoint_ack"
            assert ack["status"] == "accepted"
            session_id = uuid.UUID(open_cmd["session_id"])
            range_id = uuid.UUID(open_cmd["work_budget"]["range_id"])

            async def read_terminal():
                async with db.session_factory() as session:
                    receipt = await session.scalar(
                        text(
                            "SELECT count(*) FROM worker_report_receipts "
                            "WHERE range_id = :range_id"
                        ),
                        {"range_id": range_id},
                    )
                    sess_state = await session.scalar(
                        text("SELECT state FROM realtime_sessions WHERE id = :id"),
                        {"id": session_id},
                    )
                    attempt_state = await session.scalar(
                        text(
                            "SELECT state FROM realtime_session_attempts "
                            "WHERE session_id = :id"
                        ),
                        {"id": session_id},
                    )
                    return receipt, sess_state, attempt_state

            receipt, sess_state, attempt_state = client.portal.call(read_terminal)
            assert receipt == 1
            assert sess_state == "ended"
            assert attempt_state == "ended"
        finally:
            browser_ctx.__exit__(None, None, None)
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
def test_v6_second_session_on_same_worker_after_close(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"v6-reopen-{uuid.uuid4()}"
    model_id = f"v6-rt-reopen-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        ws, worker, grant, open_cmd, browser_ws, browser_ctx, fleet_ctx = open_live_session(
            client, worker_id, model_id
        )
        try:
            browser_ws.send_json({"type": "close"})
            close_cmd = decode_control(ws.receive_text().encode("utf-8"))
            assert close_cmd["type"] == "close_session"
            ws.send_json(command_ack(close_cmd))
            closed = session_closed_message(worker, open_cmd, grant["grant_nonce"])
            ws.send_text(json.dumps(closed, separators=(",", ":")))
            decode_control(ws.receive_text().encode("utf-8"))
        finally:
            browser_ctx.__exit__(None, None, None)

        with client.websocket_connect("/api/v1/realtime") as browser_ws2:
            browser_ws2.send_json({
                "type": "open",
                "model_id": model_id,
                "params": {"prompt": "again"},
            })
            open_cmd2 = decode_control(ws.receive_text().encode("utf-8"))
            assert open_cmd2["type"] == "open_session"
            ws.send_json(command_ack(open_cmd2))
            ws.send_json(session_ready_message(worker, open_cmd2, grant["grant_nonce"]))
            expect(browser_ws2, "ready")
        fleet_ctx.__exit__(None, None, None)


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
            expect(browser_ws, "params_updated")
        finally:
            browser_ctx.__exit__(None, None, None)
            fleet_ctx.__exit__(None, None, None)


@pytest.mark.db
def test_a_refused_v6_session_is_closed_on_the_worker_before_its_attempt_ends(monkeypatch):
    """close_session needs the attempt still open; abandoning first would get
    the close refused and leave the worker holding the runner."""
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
                close = decode_control(ws.receive_text().encode("utf-8"))
                assert close["type"] == "close_session"
                assert close["attempt_id"] == open_cmd["attempt_id"]

                async def attempt_state():
                    async with db.session_factory() as session:
                        return await session.scalar(
                            text("SELECT state FROM realtime_session_attempts "
                                 "WHERE attempt_id = :attempt_id"),
                            {"attempt_id": uuid.UUID(open_cmd["attempt_id"])},
                        )

                for _ in range(200):
                    if client.portal.call(attempt_state) == "ended":
                        break
                    client.portal.call(asyncio.sleep, 0.01)
                assert client.portal.call(attempt_state) == "ended"
