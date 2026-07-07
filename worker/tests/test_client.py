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
