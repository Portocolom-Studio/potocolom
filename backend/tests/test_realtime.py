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
from app.manifests import FRAME_P95_MAX_MS, Manifest
from app.realtime import (
    CANVAS_FRAME,
    GENERATED_FRAME,
    MIN_SUPPORTED_VERSION,
    PROTOCOL_VERSION,
    fleet_token_allowed,
    forwarding_trusts_any_peer,
    origin_allowed,
    parse_frame_p95,
    peer_is_unroutable,
)
from app.settings import get_settings
from app.tables import UsageEvent

# A real peer address rather than the default "testclient", which no ASGI server
# would ever report: the handshake still sees a production-shaped address.
FLEET_HEADERS = {"x-fleet-token": "test-fleet-token"}
client = TestClient(app, client=("127.0.0.1", 50000), headers=FLEET_HEADERS)


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


def answer_ready(ws, opened, generation=None):
    """Echo session_ready for the open the worker just received."""
    if isinstance(opened, dict):
        session_id = opened["session_id"]
        if generation is None:
            generation = opened.get("control_generation", 1)
    else:
        session_id = opened
        if generation is None:
            generation = 1
    ws.send_json({
        "type": "session_ready",
        "session_id": session_id,
        "control_generation": generation,
    })


def complete_attempt(session):
    """What a unit test uses in place of the fleet handler's session_ready."""
    session.attempt_ok = True
    session.ready.set()


def test_version_gate_rejects_older_than_n_minus_1():
    with client.websocket_connect("/api/v1/fleet") as ws:
        ws.send_json(hello(version=MIN_SUPPORTED_VERSION - 1))
        response = ws.receive_json()
        assert response["type"] == "rejected"
        assert response["min_supported_version"] == MIN_SUPPORTED_VERSION


def test_hello_with_an_absurd_realtime_p95_ms_still_registers():
    # A measurement is cosmetic; a manifest is not. An out-of-range p95
    # costs the label (None), not the worker's registration: refusing the
    # hello stranded the worker in a reconnect loop that only a process
    # restart could end, over a number the heartbeat carrying the identical
    # value would merely skip.
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        hello_msg = hello(worker_id="w-absurd", models=("vega-rt",), slots=1)
        hello_msg["models"][0]["realtime_p95_ms"] = FRAME_P95_MAX_MS + 1
        worker_ws.send_json(hello_msg)
        assert worker_ws.receive_json()["type"] == "registered"
        worker = realtime.workers["w-absurd"]
        # The model registered and its label came through as absent.
        assert worker.manifests[0].id == "vega-rt"
        assert worker.manifests[0].realtime_p95_ms is None


def test_fleet_accepts_a_correct_token_at_the_handshake(monkeypatch):
    monkeypatch.setattr(get_settings(), "fleet_token_key", "fleet-secret")
    authed = TestClient(app, client=("127.0.0.1", 50000),
                        headers={"x-fleet-token": "fleet-secret"})
    with authed.websocket_connect("/api/v1/fleet") as ws:
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
    bare = TestClient(app, client=("127.0.0.1", 50000), headers=headers)
    with pytest.raises(WebSocketDisconnect):
        with bare.websocket_connect("/api/v1/fleet") as ws:
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
        get_settings().fleet_token_key = "test-fleet-token"


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


def test_unset_fleet_token_key_refuses_even_loopback(monkeypatch):
    monkeypatch.setattr(get_settings(), "fleet_token_key", "")
    assert fleet_token_allowed(FakePeer("127.0.0.1")) is False
    bare = TestClient(app, client=("127.0.0.1", 50000))
    worker_id = "w-token-unset"
    with pytest.raises(WebSocketDisconnect):
        with bare.websocket_connect("/api/v1/fleet") as ws:
            ws.send_json(hello(worker_id=worker_id))
            ws.receive_json()
    assert worker_id not in realtime.workers


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


def test_unset_key_is_refused_when_forwarding_trusts_every_peer(monkeypatch):
    """Unset key refuses regardless of FORWARDED_ALLOW_IPS."""
    monkeypatch.setattr(get_settings(), "fleet_token_key", "")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    assert fleet_token_allowed(FakePeer("127.0.0.1")) is False


def test_unset_key_refuses_a_public_peer(monkeypatch):
    monkeypatch.setattr(get_settings(), "fleet_token_key", "")
    remote = TestClient(app, client=("8.8.8.8", 44321))
    worker_id = "w-public-peer"
    with pytest.raises(WebSocketDisconnect):
        with remote.websocket_connect("/api/v1/fleet") as ws:
            ws.send_json(hello(worker_id=worker_id))
            ws.receive_json()
    assert worker_id not in realtime.workers


def test_unset_key_refuses_a_worker_on_the_compose_network(monkeypatch):
    monkeypatch.setattr(get_settings(), "fleet_token_key", "")
    assert fleet_token_allowed(FakePeer("172.18.0.3")) is False
    bridged = TestClient(app, client=("172.18.0.3", 51000))
    worker_id = "w-bridge-peer"
    with pytest.raises(WebSocketDisconnect):
        with bridged.websocket_connect("/api/v1/fleet") as ws:
            ws.send_json(hello(worker_id=worker_id))
            ws.receive_json()
    assert worker_id not in realtime.workers


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
            assert opened["control_generation"] == 1
            # The params have to reach the worker, not merely pass validation.
            # Asserting only that the session opens would let the API forward an
            # empty dict: the simulated engine ignores the prompt, so nothing
            # here would notice, while a real model would generate from nothing.
            assert opened["params"]["prompt"] == "a red house on a hill"
            # The API filled the seed the client did not send, so the session
            # carries one from the first open and reassignment can re-open
            # with it.
            seed = opened["params"]["seed"]
            assert isinstance(seed, int)
            assert 0 <= seed < realtime.SESSION_SEED_BOUND
            answer_ready(worker_ws, opened)
            assert browser_ws.receive_json()["type"] == "ready"


def test_update_params_reaches_the_worker_and_browser():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-update-ok", parameters=REQUIRES_PROMPT))
        assert worker_ws.receive_json()["type"] == "registered"

        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim",
                                  "params": {"prompt": "a red house"}})
            opened = worker_ws.receive_json()
            assert opened["type"] == "open_session"
            answer_ready(worker_ws, opened)
            assert browser_ws.receive_json()["type"] == "ready"

            browser_ws.send_json({"type": "update_params",
                                  "params": {"prompt": "a blue house"}})
            updated = worker_ws.receive_json()
            # The merged params: the update won because later keys win, and the
            # session seed the API filled at open rides along.
            assert updated["type"] == "update_session"
            assert updated["control_generation"] == 1
            assert updated["session_id"] == opened["session_id"]
            assert updated["params"]["prompt"] == "a blue house"
            seed = updated["params"]["seed"]
            assert isinstance(seed, int)
            assert 0 <= seed < realtime.SESSION_SEED_BOUND
            acknowledged = browser_ws.receive_json()
            assert acknowledged["type"] == "params_updated"
            assert acknowledged["params"] == {"prompt": "a blue house", "seed": seed}

            # A subset update merges instead of replacing the whole dict.
            browser_ws.send_json({"type": "update_params", "params": {}})
            # An empty update is a client bug: reported, not applied.
            refused = browser_ws.receive_json()
            assert refused["type"] == "error"
            assert refused["code"] == 4000
            assert "empty" in refused["message"]

            session = realtime.sessions[uuid.UUID(opened["session_id"])]
            assert session.params == {"prompt": "a blue house", "seed": seed}


@pytest.mark.parametrize("version", [MIN_SUPPORTED_VERSION, PROTOCOL_VERSION + 1])
def test_update_params_follows_the_workers_advertised_version(version):
    """The version the worker advertised in hello is what decides, not the
    API's own. Protocol 3 already speaks unfenced update_session; a worker
    below that would silently drop it. Both N-1 (3) and a worker newer than
    the API still take updates.
    """
    refused = version < realtime.UPDATE_SESSION_PROTOCOL_VERSION
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(version=version, worker_id=f"w-ver-{version}",
                                  parameters=REQUIRES_PROMPT))
        assert worker_ws.receive_json()["type"] == "registered"
        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim",
                                  "params": {"prompt": "a red house"}})
            opened = worker_ws.receive_json()
            assert opened["type"] == "open_session"
            answer_ready(worker_ws, opened)
            assert browser_ws.receive_json()["type"] == "ready"

            browser_ws.send_json({"type": "update_params",
                                  "params": {"prompt": "a blue house"}})
            if refused:
                refused_msg = browser_ws.receive_json()
                assert refused_msg["type"] == "error"
                assert refused_msg["code"] == 4000
                assert "support" in refused_msg["message"]
                session = realtime.sessions[uuid.UUID(opened["session_id"])]
                assert session.params["prompt"] == "a red house"
            else:
                updated = worker_ws.receive_json()
                assert updated["type"] == "update_session"
                assert updated["session_id"] == opened["session_id"]
                if version >= realtime.CONTROL_GENERATION_PROTOCOL_VERSION:
                    assert updated["control_generation"] == 1
                else:
                    assert "control_generation" not in updated
                acknowledged = browser_ws.receive_json()
                assert acknowledged["type"] == "params_updated"
                session = realtime.sessions[uuid.UUID(opened["session_id"])]
                assert session.params["prompt"] == "a blue house"

            # Either way the worker's next message is the canvas frame, not
            # an update_session it would have dropped, and the session is
            # still rendering.
            canvas = bytes([CANVAS_FRAME]) + uuid.UUID(opened["session_id"]).bytes + b"still-live"
            browser_ws.send_bytes(canvas)
            assert worker_ws.receive_bytes() == canvas
            browser_ws.send_json({"type": "close"})
            assert worker_ws.receive_json()["type"] == "close_session"


def test_session_open_without_a_seed_fills_one_in_the_session_params():
    # The API owns the seed: session.params must carry one from the first
    # open, because reassign re-opens a live session with that dict and a
    # value missing from it would be re-rolled by the replacement worker,
    # jumping the image in the middle of a session.
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-seed-fill", parameters=REQUIRES_PROMPT))
        assert worker_ws.receive_json()["type"] == "registered"
        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim",
                                  "params": {"prompt": "a red house"}})
            opened = worker_ws.receive_json()
            assert opened["type"] == "open_session"
            seed = opened["params"]["seed"]
            assert isinstance(seed, int)
            assert 0 <= seed < realtime.SESSION_SEED_BOUND
            answer_ready(worker_ws, opened)
            assert browser_ws.receive_json()["type"] == "ready"
            session = realtime.sessions[uuid.UUID(opened["session_id"])]
            assert session.params["prompt"] == "a red house"
            assert session.params["seed"] == seed


def test_session_open_keeps_an_explicit_seed():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-seed-explicit", parameters=REQUIRES_PROMPT))
        assert worker_ws.receive_json()["type"] == "registered"
        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim",
                                  "params": {"prompt": "a red house", "seed": 42}})
            opened = worker_ws.receive_json()
            assert opened["type"] == "open_session"
            # The client's seed is kept so a session can be reproduced exactly.
            assert opened["params"]["seed"] == 42
            answer_ready(worker_ws, opened)
            assert browser_ws.receive_json()["type"] == "ready"
            session = realtime.sessions[uuid.UUID(opened["session_id"])]
            assert session.params["seed"] == 42


def test_update_params_refuses_a_seed_change_and_keeps_the_session():
    # A session's seed is fixed for its life: a mixed-version fleet cannot
    # agree on a mid-session change (an older worker overwrites it with the
    # seed it drew at open), so the API refuses it like an out-of-range
    # parameter instead of acknowledging a value no frame will use.
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-seed-locked", parameters=REQUIRES_PROMPT))
        assert worker_ws.receive_json()["type"] == "registered"
        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim",
                                  "params": {"prompt": "a red house"}})
            opened = worker_ws.receive_json()
            session_id = opened["session_id"]
            answer_ready(worker_ws, session_id)
            assert browser_ws.receive_json()["type"] == "ready"
            seed = realtime.sessions[uuid.UUID(session_id)].params["seed"]

            browser_ws.send_json({"type": "update_params",
                                  "params": {"prompt": "a blue house", "seed": seed + 1}})
            refused = browser_ws.receive_json()
            assert refused["type"] == "error"
            assert refused["code"] == 4000
            assert "seed" in refused["message"]

            # The refusal left the session running: the seed and the prompt
            # are untouched, nothing reached the worker, and a frame still
            # flows.
            params = realtime.sessions[uuid.UUID(session_id)].params
            assert params["seed"] == seed
            assert params["prompt"] == "a red house"
            canvas = bytes([CANVAS_FRAME]) + uuid.UUID(session_id).bytes + b"still-live"
            browser_ws.send_bytes(canvas)
            assert worker_ws.receive_bytes() == canvas
            browser_ws.send_json({"type": "close"})
            assert worker_ws.receive_json()["type"] == "close_session"


def test_open_normalises_the_seed_across_shapes():
    # The manifest declares no seed property, so every shape below passes
    # the schema's validation; the open normalises them where the API takes
    # ownership: an integer is kept, a whole float is kept as an integer, a
    # bool is replaced (bool subclasses int, so `seed: true` must not
    # survive as a seed), and a non-numeric value is replaced rather than
    # passed to an engine that would build no generator.
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-seed-shapes"))
        assert worker_ws.receive_json()["type"] == "registered"
        for sent, expected in ((42, 42), (42.0, 42), (True, None), (1.5, None),
                               ("seven", None)):
            with client.websocket_connect("/api/v1/realtime") as browser_ws:
                browser_ws.send_json({"type": "open", "model_id": "sd-sim",
                                      "params": {"prompt": "a red house", "seed": sent}})
                opened = worker_ws.receive_json()
                assert opened["type"] == "open_session"
                seed = opened["params"]["seed"]
                if expected is None:
                    assert isinstance(seed, int) and not isinstance(seed, bool)
                    assert 0 <= seed < realtime.SESSION_SEED_BOUND
                else:
                    assert seed == expected
                answer_ready(worker_ws, opened)
                assert browser_ws.receive_json()["type"] == "ready"
            # The browser teardown released the session; drain the
            # close_session so the next open reads its own open_session.
            assert worker_ws.receive_json()["type"] == "close_session"


def test_invalid_update_params_keeps_the_session_open():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-update-bad", parameters=REQUIRES_PROMPT))
        assert worker_ws.receive_json()["type"] == "registered"

        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim",
                                  "params": {"prompt": "a red house"}})
            opened = worker_ws.receive_json()
            answer_ready(worker_ws, opened)
            assert browser_ws.receive_json()["type"] == "ready"

            browser_ws.send_json({"type": "update_params", "params": {"prompt": 123}})
            refused = browser_ws.receive_json()
            assert refused["type"] == "error"
            assert refused["code"] == 4000
            params = realtime.sessions[uuid.UUID(opened["session_id"])].params
            assert params["prompt"] == "a red house"
            assert isinstance(params["seed"], int)  # the rejected update left it alone

            # The socket survived the rejection: a frame still flows, and the
            # session closes normally afterwards.
            canvas = bytes([CANVAS_FRAME]) + uuid.UUID(opened["session_id"]).bytes + b"still-alive"
            browser_ws.send_bytes(canvas)
            assert worker_ws.receive_bytes() == canvas
            browser_ws.send_json({"type": "close"})
            closed = worker_ws.receive_json()
            assert closed["type"] == "close_session"


def test_malformed_update_params_closes_as_a_protocol_violation():
    """A wire-shape violation is not a recoverable client mistake: params that
    are not an object are parsed with the same control parsing as everything
    else, so they close 4000 like any other malformed message."""
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-update-mangled", parameters=REQUIRES_PROMPT))
        assert worker_ws.receive_json()["type"] == "registered"

        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim",
                                  "params": {"prompt": "a red house"}})
            opened = worker_ws.receive_json()
            answer_ready(worker_ws, opened)
            assert browser_ws.receive_json()["type"] == "ready"

            browser_ws.send_json({"type": "update_params", "params": "not an object"})
            with pytest.raises(WebSocketDisconnect) as closed:
                browser_ws.receive_json()
            assert closed.value.code == 4000


def test_session_and_frame_relay_both_directions():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello())
        assert worker_ws.receive_json()["type"] == "registered"

        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim"})

            opened = worker_ws.receive_json()
            assert opened["type"] == "open_session"
            answer_ready(worker_ws, opened)

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

                answer_ready(other_worker_ws, session_id)
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

                answer_ready(worker_ws, session_id)
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
            answer_ready(worker_ws, session_id)
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
    with TestClient(app, headers=FLEET_HEADERS) as db_client:
        with db_client.websocket_connect("/api/v1/fleet") as worker_ws:
            worker_ws.send_json(hello(worker_id="w-usage"))
            assert worker_ws.receive_json()["type"] == "registered"
            with db_client.websocket_connect("/api/v1/realtime") as browser_ws:
                browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
                opened = worker_ws.receive_json()
                answer_ready(worker_ws, opened)
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
    with TestClient(app, headers=FLEET_HEADERS) as db_client:
        with db_client.websocket_connect("/api/v1/fleet") as worker_ws:
            worker_ws.send_json(hello(worker_id="w-closing"))
            assert worker_ws.receive_json()["type"] == "registered"
            with db_client.websocket_connect("/api/v1/realtime") as browser_ws:
                browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
                opened = worker_ws.receive_json()
                answer_ready(worker_ws, opened)
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
            answer_ready(worker_ws, opened)
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
            answer_ready(worker_ws, opened)
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
            answer_ready(worker_ws, opened)
            assert browser_ws.receive_json()["type"] == "ready"

            # A stale self-report must not undo the server-side accounting.
            worker_ws.send_json({"type": "heartbeat", "slots_in_use": 0})

            with client.websocket_connect("/api/v1/realtime") as second_ws:
                second_ws.send_json({"type": "open", "model_id": "sd-sim"})
                refusal = second_ws.receive_json()
                assert refusal["type"] == "error"
                assert refusal["code"] == 4003


def test_heartbeat_frame_p95_replaces_the_hello_measurement():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        hello_msg = hello(worker_id="w-live", models=("vega-rt",), slots=1)
        hello_msg["models"][0]["realtime_p95_ms"] = 408
        worker_ws.send_json(hello_msg)
        assert worker_ws.receive_json()["type"] == "registered"
        before = client.get("/api/v1/models").json()
        worker_ws.send_json({"type": "heartbeat", "frame_p95_ms": {"vega-rt": 333}})
        worker = realtime.workers["w-live"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and worker.frame_p95_ms != {"vega-rt": 333}:
            time.sleep(0.05)
        assert worker.frame_p95_ms == {"vega-rt": 333}
        after = client.get("/api/v1/models").json()
    entry = next(m for m in before if m["id"] == "vega-rt")
    assert entry["realtime_p95_ms"] == 408
    entry = next(m for m in after if m["id"] == "vega-rt")
    assert entry["realtime_p95_ms"] == 333


@pytest.mark.parametrize("bad", [
    "not-a-dict",
    None,
    5,
])
def test_malformed_heartbeat_frame_p95_is_ignored_and_connection_survives(bad):
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-badp95", models=("vega-rt",), slots=1))
        assert worker_ws.receive_json()["type"] == "registered"
        worker_ws.send_json({"type": "heartbeat", "frame_p95_ms": {"vega-rt": 222}})
        worker = realtime.workers["w-badp95"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and worker.frame_p95_ms != {"vega-rt": 222}:
            time.sleep(0.05)
        assert worker.frame_p95_ms == {"vega-rt": 222}

        # A non-dict payload is dropped whole, so the previous value stands
        # until the valid heartbeat after it lands, and the malformed
        # heartbeat must not close the fleet connection (messages process in
        # order).
        worker_ws.send_json({"type": "heartbeat", "frame_p95_ms": bad})
        worker_ws.send_json({"type": "heartbeat", "frame_p95_ms": {"vega-rt": 333}})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and worker.frame_p95_ms != {"vega-rt": 333}:
            time.sleep(0.05)
        assert worker.frame_p95_ms == {"vega-rt": 333}
        assert "w-badp95" in realtime.workers


def test_heartbeat_frame_p95_skips_a_bad_entry_and_keeps_the_good_one():
    # Entries within a dict are independent: one junk entry must not discard
    # the valid measurement beside it, or a worker that appends a bad entry
    # to every heartbeat pins its last accepted number indefinitely.
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-salvage",
                                  models=("vega-rt", "sdxl-turbo"), slots=1))
        assert worker_ws.receive_json()["type"] == "registered"
        worker_ws.send_json({"type": "heartbeat", "frame_p95_ms": {
            "vega-rt": "slow", "sdxl-turbo": 250,
        }})
        worker = realtime.workers["w-salvage"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and worker.frame_p95_ms != {"sdxl-turbo": 250}:
            time.sleep(0.05)
        assert worker.frame_p95_ms == {"sdxl-turbo": 250}


def test_heartbeat_junk_for_one_model_keeps_its_live_value():
    # Merging (not replacing) is what makes per-entry salvage protect the
    # value already held: a good value, then a heartbeat with junk for that
    # same model and a good value for another, must leave the first model's
    # live value in place rather than snap it back to hello's number.
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-merge",
                                  models=("vega-rt", "sdxl-turbo"), slots=1))
        assert worker_ws.receive_json()["type"] == "registered"
        worker_ws.send_json({"type": "heartbeat", "frame_p95_ms": {"vega-rt": 333}})
        worker = realtime.workers["w-merge"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and worker.frame_p95_ms != {"vega-rt": 333}:
            time.sleep(0.05)
        assert worker.frame_p95_ms == {"vega-rt": 333}

        worker_ws.send_json({"type": "heartbeat", "frame_p95_ms": {
            "vega-rt": "junk", "sdxl-turbo": 250,
        }})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and worker.frame_p95_ms != {
            "vega-rt": 333, "sdxl-turbo": 250,
        }:
            time.sleep(0.05)
        assert worker.frame_p95_ms == {"vega-rt": 333, "sdxl-turbo": 250}


def test_heartbeat_frame_p95_ignores_models_the_worker_never_advertised():
    # The merge is bounded by the worker's own manifest set: a measurement
    # for a model this worker does not serve is meaningless, and admitting
    # one would let the map grow with every heartbeat for the worker's
    # lifetime. The ids come from the registered manifests, never from the
    # heartbeat.
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-foreign", models=("vega-rt",), slots=1))
        assert worker_ws.receive_json()["type"] == "registered"
        worker_ws.send_json({"type": "heartbeat", "frame_p95_ms": {
            "vega-rt": 333, "sd-turbo": 250,
        }})
        worker = realtime.workers["w-foreign"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and worker.frame_p95_ms != {"vega-rt": 333}:
            time.sleep(0.05)
        # The advertised model was stored; the foreign one was ignored.
        assert worker.frame_p95_ms == {"vega-rt": 333}

        for index in range(5):
            worker_ws.send_json({"type": "heartbeat", "frame_p95_ms": {
                f"foreign-{index}": 200 + index,
            }})
        # Trailing barrier on the same socket: a connection processes its
        # messages in order, so once this lands every foreign heartbeat above
        # has been handled, and the map must still hold nothing beyond the
        # advertised model. Fresh unknown ids cannot grow it.
        worker_ws.send_json({"type": "heartbeat", "frame_p95_ms": {"vega-rt": 350}})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and worker.frame_p95_ms != {"vega-rt": 350}:
            time.sleep(0.05)
        assert worker.frame_p95_ms == {"vega-rt": 350}
        assert "w-foreign" in realtime.workers


def test_parse_frame_p95_accepts_whole_milliseconds():
    assert parse_frame_p95({"vega-rt": 333}) == {"vega-rt": 333}
    assert parse_frame_p95({"vega-rt": 333.0}) == {"vega-rt": 333}
    assert parse_frame_p95({"vega-rt": FRAME_P95_MAX_MS}) == {"vega-rt": FRAME_P95_MAX_MS}
    assert parse_frame_p95({}) == {}
    assert parse_frame_p95({"vega-rt": 333, "sdxl-turbo": 250}) == {
        "vega-rt": 333, "sdxl-turbo": 250,
    }


def test_parse_frame_p95_skips_bad_entries_and_keeps_good_ones():
    assert parse_frame_p95({"vega-rt": 333, "sdxl-turbo": "slow"}) == {"vega-rt": 333}
    assert parse_frame_p95({"vega-rt": 0, "sdxl-turbo": 250}) == {"sdxl-turbo": 250}
    assert parse_frame_p95({"vega-rt": 1.5, "sdxl-turbo": 250.0}) == {"sdxl-turbo": 250}
    assert parse_frame_p95({"vega-rt": True, "sdxl-turbo": 250}) == {"sdxl-turbo": 250}
    assert parse_frame_p95({"vega-rt": 333, 7: 100}) == {"vega-rt": 333}
    assert parse_frame_p95({"vega-rt": None, "sdxl-turbo": 250}) == {"sdxl-turbo": 250}
    assert parse_frame_p95({"vega-rt": -1, "sdxl-turbo": 250}) == {"sdxl-turbo": 250}
    # The ceiling is the branch that keeps an absurd number out of a browser,
    # so it is asserted at the boundary and far past it.
    assert parse_frame_p95({"vega-rt": FRAME_P95_MAX_MS}) == {"vega-rt": FRAME_P95_MAX_MS}
    assert parse_frame_p95({"vega-rt": FRAME_P95_MAX_MS + 1, "sdxl-turbo": 250}) == {
        "sdxl-turbo": 250,
    }
    assert parse_frame_p95({"vega-rt": 10**30}) == {}
    assert parse_frame_p95({"vega-rt": "slow"}) == {}
    assert parse_frame_p95({7: 100}) == {}


@pytest.mark.parametrize("bad", [
    "not-a-dict",
    None,
    [],
    5,
])
def test_parse_frame_p95_drops_whole_non_dict_payloads(bad):
    assert parse_frame_p95(bad) is None, bad


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


def test_assign_timeout_sends_close_session(monkeypatch):
    """A give-up tells the worker the session is dead, so it discards the
    runner instead of waiting for frames nobody will ever send.

    Driven directly rather than through a TestClient websocket: asserting on
    the wire there means blocking on receive_json, so a regression would hang
    the suite instead of failing it, and a hang costs a whole CI job.
    """
    monkeypatch.setattr(realtime, "SESSION_READY_TIMEOUT", 0.1)

    async def scenario():
        ws = FakeSocket()
        worker = realtime.Worker(id="w-give-up", ws=ws,
                                 manifests=[Manifest.model_validate(manifest())],
                                 realtime_slots=1)
        realtime.workers[worker.id] = worker
        session = realtime.Session(id=uuid.uuid4(), model_id="sd-sim", browser=ws)
        realtime.sessions[session.id] = session
        try:
            # The worker never answers, so the open times out.
            assert not await realtime.assign(session, worker)
            kinds = [message["type"] for message in ws.sent]
            assert kinds == ["open_session", "close_session"], ws.sent
            closed = ws.sent[-1]
            assert closed["session_id"] == str(session.id)
            assert worker.slots_in_use == 0
        finally:
            realtime.workers.pop(worker.id, None)
            realtime.sessions.pop(session.id, None)

    asyncio.run(scenario())


def test_assign_does_not_decrement_again_when_release_already_did(monkeypatch):
    """The browser leaves mid-assignment: the realtime handler's teardown
    calls release(), which decrements the slot and clears the session's
    worker, so the assignment's own failure compensation must be
    ownership-checked rather than assumed, or the worker underflows to minus
    one and is advertised as having a free slot it does not have."""
    monkeypatch.setattr(realtime, "SESSION_READY_TIMEOUT", 0.1)

    async def scenario():
        ws = FakeSocket()
        worker = realtime.Worker(id="w-left-mid", ws=ws,
                                 manifests=[Manifest.model_validate(manifest())],
                                 realtime_slots=1)
        realtime.workers[worker.id] = worker
        session = realtime.Session(id=uuid.uuid4(), model_id="sd-sim", browser=ws)
        realtime.sessions[session.id] = session
        try:
            task = asyncio.create_task(realtime.assign(session, worker))
            await asyncio.sleep(0.01)  # the open is in flight
            assert worker.slots_in_use == 1
            # What the realtime handler's teardown does when the browser
            # leaves while the assignment is in flight.
            await realtime.release(session)
            assert worker.slots_in_use == 0
            # The worker never answers: the open times out.
            assert not await task
            # The failure must not be compensated twice: release already
            # accounted for this slot, and reassign moves the session on.
            assert worker.slots_in_use == 0
            assert worker.free_slots == worker.realtime_slots
            assert session.worker is None
        finally:
            realtime.workers.pop(worker.id, None)
            realtime.sessions.pop(session.id, None)

    asyncio.run(scenario())


def test_reassign_retries_another_protocol_4_worker(monkeypatch):
    """A failed attempt tries the next protocol 4 candidate; generation fences
    the two opens so a late ready from the first cannot complete the second.
    """
    monkeypatch.setattr(realtime, "SESSION_READY_TIMEOUT", 0.05)

    async def scenario():
        browser = FakeSocket()
        first_ws = FakeSocket()
        first = realtime.Worker(id="w-first", ws=first_ws,
                                manifests=[Manifest.model_validate(manifest())],
                                realtime_slots=1)
        spare_ws = FakeSocket()
        spare = realtime.Worker(id="w-spare", ws=spare_ws,
                                manifests=[Manifest.model_validate(manifest())],
                                realtime_slots=1)
        realtime.workers.update({first.id: first, spare.id: spare})
        session = realtime.Session(id=uuid.uuid4(), model_id="sd-sim", browser=browser)
        realtime.sessions[session.id] = session
        try:
            await realtime.reassign(session)
            assert [m["type"] for m in first_ws.sent] == [
                "open_session", "close_session",
            ], first_ws.sent
            assert [m["type"] for m in spare_ws.sent] == [
                "open_session", "close_session",
            ], spare_ws.sent
            assert first_ws.sent[0]["control_generation"] == 2
            assert spare_ws.sent[0]["control_generation"] == 3
            assert first.slots_in_use == 0
            assert spare.slots_in_use == 0
            assert session.worker is None
            assert [m["type"] for m in browser.sent] == ["interrupted", "error"]
            assert browser.sent[-1]["code"] == realtime.CLOSE_NO_CAPACITY
            assert browser.close_code == realtime.CLOSE_NO_CAPACITY
        finally:
            realtime.workers.pop(first.id, None)
            realtime.workers.pop(spare.id, None)
            realtime.sessions.pop(session.id, None)

    asyncio.run(scenario())


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
            complete_attempt(session)
            await task
            assert [m["type"] for m in browser.sent] == ["interrupted", "resumed"]
            assert session.worker is replacement
            assert replacement.slots_in_use == 1
            assert browser.close_code is None  # the session survived
        finally:
            realtime.workers.pop(replacement.id, None)
            realtime.sessions.pop(session.id, None)

    asyncio.run(scenario())


def test_reassign_sends_the_same_seed_to_the_replacement_worker():
    # The defect: the seed lived only in worker-local state, so re-opening a
    # live session on a replacement worker generated a new one and the image
    # jumped mid-session. The API session owns the seed now, so the
    # open_session the replacement receives carries the session's own value.
    async def scenario():
        browser = FakeSocket()
        replacement_ws = FakeSocket()
        replacement = realtime.Worker(id="w-seed-replacement", ws=replacement_ws,
                                      manifests=[Manifest.model_validate(manifest())],
                                      realtime_slots=1)
        realtime.workers[replacement.id] = replacement
        session = realtime.Session(id=uuid.uuid4(), model_id="sd-sim", browser=browser,
                                   params={"prompt": "a red house", "seed": 77})
        realtime.sessions[session.id] = session
        try:
            task = asyncio.create_task(realtime.reassign(session))
            await asyncio.sleep(0.01)  # interrupted sent, open_session in flight
            opened = replacement_ws.sent[0]
            assert opened["type"] == "open_session"
            assert opened["params"]["seed"] == 77
            assert opened["params"]["prompt"] == "a red house"
            complete_attempt(session)
            await task
            assert session.worker is replacement
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
    with TestClient(app, headers=FLEET_HEADERS) as db_client, \
            db_client.websocket_connect("/api/v1/fleet") as owner_ws:
        owner_ws.send_json(hello(worker_id="w-closing-owner"))
        assert owner_ws.receive_json()["type"] == "registered"
        with db_client.websocket_connect("/api/v1/fleet") as other_ws:
            other_ws.send_json(hello(worker_id="w-closing-other"))
            assert other_ws.receive_json()["type"] == "registered"
            with db_client.websocket_connect("/api/v1/realtime") as browser_ws:
                browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
                session_id = owner_ws.receive_json()["session_id"]
                answer_ready(owner_ws, session_id)
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


def test_cost_admission_keeps_mixed_load_inside_the_bar():
    cheap = Manifest.model_validate(manifest("cheap"))
    expensive = Manifest.model_validate(manifest("expensive"))
    worker = realtime.Worker(
        id="w-cost", ws=FakeSocket(), manifests=[cheap, expensive],
        realtime_slots=1, admission_p95_ms={"cheap": 200, "expensive": 400},
    )
    saved_workers = dict(realtime.workers)
    saved_sessions = dict(realtime.sessions)
    try:
        realtime.workers.clear()
        realtime.sessions.clear()
        realtime.workers[worker.id] = worker
        assert realtime.pick_worker("cheap") is worker
        assert realtime.pick_worker("expensive") is worker
        live = realtime.Session(
            id=uuid.uuid4(), model_id="cheap", browser=FakeSocket(), worker=worker,
        )
        realtime.sessions[live.id] = live
        assert realtime.pick_worker("cheap") is worker
        assert realtime.pick_worker("expensive") is None
    finally:
        realtime.workers.clear()
        realtime.sessions.clear()
        realtime.workers.update(saved_workers)
        realtime.sessions.update(saved_sessions)


def test_map_bearing_worker_refuses_an_unmeasured_model():
    worker = realtime.Worker(
        id="w-unmeasured", ws=FakeSocket(),
        manifests=[Manifest.model_validate(manifest("known")),
                   Manifest.model_validate(manifest("ghost"))],
        realtime_slots=4, admission_p95_ms={"known": 200},
    )
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers[worker.id] = worker
        assert realtime.pick_worker("known") is worker
        assert realtime.pick_worker("ghost") is None
    finally:
        realtime.workers.clear()
        realtime.workers.update(saved)


def test_hello_without_the_p95_map_keeps_the_integer_pool():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-n1-pool", slots=1))
        assert worker_ws.receive_json()["type"] == "registered"
        worker = realtime.workers["w-n1-pool"]
        assert worker.admission_p95_ms is None
        assert realtime.pick_worker("sd-sim") is worker
        worker.slots_in_use = 1
        assert realtime.pick_worker("sd-sim") is None


def test_hello_map_admits_from_cost_not_the_scalar():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        msg = hello(worker_id="w-map-hello", models=("cheap", "slow"), slots=1)
        msg["realtime_p95_ms"] = {"cheap": 200, "slow": 400}
        worker_ws.send_json(msg)
        assert worker_ws.receive_json()["type"] == "registered"
        worker = realtime.workers["w-map-hello"]
        assert worker.admission_p95_ms == {"cheap": 200, "slow": 400}
        assert realtime.pick_worker("cheap") is worker
        live = realtime.Session(
            id=uuid.uuid4(), model_id="cheap", browser=FakeSocket(), worker=worker,
        )
        realtime.sessions[live.id] = live
        try:
            assert realtime.pick_worker("cheap") is worker
            assert realtime.pick_worker("slow") is None
        finally:
            realtime.sessions.pop(live.id, None)


def test_heartbeat_p95_increase_reduces_new_admissions():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        msg = hello(worker_id="w-ratchet-up", slots=2)
        msg["realtime_p95_ms"] = {"sd-sim": 200}
        worker_ws.send_json(msg)
        assert worker_ws.receive_json()["type"] == "registered"
        live = realtime.Session(
            id=uuid.uuid4(), model_id="sd-sim", browser=FakeSocket(),
            worker=realtime.workers["w-ratchet-up"],
        )
        realtime.sessions[live.id] = live
        try:
            assert realtime.pick_worker("sd-sim") is realtime.workers["w-ratchet-up"]
            worker_ws.send_json({
                "type": "heartbeat", "slots_in_use": 1, "loaded_models": [],
                "frame_p95_ms": {"sd-sim": 400}, "gpu": {},
            })
            client.get("/api/v1/health")
            assert realtime.workers["w-ratchet-up"].admission_p95_ms["sd-sim"] == 400
            assert realtime.pick_worker("sd-sim") is None
        finally:
            realtime.sessions.pop(live.id, None)


def test_heartbeat_p95_decrease_does_not_raise_admissions():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        msg = hello(worker_id="w-ratchet-down", slots=1)
        msg["realtime_p95_ms"] = {"sd-sim": 400}
        worker_ws.send_json(msg)
        assert worker_ws.receive_json()["type"] == "registered"
        live = realtime.Session(
            id=uuid.uuid4(), model_id="sd-sim", browser=FakeSocket(),
            worker=realtime.workers["w-ratchet-down"],
        )
        realtime.sessions[live.id] = live
        try:
            assert realtime.pick_worker("sd-sim") is None
            worker_ws.send_json({
                "type": "heartbeat", "slots_in_use": 1, "loaded_models": [],
                "frame_p95_ms": {"sd-sim": 200}, "gpu": {},
            })
            client.get("/api/v1/health")
            assert realtime.workers["w-ratchet-down"].admission_p95_ms["sd-sim"] == 400
            assert realtime.workers["w-ratchet-down"].frame_p95_ms["sd-sim"] == 200
            assert realtime.pick_worker("sd-sim") is None
        finally:
            realtime.sessions.pop(live.id, None)


def test_protocol_3_open_has_no_generation_and_first_attempt_still_works():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(version=MIN_SUPPORTED_VERSION,
                                  worker_id="w-p3-first"))
        assert worker_ws.receive_json()["type"] == "registered"
        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
            opened = worker_ws.receive_json()
            assert opened["type"] == "open_session"
            assert "control_generation" not in opened
            worker_ws.send_json({"type": "session_ready",
                                 "session_id": opened["session_id"]})
            assert browser_ws.receive_json()["type"] == "ready"


def test_protocol_3_worker_is_skipped_on_reassignment():
    saved_workers = dict(realtime.workers)
    saved_sessions = dict(realtime.sessions)
    try:
        realtime.workers.clear()
        realtime.sessions.clear()
        old = realtime.Worker(
            id="w-p3", ws=FakeSocket(),
            manifests=[Manifest.model_validate(manifest())],
            realtime_slots=4, protocol_version=MIN_SUPPORTED_VERSION,
        )
        new = realtime.Worker(
            id="w-p4", ws=FakeSocket(),
            manifests=[Manifest.model_validate(manifest())],
            realtime_slots=1, protocol_version=PROTOCOL_VERSION,
        )
        realtime.workers[old.id] = old
        realtime.workers[new.id] = new
        assert realtime.pick_worker("sd-sim", generation=1) is old
        assert realtime.pick_worker("sd-sim", generation=2) is new
    finally:
        realtime.workers.clear()
        realtime.sessions.clear()
        realtime.workers.update(saved_workers)
        realtime.sessions.update(saved_sessions)


def test_session_refused_reassigns_to_another_protocol_4_worker():
    """TestClient cannot drive two fleet sockets and the browser assign wait
    on one loop, so this calls place_session directly like the reassign
    tests.
    """
    async def scenario():
        browser = FakeSocket()
        first_ws = FakeSocket()
        first = realtime.Worker(
            id="w-refuse-first", ws=first_ws,
            manifests=[Manifest.model_validate(manifest())],
            realtime_slots=2,
        )
        spare_ws = FakeSocket()
        spare = realtime.Worker(
            id="w-refuse-spare", ws=spare_ws,
            manifests=[Manifest.model_validate(manifest())],
            realtime_slots=1,
        )
        realtime.workers.update({first.id: first, spare.id: spare})
        session = realtime.Session(
            id=uuid.uuid4(), model_id="sd-sim", browser=browser,
        )
        realtime.sessions[session.id] = session
        try:
            task = asyncio.create_task(realtime.place_session(session))
            await asyncio.sleep(0.01)
            assert first_ws.sent[0]["type"] == "open_session"
            assert first_ws.sent[0]["control_generation"] == 1
            session.attempt_ok = False
            session.ready.set()
            await asyncio.sleep(0.01)
            assert spare_ws.sent[0]["type"] == "open_session"
            assert spare_ws.sent[0]["session_id"] == str(session.id)
            assert spare_ws.sent[0]["control_generation"] == 2
            complete_attempt(session)
            assert await task
            assert session.worker is spare
            assert session.control_generation == 2
            assert session.state == "live"
        finally:
            realtime.workers.pop(first.id, None)
            realtime.workers.pop(spare.id, None)
            realtime.sessions.pop(session.id, None)

    asyncio.run(scenario())


def test_session_refused_with_no_other_worker_closes_4003():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-only-refuse"))
        assert worker_ws.receive_json()["type"] == "registered"
        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
            opened = worker_ws.receive_json()
            assert opened["type"] == "open_session"
            worker_ws.send_json({
                "type": "session_refused",
                "session_id": opened["session_id"],
                "control_generation": opened["control_generation"],
                "reason": "not_resident",
            })
            refusal = browser_ws.receive_json()
            assert refusal["type"] == "error"
            assert refusal["code"] == 4003


def test_unfenced_protocol_4_ready_is_ignored(monkeypatch):
    monkeypatch.setattr(realtime, "SESSION_READY_TIMEOUT", 0.1)
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-unfenced"))
        assert worker_ws.receive_json()["type"] == "registered"
        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
            opened = worker_ws.receive_json()
            worker_ws.send_json({
                "type": "session_ready",
                "session_id": opened["session_id"],
            })
            refusal = browser_ws.receive_json()
            assert refusal["type"] == "error"
            assert refusal["code"] == 4003


def test_stale_session_ready_generation_is_ignored(monkeypatch):
    monkeypatch.setattr(realtime, "SESSION_READY_TIMEOUT", 0.1)
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-stale-ready"))
        assert worker_ws.receive_json()["type"] == "registered"
        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
            opened = worker_ws.receive_json()
            worker_ws.send_json({
                "type": "session_ready",
                "session_id": opened["session_id"],
                "control_generation": opened["control_generation"] + 1,
            })
            refusal = browser_ws.receive_json()
            assert refusal["type"] == "error"
            assert refusal["code"] == 4003


def test_stale_session_refused_generation_is_ignored():
    with client.websocket_connect("/api/v1/fleet") as worker_ws:
        worker_ws.send_json(hello(worker_id="w-stale-refuse"))
        assert worker_ws.receive_json()["type"] == "registered"
        with client.websocket_connect("/api/v1/realtime") as browser_ws:
            browser_ws.send_json({"type": "open", "model_id": "sd-sim"})
            opened = worker_ws.receive_json()
            answer_ready(worker_ws, opened)
            assert browser_ws.receive_json()["type"] == "ready"
            worker_ws.send_json({
                "type": "session_refused",
                "session_id": opened["session_id"],
                "control_generation": opened["control_generation"] + 1,
                "reason": "not_resident",
            })
            client.get("/api/v1/health")
            browser_ws.send_json({"type": "update_params", "params": {"prompt": "x"}})
            assert browser_ws.receive_json()["type"] == "params_updated"


def test_stale_close_generation_is_not_sent_after_reassign():
    async def scenario():
        browser = FakeSocket()
        old_ws = FakeSocket()
        old = realtime.Worker(
            id="w-old-close", ws=old_ws,
            manifests=[Manifest.model_validate(manifest())],
            realtime_slots=1, slots_in_use=1,
        )
        replacement_ws = FakeSocket()
        replacement = realtime.Worker(
            id="w-stale-close", ws=replacement_ws,
            manifests=[Manifest.model_validate(manifest())],
            realtime_slots=1,
        )
        session = realtime.Session(
            id=uuid.uuid4(), model_id="sd-sim", browser=browser,
            worker=old, state="live", assigned_at=time.monotonic(),
        )
        saved_workers = dict(realtime.workers)
        saved_sessions = dict(realtime.sessions)
        try:
            realtime.workers.clear()
            realtime.sessions.clear()
            realtime.workers.update({old.id: old, replacement.id: replacement})
            realtime.sessions[session.id] = session
            task = asyncio.create_task(realtime.reassign(session))
            await asyncio.sleep(0.01)
            close = old_ws.sent[0]
            assert close["type"] == "close_session"
            assert close["control_generation"] == 1
            opened = replacement_ws.sent[0]
            assert opened["control_generation"] == 2
            complete_attempt(session)
            await task
            assert session.state == "live"
            assert session.control_generation == 2
            assert not any(m.get("type") == "close_session" for m in replacement_ws.sent)
        finally:
            realtime.workers.clear()
            realtime.sessions.clear()
            realtime.workers.update(saved_workers)
            realtime.sessions.update(saved_sessions)

    asyncio.run(scenario())


def test_over_capacity_drops_newest_protocol_4_session():
    cheap = Manifest.model_validate(manifest("sd-sim"))
    worker = realtime.Worker(
        id="w-shed", ws=FakeSocket(), manifests=[cheap],
        realtime_slots=4, admission_p95_ms={"sd-sim": 300},
    )
    older = realtime.Session(
        id=uuid.uuid4(), model_id="sd-sim", browser=FakeSocket(),
        worker=worker, state="live", assigned_at=1.0,
    )
    newer = realtime.Session(
        id=uuid.uuid4(), model_id="sd-sim", browser=FakeSocket(),
        worker=worker, state="live", assigned_at=2.0,
    )
    saved_workers = dict(realtime.workers)
    saved_sessions = dict(realtime.sessions)
    try:
        realtime.workers.clear()
        realtime.sessions.clear()
        realtime.workers[worker.id] = worker
        realtime.sessions[older.id] = older
        realtime.sessions[newer.id] = newer
        assert realtime.live_admission_cost(worker) == 600
        victims = realtime.over_capacity_sessions(worker)
        assert victims == [newer]
    finally:
        realtime.workers.clear()
        realtime.sessions.clear()
        realtime.workers.update(saved_workers)
        realtime.sessions.update(saved_sessions)


def test_over_capacity_leaves_protocol_3_sessions():
    worker = realtime.Worker(
        id="w-p3-shed", ws=FakeSocket(),
        manifests=[Manifest.model_validate(manifest())],
        realtime_slots=4, protocol_version=MIN_SUPPORTED_VERSION,
        admission_p95_ms={"sd-sim": 300},
    )
    live = realtime.Session(
        id=uuid.uuid4(), model_id="sd-sim", browser=FakeSocket(),
        worker=worker, state="live", assigned_at=1.0,
    )
    saved_workers = dict(realtime.workers)
    saved_sessions = dict(realtime.sessions)
    try:
        realtime.workers.clear()
        realtime.sessions.clear()
        realtime.workers[worker.id] = worker
        realtime.sessions[live.id] = live
        worker.admission_p95_ms["sd-sim"] = 600
        assert realtime.live_admission_cost(worker) == 600
        assert realtime.over_capacity_sessions(worker) == []
    finally:
        realtime.workers.clear()
        realtime.sessions.clear()
        realtime.workers.update(saved_workers)
        realtime.sessions.update(saved_sessions)


def test_heartbeat_p95_increase_reassigns_newest_protocol_4_session():
    async def scenario():
        browser_old = FakeSocket()
        browser_new = FakeSocket()
        worker_ws = FakeSocket()
        spare_ws = FakeSocket()
        worker = realtime.Worker(
            id="w-shed-live", ws=worker_ws,
            manifests=[Manifest.model_validate(manifest())],
            realtime_slots=4, admission_p95_ms={"sd-sim": 200},
        )
        spare = realtime.Worker(
            id="w-shed-spare", ws=spare_ws,
            manifests=[Manifest.model_validate(manifest())],
            realtime_slots=4, admission_p95_ms={"sd-sim": 200},
        )
        older = realtime.Session(
            id=uuid.uuid4(), model_id="sd-sim", browser=browser_old,
            worker=worker, state="live", assigned_at=1.0,
        )
        newer = realtime.Session(
            id=uuid.uuid4(), model_id="sd-sim", browser=browser_new,
            worker=worker, state="live", assigned_at=2.0,
        )
        saved_workers = dict(realtime.workers)
        saved_sessions = dict(realtime.sessions)
        try:
            realtime.workers.clear()
            realtime.sessions.clear()
            realtime.workers.update({worker.id: worker, spare.id: spare})
            realtime.sessions.update({older.id: older, newer.id: newer})
            worker.admission_p95_ms["sd-sim"] = 300
            realtime.schedule_shed_over_capacity(worker)
            await asyncio.sleep(0.01)
            assert newer.state == "assigning"
            assert older.state == "live"
            assert spare_ws.sent[0]["type"] == "open_session"
            assert spare_ws.sent[0]["session_id"] == str(newer.id)
            complete_attempt(newer)
            await asyncio.sleep(0.05)
            assert newer.state == "live"
            assert newer.worker is spare
            assert [m["type"] for m in browser_new.sent][:1] == ["interrupted"]
        finally:
            realtime.workers.clear()
            realtime.sessions.clear()
            realtime.workers.update(saved_workers)
            realtime.sessions.update(saved_sessions)

    asyncio.run(scenario())


def test_schedule_reassign_starts_only_one_placement():
    async def scenario():
        browser = FakeSocket()
        old_ws = FakeSocket()
        spare_ws = FakeSocket()
        old = realtime.Worker(
            id="w-once-old", ws=old_ws,
            manifests=[Manifest.model_validate(manifest())],
            realtime_slots=2, slots_in_use=1,
        )
        spare = realtime.Worker(
            id="w-once-spare", ws=spare_ws,
            manifests=[Manifest.model_validate(manifest())],
            realtime_slots=2,
        )
        session = realtime.Session(
            id=uuid.uuid4(), model_id="sd-sim", browser=browser,
            worker=old, state="live", assigned_at=1.0,
        )
        saved_workers = dict(realtime.workers)
        saved_sessions = dict(realtime.sessions)
        try:
            realtime.workers.clear()
            realtime.sessions.clear()
            realtime.workers.update({old.id: old, spare.id: spare})
            realtime.sessions[session.id] = session
            realtime.schedule_reassign(session)
            realtime.schedule_reassign(session)
            await asyncio.sleep(0.01)
            opens = [m for m in spare_ws.sent if m.get("type") == "open_session"]
            assert len(opens) == 1
            assert session.state == "assigning"
            complete_attempt(session)
            await asyncio.sleep(0.05)
            assert session.state == "live"
            assert session.worker is spare
        finally:
            realtime.workers.clear()
            realtime.sessions.clear()
            realtime.workers.update(saved_workers)
            realtime.sessions.update(saved_sessions)

    asyncio.run(scenario())


def test_ended_absorbs_transitions():
    session = realtime.Session(
        id=uuid.uuid4(), model_id="sd-sim", browser=FakeSocket(),
        state="ended",
    )
    assert not realtime.transition(session, "ended", "assigning")
    assert session.state == "ended"
    assert not realtime.transition(session, "live", "ending")
    assert session.state == "ended"
