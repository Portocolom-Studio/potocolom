import asyncio
import json
import uuid

import pytest

from worker.client import FRAME_HEADER_BYTES, RegistrationRejected, SessionRunner, serve_connection
from worker.settings import Settings


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


def test_latest_input_wins():
    socket = FakeSocket()

    async def scenario():
        runner = SessionRunner(uuid.uuid4(), socket, inference_seconds=0.01)
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
    asyncio.run(serve_connection(socket, Settings(worker_id="w-mangled")))
    assert socket.close_code == 4000


def test_rejected_registration_raises_cleanly():
    class RejectingSocket:
        async def send(self, data):
            pass

        async def recv(self):
            return json.dumps({"type": "rejected", "reason": "unsupported protocol version",
                               "min_supported_version": 3})

    async def scenario():
        await serve_connection(RejectingSocket(), Settings(worker_id="w-old"))

    with pytest.raises(RegistrationRejected, match="minimum supported version 3"):
        asyncio.run(scenario())
