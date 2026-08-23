"""Request identity, in both modes.

AUTH_MODE=none resolves every request to the single implicit local user.
AUTH_MODE=accounts resolves a session, presented as a bearer or as a cookie,
and applies the role and account-state policy (docs/blueprint.md, the mode
seam).
"""

import secrets
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit, db, sessions
from app.settings import get_settings
from app.tables import User


async def require_accounts_mode() -> None:
    """An install that never enabled accounts does not answer account routes."""
    if get_settings().auth_mode != "accounts":
        raise HTTPException(status_code=404, detail="Not Found")


CANNOT_SIGN_IN = frozenset({"disabled", "deletion_pending", "purging"})
UNAUTHENTICATED = HTTPException(status_code=401, detail="authentication required")


def _allowed_origins() -> set[str]:
    settings = get_settings()
    allowed = {settings.public_url.rstrip("/")}
    allowed.update(
        candidate.strip().rstrip("/")
        for candidate in settings.allowed_origins.split(",")
        if candidate.strip()
    )
    return allowed


def _check_csrf(request: Request, csrf_cookie: str | None) -> None:
    """Only for cookies: a cookie rides along on any request a page can cause.

    A bearer is presented deliberately, so it needs neither check. An absent
    Origin is refused rather than waved through, because browsers send one on
    every unsafe request and only a non-browser caller can omit it.
    """
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is None or origin.rstrip("/") not in _allowed_origins():
        raise HTTPException(status_code=403, detail="origin not allowed")
    sent = request.headers.get("x-csrf-token")
    # Compared as bytes: compare_digest refuses non-ASCII strings, and headers
    # and cookies decode as latin-1, so a planted value would raise here and
    # turn every unsafe request that browser sends into a 500.
    if not csrf_cookie or not sent or not secrets.compare_digest(
        sent.encode("latin-1", "replace"), csrf_cookie.encode("latin-1", "replace")
    ):
        raise HTTPException(status_code=403, detail="missing or mismatched CSRF token")


async def current_principal(request: Request) -> sessions.Resolved:
    """The authenticated session behind this request, in accounts mode.

    A bearer wins outright: a caller presenting one is making a claim about who
    they are, and falling back to the cookie would let a page borrow the
    browser's ambient credential after its own claim was rejected.
    """
    # Before anything reads a token: with the store down, resolving one raises
    # and the caller sees a 500 for an outage the contract answers with 503.
    await db.require_account_dependencies()
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        resolved = await sessions.resolve(header[7:].strip())
        if resolved is None:
            raise UNAUTHENTICATED
    else:
        session_name, csrf_name = sessions.cookie_names(get_settings().public_url)
        token = request.cookies.get(session_name)
        if not token:
            raise UNAUTHENTICATED
        resolved = await sessions.resolve(token)
        if resolved is None:
            raise UNAUTHENTICATED
        _check_csrf(request, request.cookies.get(csrf_name))
    if resolved.user.state in CANNOT_SIGN_IN:
        raise UNAUTHENTICATED
    return resolved


async def current_user(
    request: Request,
    session: AsyncSession = Depends(db.get_session),
) -> User:
    if get_settings().auth_mode != "none":
        principal = await current_principal(request)
        if principal.user.state == "suspended" and request.method not in SAFE_METHODS:
            # A pause, not a deletion: they may read their own work and settle
            # their account, and may change nothing.
            raise HTTPException(status_code=403, detail="account suspended")
        return principal.user
    if db.local_user_id is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    user = await session.get(User, db.local_user_id)
    if user is None:
        raise HTTPException(status_code=503, detail="local user missing")
    return user


RoleTier = Literal["viewer", "member", "admin"]
_ROLE_RANK = {"viewer": 0, "user": 1, "admin": 2}


# A safe method changes nothing, and the studio polls two of these admin reads
# every two seconds, which would bury real administrator work under millions of
# rows a caller drives for free. A read that reaches another user's data is a
# different thing: it carries a target, which this hook cannot know, so those
# routes record it themselves.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _action(request: Request) -> str:
    """The route template, not the resolved path, so ids never become actions."""
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    return f"{request.method} {path}"


def require_role(minimum: RoleTier) -> Callable[..., Awaitable[User]]:
    """Require a role tier while preserving "user" as the stored member value.

    Administrator work is audited here rather than in each route: a route added
    later cannot forget, and no route can be audited under a name that has
    drifted from the path it actually serves.
    """
    required = "user" if minimum == "member" else minimum

    async def role_user(request: Request, user: User = Depends(current_user)) -> User:
        unsafe_admin = required == "admin" and request.method not in SAFE_METHODS
        if _ROLE_RANK.get(user.role, -1) < _ROLE_RANK[required]:
            if unsafe_admin:
                # Someone with an account reaching for privileged work they do
                # not have is the signal an audit exists to carry.
                await audit.record(_action(request), actor=user, severity="high")
            raise HTTPException(status_code=403, detail="insufficient role")
        if unsafe_admin:
            await audit.record(_action(request), actor=user)
        return user

    return role_user
