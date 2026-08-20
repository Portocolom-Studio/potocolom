import asyncio
import contextlib
import time
import uuid

from worker.client import SessionRunner
from worker.engine import PromptCache, SimulatedEngine
from worker.frame_batch import (
    BATCH_WINDOW_MS,
    CompatKey,
    FrameBatchCollector,
    FrameRequest,
    compat_key,
)
from worker.manifests import SIMULATED_MANIFEST, Manifest


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


class RecordingExecutor:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []
        self.cancelled_in_batch: list[int] = []

    async def execute_frame_batch(self, requests: list[FrameRequest]) -> None:
        self.batch_sizes.append(len(requests))
        for request in requests:
            if request.cancelled:
                self.cancelled_in_batch.append(request.session_key)
                continue
            if not request.future.done():
                request.future.set_result(request.payload)


def test_compat_key_uses_model_steps_resolution():
    manifest = Manifest(
        id="vega-rt",
        name="Vega",
        capabilities=["realtime"],
        parameters={
            "type": "object",
            "properties": {
                "steps": {"type": "integer", "default": 4},
            },
        },
    )
    key = compat_key(manifest, {"steps": 2}, 512)
    assert key == CompatKey("vega-rt", 2, 512)


def test_two_compatible_sessions_batch_together():
    async def scenario():
        engine = SimulatedEngine(0.05)
        manifest = SIMULATED_MANIFEST
        cache_a = PromptCache()
        cache_b = PromptCache()
        start = time.monotonic()
        results = await asyncio.gather(
            engine.frame(manifest, {}, b"alpha", prompt_cache=cache_a),
            engine.frame(manifest, {}, b"beta", prompt_cache=cache_b),
        )
        elapsed = time.monotonic() - start
        return engine, results, elapsed

    engine, results, elapsed = asyncio.run(scenario())
    assert engine._batch_sizes == [2]
    assert results[0].data == b"alpha"
    assert results[1].data == b"beta"
    # One GPU cycle (~50 ms), not two serial (~100 ms). Bound is loose so a
    # loaded runner does not fail a scheduling claim already pinned above.
    assert elapsed < 0.2


def test_mismatched_steps_do_not_batch():
    async def scenario():
        engine = SimulatedEngine(0.02)
        manifest = Manifest(
            id="sd-sim",
            name="Sim",
            capabilities=["realtime"],
            parameters={
                "type": "object",
                "properties": {
                    "steps": {"type": "integer", "default": 2},
                },
            },
        )
        await asyncio.gather(
            engine.frame(manifest, {"steps": 2}, b"a", prompt_cache=PromptCache()),
            engine.frame(manifest, {"steps": 4}, b"b", prompt_cache=PromptCache()),
        )
        return engine

    engine = asyncio.run(scenario())
    assert engine._batch_sizes == [1, 1]


def test_collector_latest_input_wins_per_session():
    async def scenario():
        executor = RecordingExecutor()
        collector = FrameBatchCollector(executor, window_ms=20.0)
        manifest = SIMULATED_MANIFEST
        first = asyncio.create_task(
            collector.submit(1, manifest, {}, b"old", 0.0, resolution=512),
        )
        await asyncio.sleep(0)
        second = asyncio.create_task(
            collector.submit(1, manifest, {}, b"new", 0.0, resolution=512),
        )
        results = await asyncio.gather(first, second)
        return executor, results

    executor, results = asyncio.run(scenario())
    assert executor.batch_sizes == [1]
    assert results == [b"new", b"new"]


def test_round_robin_across_two_classes():
    executor = RecordingExecutor()
    collector = FrameBatchCollector(executor, window_ms=1000.0)
    manifest = Manifest(
        id="sd-sim",
        name="Sim",
        capabilities=["realtime"],
        parameters={
            "type": "object",
            "properties": {
                "steps": {"type": "integer", "default": 2},
            },
        },
    )
    loop = asyncio.new_event_loop()
    try:
        for steps, session_key, payload in (
            (2, 1, b"a1"), (4, 2, b"b1"), (2, 3, b"a2"), (4, 4, b"b2"),
        ):
            collector._enqueue(FrameRequest(
                session_key,
                compat_key(manifest, {"steps": steps}, 512),
                manifest, {"steps": steps}, 0.0, None, payload,
                loop.create_future(),
            ))
        first = collector._pick_batch()
        second = collector._pick_batch()
        assert first is not None and second is not None
        assert first[0].compat.steps != second[0].compat.steps
        assert {first[0].compat.steps, second[0].compat.steps} == {2, 4}
    finally:
        loop.close()


def test_batch_window_constant_in_range():
    assert 30 <= BATCH_WINDOW_MS <= 50


def test_collection_window_waits_before_running():
    state = {"clock": 0.0}
    ran_at: list[float] = []

    def monotonic() -> float:
        return state["clock"]

    async def fake_sleep(delay: float) -> None:
        state["clock"] += delay

    class TimingExecutor:
        async def execute_frame_batch(self, requests: list[FrameRequest]) -> None:
            ran_at.append(monotonic())
            for request in requests:
                if not request.future.done():
                    request.future.set_result(request.payload)

    async def scenario():
        collector = FrameBatchCollector(
            TimingExecutor(), window_ms=BATCH_WINDOW_MS, sleep=fake_sleep,
        )
        manifest = SIMULATED_MANIFEST
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        collector._enqueue(FrameRequest(
            1, compat_key(manifest, {}, 512), manifest, {}, 0.0, None, b"x", future,
        ))
        collector._work.set()
        task = asyncio.create_task(collector._loop())
        result = await future
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return result

    payload = asyncio.run(scenario())
    assert ran_at == [BATCH_WINDOW_MS / 1000.0]
    assert payload == b"x"


def test_closed_session_does_not_receive_frame():
    async def scenario():
        socket = FakeSocket()
        engine = SimulatedEngine(0.05, batch_window_ms=BATCH_WINDOW_MS)
        runner_a = SessionRunner(
            uuid.uuid4(), socket, engine, SIMULATED_MANIFEST, {},
        )
        runner_b = SessionRunner(
            uuid.uuid4(), socket, engine, SIMULATED_MANIFEST, {},
        )
        runner_a.submit(b"mate")
        runner_b.submit(b"victim")
        await asyncio.sleep(0)
        runner_b.close()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            frames = [m for m in socket.sent if isinstance(m, (bytes, bytearray))]
            if len(frames) >= 1:
                break
            await asyncio.sleep(0.01)
        runner_a.close()
        return socket

    socket = asyncio.run(scenario())
    frames = [m for m in socket.sent if isinstance(m, (bytes, bytearray))]
    assert len(frames) == 1
    assert frames[0].endswith(b"mate")


def test_collector_survives_an_executor_error():
    class BoomThenOk:
        def __init__(self) -> None:
            self.calls = 0

        async def execute_frame_batch(self, requests: list[FrameRequest]) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("poison")
            for request in requests:
                if not request.future.done():
                    request.future.set_result(request.payload)

    async def scenario():
        executor = BoomThenOk()
        collector = FrameBatchCollector(executor, window_ms=0.0)
        manifest = SIMULATED_MANIFEST
        try:
            await collector.submit(1, manifest, {}, b"a", 0.0, resolution=512)
        except RuntimeError:
            pass
        else:
            raise AssertionError("first batch should fail")
        second = await collector.submit(2, manifest, {}, b"b", 0.0, resolution=512)
        return second, executor.calls

    payload, calls = asyncio.run(scenario())
    assert payload == b"b"
    assert calls == 2


def test_a_newer_frame_is_not_dropped_when_the_batch_clears_compat():
    """execute_frame_batch can resolve the waiter, which then submits again
    before the collector pops session_compat. Popping anyway loses the new
    request: the next submit overwrites it and the overwritten future hangs."""

    async def scenario():
        released = asyncio.Event()
        started = asyncio.Event()

        class Slow:
            async def execute_frame_batch(self, requests: list[FrameRequest]) -> None:
                started.set()
                await released.wait()
                for request in requests:
                    if not request.future.done():
                        request.future.set_result(request.payload)

        collector = FrameBatchCollector(Slow(), window_ms=0.0)
        manifest = SIMULATED_MANIFEST
        first = asyncio.create_task(
            collector.submit(1, manifest, {}, b"a", 0.0, resolution=512),
        )
        await started.wait()
        second = asyncio.create_task(
            collector.submit(1, manifest, {}, b"b", 0.0, resolution=512),
        )
        await asyncio.sleep(0)
        released.set()
        got = await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
        later = await asyncio.wait_for(
            collector.submit(1, manifest, {}, b"c", 0.0, resolution=512),
            timeout=1,
        )
        return got, later

    got, later = asyncio.run(scenario())
    assert got == [b"a", b"b"]
    assert later == b"c"


def test_close_settles_outstanding_requests():
    async def scenario():
        class Idle:
            async def execute_frame_batch(self, requests: list[FrameRequest]) -> None:
                await asyncio.Event().wait()

        collector = FrameBatchCollector(Idle(), window_ms=1000.0)
        manifest = SIMULATED_MANIFEST
        pending = asyncio.create_task(
            collector.submit(1, manifest, {}, b"x", 0.0, resolution=512),
        )
        await asyncio.sleep(0)
        await collector.close()
        try:
            await pending
        except asyncio.CancelledError:
            return "cancelled"
        return "completed"

    assert asyncio.run(scenario()) == "cancelled"
