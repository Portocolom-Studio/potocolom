"""Server-generated request ids for HTTP responses and application logs.

A pure ASGI middleware, not BaseHTTPMiddleware: the body-limit wrapper needs
streaming bodies and the realtime router needs WebSockets to pass through
untouched, and BaseHTTPMiddleware breaks both. An incoming X-Request-ID is
ignored on purpose - only a value this server generated is safe to log.
"""

import contextvars
import logging
import secrets

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.security import unhandled_exception_response

logger = logging.getLogger("potocolom.request_id")

current_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_request_id", default=None
)


class RequestIdMiddleware:
    """Stamp every HTTP response with an id; other scopes pass straight through."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = secrets.token_hex(16)
        token = current_request_id.set(request_id)
        response_started = False

        async def send_with_id(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                MutableHeaders(scope=message)["x-request-id"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            # ServerErrorMiddleware wraps every user middleware, so its 500
            # would miss this header, and by then the contextvar is reset.
            # Answer here with the same handler the Exception handler
            # registration uses; the re-raise keeps the server's own error
            # reporting working.
            logger.error(
                "unhandled exception on %s %s", scope["method"], scope["path"], exc_info=exc
            )
            if not response_started:
                response = unhandled_exception_response(Request(scope), exc)
                response.headers["x-request-id"] = request_id
                await response(scope, receive, send)
            raise
        finally:
            current_request_id.reset(token)
