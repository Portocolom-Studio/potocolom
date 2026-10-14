"""Migration checks for preserving verifiable command identity and erasure."""

import asyncio
import hashlib
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
from app import keyring as keyring_module
from app.keyring import KeyRing
from conftest import _VERSIONS


@pytest.mark.db
def test_0034_preserves_only_verifiable_legacy_command_identity(portal_runner, monkeypatch):
    assert portal_runner(db.connect(serving=False)) is True
    admin_url = make_url(os.environ["DATABASE_URL"]).set(database="postgres")
    database = f"p6i_{secrets.token_hex(8)}"
    test_url = admin_url.set(database=database, drivername="postgresql+asyncpg")
    url = test_url.set(drivername="postgresql").render_as_string(hide_password=False)
    worker_id = f"legacy-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    lease_id = uuid.uuid4()
    grant_nonce = uuid.uuid4()
    command_id = uuid.uuid4()
    job_id = uuid.uuid4()
    unknown_command_id = uuid.uuid4()
    unknown_job_id = uuid.uuid4()
    invalid_command_id = uuid.uuid4()
    oversized_command_id = uuid.uuid4()
    user_id = uuid.uuid4()
    model_id = f"migration-model-{uuid.uuid4()}"
    range_id = uuid.uuid4()
    attempt_command_id = uuid.uuid4()
    pending_command_id = uuid.uuid4()
    pending_job_id = uuid.uuid4()
    body_hash = "a" * 64
    unknown_body_hash = "b" * 64
    pending_body_hash = "c" * 64
    ring = KeyRing([(1, b"m" * 32)])
    # Six readable, hash- and AAD-authenticated pending bodies whose subject
    # field is a non-string (int/bool/dict): _identity must treat each as no
    # identity rather than raising, so every row is erased without a guess.
    malformed_pending = []
    for sequence, kind, subject in (
        (10, "dispatch_job", 42), (11, "dispatch_job", True), (12, "dispatch_job", {}),
        (13, "update_session", 42), (14, "update_session", True),
        (15, "update_session", {}),
    ):
        command_key = uuid.uuid4()
        body = {
            "type": kind, "worker_id": worker_id,
            "incarnation": str(incarnation), "region": "migration-region",
            "owner_epoch": 1, "lease_id": str(lease_id),
            "lease_expires_at": "2026-12-31T00:00:00Z",
            "grant_nonce": str(grant_nonce), "command_sequence": sequence,
            "command_id": str(command_key),
        }
        if kind == "dispatch_job":
            body["job_id"] = subject
            body["dispatch_sequence"] = 4
        else:
            body["session_id"] = subject
            body["attempt_id"] = str(uuid.uuid4())
            body["control_generation"] = 1
        digest = hashlib.sha256(json.dumps(
            body, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode()).hexdigest()
        body["body_hash"] = digest
        aad = json.dumps({
            "body_hash": digest, "command_id": str(command_key),
            "command_sequence": sequence, "incarnation": str(incarnation),
            "worker_id": worker_id,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        ciphertext = ring.encrypt(
            "worker-command",
            json.dumps(body, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False).encode(),
            aad,
        )
        malformed_pending.append((sequence, kind, command_key, digest, ciphertext))
    ack = {
        "type": "command_ack",
        "worker_id": worker_id,
        "incarnation": str(incarnation),
        "region": "migration-region",
        "owner_epoch": 1,
        "lease_id": str(lease_id),
        "lease_expires_at": "2026-12-31T00:00:00Z",
        "grant_nonce": str(grant_nonce),
        "command_sequence": 1,
        "command_id": str(command_id),
        "body_hash": body_hash,
        "job_id": str(job_id),
        "dispatch_sequence": 4,
        "status": "accepted",
    }
    ack_body = json.dumps(ack, sort_keys=True, separators=(",", ":")).encode()
    invalid_ack = {
        **ack, "command_sequence": 3, "command_id": str(invalid_command_id),
        "body_hash": "c" * 64, "unexpected": "field",
    }
    oversized_ack = {
        **ack, "command_sequence": 4, "command_id": str(oversized_command_id),
        "body_hash": "d" * 64, "unexpected": "x" * (16 * 1024),
    }
    malformed_acks = [
        {**ack, "command_sequence": 5, "command_id": str(uuid.uuid4()),
         "body_hash": "e" * 64, "lease_id": "not-a-uuid"},
        {**ack, "command_sequence": 6, "command_id": str(uuid.uuid4()),
         "body_hash": "f" * 64, "lease_expires_at": "not-a-date"},
        {**ack, "command_sequence": 7, "command_id": str(uuid.uuid4()),
         "body_hash": "1" * 64, "owner_epoch": True},
        {**ack, "command_sequence": 8, "command_id": str(uuid.uuid4()),
         "body_hash": "2" * 64, "dispatch_sequence": 2**63},
    ]
    pending_body = {
        "type": "dispatch_job", "worker_id": worker_id,
        "incarnation": str(incarnation), "region": "migration-region",
        "owner_epoch": 1, "lease_id": str(lease_id),
        "lease_expires_at": "2026-12-31T00:00:00Z",
        "grant_nonce": str(grant_nonce), "command_sequence": 9,
        "command_id": str(pending_command_id), "body_hash": pending_body_hash,
        "dispatch_sequence": 4, "dispatch_token": "dispatch-token",
        "job_id": str(pending_job_id), "model_id": "legacy-model",
        "params": {}, "upload": {}, "work_budget": {},
    }
    pending_body_hash = hashlib.sha256(json.dumps(
        {key: value for key, value in pending_body.items() if key != "body_hash"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode()).hexdigest()
    pending_body["body_hash"] = pending_body_hash
    aad = json.dumps({
        "body_hash": pending_body_hash, "command_id": str(pending_command_id),
        "command_sequence": 9, "incarnation": str(incarnation), "worker_id": worker_id,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    pending_ciphertext = ring.encrypt(
        "worker-command",
        json.dumps(pending_body, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False, allow_nan=False).encode(),
        aad,
    )

    async def create_database() -> None:
        connection = await asyncpg.connect(admin_url.render_as_string(hide_password=False))
        try:
            await connection.execute(f'CREATE DATABASE "{database}"')
        finally:
            await connection.close()

    async def seed_legacy_rows() -> None:
        connection = await asyncpg.connect(url)
        try:
            await connection.execute(
                "INSERT INTO worker_connections "
                "(worker_id, incarnation, transport_owner_id, region, owner_epoch, lease_id, "
                "lease_expires_at, grant_nonce, grant_expires_at, realtime_slots, protocol_version, "
                "capabilities, manifests, lifecycle, last_seen_at, expires_at, "
                "next_command_sequence, acknowledged_floor) VALUES ($1,$2,$3,$4,1,$5,"
                "clock_timestamp() + interval '1 day',$6,clock_timestamp() + interval '1 day',"
                "1,6,'[]'::jsonb,'[]'::jsonb,'ready',clock_timestamp(),"
                "clock_timestamp() + interval '1 day',16,0)",
                worker_id, incarnation, uuid.uuid4(), "migration-region", lease_id, grant_nonce,
            )
            await connection.execute(
                "INSERT INTO users (id,email,role,state) VALUES ($1,$2,'user','active')",
                user_id, f"{user_id}@migration.invalid",
            )
            await connection.execute(
                "INSERT INTO models (id,name,capabilities,parameters_schema,min_vram_gb) "
                "VALUES ($1,'legacy','{}'::jsonb,'{}'::jsonb,1)", model_id,
            )
            await connection.execute(
                "INSERT INTO jobs (id,user_id,model_id,params,state,attempt) "
                "VALUES ($1,$2,$3,'{}'::jsonb,'running',1)", pending_job_id, user_id, model_id,
            )
            await connection.execute(
                "INSERT INTO worker_commands "
                "(worker_id, incarnation, command_sequence, command_id, kind, body_hash, "
                "key_version, encrypted_body, complete_bytes, state, acknowledged_at, "
                "ack_status, ack_body) VALUES ($1,$2,1,$3,'dispatch_job',$4,NULL,NULL,"
                "0,'acknowledged',clock_timestamp(),'accepted',$5)",
                worker_id, incarnation, command_id, body_hash, ack_body,
            )
            await connection.execute(
                "INSERT INTO worker_commands "
                "(worker_id, incarnation, command_sequence, command_id, kind, body_hash, "
                "key_version, encrypted_body, complete_bytes, state) "
                "VALUES ($1,$2,2,$3,'dispatch_job',$4,2,$5,16,'pending')",
                worker_id, incarnation, unknown_command_id, unknown_body_hash, b"unreadable-ciphertext",
            )
            for sequence, command_key, body in (
                (3, invalid_command_id, invalid_ack),
                (4, oversized_command_id, oversized_ack),
                *((body["command_sequence"], uuid.UUID(body["command_id"]), body)
                  for body in malformed_acks),
            ):
                await connection.execute(
                    "INSERT INTO worker_commands "
                    "(worker_id, incarnation, command_sequence, command_id, kind, body_hash, "
                    "key_version, encrypted_body, complete_bytes, state, acknowledged_at, "
                    "ack_status, ack_body) VALUES ($1,$2,$3,$4,'dispatch_job',$5,NULL,NULL,"
                    "0,'acknowledged',clock_timestamp(),'accepted',$6)",
                    worker_id, incarnation, sequence, command_key, body["body_hash"],
                    json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
                )
            await connection.execute(
                "INSERT INTO worker_commands "
                "(worker_id, incarnation, command_sequence, command_id, kind, body_hash, "
                "key_version, encrypted_body, complete_bytes, state) "
                "VALUES ($1,$2,9,$3,'dispatch_job',$4,1,$5,$6,'pending')",
                worker_id, incarnation, pending_command_id, pending_body_hash,
                pending_ciphertext, len(pending_ciphertext),
            )
            for sequence, kind, command_key, digest, ciphertext in malformed_pending:
                await connection.execute(
                    "INSERT INTO worker_commands "
                    "(worker_id, incarnation, command_sequence, command_id, kind, body_hash, "
                    "key_version, encrypted_body, complete_bytes, state) "
                    "VALUES ($1,$2,$3,$4,$5,$6,1,$7,$8,'pending')",
                    worker_id, incarnation, sequence, command_key, kind, digest,
                    ciphertext, len(ciphertext),
                )
            await connection.execute(
                "INSERT INTO worker_physical_ranges "
                "(range_id, worker_id, incarnation, region, owner_epoch, kind, subject_id, "
                "dispatch_sequence, state, report_sequence, report_floor, report_hash, gpu_ms, frames, "
                "duration_ms) VALUES ($1,$2,$3,'migration-region',1,'job',$4,4,'outstanding',"
                "7,3,$5,1200,5,42)",
                range_id, worker_id, incarnation, pending_job_id, "3" * 64,
            )
            await connection.execute(
                "INSERT INTO job_attempts "
                "(job_id,dispatch_sequence,command_id,worker_id,incarnation,owner_epoch,state,"
                "dispatch_token_hash,range_id,gpu_ms,frames,duration_ms,report_sequence) "
                "VALUES ($1,4,$2,$3,$4,1,'dispatching',$5,$6,1200,5,42,7)",
                pending_job_id, attempt_command_id, worker_id, incarnation,
                hashlib.sha256(b"dispatch-token").hexdigest(), range_id,
            )
            await connection.execute(
                "INSERT INTO worker_report_receipts "
                "(range_id,report_sequence,report_hash,gpu_ms,frames,duration_ms,ack_body) "
                "VALUES ($1,7,$2,1200,5,42,$3)",
                range_id, "3" * 64, b"report-ack",
            )
        finally:
            await connection.close()

    async def inspect_upgrade(*, expect_pending_identity: bool = True) -> None:
        connection = await asyncpg.connect(url)
        try:
            known = await connection.fetchrow(
                "SELECT state, account_user_id, subject_id, subject_sequence, ack_identity_hash, "
                "complete_bytes, ack_body FROM worker_commands WHERE worker_id=$1 "
                "AND incarnation=$2 AND command_sequence=1", worker_id, incarnation,
            )
            assert known["state"] == "acknowledged"
            assert known["account_user_id"] is None
            assert known["subject_id"] == job_id
            assert known["subject_sequence"] == 4
            assert len(known["ack_identity_hash"]) == 64
            assert known["complete_bytes"] == 0
            assert known["ack_body"] == ack_body
            erased = await connection.fetchrow(
                "SELECT state, account_user_id, subject_id, ack_identity_hash, encrypted_body, "
                "key_version, complete_bytes, ack_body FROM worker_commands WHERE worker_id=$1 "
                "AND incarnation=$2 AND command_sequence=2", worker_id, incarnation,
            )
            assert erased["state"] == "erased"
            assert erased["account_user_id"] is None
            assert erased["subject_id"] is None
            assert erased["ack_identity_hash"] is None
            assert erased["encrypted_body"] is None
            assert erased["key_version"] is None
            assert erased["complete_bytes"] == 0
            assert erased["ack_body"] is None
            for sequence in (3, 4, 5, 6, 7, 8):
                invalid = await connection.fetchrow(
                    "SELECT state, subject_id, ack_identity_hash, ack_body "
                    "FROM worker_commands WHERE worker_id=$1 AND incarnation=$2 "
                    "AND command_sequence=$3", worker_id, incarnation, sequence,
                )
                assert invalid["state"] == "erased"
                assert invalid["subject_id"] is None
                assert invalid["ack_identity_hash"] is None
                assert invalid["ack_body"] is None
            connection_row = await connection.fetchrow(
                "SELECT acknowledged_floor, next_command_sequence, closed_at FROM worker_connections "
                "WHERE worker_id=$1 AND incarnation=$2", worker_id, incarnation,
            )
            assert connection_row["acknowledged_floor"] == 0
            assert connection_row["closed_at"] is not None
            assert connection_row["next_command_sequence"] == 16
            readable = await connection.fetchrow(
                "SELECT state, account_user_id, subject_id, subject_sequence, ack_identity_hash, "
                "encrypted_body, key_version, complete_bytes FROM worker_commands "
                "WHERE worker_id=$1 AND incarnation=$2 AND command_sequence=9",
                worker_id, incarnation,
            )
            assert readable["state"] == "erased"
            assert readable["account_user_id"] is None
            if expect_pending_identity:
                assert readable["subject_id"] == pending_job_id
                assert readable["subject_sequence"] == 4
                assert len(readable["ack_identity_hash"]) == 64
            else:
                assert readable["subject_id"] is None
                assert readable["subject_sequence"] is None
                assert readable["ack_identity_hash"] is None
            assert readable["encrypted_body"] is None
            assert readable["key_version"] is None
            assert readable["complete_bytes"] == 0
            assert await connection.fetchval(
                "SELECT count(*) FROM worker_commands WHERE worker_id=$1 AND incarnation=$2 "
                "AND command_sequence BETWEEN 3 AND 8 AND state='erased' "
                "AND subject_id IS NULL AND ack_identity_hash IS NULL", worker_id, incarnation,
            ) == 6
            for sequence in (10, 11, 12, 13, 14, 15):
                malformed = await connection.fetchrow(
                    "SELECT state, account_user_id, subject_id, subject_sequence, "
                    "ack_identity_hash, encrypted_body, key_version, complete_bytes, ack_body "
                    "FROM worker_commands WHERE worker_id=$1 AND incarnation=$2 "
                    "AND command_sequence=$3", worker_id, incarnation, sequence,
                )
                assert malformed["state"] == "erased"
                assert malformed["account_user_id"] is None
                assert malformed["subject_id"] is None
                assert malformed["subject_sequence"] is None
                assert malformed["ack_identity_hash"] is None
                assert malformed["encrypted_body"] is None
                assert malformed["key_version"] is None
                assert malformed["complete_bytes"] == 0
                assert malformed["ack_body"] is None
            physical = await connection.fetchrow(
                "SELECT account_user_id, report_sequence, report_floor, report_hash, gpu_ms, "
                "frames, duration_ms FROM worker_physical_ranges WHERE range_id=$1", range_id,
            )
            assert dict(physical) == {
                "account_user_id": None, "report_sequence": 7, "report_floor": 3,
                "report_hash": "3" * 64, "gpu_ms": 1200, "frames": 5, "duration_ms": 42,
            }
            attempt = await connection.fetchrow(
                "SELECT state, report_sequence, gpu_ms, frames, duration_ms, range_id "
                "FROM job_attempts WHERE job_id=$1 AND dispatch_sequence=4", pending_job_id,
            )
            assert dict(attempt) == {
                "state": "dispatching", "report_sequence": 7, "gpu_ms": 1200,
                "frames": 5, "duration_ms": 42, "range_id": range_id,
            }
            assert await connection.fetchval(
                "SELECT count(*) FROM worker_report_receipts WHERE range_id=$1", range_id,
            ) == 1
            range_owner = await connection.fetchval(
                "SELECT account_user_id FROM worker_physical_ranges WHERE subject_id=$1",
                unknown_job_id,
            )
            assert range_owner is None
        finally:
            await connection.close()

    config = Config(str(_VERSIONS.parents[1] / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url", test_url.render_as_string(hide_password=False).replace("%", "%%")
    )
    try:
        asyncio.run(create_database())
        command.upgrade(config, "0033")
        asyncio.run(seed_legacy_rows())
        monkeypatch.setattr(keyring_module, "get_key_ring", lambda: ring)
        command.upgrade(config, "0034")
        asyncio.run(inspect_upgrade())
        command.downgrade(config, "0033")
        command.upgrade(config, "0034")
        asyncio.run(inspect_upgrade(expect_pending_identity=False))
    finally:
        async def drop_database() -> None:
            connection = await asyncpg.connect(admin_url.render_as_string(hide_password=False))
            try:
                exists = await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = $1)", database
                )
                if exists:
                    await connection.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
            finally:
                await connection.close()

        asyncio.run(drop_database())
        portal_runner(db.dispose())
