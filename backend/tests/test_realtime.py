import asyncio
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.datastructures import Address, Headers
from starlette.websockets import WebSocketDisconnect

from app import db, realtime
from app.main import app
from app.manifests import Manifest
from app.realtime import (
    CANVAS_FRAME,
    GENERATED_FRAME,
    MIN_SUPPORTED_VERSION,
    PROTOCOL_VERSION,
    fleet_token_allowed,
    forwarding_trusts_any_peer,
    origin_allowed,
    peer_is_unroutable,
)
from app.settings import get_settings
from app.tables import UsageEvent

# A real peer address rather than the default "testclient", which no ASGI server
# would ever report: permissive fleet mode decides on that address, so the tests
# should present the shape production does.
client = TestClient(app, client=("127.0.0.1", 50000))


def manifest(model_id="sd-sim", parameters=None) -> dict:
    return {"id": model_id, "name": model_id, "capabilities": ["realtime"],
            "parameters": {} if parameters is None else parameters}


# The default manifest above takes any params, which is more permissive than
# every shipped realtime model: sd-turbo, sdxl-turbo and vega-rt all require a
# prompt. A client built against the permissive shape opens sessions in the
# tests and the simulator, then is refused 4000 by the real thing, so the
# refusal is pinned here with a manifest shaped like the shipped ones.
REQUIRES_PROMPT = {
    "type": "object",
    "properties": {"prompt": {"type": "string"}},
    "required": ["prompt"],
}


class FakeHeaders:
    """Stands in for a WebSocket when only the Origin header matters.

    Uses the real Headers type so the fixture matches what the endpoint passes.
    Note this is less strict than a dict, not more: Headers.get is
    case-insensitive, so it would tolerate a lookup a dict would catch.
    """

    def __init__(self, origin):
        raw = [] if origin is None else [(b"origin", origin.encode())]
        self.headers = Headers(raw=raw)


class FakeTokenHeaders:
    """Stands in for a WebSocket when only the fleet token header matters."""

    def __init__(self, token, name=b"x-fleet-token"):
        raw = [] if token is None else [(name, token.encode("utf-8"))]
        self.headers = Headers(raw=raw)


class FakeRawHeaders:
    """Stands in for a WebSocket carrying arbitrary raw header pairs."""

    def __init__(self, raw):
        self.headers = Headers(raw=raw)


class FakePeer:
    """Stands in for a WebSocket when only the peer address matters.

    A host of None stands for the address the ASGI server did not supply, which
    is what a unix socket transport gives.
    """

    def __init__(self, host):
        self.client = None if host is None else Address(host, 1234)
        self.headers = Headers(raw=[])


class FakeSocket:
    """Minimal WebSocket stand-in for driving realtime helpers on one loop."""

    def __init__(self):
        self.sent = []
        self.close_code = None

    async def send_json(self, message):
        self.sent.append(message)

    async def close(self, code=1000):
        self.close_code = code


def hello(version=PROTOCOL_VERSION, worker_id="w-test", models=("sd-sim",), slots=1,
          parameters=None):
    return {
        "type": "hello",
        "protocol_version": version,
        "worker_id": worker_id,
        "models": [manifest(m, parameters) for m in models],
        "realtime_slots": slots,
    }


def test_version_gate_rejects_older_than_n_minus_1():
    with client.websocket_connect("/api/v1/fleet") as ws:
        ws.send_json(hello(version=MIN_SUPPORTED_VERSION - 1))
        response = ws.receive_json()
        assert response["type"] == "rejected"
        assert response["min_supported_version"] == MIN_SUPPORTED_VERSION


def test_fleet_accepts_a_correct_token_at_the_handshake(monkeypatch):
    monkeypatch.setattr(get_settings(), "fleet_token_key", "fleet-secret")
    with client.websocket_connect(
        "/api/v1/fleet", headers={"x-fleet-token": "fleet-secret"}
    ) as ws:
        ws.send_json(hello(worker_id="w-token-ok"))
        assert ws.receive_json()["type"] == "registered"


@pytest.mark.parametrize("token", ["wrong-secret", None, ""])
def test_fleet_rejects_an_invalid_token_before_accept(monkeypatch, token):
    """Assert the worker never registers, not merely that the socket closed.

    Exiting a websocket_connect block raises WebSocketDisconnect on its own,
    so `pytest.raises` around an empty body passes with the check removed
    entirely. Drive the handshake and prove nothing was admitted.
    """
    monkeypatch.setattr(get_settings(), "fleet_token_key", "fleet-secret")
    headers = {} if token is None else {"x-fleet-token": token}
    worker_id = "w-token-bad"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/fleet", headers=headers) as ws:
            ws.send_json(hello(worker_id=worker_id))
            ws.receive_json()
    assert worker_id not in realtime.workers


def test_a_refused_fleet_handshake_is_never_accepted(monkeypatch):
    """The refusal has to happen before accept, so it fails as HTTP 403 and no
    WebSocket ever exists.

    Driving this through the TestClient cannot tell the two apart: exiting a
    websocket_connect block raises WebSocketDisconnect whether the handshake was
    refused or accepted and then closed, so moving `await ws.accept()` above the
    origin and token checks passes every other test here. Call the app directly
    and look at the first message it sends.
    """
    monkeypatch.setattr(get_settings(), "fleet_token_key", "fleet-secret")
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

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
        "headers": [(b"x-fleet-token", b"wrong-secret")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
        "subprotocols": [],
    }
    asyncio.run(app(scope, receive, send))

    assert sent, "the endpoint sent nothing at all"
    assert sent[0]["type"] == "websocket.close", sent
    assert not any(message["type"] == "websocket.accept" for message in sent), sent


def test_fleet_token_check_never_raises_on_a_non_ascii_secret(monkeypatch):
    """compare_digest rejects two str arguments unless both are ASCII.

    Comparing the strings would raise here, and the caller would read the
    exception as a wrong token. The comparison works on bytes, so it cannot
    raise whatever the operator or the peer supplies. The documented contract
    is still an ASCII secret, warned about at startup (app/main.py), because
    not every client will put other bytes in a header.
    """
    secret = "cl\u00e9-secr\u00e8te"
    monkeypatch.setattr(get_settings(), "fleet_token_key", secret)
    assert fleet_token_allowed(FakeTokenHeaders(secret))
    for presented in ("cl\u00e9-autre", "anything", "", None):
        assert fleet_token_allowed(FakeTokenHeaders(presented)) is False


def test_duplicate_token_headers_are_refused():
    """Two of them, and the answer depends on which one came first.

    An intermediary can add or reorder a header, so correct-then-wrong would
    authenticate while wrong-then-correct would not. Require exactly one.
    """
    get_settings().fleet_token_key = "fleet-secret"
    try:
        good = (b"x-fleet-token", b"fleet-secret")
        bad = (b"X-Fleet-Token", b"wrong-secret")
        assert fleet_token_allowed(FakeRawHeaders([good]))
        assert not fleet_token_allowed(FakeRawHeaders([good, bad]))
        assert not fleet_token_allowed(FakeRawHeaders([bad, good]))
        assert not fleet_token_allowed(FakeRawHeaders([good, good]))
    finally:
        get_settings().fleet_token_key = ""


@pytest.mark.parametrize("name", [b"x-fleet-token", b"X-Fleet-Token", b"X-FLEET-TOKEN"])
def test_fleet_token_lookup_is_case_insensitive(monkeypatch, name):
    """The ASGI server passes the header name through as the client spelled it.

    Headers.get matches the stored key verbatim, so a worker sending
    X-Fleet-Token, which is what websockets emits for that spelling, was
    refused as if it had sent nothing: enabling the guard broke every
    legitimate worker. Header names are case-insensitive (RFC 9110), and the
    TestClient lowercases its own headers, so only a raw-level test catches it.
    """
    monkeypatch.setattr(get_settings(), "fleet_token_key", "fleet-secret")
    assert fleet_token_allowed(FakeTokenHeaders("fleet-secret", name=name))
    assert not fleet_token_allowed(FakeTokenHeaders("wrong-secret", name=name))


def test_fleet_stays_open_when_token_key_is_unset(monkeypatch):
    monkeypatch.setattr(get_settings(), "fleet_token_key", "")
    with client.websocket_connect("/api/v1/fleet") as ws:
        ws.send_json(hello(worker_id="w-token-unset"))
        assert ws.receive_json()["type"] == "registered"


@pytest.mark.parametrize("host", ["127.0.0.1", "10.1.2.3", "172.18.0.3", "192.168.1.5",
                                  "169.254.1.1", "::1", "fc00::1"])
def test_a_peer_off_the_public_internet_is_unroutable(host):
    assert peer_is_unroutable(FakePeer(host))


@pytest.mark.parametrize("host,unroutable", [("::ffff:127.0.0.1", True),
                                             ("::ffff:192.168.1.5", True),
                                             ("::ffff:8.8.8.8", False),
                                             ("::ffff:1.1.1.1", False)])
def test_an_ipv4_mapped_peer_is_classified_by_the_address_it_maps(host, unroutable):
    """A dual-stack listener reports an IPv4 client as ::ffff:A.B.C.D, so this
    is the form a public peer most plausibly arrives in. Classifying the mapped
    form as non-global would hand permissive mode straight back to the internet;
    older CPython did exactly that for some mapped ranges.
    """
    assert peer_is_unroutable(FakePeer(host)) is unroutable


@pytest.mark.parametrize("host", ["100.64.0.1", "100.115.92.2", "fd7a:115c:a1e0::1"])
def test_a_worker_reached_over_a_mesh_vpn_is_unroutable(host):
    """Tailscale hands out 100.64.0.0/10, which is carrier-grade NAT space:
    not private, and not global either. Testing `not is_private` instead of
    `is_global` would refuse every worker on a mesh VPN, which is a normal way
    to run one away from the LAN.
    """
    assert peer_is_unroutable(FakePeer(host))


@pytest.mark.parametrize("host", ["8.8.8.8", "1.1.1.1", "2606:4700::1111"])
def test_a_public_peer_is_routable(host):
    """Addresses that genuinely route. The documentation ranges of RFC 5737,
    203.0.113.0/24 and friends, are reserved rather than global, so using one
    here would assert the opposite of what it looks like."""
    assert not peer_is_unroutable(FakePeer(host))


@pytest.mark.parametrize("host", ["", "testclient", "not-an-address", "127.1",
                                  "2130706433", "8.8.8.8:80", " 8.8.8.8 ", "[::1]"])
def test_an_address_that_does_not_parse_counts_as_local(host):
    """A socket always yields a real address, so these come from the test
    harness or from a forged X-Forwarded-For that uvicorn was told to trust.
    Measured: with trusted hosts of 172.18.0.0/16, a peer in that range sending
    "not-an-address" arrives as ("not-an-address", 0). Refusing these would buy
    nothing, because an attacker in that position would send a parseable
    "127.0.0.1" instead, which no notation rule here can distinguish from a real
    one. The configuration is the exposure, and main.py warns about it.
    """
    assert peer_is_unroutable(FakePeer(host))


def test_no_peer_address_at_all_is_refused():
    """uvicorn reports None for a unix-socket peer, which in practice means it
    sits behind a proxy: every public request would then look identical to a
    local one. Such a deployment has to set the secret, so permissive mode
    refuses rather than admitting the whole internet through one socket.
    """
    assert not peer_is_unroutable(FakePeer(None))


@pytest.mark.parametrize("host", ["2001::1", "2001:0:53aa:64c:0:5efe:1.2.3.4",
                                  "2002:c000:204::1"])
def test_a_transition_address_is_refused_though_it_is_not_global(host):
    """Teredo 2001::/32 and 6to4 2002::/16 are classified non-global, but they
    carry traffic to and from the internet through relays, so is_global alone
    would admit a remote client. NAT64's 64:ff9b::/96 needs no special case: it
    is already global.
    """
    assert not peer_is_unroutable(FakePeer(host))
    assert not peer_is_unroutable(FakePeer("64:ff9b::8.8.8.8"))


@pytest.mark.parametrize("spec", ["*", "0.0.0.0/0", "::/0", "127.0.0.1,0.0.0.0/0",
                                  "10.0.0.0/8, ::/0", " 0.0.0.0/0 "])
def test_a_forwarding_setting_that_trusts_everyone_is_recognised(spec):
    """Measured against uvicorn 0.50.1: each of these makes it accept
    X-Forwarded-For from a public peer, which forges the address permissive mode
    checks.
    """
    assert forwarding_trusts_any_peer(spec)


@pytest.mark.parametrize("spec", ["", "127.0.0.1", "0.0.0.0", "::", "10.0.0.0/8",
                                  "127.0.0.1,172.18.0.0/16", "some-host",
                                  " * ", "*,127.0.0.1", "127.0.0.1, *"])
def test_a_narrow_forwarding_setting_is_not_warned_about(spec):
    """The bare literals 0.0.0.0 and :: are single addresses in uvicorn and trust
    nothing, so warning about them would cry wolf; an earlier version of this
    check did exactly that while missing the /0 forms above. uvicorn does not
    treat a wildcard inside a list or with surrounding whitespace as trust-all,
    so warning about those would cry wolf too.
    """
    assert not forwarding_trusts_any_peer(spec)


def test_permissive_mode_is_refused_when_forwarding_trusts_every_peer(monkeypatch):
    """With FORWARDED_ALLOW_IPS=* uvicorn takes the peer address from a header
    the client controls, so permissive mode cannot trust it even for a loopback
    peer: if the unroutable check were consulted, a loopback peer would pass.
    """
    monkeypatch.setattr(get_settings(), "fleet_token_key", "")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    assert fleet_token_allowed(FakePeer("127.0.0.1")) is False


def test_permissive_mode_refuses_a_public_peer(monkeypatch):
    """Compose publishes on 0.0.0.0, so an unset secret on a host with a public
    address used to accept worker registrations from anyone. A registered worker
    is handed other people's prompts and canvas frames."""
    monkeypatch.setattr(get_settings(), "fleet_token_key", "")
    remote = TestClient(app, client=("8.8.8.8", 44321))
    worker_id = "w-public-peer"
    with pytest.raises(WebSocketDisconnect):
        with remote.websocket_connect("/api/v1/fleet") as ws:
            ws.send_json(hello(worker_id=worker_id))
            ws.receive_json()
    assert worker_id not in realtime.workers


def test_permissive_mode_admits_a_worker_on_the_compose_network(monkeypatch):
    monkeypatch.setattr(get_settings(), "fleet_token_key", "")
    bridged = TestClient(app, client=("172.18.0.3", 51000))
    with bridged.websocket_connect("/api/v1/fleet") as ws:
        ws.send_json(hello(worker_id="w-bridge-peer"))
        assert ws.receive_json()["type"] == "registered"


def test_a_correct_token_still_admits_a_public_peer(monkeypatch):
    """The peer check guards permissive mode only. A remote worker holding the
    secret is exactly what an operator configures on purpose, and refusing it
    would make the secret useless for the deployment that needs it most."""
    monkeypatch.setattr(get_settings(), "fleet_token_key", "fleet-secret")
    remote = TestClient(app, client=("1.1.1.1", 44322))
    with remote.websocket_connect(
        "/api/v1/fleet", headers={"x-fleet-token": "fleet-secret"}
    ) as ws:
        ws.send_json(hello(worker_id="w-remote-token"))
        assert ws.receive_json()["type"] == "registered"


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


def test_open_without_the_required_prompt_is_refused():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-prompt", parameters=REQUIRES_PROMPT))
        assert worker_ws.receive_json()["type"] == "registered"

        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
            response = browser_ws.receive_json()
            assert response["type"] == "error"
            assert response["code"] == 4000


def test_open_carrying_the_required_prompt_opens_the_session():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-prompt-ok", parameters=REQUIRES_PROMPT))
        assert worker_ws.receive_json()["type"] == "registered"

        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({
                "type": "open",
                "model_id": "sd-sim",
                "params": {"prompt": "a red house on a hill"},
            })
            opened = worker_ws.receive_json()
            assert opened["type"] == "open_session"
            # The params have to reach the worker, not merely pass validation.
            # Asserting only that the session opens would let the API forward an
            # empty dict: the simulated engine ignores the prompt, so nothing
            # here would notice, while a real model would generate from nothing.
            assert opened["params"] == {"prompt": "a red house on a hill"}
            worker_ws.send_json({"type": "session_ready", "session_id": opened["session_id"]})
            assert browser_ws.receive_json()["type"] == "ready"


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


def test_worker_relay_requires_its_session_and_generated_frames():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-session-owner"))
        assert worker_ws.receive_json()["type"] == "registered"
        with client.websocket_connect("/api/v1/fleet") as other_worker_ws:
            other_worker_ws.send_json(hello(worker_id="w-session-other"))
            assert other_worker_ws.receive_json()["type"] == "registered"
            with client.websocket_connect("/api/v1/realtime") as browser_ws:
                browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
                opened = worker_ws.receive_json()
                session_id = opened["session_id"]
                session = realtime.sessions[uuid.UUID(session_id)]

                other_worker_ws.send_json({"type": "session_ready", "session_id": session_id})
                other_worker_ws.send_bytes(
                    bytes([GENERATED_FRAME]) + uuid.UUID(session_id).bytes + b"foreign"
                )
                # Barrier on the stranger's own socket: a connection processes
                # its messages in order, so once this violation has closed it
                # the two above have been handled. Asserting straight after a
                # send is a race that passes with the guard removed.
                other_worker_ws.send_text("not json at all")
                with pytest.raises(WebSocketDisconnect) as closed:
                    other_worker_ws.receive_json()
                assert closed.value.code == realtime.CLOSE_PROTOCOL_VIOLATION
                assert not session.ready.is_set(), "a stranger readied the session"

                worker_ws.send_json({"type": "session_ready", "session_id": session_id})
                assert browser_ws.receive_json()["type"] == "ready"
                generated = bytes([GENERATED_FRAME]) + uuid.UUID(session_id).bytes + b"owned"
                worker_ws.send_bytes(generated)
                # Arrives after the foreign frame above was dropped, which is
                # what proves the drop: a relayed foreign frame would be read
                # here instead.
                assert browser_ws.receive_bytes() == generated
                assert session.worker is realtime.workers["w-session-owner"]


def test_assigned_worker_sending_a_canvas_frame_is_a_protocol_violation():
    """A non-owner's frame is dropped, because reassignment races produce those.

    The assigned worker sending a canvas frame is not a race: nothing in the
    protocol lets a worker send that direction, so it closes 4000 rather than
    being ignored while the peer stays connected.
    """
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-wrong-kind"))
        assert worker_ws.receive_json()["type"] == "registered"
        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
            session_id = worker_ws.receive_json()["session_id"]
            worker_ws.send_json({"type": "session_ready", "session_id": session_id})
            assert browser_ws.receive_json()["type"] == "ready"
            worker_ws.send_bytes(
                bytes([CANVAS_FRAME]) + uuid.UUID(session_id).bytes + b"wrong-direction"
            )
            # Deadline rather than a blocking receive: with the check removed
            # the frame is merely ignored and receive_json would hang forever,
            # which is a worse failure than a red test.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and "w-wrong-kind" in realtime.workers:
                time.sleep(0.05)
            assert "w-wrong-kind" not in realtime.workers, "the violation was ignored"


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


def test_fleet_closes_4000_on_a_non_string_job_id():
    """Drive the socket, not peer_uuid.

    uuid.UUID raises AttributeError on an int and TypeError on null, neither of
    which is in the fleet handler's except tuple, so before peer_uuid these
    escaped the endpoint. Testing the helper alone leaves jobs.py unguarded by
    the suite, which is the failure mode that let this class survive four
    review rounds (issue #232).
    """
    for bad in (5, None, [1], {"a": 1}):
        with client.websocket_connect("/api/v1/fleet") as ws:
            ws.send_json(hello(worker_id=f"w-badid-{type(bad).__name__}"))
            assert ws.receive_json()["type"] == "registered"
            ws.send_json({"type": "job_done", "job_id": bad, "gpu_ms": 1})
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == realtime.CLOSE_PROTOCOL_VIOLATION, bad


def test_fleet_survives_an_over_deep_manifest_and_an_oversized_number():
    # json_finite costs three frames per level against json.loads' ~993, so an
    # uncapped walk raised where the parser succeeded. A number past CPython's
    # 4300-digit int limit raises a bare ValueError, not JSONDecodeError.
    deep: object = 1
    for _ in range(400):
        deep = {"a": deep}
    with client.websocket_connect("/api/v1/fleet") as ws:
        ws.send_json({"type": "hello", "protocol_version": PROTOCOL_VERSION,
                      "worker_id": "w-deep", "realtime_slots": 0,
                      "models": [{"id": "m", "name": "m", "capabilities": ["text_to_image"],
                                  "parameters": deep}]})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == realtime.CLOSE_PROTOCOL_VIOLATION

    with client.websocket_connect("/api/v1/fleet") as ws:
        ws.send_text('{"type": "hello", "protocol_version": ' + "9" * 5000 + "}")
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == realtime.CLOSE_PROTOCOL_VIOLATION


def test_hello_refusal_reason_fits_a_close_frame():
    """A close frame carries 125 bytes total, 2 of them the code.

    manifest.id reaches the refusal message unfiltered, so truncating the
    reason by code points overflowed the frame for any multi-byte id.
    websockets then raises its own ProtocolError, a different class from ours,
    which escapes the handler and aborts with 1006 and no reason: strictly
    worse than the bare close the reason was added to improve (issue #232).
    """
    for bad_id in ("m" * 200, "\u6f22" * 200, "\U0001f600" * 200, "m\ud800m"):
        with client.websocket_connect("/api/v1/fleet") as ws:
            ws.send_json({"type": "hello", "protocol_version": PROTOCOL_VERSION,
                          "worker_id": "w-reason", "realtime_slots": 0,
                          "models": [{"id": bad_id, "name": "n",
                                      "capabilities": ["upscale", "text_to_image"],
                                      "parameters": {}}]})
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == realtime.CLOSE_PROTOCOL_VIOLATION
            encoded = (closed.value.reason or "").encode("utf-8")
            assert len(encoded) <= 123, f"{bad_id[:8]}: {len(encoded)} bytes"


@pytest.mark.db
def test_session_closed_from_another_worker_is_ignored():
    """The last session-scoped message still routed by UUID alone.

    A worker that held this session earlier still knows its id. Popping the
    entry on its word drops the current owner's accounting and bills this user
    for a session it did not run.

    Asserted through the persisted event rather than the in-memory dict: the
    two sockets are separate tasks, so any assertion timed against a send is a
    race, and a racy test passes with the check removed.
    """
    with TestClient(app) as db_client, \
            db_client.websocket_connect("/api/v1/fleet") as owner_ws:
        owner_ws.send_json(hello(worker_id="w-closing-owner"))
        assert owner_ws.receive_json()["type"] == "registered"
        with db_client.websocket_connect("/api/v1/fleet") as other_ws:
            other_ws.send_json(hello(worker_id="w-closing-other"))
            assert other_ws.receive_json()["type"] == "registered"
            with db_client.websocket_connect("/api/v1/realtime") as browser_ws:
                browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
                session_id = owner_ws.receive_json()["session_id"]
                owner_ws.send_json({"type": "session_ready", "session_id": session_id})
                assert browser_ws.receive_json()["type"] == "ready"
            assert owner_ws.receive_json()["type"] == "close_session"

            async def recorded():
                assert db.session_factory is not None
                async with db.session_factory() as session:
                    rows = (await session.execute(
                        select(UsageEvent).where(
                            UsageEvent.kind == "realtime",
                            UsageEvent.duration_ms.in_((700, 9999)),
                        )
                    )).scalars().all()
                    return [row.frames for row in rows]

            # The stranger acts alone, so nothing races it: give the server a
            # window in which its claim would have been written, then check.
            other_ws.send_json({"type": "session_closed", "session_id": session_id,
                                "frames": 9999, "gpu_ms": 9999, "duration_ms": 9999,
                                "category": "other"})
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                assert 9999 not in asyncio.run(recorded()), "a stranger closed the session"
                time.sleep(0.05)

            owner_ws.send_json({"type": "session_closed", "session_id": session_id,
                                "frames": 7, "gpu_ms": 70, "duration_ms": 700,
                                "category": "other"})
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and 7 not in asyncio.run(recorded()):
                time.sleep(0.05)
            assert 7 in asyncio.run(recorded()), "the owner's close was not recorded"
