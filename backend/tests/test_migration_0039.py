"""0039 adds the billing role and the one-subject binding (issue #1003)."""

import asyncio
import os
import uuid

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app import db
from conftest import _VERSIONS


@pytest.fixture(autouse=True)
def _run_database_migrated(portal_runner):
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
        self.name = f"m38_{uuid.uuid4().hex[:16]}"
        self.url = self.admin_url.set(database=self.name, drivername="postgresql") \
            .render_as_string(hide_password=False)
        self.alembic_url = self.admin_url.set(
            database=self.name, drivername="postgresql+asyncpg") \
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


async def _seed(connection) -> None:
    member = uuid.uuid4()
    viewer = uuid.uuid4()
    await connection.execute(
        "INSERT INTO users (id, email, role, state, mail_verified) VALUES "
        "($1, 'member@example.com', 'user', 'active', true), "
        "($2, 'viewer@example.com', 'viewer', 'active', false)",
        member, viewer)
    await connection.execute(
        "INSERT INTO invitations (id, email, role, token_hash, expires_at) "
        "VALUES ($1, 'guest@example.com', 'user', $2, now() + interval '1 day')",
        uuid.uuid4(), b"invite-hash-bytes")


@pytest.mark.db
def test_populated_rows_keep_their_roles_and_billing_is_a_role_after_upgrade():
    with Scratch() as scratch:
        command.upgrade(_config(scratch.alembic_url), "0038")
        scratch.run(_seed)
        command.upgrade(_config(scratch.alembic_url), "head")

        async def roles(connection):
            users = await connection.fetch(
                "SELECT email, role FROM users ORDER BY email")
            invites = await connection.fetch("SELECT role FROM invitations")
            return [tuple(row) for row in users], [row["role"] for row in invites]

        assert scratch.run(roles) == (
            [("member@example.com", "user"), ("viewer@example.com", "viewer")],
            ["user"],
        )

        delegate = uuid.uuid4()

        async def bind_member(connection):
            member = await connection.fetchval(
                "SELECT id FROM users WHERE email = 'member@example.com'")
            await connection.execute(
                "INSERT INTO users (id, email, role, state, mail_verified) "
                "VALUES ($1, 'finance@example.com', 'billing', 'active', false)",
                delegate)
            await connection.execute(
                "INSERT INTO billing_bindings (delegate_user_id, subject_user_id) "
                "VALUES ($1, $2)",
                delegate, member)
            same = False
            try:
                await connection.execute(
                    "INSERT INTO billing_bindings (delegate_user_id, subject_user_id) "
                    "VALUES ($1, $1)",
                    delegate)
            except asyncpg.CheckViolationError:
                same = True
            return same

        assert scratch.run(bind_member) is True

        with pytest.raises(Exception, match="billing"):
            command.downgrade(_config(scratch.alembic_url), "0038")

        async def table_still_there(connection):
            return await connection.fetchval(
                "SELECT to_regclass('public.billing_bindings')::text")

        assert scratch.run(table_still_there) == "billing_bindings"

        async def clear_billing(connection):
            await connection.execute("DELETE FROM billing_bindings")
            await connection.execute(
                "UPDATE users SET role = 'user' WHERE role = 'billing'")
            await connection.execute(
                "UPDATE invitations SET role = 'viewer' WHERE role = 'billing'")

        scratch.run(clear_billing)

        async def invite_billing(connection):
            await connection.execute(
                "UPDATE invitations SET role = 'billing' WHERE email = 'guest@example.com'")

        scratch.run(invite_billing)
        with pytest.raises(Exception, match="billing"):
            command.downgrade(_config(scratch.alembic_url), "0038")
        scratch.run(clear_billing)
        command.downgrade(_config(scratch.alembic_url), "0038")

        async def billing_role_rejected(connection):
            try:
                await connection.execute(
                    "INSERT INTO users (id, email, role, state, mail_verified) "
                    "VALUES ($1, 'again@example.com', 'billing', 'active', false)",
                    uuid.uuid4())
            except asyncpg.CheckViolationError:
                return True
            return False

        assert scratch.run(billing_role_rejected) is True


@pytest.mark.db
def test_a_fresh_database_accepts_a_billing_binding():
    with Scratch() as scratch:
        command.upgrade(_config(scratch.alembic_url), "head")
        delegate = uuid.uuid4()
        subject = uuid.uuid4()

        async def seed(connection):
            await connection.execute(
                "INSERT INTO users (id, email, role, state, mail_verified) VALUES "
                "($1, 'finance@example.com', 'billing', 'active', false), "
                "($2, 'payer@example.com', 'admin', 'active', true)",
                delegate, subject)
            await connection.execute(
                "INSERT INTO billing_bindings (delegate_user_id, subject_user_id) "
                "VALUES ($1, $2)",
                delegate, subject)
            return await connection.fetchval(
                "SELECT count(*) FROM billing_bindings")

        assert scratch.run(seed) == 1
