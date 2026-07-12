"""Database engine, startup migration and the single local user.

Self-hosted installs migrate automatically on startup (docs/decisions.md); the
cloud profile will run the same migrations as a gated deploy step instead.

Startup tolerates an unreachable database: the API comes up degraded (realtime
relay works, anything touching rows answers 503) rather than flapping the load
balancer health check, which answers from process state only.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.settings import get_settings
from app.tables import User

logger = logging.getLogger("potocolom.db")

LOCAL_USER_EMAIL = "local@localhost"

engine: AsyncEngine | None = None
session_factory: async_sessionmaker[AsyncSession] | None = None
local_user_id: uuid.UUID | None = None


def async_url(url: str) -> str:
    """DATABASE_URL is plain postgresql://; SQLAlchemy async needs the driver spelled out."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _migrate(database_url: str) -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", async_url(database_url))
    command.upgrade(config, "head")


async def connect() -> bool:
    global engine, session_factory, local_user_id
    settings = get_settings()
    try:
        # Alembic's env.py runs its own event loop, so migrate off this one.
        await asyncio.to_thread(_migrate, settings.database_url)
    except Exception as error:
        logger.warning("database unavailable (%s); generations and history are disabled", error)
        return False
    # No connection pool: asyncpg connections are bound to the event loop that
    # created them, and the test client legitimately drives requests and
    # websockets on separate loops. A single-process self-host loses little;
    # pool tuning belongs to the cloud profile work.
    engine = create_async_engine(async_url(settings.database_url), poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    local_user_id = await _ensure_local_user(session_factory)
    return True


async def dispose() -> None:
    global engine, session_factory, local_user_id
    if engine is not None:
        await engine.dispose()
    engine = None
    session_factory = None
    local_user_id = None


async def _ensure_local_user(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """AUTH_MODE=none maps every request to one local user (docs/blueprint.md)."""
    async with factory() as session:
        user = (
            await session.execute(select(User).where(User.email == LOCAL_USER_EMAIL))
        ).scalar_one_or_none()
        if user is None:
            user = User(email=LOCAL_USER_EMAIL)
            session.add(user)
            await session.commit()
        return user.id


async def get_session() -> AsyncIterator[AsyncSession]:
    if session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with session_factory() as session:
        yield session
