"""Retain original command identity after command bodies are erased.

Revision ID: 0034
Revises: 0033
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

_COMMON = (
    "worker_id", "incarnation", "region", "owner_epoch", "lease_id",
    "lease_expires_at", "grant_nonce", "command_sequence", "command_id", "body_hash",
)
_SUBJECTS = {
    "open_session": ("session_id", "attempt_id", "control_generation"),
    "update_session": ("session_id", "attempt_id", "control_generation"),
    "close_session": ("session_id", "attempt_id", "control_generation"),
    "dispatch_job": ("job_id", "dispatch_sequence"),
    "cancel_job": ("job_id", "dispatch_sequence"),
}
_REFUSALS = (
    "authority_expired", "authority_mismatch", "attempt_stale", "revision_stale",
    "unsupported_control", "out_of_memory", "model_unavailable", "invalid_params",
    "capacity_unavailable", "dependency_unavailable", "checkpoint_incompatible",
)
_MAX_SEQUENCE = 2**63 - 1
_RFC3339 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z\Z"
)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _projection(kind: str, value: dict) -> dict | None:
    subjects = _SUBJECTS.get(kind)
    if subjects is None or value.get("type") not in {kind, "command_ack"}:
        return None
    names = _COMMON + subjects
    if any(name not in value for name in names):
        return None
    projection = {"ack_variant": "session" if kind.endswith("session") else "job",
                  "command_kind": kind}
    projection.update((name, value[name]) for name in names)
    return projection


def _identity(kind: str, value: dict) -> tuple[uuid.UUID, int, str] | None:
    projection = _projection(kind, value)
    if projection is None:
        return None
    try:
        raw_subject = value["session_id"] if kind.endswith("session") else value["job_id"]
        if not isinstance(raw_subject, str):
            return None
        subject = uuid.UUID(raw_subject)
        sequence = value["control_generation"] if kind.endswith("session") else value["dispatch_sequence"]
        if (not isinstance(sequence, int) or isinstance(sequence, bool)
                or not 1 <= sequence <= _MAX_SEQUENCE):
            return None
        digest = hashlib.sha256(_canonical(projection)).hexdigest()
        return subject, sequence, digest
    except (KeyError, TypeError, ValueError, UnicodeError):
        return None


def _legacy_plaintext(row) -> dict | None:
    try:
        from app.keyring import get_key_ring

        aad = _canonical({
            "body_hash": row.body_hash,
            "command_id": str(row.command_id),
            "command_sequence": row.command_sequence,
            "incarnation": str(row.incarnation),
            "worker_id": row.worker_id,
        })
        plaintext = get_key_ring().decrypt("worker-command", row.encrypted_body, aad)
        value = json.loads(plaintext)
        if (not isinstance(value, dict) or value.get("type") != row.kind
                or hashlib.sha256(_canonical({k: v for k, v in value.items()
                                              if k != "body_hash"})).hexdigest() != row.body_hash
                or _canonical(value) != plaintext):
            return None
        return value
    except Exception:
        return None


def _legacy_ack(row) -> tuple[uuid.UUID, int, str] | None:
    try:
        if row.ack_body is None or len(row.ack_body) > 16 * 1024:
            return None
        value = json.loads(row.ack_body)
        if (not isinstance(value, dict) or _canonical(value) != row.ack_body
                or value.get("type") != "command_ack"
                or value.get("worker_id") != row.worker_id
                or value.get("incarnation") != str(row.incarnation)
                or value.get("command_sequence") != row.command_sequence
                or value.get("command_id") != str(row.command_id)
                or value.get("body_hash") != row.body_hash
                or value.get("status") not in {"accepted", "refused"}
                or row.acknowledged_at is None
                or value.get("status") != row.ack_status
                or value.get("code") != row.ack_code
                or row.expected_sequence is not None
                or value.get("expected_sequence") is not None
                or (value.get("status") == "accepted" and value.get("code") is not None)
                or (value.get("status") == "refused" and value.get("code") not in _REFUSALS)):
            return None
        if (not isinstance(value.get("worker_id"), str)
                or not isinstance(value.get("region"), str)
                or not isinstance(value.get("owner_epoch"), int)
                or isinstance(value.get("owner_epoch"), bool)
                or not 1 <= value["owner_epoch"] <= _MAX_SEQUENCE
                or not _RFC3339.fullmatch(value.get("lease_expires_at", ""))
                or not _HEX64.fullmatch(value.get("body_hash", ""))):
            return None
        for field in ("incarnation", "lease_id", "grant_nonce", "command_id"):
            if not _canonical_uuid(value.get(field)):
                return None
        if "session_id" in value:
            if not _canonical_uuid(value.get("session_id")) or not _canonical_uuid(value.get("attempt_id")):
                return None
            sequence = value.get("control_generation")
            if (not isinstance(sequence, int) or isinstance(sequence, bool)
                    or not 1 <= sequence <= _MAX_SEQUENCE):
                return None
        else:
            sequence = value.get("dispatch_sequence")
            if not _canonical_uuid(value.get("job_id")) or (
                not isinstance(sequence, int) or isinstance(sequence, bool)
                or not 1 <= sequence <= _MAX_SEQUENCE
            ):
                return None
        if _projection(row.kind, value) is None:
            return None
        required = set(_COMMON + _SUBJECTS[row.kind]) | {"type", "status"}
        if value["status"] == "refused":
            required.add("code")
        if set(value) != required:
            return None
        return _identity(row.kind, value)
    except (KeyError, TypeError, ValueError, UnicodeError):
        return None


def _canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False


def upgrade() -> None:
    op.drop_constraint("worker_connections_sequence", "worker_connections", type_="check")
    op.create_check_constraint(
        "worker_connections_sequence", "worker_connections",
        "next_command_sequence > 0 AND acknowledged_floor >= 0 "
        "AND acknowledged_floor < next_command_sequence",
    )
    op.add_column("worker_commands", sa.Column("account_user_id", sa.Uuid()))
    op.add_column("worker_commands", sa.Column("subject_id", sa.Uuid()))
    op.add_column("worker_commands", sa.Column("subject_sequence", sa.BigInteger()))
    op.add_column("worker_commands", sa.Column("ack_identity_hash", sa.String(64)))
    op.add_column("worker_physical_ranges", sa.Column("account_user_id", sa.Uuid()))

    bind = op.get_bind()
    columns = (
        "worker_id, incarnation, command_sequence, command_id, kind, body_hash, "
        "encrypted_body, state, acknowledged_at, ack_status, ack_code, expected_sequence, ack_body"
    )
    cursor = None
    while True:
        if cursor is None:
            statement = (
                f"SELECT {columns} FROM worker_commands "
                "ORDER BY worker_id, incarnation, command_sequence LIMIT 256"
            )
            values = {}
        else:
            statement = (
                f"SELECT {columns} FROM worker_commands "
                "WHERE (worker_id, incarnation, command_sequence) > "
                "(:worker_id, :incarnation, :sequence) "
                "ORDER BY worker_id, incarnation, command_sequence LIMIT 256"
            )
            values = {
                "worker_id": cursor[0], "incarnation": cursor[1], "sequence": cursor[2],
            }
        batch = bind.execute(sa.text(statement), values).mappings().all()
        if not batch:
            break
        for row in batch:
            identity = None
            if row["state"] == "acknowledged":
                identity = _legacy_ack(row)
            elif row["state"] == "pending":
                identity = _identity(row["kind"], _legacy_plaintext(row) or {})
                bind.execute(sa.text(
                    "UPDATE worker_connections SET closed_at = "
                    "coalesce(closed_at, clock_timestamp()) "
                    "WHERE worker_id = :worker_id AND incarnation = :incarnation"
                ), {"worker_id": row["worker_id"], "incarnation": row["incarnation"]})
            if row["state"] == "pending":
                bind.execute(sa.text(
                    "UPDATE worker_commands SET subject_id = :subject_id, "
                    "subject_sequence = :subject_sequence, ack_identity_hash = :digest, "
                    "encrypted_body = NULL, key_version = NULL, complete_bytes = 0, "
                    "state = 'erased', ack_status = NULL, ack_code = NULL, "
                    "expected_sequence = NULL, ack_body = NULL, acknowledged_at = NULL "
                    "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                    "AND command_sequence = :sequence"
                ), {
                    "subject_id": identity[0] if identity else None,
                    "subject_sequence": identity[1] if identity else None,
                    "digest": identity[2] if identity else None,
                    "worker_id": row["worker_id"], "incarnation": row["incarnation"],
                    "sequence": row["command_sequence"],
                })
            elif row["state"] == "acknowledged":
                bind.execute(sa.text(
                    "UPDATE worker_commands SET subject_id = :subject_id, "
                    "subject_sequence = :subject_sequence, ack_identity_hash = :digest, "
                    "complete_bytes = 0 WHERE worker_id = :worker_id "
                    "AND incarnation = :incarnation AND command_sequence = :sequence"
                ), {
                    "subject_id": identity[0] if identity else None,
                    "subject_sequence": identity[1] if identity else None,
                    "digest": identity[2] if identity else None,
                    "worker_id": row["worker_id"], "incarnation": row["incarnation"],
                    "sequence": row["command_sequence"],
                })
                if identity is None:
                    bind.execute(sa.text(
                        "UPDATE worker_commands SET state = 'erased', ack_status = NULL, "
                        "ack_code = NULL, expected_sequence = NULL, ack_body = NULL, "
                        "acknowledged_at = NULL WHERE worker_id = :worker_id "
                        "AND incarnation = :incarnation AND command_sequence = :sequence"
                    ), {"worker_id": row["worker_id"], "incarnation": row["incarnation"],
                        "sequence": row["command_sequence"]})
        last = batch[-1]
        cursor = (last["worker_id"], last["incarnation"], last["command_sequence"])

    op.create_check_constraint(
        "worker_commands_identity_hash", "worker_commands",
        "ack_identity_hash IS NULL OR ack_identity_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "worker_commands_identity", "worker_commands",
        "(state = 'pending' AND account_user_id IS NOT NULL AND subject_id IS NOT NULL "
        "AND subject_sequence IS NOT NULL AND subject_sequence BETWEEN 1 AND 9223372036854775807 "
        "AND ack_identity_hash IS NOT NULL) OR "
        "(state = 'acknowledged' AND subject_id IS NOT NULL AND subject_sequence IS NOT NULL "
        "AND subject_sequence BETWEEN 1 AND 9223372036854775807 "
        "AND ack_identity_hash IS NOT NULL) OR state = 'erased'",
    )
    op.create_check_constraint(
        "worker_commands_state_contract", "worker_commands",
        "(state = 'pending' AND encrypted_body IS NOT NULL AND key_version IS NOT NULL "
        "AND key_version > 0 AND complete_bytes BETWEEN 1 AND CASE WHEN kind IN "
        "('dispatch_job','open_session','update_session') THEN 1048576 ELSE 16384 END "
        "AND ack_status IS NULL AND ack_code IS NULL AND expected_sequence IS NULL "
        "AND ack_body IS NULL AND acknowledged_at IS NULL) OR "
        "(state = 'acknowledged' AND encrypted_body IS NULL AND key_version IS NULL "
        "AND complete_bytes = 0 AND acknowledged_at IS NOT NULL AND expected_sequence IS NULL "
        "AND ack_body IS NOT NULL AND ack_status IS NOT NULL "
        "AND octet_length(ack_body) BETWEEN 1 AND 16384 "
        "AND ((ack_status = 'accepted' AND ack_code IS NULL) OR "
        "(ack_status = 'refused' AND ack_code IS NOT NULL AND ack_code IN "
        "('authority_expired','authority_mismatch','attempt_stale','revision_stale',"
        "'unsupported_control','out_of_memory','model_unavailable','invalid_params',"
        "'capacity_unavailable','dependency_unavailable','checkpoint_incompatible')))) OR "
        "(state = 'erased' AND encrypted_body IS NULL AND key_version IS NULL "
        "AND complete_bytes = 0 AND ack_status IS NULL AND ack_code IS NULL "
        "AND expected_sequence IS NULL AND ack_body IS NULL AND acknowledged_at IS NULL)",
    )
    op.create_check_constraint(
        "worker_commands_kind", "worker_commands",
        "kind IN ('open_session','update_session','close_session','dispatch_job','cancel_job')",
    )
    op.create_index(
        "worker_commands_account", "worker_commands",
        ["account_user_id", "worker_id", "incarnation", "command_sequence"],
        postgresql_where=sa.text("account_user_id IS NOT NULL"),
    )
    op.create_index(
        "worker_commands_subject", "worker_commands",
        ["kind", "subject_id", "subject_sequence", "worker_id", "incarnation",
         "command_sequence"],
        postgresql_where=sa.text("subject_id IS NOT NULL"),
    )
    op.create_index(
        "physical_ranges_account", "worker_physical_ranges",
        ["account_user_id", "worker_id", "incarnation", "range_id"],
    )


def downgrade() -> None:
    op.drop_index("physical_ranges_account", table_name="worker_physical_ranges")
    op.drop_index("worker_commands_subject", table_name="worker_commands")
    op.drop_index("worker_commands_account", table_name="worker_commands")
    op.drop_constraint("worker_commands_kind", "worker_commands", type_="check")
    op.drop_constraint("worker_commands_state_contract", "worker_commands", type_="check")
    op.drop_constraint("worker_commands_identity", "worker_commands", type_="check")
    op.drop_constraint("worker_commands_identity_hash", "worker_commands", type_="check")
    op.drop_constraint("worker_connections_sequence", "worker_connections", type_="check")
    op.create_check_constraint(
        "worker_connections_sequence", "worker_connections",
        "next_command_sequence > 0 AND acknowledged_floor >= 0",
    )
    op.drop_column("worker_physical_ranges", "account_user_id")
    op.drop_column("worker_commands", "ack_identity_hash")
    op.drop_column("worker_commands", "subject_sequence")
    op.drop_column("worker_commands", "subject_id")
    op.drop_column("worker_commands", "account_user_id")
