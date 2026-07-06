import asyncio
import uuid

from worker.client import FRAME_HEADER_BYTES, SessionRunner


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
