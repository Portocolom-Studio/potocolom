"""Every HTTP response carries a server-generated request id, and so does
every application log record produced while the request is handled."""

import asyncio
import io
import json
import logging
import re
from contextlib import asynccontextmanager, contextmanager

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.body_limit import RequestBodyLimitMiddleware
from app.logs import build_handler
from app.main import app
from app.request_id import RequestIdMiddleware, current_request_id
from app.security import SecurityHeadersMiddleware

HEX_32 = re.compile(r"^[0-9a-f]{32}$")
LOG = "potocolom.test_request_id"

client = TestClient(app)


def _app(lifespan=None) -> FastAPI:
    """The production middleware wiring around a few test-only routes."""
    inner = FastAPI(lifespan=lifespan)
    inner.add_middleware(RequestBodyLimitMiddleware)
    inner.add_middleware(SecurityHeadersMiddleware)
    inner.add_middleware(RequestIdMiddleware)

    class Payload(BaseModel):
        n: int

    @inner.get("/api/v1/logged")
    async def logged() -> dict:
        logging.getLogger(LOG).info("handled a logged request")
        return {"ok": True}

    @inner.post("/api/v1/validated")
    async def validated(payload: Payload) -> dict:
        return {"n": payload.n}

    @inner.get("/api/v1/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    @inner.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    return inner


@contextmanager
def _captured(log_format: str):
    """Capture what the real handler and formatter from setup_logging emit."""
    stream = io.StringIO()
    handler = build_handler(log_format, stream)
    root = logging.getLogger()
    previous_level = root.level
    # httpx logs its own line per request; these tests read the stream as the
    # application's records, so keep that noise out.
    client_logger = logging.getLogger("httpx")
    previous_client_level = client_logger.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    client_logger.setLevel(logging.WARNING)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
        client_logger.setLevel(previous_client_level)


def _logged_id(log_format: str, text: str) -> str | None:
    if log_format == "json":
        return json.loads(text).get("request_id")
    match = re.search(r"request_id=([0-9a-f]{32})", text)
    return match.group(1) if match else None


def test_real_app_response_carries_request_id():
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert HEX_32.match(response.headers["x-request-id"])


def test_two_requests_get_distinct_ids():
    first = client.get("/api/v1/health").headers["x-request-id"]
    second = client.get("/api/v1/health").headers["x-request-id"]
    assert HEX_32.match(first)
    assert HEX_32.match(second)
    assert first != second


def test_404_carries_request_id():
    response = client.get("/api/v1/no-such-endpoint")
    assert response.status_code == 404
    assert HEX_32.match(response.headers["x-request-id"])


def test_413_body_limit_carries_request_id(monkeypatch):
    monkeypatch.setattr("app.body_limit.MAX_JSON_BODY_BYTES", 32)
    response = client.post("/api/v1/shared", json={"token": "a" * 64})
    assert response.status_code == 413
    assert HEX_32.match(response.headers["x-request-id"])


def test_422_validation_error_carries_request_id():
    inner = _app()
    with TestClient(inner) as inner_client:
        response = inner_client.post("/api/v1/validated", json={"n": "not-an-int"})
    assert response.status_code == 422
    assert HEX_32.match(response.headers["x-request-id"])


@pytest.mark.parametrize("log_format", ["plain", "json"])
def test_route_log_carries_the_response_id(log_format):
    inner = _app()
    with _captured(log_format) as stream, TestClient(inner) as inner_client:
        response = inner_client.get("/api/v1/logged")
    assert response.status_code == 200
    request_id = response.headers["x-request-id"]
    assert HEX_32.match(request_id)
    lines = [line for line in stream.getvalue().splitlines() if "handled a logged" in line]
    assert len(lines) == 1
    assert _logged_id(log_format, lines[0]) == request_id
    if log_format == "plain":
        assert lines[0].endswith(
            f"INFO {LOG}: handled a logged request request_id={request_id}"
        )
    else:
        assert json.loads(lines[0])["request_id"] == request_id


@pytest.mark.parametrize("log_format", ["plain", "json"])
def test_unhandled_500_header_matches_the_logged_error(log_format):
    inner = _app()
    with _captured(log_format) as stream, TestClient(
        inner, raise_server_exceptions=False
    ) as inner_client:
        response = inner_client.get("/api/v1/boom")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    request_id = response.headers["x-request-id"]
    assert HEX_32.match(request_id)
    output = stream.getvalue()
    assert "unhandled exception on GET /api/v1/boom" in output
    if log_format == "json":
        lines = output.splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["request_id"] == request_id
    else:
        first = next(line for line in output.splitlines() if "unhandled exception" in line)
        assert first.endswith(f"GET /api/v1/boom request_id={request_id}")


@pytest.mark.parametrize("hostile", [
    "0" * 32 + "a" * 4096 + " INFO potocolom.fake: invented log line",
    "0123456789abcdef" * 2,
])
def test_client_supplied_id_is_ignored(hostile):
    inner = _app()
    with _captured("plain") as stream, TestClient(inner) as inner_client:
        response = inner_client.get("/api/v1/logged", headers={"x-request-id": hostile})
    request_id = response.headers["x-request-id"]
    assert HEX_32.match(request_id)
    assert request_id != hostile
    output = stream.getvalue()
    assert hostile not in output
    assert "potocolom.fake" not in output


def test_header_with_crlf_and_a_fake_log_line_is_ignored():
    hostile = b"deadbeef\r\n2026-01-01 INFO potocolom.fake: invented log line"
    inner = _app()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/logged",
        "raw_path": b"/api/v1/logged",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"x-request-id", hostile)],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    with _captured("plain") as stream:
        asyncio.run(inner(scope, receive, send))

    start = next(message for message in messages if message["type"] == "http.response.start")
    assert start["status"] == 200
    ours = [value for name, value in start["headers"] if name == b"x-request-id"]
    assert len(ours) == 1
    assert HEX_32.match(ours[0].decode())
    output = stream.getvalue()
    assert hostile.decode() not in output
    assert "potocolom.fake" not in output


@pytest.mark.parametrize("log_format", ["plain", "json"])
def test_background_logs_after_a_request_carry_no_id(log_format):
    """A task started at lifespan copied a context with no request in it; a
    finished request must not leave its id where later records can pick it up."""
    state: dict = {}

    @asynccontextmanager
    async def lifespan(_inner: FastAPI):
        fired = asyncio.Event()
        logged = asyncio.Event()

        async def background() -> None:
            await fired.wait()
            logging.getLogger(LOG).info("background line")
            logged.set()

        task = asyncio.create_task(background())
        state.update(fired=fired, logged=logged)
        yield
        task.cancel()

    inner = _app(lifespan)
    with _captured(log_format) as stream, TestClient(inner) as inner_client:
        response = inner_client.get("/api/v1/logged")
        assert HEX_32.match(response.headers["x-request-id"])
        inner_client.portal.call(state["fired"].set)
        inner_client.portal.call(state["logged"].wait)
    lines = [line for line in stream.getvalue().splitlines() if "background line" in line]
    assert len(lines) == 1
    assert _logged_id(log_format, lines[0]) is None


def test_websocket_still_works():
    inner = _app()
    with TestClient(inner) as inner_client:
        with inner_client.websocket_connect("/ws") as session:
            assert session.receive_text() == "hello"
    assert current_request_id.get() is None


def test_the_id_does_not_outlive_its_request_in_the_same_task():
    inner = _app()
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": "/api/v1/logged",
        "raw_path": b"/api/v1/logged", "query_string": b"", "root_path": "",
        "headers": [(b"host", b"testserver")], "server": ("testserver", 80),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    async def request_then_log() -> None:
        await inner(scope, receive, send)
        logging.getLogger(LOG).info("after the request")

    with _captured("plain") as stream:
        asyncio.run(request_then_log())
    after = next(line for line in stream.getvalue().splitlines() if "after the request" in line)
    assert "request_id=" not in after
