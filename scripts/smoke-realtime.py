import asyncio
import json
import uuid

import websockets


async def main() -> None:
    async with asyncio.timeout(30):
        async with websockets.connect("ws://127.0.0.1:8080/api/v1/realtime") as socket:
            await socket.send(json.dumps({
                "type": "open", "model_id": "sd-sim",
                "params": {"prompt": "release frame test"},
            }))
            ready = json.loads(await socket.recv())
            assert ready["type"] == "ready", ready
            session = uuid.UUID(ready["session_id"]).bytes
            payload = b"release-frame-roundtrip"
            await socket.send(b"\x01" + session + payload)
            while True:
                message = await socket.recv()
                if isinstance(message, bytes):
                    assert message == b"\x02" + session + payload, message
                    break
                control = json.loads(message)
                assert control["type"] != "error", control
            await socket.send(json.dumps({"type": "close"}))
    print("realtime frame roundtrip passed")


if __name__ == "__main__":
    asyncio.run(main())
