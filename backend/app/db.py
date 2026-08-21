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
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.keyring import KeyRingError, get_key_ring
from app.settings import get_settings
from app.tables import InstallationAuthState, User

logger = logging.getLogger("potocolom.db")

LOCAL_USER_EMAIL = "local@localhost"
MIN_POSTGRES_VERSION = (13, 0)
ACCOUNTS_STARTUP_LOCK_KEY = 184467

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


def _postgres_version_supported(version: tuple[int, ...]) -> bool:
    return version >= MIN_POSTGRES_VERSION


async def _postgres_version(database_url: str) -> tuple[int, ...]:
    check_engine = create_async_engine(async_url(database_url), poolclass=NullPool)
    try:
        async with check_engine.connect() as connection:
            version = connection.dialect.server_version_info
            if version is None:
                raise RuntimeError("could not determine PostgreSQL server version")
            return version
    finally:
        await check_engine.dispose()


async def connect() -> bool:
    global engine, session_factory, local_user_id
    settings = get_settings()
    try:
        postgres_version = await _postgres_version(settings.database_url)
        if not _postgres_version_supported(postgres_version):
            minimum = ".".join(str(part) for part in MIN_POSTGRES_VERSION)
            found = ".".join(str(part) for part in postgres_version)
            raise RuntimeError(
                f"PostgreSQL {minimum} or newer is required; found PostgreSQL {found}"
            )
        # Alembic's env.py runs its own event loop, so migrate off this one.
        await asyncio.to_thread(_migrate, settings.database_url)
    except Exception as error:
        logger.warning("database unavailable (%s); generations and history are disabled", error)
        return False
    engine = create_async_engine(async_url(settings.database_url), pool_size=5, max_overflow=10)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    await validate_startup_auth_mode(settings.auth_mode)
    await validate_startup_key_ring()
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
            user = User(email=LOCAL_USER_EMAIL, role="admin")
            session.add(user)
            await session.commit()
        elif user.role != "admin":
            user.role = "admin"
            await session.commit()
        return user.id


async def get_session() -> AsyncIterator[AsyncSession]:
    if session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with session_factory() as session:
        yield session


async def require_account_dependencies() -> None:
    if engine is None or session_factory is None:
        raise HTTPException(status_code=503, detail="account dependencies unavailable")


async def read_installation_auth_mode() -> str | None:
    if session_factory is None:
        raise RuntimeError("database unavailable")
    async with session_factory() as session:
        state = await session.get(InstallationAuthState, 1)
        return state.auth_mode if state is not None else None


async def enable_accounts_mode(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO installation_auth_state (id, auth_mode) "
                    "VALUES (1, 'accounts') ON CONFLICT (id) DO NOTHING"
                )
            )


async def read_installation_root_key_version() -> int | None:
    if session_factory is None:
        raise RuntimeError("database unavailable")
    async with session_factory() as session:
        state = await session.get(InstallationAuthState, 1)
        return state.root_key_version if state is not None else None


async def validate_startup_key_ring() -> None:
    """PostgreSQL is the authority on the version this installation writes with.

    A ring that no longer holds it cannot read anything written since, so
    refusing to start is the only honest outcome: coming up would answer with
    an installation that silently lost every encrypted secret.
    """
    recorded = await read_installation_root_key_version()
    if recorded is None:
        return
    try:
        ring = get_key_ring()
    except KeyRingError as error:
        raise RuntimeError(f"root key ring is unusable: {error}") from error
    if recorded not in ring.versions:
        raise RuntimeError(f"root key version {recorded} is missing from the key ring")


async def validate_startup_auth_mode(configured: str) -> None:
    persisted = await read_installation_auth_mode()
    if configured == "accounts" and persisted != "accounts":
        raise RuntimeError("accounts mode requires explicit installation enable")
    if configured == "none" and persisted == "accounts":
        raise RuntimeError("accounts installation cannot start in none mode")


@asynccontextmanager
async def accounts_startup_lock(asyncpg_connection) -> AsyncIterator[None]:
    acquired = await asyncpg_connection.fetchval(
        "SELECT pg_try_advisory_lock($1::bigint)", ACCOUNTS_STARTUP_LOCK_KEY
    )
    if not acquired:
        raise RuntimeError("another accounts startup is in progress")
    try:
        yield
    finally:
        await asyncpg_connection.fetchval(
            "SELECT pg_advisory_unlock($1::bigint)", ACCOUNTS_STARTUP_LOCK_KEY
        )
