"""conftest decides whether the shared test database is stamped ahead of this
checkout by reading revision ids off migration filenames. Keep that true."""

import asyncio
import os
import re
import secrets
import uuid

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app import db
from conftest import _VERSIONS, _local_revisions


def test_local_revisions_match_declared_revisions():
    declared = set()
    for path in _VERSIONS.glob("[0-9]*.py"):
        match = re.search(r'^revision = "([^"]+)"', path.read_text(), re.MULTILINE)
        assert match, f"{path.name} declares no revision"
        declared.add(match.group(1))
    assert declared, "no migrations found"
    assert _local_revisions() == declared


WORKER_COLUMNS = {
    "worker_id", "incarnation", "transport_owner_id", "region", "owner_epoch",
    "lease_id", "lease_expires_at", "grant_nonce", "grant_expires_at", "grant_ready",
    "realtime_slots", "protocol_version", "capabilities", "manifests", "device",
    "memory_mode", "envelope_revision", "execution_envelope", "observations",
    "lifecycle", "last_seen_at", "expires_at", "closed_at", "created_at",
}
# Columns the protocol 6 slice deliberately does not carry yet; later slices
# add them back with the code that reads them.
ABSENT_WORKER_COLUMNS = {
    "measurement_id", "drain_id", "drain_requested_at", "physical_complete",
    "previous_incarnation",
}
WORKER_COLUMNS_0030 = WORKER_COLUMNS | {"next_command_sequence", "acknowledged_floor"}


@pytest.mark.db
def test_0029_upgrade_creates_the_authority_tables_and_downgrade_drops_them(portal_runner):
    # Migrate this run's database first: the teardown above it deletes job
    # rows, and this file may be the first one in a run to reach the database.
    assert portal_runner(db.connect(serving=False)) is True
    admin_url = make_url(os.environ["DATABASE_URL"]).set(database="postgres")
    database = f"p6m_{secrets.token_hex(8)}"
    test_url = admin_url.set(database=database, drivername="postgresql+asyncpg")
    url = test_url.set(drivername="postgresql").render_as_string(hide_password=False)

    async def create_database() -> None:
        connection = await asyncpg.connect(admin_url.render_as_string(hide_password=False))
        try:
            assert not await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = $1)", database
            )
            await connection.execute(f'CREATE DATABASE "{database}"')
        finally:
            await connection.close()

    config = Config(str(_VERSIONS.parents[1] / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        test_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    try:
        asyncio.run(create_database())
        command.upgrade(config, "0029")

        async def verify_upgrade() -> None:
            connection = await asyncpg.connect(url)
            try:
                worker_columns = set(await connection.fetchval(
                    "SELECT array_agg(column_name) FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'worker_connections'"
                ))
                assert WORKER_COLUMNS <= worker_columns
                assert not (ABSENT_WORKER_COLUMNS & worker_columns)
                lease_columns = set(await connection.fetchval(
                    "SELECT array_agg(column_name) FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'scheduler_leases'"
                ))
                assert lease_columns == {"region", "owner_id", "owner_epoch",
                                         "lease_id", "expires_at"}
                indexes = set(await connection.fetchval(
                    "SELECT array_agg(indexname) FROM pg_indexes "
                    "WHERE tablename = 'worker_connections'"
                ))
                assert {"worker_connections_one_current",
                        "worker_connections_ready_region"} <= indexes
                constraints = set(await connection.fetchval(
                    "SELECT array_agg(conname) FROM pg_constraint "
                    "WHERE conrelid = 'worker_connections'::regclass"
                ))
                assert {"worker_connections_lifecycle", "worker_connections_protocol",
                        "worker_connections_epoch",
                        "worker_connections_slots"} <= constraints
                await connection.execute(
                    "INSERT INTO scheduler_leases "
                    "(region, owner_id, owner_epoch, lease_id, expires_at) "
                    "VALUES ('migration-fixture', $1, 1, $2, "
                    "clock_timestamp() + interval '10 seconds')",
                    uuid.uuid4(), uuid.uuid4(),
                )
                # The exact column list register_worker writes must take a row.
                await connection.execute(
                    "INSERT INTO worker_connections "
                    "(worker_id, incarnation, transport_owner_id, region, owner_epoch, "
                    "lease_id, lease_expires_at, grant_nonce, grant_expires_at, "
                    "protocol_version, realtime_slots, capabilities, manifests, device, "
                    "memory_mode, execution_envelope, envelope_revision, lifecycle, "
                    "last_seen_at, expires_at) "
                    "VALUES ('migration-fixture-worker', $1, $2, 'local', 1, $3, "
                    "clock_timestamp() + interval '10 seconds', $4, "
                    "clock_timestamp() + interval '10 seconds', 6, 1, "
                    "'[\"authority_fence\"]'::jsonb, '[{\"id\":\"migration-model\"}]'::jsonb, "
                    "'cpu-test', 'offload', '{}'::jsonb, 1, 'connecting', "
                    "clock_timestamp(), clock_timestamp() + interval '90 seconds')",
                    uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(),
                )
            finally:
                await connection.close()

        asyncio.run(verify_upgrade())
        command.downgrade(config, "0028")

        async def verify_downgrade() -> None:
            connection = await asyncpg.connect(url)
            try:
                assert await connection.fetchval(
                    "SELECT to_regclass('public.scheduler_leases') IS NULL")
                assert await connection.fetchval(
                    "SELECT to_regclass('public.worker_connections') IS NULL")
            finally:
                await connection.close()

        asyncio.run(verify_downgrade())
        command.upgrade(config, "0029")

        async def verify_reupgrade() -> None:
            connection = await asyncpg.connect(url)
            try:
                assert await connection.fetchval(
                    "SELECT count(*) FROM alembic_version WHERE version_num = '0029'") == 1
                assert await connection.fetchval(
                    "SELECT to_regclass('public.worker_connections') IS NOT NULL")
            finally:
                await connection.close()

        asyncio.run(verify_reupgrade())
    finally:
        async def drop_database() -> None:
            connection = await asyncpg.connect(admin_url.render_as_string(hide_password=False))
            try:
                exists = await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = $1)", database)
                if exists:
                    await connection.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
            finally:
                await connection.close()

        asyncio.run(drop_database())
        portal_runner(db.dispose())


@pytest.mark.db
def test_0030_upgrade_adds_command_journal_and_downgrade_drops_it(portal_runner):
    assert portal_runner(db.connect(serving=False)) is True
    admin_url = make_url(os.environ["DATABASE_URL"]).set(database="postgres")
    database = f"p6m_{secrets.token_hex(8)}"
    test_url = admin_url.set(database=database, drivername="postgresql+asyncpg")
    url = test_url.set(drivername="postgresql").render_as_string(hide_password=False)

    async def create_database() -> None:
        connection = await asyncpg.connect(admin_url.render_as_string(hide_password=False))
        try:
            await connection.execute(f'CREATE DATABASE "{database}"')
        finally:
            await connection.close()

    config = Config(str(_VERSIONS.parents[1] / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        test_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    try:
        asyncio.run(create_database())
        command.upgrade(config, "0030")

        async def verify_upgrade() -> None:
            connection = await asyncpg.connect(url)
            try:
                worker_columns = set(await connection.fetchval(
                    "SELECT array_agg(column_name) FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'worker_connections'"
                ))
                assert WORKER_COLUMNS_0030 <= worker_columns
                assert await connection.fetchval(
                    "SELECT to_regclass('public.worker_commands') IS NOT NULL")
                assert await connection.fetchval(
                    "SELECT to_regclass('public.job_attempts') IS NOT NULL")
                job_columns = set(await connection.fetchval(
                    "SELECT array_agg(column_name) FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'jobs'"
                ))
                assert "current_dispatch_sequence" in job_columns
            finally:
                await connection.close()

        asyncio.run(verify_upgrade())
        command.downgrade(config, "0029")

        async def verify_downgrade() -> None:
            connection = await asyncpg.connect(url)
            try:
                assert await connection.fetchval(
                    "SELECT to_regclass('public.worker_commands') IS NULL")
                assert await connection.fetchval(
                    "SELECT to_regclass('public.job_attempts') IS NULL")
                worker_columns = set(await connection.fetchval(
                    "SELECT array_agg(column_name) FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'worker_connections'"
                ))
                assert "next_command_sequence" not in worker_columns
            finally:
                await connection.close()

        asyncio.run(verify_downgrade())
    finally:
        async def drop_database() -> None:
            connection = await asyncpg.connect(admin_url.render_as_string(hide_password=False))
            try:
                exists = await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = $1)", database)
                if exists:
                    await connection.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
            finally:
                await connection.close()

        asyncio.run(drop_database())
        portal_runner(db.dispose())


@pytest.mark.db
def test_0031_upgrade_adds_report_receipts_and_downgrade_drops_them(portal_runner):
    assert portal_runner(db.connect(serving=False)) is True
    admin_url = make_url(os.environ["DATABASE_URL"]).set(database="postgres")
    database = f"p6m_{secrets.token_hex(8)}"
    test_url = admin_url.set(database=database, drivername="postgresql+asyncpg")
    url = test_url.set(drivername="postgresql").render_as_string(hide_password=False)

    async def create_database() -> None:
        connection = await asyncpg.connect(admin_url.render_as_string(hide_password=False))
        try:
            await connection.execute(f'CREATE DATABASE "{database}"')
        finally:
            await connection.close()

    config = Config(str(_VERSIONS.parents[1] / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        test_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    try:
        asyncio.run(create_database())
        command.upgrade(config, "0031")

        async def verify_upgrade() -> None:
            connection = await asyncpg.connect(url)
            try:
                assert await connection.fetchval(
                    "SELECT to_regclass('public.worker_physical_ranges') IS NOT NULL")
                assert await connection.fetchval(
                    "SELECT to_regclass('public.worker_report_receipts') IS NOT NULL")
                attempt_columns = set(await connection.fetchval(
                    "SELECT array_agg(column_name) FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'job_attempts'"
                ))
                assert {"gpu_ms", "frames", "duration_ms", "report_sequence",
                        "terminal_at"} <= attempt_columns
            finally:
                await connection.close()

        asyncio.run(verify_upgrade())
        command.downgrade(config, "0030")

        async def verify_downgrade() -> None:
            connection = await asyncpg.connect(url)
            try:
                assert await connection.fetchval(
                    "SELECT to_regclass('public.worker_physical_ranges') IS NULL")
                assert await connection.fetchval(
                    "SELECT to_regclass('public.worker_report_receipts') IS NULL")
                attempt_columns = set(await connection.fetchval(
                    "SELECT array_agg(column_name) FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'job_attempts'"
                ))
                assert "gpu_ms" not in attempt_columns
            finally:
                await connection.close()

        asyncio.run(verify_downgrade())
    finally:
        async def drop_database() -> None:
            connection = await asyncpg.connect(admin_url.render_as_string(hide_password=False))
            try:
                exists = await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = $1)", database)
                if exists:
                    await connection.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
            finally:
                await connection.close()

        asyncio.run(drop_database())
        portal_runner(db.dispose())
