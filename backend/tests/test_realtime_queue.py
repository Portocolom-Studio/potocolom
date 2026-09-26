"""The realtime admission queue (issue #19, part 2).

An open that finds no free worker is queued instead of refused 4003; a
reassigned or idle session with no candidate queues too. Admission is first-fit
in queued_at order: a queued session whose model has no room does not block a
later one whose model has room. These tests drive the real endpoints and the
real queue helpers, not a replica.
"""

import asyncio
import time
import uuid
from contextlib import suppress

import anyio
import pytest
from conftest import run_on_test_loop
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import realtime, sessions
from app.main import app
from app.manifests import Manifest
from app.realtime import CANVAS_FRAME
from tests.test_realtime import (
    FLEET_HEADERS,
    REQUIRES_PROMPT,
    FakeSocket,
    answer_ready,
    client,
    expect,
    hello,
    manifest,
    pump_writer,
)
from tests.test_realtime_auth import _open, _signed_in, accounts

__all__ = ["accounts"]


@pytest.fixture(autouse=True, scope="module")
def _one_event_loop():
    """Every socket in this file on one event loop, as the server runs them.

    An unentered TestClient gives each connection a portal of its own, so the
    fleet handler and the browser handler ran on different loops while sharing
    realtime.py's module state (issues #431, #440). Entering the client would
    share a portal too, but it would also run the app lifespan, which these
    tests do not want.
    """
    with anyio.from_thread.start_blocking_portal("asyncio") as portal:
        client.portal = portal
        yield
        client.portal = None


def test_a_full_pool_queues_an_open_and_admits_it_later():
    """One slot, one live session: a second browser is queued at position 1,
    not refused 4003, and is admitted with ready once the first closes."""
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-queue-one", slots=1))
        expect(worker_ws, "registered")
        with client.websocket_connect("/api/v1/realtime") as first_ws:
            first_ws.send_json({"type": "open", "model_id": "sd-sim"})
            opened = expect(worker_ws, "open_session")
            answer_ready(worker_ws, opened)
            expect(first_ws, "ready")
            with client.websocket_connect("/api/v1/realtime") as second_ws:
                second_ws.send_json({"type": "open", "model_id": "sd-sim"})
                queued = expect(second_ws, "queued")
                assert queued["position"] == 1
                waiting = next(s for s in realtime.sessions.values()
                               if s.state == "queued")
                # The first browser closes, freeing the single slot.
                first_ws.send_json({"type": "close"})
                with pytest.raises(WebSocketDisconnect):
                    first_ws.receive_json()
                closed = expect(worker_ws, "close_session")
                assert closed["session_id"] == opened["session_id"]
                reopened = expect(worker_ws, "open_session")
                assert reopened["session_id"] == str(waiting.id)
                answer_ready(worker_ws, reopened)
                ready = expect(second_ws, "ready")
                assert ready["session_id"] == str(waiting.id)
                assert waiting.state == "live"


def test_two_queued_one_slot_frees_admits_exactly_one(monkeypatch):
    """With two queued and one slot freed, exactly one is admitted; the other
    is still queued and is told its new position 1."""
    monkeypatch.setattr(realtime, "MAX_REALTIME_SESSIONS_PER_USER", 8)
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-queue-two", slots=1))
        expect(worker_ws, "registered")
        with client.websocket_connect("/api/v1/realtime") as live_ws:
            live_ws.send_json({"type": "open", "model_id": "sd-sim"})
            opened = expect(worker_ws, "open_session")
            answer_ready(worker_ws, opened)
            expect(live_ws, "ready")
            with client.websocket_connect("/api/v1/realtime") as head_ws:
                head_ws.send_json({"type": "open", "model_id": "sd-sim"})
                head_queued = expect(head_ws, "queued")
                assert head_queued["position"] == 1
                with client.websocket_connect("/api/v1/realtime") as tail_ws:
                    tail_ws.send_json({"type": "open", "model_id": "sd-sim"})
                    tail_queued = expect(tail_ws, "queued")
                    assert tail_queued["position"] == 2
                    live_ws.send_json({"type": "close"})
                    with pytest.raises(WebSocketDisconnect):
                        live_ws.receive_json()
                    expect(worker_ws, "close_session")
                    reopened = expect(worker_ws, "open_session")
                    answer_ready(worker_ws, reopened)
                    admitted = expect(head_ws, "ready")
                    assert admitted["session_id"] == reopened["session_id"]
                    waiting = expect(tail_ws, "queued")
                    assert waiting["position"] == 1


def test_a_queued_session_leaving_shifts_positions(monkeypatch):
    """A queued session whose browser closes leaves the queue; the one behind
    moves from position 2 to 1."""
    monkeypatch.setattr(realtime, "MAX_REALTIME_SESSIONS_PER_USER", 8)
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-queue-shift", slots=1))
        expect(worker_ws, "registered")
        with client.websocket_connect("/api/v1/realtime") as live_ws:
            live_ws.send_json({"type": "open", "model_id": "sd-sim"})
            opened = expect(worker_ws, "open_session")
            answer_ready(worker_ws, opened)
            expect(live_ws, "ready")
            with client.websocket_connect("/api/v1/realtime") as head_ws:
                head_ws.send_json({"type": "open", "model_id": "sd-sim"})
                expect(head_ws, "queued")
                with client.websocket_connect("/api/v1/realtime") as tail_ws:
                    tail_ws.send_json({"type": "open", "model_id": "sd-sim"})
                    tail_queued = expect(tail_ws, "queued")
                    assert tail_queued["position"] == 2
                    head_ws.send_json({"type": "close"})
                    with pytest.raises(WebSocketDisconnect):
                        head_ws.receive_json()
                    moved = expect(tail_ws, "queued")
                    assert moved["position"] == 1


@pytest.mark.db
def test_revoking_a_queued_session_closes_it_without_an_open(accounts):
    """A revoked queued session's browser gets 4401 and the close, no
    open_session for it ever reaches the worker (the holder still owns the
    only slot), and the session is gone from realtime.sessions."""
    with TestClient(app, client=("127.0.0.1", 50000),
                    headers=FLEET_HEADERS) as client:
        holder, _ = _signed_in(client, "queue-holder@example.com")
        with client.websocket_connect("/api/v1/fleet") as worker_ws:
            worker_ws.send_json(hello(worker_id="w-queue-revoke", slots=1,
                                      parameters=REQUIRES_PROMPT))
            assert worker_ws.receive_json()["type"] == "registered"
            with client.websocket_connect("/api/v1/realtime") as holder_ws:
                _open(holder_ws)
                opened = worker_ws.receive_json()
                answer_ready(worker_ws, opened)
                assert holder_ws.receive_json()["type"] == "ready"
                # A second account queues behind the holder's slot. The cookie
                # is re-minted before this socket opens, so the two sockets
                # bind different principals.
                queued_user, queued_issued = _signed_in(
                    client, "queue-queued@example.com")
                with client.websocket_connect("/api/v1/realtime") as queued_ws:
                    _open(queued_ws)
                    queued_msg = queued_ws.receive_json()
                    assert queued_msg["type"] == "queued"
                    assert queued_msg["position"] == 1
                    queued = next(s for s in realtime.sessions.values()
                                  if s.user_id == queued_user.id)
                    assert queued.state == "queued"
                    assert queued.worker is None
                    resolved = client.portal.call(sessions.resolve,
                                                  queued_issued.token)
                    client.portal.call(sessions.revoke, resolved.session.id)
                    closing = queued_ws.receive_json()
                    assert closing["type"] == "error"
                    assert closing["code"] == realtime.CLOSE_UNAUTHORIZED == 4401
                    # Gone from the map, and the only slot is still the
                    # holder's: an open_session for the queued session would
                    # have needed a slot (assign) and one was never taken.
                    assert queued.id not in realtime.sessions
                    assert realtime.workers["w-queue-revoke"].slots_in_use == 1
                    assert queued.worker is None


def test_a_worker_registration_admits_the_head():
    """Register a busy worker, queue a session, then register a second worker
    for the same model: the queued session is admitted via the new worker."""
    with client.websocket_connect("/api/v1/fleet") as first_worker_ws:
        first_worker_ws.send_json(hello(worker_id="w-queue-reg-a", slots=1))
        expect(first_worker_ws, "registered")
        with client.websocket_connect("/api/v1/realtime") as live_ws:
            live_ws.send_json({"type": "open", "model_id": "sd-sim"})
            opened = expect(first_worker_ws, "open_session")
            answer_ready(first_worker_ws, opened)
            expect(live_ws, "ready")
            with client.websocket_connect("/api/v1/realtime") as waiting_ws:
                waiting_ws.send_json({"type": "open", "model_id": "sd-sim"})
                queued = expect(waiting_ws, "queued")
                assert queued["position"] == 1
                waiting = next(s for s in realtime.sessions.values()
                               if s.state == "queued")
                with client.websocket_connect("/api/v1/fleet") as second_worker_ws:
                    second_worker_ws.send_json(hello(worker_id="w-queue-reg-b",
                                                     slots=1))
                    expect(second_worker_ws, "registered")
                    received = expect(second_worker_ws, "open_session")
                    assert received["session_id"] == str(waiting.id)
                    answer_ready(second_worker_ws, received)
                    ready = expect(waiting_ws, "ready")
                    assert ready["session_id"] == str(waiting.id)
                    assert waiting.worker is realtime.workers["w-queue-reg-b"]


def test_reassign_with_no_candidate_queues_then_resumes():
    """A live session whose only worker disconnects is interrupted, then
    queued; a new worker registering admits it with resumed and an
    open_session on the new worker."""
    with client.websocket_connect("/api/v1/realtime") as browser_ws:
        with client.websocket_connect("/api/v1/fleet") as worker_ws:
            worker_ws.send_json(hello(worker_id="w-queue-reassign", slots=1))
            expect(worker_ws, "registered")
            browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
            opened = expect(worker_ws, "open_session")
            answer_ready(worker_ws, opened)
            expect(browser_ws, "ready")
            session = realtime.sessions[uuid.UUID(opened["session_id"])]
        # The only worker went away: interrupted, then queued.
        expect(browser_ws, "interrupted")
        queued = expect(browser_ws, "queued")
        assert queued["position"] == 1
        assert session.state == "queued"
        with client.websocket_connect("/api/v1/fleet") as new_worker_ws:
            new_worker_ws.send_json(hello(worker_id="w-queue-reassign-b", slots=1))
            expect(new_worker_ws, "registered")
            reopened = expect(new_worker_ws, "open_session")
            assert reopened["session_id"] == str(session.id)
            answer_ready(new_worker_ws, reopened)
            expect(browser_ws, "resumed")
            assert session.state == "live"
            assert session.worker is realtime.workers["w-queue-reassign-b"]


def test_idle_resume_with_no_room_queues_and_resumes(monkeypatch):
    """An idle session whose re-placement finds no room is queued on its next
    frame; when the slot frees it is resumed and the worker receives the
    pending canvas frame."""
    monkeypatch.setattr(realtime, "IDLE_RELEASE_SECONDS", 0.05)
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-queue-idle", slots=1))
        expect(worker_ws, "registered")
        with client.websocket_connect("/api/v1/realtime") as first_ws:
            first_ws.send_json({"type": "open", "model_id": "sd-sim"})
            opened = expect(worker_ws, "open_session")
            answer_ready(worker_ws, opened)
            expect(first_ws, "ready")
            first = realtime.sessions[uuid.UUID(opened["session_id"])]
            first.last_input = time.monotonic() - 1
            client.portal.call(realtime.release_idle_sessions)
            expect(worker_ws, "close_session")
            assert first.state == "idle"
            with client.websocket_connect("/api/v1/realtime") as second_ws:
                second_ws.send_json({"type": "open", "model_id": "sd-sim"})
                second_opened = expect(worker_ws, "open_session")
                answer_ready(worker_ws, second_opened)
                expect(second_ws, "ready")
                canvas = bytes([CANVAS_FRAME]) + first.id.bytes + b"resume-me"
                first_ws.send_bytes(canvas)
                queued = expect(first_ws, "queued")
                assert queued["position"] == 1
                assert first.state == "queued"
            # The second session closed: the idle session resumes with its
            # newest pending frame.
            closed = expect(worker_ws, "close_session")
            assert closed["session_id"] == second_opened["session_id"]
            reopened = expect(worker_ws, "open_session")
            assert reopened["session_id"] == str(first.id)
            answer_ready(worker_ws, reopened)
            assert worker_ws.receive_bytes() == canvas
            expect(first_ws, "resumed")
            assert first.state == "live"


def test_first_fit_skips_a_head_without_room():
    """The head of the queue wanting a full model does not block the next
    session whose model has a free slot: the second is admitted first-fit."""
    with client.websocket_connect("/api/v1/fleet") as worker_a_ws:
        worker_a_ws.send_json(hello(worker_id="w-queue-fit-a", slots=1))
        expect(worker_a_ws, "registered")
        with client.websocket_connect("/api/v1/fleet") as worker_b_ws:
            worker_b_ws.send_json(hello(worker_id="w-queue-fit-b",
                                        models=("other-model",), slots=1))
            expect(worker_b_ws, "registered")
            worker_a = realtime.workers["w-queue-fit-a"]
            worker_b = realtime.workers["w-queue-fit-b"]
            occupant_a = realtime.Session(
                id=uuid.uuid4(), model_id="sd-sim", browser=FakeSocket(),
                worker=worker_a, state="live", assigned_at=time.monotonic(),
                user_id=uuid.uuid4())
            occupant_b = realtime.Session(
                id=uuid.uuid4(), model_id="other-model", browser=FakeSocket(),
                worker=worker_b, state="live", assigned_at=time.monotonic(),
                user_id=uuid.uuid4())
            worker_a.slots_in_use = 1
            worker_b.slots_in_use = 1
            realtime.sessions.update({occupant_a.id: occupant_a,
                                      occupant_b.id: occupant_b})
            try:
                with client.websocket_connect("/api/v1/realtime") as head_ws:
                    head_ws.send_json({"type": "open", "model_id": "sd-sim"})
                    head_queued = expect(head_ws, "queued")
                    assert head_queued["position"] == 1
                    with client.websocket_connect("/api/v1/realtime") as tail_ws:
                        tail_ws.send_json({"type": "open", "model_id": "other-model"})
                        tail_queued = expect(tail_ws, "queued")
                        assert tail_queued["position"] == 2
                        head = realtime.queued_sessions()[0]
                        tail = realtime.queued_sessions()[1]
                        # A slot frees on B's worker: the tail is admitted
                        # while the head's model stays full.
                        client.portal.call(realtime.release, occupant_b)
                        closed = expect(worker_b_ws, "close_session")
                        assert closed["session_id"] == str(occupant_b.id)
                        tail_open = expect(worker_b_ws, "open_session")
                        assert tail_open["session_id"] == str(tail.id)
                        answer_ready(worker_b_ws, tail_open)
                        ready = expect(tail_ws, "ready")
                        assert ready["session_id"] == str(tail.id)
                        assert tail.state == "live"
                        assert head.state == "queued"
                        assert realtime.queued_sessions()[0] is head
                        assert head.queued_position == 1
                        assert realtime.workers["w-queue-fit-a"].slots_in_use == 1
            finally:
                realtime.sessions.pop(occupant_a.id, None)
                realtime.sessions.pop(occupant_b.id, None)


def test_the_account_cap_still_counts_queued_sessions():
    """A third socket for one account is refused 4003 with the account
    message even when one held session is only queued."""
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-queue-cap", slots=1))
        expect(worker_ws, "registered")
        with client.websocket_connect("/api/v1/realtime") as live_ws:
            live_ws.send_json({"type": "open", "model_id": "sd-sim"})
            opened = expect(worker_ws, "open_session")
            answer_ready(worker_ws, opened)
            expect(live_ws, "ready")
            with client.websocket_connect("/api/v1/realtime") as queued_ws:
                queued_ws.send_json({"type": "open", "model_id": "sd-sim"})
                expect(queued_ws, "queued")
                with client.websocket_connect("/api/v1/realtime") as third_ws:
                    third_ws.send_json({"type": "open", "model_id": "sd-sim"})
                    refused = expect(third_ws, "error")
                    assert refused["code"] == realtime.CLOSE_NO_CAPACITY == 4003
                    assert refused["message"] == "too many realtime sessions for this account"


def test_force_repost_repeats_unchanged_positions():
    """The sweep's forced repost posts the same position twice, as a keepalive
    for a queued socket that has not moved."""

    async def scenario():
        browser = FakeSocket()
        session = realtime.Session(id=uuid.uuid4(), model_id="sd-sim",
                                   browser=browser, state="queued",
                                   queued_at=time.monotonic())
        realtime.sessions[session.id] = session
        session.writer = asyncio.create_task(realtime.browser_writer(session))
        try:
            realtime.post_positions(force=True)
            await pump_writer(session)
            realtime.post_positions(force=True)
            await pump_writer(session)
            assert browser.sent == [
                {"type": "queued", "position": 1},
                {"type": "queued", "position": 1},
            ]
        finally:
            session.writer.cancel()
            with suppress(asyncio.CancelledError):
                await session.writer
            realtime.sessions.pop(session.id, None)

    run_on_test_loop(scenario())


def test_a_requeued_session_keeps_its_place(monkeypatch):
    """A queued session that is admitted and then refused again keeps its
    earlier queued_at, so the head stays the head and a later slot goes to it
    first."""
    monkeypatch.setattr(realtime, "SESSION_READY_TIMEOUT", 0.05)

    async def scenario():
        worker_ws = FakeSocket()
        worker = realtime.Worker(id="w-requeue-keep", ws=worker_ws,
                                 manifests=[Manifest.model_validate(manifest())],
                                 realtime_slots=1)
        realtime.workers[worker.id] = worker
        head = realtime.Session(id=uuid.uuid4(), model_id="sd-sim",
                                browser=FakeSocket(), state="queued",
                                queued_at=1.0, queued_position=1)
        tail = realtime.Session(id=uuid.uuid4(), model_id="sd-sim",
                                browser=FakeSocket(), state="queued",
                                queued_at=2.0, queued_position=2)
        realtime.sessions.update({head.id: head, tail.id: tail})
        try:
            # Both pass the synchronous check for the one slot; the head wins
            # it by starting its placement first, then the attempt is refused.
            realtime.admit_queued()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and tail.state != "queued":
                await asyncio.sleep(0.01)
            assert tail.state == "queued"
            assert head.state == "assigning"
            opened = next(m for m in worker_ws.sent
                          if m.get("type") == "open_session")
            assert opened["session_id"] == str(head.id)
            head.attempt_ok = False
            head.ready.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and head.state != "queued":
                await asyncio.sleep(0.01)
            assert head.state == "queued"
            assert realtime.queued_sessions() == [head, tail]
            assert head.queued_position == 1
            assert tail.queued_position == 2
        finally:
            deadline = time.monotonic() + 5
            while realtime._admit_tasks and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            realtime.workers.pop(worker.id, None)
            realtime.sessions.pop(head.id, None)
            realtime.sessions.pop(tail.id, None)

    run_on_test_loop(scenario())