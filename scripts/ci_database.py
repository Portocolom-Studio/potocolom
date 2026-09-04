"""Which PostgreSQL database a URL names, and the CI rule for simulation.

The self-hosted runner shares one PostgreSQL with local development. Backend
CI already starts its own container. Simulation did not, so a local
`make auth-enable` could redden main (issue #459).
"""

from __future__ import annotations

import asyncio
import os
import re
from urllib.parse import urlsplit

DEVELOPER_DATABASE = "potocolom"
CI_DATABASE = "potocolom_ci"
DEFAULT_URL = "postgresql://potocolom:potocolom@localhost:5432/potocolom"
_NAME = re.compile(r"^[A-Za-z0-9_]+$")


def database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/").split("?")[0]


def refuse_developer_database(*, url: str, ci: bool) -> None:
    """Exit if CI would otherwise use the developer database.

    The workflow sets DATABASE_URL to potocolom_ci. This is the backstop:
    deleting that env var must fail the job, not hit `potocolom`.
    """
    if ci and database_name(url) == DEVELOPER_DATABASE:
        raise SystemExit(
            "simulation in CI must not use the developer database; "
            f"set DATABASE_URL to a database named {CI_DATABASE}"
        )


def prepare_ci_database(url: str) -> None:
    """Create the named database if needed, then clear installation auth state.

    A leftover `accounts` row is what made simulation fail after a local
    auth-enable. Clearing it here is safe because CI never shares this name
    with a developer checkout.
    """
    asyncio.run(_prepare(url))


async def _prepare(url: str) -> None:
    import asyncpg

    parsed = urlsplit(url)
    name = database_name(url)
    if not _NAME.fullmatch(name):
        raise SystemExit(f"database name {name!r} is not usable")
    conn = await asyncpg.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        user=parsed.username,
        password=parsed.password,
        database="postgres",
        timeout=10,
    )
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", name
        )
        if not exists:
            try:
                await conn.execute(f'CREATE DATABASE "{name}"')
            except asyncpg.DuplicateDatabaseError:
                # Another job created it between the catalog check and here.
                pass
    finally:
        await conn.close()
    target = await asyncpg.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        user=parsed.username,
        password=parsed.password,
        database=name,
        timeout=10,
    )
    try:
        if await target.fetchval(
            "SELECT to_regclass($1)", "public.installation_auth_state"
        ) is None:
            return
        await target.execute(
            "UPDATE installation_auth_state SET auth_mode = 'none', "
            "root_key_version = NULL WHERE id = 1"
        )
    finally:
        await target.close()


def ci_from_environ(environ: dict[str, str] | os._Environ[str] = os.environ) -> bool:
    return environ.get("CI", "") != ""
