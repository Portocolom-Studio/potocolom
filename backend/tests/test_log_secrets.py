"""Bearer capabilities and worker-supplied failure text stay out of every
log line (issue #690).

Access records are driven through the real filter and uvicorn's own
AccessFormatter: first as records shaped exactly the way uvicorn emits them,
then through a real in-process server whose lifespan installs the filter.
Job failures travel the real fleet WebSocket and are captured through the
real setup_logging handler in both formats. Every scenario carries a unique
canary that must never appear in a log line.
"""

import io
import logging
import socket
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from uvicorn.logging import AccessFormatter

from app import access_log, jobs, realtime
from app.logs import setup_logging
from app.main import app
from app.realtime import PROTOCOL_VERSION
from app.settings import get_settings
from test_jobs import FLEET_HEADERS, fleet_hello, png_bytes, poll_until, put_upload

# Exactly the format uvicorn's LOGGING_CONFIG gives its access handler.
_ACCESS_FORMAT = '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'


def _capture_access() -> tuple[logging.Handler, list[str]]:
    handler = logging.Handler()
    handler.setFormatter(AccessFormatter(_ACCESS_FORMAT, use_colors=False))
    lines: list[str] = []
    handler.emit = lambda record: lines.append(handler.format(record))
    return handler, lines


def _drive_access_record(path: str, status: int) -> str:
    """One record shaped the way uvicorn emits it, rendered by uvicorn's own
    formatter after the installed filter has seen it."""
    logger = logging.getLogger(access_log.ACCESS_LOGGER)
    access_log.install()
    handler, lines = _capture_access()
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:50123", "GET", path, "1.1", status)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    assert len(lines) == 1, lines
    return lines[0]


def test_an_access_line_never_carries_a_query_string():
    canary = f"access-canary-{uuid.uuid4().hex}"
    for status in (200, 403):
        line = _drive_access_record(
            f"/api/v1/worker-input?token={canary}&expires=9", status
        )
        assert canary not in line, line
        assert "GET /api/v1/worker-input HTTP/1.1" in line, line
        assert str(status) in line, line


def test_an_ordinary_access_line_keeps_its_path():
    line = _drive_access_record("/api/v1/health", 200)
    assert "GET /api/v1/health HTTP/1.1" in line, line
    assert "200" in line, line


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_health(base: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/api/v1/health", timeout=2).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.05)
    raise AssertionError("the in-process server never answered /api/v1/health")


@pytest.mark.db
def test_a_live_uvicorn_access_log_never_carries_the_capability():
    canary = f"live-canary-{uuid.uuid4().hex}"
    expires = int(time.time()) + 3600
    key = f"input/{uuid.uuid4().hex}.png"
    root = Path(get_settings().storage_local_path)
    (root / key).parent.mkdir(parents=True, exist_ok=True)
    (root / key).write_bytes(png_bytes())
    jobs.register_input_capability(canary, key, expires)

    access_logger = logging.getLogger(access_log.ACCESS_LOGGER)
    # Earlier tests in this file installed the filter already; clearing it
    # makes the assertions test this app's lifespan wiring, not that call.
    access_logger.filters.clear()
    handler, lines = _capture_access()
    access_logger.addHandler(handler)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_config=None, lifespan="on",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_for_health(base)
        fetched = httpx.get(
            f"{base}/api/v1/worker-input?token={canary}&expires={expires}", timeout=10
        )
        assert fetched.status_code == 200
        refused = httpx.get(
            f"{base}/api/v1/worker-input?token={canary}-wrong&expires={expires}", timeout=10
        )
        assert refused.status_code == 403
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        access_logger.removeHandler(handler)
        jobs.input_capabilities.pop(canary, None)

    joined = "\n".join(lines)
    assert canary not in joined, joined
    assert '"GET /api/v1/worker-input HTTP/1.1" 200' in joined, joined
    assert '"GET /api/v1/worker-input HTTP/1.1" 403' in joined, joined
    assert '"GET /api/v1/health HTTP/1.1" 200' in joined, joined


def _capture_app_logs(log_format: str) -> tuple[io.StringIO, Callable[[], None]]:
    """Point the real setup_logging handler at a buffer, formatter and all."""
    setup_logging(log_format)
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    buffer = io.StringIO()
    original = handler.stream
    handler.setStream(buffer)
    return buffer, lambda: handler.setStream(original)


def _fail_one_job_over_fleet(worker_ws, client, reason: str) -> tuple[str, dict]:
    fleet_hello(worker_ws, "w-log-canary")
    job_id = client.post(
        "/api/v1/generations",
        json={"model_id": "sd-test", "params": {"prompt": "fail"}},
    ).json()["job_id"]
    dispatch = worker_ws.receive_json()
    worker_ws.send_json({"type": "job_failed", "job_id": job_id, "reason": reason,
                         "dispatch_token": dispatch["dispatch_token"]})
    return job_id, poll_until(client, job_id, "failed")


@pytest.mark.db
@pytest.mark.parametrize("log_format", ["plain", "json"])
def test_a_worker_failure_reason_never_reaches_the_log(log_format):
    canary = f"reason-canary-{uuid.uuid4().hex}"
    with TestClient(app, headers=FLEET_HEADERS) as client:
        buffer, restore = _capture_app_logs(log_format)
        try:
            with client.websocket_connect("/api/v1/fleet") as worker_ws:
                job_id, job = _fail_one_job_over_fleet(worker_ws, client, canary)
        finally:
            output = buffer.getvalue()
            restore()
    assert job["failure_reason"] == canary
    assert canary not in output, output
    assert f"job {job_id} failed" in output, output


@pytest.mark.db
@pytest.mark.parametrize("log_format", ["plain", "json"])
def test_an_unstorable_failure_reason_never_reaches_the_log(log_format):
    canary = f"nul-reason-canary-{uuid.uuid4().hex}"
    with TestClient(app, headers=FLEET_HEADERS) as client:
        buffer, restore = _capture_app_logs(log_format)
        try:
            with client.websocket_connect("/api/v1/fleet") as worker_ws:
                job_id, job = _fail_one_job_over_fleet(
                    worker_ws, client, f"boom\x00{canary}"
                )
        finally:
            output = buffer.getvalue()
            restore()
    assert job["failure_reason"] == "worker reported failure"
    assert canary not in output, output
    assert f"job {job_id} failed" in output, output
    assert "unstorable" in output, output


@pytest.mark.db
def test_a_job_done_gpu_ms_never_reaches_the_log():
    canary = f"gpu-ms-canary-{uuid.uuid4().hex}"
    with TestClient(app, headers=FLEET_HEADERS) as client:
        buffer, restore = _capture_app_logs("plain")
        try:
            with client.websocket_connect("/api/v1/fleet") as worker_ws:
                fleet_hello(worker_ws, "w-gpu-ms-canary")
                job_id = client.post(
                    "/api/v1/generations",
                    json={"model_id": "sd-test", "params": {"prompt": "gpu ms"}},
                ).json()["job_id"]
                dispatch = worker_ws.receive_json()
                assert put_upload(
                    client, dispatch["upload"], png_bytes()
                ).status_code == 200
                worker_ws.send_json({"type": "job_done", "job_id": job_id,
                                     "gpu_ms": canary, "width": 512, "height": 512,
                                     "dispatch_token": dispatch["dispatch_token"]})
                job = poll_until(client, job_id, "succeeded")
        finally:
            output = buffer.getvalue()
            restore()
    assert job["gpu_ms"] == 0
    assert canary not in output, output
    assert f"job {job_id} succeeded" in output, output


def _capture_logger(name: str) -> tuple[logging.Handler, list[str]]:
    records: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    logging.getLogger(name).addHandler(handler)
    return handler, records


def test_a_refused_hello_never_logs_its_manifest_text():
    canary = f"hello-canary-{uuid.uuid4().hex}"
    handler, records = _capture_logger("potocolom.realtime")
    try:
        with TestClient(app, headers=FLEET_HEADERS) as client:
            with client.websocket_connect("/api/v1/fleet") as worker_ws:
                worker_ws.send_json({
                    "type": "hello",
                    "protocol_version": PROTOCOL_VERSION,
                    "worker_id": "w-hello-canary",
                    "realtime_slots": 1,
                    "models": [{
                        "id": "m",
                        "name": f"n\x00{canary}",
                        "capabilities": ["text_to_image"],
                        "parameters": {},
                    }],
                })
                with pytest.raises(WebSocketDisconnect) as closed:
                    worker_ws.receive_json()
                assert closed.value.code == realtime.CLOSE_PROTOCOL_VIOLATION
    finally:
        logging.getLogger("potocolom.realtime").removeHandler(handler)
    joined = "\n".join(records)
    assert canary not in joined, joined
    assert "fleet hello refused" in joined, joined


@pytest.mark.parametrize("url, line", [
    ("/api/v1/fleet", "fleet handshake refused"),
    ("/api/v1/realtime", "realtime handshake refused"),
])
def test_a_refused_handshake_never_logs_the_origin_header(url, line):
    canary = f"origin-canary-{uuid.uuid4().hex}"
    handler, records = _capture_logger("potocolom.realtime")
    try:
        with TestClient(app, headers=FLEET_HEADERS) as client:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(
                    url, headers={"Origin": f"http://{canary}.invalid"}
                ) as browser_ws:
                    browser_ws.receive_json()
    finally:
        logging.getLogger("potocolom.realtime").removeHandler(handler)
    joined = "\n".join(records)
    assert canary not in joined, joined
    assert line in joined, joined


def test_worker_input_refusals_never_carry_the_token():
    canary = f"input-canary-{uuid.uuid4().hex}"
    expires = int(time.time()) + 60
    with TestClient(app) as client:
        def refuse(token: str, at: int = expires, status: int = 403):
            response = client.get(
                "/api/v1/worker-input", params={"token": token, "expires": at}
            )
            assert response.status_code == status, response.text
            assert canary not in response.text, response.text
            return response

        # Unknown capability.
        assert refuse(canary).json() == {"detail": "input not authorized"}

        # A capability whose key escapes the storage root: this used to echo
        # str(error) as the detail.
        jobs.register_input_capability(canary, "../../escape.png", expires)
        try:
            assert refuse(canary).json() == {"detail": "input not authorized"}
        finally:
            jobs.input_capabilities.pop(canary, None)

        # A capability whose file is not on disk.
        jobs.register_input_capability(canary, f"input/{uuid.uuid4().hex}.png", expires)
        try:
            assert refuse(canary).json() == {"detail": "input not authorized"}
        finally:
            jobs.input_capabilities.pop(canary, None)

        # An expired capability: resolve drops it before comparing.
        jobs.register_input_capability(canary, "input/gone.png", int(time.time()) - 10)
        try:
            assert refuse(canary).json() == {"detail": "input not authorized"}
        finally:
            jobs.input_capabilities.pop(canary, None)

        # Non-ASCII, and a character PostgreSQL cannot store at all; the
        # latter is answered by request validation, which must not echo input.
        refuse(f"{canary}\u00e9")
        refuse(f"{canary}\x00", status=422)
