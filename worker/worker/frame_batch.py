"""Cross-session frame batching below the slot abstraction (issue #294).

Sessions submit frames into a short collection window. Pending frames in the
same compatibility class (model, steps, resolution) run as one GPU cycle.
The wire protocol never sees batch ids.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from worker.manifests import Manifest

logger = logging.getLogger("potocolom.worker")

# Collection window: order of 30-50 ms (docs/decisions.md).
BATCH_WINDOW_MS = 40


def occupancy_share_ms(cycle_ms: int, batch_size: int) -> int:
    """Split one GPU cycle across the sessions that occupied it.

    Admission still sums serialized per-model p95. Reporting the whole
    cycle on every session would raise that p95 after a handful of
    batched frames and shed the extra sessions the batch was meant to serve.
    """
    if batch_size <= 1:
        return cycle_ms
    return cycle_ms // batch_size


@dataclass(frozen=True)
class CompatKey:
    model_id: str
    steps: int
    resolution: int


@dataclass
class FrameRequest:
    session_key: int
    compat: CompatKey
    manifest: Manifest
    params: dict
    strength: float
    prompt_cache: Any
    # Diffusers path: decoded canvas; simulated path: raw payload bytes.
    payload: Any
    future: asyncio.Future
    cancelled: bool = False
    profile: bool = False
    stages: dict[str, int] | None = None


class BatchExecutor(Protocol):
    async def execute_frame_batch(self, requests: list[FrameRequest]) -> None: ...


def compat_key(
    manifest: Manifest, params: dict, resolution: int,
) -> CompatKey:
    properties = manifest.parameters.get("properties", {})
    steps = int(params.get(
        "steps", properties.get("steps", {}).get("default", 2),
    ))
    return CompatKey(manifest.id, steps, resolution)


class FrameBatchCollector:
    """Collects pending frames and runs one batch per GPU cycle."""

    def __init__(
        self,
        executor: BatchExecutor,
        *,
        window_ms: float = BATCH_WINDOW_MS,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self._executor = executor
        self._window_s = window_ms / 1000.0
        self._sleep = sleep
        self._pending: dict[CompatKey, dict[int, FrameRequest]] = {}
        self._session_compat: dict[int, CompatKey] = {}
        self._compat_ring: list[CompatKey] = []
        self._next_class_index = 0
        self._work = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        task = self._loop_task
        if task is None or task.done():
            self._loop_task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        for bucket in self._pending.values():
            for request in bucket.values():
                if not request.future.done():
                    request.future.cancel()
        self._pending.clear()
        self._session_compat.clear()
        self._compat_ring.clear()

    async def submit(
        self,
        session_key: int,
        manifest: Manifest,
        params: dict,
        payload: Any,
        strength: float,
        *,
        prompt_cache: Any = None,
        resolution: int,
        profile: bool = False,
    ) -> Any:
        self.start()
        compat = compat_key(manifest, params, resolution)
        existing = self._pending_for(session_key)
        if existing is not None and not existing.future.done():
            existing.manifest = manifest
            existing.params = dict(params)
            existing.strength = strength
            existing.prompt_cache = prompt_cache
            existing.payload = payload
            existing.profile = profile
            if existing.compat != compat:
                self._rebucket(existing, compat)
            try:
                return await existing.future
            except asyncio.CancelledError:
                existing.cancelled = True
                raise
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        request = FrameRequest(
            session_key, compat, manifest, dict(params), strength,
            prompt_cache, payload, future, profile=profile,
        )
        self._enqueue(request)
        try:
            return await future
        except asyncio.CancelledError:
            request.cancelled = True
            raise

    def _pending_for(self, session_key: int) -> FrameRequest | None:
        compat = self._session_compat.get(session_key)
        if compat is None:
            return None
        bucket = self._pending.get(compat)
        if bucket is None:
            return None
        return bucket.get(session_key)

    def _rebucket(self, request: FrameRequest, compat: CompatKey) -> None:
        old_compat = request.compat
        old_bucket = self._pending.get(old_compat)
        if old_bucket is not None:
            old_bucket.pop(request.session_key, None)
            if not old_bucket:
                self._pending.pop(old_compat, None)
                self._compat_ring = [
                    key for key in self._compat_ring if key != old_compat
                ]
        request.compat = compat
        self._session_compat[request.session_key] = compat
        bucket = self._pending.setdefault(compat, {})
        bucket[request.session_key] = request
        if compat not in self._compat_ring:
            self._compat_ring.append(compat)
        self._work.set()

    def _enqueue(self, request: FrameRequest) -> None:
        session_key = request.session_key
        old_compat = self._session_compat.get(session_key)
        if old_compat is not None:
            old_bucket = self._pending.get(old_compat)
            if old_bucket is not None:
                old_bucket.pop(session_key, None)
                if not old_bucket:
                    self._pending.pop(old_compat, None)
                    self._compat_ring = [
                        key for key in self._compat_ring if key != old_compat
                    ]
        self._session_compat[session_key] = request.compat
        bucket = self._pending.setdefault(request.compat, {})
        bucket[session_key] = request
        if request.compat not in self._compat_ring:
            self._compat_ring.append(request.compat)
        self._work.set()

    async def _loop(self) -> None:
        while True:
            await self._work.wait()
            self._work.clear()
            if not self._pending:
                continue
            await self._sleep(self._window_s)
            while self._work.is_set():
                self._work.clear()
            while self._pending:
                batch = self._pick_batch()
                if batch is None:
                    break
                try:
                    await self._executor.execute_frame_batch(batch)
                except Exception as error:
                    logger.exception("frame batch failed")
                    for request in batch:
                        if not request.cancelled and not request.future.done():
                            request.future.set_exception(error)
                except asyncio.CancelledError:
                    for request in batch:
                        if not request.future.done():
                            request.future.cancel()
                    raise
                for request in batch:
                    if self._pending_for(request.session_key) is None:
                        self._session_compat.pop(request.session_key, None)

    def _pick_batch(self) -> list[FrameRequest] | None:
        if not self._compat_ring:
            return None
        ring_len = len(self._compat_ring)
        for offset in range(ring_len):
            index = (self._next_class_index + offset) % ring_len
            compat = self._compat_ring[index]
            bucket = self._pending.get(compat)
            if not bucket:
                continue
            requests = list(bucket.values())
            self._pending.pop(compat, None)
            self._compat_ring = [
                key for key in self._compat_ring if key != compat
            ]
            # Removing the served class shifts later indexes down. Keep
            # the cursor on what used to be next, which is now at index.
            if self._compat_ring:
                self._next_class_index = index % len(self._compat_ring)
            else:
                self._next_class_index = 0
            return requests
        return None
