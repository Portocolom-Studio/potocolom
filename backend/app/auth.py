"""Request identity. Only AUTH_MODE=none exists so far: every request acts as
the single local user. The local and oauth providers land with the accounts
milestone behind the same dependency (docs/blueprint.md, the mode seam)."""

from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit, db
from app.tables import User


async def current_user(session: AsyncSession = Depends(db.get_session)) -> User:
    if db.local_user_id is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    user = await session.get(User, db.local_user_id)
    if user is None:
        raise HTTPException(status_code=503, detail="local user missing")
    return user


RoleTier = Literal["viewer", "member", "admin"]
_ROLE_RANK = {"viewer": 0, "user": 1, "admin": 2}


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
        if _ROLE_RANK.get(user.role, -1) < _ROLE_RANK[required]:
            raise HTTPException(status_code=403, detail="insufficient role")
        if required == "admin":
            await audit.record(_action(request), actor=user)
        return user

    return role_user
