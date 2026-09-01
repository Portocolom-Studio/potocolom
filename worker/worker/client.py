"""The worker's side of the fleet connection, per docs/connection-handling.md.

Real inference lives behind the Engine seam (worker/engine.py): diffusers
when a models directory is configured, a simulated engine otherwise.
Everything else (dial out, backoff, registration, heartbeats, latest input
wins, job execution) is identical in both cases.
"""

import asyncio
import json
import logging
import random
import time
import uuid
from contextlib import suppress

import httpx
import websockets

from worker.engine import (
    Cancelled,
    Engine,
    NotResidentError,
    PromptCache,
    SimulatedEngine,
    make_thumbnail_webp,
)
from worker.categorize import categorize_output
from worker.manifests import SIMULATED_MANIFEST, Manifest, load_manifests
from worker.gpu_metrics import sample_gpu
from worker.settings import Settings, get_settings

logger = logging.getLogger("potocolom.worker")

# Wire constants; keep in sync with backend/app/realtime.py.
PROTOCOL_VERSION = 4
GENERATED_FRAME = 0x02
FRAME_HEADER_BYTES = 17
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


def build_runtime(settings: Settings) -> tuple[list[Manifest], Engine]:
    """Built once per process: reconnects keep loaded pipelines warm."""
    if settings.models_dir:
        from worker.engine import DiffusersEngine

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

    Reconnects reuse a warm engine, so calibration is a no-op once slots are set.
    DiffusersEngine only: the simulated engine has nothing to time. The default
    is warmed first so the studio's preselected model is not the extra cold
    load; the rest of the realtime set is still calibrated so admission cost
    is each model's own p95 rather than the last write.
    """
    if configured_slots <= 0 or not hasattr(engine, "torch_compile"):
        return
    if getattr(engine, "_calibrated_slots", None) is not None:
        return
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

    def __init__(self, job_id: str, token: object) -> None:
        self.job_id = job_id
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
        self.session_id = session_id
        self.manifest = manifest
        self.params = params
        self.generation = generation
        self.pending: bytes | None = None
        self.arrived = asyncio.Event()
        self.dropped = 0
        self.frames = 0
        self.gpu_ms = 0
        self.started_at = time.monotonic()
        self._ready_sent = False
        self._ended = False
        # One holder for the session's prompt embeddings, passed to every
        # frame: the cache lives and dies with the runner, so an engine-held
        # cache would never have a release path to forget.
        self.prompt_cache = PromptCache()
        self._task = asyncio.create_task(self._run(ws, engine, manifest))

    def submit(self, payload: bytes) -> None:
        if self.pending is not None:
            self.dropped += 1
        self.pending = payload
        self.arrived.set()

    def lifecycle(self, kind: str, **extra) -> dict:
        payload = {
            "type": kind,
            "session_id": str(self.session_id),
            "control_generation": self.generation,
            **extra,
        }
        return payload

    async def resend_ready(self, ws) -> None:
        """Idempotent open: repeat the ready we already sent, if any."""
        if self._ready_sent and not self._ended:
            with suppress(websockets.WebSocketException):
                await ws.send(json.dumps(self.lifecycle("session_ready")))

    async def _ready(self, ws) -> None:
        self._ready_sent = True
        await ws.send(json.dumps(self.lifecycle("session_ready")))

    async def _refuse(self, ws, reason: str) -> None:
        if self._ended:
            return
        self._ended = True
        with suppress(websockets.WebSocketException):
            await ws.send(json.dumps(self.lifecycle("session_refused", reason=reason)))

    async def _run(self, ws, engine: Engine, manifest: Manifest) -> None:
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
                self.session_id, manifest.id,
            )
            resident = False
        if not resident:
            logger.warning(
                "session %s cannot render: model %s is not fully resident",
                self.session_id, manifest.id,
            )
            await self._refuse(ws, "not_resident")
            return
        try:
            await self._ready(ws)
        except websockets.WebSocketException:
            logger.warning("session %s lost the connection before session_ready",
                           self.session_id)
            return
        steps_default = default_steps(manifest)
        residency_failures = 0
        while True:
            await self.arrived.wait()
            self.arrived.clear()
            payload, self.pending = self.pending, None
            if payload is None:  # unreachable today; narrows the Optional for mypy
                continue
            try:
                # self.params is read per frame, so an update_session lands on
                # the next frame while one in flight finishes on the old dict.
                generated = await engine.frame(
                    manifest, self.params, payload, prompt_cache=self.prompt_cache,
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
                                 self.session_id)
                if isinstance(error, NotResidentError):
                    residency_failures += 1
                    if residency_failures >= 2:
                        await self._refuse(ws, "not_resident")
                        return
                else:
                    residency_failures = 0
                continue
            residency_failures = 0
            self.frames += 1
            self.gpu_ms += generated.gpu_ms
            # The same quantity calibration measures: worker-side inference
            # time per frame, and the number the 500 ms bar is defined
            # against. Real frames supersede the calibration estimate, but
            # only when the session rendered at the manifest's declared
            # defaults: the advertised number claims the model's cost at
            # defaults, so a session at other settings (steps is the
            # cost-determining parameter; width and height are fixed enums)
            # measures something else and must not overwrite it. The default
            # is fixed for the session; self.params is not, so an update to
            # steps stops the observing from the next frame on.
            if steps_default is not None and self.params.get("steps") == steps_default:
                engine.observe_frame_ms(manifest.id, generated.gpu_ms)
            try:
                await ws.send(
                    bytes([GENERATED_FRAME]) + self.session_id.bytes + generated.data)
            except websockets.WebSocketException:
                logger.warning("session %s lost the connection while sending a frame",
                               self.session_id)
                return

    def close(self) -> None:
        self._ended = True
        self._task.cancel()


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

    def progress(fraction: float) -> None:
        nonlocal last_fraction
        last_fraction = fraction
        progress_tasks.append(asyncio.create_task(send_progress(fraction)))

    async def send_progress(fraction: float) -> None:
        with suppress(websockets.WebSocketException):
            await ws.send(json.dumps({"type": "job_progress", "job_id": job_id,
                                      "progress": round(fraction, 4), **stamp}))

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
        category, score = categorize_output(result.data)
        done_msg["category"] = category
        if score is not None:
            done_msg["category_score"] = score
        if has_thumbnail:
            done_msg["has_thumbnail"] = True
        # Stamped last so it covers every step the user waits through, including
        # categorization once that stops being a stub.
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


async def serve_connection(ws, settings: Settings, manifests: list[Manifest],
                           engine: Engine) -> None:
    await warmup_realtime(engine, manifests, settings.realtime_slots)
    wire_manifests = engine.measured_manifests(manifests)
    # A measurement from this worker on this card, not a property of the
    # model: attach it at the wire edge so the API can advertise what a real
    # frame costs without Manifest.wire() growing a field of its own.
    p95_map: dict[str, int] = {}
    for wire in wire_manifests:
        p95 = engine.realtime_p95_ms(wire["id"])
        if p95 is not None:
            wire["realtime_p95_ms"] = p95
            p95_map[wire["id"]] = p95
    p95_map.update(frame_p95_payload(engine))
    hello = {
        "type": "hello",
        "protocol_version": PROTOCOL_VERSION,
        "worker_id": settings.worker_id,
        "models": wire_manifests,
        "realtime_slots": engine.effective_realtime_slots(wire_manifests,
                                                           settings.realtime_slots),
        "device": settings.device,
        "memory_mode": settings.memory_mode,
    }
    # Optional top-level map. An N-1 worker omits it and an older API keeps
    # today's integer pool. Per-manifest realtime_p95_ms is not this map:
    # current workers already send that field.
    if p95_map:
        hello["realtime_p95_ms"] = p95_map
        batch_map = batch_ms_payload(engine)
        if batch_map:
            hello["realtime_batch_ms"] = batch_map
    await ws.send(json.dumps(hello))
    try:
        response = json.loads(await ws.recv())
        reply_type = response["type"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        logger.warning("malformed registration reply (%s), closing to reconnect", error)
        await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
        return
    if reply_type == "rejected":
        raise RegistrationRejected(
            f"{response.get('reason', 'rejected')}; "
            f"minimum supported version {response.get('min_supported_version')}"
        )
    if reply_type != "registered":
        raise RegistrationRejected(f"unexpected registration reply: {response}")
    logger.info("registered as %s", settings.worker_id)

    ws = LockedWebSocket(ws)
    by_id = {manifest.id: manifest for manifest in manifests}
    runners: dict[uuid.UUID, SessionRunner] = {}
    # Highest generation seen per session, including tombstones after close
    # so a delayed open cannot resurrect a finished session. Dropped when
    # this connection ends.
    highest_generation: dict[uuid.UUID, int] = {}
    # Keyed by task rather than by job id: a requeued attempt of a job already
    # running here would overwrite an id key and lose the first task, which
    # the teardown below still has to cancel.
    jobs: dict[asyncio.Task, JobRun] = {}

    def forget(task: asyncio.Task) -> None:
        jobs.pop(task, None)

    async def heartbeats() -> None:
        while True:
            await asyncio.sleep(settings.heartbeat_seconds)
            gpu = await asyncio.to_thread(sample_gpu, settings.device)
            await ws.send(json.dumps({
                "type": "heartbeat",
                "slots_in_use": len(runners),
                "loaded_models": engine.loaded_models(),
                "gpu": gpu,
                # The API overwrites the calibration estimate with these,
                # for every model the engine has measured; residency is
                # irrelevant to a past measurement, so an evicted model
                # keeps reporting its number.
                "frame_p95_ms": frame_p95_payload(engine),
            }))

    heartbeat_task = asyncio.create_task(heartbeats())
    try:
        async for message in ws:
            try:
                if isinstance(message, bytes):
                    if len(message) < FRAME_HEADER_BYTES:
                        raise ValueError("binary frame shorter than the header")
                    session_id = uuid.UUID(bytes=message[1:FRAME_HEADER_BYTES])
                    if session_id in runners:
                        runners[session_id].submit(message[FRAME_HEADER_BYTES:])
                else:
                    control = json.loads(message)
                    if control["type"] == "open_session":
                        session_id = uuid.UUID(control["session_id"])
                        generation = control_generation(control)
                        if generation is None:
                            # Unfenced protocol 4 message: do not believe it.
                            logger.warning(
                                "open_session for %s omitted control_generation; ignored",
                                control.get("session_id"),
                            )
                            continue
                        highest = highest_generation.get(session_id, 0)
                        if generation < highest:
                            continue
                        if generation == highest:
                            runner = runners.get(session_id)
                            if runner is not None:
                                await runner.resend_ready(ws)
                            continue
                        old = runners.pop(session_id, None)
                        if old is not None:
                            old.close()
                        highest_generation[session_id] = generation
                        manifest = by_id[control["model_id"]]
                        runners[session_id] = SessionRunner(
                            session_id, ws, engine, manifest,
                            ensure_seed(manifest.with_defaults(
                                control.get("params") or {})),
                            generation)
                    elif control["type"] == "update_session":
                        # Ignored for an unknown session rather than raised:
                        # the API may send an update for a session this
                        # worker has just torn down, and the teardown and the
                        # update cross on the wire without either side being
                        # wrong. Equality with the active runner is required;
                        # an unfenced or stale generation is ignored.
                        generation = control_generation(control)
                        runner = runners.get(uuid.UUID(control["session_id"]))
                        if runner is None or generation != runner.generation:
                            continue
                        # The API owns the seed when an update carries
                        # one: its stored params are the browser's keys
                        # merged over the session's, and applying the
                        # update as-is is what keeps the params_updated
                        # acknowledgement honest. When the update has no
                        # seed, the runner's own value from open is the
                        # fallback: an older API never fills a seed, so
                        # replacing the params would delete the only
                        # seed there is and the conditioned path would
                        # build no generator, re-rolling every frame.
                        # with_defaults restores what open_session did,
                        # filling only absent keys, so a subset update
                        # cannot leave the engine's hardcoded fallbacks
                        # in charge of settings the manifest declares.
                        updated = runner.manifest.with_defaults(control["params"])
                        seed = normalise_seed(updated.get("seed"))
                        if seed is None:
                            # An update without a seed, or with one that
                            # is not a seed (a bool, a fractional float):
                            # the runner's own value from open is the
                            # fallback - the session's seed is fixed for
                            # its life, and an older API never fills one,
                            # so replacing the params would delete the
                            # only seed there is and the conditioned path
                            # would build no generator, re-rolling every
                            # frame.
                            seed = runner.params["seed"]
                        updated["seed"] = seed
                        runner.params = updated
                    elif control["type"] == "close_session":
                        session_id = uuid.UUID(control["session_id"])
                        generation = control_generation(control)
                        # A missing runner is a no-op, never an error: the
                        # API may close a session this worker has already
                        # torn down, or one that never opened here.
                        runner = runners.get(session_id)
                        if runner is None:
                            if generation is not None:
                                highest_generation[session_id] = max(
                                    highest_generation.get(session_id, 0), generation)
                            continue
                        if generation != runner.generation:
                            continue
                        runners.pop(session_id, None)
                        runner.close()
                        highest_generation[session_id] = max(
                            highest_generation.get(session_id, 0), runner.generation)
                        category, score = categorize_output(None)
                        closed = {
                            "type": "session_closed",
                            "session_id": control["session_id"],
                            "frames": runner.frames,
                            "gpu_ms": runner.gpu_ms,
                            "duration_ms": int((time.monotonic() - runner.started_at) * 1000),
                            "category": category,
                            "control_generation": runner.generation,
                        }
                        if score is not None:
                            closed["category_score"] = score
                        await ws.send(json.dumps(closed))
                    elif control["type"] == "dispatch_job":
                        job = JobRun(control["job_id"], control.get("dispatch_token"))
                        task = asyncio.create_task(run_job(
                            ws, engine, by_id[control["model_id"]], control, job.stop))
                        jobs[task] = job
                        task.add_done_callback(forget)
                    elif control["type"] == "cancel_job":
                        # A job that already finished, or one this worker never
                        # ran, leaves nothing to stop. That is not a violation:
                        # the API sends this after cancelling the row, and the
                        # two cross on the wire without either side being wrong.
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
                # docs/connection-handling.md: protocol violations close with
                # 4000 from either side; run() then reconnects with backoff.
                logger.warning("protocol violation from the API (%s), closing", error)
                await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
                return
    finally:
        heartbeat_task.cancel()
        runner_tasks: list[asyncio.Task] = []
        for runner in runners.values():
            runner.close()
            runner_tasks.append(runner._task)
        for task in jobs:
            task.cancel()
        await asyncio.gather(heartbeat_task, *jobs, *runner_tasks, return_exceptions=True)


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
