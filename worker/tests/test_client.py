import asyncio
import io
import json
import uuid

import pytest
from PIL import Image

from worker.client import (
    FRAME_HEADER_BYTES,
    RegistrationRejected,
    SessionRunner,
    run_job,
    serve_connection,
)
from worker.engine import SimulatedEngine
from worker.manifests import SIMULATED_MANIFEST
from worker.settings import Settings


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


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
    assert len(socket.sent) == 1
    assert socket.sent[0][FRAME_HEADER_BYTES:] == b"third"


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


class FakeUpload:
    """Stands in for httpx.AsyncClient; records the PUT it receives."""

    puts: list[tuple[str, bytes]] = []
    gets: list[str] = []
    get_body = b"input-webp"
    fail = False

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
        FakeUpload.puts.append((url, content))

        class Response:
            @staticmethod
            def raise_for_status():
                if FakeUpload.fail:
                    raise RuntimeError("upload refused")

        return Response()


def dispatch_control():
    return {
        "type": "dispatch_job",
        "job_id": "j-1",
        "model_id": "sd-sim",
        "params": {"prompt": "a test"},
        "upload": {"url": "http://api/api/v1/files/u/j-1.webp", "headers": {}},
        "thumb_upload": {"url": "http://api/api/v1/files/u/j-1-thumb.webp", "headers": {}},
    }


def test_run_job_generates_uploads_and_reports(monkeypatch):
    monkeypatch.setattr("worker.client.httpx.AsyncClient", FakeUpload)
    FakeUpload.puts = []
    FakeUpload.fail = False
    socket = FakeSocket()

    asyncio.run(run_job(socket, SimulatedEngine(0.01), SIMULATED_MANIFEST, dispatch_control()))

    assert len(FakeUpload.puts) == 2
    url, content = FakeUpload.puts[0]
    assert url.endswith("j-1.webp")
    assert content[:4] == b"RIFF"  # WebP container
    thumb_url, thumb_content = FakeUpload.puts[1]
    assert thumb_url.endswith("j-1-thumb.webp")
    assert thumb_content[:4] == b"RIFF"
    reports = [json.loads(m) for m in socket.sent]
    types = [r["type"] for r in reports]
    assert "job_progress" in types
    assert types.count("job_done") == 1
    done = next(r for r in reports if r["type"] == "job_done")
    assert done["width"] == 512 and done["height"] == 512
    assert done["gpu_ms"] >= 0


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
