import asyncio
import json
import logging
import os
import stat
from http import HTTPStatus

import httpx
import pytest
import websockets

from worker.client import (
    BACKOFF_CAP,
    BACKOFF_INITIAL,
    BACKOFF_JITTER,
    FLEET_STATE_LOCK,
    run,
)
from worker.engine import SimulatedEngine
from worker.manifests import SIMULATED_MANIFEST
from worker.process_lock import acquire_exclusive_lock
from worker.settings import Settings

JOIN_TOKEN = "join-token-SECRET"
CREDENTIAL = "ptc_fleet_credential-SECRET"
WORKER_ID = "m-" + "a" * 32
STATE_FILE = "fleet-credential.json"


class StopReconnect(Exception):
    pass


def _files(directory):
    """What the worker saved, without the lock file it holds while it runs."""
    return sorted(name for name in os.listdir(directory) if name != FLEET_STATE_LOCK)


def _issue(_request):
    return httpx.Response(200, json={"worker_id": WORKER_ID, "credential": CREDENTIAL})


def _enroll_server(monkeypatch, answer):
    """Answers every enroll POST with answer(request); returns the requests seen."""
    requests = []

    def handler(request):
        requests.append(request)
        return answer(request)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "worker.client.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    return requests


def _stop_at_the_reconnect_wait(monkeypatch, waits):
    """Records each reconnect wait of run() and raises StopReconnect, so a retry loop
    ends. Only worker.client sees this stand-in: patching asyncio.sleep itself would
    also stop websockets' keepalive task, which sleeps on the same module."""

    class ClientAsyncio:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, delay):
            waits.append(delay)
            raise StopReconnect

    monkeypatch.setattr("worker.client.asyncio", ClientAsyncio())


def _offline(monkeypatch, settings):
    """Stubs what run() touches before a socket opens. The dial is refused and the
    reconnect wait raises StopReconnect, so a retry loop always ends."""
    connections, built = [], []

    class Connection:
        def __init__(self, url, **_kwargs):
            connections.append(url)

        async def __aenter__(self):
            raise OSError("refused by the test")

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr("worker.client.get_settings", lambda: settings)
    monkeypatch.setattr(
        "worker.client.build_runtime",
        lambda _settings: built.append(1) or ([SIMULATED_MANIFEST], SimulatedEngine(0.01)))
    monkeypatch.setattr("worker.client.websockets.connect", Connection)
    _stop_at_the_reconnect_wait(monkeypatch, [])
    return connections, built


def _run_against_fleet(monkeypatch, handler, *, refuse=False, **settings_fields):
    """Runs run() against a local fleet socket. handler serves each accepted socket,
    and refuse=True answers every handshake with HTTP 403. Returns the reconnect
    waits that run() asked for."""
    waits = []

    def process_request(connection, _request):
        return connection.respond(HTTPStatus.FORBIDDEN, "removed\n") if refuse else None

    monkeypatch.setattr(
        "worker.client.build_runtime",
        lambda _settings: ([SIMULATED_MANIFEST], SimulatedEngine(0.01)))
    _stop_at_the_reconnect_wait(monkeypatch, waits)

    async def scenario():
        # Built before the server starts: a refused field must fail here, not inside
        # the server's context, which then waits forever for its connections.
        settings = Settings(**settings_fields)
        async with websockets.serve(handler, "127.0.0.1", 0,
                                    process_request=process_request) as server:
            port = server.sockets[0].getsockname()[1]
            settings = settings.model_copy(
                update={"api_url": f"ws://127.0.0.1:{port}/api/v1/fleet"})
            monkeypatch.setattr("worker.client.get_settings", lambda: settings)
            with pytest.raises(StopReconnect):
                await run()

    asyncio.run(scenario())
    return waits


def _recording_handler(seen):
    """Records the x-fleet-token header and the first message of each socket, then closes it."""
    async def handler(ws):
        seen.append((ws.request.headers.get("x-fleet-token"), json.loads(await ws.recv())))
        await ws.close()

    return handler


def _saved(tmp_path, content=None):
    path = tmp_path / STATE_FILE
    path.write_text(content or json.dumps({"worker_id": WORKER_ID, "credential": CREDENTIAL}))
    return path


def _normal_backoff(wait):
    return BACKOFF_INITIAL <= wait <= BACKOFF_INITIAL * (1 + BACKOFF_JITTER)


@pytest.mark.parametrize("api_url, enroll_url, base_url", [
    ("ws://gpu-host:8080/api/v1/fleet", "http://gpu-host:8080/api/v1/fleet/enroll", "http://gpu-host:8080"),
    ("wss://studio.example.com/api/v1/fleet",
     "https://studio.example.com/api/v1/fleet/enroll", "https://studio.example.com"),
])
def test_first_start_posts_the_join_token_and_saves_the_credential(
        monkeypatch, tmp_path, caplog, api_url, enroll_url, base_url):
    caplog.set_level(logging.DEBUG)
    requests = _enroll_server(monkeypatch, _issue)
    connections, _built = _offline(monkeypatch, Settings(
        api_url=api_url, join_token=JOIN_TOKEN, state_dir=str(tmp_path)))

    with pytest.raises(StopReconnect):
        asyncio.run(run())

    assert [(str(r.url), json.loads(r.content)) for r in requests] == [
        (enroll_url, {"join_token": JOIN_TOKEN, "base_url": base_url}),
    ]
    assert connections == [api_url]
    path = tmp_path / STATE_FILE
    assert json.loads(path.read_text()) == {"worker_id": WORKER_ID, "credential": CREDENTIAL}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert _files(tmp_path) == [STATE_FILE]
    assert JOIN_TOKEN not in caplog.text
    assert CREDENTIAL not in caplog.text


def test_first_start_connects_with_the_issued_identity(monkeypatch, tmp_path):
    _enroll_server(monkeypatch, _issue)
    seen = []

    _run_against_fleet(monkeypatch, _recording_handler(seen), join_token=JOIN_TOKEN,
                       state_dir=str(tmp_path), worker_id="worker-local")

    token, hello = seen[0]
    assert token == CREDENTIAL
    assert hello["type"] == "hello"
    assert hello["worker_id"] == WORKER_ID


def test_a_saved_credential_is_used_and_the_join_token_is_not_spent(monkeypatch, tmp_path):
    path = _saved(tmp_path)
    requests = _enroll_server(monkeypatch, _issue)
    seen = []

    _run_against_fleet(monkeypatch, _recording_handler(seen), join_token="another-join-token",
                       state_dir=str(tmp_path))

    assert requests == []
    token, hello = seen[0]
    assert token == CREDENTIAL
    assert hello["worker_id"] == WORKER_ID
    assert json.loads(path.read_text()) == {"worker_id": WORKER_ID, "credential": CREDENTIAL}


def test_fleet_token_without_a_join_token_behaves_as_before(monkeypatch, tmp_path):
    requests = _enroll_server(monkeypatch, _issue)
    seen = []
    monkeypatch.chdir(tmp_path)

    _run_against_fleet(monkeypatch, _recording_handler(seen), fleet_token="fleet-secret",
                       worker_id="w-plain")

    assert requests == []
    token, hello = seen[0]
    assert token == "fleet-secret"
    assert hello["worker_id"] == "w-plain"
    assert _files(tmp_path) == []


def test_join_token_without_state_dir_refuses_to_start(monkeypatch):
    requests = _enroll_server(monkeypatch, _issue)
    connections, built = _offline(monkeypatch, Settings(join_token=JOIN_TOKEN, worker_id="w-x"))

    with pytest.raises(RuntimeError) as error:
        asyncio.run(run())

    assert "STATE_DIR" in str(error.value)
    assert JOIN_TOKEN not in str(error.value)
    assert requests == []
    assert connections == []
    assert built == []


def test_a_state_dir_that_cannot_be_written_refuses_before_the_token_is_spent(
        monkeypatch, tmp_path):
    requests = _enroll_server(monkeypatch, _issue)
    missing = tmp_path / "not-mounted"
    connections, built = _offline(monkeypatch, Settings(
        join_token=JOIN_TOKEN, state_dir=str(missing)))

    with pytest.raises(RuntimeError) as error:
        asyncio.run(run())

    assert "STATE_DIR" in str(error.value)
    assert str(missing) in str(error.value)
    assert JOIN_TOKEN not in str(error.value)
    assert requests == []
    assert connections == []
    assert built == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes to a read-only folder")
def test_an_existing_state_dir_that_refuses_writes_refuses_before_the_token_is_spent(
        monkeypatch, tmp_path):
    requests = _enroll_server(monkeypatch, _issue)
    read_only = tmp_path / "read-only"
    read_only.mkdir()
    read_only.chmod(0o500)
    connections, built = _offline(monkeypatch, Settings(
        join_token=JOIN_TOKEN, state_dir=str(read_only)))
    try:
        with pytest.raises(RuntimeError) as error:
            asyncio.run(run())
    finally:
        read_only.chmod(0o700)

    assert "STATE_DIR" in str(error.value)
    assert JOIN_TOKEN not in str(error.value)
    assert requests == []
    assert connections == []
    assert built == []


def test_a_second_worker_on_the_same_state_dir_is_refused_before_the_token_is_spent(
        monkeypatch, tmp_path):
    # Two workers on one STATE_DIR would both spend a token and the second save
    # would replace the first identity, or both would connect as one worker.
    requests = _enroll_server(monkeypatch, _issue)
    other = acquire_exclusive_lock(str(tmp_path / FLEET_STATE_LOCK))
    connections, built = _offline(monkeypatch, Settings(
        join_token=JOIN_TOKEN, state_dir=str(tmp_path)))
    try:
        with pytest.raises(RuntimeError) as error:
            asyncio.run(run())
    finally:
        other.close()

    assert "another worker" in str(error.value)
    assert str(tmp_path) in str(error.value)
    assert JOIN_TOKEN not in str(error.value)
    assert requests == []
    assert connections == []
    assert built == []


def test_a_spent_join_token_stops_the_worker_with_a_message_for_the_studio(monkeypatch, tmp_path):
    requests = _enroll_server(monkeypatch, lambda _r: httpx.Response(403, json={"detail": "no"}))
    connections, _built = _offline(monkeypatch, Settings(
        join_token=JOIN_TOKEN, state_dir=str(tmp_path)))

    with pytest.raises(RuntimeError) as error:
        asyncio.run(run())

    message = str(error.value)
    assert "Administration" in message
    assert "studio" in message
    assert JOIN_TOKEN not in message
    assert len(requests) == 1
    assert connections == []
    assert _files(tmp_path) == []


def _unreachable(_request):
    raise httpx.ConnectError("no route to the studio")


@pytest.mark.parametrize("answer, expected", [
    (lambda _r: httpx.Response(500, text="internal"), "500"),
    (lambda _r: httpx.Response(200, json={"worker_id": WORKER_ID}), "credential"),
    (lambda _r: httpx.Response(200, json={"worker_id": WORKER_ID, "credential": ""}), "credential"),
    (lambda _r: httpx.Response(200, text="<html>not json</html>"), "credential"),
    (_unreachable, "ConnectError"),
], ids=["server-error", "no-credential", "empty-credential", "not-json", "network-error"])
def test_other_enroll_failures_stop_the_worker_without_the_token(
        monkeypatch, tmp_path, answer, expected):
    _enroll_server(monkeypatch, answer)
    connections, _built = _offline(monkeypatch, Settings(
        join_token=JOIN_TOKEN, state_dir=str(tmp_path)))

    with pytest.raises(RuntimeError) as error:
        asyncio.run(run())

    message = str(error.value)
    assert expected in message
    assert JOIN_TOKEN not in message
    assert CREDENTIAL not in message
    assert connections == []
    assert _files(tmp_path) == []


def test_enroll_does_not_follow_a_redirect(monkeypatch, tmp_path):
    requests = _enroll_server(monkeypatch, lambda _r: httpx.Response(
        307, headers={"location": "http://elsewhere.example/api/v1/fleet/enroll"}))
    _offline(monkeypatch, Settings(join_token=JOIN_TOKEN, state_dir=str(tmp_path)))

    with pytest.raises(RuntimeError, match="307") as error:
        asyncio.run(run())

    assert len(requests) == 1
    assert JOIN_TOKEN not in str(error.value)
    assert _files(tmp_path) == []


def test_a_crash_before_the_rename_leaves_no_credential_file(monkeypatch, tmp_path):
    _enroll_server(monkeypatch, _issue)
    connections, _built = _offline(monkeypatch, Settings(
        join_token=JOIN_TOKEN, state_dir=str(tmp_path)))

    def crash(_source, _target):
        raise OSError("crash before the rename")

    monkeypatch.setattr("worker.client.os.replace", crash)

    with pytest.raises(OSError, match="crash before the rename"):
        asyncio.run(run())

    assert not (tmp_path / STATE_FILE).exists()
    assert _files(tmp_path) == []
    assert connections == []


@pytest.mark.parametrize("content", [
    "not json",
    "[]",
    json.dumps({"worker_id": WORKER_ID}),
    json.dumps({"worker_id": "", "credential": CREDENTIAL}),
], ids=["not-json", "not-an-object", "no-credential", "empty-worker-id"])
def test_an_unreadable_saved_file_stops_the_worker_and_names_the_path(
        monkeypatch, tmp_path, content):
    path = _saved(tmp_path, content)
    requests = _enroll_server(monkeypatch, _issue)
    connections, _built = _offline(monkeypatch, Settings(
        join_token=JOIN_TOKEN, state_dir=str(tmp_path)))

    with pytest.raises(RuntimeError) as error:
        asyncio.run(run())

    assert str(path) in str(error.value)
    assert requests == []
    assert connections == []
    assert path.read_text() == content


def test_a_removed_machine_keeps_its_file_and_waits_the_cap(monkeypatch, tmp_path, caplog):
    path = _saved(tmp_path)
    content = path.read_text()

    async def never_served(_ws):
        raise AssertionError("a refused handshake reaches no handler")

    with caplog.at_level(logging.ERROR, logger="potocolom.worker"):
        waits = _run_against_fleet(monkeypatch, never_served, refuse=True,
                                   state_dir=str(tmp_path))

    assert waits == [BACKOFF_CAP]
    assert path.read_text() == content
    removed = [r.getMessage() for r in caplog.records if "removed" in r.getMessage()]
    assert removed and str(path) in removed[0]
    assert CREDENTIAL not in caplog.text


def test_a_shared_secret_refused_with_403_keeps_the_normal_backoff(monkeypatch):
    async def never_served(_ws):
        raise AssertionError("a refused handshake reaches no handler")

    waits = _run_against_fleet(monkeypatch, never_served, refuse=True, fleet_token="fleet-secret")

    assert _normal_backoff(waits[0])


@pytest.mark.parametrize("code", [1013, 4403])
def test_a_close_with_an_outage_or_registration_code_keeps_the_normal_backoff(
        monkeypatch, tmp_path, code):
    path = _saved(tmp_path)
    content = path.read_text()

    async def close_now(ws):
        await ws.close(code=code)

    waits = _run_against_fleet(monkeypatch, close_now, state_dir=str(tmp_path))

    assert _normal_backoff(waits[0])
    assert path.read_text() == content
