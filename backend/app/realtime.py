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
import random
import time
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Literal

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
PROTOCOL_VERSION = 4
MIN_SUPPORTED_VERSION = PROTOCOL_VERSION - 1
# The protocol version that introduced the update_session message. An N-1
# worker is deliberately welcome (MIN_SUPPORTED_VERSION). Protocol 3 already
# speaks that message unfenced; protocol 4 adds control_generation on it.
# The API still refuses an update whose assigned worker predates the
# message entirely rather than acknowledge one the worker cannot apply.
UPDATE_SESSION_PROTOCOL_VERSION = 3
# Lifecycle fencing: control_generation on open/update/close and on
# session_ready / session_refused. A protocol 3 worker has no generation
# and may serve a session's first attempt only.
CONTROL_GENERATION_PROTOCOL_VERSION = 4

CANVAS_FRAME = 0x01
GENERATED_FRAME = 0x02
FRAME_HEADER_BYTES = 17  # 1 byte kind + 16 byte session uuid

CLOSE_PROTOCOL_VIOLATION = 4000
CLOSE_UNSUPPORTED_VERSION = 4002
CLOSE_NO_CAPACITY = 4003
CLOSE_UNKNOWN_MODEL = 4004
# Authentication outcomes, from the authentication contract: a missing cookie
# fails the handshake outright, a cookie that resolves to nothing closes 4401,
# and a principal without permission to spend a realtime slot closes 4403.
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
# Only these may consume a slot. A viewer reads their own work; realtime is
# not reading.
REALTIME_ROLES = frozenset({"user", "admin"})
# How long revocation waits for one socket to take its close. A wedged
# transport must not hold the request that revoked it.
CLOSE_TIMEOUT = 2.0
FLEET_TOKEN_HEADER = "x-fleet-token"
# Same 500 ms bar the worker uses in slots_from_frame_ms. Duplicated rather
# than imported: the two packages have no shared module.
REALTIME_BAR_MS = 500

SESSION_READY_TIMEOUT = 10.0
WORKER_DEAD_SECONDS = 90.0  # 3 missed heartbeats, docs/connection-handling.md

# One bound for every session seed, shared with the worker's SEED_BOUND
# (worker/worker/client.py): the API fills the seed at session open and the
# worker's ensure_seed fallback must draw from the same range, so both sides
# agree on what a seed is. The two packages have no shared import, so the
# number is written twice with this comment binding them, like the wire
# constants above.
SESSION_SEED_BOUND = 2**31 - 1

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

    An unset key refuses the handshake. Preflight writes FLEET_SECRET so a
    fresh install has one; an upgrade with an empty key fails startup with
    a message that names scripts/preflight.sh. Compare encoded bytes:
    compare_digest refuses two str arguments unless both are ASCII, so
    comparing the strings would raise on a secret an operator is perfectly
    entitled to choose, and the handler would then read that as a wrong
    token and refuse forever.
    """
    key = get_settings().fleet_token_key
    if not key:
        logger.warning(
            "fleet handshake refused: FLEET_TOKEN_KEY is unset; "
            "run scripts/preflight.sh to write deploy/compose/.env"
        )
        return False
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
    # Advertised in hello, and read by three gates: the update_params handler
    # compares it against UPDATE_SESSION_PROTOCOL_VERSION to refuse an update
    # an older worker would silently drop, the dispatch-token check in
    # jobs.py requires the token from a worker at 3 or newer, and assign /
    # reassign send control_generation only to protocol 4. A registration
    # always sets it; the default covers a Worker built without one, and it is
    # the current version rather than None so that gate fails closed rather
    # than granting the leniency that exists only for an older worker
    # (issue #282).
    protocol_version: int = PROTOCOL_VERSION
    slots_in_use: int = 0
    jobs_in_flight: int = 0  # queued jobs; capped at JOB_DISPATCH_DEPTH in jobs.py
    last_seen: float = field(default_factory=time.monotonic)
    # Live per-model frame p95 from heartbeats; supersedes the calibration
    # value the worker sent in hello (registry.available()).
    frame_p95_ms: dict[str, int] = field(default_factory=dict)
    # Admission costs from hello's optional top-level realtime_p95_ms map.
    # None means the worker omitted the map (N-1 integer pool). Heartbeat
    # may raise entries; it must not lower them on this connection.
    admission_p95_ms: dict[str, int] | None = None

    @property
    def models(self) -> list[str]:
        return [m.id for m in self.manifests]

    @property
    def free_slots(self) -> int:
        return self.realtime_slots - self.slots_in_use


SessionState = Literal["queued", "assigning", "live", "idle", "ending", "ended"]


@dataclass
class Session:
    id: uuid.UUID
    model_id: str
    browser: WebSocket
    params: dict = field(default_factory=dict)
    user_id: uuid.UUID | None = None
    # The account session this socket was opened under, so revoking that
    # session can find and close this one.
    auth_session_id: uuid.UUID | None = None
    worker: Worker | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    # queued and idle are in the enum so later work does not widen it;
    # this protocol does not ship the admission queue or idle release.
    state: SessionState = "assigning"
    control_generation: int = 1
    attempt_ok: bool = False
    assigned_at: float = 0.0

    @property
    def is_live(self) -> bool:
        """False once the browser handler's teardown has removed the session."""
        return self.id in sessions


def transition(session: Session, expected: SessionState | set[SessionState],
               new: SessionState) -> bool:
    """Compare expected state and move. False if the session is ended or elsewhere.

    ended absorbs: a transition out of it is a no-op rather than an error,
    because a late message is exactly what fencing expects to see.
    """
    if session.state == "ended":
        return False
    allowed = {expected} if isinstance(expected, str) else expected
    if session.state not in allowed:
        return False
    session.state = new
    return True


def speaks_generation(worker: Worker) -> bool:
    return worker.protocol_version >= CONTROL_GENERATION_PROTOCOL_VERSION


def message_generation(control: dict, worker: Worker) -> int | None:
    """Generation this lifecycle message answers, or None if it must be ignored.

    Protocol 4 requires a positive int. An unfenced protocol 4 message is
    not believed: that is the race fencing exists to prevent, and it is
    deliberately not the jobs-path exception for a missing dispatch_token.
    Protocol 3 has no generations; extra fields are ignored and the
    message is generation 1 (first attempt only).
    """
    raw = control.get("control_generation")
    if speaks_generation(worker):
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            return None
        return raw
    return 1


def with_generation(payload: dict, worker: Worker, generation: int) -> dict:
    if speaks_generation(worker):
        payload["control_generation"] = generation
    return payload


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


def live_admission_cost(worker: Worker) -> int:
    """Serialized frame cost of sessions currently on this worker."""
    if worker.admission_p95_ms is None:
        return 0
    total = 0
    for session in sessions.values():
        if session.worker is not worker:
            continue
        cost = worker.admission_p95_ms.get(session.model_id)
        if cost:
            total += cost
    return total


def pick_worker(model_id: str, *, generation: int = 1,
                exclude_ids: set[str] | None = None) -> Worker | None:
    ranked: list[tuple[int, Worker]] = []
    for worker in workers.values():
        if exclude_ids is not None and worker.id in exclude_ids:
            continue
        if model_id not in worker.models:
            continue
        # A protocol 3 worker may serve a session's first attempt only and
        # is never a reassignment candidate: it has no generation with which
        # to tell two attempts apart.
        if generation > 1 and not speaks_generation(worker):
            continue
        if worker.admission_p95_ms is None:
            if worker.free_slots > 0:
                ranked.append((worker.free_slots, worker))
            continue
        cost = worker.admission_p95_ms.get(model_id)
        if not cost:
            continue
        leftover = REALTIME_BAR_MS - live_admission_cost(worker) - cost
        if leftover >= 0:
            ranked.append((leftover, worker))
    if not ranked:
        return None
    return max(ranked, key=lambda item: item[0])[1]


def model_known(model_id: str) -> bool:
    return any(model_id in w.models for w in workers.values())


async def close_abandoned_session(worker: Worker, session: Session,
                                  generation: int | None = None) -> None:
    """Best-effort notice that the API has given up on an open.

    The open may still be in flight when the API stops waiting, and a
    close_session is what lets the worker discard the runner instead of
    serving a session nobody will ever feed frames to. Only while the worker
    is still the same registered incarnation, like release(): a departed
    incarnation dies with its connection, so there is nothing left to close.
    """
    if workers.get(worker.id) is not worker:
        return
    payload = {"type": "close_session", "session_id": str(session.id)}
    if generation is None:
        generation = session.control_generation
    await safe_send(worker.ws.send_json(with_generation(payload, worker, generation)))


async def assign(session: Session, worker: Worker) -> bool:
    """Open the session on a worker and wait for its slot. True when ready.

    On any failure the slot increment is compensated here, so no caller can
    leak a slot by abandoning the session mid-assignment.

    Giving up also tells the worker the session is dead
    (close_abandoned_session): a worker that knows nothing would create a
    runner for a session nobody will ever close and wait for frames that
    cannot arrive for the life of the process.
    """
    session.worker = worker
    session.ready.clear()
    session.attempt_ok = False
    worker.slots_in_use += 1
    sent_generation = session.control_generation
    try:
        payload = {
            "type": "open_session",
            "session_id": str(session.id),
            "model_id": session.model_id,
            "params": session.params,
        }
        await worker.ws.send_json(with_generation(payload, worker, sent_generation))
        await asyncio.wait_for(session.ready.wait(), SESSION_READY_TIMEOUT)
    except (TimeoutError, RuntimeError):  # unresponsive worker, or its socket just closed
        if session.worker is worker:
            # release() and assign() are two writers of one counter, so the
            # compensation is ownership-checked rather than assumed: the
            # browser may have left mid-assignment, and its teardown's
            # release() has already decremented the slot this call
            # incremented and cleared the session's worker. Decrementing
            # again would underflow the counter and advertise a free slot
            # that does not exist.
            worker.slots_in_use -= 1
            session.worker = None
            await close_abandoned_session(worker, session, sent_generation)
        return False
    if session.control_generation != sent_generation:
        # Another waiter (reassign) advanced the generation. Do not
        # compensate: that attempt owns the slot now.
        return session.state == "live"
    if session.attempt_ok and session.worker is worker:
        if session.state == "assigning":
            transition(session, "assigning", "live")
            session.assigned_at = time.monotonic()
        return True
    if session.worker is worker:
        worker.slots_in_use -= 1
        session.worker = None
        await close_abandoned_session(worker, session, sent_generation)
    return False


async def place_session(session: Session, *, exclude_ids: set[str] | None = None) -> bool:
    """Try workers until one answers ready, or none remain.

    A failed attempt (timeout or session_refused) is not a failed session:
    generation increases and the next protocol 4 candidate is tried.
    """
    skipped: set[str] = set(exclude_ids or ())
    while session.is_live and session.state == "assigning":
        worker = pick_worker(
            session.model_id,
            generation=session.control_generation,
            exclude_ids=skipped,
        )
        if worker is None:
            return False
        started = session.control_generation
        ok = await assign(session, worker)
        if ok or session.state == "live":
            return True
        if not session.is_live or session.state != "assigning":
            return False
        skipped.add(worker.id)
        if not ok and session.control_generation == started:
            session.control_generation += 1
    return session.state == "live"


async def release(session: Session) -> None:
    if session.worker is None:
        return
    worker, session.worker = session.worker, None
    worker.slots_in_use -= 1
    generation = session.control_generation
    if workers.get(worker.id) is worker:  # still connected, same incarnation
        await safe_send(worker.ws.send_json(with_generation(
            {"type": "close_session", "session_id": str(session.id)},
            worker, generation,
        )))


async def reassign(session: Session) -> None:
    """The session's worker vanished or refused: interrupted, new worker, resumed."""
    if session.state == "ended":
        return
    if session.state == "live":
        if not transition(session, "live", "assigning"):
            return
    elif session.state != "assigning":
        return
    worker = session.worker
    generation = session.control_generation
    if worker is not None:
        if session.worker is worker:
            worker.slots_in_use -= 1
            session.worker = None
        await close_abandoned_session(worker, session, generation)
    if not session.is_live:
        return
    await safe_send(session.browser.send_json({"type": "interrupted"}))
    session.control_generation += 1
    exclude_ids = {worker.id} if worker is not None else set()
    if await place_session(session, exclude_ids=exclude_ids):
        if not session.is_live:
            await release(session)
            return
        logger.info("session %s resumed on worker %s", session.id,
                    session.worker.id if session.worker else "?")
        await safe_send(session.browser.send_json({"type": "resumed"}))
        return
    logger.warning("session %s lost its worker and no replacement was available",
                   session.id)
    transition(session, "assigning", "ending")
    await refuse(session.browser, CLOSE_NO_CAPACITY, "no worker capacity")


_reassign_tasks: set[asyncio.Task] = set()


def schedule_reassign(session: Session) -> None:
    """Hold one reassign task so the loop cannot drop it.

    Only a won live -> assigning transition starts a task. Shed, a live
    session_refused, and worker-loss cleanup all call this, so two of
    those in one recv batch cannot start two placement loops.
    """
    if not transition(session, "live", "assigning"):
        return

    async def guarded() -> None:
        try:
            await reassign(session)
        except asyncio.CancelledError:
            raise
        except BaseException:
            logger.exception("reassign failed for session %s", session.id)

    task = asyncio.create_task(guarded())
    _reassign_tasks.add(task)
    task.add_done_callback(_reassign_tasks.discard)


def over_capacity_sessions(worker: Worker) -> list[Session]:
    """Newest live protocol-4 sessions on this worker until the live sum fits.

    Protocol 3 sessions stay: that worker cannot fence a replacement, and
    new admissions are already blocked. Newest-first keeps the sessions that
    were honest when admitted.
    """
    if worker.admission_p95_ms is None or not speaks_generation(worker):
        return []
    overflow = live_admission_cost(worker) - REALTIME_BAR_MS
    if overflow <= 0:
        return []
    live = [
        session for session in sessions.values()
        if session.worker is worker and session.state == "live"
    ]
    live.sort(key=lambda item: item.assigned_at, reverse=True)
    victims: list[Session] = []
    shed = 0
    for session in live:
        if shed >= overflow:
            break
        cost = worker.admission_p95_ms.get(session.model_id) or 0
        victims.append(session)
        shed += cost
    return victims


def schedule_shed_over_capacity(worker: Worker) -> None:
    for session in over_capacity_sessions(worker):
        schedule_reassign(session)


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
            logger.warning(
                "fleet handshake refused: FLEET_TOKEN_KEY is unset; "
                "run scripts/preflight.sh to write deploy/compose/.env"
            )
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
                        protocol_version=version,
                        device=hello.get("device"),
                        memory_mode=hello.get("memory_mode"))
        if "realtime_p95_ms" in hello:
            parsed = parse_frame_p95(hello.get("realtime_p95_ms"))
            worker.admission_p95_ms = parsed if parsed is not None else {}
            if worker.admission_p95_ms:
                worker.admission_p95_ms = {
                    model_id: value
                    for model_id, value in worker.admission_p95_ms.items()
                    if model_id in worker.models
                }
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
                        generation = message_generation(control, worker)
                        if (session is not None and session.worker is worker
                                and generation is not None
                                and generation == session.control_generation
                                and transition(session, "assigning", "live")):
                            session.assigned_at = time.monotonic()
                            session.attempt_ok = True
                            session.ready.set()
                    elif control["type"] == "session_refused":
                        session = sessions.get(peer_uuid(control["session_id"]))
                        generation = message_generation(control, worker)
                        if (session is None or session.worker is not worker
                                or generation is None
                                or generation != session.control_generation):
                            pass
                        elif session.state == "assigning":
                            session.attempt_ok = False
                            session.ready.set()
                        elif session.state == "live":
                            schedule_reassign(session)
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
                            # does. The merge is bounded by the worker's own
                            # manifest set: a measurement for a model this
                            # worker does not serve is meaningless, and
                            # admitting one would let the map grow with every
                            # heartbeat for the worker's lifetime. The ids
                            # come from the registered manifests, never from
                            # anything the heartbeat carries.
                            worker.frame_p95_ms.update({
                                model_id: value
                                for model_id, value in measured.items()
                                if model_id in worker.models
                            })
                            if worker.admission_p95_ms is not None:
                                raised = False
                                for model_id, value in measured.items():
                                    if model_id not in worker.models:
                                        continue
                                    held = worker.admission_p95_ms.get(model_id)
                                    if held is None:
                                        continue
                                    if value > held:
                                        worker.admission_p95_ms[model_id] = value
                                        raised = True
                                if raised:
                                    schedule_shed_over_capacity(worker)
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
            if session.state == "assigning":
                session.attempt_ok = False
                session.ready.set()
            elif session.state == "live":
                schedule_reassign(session)


def session_seed(value: object) -> int | None:
    """The seed a session opens with, normalised where the API owns it.

    Returns an integer seed, or None when the caller must draw a fresh one.
    An integer is kept as-is. A float that is a whole number is kept as an
    integer: JSON Schema accepts 42.0 as an integer, and the engine's
    generator wants an int. A bool is refused even though it subclasses int,
    or `seed: true` would survive as a seed. Anything else (a fractional
    float, a string, null) is refused too: a manifest that does not declare
    a seed property lets such a value through validation entirely, and the
    engine then builds no generator. Mirrors the worker's normalise_seed
    (worker/client.py): the two packages have no shared import, so each
    boundary writes its own, with this comment pointing at the other the
    way SESSION_SEED_BOUND and SEED_BOUND already do.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


@dataclass
class Handshake:
    """How a realtime upgrade was judged.

    close_code None admits the socket. Anything else is accepted and then
    closed with that code, because a close code cannot be sent before accept.
    """

    user_id: uuid.UUID | None = None
    auth_session_id: uuid.UUID | None = None
    close_code: int | None = None


async def _handshake_principal(ws: WebSocket) -> Handshake | None:
    """The account behind this socket, or None to refuse before accepting.

    Bind once: the principal is resolved here and never read from the browser,
    which cannot select an identity. Revocation is explicit and closes the
    socket, rather than being noticed on some later frame.
    """
    if get_settings().auth_mode == "none":
        # The implicit local user, which is None while the database is down.
        # That is not an authentication failure; it is the mode working.
        return Handshake(user_id=db.local_user_id)
    from app import sessions as account_sessions

    name, _ = account_sessions.cookie_names(get_settings().public_url)
    token = ws.cookies.get(name)
    if not token:
        return None
    try:
        resolved = await account_sessions.resolve(token)
    except Exception:
        # The none branch above tolerates a database that is down. This one
        # must be equally defined: refuse, rather than raise out of the
        # endpoint before accept and make every reconnect a traceback.
        logger.warning("realtime handshake could not resolve a session")
        return Handshake(close_code=CLOSE_UNAUTHORIZED)
    if resolved is None:
        return Handshake(close_code=CLOSE_UNAUTHORIZED)
    if resolved.user.role not in REALTIME_ROLES or resolved.user.state != "active":
        return Handshake(close_code=CLOSE_FORBIDDEN)
    return Handshake(user_id=resolved.user.id, auth_session_id=resolved.session.id)


async def _still_live(handshake: Handshake) -> bool:
    if handshake.auth_session_id is None:
        return True
    from app import sessions as account_sessions

    try:
        return await account_sessions.is_live(handshake.auth_session_id)
    except Exception:
        # Cannot prove the session is alive, so treat it as dead. The socket
        # is about to hold a GPU slot on the strength of this answer.
        logger.warning("realtime could not confirm a session is still live")
        return False


@router.websocket("/api/v1/realtime")
async def realtime(ws: WebSocket) -> None:
    if not origin_allowed(ws):
        logger.warning("realtime handshake refused from origin %s", ws.headers.get("origin"))
        await ws.close()  # before accept: the handshake fails with HTTP 403
        return
    handshake = await _handshake_principal(ws)
    if handshake is None:
        # No credential at all: refused before accept, so the upgrade fails as
        # HTTP 403 and no socket exists to admit anything.
        await ws.close()
        return
    await ws.accept()
    if handshake.close_code is not None:
        await refuse(ws, handshake.close_code, "not permitted to open a realtime session")
        return
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
    seed = session_seed(params.get("seed"))
    # The API owns the session seed, not the worker: a session outlives
    # its worker, so reassign re-opens with session.params and the value
    # must ride on it. An explicit client seed is kept so a session can be
    # reproduced exactly (normalised above: a whole float is an integer in
    # JSON Schema); a missing, boolean or non-numeric one is filled here so
    # the worker always receives an int (worker/client.py's ensure_seed is
    # only the fallback for an older API). SESSION_SEED_BOUND matches the
    # worker's SEED_BOUND. This coercion lives only here, on the open path:
    # the update path refuses a seed outright (a session's seed is fixed
    # for its life), so it must not be repeated there.
    params = {**params, "seed": seed if seed is not None
              else random.randrange(SESSION_SEED_BOUND)}
    if pick_worker(model_id) is None:
        await refuse(ws, CLOSE_NO_CAPACITY, "no worker capacity")
        return
    session = Session(
        id=uuid.uuid4(), model_id=model_id, browser=ws, params=params,
        user_id=handshake.user_id, auth_session_id=handshake.auth_session_id)
    sessions[session.id] = session
    # Registered first, then re-checked: a revocation that commits between the
    # handshake and this line runs its close sweep before the session is in
    # the dictionary, so nothing would ever close this socket.
    if not await _still_live(handshake):
        del sessions[session.id]
        await refuse(ws, CLOSE_UNAUTHORIZED, "session revoked")
        return
    try:
        accepted = await place_session(session)
        if not accepted:
            transition(session, "assigning", "ending")
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
                        if "seed" in params:
                            # A session's seed is fixed for its life: that is
                            # the invariant the rest of the seed work
                            # assumes, and it is the only version-safe
                            # answer, because a mixed-version fleet cannot
                            # agree on a mid-session change (an N-1 worker
                            # overwrites it with the seed it drew at open,
                            # so the browser and the API would acknowledge a
                            # value no frame used). A deliberate reroll is a
                            # feature and needs its own design (a new
                            # session or an explicit protocol message), not
                            # a silent parameter update; reject it like an
                            # out-of-range parameter and leave the session
                            # running.
                            await safe_send(ws.send_json({
                                "type": "error",
                                "code": CLOSE_PROTOCOL_VIOLATION,
                                "message": "seed is fixed at session open and cannot be changed",
                            }))
                            continue
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
                        if (session.worker is not None
                                and session.worker.protocol_version
                                < UPDATE_SESSION_PROTOCOL_VERSION):
                            # The assigned worker predates update_session (an
                            # N-1 worker is deliberately welcome) and would
                            # silently drop it, leaving the browser told the
                            # update applied while every later frame still
                            # renders the old prompt. Acknowledging an update
                            # the worker cannot apply is worse than refusing
                            # it: the user cannot tell a silent no-op from a
                            # model that ignored their prompt. Refuse, and
                            # leave the session rendering what it renders.
                            await safe_send(ws.send_json({
                                "type": "error",
                                "code": CLOSE_PROTOCOL_VIOLATION,
                                "message": "the assigned worker does not "
                                           "support live parameter updates",
                            }))
                            continue
                        # Later keys win, so a second update of the same
                        # parameter overwrites the first. The merged dict is
                        # what the session holds from here on, what a worker
                        # replaces its params with, and what the browser is
                        # told. Not "what applied": this acknowledges even
                        # while a reassignment leaves the session without a
                        # worker, and a worker fills in manifest defaults for
                        # keys nobody set, which never appear here.
                        session.params.update(params)
                        if session.worker is not None:
                            await safe_send(session.worker.ws.send_json(with_generation({
                                "type": "update_session",
                                "session_id": str(session.id),
                                "params": session.params,
                            }, session.worker, session.control_generation)))
                        await safe_send(ws.send_json({
                            "type": "params_updated",
                            "params": session.params,
                        }))
            except ProtocolError:
                await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
                break
    finally:
        sessions.pop(session.id, None)
        transition(session, {"queued", "assigning", "live", "idle", "ending"}, "ending")
        transition(session, "ending", "ended")
        worker = session.worker
        if (
            worker is not None
            and session.user_id is not None
            and workers.get(worker.id) is worker
        ):
            closing_sessions[session.id] = (
                session.user_id, session.model_id, worker)
        await release(session)


async def close_revoked(user_id: uuid.UUID, auth_session_id: uuid.UUID | None = None) -> None:
    """Close every live socket a revoked account session was holding.

    The principal binds once at the handshake, so nothing would notice the
    revocation on its own. This is what makes logout, disable, deletion and a
    role change actually reach a canvas that is already drawing.
    """
    doomed = [
        session for session in list(sessions.values())
        if session.user_id == user_id
        and (auth_session_id is None or session.auth_session_id == auth_session_id)
    ]
    # Concurrently: refuse writes before it closes, and a browser that stopped
    # reading blocks that write with no timeout. Sequentially, one such socket
    # would keep every other socket on the account alive and hang the logout
    # request that asked for them to go.
    # The server side goes first and unconditionally. A wedged transport eats
    # the write below and its close with it, and leaving the session
    # registered would keep a revoked account holding a GPU slot for as long
    # as it holds the connection.
    for session in doomed:
        sessions.pop(session.id, None)
        transition(session, {"queued", "assigning", "live", "idle", "ending"}, "ending")
        transition(session, "ending", "ended")
        await release(session)
    await asyncio.gather(
        *(asyncio.wait_for(
            refuse(session.browser, CLOSE_UNAUTHORIZED, "session revoked"), CLOSE_TIMEOUT)
          for session in doomed),
        return_exceptions=True,
    )
