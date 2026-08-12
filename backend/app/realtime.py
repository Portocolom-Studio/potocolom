"""Fleet and realtime WebSocket endpoints, per docs/connection-handling.md.

Single process, in-memory state: this is the self-hosted dispatch path. The
cloud profile replaces the in-process relay with Redis pub/sub behind the same
message flow (docs/blueprint.md); nothing on the wire changes.

Slot accounting has exactly one writer: assign() and release() on this side.
Worker heartbeats refresh liveness only; their self-reported counts are never
written back, so an in-flight heartbeat cannot undo a just-committed slot.
"""

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import time
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.manifests import (
    FRAME_P95_MAX_MS,
    Manifest,
    parse_manifests,
    validate_param_update,
    validate_params,
)
from app.settings import get_settings
from app import db

logger = logging.getLogger("potocolom.realtime")

# Wire constants; keep in sync with worker/worker/client.py.
PROTOCOL_VERSION = 2
MIN_SUPPORTED_VERSION = PROTOCOL_VERSION - 1

CANVAS_FRAME = 0x01
GENERATED_FRAME = 0x02
FRAME_HEADER_BYTES = 17  # 1 byte kind + 16 byte session uuid

CLOSE_PROTOCOL_VIOLATION = 4000
CLOSE_UNSUPPORTED_VERSION = 4002
CLOSE_NO_CAPACITY = 4003
CLOSE_UNKNOWN_MODEL = 4004
FLEET_TOKEN_HEADER = "x-fleet-token"

SESSION_READY_TIMEOUT = 10.0
WORKER_DEAD_SECONDS = 90.0  # 3 missed heartbeats, docs/connection-handling.md

router = APIRouter()


def origin_allowed(ws: WebSocket) -> bool:
    """Origin is a separate control; the documented mitigation is a trusted
    LAN (README.md). WebSocket handshakes ignore the same-origin policy
    and send no preflight, so without this a page the operator merely visits
    reaches both sockets from outside that LAN. Browsers always send Origin and
    cannot forge it; worker processes send none (issue #201)."""
    origin = ws.headers.get("origin")
    if origin is None:
        return True
    settings = get_settings()
    allowed = {settings.public_url.rstrip("/")}
    allowed.update(
        candidate.strip().rstrip("/")
        for candidate in settings.allowed_origins.split(",")
        if candidate.strip()
    )
    return origin.rstrip("/") in allowed


# Classified non-global, but reachable from the public internet through relays,
# so admitting them would contradict what this check is for: Teredo (RFC 4380)
# and 6to4 (RFC 3056, deprecated by RFC 7526).
PUBLIC_TRANSITION_NETWORKS = (
    ipaddress.ip_network("2001::/32"),
    ipaddress.ip_network("2002::/16"),
)


def forwarding_trusts_any_peer(forwarded_allow_ips: str) -> bool:
    """Whether this FORWARDED_ALLOW_IPS lets any peer forge its own address.

    uvicorn rewrites the peer address from X-Forwarded-For for every client it
    trusts, so a setting that trusts everything hands the address this module
    checks to whoever connects. Measured against uvicorn 0.50.1: the wildcard
    "*" is special only when it is the entire value, with no surrounding
    whitespace and no other list entries; a zero-length prefix, "0.0.0.0/0" for
    IPv4 or "::/0" for IPv6, trusts every peer in any position of the
    comma-separated list. The bare literals "0.0.0.0" and "::" are single
    addresses that trust nothing, so warning about those would be a false
    alarm.
    """
    if forwarded_allow_ips == "*":
        return True
    for entry in forwarded_allow_ips.split(","):
        candidate = entry.strip()
        try:
            if ipaddress.ip_network(candidate, strict=False).prefixlen == 0:
                return True
        except ValueError:
            continue  # a hostname or junk; uvicorn matches it literally at most
    return False


def peer_is_unroutable(ws: WebSocket) -> bool:
    """Whether the peer address cannot be reached from the public internet.

    Permissive mode is documented as trusted-LAN only (README.md,
    docs/self-hosting.md). Nothing enforced that: compose publishes the API on
    0.0.0.0, so a host with a public address and no firewall accepted worker
    registrations from anyone. This is that premise checked rather than assumed.

    Loopback, RFC 1918, carrier-grade NAT and link-local ranges, and IPv6 ULA
    are all non-global, so a compose worker on the bridge network and a LAN
    worker on an IPv4 or ULA address both still connect. A worker on a global
    IPv6 address does not, even on the same LAN, because nothing distinguishes
    it from a remote one.

    No peer address at all is refused. A socket always yields one, so this means
    a transport with no IP peer, and in practice that is uvicorn behind a proxy
    over a unix socket: there every public request would arrive indistinguishable
    from a local one. Such a deployment is fronted, and a fronted deployment has
    to set the secret regardless.

    An address that does not parse is admitted and logged. This code never reads
    X-Forwarded-For, but uvicorn does, and when told to trust the forwarding peer
    it copies that header in verbatim without validating it; an attacker in that
    position would send a parseable "127.0.0.1" rather than a string that does
    not parse, so refusing the unparseable form closes nothing. The
    configuration is what has to be fixed, and app/main.py warns about it.

    A reverse proxy usually replaces the peer with its own address, so a fronted
    deployment gains little here and must set the secret.
    """
    client = ws.client
    if client is None:
        logger.warning("fleet handshake has no peer address; refusing in permissive mode")
        return False
    try:
        address = ipaddress.ip_address(client.host)
    except ValueError:
        logger.warning(
            "fleet peer address %r does not parse; treating it as local", client.host)
        return True
    if address.is_global:
        return False
    return not any(address in network for network in PUBLIC_TRANSITION_NETWORKS)


def fleet_token_allowed(ws: WebSocket) -> bool:
    """Whether the peer may open a fleet socket.

    An unset key stays permissive, which keeps the one-command self-hosted
    start working (docs/decisions.md), but only for a peer that is not routable
    from the internet: permissive plus a public peer is an open door to worker
    registration, and a registered worker receives other people's prompts and
    canvas frames. Compare encoded bytes: compare_digest refuses two str
    arguments unless both are ASCII, so comparing the strings would raise on a
    secret an operator is perfectly entitled to choose, and the handler would
    then read that as a wrong token and refuse forever.
    """
    key = get_settings().fleet_token_key
    if not key:
        # The peer address cannot be trusted at all in this configuration:
        # uvicorn will have taken it from a header the client controls, so
        # there is nothing left for the check to decide on.
        if forwarding_trusts_any_peer(os.environ.get("FORWARDED_ALLOW_IPS", "")):
            logger.warning(
                "fleet handshake refused: FLEET_TOKEN_KEY is unset and "
                "FORWARDED_ALLOW_IPS trusts every peer, so uvicorn takes the "
                "peer address from X-Forwarded-For and the client can forge it")
            return False
        return peer_is_unroutable(ws)
    # Scan raw: header names are case-insensitive per RFC 9110, but Headers.get
    # matches the stored key verbatim, and the ASGI server passes the name
    # through with whatever casing the client sent. Any worker spelling this
    # X-Fleet-Token would otherwise be refused as if it sent nothing.
    presented = [value for name, value in ws.headers.raw
                 if name.lower() == FLEET_TOKEN_HEADER.encode()]
    # Exactly one, or the answer depends on which duplicate an intermediary
    # happened to put first: correct-then-wrong would authenticate and
    # wrong-then-correct would not.
    if len(presented) != 1:
        return False
    return hmac.compare_digest(presented[0], key.encode("utf-8", "surrogateescape"))


class ProtocolError(Exception):
    """The peer violated docs/connection-handling.md; close with 4000."""


_SEND_FAILURES = (WebSocketDisconnect, RuntimeError, ConnectionError, BrokenPipeError)


async def safe_send(sending: "Coroutine[object, object, None]") -> None:
    """Send to a peer that may have just closed; a dead socket is not an error."""
    try:
        await sending
    except asyncio.CancelledError:
        raise
    except _SEND_FAILURES:
        return


async def refuse(ws: WebSocket, code: int, message: str) -> None:
    """Send a terminal error and close, tolerating a peer that is already gone."""
    try:
        await ws.send_json({"type": "error", "code": code, "message": message})
        await ws.close(code=code)
    except asyncio.CancelledError:
        raise
    except _SEND_FAILURES:
        return


def parse_control(text: str) -> dict:
    try:
        control = json.loads(text)
    except (ValueError, RecursionError) as error:
        # JSONDecodeError subclasses ValueError, but CPython refuses an int
        # conversion past 4300 digits with the bare parent, and the parser
        # itself gives up past about 993 levels of nesting with a
        # RecursionError. Three of the four callers only catch ProtocolError.
        raise ProtocolError("malformed JSON") from error
    if not isinstance(control, dict) or "type" not in control:
        raise ProtocolError("control message without a type")
    return control


def peer_uuid(value: object) -> uuid.UUID:
    """Parse an id a peer sent.

    uuid.UUID raises AttributeError on an int and TypeError on null, neither of
    which belongs in the handler's except tuple: widening it would relabel an
    internal bug as a protocol violation and close 4000 with no traceback.
    """
    if not isinstance(value, str):
        raise ProtocolError("id must be a string")
    return uuid.UUID(value)


def frame_session_id(data: bytes) -> uuid.UUID:
    if len(data) < FRAME_HEADER_BYTES:
        raise ProtocolError("binary frame shorter than the header")
    return uuid.UUID(bytes=data[1:FRAME_HEADER_BYTES])


def parse_frame_p95(raw: object) -> dict[str, int] | None:
    """Validate a heartbeat's live per-model frame p95s; None means drop it.

    A payload that is not a dict at all is dropped whole: there is nothing to
    salvage. Entries within a dict are independent: a malformed one is
    skipped rather than discarding the valid entries beside it, so a worker
    that appends one junk entry to every heartbeat cannot pin the last
    accepted number. Accepts only string keys and integer values (or floats
    that are whole numbers) greater than zero and below the ceiling, which
    hello's realtime_p95_ms shares (FRAME_P95_MAX_MS). Anything unusable is
    skipped silently rather than raised: a malformed heartbeat must not kill
    the fleet connection.
    """
    if not isinstance(raw, dict):
        return None
    measured: dict[str, int] = {}
    for model_id, value in raw.items():
        if not isinstance(model_id, str):
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, float):
            if not value.is_integer():
                continue
            value = int(value)
        if not isinstance(value, int) or value <= 0 or value > FRAME_P95_MAX_MS:
            continue
        measured[model_id] = value
    return measured


@dataclass
class Worker:
    id: str
    ws: WebSocket
    manifests: list[Manifest]
    realtime_slots: int
    device: str | None = None
    memory_mode: str | None = None
    slots_in_use: int = 0
    jobs_in_flight: int = 0  # queued jobs; capped at JOB_DISPATCH_DEPTH in jobs.py
    last_seen: float = field(default_factory=time.monotonic)
    # Live per-model frame p95 from heartbeats; supersedes the calibration
    # value the worker sent in hello (registry.available()).
    frame_p95_ms: dict[str, int] = field(default_factory=dict)

    @property
    def models(self) -> list[str]:
        return [m.id for m in self.manifests]

    @property
    def free_slots(self) -> int:
        return self.realtime_slots - self.slots_in_use


@dataclass
class Session:
    id: uuid.UUID
    model_id: str
    browser: WebSocket
    params: dict = field(default_factory=dict)
    user_id: uuid.UUID | None = None
    worker: Worker | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def is_live(self) -> bool:
        """False once the browser handler's teardown has removed the session."""
        return self.id in sessions


workers: dict[str, Worker] = {}
sessions: dict[uuid.UUID, Session] = {}
gpu_requests: dict[str, asyncio.Future] = {}
closing_sessions: dict[uuid.UUID, tuple[uuid.UUID, str, Worker]] = {}


def pick_any_worker() -> Worker | None:
    """Return a connected worker, pruning sockets already closed under us.

    Fleet cleanup removes workers in its `finally`, but a socket can die in
    the window before that runs; picking it would crash the studio GPU
    endpoint on send instead of returning 503.
    """
    for worker_id, worker in list(workers.items()):
        state = getattr(worker.ws, "client_state", None)
        if state is not None and state != WebSocketState.CONNECTED:
            if workers.get(worker_id) is worker:
                del workers[worker_id]
            continue
        return worker
    return None


def pick_worker_for_model(model_id: str) -> Worker | None:
    for worker in workers.values():
        if model_id in worker.models:
            return worker
    return None


def resolve_gpu_request(control: dict) -> None:
    request_id = control.get("request_id")
    if not isinstance(request_id, str):
        return
    future = gpu_requests.pop(request_id, None)
    if future is not None and not future.done():
        future.set_result(control)


async def gpu_command(worker: Worker, command: dict, timeout: float = 120.0) -> dict:
    request_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    gpu_requests[request_id] = future
    try:
        await worker.ws.send_json({**command, "request_id": request_id})
        result = await asyncio.wait_for(future, timeout)
        return result
    except TimeoutError as error:
        raise HTTPException(status_code=504,
                            detail="worker did not respond to gpu command") from error
    finally:
        gpu_requests.pop(request_id, None)


def pick_worker(model_id: str) -> Worker | None:
    candidates = [w for w in workers.values() if model_id in w.models and w.free_slots > 0]
    return max(candidates, key=lambda w: w.free_slots, default=None)


def model_known(model_id: str) -> bool:
    return any(model_id in w.models for w in workers.values())


async def assign(session: Session, worker: Worker) -> bool:
    """Open the session on a worker and wait for its slot. True when ready.

    On any failure the slot increment is compensated here, so no caller can
    leak a slot by abandoning the session mid-assignment.
    """
    session.worker = worker
    session.ready.clear()
    worker.slots_in_use += 1
    try:
        await worker.ws.send_json(
            {"type": "open_session", "session_id": str(session.id),
             "model_id": session.model_id, "params": session.params}
        )
        await asyncio.wait_for(session.ready.wait(), SESSION_READY_TIMEOUT)
    except (TimeoutError, RuntimeError):  # unresponsive worker, or its socket just closed
        worker.slots_in_use -= 1
        session.worker = None
        return False
    return True


async def release(session: Session) -> None:
    if session.worker is None:
        return
    worker, session.worker = session.worker, None
    worker.slots_in_use -= 1
    if workers.get(worker.id) is worker:  # still connected, same incarnation
        await safe_send(
            worker.ws.send_json({"type": "close_session", "session_id": str(session.id)})
        )


async def reassign(session: Session) -> None:
    """The session's worker vanished: interrupted, new worker, resumed."""
    session.worker = None
    if not session.is_live:  # browser already gone
        return
    await safe_send(session.browser.send_json({"type": "interrupted"}))
    replacement = pick_worker(session.model_id)
    if replacement is None or not await assign(session, replacement):
        logger.warning("session %s lost its worker and no replacement was available", session.id)
        await refuse(session.browser, CLOSE_NO_CAPACITY, "no worker capacity")
        return
    if not session.is_live:  # browser disconnected while we assigned
        await release(session)
        return
    logger.info("session %s resumed on worker %s", session.id, replacement.id)
    await safe_send(session.browser.send_json({"type": "resumed"}))


async def reap_once() -> None:
    cutoff = time.monotonic() - WORKER_DEAD_SECONDS
    for worker in [w for w in workers.values() if w.last_seen < cutoff]:
        logger.warning("worker %s silent for %ds, closing", worker.id, int(WORKER_DEAD_SECONDS))
        # Closing server side wakes the fleet handler, whose cleanup
        # removes the worker and reassigns its sessions.
        await safe_send(worker.ws.close())


async def reap_dead_workers() -> None:
    while True:
        await asyncio.sleep(WORKER_DEAD_SECONDS / 3)
        await reap_once()


@router.websocket("/api/v1/fleet")
async def fleet(ws: WebSocket) -> None:
    if not origin_allowed(ws):
        logger.warning("fleet handshake refused from origin %s", ws.headers.get("origin"))
        await ws.close()  # before accept: the handshake fails with HTTP 403
        return
    if not fleet_token_allowed(ws):
        if get_settings().fleet_token_key:
            logger.warning("fleet handshake refused: invalid token")
        else:
            # Naming the cause matters: the operator who exposed the port would
            # otherwise read "invalid token" and go looking for a secret that
            # was never the problem.
            logger.warning(
                "fleet handshake refused: FLEET_TOKEN_KEY is unset, so only a worker "
                "whose address cannot route from the internet is accepted, and %s "
                "does not qualify; set the secret to admit remote workers",
                ws.client.host if ws.client else "a peer with no address")
        await ws.close()  # before accept: the handshake fails with HTTP 403
        return
    await ws.accept()
    try:
        hello = parse_control(await ws.receive_text())
        if hello["type"] != "hello":
            raise ProtocolError("first message must be hello")
        version = hello["protocol_version"]
        try:
            worker_manifests = parse_manifests(hello["models"])
        except ValueError as error:
            raise ProtocolError(str(error)) from error
        worker = Worker(id=hello["worker_id"], ws=ws, manifests=worker_manifests,
                        realtime_slots=hello["realtime_slots"],
                        device=hello.get("device"),
                        memory_mode=hello.get("memory_mode"))
        if not (isinstance(version, int) and isinstance(worker.id, str)
                and isinstance(worker.realtime_slots, int)
                and (worker.device is None or isinstance(worker.device, str))
                and (worker.memory_mode is None or isinstance(worker.memory_mode, str))):
            raise ProtocolError("hello fields have wrong types")
    except (ProtocolError, KeyError) as error:
        # Logged: a rejected hello is otherwise silent on both sides, so an
        # operator with a bad manifest sees a worker that starts and never
        # registers, with nothing explaining why.
        logger.warning("fleet hello refused: %s", error)
        # Reason on the wire: the worker logs the close it receives, and
        # without it an operator with a bad manifest sees only a reconnect
        # loop with no cause on either side. Truncate in BYTES, not code
        # points: a close frame carries at most 125, of which 2 are the code,
        # and manifest ids reach this message unfiltered. Over that, websockets
        # raises its own ProtocolError, which is a different class from ours
        # and so escapes this handler, aborting with 1006 and no reason at all.
        # The ignore on encode also drops the lone surrogates json.loads
        # accepts but UTF-8 cannot represent.
        detail = str(error).encode("utf-8", "ignore")[:123].decode("utf-8", "ignore")
        await ws.close(code=CLOSE_PROTOCOL_VIOLATION, reason=detail)
        return
    if version < MIN_SUPPORTED_VERSION:
        logger.warning("worker %s rejected: protocol version %s below %s",
                       worker.id, version, MIN_SUPPORTED_VERSION)
        await ws.send_json({"type": "rejected", "reason": "unsupported protocol version",
                            "min_supported_version": MIN_SUPPORTED_VERSION})
        await ws.close(code=CLOSE_UNSUPPORTED_VERSION)
        return
    workers[worker.id] = worker
    logger.info("worker %s registered models=%s slots=%d",
                worker.id, worker.models, worker.realtime_slots)
    await ws.send_json({"type": "registered"})
    from app import gpu_samples, registry  # late import; registry reads this module's state
    gpu_samples.schedule_worker_identity(worker.id, worker.device, worker.memory_mode)
    try:
        # Inside the try: this awaits a database write, and a failure before
        # the try left the worker in `workers` with no cleanup path, so it kept
        # being advertised until the 90 second reaper noticed.
        await registry.persist_manifests(worker.manifests)
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            try:
                if message.get("bytes") is not None:
                    data = message["bytes"]
                    session = sessions.get(frame_session_id(data))
                    # The worker is a network peer too: only the assigned
                    # worker may relay frames for this session. A frame for a
                    # session this worker does not own is dropped, because
                    # reassignment races produce those legitimately; the
                    # assigned worker sending the wrong kind is not a race.
                    if session is not None and session.worker is worker:
                        if data[0] != GENERATED_FRAME:
                            raise ProtocolError("worker frame is not a generated frame")
                        await safe_send(session.browser.send_bytes(data))
                elif message.get("text") is not None:
                    control = parse_control(message["text"])
                    worker.last_seen = time.monotonic()
                    if control["type"] == "session_ready":
                        session = sessions.get(peer_uuid(control["session_id"]))
                        if session is not None and session.worker is worker:
                            session.ready.set()
                    elif control["type"] in ("job_progress", "job_done", "job_failed"):
                        from app import jobs  # late import; jobs reads this module's state
                        await jobs.on_worker_message(worker, control)
                    elif control["type"] == "session_closed":
                        session_id = peer_uuid(control["session_id"])
                        # Peek before popping: a worker that held this session
                        # earlier still knows its id, and popping on its word
                        # would drop the current owner's entry and bill this
                        # user for a session it did not run.
                        owner = closing_sessions.get(session_id)
                        if owner is not None and owner[2] is worker:
                            del closing_sessions[session_id]
                            from app import usage_events
                            usage_events.schedule_realtime(owner[0], owner[1], control)
                    elif control["type"] in ("gpu_status", "model_loaded",
                                             "model_unloaded", "gpu_error"):
                        resolve_gpu_request(control)
                    elif control["type"] == "heartbeat":
                        gpu = control.get("gpu")
                        if worker.device is None and isinstance(gpu, dict):
                            device = gpu.get("device")
                            if isinstance(device, str):
                                worker.device = device
                        if worker.memory_mode is None:
                            memory_mode = control.get("memory_mode")
                            if isinstance(memory_mode, str):
                                worker.memory_mode = memory_mode
                        measured = parse_frame_p95(control.get("frame_p95_ms"))
                        if measured is not None:
                            # Merge, not replace: a skipped entry must leave
                            # the value already held for that model in place,
                            # or a worker that intermittently sends junk for
                            # one model makes that model's label flap between
                            # hello's value and the live one. Merging is safe
                            # because measurements only accumulate for models
                            # this worker has measured, and a stale entry is
                            # a worse outcome than no entry only if it can
                            # never be corrected, which a later heartbeat
                            # does.
                            worker.frame_p95_ms.update(measured)
                        gpu_samples.schedule_heartbeat_sample(
                            worker.id, control, worker.device, worker.memory_mode
                        )
                    # Heartbeats refresh last_seen only; slot accounting has
                    # one writer (assign/release), so self-reported counts
                    # are deliberately not written back.
            except (ProtocolError, KeyError, ValueError):
                logger.warning("worker %s violated the protocol, closing", worker.id)
                await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
                break
    finally:
        if workers.get(worker.id) is worker:
            del workers[worker.id]
        for session_id, owner in list(closing_sessions.items()):
            if owner[2] is worker:
                closing_sessions.pop(session_id, None)
        from app import jobs
        jobs.on_worker_lost(worker)
        orphaned = [s for s in sessions.values() if s.worker is worker]
        if orphaned:
            logger.info("worker %s disconnected with %d sessions to reassign",
                        worker.id, len(orphaned))
        for session in orphaned:
            asyncio.ensure_future(reassign(session))


@router.websocket("/api/v1/realtime")
async def realtime(ws: WebSocket) -> None:
    if not origin_allowed(ws):
        logger.warning("realtime handshake refused from origin %s", ws.headers.get("origin"))
        await ws.close()  # before accept: the handshake fails with HTTP 403
        return
    await ws.accept()
    try:
        opening = parse_control(await ws.receive_text())
        if opening["type"] != "open":
            raise ProtocolError("first message must be open")
        model_id = opening["model_id"]
        params = opening.get("params") or {}
        if not isinstance(params, dict):
            raise ProtocolError("params must be an object")
    except (ProtocolError, KeyError):
        await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
        return
    if not model_known(model_id):
        await refuse(ws, CLOSE_UNKNOWN_MODEL, "unknown model")
        return
    from app import registry

    manifest = registry.available().get(model_id)
    if manifest is not None:
        if validate_params(manifest, params) is not None:
            await refuse(ws, CLOSE_PROTOCOL_VIOLATION, "invalid params")
            return
    worker = pick_worker(model_id)
    if worker is None:
        await refuse(ws, CLOSE_NO_CAPACITY, "no worker capacity")
        return
    session = Session(
        id=uuid.uuid4(), model_id=model_id, browser=ws, params=params,
        # WebSocket identity is not derived from current_user until upgrade auth
        # gains a session cookie or one-time ticket.
        user_id=db.local_user_id)
    sessions[session.id] = session
    try:
        if not await assign(session, worker):
            await refuse(ws, CLOSE_NO_CAPACITY, "worker did not become ready")
            return
        await ws.send_json({"type": "ready", "session_id": str(session.id)})
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            try:
                if message.get("bytes") is not None:
                    data = message["bytes"]
                    # The browser is untrusted: frames must be canvas frames
                    # for this connection's own session, nothing else.
                    if frame_session_id(data) != session.id or data[0] != CANVAS_FRAME:
                        raise ProtocolError("frame does not belong to this session")
                    if session.worker is not None:  # a dead worker means reassign is in flight
                        await safe_send(session.worker.ws.send_bytes(data))
                elif message.get("text") is not None:
                    control = parse_control(message["text"])
                    if control["type"] == "close":
                        break
                    if control["type"] == "update_params":
                        params = control.get("params")
                        if not isinstance(params, dict):
                            raise ProtocolError("params must be an object")
                        manifest = registry.available().get(session.model_id)
                        if manifest is not None:
                            invalid = validate_param_update(manifest, params)
                            if invalid is not None:
                                # A bad update is a recoverable client mistake,
                                # unlike a bad open, which happens before a
                                # session exists: report it and keep the socket.
                                await safe_send(ws.send_json({
                                    "type": "error",
                                    "code": CLOSE_PROTOCOL_VIOLATION,
                                    "message": invalid,
                                }))
                                continue
                        # Later keys win, so a second update of the same
                        # parameter overwrites the first. The merged dict is
                        # what the worker replaces its params with, and what
                        # the browser confirms as actually applied.
                        session.params.update(params)
                        if session.worker is not None:
                            await safe_send(session.worker.ws.send_json({
                                "type": "update_session",
                                "session_id": str(session.id),
                                "params": session.params,
                            }))
                        await safe_send(ws.send_json({
                            "type": "params_updated",
                            "params": session.params,
                        }))
            except ProtocolError:
                await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
                break
    finally:
        sessions.pop(session.id, None)
        worker = session.worker
        if (
            worker is not None
            and session.user_id is not None
            and workers.get(worker.id) is worker
        ):
            closing_sessions[session.id] = (
                session.user_id, session.model_id, worker)
        await release(session)
