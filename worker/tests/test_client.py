import asyncio
import io
import json
import uuid

import pytest
from PIL import Image

from worker.client import (
    FRAME_HEADER_BYTES,
    RegistrationRejected,
    SEED_BOUND,
    SessionRunner,
    ensure_seed,
    frame_p95_payload,
    run,
    run_job,
    serve_connection,
)
from worker.engine import GeneratedFrame, SimulatedEngine
from worker.manifests import SIMULATED_MANIFEST, Manifest
from worker.settings import Settings


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


def test_ensure_seed_keeps_explicit_integer_seed():
    params = {"prompt": "x", "seed": 42}
    assert ensure_seed(params) is params
    assert params["seed"] == 42


def test_ensure_seed_adds_integer_seed_within_bound():
    params = ensure_seed({"prompt": "x"})
    assert isinstance(params["seed"], int)
    assert 0 <= params["seed"] < SEED_BOUND
    assert "prompt" in params


def test_ensure_seed_differs_between_sessions():
    assert ensure_seed({})["seed"] != ensure_seed({})["seed"]


def test_ensure_seed_non_integer_seed_is_replaced():
    params = ensure_seed({"seed": "seven"})
    assert isinstance(params["seed"], int)


def test_latest_input_wins():
    socket = FakeSocket()

    async def scenario():
        runner = SessionRunner(uuid.uuid4(), socket, SimulatedEngine(0.01),
                               SIMULATED_MANIFEST, {})
        runner.submit(b"first")
        runner.submit(b"second")
        runner.submit(b"third")
        await asyncio.sleep(0.05)
        runner.close()
        return runner

    runner = asyncio.run(scenario())
    assert runner.dropped == 2
    assert not hasattr(runner, "last_output")
    assert len(socket.sent) == 1
    assert socket.sent[0][FRAME_HEADER_BYTES:] == b"third"


class RecordingSocket:
    """Plays the API role for serve_connection and records what it sent."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.close_code = None

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        return json.dumps({"type": "registered"})

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)

    async def close(self, code=1000):
        self.close_code = code


def drive_update_session(monkeypatch, messages):
    """Run serve_connection through the given API messages, recording runners.

    SessionRunner is replaced by a subclass that records its instances, so the
    test can inspect the params the update_session handler replaced.
    """
    created = []

    class RecordingRunner(SessionRunner):
        def __init__(self, *args):
            created.append(self)
            super().__init__(*args)

    monkeypatch.setattr("worker.client.SessionRunner", RecordingRunner)
    socket = RecordingSocket(messages)
    asyncio.run(serve_connection(socket, Settings(worker_id="w-update"),
                                 [SIMULATED_MANIFEST], SimulatedEngine(0.01)))
    return socket, created


def test_update_session_replaces_params_and_keeps_seed(monkeypatch):
    session_id = str(uuid.uuid4())
    socket, created = drive_update_session(monkeypatch, [
        json.dumps({"type": "open_session", "session_id": session_id,
                    "model_id": "sd-sim", "params": {"prompt": "a red house"}}),
        json.dumps({"type": "update_session", "session_id": session_id,
                    "params": {"prompt": "a blue house"}}),
    ])
    assert len(created) == 1
    runner = created[0]
    seed = runner.params["seed"]
    assert isinstance(seed, int)
    # The update replaced the prompt and left the open's seed untouched: an
    # update that re-rolled it would make the image jump on the next frame.
    assert runner.params == {"prompt": "a blue house", "seed": seed}
    assert socket.close_code is None


def test_update_session_for_an_unknown_session_is_ignored(monkeypatch):
    session_id = str(uuid.uuid4())
    socket, created = drive_update_session(monkeypatch, [
        json.dumps({"type": "open_session", "session_id": session_id,
                    "model_id": "sd-sim", "params": {"prompt": "a red house"}}),
        json.dumps({"type": "update_session",
                    "session_id": str(uuid.uuid4()),
                    "params": {"prompt": "a blue house"}}),
    ])
    assert len(created) == 1
    runner = created[0]
    seed = runner.params["seed"]
    # The unknown session was ignored, so the live runner kept its params and
    # the connection stayed open instead of closing 4000.
    assert runner.params == {"prompt": "a red house", "seed": seed}
    assert socket.close_code is None


def test_malformed_control_closes_and_returns_for_reconnect():
    class ScriptedSocket:
        def __init__(self, messages):
            self.messages = list(messages)
            self.sent = []
            self.close_code = None

        async def send(self, data):
            self.sent.append(data)

        async def recv(self):
            return json.dumps({"type": "registered"})

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.messages:
                raise StopAsyncIteration
            return self.messages.pop(0)

        async def close(self, code=1000):
            self.close_code = code

    socket = ScriptedSocket(messages=["this is not json"])
    asyncio.run(serve_connection(socket, Settings(worker_id="w-mangled"),
                                 [SIMULATED_MANIFEST], SimulatedEngine(0.01)))
    assert socket.close_code == 4000


def test_hello_carries_manifests():
    class HelloOnlySocket(FakeSocket):
        async def recv(self):
            return json.dumps({"type": "registered"})

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    socket = HelloOnlySocket()
    asyncio.run(serve_connection(socket, Settings(worker_id="w-hello"),
                                 [SIMULATED_MANIFEST], SimulatedEngine(0.01)))
    hello = json.loads(socket.sent[0])
    manifest = hello["models"][0]
    assert manifest["id"] == "sd-sim"
    assert "realtime" in manifest["capabilities"]
    assert "source" not in manifest  # weight locations stay worker side
    # The simulated engine never calibrated a frame, so nothing is advertised.
    assert "realtime_p95_ms" not in manifest
    assert hello["device"] == "cpu"
    assert hello["memory_mode"] == "auto"


def test_hello_carries_measured_realtime_p95_ms():
    class P95Engine(SimulatedEngine):
        def __init__(self):
            super().__init__(0.01)
            self._measured = {"vega-rt": 408}

        def realtime_p95_ms(self, model_id):
            return self._measured.get(model_id)

    class HelloOnlySocket(FakeSocket):
        async def recv(self):
            return json.dumps({"type": "registered"})

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    socket = HelloOnlySocket()
    manifests = [Manifest(id="vega-rt", name="VegaRT",
                          capabilities=["text_to_image", "image_to_image", "realtime"],
                          min_vram_gb=8)]
    asyncio.run(serve_connection(socket, Settings(worker_id="w-p95"),
                                 manifests, P95Engine()))
    hello = json.loads(socket.sent[0])
    assert hello["models"][0]["realtime_p95_ms"] == 408


def test_frame_p95_payload_reports_measured_models_even_evicted():
    class P95Engine(SimulatedEngine):
        def __init__(self):
            super().__init__(0.01)
            self._measured = {"vega-rt": 412}

        def realtime_p95_ms(self, model_id):
            return self._measured.get(model_id)

        def p95_model_ids(self):
            return list(self._measured)

        def loaded_models(self):
            return []  # evicted: residency must not silence a past measurement

    assert frame_p95_payload(P95Engine()) == {"vega-rt": 412}


def test_heartbeat_carries_live_frame_p95():
    import time

    class P95Engine(SimulatedEngine):
        def __init__(self):
            super().__init__(0.01)
            self._measured = {"vega-rt": 412}

        def realtime_p95_ms(self, model_id):
            return self._measured.get(model_id)

        def p95_model_ids(self):
            # The engine holds measurements for both ids; the payload still
            # omits the one with no number.
            return ["vega-rt", "sdxl-turbo"]

    class HeartbeatSocket(RecordingSocket):
        def __init__(self):
            # One message so the connection loop is live while the heartbeat
            # task runs, then it blocks until the test releases it.
            super().__init__([json.dumps({"type": "gpu_status", "request_id": "r1"})])
            self.release = asyncio.Event()

        async def __anext__(self):
            if self.messages:
                return self.messages.pop(0)
            await self.release.wait()
            raise StopAsyncIteration

    socket = HeartbeatSocket()
    manifests = [Manifest(id="vega-rt", name="VegaRT",
                          capabilities=["text_to_image", "image_to_image", "realtime"],
                          min_vram_gb=8),
                 Manifest(id="sdxl-turbo", name="SDXL Turbo",
                          capabilities=["text_to_image", "image_to_image", "realtime"],
                          min_vram_gb=10)]

    async def scenario():
        task = asyncio.create_task(serve_connection(
            socket, Settings(worker_id="w-hb", heartbeat_seconds=0.01),
            manifests, P95Engine()))
        heartbeat = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            for message in socket.sent:
                if isinstance(message, str):
                    payload = json.loads(message)
                    if payload.get("type") == "heartbeat":
                        heartbeat = payload
            if heartbeat is not None:
                break
            await asyncio.sleep(0.005)
        assert heartbeat is not None, "no heartbeat was sent"
        # The measured model is carried; the unmeasured one is omitted.
        assert heartbeat["frame_p95_ms"] == {"vega-rt": 412}
        socket.release.set()
        await task

    asyncio.run(scenario())


def _realtime_manifest(model_id, steps_default):
    return Manifest(
        id=model_id, name=model_id,
        capabilities=["text_to_image", "image_to_image", "realtime"],
        min_vram_gb=8,
        parameters={"type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "steps": {"type": "integer", "minimum": 1,
                                  "maximum": 8, "default": steps_default},
                    },
                    "required": ["prompt"]},
    )


class RecordingEngine(SimulatedEngine):
    """Renders instantly with a canned gpu_ms and records observations."""

    def __init__(self):
        super().__init__(0.01)
        self.observed = []

    async def frame(self, manifest, params, payload):
        await asyncio.sleep(0.01)
        return GeneratedFrame(payload, 200)

    def observe_frame_ms(self, model_id, gpu_ms):
        self.observed.append((model_id, gpu_ms))

    def realtime_p95_ms(self, model_id):
        if sum(1 for m, _ in self.observed if m == model_id) >= 2:
            return 200
        return None

    def p95_model_ids(self):
        return sorted({m for m, _ in self.observed})


def test_session_runner_observes_each_rendered_frame_for_its_model():
    # The call site that makes the live p95 feature exist: if it is deleted
    # the engine sees nothing and this fails.
    socket = FakeSocket()
    engine = RecordingEngine()
    manifest = _realtime_manifest("vega-rt", steps_default=4)

    async def scenario():
        runner = SessionRunner(uuid.uuid4(), socket, engine, manifest,
                               ensure_seed(manifest.with_defaults({"prompt": "x"})))
        runner.submit(b"first")
        await asyncio.sleep(0.03)
        runner.submit(b"second")
        await asyncio.sleep(0.03)
        runner.close()

    asyncio.run(scenario())
    assert engine.observed == [("vega-rt", 200), ("vega-rt", 200)]
    assert len(socket.sent) == 2


def test_heartbeat_advertises_only_frames_at_default_steps():
    # The advertised number claims the model's cost at its declared defaults,
    # so a session at other settings must not supersede it: frames rendered
    # at the default change what a heartbeat carries, frames off-default do
    # not.
    socket = FakeSocket()
    engine = RecordingEngine()
    default_manifest = _realtime_manifest("sdxl-turbo", steps_default=1)
    off_default_manifest = _realtime_manifest("vega-rt", steps_default=4)

    async def run_session(manifest, params, frames):
        runner = SessionRunner(uuid.uuid4(), socket, engine, manifest,
                               ensure_seed(manifest.with_defaults(params)))
        for _ in range(frames):
            runner.submit(b"canvas")
            await asyncio.sleep(0.03)
        runner.close()
        await asyncio.sleep(0.01)

    async def scenario():
        await run_session(default_manifest, {"prompt": "x"}, frames=2)
        await run_session(off_default_manifest, {"prompt": "x", "steps": 8}, frames=2)

    asyncio.run(scenario())
    # Only the default-steps session was observed, and only it is advertised.
    assert engine.observed == [("sdxl-turbo", 200), ("sdxl-turbo", 200)]
    assert frame_p95_payload(engine) == {"sdxl-turbo": 200}


def test_rejected_registration_raises_cleanly():
    class RejectingSocket:
        async def send(self, data):
            pass

        async def recv(self):
            return json.dumps({"type": "rejected", "reason": "unsupported protocol version",
                               "min_supported_version": 3})

    async def scenario():
        await serve_connection(RejectingSocket(), Settings(worker_id="w-old"),
                               [SIMULATED_MANIFEST], SimulatedEngine(0.01))

    with pytest.raises(RegistrationRejected, match="minimum supported version 3"):
        asyncio.run(scenario())


def test_run_sends_the_fleet_token_as_a_handshake_header(monkeypatch):
    calls = []

    class Connection:
        def __init__(self, url, **kwargs):
            calls.append((url, kwargs))

        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return None

    async def fake_serve_connection(*args):
        return None

    class StopReconnect(Exception):
        pass

    async def stop_sleep(_delay):
        raise StopReconnect

    settings = Settings(worker_id="w-token", fleet_token="fleet-secret")
    monkeypatch.setattr("worker.client.get_settings", lambda: settings)
    monkeypatch.setattr("worker.client.build_runtime",
                        lambda _settings: ([SIMULATED_MANIFEST], SimulatedEngine(0.01)))
    monkeypatch.setattr("worker.client.websockets.connect", Connection)
    monkeypatch.setattr("worker.client.serve_connection", fake_serve_connection)
    monkeypatch.setattr("worker.client.asyncio.sleep", stop_sleep)

    with pytest.raises(StopReconnect):
        asyncio.run(run())

    assert calls == [(settings.api_url, {"additional_headers": {
        "x-fleet-token": "fleet-secret",
    }})]


class FakeUpload:
    """Stands in for httpx.AsyncClient; records the PUT it receives."""

    puts: list[tuple[str, bytes, dict[str, str] | None]] = []
    gets: list[str] = []
    get_body = b"input-webp"
    fail = False
    fail_thumb = False

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, url, headers=None):
        FakeUpload.gets.append(url)

        class Response:
            content = FakeUpload.get_body

            @staticmethod
            def raise_for_status():
                if FakeUpload.fail:
                    raise RuntimeError("download refused")

        return Response()

    async def put(self, url, content=b"", headers=None):
        FakeUpload.puts.append((url, content, headers))

        class Response:
            @staticmethod
            def raise_for_status():
                if FakeUpload.fail:
                    raise RuntimeError("upload refused")
                if FakeUpload.fail_thumb and url.endswith("-thumb.webp"):
                    raise RuntimeError("thumb upload refused")

        return Response()


def dispatch_control():
    return {
        "type": "dispatch_job",
        "job_id": "j-1",
        "model_id": "sd-sim",
        "params": {"prompt": "a test"},
        "upload": {
            "url": "http://api/api/v1/files/u/j-1.png",
            "headers": {"Content-Type": "image/png"},
        },
        "thumb_upload": {
            "url": "http://api/api/v1/files/u/j-1-thumb.webp",
            "headers": {"Content-Type": "image/webp"},
        },
    }


def test_run_job_generates_uploads_and_reports(monkeypatch):
    monkeypatch.setattr("worker.client.httpx.AsyncClient", FakeUpload)
    FakeUpload.puts = []
    FakeUpload.fail = False
    socket = FakeSocket()

    asyncio.run(run_job(socket, SimulatedEngine(0.01), SIMULATED_MANIFEST, dispatch_control()))

    assert len(FakeUpload.puts) == 2
    url, content, headers = FakeUpload.puts[0]
    assert url.endswith("j-1.png")
    assert headers == {"Content-Type": "image/png"}
    assert content[:8] == b"\x89PNG\r\n\x1a\n"
    thumb_url, thumb_content, thumb_headers = FakeUpload.puts[1]
    assert thumb_url.endswith("j-1-thumb.webp")
    assert thumb_headers == {"Content-Type": "image/webp"}
    assert thumb_content[:4] == b"RIFF"
    reports = [json.loads(m) for m in socket.sent]
    types = [r["type"] for r in reports]
    assert "job_progress" in types
    assert types.count("job_done") == 1
    done = next(r for r in reports if r["type"] == "job_done")
    assert done["width"] == 512 and done["height"] == 512
    assert done["gpu_ms"] >= 0
    assert done["input_fetch_ms"] >= 0
    assert done["load_ms"] >= 0
    assert done["postprocess_ms"] >= 0
    assert done["duration_ms"] >= 0
    assert done["category"] == "other"
    assert "category_score" not in done
    assert done["has_thumbnail"] is True


def test_run_job_delivers_without_thumbnail_when_thumb_upload_fails(monkeypatch):
    monkeypatch.setattr("worker.client.httpx.AsyncClient", FakeUpload)
    FakeUpload.puts = []
    FakeUpload.fail = False
    FakeUpload.fail_thumb = True
    socket = FakeSocket()
    try:
        asyncio.run(run_job(socket, SimulatedEngine(0.01), SIMULATED_MANIFEST,
                            dispatch_control()))
    finally:
        FakeUpload.fail_thumb = False

    reports = [json.loads(m) for m in socket.sent]
    done = next(r for r in reports if r["type"] == "job_done")
    assert "has_thumbnail" not in done
    assert not any(r["type"] == "job_failed" for r in reports)


def test_run_job_reports_failure(monkeypatch):
    monkeypatch.setattr("worker.client.httpx.AsyncClient", FakeUpload)
    FakeUpload.puts = []
    FakeUpload.fail = True
    socket = FakeSocket()

    asyncio.run(run_job(socket, SimulatedEngine(0.01), SIMULATED_MANIFEST, dispatch_control()))

    reports = [json.loads(m) for m in socket.sent]
    assert not any(r["type"] == "job_done" for r in reports)
    failed = next(r for r in reports if r["type"] == "job_failed")
    assert failed["job_id"] == "j-1"
    assert "upload refused" in failed["reason"]


def test_run_job_downloads_input_image(monkeypatch):
    monkeypatch.setattr("worker.client.httpx.AsyncClient", FakeUpload)
    FakeUpload.puts = []
    FakeUpload.gets = []
    FakeUpload.fail = False
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 20, 30)).save(buffer, "WEBP")
    FakeUpload.get_body = buffer.getvalue()
    socket = FakeSocket()
    control = dispatch_control()
    control["input"] = {"url": "http://api/api/v1/files/source.webp"}

    asyncio.run(run_job(socket, SimulatedEngine(0.01), SIMULATED_MANIFEST, control))

    assert FakeUpload.gets == ["http://api/api/v1/files/source.webp"]
    assert len(FakeUpload.puts) == 2
    reports = [json.loads(m) for m in socket.sent]
    assert any(r["type"] == "job_done" for r in reports)
