"""0035 records the installation's auth mode as a decision (issue #792).

Each starting state gets its own database, migrated to 0034 and seeded, so the
upgrade is the only thing that changes between the three answers.
"""

import asyncio
import os
import secrets

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app import db
from conftest import _VERSIONS


@pytest.fixture(autouse=True)
def _run_database_migrated(portal_runner):
    # The cleanup after each db test reads this run's own database, which has
    # to be at head before that read, as in test_migration_0033.
    assert portal_runner(db.connect(serving=False)) is True
    portal_runner(db.dispose())


def _config(url: str) -> Config:
    config = Config(str(_VERSIONS.parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


class Scratch:
    """One throwaway database, created on this run's server and dropped after."""

    def __init__(self) -> None:
        self.admin_url = make_url(os.environ["DATABASE_URL"]).set(database="postgres")
        self.name = f"m35_{secrets.token_hex(8)}"
        # asyncpg for the test's own connections; alembic runs env.py on the
        # asyncpg driver too, as db._migrate hands it.
        self.url = self.admin_url.set(database=self.name, drivername="postgresql") \
            .render_as_string(hide_password=False)
        self.alembic_url = self.admin_url.set(database=self.name, drivername="postgresql+asyncpg") \
            .render_as_string(hide_password=False)

    async def _admin(self, statement: str) -> None:
        connection = await asyncpg.connect(self.admin_url.render_as_string(hide_password=False))
        try:
            await connection.execute(statement)
        finally:
            await connection.close()

    def __enter__(self) -> "Scratch":
        asyncio.run(self._admin(f'CREATE DATABASE "{self.name}"'))
        return self

    def __exit__(self, *exc) -> None:
        asyncio.run(self._admin(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)'))

    def run(self, coroutine_factory):
        async def go():
            connection = await asyncpg.connect(self.url)
            try:
                return await coroutine_factory(connection)
            finally:
                await connection.close()

        return asyncio.run(go())


def _mode():
    async def read(connection):
        rows = await connection.fetch("SELECT id, auth_mode, root_key_version "
                                      "FROM installation_auth_state ORDER BY id")
        return [tuple(row) for row in rows]

    return read


@pytest.mark.db
def test_an_empty_database_is_unclaimed_after_the_upgrade():
    with Scratch() as scratch:
        command.upgrade(_config(scratch.alembic_url), "0034")
        command.upgrade(_config(scratch.alembic_url), "head")
        assert scratch.run(_mode()) == [(1, "unclaimed", None)]


@pytest.mark.db
def test_an_install_with_people_is_decided_none_so_an_upgrade_never_shows_setup():
    with Scratch() as scratch:
        command.upgrade(_config(scratch.alembic_url), "0034")

        async def seed(connection):
            # The implicit local user every pre-#792 install has, with no row in
            # installation_auth_state: that install was none by absence.
            await connection.execute(
                "INSERT INTO users (id, email, role, state, mail_verified) "
                "VALUES (gen_random_uuid(), 'local@localhost', 'admin', 'active', false)")

        scratch.run(seed)
        command.upgrade(_config(scratch.alembic_url), "head")
        assert scratch.run(_mode()) == [(1, "none", None)]


@pytest.mark.db
def test_an_existing_accounts_row_keeps_its_mode_and_key_version():
    with Scratch() as scratch:
        command.upgrade(_config(scratch.alembic_url), "0034")

        async def seed(connection):
            await connection.execute(
                "INSERT INTO installation_auth_state (id, auth_mode, root_key_version) "
                "VALUES (1, 'accounts', 3)")

        scratch.run(seed)
        command.upgrade(_config(scratch.alembic_url), "head")
        assert scratch.run(_mode()) == [(1, "accounts", 3)]


@pytest.mark.db
def test_the_mode_column_refuses_a_value_outside_the_three():
    with Scratch() as scratch:
        command.upgrade(_config(scratch.alembic_url), "head")

        async def write(connection):
            await connection.execute(
                "UPDATE installation_auth_state SET auth_mode = 'local' WHERE id = 1")

        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            scratch.run(write)


@pytest.mark.db
def test_downgrade_removes_the_new_decisions_and_keeps_accounts():
    """Before this revision a missing row meant none, so unclaimed and none rows
    go, and an accounts row stays because the earlier code reads it."""
    with Scratch() as scratch:
        command.upgrade(_config(scratch.alembic_url), "head")

        async def keep_accounts(connection):
            await connection.execute(
                "UPDATE installation_auth_state SET auth_mode = 'accounts', "
                "root_key_version = 1 WHERE id = 1")

        scratch.run(keep_accounts)
        command.downgrade(_config(scratch.alembic_url), "0034")

        async def rows(connection):
            return [tuple(row) for row in await connection.fetch(
                "SELECT auth_mode FROM installation_auth_state")]

        assert scratch.run(rows) == [("accounts",)]
        command.upgrade(_config(scratch.alembic_url), "head")
        assert scratch.run(_mode()) == [(1, "accounts", 1)]
