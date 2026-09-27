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
            payload = b"release-frame-before-failover"
            await socket.send(b"\x01" + session + payload)
            while True:
                message = await socket.recv()
                if isinstance(message, bytes):
                    assert message == b"\x02" + session + payload, message
                    break
                control = json.loads(message)
                assert control["type"] != "error", control
            print(f"FIRST_FRAME {ready['session_id']}", flush=True)

            controls = []
            while controls != ["interrupted", "resumed"]:
                message = await socket.recv()
                assert not isinstance(message, bytes), message
                control = json.loads(message)
                assert control["type"] != "error", control
                if control["type"] in {"interrupted", "resumed"}:
                    controls.append(control["type"])
                    assert controls in (["interrupted"], ["interrupted", "resumed"]), controls

            payload = b"release-frame-after-failover"
            await socket.send(b"\x01" + session + payload)
            while True:
                message = await socket.recv()
                if isinstance(message, bytes):
                    assert message == b"\x02" + session + payload, message
                    break
                control = json.loads(message)
                assert control["type"] != "error", control
            await socket.send(json.dumps({"type": "close"}))
    print(f"FAILOVER_PASSED {ready['session_id']}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
