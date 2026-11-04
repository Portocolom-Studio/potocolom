"""The installation's auth mode, as this process serves it.

PostgreSQL records the decision in installation_auth_state. This module holds
the copy every request reads, so no route asks the database per request and
no route mistakes the AUTH_MODE override for the decision. The copy is loaded
when startup settles the mode (db.connect) and changes only through the two
one-way moves to accounts: the setup claim and the runtime enable.

Until startup has loaded it (tests, the offline tools, a database that was
down at startup) the override answers. With no override the answer is
unclaimed, which fails closed: a process that cannot read the decision serves
no account-less work, and the gate does not answer for it, because the gate
only answers for a decision it has read.

SetupGate answers every API route with 503 while the installation is unclaimed,
except the few routes that must work before anyone has chosen.
"""

from collections.abc import Awaitable, Callable

from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from app.settings import Settings, get_settings

UNCLAIMED = "unclaimed"
NONE = "none"
ACCOUNTS = "accounts"

_loaded: str | None = None

# Answered before anything is decided, so the browser can show setup and
# workers can report their hardware. Ready is a probe that reports store
# availability and carries no data, and the installer polls it.
SETUP_OPEN_HTTP = frozenset({
    ("GET", "/api/v1/config"),
    ("GET", "/api/v1/health"),
    ("HEAD", "/api/v1/health"),
    ("GET", "/api/v1/ready"),
    ("HEAD", "/api/v1/ready"),
    ("POST", "/api/v1/auth/setup"),
})
SETUP_OPEN_SOCKETS = frozenset({"/api/v1/fleet"})


def load(mode: str) -> None:
    global _loaded
    _loaded = mode


def clear() -> None:
    global _loaded
    _loaded = None


def current() -> str:
    if _loaded is not None:
        return _loaded
    return get_settings().auth_mode or UNCLAIMED


def setup_required() -> bool:
    return _loaded == UNCLAIMED


def sign_in_methods(settings: Settings) -> list[str]:
    if current() != ACCOUNTS:
        return []
    return ["password", *settings.oauth_offered]


class SetupGate:
    """Pure ASGI, innermost of the app's middleware, so the headers and the
    request id that wrap it still stamp its answers.

    It reads the holder on every request, which is an attribute read, and it
    never touches the database: the answer must not depend on a store that
    may be down.
    """

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] not in {"http", "websocket"}
                or not scope["path"].startswith("/api/")
                or not setup_required()):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            if scope["path"] in SETUP_OPEN_SOCKETS:
                await self.app(scope, receive, send)
                return
            # Before accept, so the upgrade fails as HTTP 403 and no socket exists.
            await send({"type": "websocket.close"})
            return
        if (scope["method"], scope["path"]) in SETUP_OPEN_HTTP:
            await self.app(scope, receive, send)
            return
        response = JSONResponse({"detail": "setup_required"}, status_code=503)
        await response(scope, receive, send)
