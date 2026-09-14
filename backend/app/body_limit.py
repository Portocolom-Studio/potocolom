"""Cap request bodies before FastAPI buffers them.

JSON and form routes have no other ceiling: FastAPI reads a model-bound body
before route dependencies run, so an unauthenticated POST can choose how much
memory the process holds. File uploads and SES feedback already stream with
their own caps; this wrapper uses those same numbers on those paths and a
smaller default everywhere else.
"""

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import app.files as files
import app.ses_feedback as ses_feedback

# Larger than any JSON the routes accept. This is a per-request bound, not
# a process bound: one unauthenticated JSON body cannot exceed 1 MiB.
# File uploads stay at 64 MiB and SES feedback stays at 512 KiB.
MAX_JSON_BODY_BYTES = 1 * 1024 * 1024

TOO_LARGE = "request body too large"
_FILES_PREFIX = "/api/v1/files/"
_SES_PATH = "/api/v1/mail/feedback"


def max_body_bytes(path: str) -> int:
    if path.startswith(_FILES_PREFIX):
        return files.MAX_UPLOAD_BYTES
    if path == _SES_PATH:
        return ses_feedback.MAX_BODY_BYTES
    return MAX_JSON_BODY_BYTES


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers") or ():
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
    await JSONResponse({"detail": TOO_LARGE}, status_code=413)(scope, receive, send)


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        limit = max_body_bytes(path)
        length = _content_length(scope)
        if length is not None and length > limit:
            await _reject(scope, receive, send)
            return

        if path.startswith(_FILES_PREFIX):
            await self._stream_files(scope, receive, send, limit)
            return

        await self._buffer_then_replay(scope, receive, send, limit)

    async def _stream_files(
        self, scope: Scope, receive: Receive, send: Send, limit: int
    ) -> None:
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    raise HTTPException(status_code=413, detail=TOO_LARGE)
            return message

        await self.app(scope, limited_receive, send)

    async def _buffer_then_replay(
        self, scope: Scope, receive: Receive, send: Send, limit: int
    ) -> None:
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body") or b""
            if len(body) + len(chunk) > limit:
                await _reject(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body"):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            # Streaming responses wait here for http.disconnect. Swallowing
            # that message leaves the sender looping on the request body.
            return await receive()

        await self.app(scope, replay, send)
