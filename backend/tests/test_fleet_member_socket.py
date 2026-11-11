"""Fleet members on the fleet socket (issue #885, second PR): a member's credential
admits its own socket, the worker id rules hold, last seen is kept, and a revoke
closes a live socket. The administration and enrolment routes are tested in
test_fleet_members.py.
"""

import time
import uuid

import pytest
from conftest import run_on_test_loop
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app import fleet_members, realtime
from app.main import app
from app.realtime import PROTOCOL_VERSION
from tests.test_fleet_members import (
    BASE_URL,
    MEMBERS,
    ORIGIN,
    _audit_rows,
    _create,
    _enroll,
    _plant,
    none_mode,
)

__all__ = ["none_mode"]
pytestmark = pytest.mark.db

REQUIRES_PROMPT = {
    "type": "object",
    "properties": {"prompt": {"type": "string"}},
    "required": ["prompt"],
}
SHARED_SECRET = "test-fleet-token"
ASGI_CLIENT = ("127.0.0.1", 50000)


def _hello(worker_id: str) -> dict:
    return {
        "type": "hello",
        "protocol_version": PROTOCOL_VERSION,
        "worker_id": worker_id,
        "models": [{"id": "fleet-member-model", "name": "fleet-member-model",
                    "capabilities": ["realtime"], "parameters": REQUIRES_PROMPT}],
        "realtime_slots": 1,
    }


def _wait_for(predicate, what: str) -> None:
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.01)


def _listed(client, member_id: str) -> dict:
    (row,) = [row for row in client.get(MEMBERS).json() if row["id"] == member_id]
    return row


def _handshake(headers: list[tuple[bytes, bytes]]) -> list[dict]:
    """Drive the fleet endpoint directly and return what it sent, in order.

    Exiting a TestClient socket raises WebSocketDisconnect whether the handshake
    was refused or accepted and then closed. The difference between the two
    (HTTP 403 against close 1013) is only visible in the first messages sent.
    """
    sent: list[dict] = []
    received = [{"type": "websocket.connect"}]

    async def receive():
        # An admitted peer that sends nothing else: after the connect, a disconnect.
        return received.pop() if received else {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": "/api/v1/fleet",
        "raw_path": b"/api/v1/fleet",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ASGI_CLIENT,
        "server": ("127.0.0.1", 8000),
        "subprotocols": [],
    }
    run_on_test_loop(app(scope, receive, send))
    return sent


def _credential_headers(credential: str) -> list[tuple[bytes, bytes]]:
    return [(b"x-fleet-token", credential.encode())]


def _enrolled(client) -> tuple[str, dict]:
    member = _create(client, {"Origin": ORIGIN}, label="Socket").json()
    enrolled = _enroll(client, member["join_token"], BASE_URL).json()
    return member["id"], enrolled


def test_a_member_credential_registers_under_its_own_id_and_tracks_its_socket(none_mode):
    with TestClient(app, base_url=ORIGIN) as client:
        member_id, enrolled = _enrolled(client)
        worker_id = enrolled["worker_id"]
        with client.websocket_connect(
                "/api/v1/fleet", headers={"x-fleet-token": enrolled["credential"]}) as ws:
            ws.send_json(_hello(worker_id))
            assert ws.receive_json()["type"] == "registered"
            assert worker_id in realtime.workers
            assert _listed(client, member_id)["state"] == "connected"
            # The registration write runs after the reply, so wait for it.
            _wait_for(lambda: _listed(client, member_id)["last_seen_at"] is not None,
                      "last_seen_at after registration")
        # The state turns offline before cleanup writes last_seen_at, so the
        # two are waited for together.
        def offline_and_seen() -> bool:
            row = _listed(client, member_id)
            return row["state"] == "offline" and row["last_seen_at"] is not None

        _wait_for(offline_and_seen, "the member to read offline with a last seen time")
        assert worker_id not in realtime.workers


def test_a_member_hello_with_another_worker_id_is_closed_4000(none_mode):
    with TestClient(app, base_url=ORIGIN) as client:
        _, enrolled = _enrolled(client)
        other = "w-not-the-member"
        with client.websocket_connect(
                "/api/v1/fleet", headers={"x-fleet-token": enrolled["credential"]}) as ws:
            ws.send_json(_hello(other))
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
        assert closed.value.code == realtime.CLOSE_PROTOCOL_VIOLATION
        assert closed.value.reason == "worker id does not match the credential"
        assert other not in realtime.workers
        assert enrolled["worker_id"] not in realtime.workers


def test_a_shared_secret_hello_with_a_member_id_is_closed_4000_as_reserved(none_mode):
    with TestClient(app, base_url=ORIGIN) as client:
        taken = f"m-{uuid.uuid4().hex}"
        with client.websocket_connect(
                "/api/v1/fleet", headers={"x-fleet-token": SHARED_SECRET}) as ws:
            ws.send_json(_hello(taken))
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
        assert closed.value.code == realtime.CLOSE_PROTOCOL_VIOLATION
        assert closed.value.reason == "worker id is reserved"
        assert taken not in realtime.workers


def test_a_live_credential_is_admitted_and_a_revoked_one_refused_before_accept(none_mode):
    """The live credential is the control: it shows the handshake reaches accept,
    so the revoked one's refusal is the revoke check and not a blanket refusal."""
    live = fleet_members.CREDENTIAL_PREFIX + "planted-live-" + uuid.uuid4().hex
    revoked = fleet_members.CREDENTIAL_PREFIX + "planted-revoked-" + uuid.uuid4().hex
    none_mode(_plant("planted-join-" + uuid.uuid4().hex, live))
    none_mode(_plant("planted-join-" + uuid.uuid4().hex, revoked, revoked=True))
    before = none_mode(_audit_rows())

    admitted = _handshake(_credential_headers(live))
    refused = _handshake(_credential_headers(revoked))

    assert admitted[0]["type"] == "websocket.accept", admitted
    assert [message["type"] for message in refused] == ["websocket.close"], refused
    assert none_mode(_audit_rows()) == before


def test_an_unknown_credential_is_looked_up_refused_before_accept_and_audits_nothing(
        none_mode, monkeypatch):
    looked_up = []
    real_lookup = fleet_members.member_for_credential

    async def spy(credential):
        looked_up.append(credential)
        return await real_lookup(credential)

    monkeypatch.setattr(fleet_members, "member_for_credential", spy)
    before = none_mode(_audit_rows())
    unknown = fleet_members.CREDENTIAL_PREFIX + "never-issued-" + uuid.uuid4().hex

    sent = _handshake(_credential_headers(unknown))

    assert looked_up == [unknown]
    assert [message["type"] for message in sent] == ["websocket.close"], sent
    assert none_mode(_audit_rows()) == before


def test_revoking_a_connected_member_closes_its_socket_and_removes_the_worker(none_mode):
    with TestClient(app, base_url=ORIGIN) as client:
        member_id, enrolled = _enrolled(client)
        worker_id = enrolled["worker_id"]
        with client.websocket_connect(
                "/api/v1/fleet", headers={"x-fleet-token": enrolled["credential"]}) as ws:
            ws.send_json(_hello(worker_id))
            assert ws.receive_json()["type"] == "registered"
            assert client.delete(f"{MEMBERS}/{member_id}",
                                 headers={"Origin": ORIGIN}).status_code == 204
            # The server's side is checked first: a socket the revoke left open
            # would make a bare receive below wait forever. The peer only
            # answers the close, and so only ends the handler, when this block exits.
            _wait_for(lambda: worker_id in realtime.workers
                      and realtime.workers[worker_id].ws.application_state
                      == WebSocketState.DISCONNECTED,
                      "the revoke to close the worker's socket")
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 1000
        _wait_for(lambda: worker_id not in realtime.workers,
                  "the cleanup of the revoked worker to finish")
        assert _listed(client, member_id)["state"] == "revoked"


def test_a_member_revoked_between_the_handshake_and_the_hello_is_not_kept(none_mode):
    with TestClient(app, base_url=ORIGIN) as client:
        member_id, enrolled = _enrolled(client)
        with client.websocket_connect(
                "/api/v1/fleet", headers={"x-fleet-token": enrolled["credential"]}) as ws:
            # The credential was read before accept. No worker is registered yet,
            # so this revoke finds no socket to close.
            assert client.delete(f"{MEMBERS}/{member_id}",
                                 headers={"Origin": ORIGIN}).status_code == 204
            ws.send_json(_hello(enrolled["worker_id"]))
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    ws.receive_json()
        assert closed.value.code == realtime.CLOSE_FORBIDDEN
        assert closed.value.reason == "fleet member removed"
        _wait_for(lambda: enrolled["worker_id"] not in realtime.workers,
                  "the revoked worker to leave the registry")


def test_a_database_failure_during_the_lookup_accepts_then_closes_1013(none_mode, monkeypatch):
    async def down(credential):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(fleet_members, "member_for_credential", down)
    sent = _handshake(_credential_headers(fleet_members.CREDENTIAL_PREFIX + "anything"))

    assert [message["type"] for message in sent] == ["websocket.accept", "websocket.close"]
    assert sent[1]["code"] == realtime.CLOSE_TRY_AGAIN_LATER == 1013


def test_a_database_failure_after_the_hello_closes_1013_and_cleans_up(none_mode, monkeypatch):
    with TestClient(app, base_url=ORIGIN) as client:
        _, enrolled = _enrolled(client)
        lookup = fleet_members.member_for_credential
        calls = []

        async def fails_the_second_time(credential):
            calls.append(credential)
            if len(calls) > 1:
                raise RuntimeError("database unavailable")
            return await lookup(credential)

        monkeypatch.setattr(fleet_members, "member_for_credential", fails_the_second_time)
        with client.websocket_connect(
                "/api/v1/fleet", headers={"x-fleet-token": enrolled["credential"]}) as ws:
            ws.send_json(_hello(enrolled["worker_id"]))
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    ws.receive_json()
        assert closed.value.code == realtime.CLOSE_TRY_AGAIN_LATER
        _wait_for(lambda: enrolled["worker_id"] not in realtime.workers,
                  "the worker to leave the registry")


def test_the_shared_secret_never_queries_the_member_table(none_mode, monkeypatch):
    looked_up = []

    async def spy(credential):
        looked_up.append(credential)
        return None

    monkeypatch.setattr(fleet_members, "member_for_credential", spy)
    with TestClient(app, base_url=ORIGIN) as client:
        with client.websocket_connect(
                "/api/v1/fleet", headers={"x-fleet-token": SHARED_SECRET}) as ws:
            ws.send_json(_hello("w-shared-no-lookup"))
            assert ws.receive_json()["type"] == "registered"
    assert looked_up == []


def test_a_failed_last_seen_write_neither_breaks_registration_nor_cleanup(none_mode,
                                                                         monkeypatch):
    async def broken(member_id):
        raise RuntimeError("write refused")

    monkeypatch.setattr(fleet_members, "_write_last_seen", broken)
    with TestClient(app, base_url=ORIGIN) as client:
        member_id, enrolled = _enrolled(client)
        worker_id = enrolled["worker_id"]
        with client.websocket_connect(
                "/api/v1/fleet", headers={"x-fleet-token": enrolled["credential"]}) as ws:
            ws.send_json(_hello(worker_id))
            assert ws.receive_json()["type"] == "registered"
            assert _listed(client, member_id)["state"] == "connected"
        _wait_for(lambda: worker_id not in realtime.workers,
                  "cleanup to remove the worker after its socket closed")
        assert _listed(client, member_id)["state"] == "offline"
