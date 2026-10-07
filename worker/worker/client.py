"""The worker's side of the fleet connection, per docs/connection-handling.md.

Real inference lives behind the Engine seam (worker/engine.py): diffusers
when a models directory is configured, a simulated engine otherwise.
Everything else (dial out, backoff, registration, heartbeats, latest input
wins, job execution) is identical in both cases.
"""

import asyncio
import contextvars
import hashlib
import json
import logging
import random
import time
import uuid
from contextlib import suppress
from typing import Any

import httpx
import websockets

from worker.protocol6 import (
    Protocol6Error,
    canonical_bytes,
    decode_control,
    preflight_worker_control,
    validate_message,
)

from worker.engine import (
    Cancelled,
    Engine,
    NotResidentError,
    PromptCache,
    SimulatedEngine,
    make_thumbnail_webp,
)
from worker.categorize import categorize_output, enable_categorizer
from worker.manifests import SIMULATED_MANIFEST, Manifest, load_manifests
from worker.gpu_metrics import sample_gpu
from worker.settings import Settings, get_settings

logger = logging.getLogger("potocolom.worker")

# Wire constants; keep in sync with backend/app/realtime.py.
PROTOCOL_VERSION = 6
PROTOCOL6_CAPABILITIES = [
    "command_dedupe",
    "usage_checkpoint",
    "gpu_drain_accounting",
    "authority_fence",
    "work_budget",
]
GENERATED_FRAME = 0x02
# 1 byte kind + 16 byte session uuid: the fixed prefix of the header, which is
# also the whole header of a protocol 4 API.
LEGACY_FRAME_HEADER_BYTES = 17
# Protocol 5 adds the 4 byte big-endian input revision to both frame kinds, so
# a frame shorter than this never reached the runner and closes 4000.
FRAME_HEADER_BYTES = 21
# The API's own receive limit (uvicorn --ws-max-size). It forwards canvas
# frames of up to 21 + 1 MiB bytes, over the websockets default of exactly
# 1 MiB, which would drop the fleet socket and every session on it.
FLEET_MAX_MESSAGE_BYTES = 2 * 1024 * 1024
CLOSE_PROTOCOL_VIOLATION = 4000

UPLOAD_TIMEOUT = 60.0
# Heartbeat interval while a job runs without denoising progress (model load).
PROGRESS_KEEPALIVE_SECONDS = 60.0



class RegistrationRejected(Exception):
    """The API refused this worker's protocol version; do not retry."""

BACKOFF_INITIAL = 1.0
BACKOFF_CAP = 30.0
BACKOFF_JITTER = 0.25

# One bound for every session seed: large enough that torch.Generator accepts
# it comfortably, small enough that consecutive sessions never collide by luck.
# Mirrors the API's SESSION_SEED_BOUND (app/realtime.py), which fills the seed
# at session open; the two packages have no shared import, so the number is
# written twice with this comment binding them.
SEED_BOUND = 2**31 - 1


def frame_p95_payload(engine: Engine) -> dict[str, int]:
    """The live per-model frame p95s for a heartbeat; models with no
    measurement (never calibrated, or not yet enough observed frames) are
    omitted. Built from the engine's measured ids, not its residency: a model
    evicted from VRAM still holds a valid past measurement, and dropping it
    would make the advertised number flap with memory pressure."""
    return {
        model_id: p95
        for model_id in engine.p95_model_ids()
        if (p95 := engine.realtime_p95_ms(model_id)) is not None
    }


def batch_ms_payload(engine: Engine) -> dict[str, list[int]]:
    realtime_batch_ms = getattr(engine, "realtime_batch_ms", None)
    if realtime_batch_ms is None:
        return {}
    return {
        model_id: list(curve)
        for model_id in engine.p95_model_ids()
        if (curve := realtime_batch_ms(model_id))
    }


def default_steps(manifest: Manifest) -> object | None:
    """The manifest's declared default step count, or None if it declares none.

    A worker-supplied schema is not guaranteed to be the shape it should be,
    so every level is checked rather than assumed.
    """
    properties = manifest.parameters.get("properties")
    if not isinstance(properties, dict):
        return None
    steps = properties.get("steps")
    if not isinstance(steps, dict):
        return None
    return steps.get("default")


def planned_job_steps(
    manifest: Manifest, params: dict, *, input_image: bytes | None = None,
) -> int:
    """Step count the engine uses for progress callbacks on this job."""
    properties = manifest.parameters.get("properties")
    schema_steps = 2
    if isinstance(properties, dict):
        steps_schema = properties.get("steps")
        if isinstance(steps_schema, dict) and steps_schema.get("default") is not None:
            schema_steps = int(steps_schema["default"])
    if "upscale" in manifest.capabilities:
        return 1
    steps = max(1, int(params.get("steps", schema_steps)))
    if input_image is not None and "image_to_image" in manifest.capabilities:
        strength = min(max(float(params.get("strength", 0.75)), 0.05), 1.0)
        return max(1, int(steps * strength))
    return steps


def normalise_seed(value: object) -> int | None:
    """The seed a session's params must hold, normalised at the worker's
    boundary so everything downstream sees an integer.

    An integer is kept as-is. A float that is a whole number is kept as an
    integer: JSON Schema accepts 42.0 as an integer, so an older API that
    only validates shapes forwards it, and the engine's generator wants an
    int. A bool is refused even though it subclasses int, or `seed: true`
    would survive as a seed. Anything else (a fractional float, a string,
    null) is refused too: the caller draws a fresh seed. Mirrors the API's
    session_seed (app/realtime.py): the two packages have no shared import,
    so each boundary writes its own, with this comment pointing at the
    other the way SEED_BOUND and SESSION_SEED_BOUND already do.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def ensure_seed(params: dict) -> dict:
    """Return params with a session-stable seed, honoring an explicit one.

    A realtime session renders the same canvas the same way on every frame:
    a fresh latent per frame would re-roll the whole image when nothing
    changed (measured at 85.9 percent of pixels). An explicit seed from the
    client is kept as-is so a session can be reproduced exactly. The API
    fills the seed at session open (app/realtime.py, SESSION_SEED_BOUND is
    this module's SEED_BOUND), so this is the fallback for a params dict
    that arrives without one: an older API will not send it.
    """
    raw = params.get("seed")
    seed = normalise_seed(raw)
    if seed is None:
        seed = random.randrange(SEED_BOUND)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return params
    seeded = dict(params)
    seeded["seed"] = seed
    return seeded


def control_generation(control: dict) -> int | None:
    """Positive int generation, or None when the message is unfenced.

    Protocol 4 does not believe an open/update/close that omits the field.
    """
    raw = control.get("control_generation")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return None
    return raw


class LockedWebSocket:
    """Serialize ws.send calls; cheap insurance against concurrent writers."""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._lock = asyncio.Lock()

    async def send(self, data) -> None:
        async with self._lock:
            await self._ws.send(data)

    def __aiter__(self):
        return self._ws.__aiter__()

    def __getattr__(self, name):
        return getattr(self._ws, name)


_protocol6_identity: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "protocol6_identity", default=None,
)


def _report_hash(message: dict[str, Any]) -> str:
    source = {key: value for key, value in message.items() if key != "report_hash"}
    return hashlib.sha256(canonical_bytes(source)).hexdigest()


def _enrich_protocol6_message(message: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    kind = message.get("type")
    result = dict(message)
    common = (
        "worker_id",
        "incarnation",
        "region",
        "owner_epoch",
        "grant_nonce",
        "lease_id",
        "lease_expires_at",
    )
    if kind in {"session_ready", "session_refused", "session_closed"}:
        identity_fields = [
            *common,
            "session_id",
            "attempt_id",
            "control_generation",
        ]
        if kind == "session_ready":
            identity_fields.append("params_revision")
    elif kind in {"job_progress", "job_done", "job_failed", "job_cancelled"}:
        identity_fields = [*common, "job_id", "dispatch_sequence", "dispatch_token"]
    else:
        identity_fields = list(common)
    result.update({key: identity[key] for key in identity_fields if key in identity})
    if kind == "session_ready":
        result.setdefault("params_revision", identity.get("params_revision", 1))
    if kind == "session_refused":
        reason = result.pop("reason", "model_unavailable")
        result.setdefault(
            "code",
            {
                "not_resident": "model_unavailable",
                "invalid_params": "invalid_params",
            }.get(reason, "model_unavailable"),
        )
    if kind == "session_closed":
        result.update(
            {
                "range_id": identity["range_id"],
                "report_sequence": identity.setdefault("session_report_sequence", 1),
                "physical_complete": True,
            }
        )
    if kind in {"job_progress", "job_done", "job_failed", "job_cancelled"}:
        result.setdefault("dispatch_sequence", identity["dispatch_sequence"])
        result.setdefault("dispatch_token", identity["dispatch_token"])
    if kind == "job_failed":
        result.setdefault("failure_code", "generation_failed")
    if kind in {"job_done", "job_failed", "job_cancelled"}:
        result.update(
            {
                "range_id": identity["range_id"],
                "report_sequence": identity.setdefault("job_report_sequence", 1),
                "gpu_ms": result.get("gpu_ms", 0),
                "duration_ms": result.get("duration_ms", 0),
                "images": result.get("images", 1 if kind == "job_done" else 0),
                "physical_complete": True,
            }
        )
    if kind in {"session_closed", "job_done", "job_failed", "job_cancelled"}:
        result["report_hash"] = _report_hash(result)
    return result


class Protocol6WebSocket:
    """Encodes protocol 6 control messages and stamps reports from context identity."""

    def __init__(self, ws, worker_id: str, incarnation: str) -> None:
        self._ws = ws
        self._worker_id = worker_id
        self._incarnation = incarnation
        self._incarnation_uuid = uuid.UUID(incarnation)

    async def send(self, data: str | bytes | bytearray | dict[str, Any]) -> None:
        if isinstance(data, (bytes, bytearray)):
            await self._ws.send(data)
            return
        if isinstance(data, dict):
            message = data
        elif isinstance(data, str):
            message = json.loads(data)
        else:
            raise TypeError("protocol 6 send expects text, bytes, or a control object")
        identity = _protocol6_identity.get()
        if identity is not None:
            message = _enrich_protocol6_message(message, identity)
        validate_message(
            message,
            expected_worker_id=self._worker_id,
            expected_incarnation=self._incarnation_uuid,
        )
        encoded = preflight_worker_control(
            message,
            expected_worker_id=self._worker_id,
            expected_incarnation=self._incarnation_uuid,
        )
        await self._ws.send(encoded.decode("utf-8"))

    async def send_message(self, message: dict[str, Any]) -> None:
        await self.send(message)

    def __aiter__(self):
        return self._ws.__aiter__()

    def __getattr__(self, name):
        return getattr(self._ws, name)


def _command_identity(command: dict[str, Any]) -> dict[str, Any]:
    identity: dict[str, Any] = {
        key: command[key]
        for key in (
            "worker_id", "incarnation", "region", "lease_id", "owner_epoch",
            "grant_nonce", "lease_expires_at", "session_id", "attempt_id",
            "control_generation", "params_revision", "job_id", "dispatch_sequence",
            "dispatch_token",
        )
        if key in command
    }
    budget = command.get("work_budget")
    if isinstance(budget, dict):
        identity["range_id"] = budget["range_id"]
    return identity


def _command_ack_fields(command: dict[str, Any]) -> dict[str, Any]:
    ack: dict[str, Any] = {
        "type": "command_ack",
        "worker_id": command["worker_id"],
        "incarnation": command["incarnation"],
        "region": command["region"],
        "owner_epoch": command["owner_epoch"],
        "lease_id": command["lease_id"],
        "lease_expires_at": command["lease_expires_at"],
        "grant_nonce": command["grant_nonce"],
        "command_sequence": command["command_sequence"],
        "command_id": command["command_id"],
        "body_hash": command["body_hash"],
    }
    subjects = {
        "open_session": ("session_id", "attempt_id", "control_generation"),
        "update_session": ("session_id", "attempt_id", "control_generation"),
        "close_session": ("session_id", "attempt_id", "control_generation"),
        "dispatch_job": ("job_id", "dispatch_sequence"),
        "cancel_job": ("job_id", "dispatch_sequence"),
    }
    for field in subjects.get(command["type"], ()):
        ack[field] = command[field]
    return ack


class Protocol6CommandLedger:
    def __init__(self) -> None:
        self._next_sequence = 1
        self._applied_command_id: dict[int, str] = {}
        self._ack_text: dict[int, str] = {}

    def _encode_ack(self, ack: dict[str, Any], worker_id: str, incarnation: uuid.UUID) -> str:
        validate_message(ack, expected_worker_id=worker_id, expected_incarnation=incarnation)
        return preflight_worker_control(
            ack, expected_worker_id=worker_id, expected_incarnation=incarnation,
        ).decode("utf-8")

    async def handle(
        self,
        command: dict[str, Any],
        apply,
        *,
        worker_id: str,
        incarnation: uuid.UUID,
    ) -> str:
        sequence = command["command_sequence"]
        command_id = command["command_id"]
        if sequence in self._applied_command_id:
            if self._applied_command_id[sequence] == command_id:
                return self._ack_text[sequence]
            ack = _command_ack_fields(command)
            ack["status"] = "refused"
            ack["code"] = "sequence_gap"
            ack["expected_sequence"] = self._next_sequence
            return self._encode_ack(ack, worker_id, incarnation)
        if sequence > self._next_sequence:
            ack = _command_ack_fields(command)
            ack["status"] = "refused"
            ack["code"] = "sequence_gap"
            ack["expected_sequence"] = self._next_sequence
            return self._encode_ack(ack, worker_id, incarnation)
        status, code = await apply(command)
        ack = _command_ack_fields(command)
        ack["status"] = status
        if code is not None:
            ack["code"] = code
        text = self._encode_ack(ack, worker_id, incarnation)
        self._applied_command_id[sequence] = command_id
        self._ack_text[sequence] = text
        self._next_sequence = sequence + 1
        return text


def build_runtime(settings: Settings) -> tuple[list[Manifest], Engine]:
    """Built once per process: reconnects keep loaded pipelines warm."""
    if settings.models_dir:
        from worker.engine import DiffusersEngine

        # Only the real engine categorizes: the model download is a real-model
        # cost, so `make simulate` and the simulated tests stay on the stub.
        enable_categorizer()
        return load_manifests(settings.models_dir), DiffusersEngine(
            settings.device,
            memory_mode=settings.memory_mode,
            models_dir=settings.models_dir,
            torch_compile=settings.torch_compile,
            attention_backend=settings.attention_backend,
        )
    return [SIMULATED_MANIFEST], SimulatedEngine(settings.inference_seconds)


async def warmup_realtime(engine: Engine, manifests: list[Manifest],
                          configured_slots: int) -> None:
    """Load and time every remaining realtime model before hello.

    Reconnects reuse a warm engine, so calibration is a no-op once slots are set,
    unless the last pass failed, when the reconnect measures again.
    DiffusersEngine only: the simulated engine has nothing to time. The default
    is warmed first so the studio's preselected model is not the extra cold
    load; the rest of the realtime set is still calibrated so admission cost
    is each model's own p95 rather than the last write.
    """
    if configured_slots <= 0 or not hasattr(engine, "torch_compile"):
        return
    if (getattr(engine, "_calibrated_slots", None) is not None
            and not getattr(engine, "_calibration_failed", False)):
        return
    setattr(engine, "_calibration_failed", False)
    wire = engine.measured_manifests(manifests)
    live_ids = {
        item["id"] for item in wire if "realtime" in item.get("capabilities", [])
    }
    candidates = [manifest for manifest in manifests
                  if manifest.id in live_ids and not manifest.benchmark_only]
    if not candidates:
        return
    declared = [manifest for manifest in candidates if manifest.default]
    if len(declared) > 1:
        # The studio's picker takes the first default in ITS order, which is by
        # model id, while manifests arrive here in filename order. With one
        # default the two agree; with several they can disagree and the first
        # warm model is not the opened one. Say so rather than pick silently.
        logger.warning(
            "several realtime models declare default (%s); warming %s first, which the "
            "studio may not be the one it preselects",
            ", ".join(sorted(m.id for m in declared)), declared[0].id,
        )
    # Warm what the studio opens first: the manifest declaring `default` is
    # the one its picker preselects (fallbackModelId in studio.svelte.ts).
    # Then every other remaining realtime model, so hello's cost map is not
    # a single model's p95 applied to the rest (issue #285).
    ordered: list[Manifest] = []
    if declared:
        ordered.append(declared[0])
    seen = {manifest.id for manifest in ordered}
    for manifest in candidates:
        if manifest.id not in seen:
            ordered.append(manifest)
            seen.add(manifest.id)
    for manifest in ordered:
        slots = await engine.calibrate_realtime(manifest, configured_slots)
        logger.info("warmup realtime model=%s slots=%d", manifest.id, slots)


class JobRun:
    """A dispatched job and the flag that asks it to stop.

    Cancellation is cooperative and best effort: the API cancels the row
    before it asks, so a missed ask costs the fleet one wasted image and
    nothing else. The token is what makes an ask believable, because a stall
    requeue can dispatch the same job id to this worker twice and only the
    dispatch that is running may be stopped.
    """

    def __init__(self, job_id: str, token: object, identity: dict[str, Any] | None = None) -> None:
        self.job_id = job_id
        self.identity = identity or {}
        # Filtered the way run_job filters what it stamps: a dispatch that
        # carries no usable token cannot be cancelled at all, which is the
        # same answer an older API gets by never sending cancel_job.
        self.token = token if isinstance(token, str) and token else None
        self.stop = asyncio.Event()

    def stops(self, control: dict) -> bool:
        """True when a cancel_job names this dispatch. A token that is missing
        or belongs to another attempt is as stale here as it is at the API,
        and is ignored the same way."""
        return (control["job_id"] == self.job_id and self.token is not None
                and control.get("dispatch_token") == self.token)


class SessionRunner:
    """Holds at most one pending canvas frame; newer input overwrites older."""

    def __init__(self, session_id: uuid.UUID, ws, engine: Engine, manifest: Manifest,
                 params: dict, generation: int = 1):
        self._session_id = session_id
        self._ws = ws
        self._engine = engine
        self._manifest = manifest
        self._params = params
        self._generation = generation
        self._pending: tuple[int, bytes] | None = None
        # Highest input revision accepted, 0 before the first frame. The API
        # stamps a per-session revision on every canvas frame, so a replay or
        # an out-of-order one is dropped here rather than rendered.
        self._accepted_revision = 0
        self._arrived = asyncio.Event()
        self.dropped = 0
        self._frames = 0
        self._gpu_ms = 0
        # The WebP this runner last sent, so close_report can categorize what
        # the person actually saw; None while no frame has been delivered.
        self._last_frame: bytes | None = None
        self._started_at = time.monotonic()
        self._ready_sent = False
        self._ended = False
        self._cancel_requested = False
        self._p6_identity: dict[str, Any] | None = None
        # One holder for the session's prompt embeddings, passed to every
        # frame: the cache lives and dies with the runner, so an engine-held
        # cache would never have a release path to forget.
        self._prompt_cache = PromptCache()
        self._task = asyncio.create_task(self._run())

    def submit(self, revision: int, payload: bytes) -> None:
        """Accept one canvas frame; a revision already seen is not new input.

        dropped counts only what a newer frame overwrote: that is latest input
        wins, and a stale frame is a replay rather than congestion.
        """
        if revision <= self._accepted_revision:
            return
        self._accepted_revision = revision
        if self._pending is not None:
            self.dropped += 1
        self._pending = (revision, payload)
        self._arrived.set()

    def matches_generation(self, generation: int | None) -> bool:
        return generation == self._generation

    def update(self, params: dict) -> None:
        updated = self._manifest.with_defaults(params)
        seed = normalise_seed(updated.get("seed"))
        if seed is None:
            seed = self._params["seed"]
        updated["seed"] = seed
        self._params = updated

    def lifecycle(self, kind: str, **extra) -> dict:
        payload = {
            "type": kind,
            "session_id": str(self._session_id),
            "control_generation": self._generation,
            **extra,
        }
        return payload

    async def resend_ready(self) -> None:
        """Idempotent open: repeat the ready we already sent, if any."""
        if self._ready_sent and not self._ended:
            with suppress(websockets.WebSocketException):
                await self._ws.send(json.dumps(self.lifecycle("session_ready")))

    async def _ready(self) -> None:
        if self._ended:
            return
        self._ready_sent = True
        token = None
        if self._p6_identity is not None:
            token = _protocol6_identity.set(self._p6_identity)
        try:
            await self._ws.send(json.dumps(self.lifecycle("session_ready")))
        finally:
            if token is not None:
                _protocol6_identity.reset(token)

    async def _refuse(self, reason: str) -> None:
        if self._ended:
            return
        self._ended = True
        token = None
        if self._p6_identity is not None:
            token = _protocol6_identity.set(self._p6_identity)
        try:
            with suppress(websockets.WebSocketException):
                await self._ws.send(json.dumps(self.lifecycle("session_refused", reason=reason)))
        finally:
            if token is not None:
                _protocol6_identity.reset(token)

    async def _run(self) -> None:
        ws = self._ws
        engine = self._engine
        manifest = self._manifest
        # Residency is decided once, before any frame waits on a stale rung
        # answer. This cannot live in the open_session handler in the control
        # loop: that loop also reads every heartbeat and every frame from
        # every session, and awaiting a model load there would stall the
        # socket until the load finished. Ready and refused are sent here,
        # after the answer is known, so the API never marks a session live
        # that this runner cannot serve.
        try:
            resident = await engine.ensure_realtime_resident(manifest)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A load that fails for a reason the rung ladder does not cover,
            # missing weights or a bad import, is an attempt failure: the
            # API reassigns or closes 4003. Dying quietly into the frame
            # loop is the silent blank canvas issue #270 exists to close.
            logger.exception(
                "session %s could not make model %s resident",
                self._session_id, manifest.id,
            )
            resident = False
        if not resident:
            logger.warning(
                "session %s cannot render: model %s is not fully resident",
                self._session_id, manifest.id,
            )
            await self._refuse("not_resident")
            return
        try:
            await self._ready()
        except websockets.WebSocketException:
            logger.warning("session %s lost the connection before session_ready",
                           self._session_id)
            return
        steps_default = default_steps(manifest)
        residency_failures = 0
        while True:
            await self._arrived.wait()
            self._arrived.clear()
            pending, self._pending = self._pending, None
            if pending is None:  # unreachable today; narrows the Optional for mypy
                continue
            revision, payload = pending
            try:
                # The params are read per frame, so an update_session lands on
                # the next frame while one in flight finishes on the old dict.
                generated = await engine.frame(
                    manifest, self._params, payload, prompt_cache=self._prompt_cache,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # A one-off bad frame is logged and the loop continues. The
                # same residency refusal repeating is an attempt failure:
                # stay here and the browser looks Active while nothing
                # renders (issue #270). Match the engine's type, not its
                # message: rewording the raise must not disable refusal.
                logger.exception("session %s dropped a frame on an inference error",
                                 self._session_id)
                if isinstance(error, NotResidentError):
                    residency_failures += 1
                    if residency_failures >= 2:
                        await self._refuse("not_resident")
                        return
                else:
                    residency_failures = 0
                continue
            residency_failures = 0
            self._frames += 1
            self._gpu_ms += generated.gpu_ms
            # The same quantity calibration measures: worker-side inference
            # time per frame, and the number the 500 ms bar is defined
            # against. Real frames supersede the calibration estimate, but
            # only when the session rendered at the manifest's declared
            # defaults: the advertised number claims the model's cost at
            # defaults, so a session at other settings (steps is the
            # cost-determining parameter; width and height are fixed enums)
            # measures something else and must not overwrite it. The default
            # is fixed for the session; the params are not, so an update to
            # steps stops the observing from the next frame on.
            if steps_default is not None and self._params.get("steps") == steps_default:
                engine.observe_frame_ms(manifest.id, generated.gpu_ms)
            if self._ended:
                return
            try:
                await ws.send(
                    bytes([GENERATED_FRAME]) + self._session_id.bytes
                    + revision.to_bytes(4, "big") + generated.data)
            except websockets.WebSocketException:
                logger.warning("session %s lost the connection while sending a frame",
                               self._session_id)
                return
            self._last_frame = generated.data

    def close(self) -> None:
        self._ended = True
        # Shutdown also visits runners retired during a replacement, so a
        # repeated close must not deliver a second cancellation.
        if not self._cancel_requested:
            self._cancel_requested = True
            self._task.cancel()

    async def wait(self) -> None:
        with suppress(asyncio.CancelledError):
            await self._task

    def add_done_callback(self, callback) -> None:
        self._task.add_done_callback(lambda _: callback(self))

    async def close_report(self) -> dict:
        # Categorization is tens to hundreds of ms of CPU beside the loop that
        # relays frames and controls, so it runs in a worker thread.
        category, score = await asyncio.to_thread(categorize_output, self._last_frame)
        report = {
            "type": "session_closed",
            "session_id": str(self._session_id),
            "frames": self._frames,
            "gpu_ms": self._gpu_ms,
            "duration_ms": int((time.monotonic() - self._started_at) * 1000),
            "category": category,
            "control_generation": self._generation,
        }
        if score is not None:
            report["category_score"] = score
        return report


class SessionManager:
    """Own realtime runner lifecycle and generation fencing for one connection."""

    def __init__(self, ws, engine: Engine, manifests: list[Manifest]):
        self._ws = ws
        self._engine = engine
        self._by_id = {manifest.id: manifest for manifest in manifests}
        self._runners: dict[uuid.UUID, SessionRunner] = {}
        self._highest_generation: dict[uuid.UUID, int] = {}
        self._retired: set[SessionRunner] = set()

    @property
    def active_count(self) -> int:
        return len(self._runners)

    def _forget_retired(self, runner: SessionRunner) -> None:
        self._retired.discard(runner)

    def _retire(self, runner: SessionRunner) -> None:
        self._retired.add(runner)
        runner.add_done_callback(self._forget_retired)
        runner.close()

    async def open(self, control: dict, *, p6_identity: dict[str, Any] | None = None) -> None:
        session_id = uuid.UUID(control["session_id"])
        generation = control_generation(control)
        if generation is None:
            logger.warning(
                "open_session for %s omitted control_generation; ignored",
                control.get("session_id"),
            )
            return
        highest = self._highest_generation.get(session_id, 0)
        if generation < highest:
            return
        if generation == highest:
            runner = self._runners.get(session_id)
            if runner is not None:
                await runner.resend_ready()
            return
        old = self._runners.pop(session_id, None)
        if old is not None:
            self._retire(old)
        self._highest_generation[session_id] = generation
        manifest = self._by_id[control["model_id"]]
        runner = SessionRunner(
            session_id, self._ws, self._engine, manifest,
            ensure_seed(manifest.with_defaults(control.get("params") or {})),
            generation)
        runner._p6_identity = p6_identity
        self._runners[session_id] = runner

    async def update(self, control: dict) -> None:
        generation = control_generation(control)
        runner = self._runners.get(uuid.UUID(control["session_id"]))
        if runner is None or not runner.matches_generation(generation):
            return
        runner.update(control["params"])

    async def close(self, control: dict) -> None:
        session_id = uuid.UUID(control["session_id"])
        generation = control_generation(control)
        runner = self._runners.get(session_id)
        if runner is None:
            if generation is not None:
                self._highest_generation[session_id] = max(
                    self._highest_generation.get(session_id, 0), generation)
            return
        if not runner.matches_generation(generation):
            return
        self._runners.pop(session_id, None)
        self._retire(runner)
        token = None
        if runner._p6_identity is not None:
            token = _protocol6_identity.set(runner._p6_identity)
        try:
            await self._ws.send(json.dumps(await runner.close_report()))
        finally:
            if token is not None:
                _protocol6_identity.reset(token)

    def submit(self, session_id: uuid.UUID, revision: int, payload: bytes) -> None:
        runner = self._runners.get(session_id)
        if runner is not None:
            runner.submit(revision, payload)

    async def shutdown(self) -> None:
        runners = list(self._runners.values()) + list(self._retired)
        self._runners.clear()
        self._highest_generation.clear()
        for runner in runners:
            runner.close()
        await asyncio.gather(*(runner.wait() for runner in runners),
                             return_exceptions=True)
        self._retired.clear()


async def run_job(ws, engine: Engine, manifest: Manifest, control: dict,
                  stop: asyncio.Event | None = None) -> None:
    """One queued job: generate, upload to the given target, report the result.
    Failures are reported, never raised: the connection outlives the job.

    Setting `stop` asks the job to end where it can, and it then reports
    job_cancelled with the GPU time it spent instead of uploading an image
    the API would discard.
    """
    job_id = control["job_id"]
    stop = stop or asyncio.Event()
    # Echoed on every message about this job so the API can tell this dispatch
    # from an earlier attempt of the same job that reached the same worker
    # (docs/connection-handling.md). An older API sends none, so send none.
    token = control.get("dispatch_token")
    stamp = {"dispatch_token": token} if isinstance(token, str) and token else {}
    job_started = time.monotonic()
    progress_tasks: list[asyncio.Task[None]] = []
    last_fraction = 0.0
    protocol6 = isinstance(ws, Protocol6WebSocket)
    job_phase = "load"
    job_step = 0
    job_steps = 1

    def progress(fraction: float) -> None:
        nonlocal last_fraction, job_phase, job_step, job_steps
        last_fraction = fraction
        if protocol6:
            if fraction > 0:
                job_phase = "inference"
                if fraction < 1.0 and job_step > 0:
                    job_steps = max(job_steps, round(job_step / fraction))
                job_step = min(job_steps, max(1, int(round(fraction * job_steps))))
                if fraction >= 1.0:
                    job_step = job_steps
        progress_tasks.append(asyncio.create_task(send_progress(fraction)))

    async def send_progress(fraction: float) -> None:
        with suppress(websockets.WebSocketException):
            payload: dict[str, Any] = {
                "type": "job_progress",
                "job_id": job_id,
                "progress": round(fraction, 4),
                **stamp,
            }
            if protocol6:
                payload["phase"] = job_phase
                payload["step"] = job_step
                payload["steps"] = job_steps
            await ws.send(json.dumps(payload))

    async def progress_keepalive() -> None:
        while True:
            try:
                await asyncio.sleep(PROGRESS_KEEPALIVE_SECONDS)
                await send_progress(last_fraction)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("job %s progress keepalive failed", job_id)

    async def report_cancelled(gpu_ms: int) -> None:
        # The row is already cancelled at the API, so this says only what the
        # GPU cost before the job stopped; that time is charged because it was
        # spent. Nothing is uploaded: whatever this run produced is discarded
        # there anyway.
        logger.info("job %s cancelled after %d gpu_ms", job_id, gpu_ms)
        with suppress(websockets.WebSocketException):
            await ws.send(json.dumps({"type": "job_cancelled", "job_id": job_id,
                                      "gpu_ms": gpu_ms, **stamp}))

    keepalive_task = asyncio.create_task(progress_keepalive())
    input_fetch_ms = 0
    postprocess_ms = 0
    try:
        params = manifest.with_defaults(control.get("params") or {})
        if protocol6:
            job_steps = planned_job_steps(manifest, params, input_image=None)
        upload = control["upload"]
        thumb_upload = control.get("thumb_upload")
        has_thumbnail = False
        input_image = None
        input_spec = control.get("input")
        if input_spec and input_spec.get("url"):
            # Short-lived client: inference can run for minutes and must not
            # hold a connection pool open.
            fetch_start = time.monotonic()
            async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as client:
                response = await client.get(input_spec["url"])
                response.raise_for_status()
                input_image = response.content
            input_fetch_ms = int((time.monotonic() - fetch_start) * 1000)
            if protocol6:
                job_steps = planned_job_steps(manifest, params, input_image=input_image)
        if stop.is_set():
            # Asked to stop while the input was still downloading: the GPU
            # never ran, so there is no time to charge and nothing to upload.
            await report_cancelled(0)
            return
        gpu_started = time.monotonic()
        try:
            result = await engine.generate(manifest, params, progress,
                                            input_image=input_image,
                                            cancelled=stop.is_set)
        except Cancelled:
            # The engine stopped between steps or between tiles, so it has no
            # image to hand back; the GPU still ran until it stopped.
            await report_cancelled(int((time.monotonic() - gpu_started) * 1000))
            return
        if stop.is_set():
            # The image is finished and already worthless: the API discards an
            # upload for a cancelled job, so sending it is pure waste.
            await report_cancelled(result.gpu_ms)
            return
        post_start = time.monotonic()
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as client:
            response = await client.put(upload["url"], content=result.data,
                                        headers=upload.get("headers") or {})
            response.raise_for_status()
            if thumb_upload:
                # Best effort: the full result is already stored, and the API
                # only records a thumbnail when job_done reports one.
                try:
                    thumb_data = make_thumbnail_webp(result.data)
                    response = await client.put(thumb_upload["url"], content=thumb_data,
                                                headers=thumb_upload.get("headers") or {})
                    response.raise_for_status()
                    has_thumbnail = True
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("job %s thumbnail failed; delivering without one",
                                     job_id)
        postprocess_ms = int((time.monotonic() - post_start) * 1000)
        done_msg: dict = {"type": "job_done", "job_id": job_id, **stamp,
                          "gpu_ms": result.gpu_ms,
                          "input_fetch_ms": input_fetch_ms,
                          "load_ms": result.load_ms,
                          "postprocess_ms": postprocess_ms,
                          "width": result.width, "height": result.height}
        # Off the event loop like the close report: categorization is CPU work.
        category, score = await asyncio.to_thread(categorize_output, result.data)
        done_msg["category"] = category
        if score is not None:
            done_msg["category_score"] = score
        if has_thumbnail:
            done_msg["has_thumbnail"] = True
        # Stamped last so it covers every step the user waits through,
        # including categorization.
        done_msg["duration_ms"] = int((time.monotonic() - job_started) * 1000)
        await ws.send(json.dumps(done_msg))
        logger.info("job %s done in %d gpu_ms", job_id, result.gpu_ms)
    except asyncio.CancelledError:
        raise
    except websockets.WebSocketException:
        logger.warning("job %s finished but the connection is gone; the API requeues it", job_id)
    except Exception as error:
        logger.exception("job %s failed", job_id)
        with suppress(websockets.WebSocketException):
            await ws.send(json.dumps({"type": "job_failed", "job_id": job_id,
                                      "reason": str(error), **stamp}))
    finally:
        keepalive_task.cancel()
        with suppress(asyncio.CancelledError):
            await keepalive_task
        if progress_tasks:
            await asyncio.gather(*progress_tasks, return_exceptions=True)


async def _gpu_load(ws, engine: Engine, by_id: dict[str, Manifest], control: dict) -> None:
    request_id = control["request_id"]
    try:
        manifest = by_id[control["model_id"]]
        load_ms = await engine.load_model(manifest)
        await ws.send(json.dumps({
            "type": "model_loaded",
            "request_id": request_id,
            "model_id": manifest.id,
            "load_ms": load_ms,
            "loaded_models": engine.loaded_models(),
        }))
    except Exception as error:
        logger.exception("load_model %s failed", control.get("model_id"))
        await ws.send(json.dumps({
            "type": "gpu_error",
            "request_id": request_id,
            "reason": str(error),
        }))


async def _gpu_unload(ws, engine: Engine, control: dict) -> None:
    request_id = control["request_id"]
    try:
        model_id = control.get("model_id")
        if model_id:
            await engine.unload_model(model_id)
        else:
            await engine.unload_all()
        await ws.send(json.dumps({
            "type": "model_unloaded",
            "request_id": request_id,
            "loaded_models": engine.loaded_models(),
        }))
    except Exception as error:
        logger.exception("unload_model failed")
        await ws.send(json.dumps({
            "type": "gpu_error",
            "request_id": request_id,
            "reason": str(error),
        }))


async def _serve_protocol5(
    ws,
    settings: Settings,
    manifests: list[Manifest],
    engine: Engine,
) -> None:
    ws = LockedWebSocket(ws)
    by_id = {manifest.id: manifest for manifest in manifests}
    sessions = SessionManager(ws, engine, manifests)
    jobs: dict[asyncio.Task, JobRun] = {}

    def forget(task: asyncio.Task) -> None:
        jobs.pop(task, None)

    async def heartbeats() -> None:
        while True:
            await asyncio.sleep(settings.heartbeat_seconds)
            try:
                gpu = await asyncio.to_thread(sample_gpu, settings.device)
                await ws.send(json.dumps({
                    "type": "heartbeat",
                    "slots_in_use": sessions.active_count,
                    "loaded_models": engine.loaded_models(),
                    "gpu": gpu,
                    "frame_p95_ms": frame_p95_payload(engine),
                }))
            except asyncio.CancelledError:
                raise
            except websockets.ConnectionClosed:
                raise
            except Exception:
                if getattr(ws, "close_code", None) is not None:
                    return
                logger.exception("heartbeat failed; the next one retries")

    heartbeat_task = asyncio.create_task(heartbeats())
    try:
        async for message in ws:
            try:
                if isinstance(message, bytes):
                    if len(message) < FRAME_HEADER_BYTES:
                        raise ValueError("binary frame shorter than the header")
                    session_id = uuid.UUID(bytes=message[1:LEGACY_FRAME_HEADER_BYTES])
                    revision = int.from_bytes(
                        message[LEGACY_FRAME_HEADER_BYTES:FRAME_HEADER_BYTES], "big")
                    sessions.submit(session_id, revision, message[FRAME_HEADER_BYTES:])
                else:
                    control = json.loads(message)
                    if control["type"] == "open_session":
                        await sessions.open(control)
                    elif control["type"] == "update_session":
                        await sessions.update(control)
                    elif control["type"] == "close_session":
                        await sessions.close(control)
                    elif control["type"] == "dispatch_job":
                        job = JobRun(control["job_id"], control.get("dispatch_token"))
                        task = asyncio.create_task(run_job(
                            ws, engine, by_id[control["model_id"]], control,
                            job.stop))
                        jobs[task] = job
                        task.add_done_callback(forget)
                    elif control["type"] == "cancel_job":
                        for job in jobs.values():
                            if job.stops(control):
                                job.stop.set()
                    elif control["type"] == "gpu_status":
                        gpu = await asyncio.to_thread(sample_gpu, settings.device)
                        await ws.send(json.dumps({
                            "type": "gpu_status",
                            "request_id": control["request_id"],
                            "loaded_models": engine.loaded_models(),
                            "gpu": gpu,
                        }))
                    elif control["type"] == "load_model":
                        await _gpu_load(ws, engine, by_id, control)
                    elif control["type"] == "unload_model":
                        await _gpu_unload(ws, engine, control)
            except (json.JSONDecodeError, KeyError, ValueError, TypeError) as error:
                logger.warning("protocol violation from the API (%s), closing", error)
                await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
                return
    finally:
        heartbeat_task.cancel()
        for task in jobs:
            task.cancel()
        await asyncio.gather(heartbeat_task, *jobs, sessions.shutdown(),
                             return_exceptions=True)


async def _serve_protocol6(
    ws,
    settings: Settings,
    manifests: list[Manifest],
    engine: Engine,
    registered: dict[str, Any],
    hello_grant_nonce: str,
) -> None:
    worker_id = settings.worker_id
    incarnation = registered["incarnation"]
    region = registered["region"]
    incarnation_uuid = uuid.UUID(incarnation)
    transport = LockedWebSocket(ws)
    output = Protocol6WebSocket(transport, worker_id, incarnation)
    by_id = {manifest.id: manifest for manifest in manifests}
    sessions = SessionManager(output, engine, manifests)
    jobs: dict[asyncio.Task, JobRun] = {}
    commands = Protocol6CommandLedger()
    grant_remaining_ms = 0
    grant_ready = False

    def forget(task: asyncio.Task) -> None:
        jobs.pop(task, None)

    async def send_grant_request() -> None:
        message = {
            "type": "grant_request",
            "worker_id": worker_id,
            "incarnation": incarnation,
            "region": region,
            "grant_nonce": str(uuid.uuid4()),
        }
        await output.send_message(message)

    async def grant_renewal() -> None:
        nonlocal grant_remaining_ms, grant_ready
        while True:
            if not grant_ready:
                await asyncio.sleep(5)
                await send_grant_request()
                continue
            delay = max(5.0, grant_remaining_ms / 2000.0)
            await asyncio.sleep(delay)
            await send_grant_request()

    async def heartbeats() -> None:
        while True:
            await asyncio.sleep(settings.heartbeat_seconds)
            gpu = await asyncio.to_thread(sample_gpu, settings.device)
            message = {
                "type": "heartbeat",
                "worker_id": worker_id,
                "incarnation": incarnation,
                "region": region,
                "slots_in_use": sessions.active_count,
                "loaded_models": engine.loaded_models(),
                "gpu": gpu,
                "frame_p95_ms": frame_p95_payload(engine),
            }
            await output.send_message(message)

    async def apply_command(command: dict[str, Any]) -> tuple[str, str | None]:
        kind = command["type"]
        if kind == "open_session":
            identity = _command_identity(command)
            await sessions.open(command, p6_identity=identity)
            return "accepted", None
        if kind == "update_session":
            await sessions.update(command)
            runner = sessions._runners.get(uuid.UUID(command["session_id"]))
            if runner is not None and runner._p6_identity is not None:
                identity = dict(runner._p6_identity)
                identity.update(_command_identity(command))
                runner._p6_identity = identity
            return "accepted", None
        if kind == "close_session":
            await sessions.close(command)
            return "accepted", None
        if kind == "dispatch_job":
            identity = _command_identity(command)
            job = JobRun(command["job_id"], command.get("dispatch_token"), identity)

            async def run_dispatched_job() -> None:
                token = _protocol6_identity.set(identity)
                try:
                    await run_job(
                        output, engine, by_id[command["model_id"]], command, job.stop)
                finally:
                    _protocol6_identity.reset(token)

            task = asyncio.create_task(run_dispatched_job())
            jobs[task] = job
            task.add_done_callback(forget)
            return "accepted", None
        if kind == "cancel_job":
            for job in jobs.values():
                if job.stops(command):
                    job.stop.set()
            return "accepted", None
        return "refused", "unsupported_control"

    grant_task = asyncio.create_task(grant_renewal())
    heartbeat_task = asyncio.create_task(heartbeats())
    await send_grant_request()
    try:
        async for message in transport:
            try:
                if isinstance(message, bytes):
                    if len(message) < FRAME_HEADER_BYTES:
                        raise ValueError("binary frame shorter than the header")
                    session_id = uuid.UUID(bytes=message[1:LEGACY_FRAME_HEADER_BYTES])
                    revision = int.from_bytes(
                        message[LEGACY_FRAME_HEADER_BYTES:FRAME_HEADER_BYTES], "big")
                    sessions.submit(session_id, revision, message[FRAME_HEADER_BYTES:])
                    continue
                control = decode_control(message)
                validate_message(
                    control,
                    expected_worker_id=worker_id,
                    expected_incarnation=incarnation_uuid,
                )
                if control["type"] == "work_grant":
                    grant_remaining_ms = control["remaining_ms"]
                    grant_ready = control["ready"]
                    continue
                if control["type"] == "checkpoint_ack":
                    if control.get("status") == "refused":
                        logger.warning("checkpoint_ack refused (%s)", control.get("code"))
                    continue
                if control["type"] in {
                    "open_session", "update_session", "close_session",
                    "dispatch_job", "cancel_job",
                }:
                    ack_text = await commands.handle(
                        control, apply_command,
                        worker_id=worker_id, incarnation=incarnation_uuid,
                    )
                    await transport.send(ack_text)
                    continue
            except (Protocol6Error, json.JSONDecodeError, KeyError, ValueError, TypeError) as error:
                logger.warning("protocol violation from the API (%s), closing", error)
                await transport.close(code=CLOSE_PROTOCOL_VIOLATION)
                return
    finally:
        grant_task.cancel()
        heartbeat_task.cancel()
        for task in jobs:
            task.cancel()
        await asyncio.gather(grant_task, heartbeat_task, *jobs, sessions.shutdown(),
                             return_exceptions=True)


async def serve_connection(ws, settings: Settings, manifests: list[Manifest],
                           engine: Engine) -> None:
    await warmup_realtime(engine, manifests, settings.realtime_slots)
    wire_manifests = engine.measured_manifests(manifests)
    p95_map: dict[str, int] = {}
    for wire in wire_manifests:
        p95 = engine.realtime_p95_ms(wire["id"])
        if p95 is not None:
            wire["realtime_p95_ms"] = p95
            p95_map[wire["id"]] = p95
    p95_map.update(frame_p95_payload(engine))
    incarnation = str(uuid.uuid4())
    hello_grant_nonce = str(uuid.uuid4())
    hello = {
        "type": "hello",
        "protocol_version": PROTOCOL_VERSION,
        "compatible_versions": [6, 5],
        "capabilities": list(PROTOCOL6_CAPABILITIES),
        "incarnation": incarnation,
        "grant_nonce": hello_grant_nonce,
        "worker_id": settings.worker_id,
        "models": wire_manifests,
        "realtime_slots": engine.effective_realtime_slots(wire_manifests,
                                                           settings.realtime_slots),
        "device": settings.device,
        "memory_mode": settings.memory_mode,
    }
    if p95_map:
        hello["realtime_p95_ms"] = p95_map
        batch_map = batch_ms_payload(engine)
        if batch_map:
            hello["realtime_batch_ms"] = batch_map
    await ws.send(json.dumps(hello))
    try:
        raw_response = await ws.recv()
        response = json.loads(raw_response)
        reply_type = response["type"]
    except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError) as error:
        logger.warning("malformed registration reply (%s), closing to reconnect", error)
        await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
        return
    if reply_type == "rejected" and response.get("reason") == "recovery_unavailable":
        # The API's durable authority is briefly missing, not this worker's
        # version: reconnect with backoff instead of exiting.
        logger.warning("registration deferred (recovery_unavailable), reconnecting")
        await ws.close()
        return
    if reply_type == "rejected":
        raise RegistrationRejected(
            f"{response.get('reason', 'rejected')}; "
            f"minimum supported version {response.get('min_supported_version')}"
        )
    if reply_type != "registered":
        raise RegistrationRejected(f"unexpected registration reply: {response}")
    protocol_version = response.get("protocol_version", 5)
    if protocol_version == 6:
        try:
            registered = decode_control(raw_response)
            validate_message(
                registered,
                expected_worker_id=settings.worker_id,
                expected_incarnation=uuid.UUID(incarnation),
            )
        except (Protocol6Error, ValueError) as error:
            logger.warning("malformed protocol 6 registration (%s), closing", error)
            await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
            return
        if registered.get("ready") is not False or registered.get("remaining_ms") != 0:
            logger.warning("protocol 6 registration attempted to grant capacity")
            await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
            return
        if registered["grant_nonce"] != hello_grant_nonce:
            logger.warning("protocol 6 registration changed its hello nonce")
            await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
            return
        logger.info("registered as %s (protocol 6)", settings.worker_id)
        await _serve_protocol6(
            ws, settings, manifests, engine, registered, hello_grant_nonce,
        )
        return
    if protocol_version != 5:
        logger.warning("registration did not select a supported worker protocol")
        await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
        return
    logger.info("registered as %s", settings.worker_id)
    await _serve_protocol5(ws, settings, manifests, engine)


async def run() -> None:
    settings = get_settings()
    manifests, engine = build_runtime(settings)
    delay = BACKOFF_INITIAL
    try:
        while True:
            try:
                async with websockets.connect(
                    settings.api_url,
                    # Lowercase: header names are case-insensitive, but not every
                    # ASGI stack normalises them before the application looks.
                    additional_headers={"x-fleet-token": settings.fleet_token},
                    max_size=FLEET_MAX_MESSAGE_BYTES,
                ) as ws:
                    delay = BACKOFF_INITIAL
                    await serve_connection(ws, settings, manifests, engine)
            except RegistrationRejected as error:
                logger.error("registration rejected (%s); update this worker, not retrying", error)
                return
            except (OSError, websockets.WebSocketException) as error:
                logger.warning("connection lost (%s), retrying in %.0fs", error, delay)
            await asyncio.sleep(delay * (1 + random.random() * BACKOFF_JITTER))
            delay = min(delay * 2, BACKOFF_CAP)
    finally:
        # build_runtime is called once, so the engine outlives every reconnect:
        # it is stopped when the process is, never inside the retry loop.
        await engine.close()
