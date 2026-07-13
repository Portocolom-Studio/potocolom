"""Request identity. Only AUTH_MODE=none exists so far: every request acts as
the single local user. The local and oauth providers land with the accounts
milestone behind the same dependency (docs/blueprint.md, the mode seam)."""

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.tables import User


async def current_user(session: AsyncSession = Depends(db.get_session)) -> User:
    if db.local_user_id is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    user = await session.get(User, db.local_user_id)
    if user is None:
        raise HTTPException(status_code=503, detail="local user missing")
    return user
