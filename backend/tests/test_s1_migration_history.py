"""Released populated schemas still upgrade through the current identity migration."""

import asyncio
import json
import os
import secrets
import uuid

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app import db
from conftest import _VERSIONS


@pytest.mark.db
@pytest.mark.parametrize("origin", ["0023", "0028"])
def test_populated_released_schema_upgrades_through_0034(origin, portal_runner):
    assert portal_runner(db.connect(serving=False)) is True
    admin_url = make_url(os.environ["DATABASE_URL"]).set(database="postgres")
    database = f"s1mh_{secrets.token_hex(8)}"
    test_url = admin_url.set(database=database, drivername="postgresql+asyncpg")
    url = test_url.set(drivername="postgresql").render_as_string(hide_password=False)
    account_id = uuid.uuid4()
    job_id = uuid.uuid4()
    model_id = f"migration-{uuid.uuid4()}"

    async def create_database() -> None:
        connection = await asyncpg.connect(admin_url.render_as_string(hide_password=False))
        try:
            await connection.execute(f'CREATE DATABASE "{database}"')
        finally:
            await connection.close()

    async def seed_released_schema() -> None:
        connection = await asyncpg.connect(url)
        try:
            await connection.execute(
                "INSERT INTO users (id,email,role,state) VALUES ($1,$2,'user','active')",
                account_id, f"{account_id}@migration.invalid",
            )
            await connection.execute(
                "INSERT INTO auth_identities (id,user_id,provider,subject,password_hash) "
                "VALUES ($1,$2,'password',$3,'retained-test-hash')",
                uuid.uuid4(), account_id, f"{account_id}@migration.invalid",
            )
            await connection.execute(
                "INSERT INTO auth_factors "
                "(id,user_id,kind,secret_ciphertext,key_version,replace_attempts) "
                "VALUES ($1,$2,'totp',$3,1,3)",
                uuid.uuid4(), account_id, b"retained-factor-ciphertext",
            )
            await connection.execute(
                "INSERT INTO models (id,name,capabilities,parameters_schema,min_vram_gb) "
                "VALUES ($1,'retained model','{}'::jsonb,'{}'::jsonb,8)",
                model_id,
            )
            await connection.execute(
                "INSERT INTO jobs (id,user_id,model_id,params,state,attempt) "
                "VALUES ($1,$2,$3,'{\"seed\":7}'::jsonb,'completed',2)",
                job_id, account_id, model_id,
            )
            if origin == "0028":
                await connection.execute(
                    "INSERT INTO status_banner (id,kind,custom_text,updated_by) "
                    "VALUES (true,'maintenance','retained banner',$1)", account_id,
                )
            assert await connection.fetchval(
                "SELECT to_regclass('worker_connections') IS NULL "
                "AND to_regclass('worker_commands') IS NULL "
                "AND to_regclass('worker_physical_ranges') IS NULL"
            )
        finally:
            await connection.close()

    async def inspect_upgrade() -> None:
        connection = await asyncpg.connect(url)
        try:
            assert await connection.fetchval("SELECT version_num FROM alembic_version") == "0034"
            account = await connection.fetchrow(
                "SELECT email,role,state FROM users WHERE id=$1", account_id,
            )
            assert dict(account) == {
                "email": f"{account_id}@migration.invalid", "role": "user", "state": "active",
            }
            identity = await connection.fetchrow(
                "SELECT provider,subject,password_hash FROM auth_identities WHERE user_id=$1",
                account_id,
            )
            assert dict(identity) == {
                "provider": "password", "subject": f"{account_id}@migration.invalid",
                "password_hash": "retained-test-hash",
            }
            factor = await connection.fetchrow(
                "SELECT secret_ciphertext,replace_attempts FROM auth_factors WHERE user_id=$1",
                account_id,
            )
            assert dict(factor) == {
                "secret_ciphertext": b"retained-factor-ciphertext", "replace_attempts": 3,
            }
            job = await connection.fetchrow(
                "SELECT user_id,model_id,params,state,attempt,current_dispatch_sequence "
                "FROM jobs WHERE id=$1", job_id,
            )
            job_values = dict(job)
            job_values["params"] = json.loads(job_values["params"])
            assert job_values == {
                "user_id": account_id, "model_id": model_id, "params": {"seed": 7},
                "state": "completed", "attempt": 2, "current_dispatch_sequence": 0,
            }
            assert await connection.fetchval(
                "SELECT count(*) FROM worker_connections"
            ) == 0
            assert await connection.fetchval("SELECT count(*) FROM worker_commands") == 0
            assert await connection.fetchval(
                "SELECT count(*) FROM worker_physical_ranges"
            ) == 0
            identity_columns = await connection.fetch(
                "SELECT table_name,column_name,is_nullable FROM information_schema.columns "
                "WHERE table_schema='public' AND "
                "((table_name='worker_commands' AND column_name IN "
                "('account_user_id','subject_id','subject_sequence','ack_identity_hash')) "
                "OR (table_name='worker_physical_ranges' AND column_name='account_user_id'))"
            )
            assert {(row["table_name"], row["column_name"], row["is_nullable"])
                    for row in identity_columns} == {
                ("worker_commands", "account_user_id", "YES"),
                ("worker_commands", "subject_id", "YES"),
                ("worker_commands", "subject_sequence", "YES"),
                ("worker_commands", "ack_identity_hash", "YES"),
                ("worker_physical_ranges", "account_user_id", "YES"),
            }
            if origin == "0028":
                assert await connection.fetchval(
                    "SELECT custom_text FROM status_banner WHERE id=true"
                ) == "retained banner"
        finally:
            await connection.close()

    config = Config(str(_VERSIONS.parents[1] / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url", test_url.render_as_string(hide_password=False).replace("%", "%%")
    )
    try:
        asyncio.run(create_database())
        command.upgrade(config, origin)
        asyncio.run(seed_released_schema())
        command.upgrade(config, "head")
        asyncio.run(inspect_upgrade())
    finally:
        async def drop_database() -> None:
            connection = await asyncpg.connect(admin_url.render_as_string(hide_password=False))
            try:
                exists = await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname=$1)", database
                )
                if exists:
                    await connection.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
            finally:
                await connection.close()

        asyncio.run(drop_database())
        portal_runner(db.dispose())
