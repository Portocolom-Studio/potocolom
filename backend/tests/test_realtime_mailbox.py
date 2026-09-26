"""Per-session browser mailboxes (issue #19).

The relay rule, docs/connection-handling.md: a shared reader never awaits
delivery to one browser, a mailbox keeps only the latest frame, and lifecycle
controls go ahead of frames. These tests drive the real post/post_frame/
post_close/browser_writer and the real fleet reader, not a replica.
"""

import asyncio
import json
import time
import uuid
from contextlib import suppress

from conftest import run_on_test_loop

from app import realtime
from app.main import app
from app.realtime import GENERATED_FRAME


class FakeBrowser:
    """A browser WebSocket stand-in that records sends, parking in one send
    while blocking, as a transport that never reads would."""

    def __init__(self, blocking: bool = False) -> None:
        self.blocking = blocking
        self.release = asyncio.Event()
        self.in_flight: list[bytes] = []
        self.sent: list = []
        self.close_code: int | None = None

    async def send_bytes(self, data: bytes) -> None:
        if self.blocking:
            self.in_flight.append(data)
            await self.release.wait()
        self.sent.append(data)

    async def send_json(self, message) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000) -> None:
        self.close_code = code


def hello(worker_id="w-mailbox", models=("sd-sim",), slots=2):
    return {
        "type": "hello",
        "protocol_version": realtime.PROTOCOL_VERSION,
        "worker_id": worker_id,
        "models": [{"id": model, "name": model, "capabilities": ["realtime"],
                    "parameters": {}} for model in models],
        "realtime_slots": slots,
    }


class FleetSocket:
    """A worker WebSocket stand-in that feeds the real fleet handler on demand.

    The scenario owns the inbox, so it can wait until the reader is registered
    and parked before injecting the next frame, which is what makes the
    slow-consumer ordering deterministic.
    """

    def __init__(self, worker_id="w-mailbox") -> None:
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.inbox.put_nowait({"type": "websocket.connect"})
        self.inbox.put_nowait({"type": "websocket.receive",
                               "text": json.dumps(hello(worker_id=worker_id))})
        self.sent: list[dict] = []

    async def receive(self):
        return await self.inbox.get()

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    def send_frame(self, data: bytes) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "bytes": data})

    def close(self) -> None:
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})


def _ws_scope(path, headers=()):
    """A minimal ASGI websocket scope for driving an endpoint directly."""
    return {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": list(headers),
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
        "subprotocols": [],
    }


async def pump_writer(session):
    """Let realtime.browser_writer drain the mailbox on a non-blocking fake."""
    for _ in range(20):
        await asyncio.sleep(0)
        if session.writer.done():
            return


def test_post_frame_keeps_only_the_latest_frame():
    """A frame that arrives while the previous one is stuck mid-send replaces
    it, so the slow browser gets exactly the first and the newest, not a
    backlog."""
    async def scenario():
        browser = FakeBrowser(blocking=True)
        session = realtime.Session(id=uuid.uuid4(), model_id="sd-sim",
                                   browser=browser)
        session.writer = asyncio.create_task(realtime.browser_writer(session))
        try:
            realtime.post_frame(session, b"frame-1")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and browser.in_flight != [b"frame-1"]:
                await asyncio.sleep(0.01)
            assert browser.in_flight == [b"frame-1"]
            for index in range(2, 11):
                realtime.post_frame(session, f"frame-{index}".encode())
            assert session.out_frame == b"frame-10"
            browser.release.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and len(browser.sent) < 2:
                await asyncio.sleep(0.01)
            assert browser.sent == [b"frame-1", b"frame-10"]
        finally:
            session.writer.cancel()
            with suppress(asyncio.CancelledError):
                await session.writer

    run_on_test_loop(scenario())


def test_controls_go_ahead_of_frames():
    """Lifecycle controls are delivered before the frame they overtake, even
    when the frame was posted first and the writer starts after both."""
    async def scenario():
        browser = FakeBrowser()
        session = realtime.Session(id=uuid.uuid4(), model_id="sd-sim",
                                   browser=browser)
        realtime.post_frame(session, b"generated-frame")
        realtime.post(session, {"type": "resumed"})
        session.writer = asyncio.create_task(realtime.browser_writer(session))
        try:
            await pump_writer(session)
            assert browser.sent == [{"type": "resumed"}, b"generated-frame"]
        finally:
            session.writer.cancel()
            with suppress(asyncio.CancelledError):
                await session.writer

    run_on_test_loop(scenario())


def test_post_close_sends_controls_then_error_then_close():
    """A posted close empties the queued controls, then the error frame and
    the close code, drops the pending frame, finishes the writer task, and the
    first close wins over any later one."""
    async def scenario():
        browser = FakeBrowser()
        session = realtime.Session(id=uuid.uuid4(), model_id="sd-sim",
                                   browser=browser)
        session.writer = asyncio.create_task(realtime.browser_writer(session))
        try:
            realtime.post(session, {"type": "resumed"})
            realtime.post_frame(session, b"dropped-by-close")
            realtime.post_close(session, 4003, "no worker capacity")
            await pump_writer(session)
            assert browser.sent == [
                {"type": "resumed"},
                {"type": "error", "code": 4003, "message": "no worker capacity"},
            ]
            assert b"dropped-by-close" not in browser.sent
            assert browser.close_code == 4003
            assert session.out_frame is None
            assert not session.out_controls
            assert session.writer.done()
            realtime.post_close(session, 4000, "protocol violation")
            assert session.out_close == (4003, "no worker capacity")
        finally:
            if not session.writer.done():
                session.writer.cancel()
                with suppress(asyncio.CancelledError):
                    await session.writer

    run_on_test_loop(scenario())


def test_a_slow_browser_does_not_stall_the_relay_to_its_neighbour():
    """The fleet reader posts to each session's mailbox instead of awaiting the
    browser inline (the measured neighbour p95 was 6667 ms, scripts/stress.py
    --scenario slow-consumer), so one browser that never reads cannot hold up
    the frames its worker relays to another session."""
    async def scenario():
        saved_workers = dict(realtime.workers)
        saved_sessions = dict(realtime.sessions)
        realtime.workers.clear()
        realtime.sessions.clear()
        worker_socket = FleetSocket(worker_id="w-mailbox-relay")
        scope = _ws_scope("/api/v1/fleet",
                          headers=[(b"x-fleet-token", b"test-fleet-token")])
        task = asyncio.create_task(
            app(scope, worker_socket.receive, worker_socket.send))
        browser_a = FakeBrowser(blocking=True)
        browser_b = FakeBrowser()
        session_a = session_b = None
        try:
            def registered() -> bool:
                return "w-mailbox-relay" in realtime.workers and any(
                    message.get("type") == "websocket.send"
                    and json.loads(message.get("text", "{}")).get("type") == "registered"
                    for message in worker_socket.sent
                )

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not registered():
                await asyncio.sleep(0.01)
            assert registered(), worker_socket.sent
            worker = realtime.workers["w-mailbox-relay"]
            session_a = realtime.Session(
                id=uuid.uuid4(), model_id="sd-sim", browser=browser_a,
                worker=worker, state="live", assigned_at=time.monotonic(),
            )
            session_b = realtime.Session(
                id=uuid.uuid4(), model_id="sd-sim", browser=browser_b,
                worker=worker, state="live", assigned_at=time.monotonic(),
            )
            realtime.sessions.update({session_a.id: session_a,
                                      session_b.id: session_b})
            session_a.writer = asyncio.create_task(realtime.browser_writer(session_a))
            session_b.writer = asyncio.create_task(realtime.browser_writer(session_b))

            frame_a = bytes([GENERATED_FRAME]) + session_a.id.bytes + b"for-slow"
            frame_b = bytes([GENERATED_FRAME]) + session_b.id.bytes + b"for-neighbour"
            worker_socket.send_frame(frame_a)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and browser_a.in_flight != [frame_a]:
                await asyncio.sleep(0.01)
            assert browser_a.in_flight == [frame_a], \
                "the slow browser never received its frame"

            worker_socket.send_frame(frame_b)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and browser_b.sent != [frame_b]:
                await asyncio.sleep(0.01)
            assert browser_b.sent == [frame_b], (
                "the neighbour's frame never arrived: the relay is blocked "
                "on the slow browser's send"
            )
        finally:
            for doomed in (session_a, session_b):
                if doomed is not None:
                    if doomed.writer is not None:
                        doomed.writer.cancel()
                        with suppress(asyncio.CancelledError):
                            await doomed.writer
                    realtime.sessions.pop(doomed.id, None)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            realtime.workers.clear()
            realtime.workers.update(saved_workers)
            realtime.sessions.update(saved_sessions)

    run_on_test_loop(scenario())