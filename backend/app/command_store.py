"""Bounded encrypted journal for exact protocol6 worker commands."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from app import db
from app.account_lock import hold_the_account
from app.command_subjects import (
    CommandRefused,
    current_principal as _current_command_principal,
    MAX_SEQUENCE,
    lock_delivery_subject,
    lock_reported_command,
    prepare_command_subject,
    apply_command_claim,
    SUBJECT_FIELDS,
    realtime_erasure_selection,
    subject_erasure_selection,
    validate_command_identity,
    command_size_limit,
    account_erasure_selection,
    lock_update_ack_subject,
)
from app.keyring import KeyRingError, get_key_ring
from app.protocol6 import canonical_bytes, decode_control, preflight_worker_control, validate_message
from app.worker_authority import SCHEDULER_OWNER_ID, TRANSPORT_OWNER_ID

MAX_RETAINED_COMMANDS = 256
MAX_PENDING_BYTES = 4 * 1024 * 1024
MAX_SMALL_COMMAND_BYTES = 16 * 1024
MAX_PARAMETER_COMMAND_BYTES = 1024 * 1024
CONSUMING_REFUSALS = frozenset({
    "authority_expired", "authority_mismatch", "attempt_stale", "revision_stale",
    "unsupported_control", "out_of_memory", "model_unavailable", "invalid_params",
    "capacity_unavailable", "dependency_unavailable", "checkpoint_incompatible",
})
_ACK_COMMON = (
    "worker_id", "incarnation", "region", "owner_epoch", "lease_id",
    "lease_expires_at", "grant_nonce", "command_sequence", "command_id", "body_hash",
)
_ACK_SUBJECTS = SUBJECT_FIELDS


@dataclass(frozen=True)
class StoredCommand:
    worker_id: str
    incarnation: uuid.UUID
    sequence: int
    command_id: uuid.UUID
    body_hash: str
    body: bytes
    created_at: datetime
    account_user_id: uuid.UUID | None = None
    subject_id: uuid.UUID | None = None
    subject_sequence: int | None = None
    ack_identity_hash: str | None = None


@dataclass(frozen=True)
class CommandAckResult:
    recorded: bool
    obsolete: bool = False
    publication: dict | None = None


def _aad(worker_id: str, incarnation: uuid.UUID, sequence: int,
         command_id: uuid.UUID, body_hash: str) -> bytes:
    return canonical_bytes({
        "body_hash": body_hash,
        "command_id": str(command_id),
        "command_sequence": sequence,
        "incarnation": str(incarnation),
        "worker_id": worker_id,
    })


def _ack_identity_projection(kind: str, value: dict) -> dict:
    fields = _ACK_SUBJECTS.get(kind)
    if fields is None:
        raise CommandRefused("command kind has no ACK identity")
    names = _ACK_COMMON + fields
    if any(name not in value for name in names):
        raise CommandRefused("command acknowledgement identity is incomplete")
    projection = {
        "ack_variant": "session" if kind.endswith("session") else "job",
        "command_kind": kind,
    }
    projection.update((name, value[name]) for name in names)
    return projection


def _ack_identity_hash(kind: str, value: dict) -> str:
    return hashlib.sha256(canonical_bytes(_ack_identity_projection(kind, value))).hexdigest()


def _command_identity(kind: str, value: dict) -> tuple[uuid.UUID, int]:
    if kind.endswith("session"):
        subject, sequence = value.get("session_id"), value.get("control_generation")
    else:
        subject, sequence = value.get("job_id"), value.get("dispatch_sequence")
    try:
        subject_id = uuid.UUID(subject)
    except (TypeError, ValueError):
        raise CommandRefused("command subject identity is invalid") from None
    if (not isinstance(sequence, int) or isinstance(sequence, bool)
            or not 1 <= sequence <= MAX_SEQUENCE):
        raise CommandRefused("command subject sequence is invalid")
    return subject_id, sequence


def _ack_matches(command: dict, ack: dict) -> bool:
    common = ("worker_id", "incarnation", "region", "owner_epoch", "lease_id",
              "grant_nonce", "command_sequence", "command_id", "body_hash")
    subjects = {
        "open_session": ("session_id", "attempt_id", "control_generation"),
        "update_session": ("session_id", "attempt_id", "control_generation"),
        "close_session": ("session_id", "attempt_id", "control_generation"),
        "dispatch_job": ("job_id", "dispatch_sequence"),
        "cancel_job": ("job_id", "dispatch_sequence"),
    }
    fields = common + subjects.get(command["type"], ())
    if any(ack.get(field) != command.get(field) for field in fields):
        return False
    return "lease_expires_at" not in ack or ack["lease_expires_at"] == command["lease_expires_at"]


def _known_acknowledged(row) -> bool:
    body = row["ack_body"]
    if (row["state"] != "acknowledged" or row["ack_identity_hash"] is None
            or row["acknowledged_at"] is None or row["expected_sequence"] is not None
            or body is None or len(body) > MAX_SMALL_COMMAND_BYTES):
        return False
    try:
        ack = json.loads(body)
        if canonical_bytes(ack) != bytes(body) or ack.get("type") != "command_ack":
            return False
        validate_message(
            ack, expected_worker_id=row["worker_id"],
            expected_incarnation=row["incarnation"],
        )
        status = ack["status"]
        if (status != row["ack_status"] or ack.get("code") != row["ack_code"]
                or ack.get("expected_sequence") is not None):
            return False
        if ((status == "accepted" and row["ack_code"] is not None)
                or (status == "refused" and row["ack_code"] not in CONSUMING_REFUSALS)):
            return False
        required = set(_ACK_COMMON + _ACK_SUBJECTS[row["kind"]]) | {"type", "status"}
        if status == "refused":
            required.add("code")
        if set(ack) != required:
            return False
        return (
            ack["command_id"] == str(row["command_id"])
            and ack["body_hash"] == row["body_hash"]
            and ack["command_sequence"] == row["command_sequence"]
            and hmac.compare_digest(
                row["ack_identity_hash"], _ack_identity_hash(row["kind"], ack)
            )
        )
    except (KeyError, TypeError, ValueError, CommandRefused):
        return False


async def commit_command(
    worker_id: str,
    incarnation: uuid.UUID,
    command_type: str,
    fields: dict,
    *,
    grant_nonce: uuid.UUID,
    transport_owner_id: uuid.UUID | None = None,
    account_user_id: uuid.UUID | None = None,
    session_context: dict | None = None,
    job_context: dict | None = None,
    desired_committed=None,
) -> StoredCommand:
    validate_command_identity(command_type, account_user_id, fields)
    if desired_committed is not None and command_type != "update_session":
        raise CommandRefused("desired state callback is only valid for session updates")
    if db.session_factory is None:
        raise CommandRefused("durable command store unavailable")
    command_id = uuid.uuid4()
    expected_transport_owner = transport_owner_id or TRANSPORT_OWNER_ID
    committed_desired = None
    async with db.session_factory() as session:
        async with session.begin():
            if account_user_id is not None:
                await hold_the_account(session, account_user_id, wait=True)
            next_hint = await session.scalar(
                text(
                    "SELECT next_command_sequence FROM worker_connections "
                    "WHERE worker_id = :worker_id AND incarnation = :incarnation"
                ),
                {"worker_id": worker_id, "incarnation": incarnation},
            )
            if next_hint is not None and next_hint >= MAX_SEQUENCE:
                raise CommandRefused("worker command sequence is exhausted")
            claim, cancel_attempt, message_fields, committed_desired = (
                await prepare_command_subject(
                    session, command_type, fields, account_user_id=account_user_id,
                    session_context=session_context, job_context=job_context,
                    worker_id=worker_id, incarnation=incarnation,
                    expected_transport_owner=expected_transport_owner,
                )
            )
            worker_hint = (await session.execute(
                text(
                    "SELECT region, next_command_sequence, acknowledged_floor FROM worker_connections "
                    "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                    "AND closed_at IS NULL AND lifecycle = 'ready'"
                ),
                {"worker_id": worker_id, "incarnation": incarnation},
            )).mappings().one_or_none()
            if worker_hint is None:
                raise CommandRefused("worker has no current work grant")
            locked_commands = (await session.execute(
                text(
                    "SELECT worker_id, incarnation, command_sequence, command_id, kind, "
                    "body_hash, state, ack_identity_hash, complete_bytes, acknowledged_at, "
                    "ack_status, ack_code, expected_sequence, ack_body "
                    "FROM worker_commands "
                    "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                    "ORDER BY command_sequence FOR UPDATE"
                ),
                {"worker_id": worker_id, "incarnation": incarnation},
            )).mappings().all()
            lease = (await session.execute(
                text(
                    "SELECT owner_id, owner_epoch, lease_id, expires_at FROM scheduler_leases "
                    "WHERE region = :region FOR UPDATE"
                ),
                {"region": worker_hint["region"]},
            )).mappings().one_or_none()
            if lease is None or lease["owner_id"] != SCHEDULER_OWNER_ID:
                raise CommandRefused("regional scheduler lease is unavailable")
            worker = (await session.execute(
                text(
                    "SELECT region, owner_epoch, lease_id, lease_expires_at, grant_nonce, "
                    "grant_expires_at, grant_ready, realtime_slots, next_command_sequence, "
                    "acknowledged_floor, transport_owner_id "
                    "FROM worker_connections WHERE worker_id = :worker_id "
                    "AND incarnation = :incarnation "
                    "AND closed_at IS NULL "
                    "AND lifecycle = 'ready' FOR UPDATE"
                ),
                {"worker_id": worker_id, "incarnation": incarnation},
            )).mappings().one_or_none()
            if (worker is None or worker["transport_owner_id"] != expected_transport_owner
                    or not worker["grant_ready"]):
                raise CommandRefused("worker has no current work grant")
            if (worker["region"] != worker_hint["region"]
                    or worker["next_command_sequence"] != worker_hint["next_command_sequence"]
                    or worker["acknowledged_floor"] != worker_hint["acknowledged_floor"]):
                raise CommandRefused("worker command allocation changed; retry the command")
            if cancel_attempt is not None and (
                cancel_attempt["worker_id"] != worker_id
                or cancel_attempt["incarnation"] != incarnation
                or cancel_attempt["owner_epoch"] != worker["owner_epoch"]
            ):
                raise CommandRefused("job cancellation owner is stale")
            if worker["grant_nonce"] != grant_nonce:
                raise CommandRefused("worker grant nonce is stale")
            if (worker["owner_epoch"] != lease["owner_epoch"]
                    or worker["lease_id"] != lease["lease_id"]):
                raise CommandRefused("worker authority is no longer current")
            deadlines = (await session.execute(
                text(
                    "SELECT :lease_expires_at > clock_timestamp() AS lease_live, "
                    ":grant_expires_at > clock_timestamp() AS grant_live"
                ),
                {
                    "lease_expires_at": lease["expires_at"],
                    "grant_expires_at": worker["grant_expires_at"],
                },
            )).mappings().one()
            if not deadlines["lease_live"]:
                raise CommandRefused("regional scheduler lease has expired")
            if not deadlines["grant_live"]:
                raise CommandRefused("worker grant has expired")
            sequence = worker["next_command_sequence"]
            if sequence >= MAX_SEQUENCE:
                raise CommandRefused("worker command sequence is exhausted")
            message = {
                **message_fields,
                "type": command_type,
                "worker_id": worker_id,
                "incarnation": str(incarnation),
                "region": worker["region"],
                "owner_epoch": worker["owner_epoch"],
                "lease_id": str(worker["lease_id"]),
                "lease_expires_at": worker["lease_expires_at"].isoformat().replace(
                    "+00:00", "Z"
                ),
                "grant_nonce": str(grant_nonce),
                "command_sequence": sequence,
                "command_id": str(command_id),
            }
            body_hash = hashlib.sha256(canonical_bytes(message)).hexdigest()
            message["body_hash"] = body_hash
            subject_id, subject_sequence = _command_identity(command_type, message)
            ack_identity_hash = _ack_identity_hash(command_type, message)
            try:
                body = preflight_worker_control(
                    message,
                    expected_worker_id=worker_id,
                    expected_incarnation=incarnation,
                )
            except ValueError:
                raise CommandRefused("command does not match protocol6") from None
            if len(body) > command_size_limit(
                command_type, MAX_PARAMETER_COMMAND_BYTES, MAX_SMALL_COMMAND_BYTES
            ):
                raise CommandRefused("command exceeds protocol6 size limit")
            floor = worker["acknowledged_floor"]
            locked_by_sequence = {row["command_sequence"]: row for row in locked_commands}
            while floor + 1 < sequence:
                row = locked_by_sequence.get(floor + 1)
                if row is None or not _known_acknowledged(row):
                    break
                floor += 1
            if floor > worker["acknowledged_floor"]:
                await session.execute(
                    text(
                        "UPDATE worker_connections SET acknowledged_floor = :floor "
                        "WHERE worker_id = :worker_id AND incarnation = :incarnation"
                    ), {"worker_id": worker_id, "incarnation": incarnation, "floor": floor},
                )
                await session.execute(
                    text(
                        "DELETE FROM worker_commands WHERE worker_id = :worker_id "
                        "AND incarnation = :incarnation AND command_sequence <= :floor "
                        "AND state = 'acknowledged'"
                    ), {"worker_id": worker_id, "incarnation": incarnation, "floor": floor},
                )
            retained_rows = [
                row for row in locked_commands
                if not (floor > worker["acknowledged_floor"]
                        and row["command_sequence"] <= floor
                        and row["state"] == "acknowledged")
            ]
            retained = len(retained_rows)
            pending_bytes = sum(
                row["complete_bytes"] for row in locked_commands
                if row["state"] == "pending"
            )
            if retained >= MAX_RETAINED_COMMANDS or pending_bytes + len(body) > MAX_PENDING_BYTES:
                raise CommandRefused("worker command journal is full")
            try:
                keyring = get_key_ring()
                aad = _aad(worker_id, incarnation, sequence, command_id, body_hash)
                encrypted = keyring.encrypt("worker-command", body, aad)
                key_version = keyring.version_of(encrypted)
            except KeyRingError:
                raise CommandRefused("worker command key is unavailable") from None
            await apply_command_claim(
                session, claim, worker_id=worker_id, incarnation=incarnation,
                account_user_id=account_user_id, command_id=command_id, worker=worker,
            )
            created_at = await session.scalar(
                text(
                    "INSERT INTO worker_commands "
                    "(worker_id, incarnation, command_sequence, command_id, kind, body_hash, "
                    "account_user_id, subject_id, subject_sequence, ack_identity_hash, "
                    "key_version, encrypted_body, complete_bytes, state) "
                    "VALUES (:worker_id, :incarnation, :sequence, :command_id, :kind, :body_hash, "
                    ":account_user_id, :subject_id, :subject_sequence, :ack_identity_hash, "
                    ":key_version, :encrypted_body, :complete_bytes, 'pending') "
                    "RETURNING created_at"
                ),
                {
                    "worker_id": worker_id,
                    "incarnation": incarnation,
                    "sequence": sequence,
                    "command_id": command_id,
                    "kind": command_type,
                    "body_hash": body_hash,
                    "account_user_id": account_user_id,
                    "subject_id": subject_id,
                    "subject_sequence": subject_sequence,
                    "ack_identity_hash": ack_identity_hash,
                    "key_version": key_version,
                    "encrypted_body": encrypted,
                    "complete_bytes": len(body),
                },
            )
            await session.execute(
                text(
                    "UPDATE worker_connections SET next_command_sequence = next_command_sequence + 1 "
                    "WHERE worker_id = :worker_id AND incarnation = :incarnation"
                ),
                {"worker_id": worker_id, "incarnation": incarnation},
            )
        stored = StoredCommand(worker_id, incarnation, sequence, command_id,
                               body_hash, b"", created_at, account_user_id, subject_id,
                               subject_sequence, ack_identity_hash)
        if desired_committed is not None and committed_desired is not None:
            desired_committed(
                dict(committed_desired["desired_params"]),
                int(committed_desired["desired_revision"]),
            )
        return stored


async def read_exact_pending(
    worker_id: str,
    incarnation: uuid.UUID,
    *,
    transport_owner_id: uuid.UUID | None = None,
) -> StoredCommand | None:
    if db.session_factory is None:
        raise CommandRefused("durable command store unavailable")
    expected_transport_owner = transport_owner_id or TRANSPORT_OWNER_ID
    async with db.session_factory() as session:
        authority = (await session.execute(
            text(
                "SELECT worker.owner_epoch, worker.lease_id, worker.acknowledged_floor, "
                "worker.next_command_sequence "
                "FROM worker_connections worker JOIN scheduler_leases lease "
                "ON lease.region = worker.region AND lease.owner_epoch = worker.owner_epoch "
                "AND lease.lease_id = worker.lease_id "
                "WHERE worker.worker_id = :worker_id AND worker.incarnation = :incarnation "
                "AND worker.transport_owner_id = :transport_owner_id AND worker.closed_at IS NULL "
                "AND worker.lifecycle IN ('ready','draining') AND lease.expires_at > clock_timestamp()"
            ),
            {"worker_id": worker_id, "incarnation": incarnation,
             "transport_owner_id": expected_transport_owner},
        )).mappings().one_or_none()
        if authority is None:
            fenced = (await session.execute(
                text(
                    "SELECT acknowledged_floor, next_command_sequence FROM worker_connections "
                    "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                    "AND transport_owner_id = :transport_owner_id"
                ),
                {"worker_id": worker_id, "incarnation": incarnation,
                 "transport_owner_id": expected_transport_owner},
            )).mappings().one_or_none()
            if fenced is not None and fenced["acknowledged_floor"] + 1 < fenced["next_command_sequence"]:
                head = await session.scalar(
                    text(
                        "SELECT state FROM worker_commands WHERE worker_id = :worker_id "
                        "AND incarnation = :incarnation AND command_sequence = :sequence"
                    ),
                    {"worker_id": worker_id, "incarnation": incarnation,
                     "sequence": fenced["acknowledged_floor"] + 1},
                )
                if head != "pending":
                    raise CommandRefused("worker command head is unresolved")
            return None
        expected_sequence = authority["acknowledged_floor"] + 1
        if expected_sequence >= authority["next_command_sequence"]:
            return None
        row = (await session.execute(
            text(
                "SELECT command_sequence, command_id, body_hash, key_version, encrypted_body, "
                "created_at, account_user_id, subject_id, subject_sequence, ack_identity_hash, state "
                "FROM worker_commands WHERE worker_id = :worker_id "
                "AND incarnation = :incarnation AND command_sequence = :sequence"
            ),
            {"worker_id": worker_id, "incarnation": incarnation, "sequence": expected_sequence},
        )).mappings().one_or_none()
        if row is None:
            raise CommandRefused("worker command journal has an unresolved gap")
        if row["state"] != "pending":
            raise CommandRefused("worker command head is unresolved")
        try:
            plaintext = get_key_ring().decrypt(
                "worker-command",
                row["encrypted_body"],
                _aad(worker_id, incarnation, row["command_sequence"],
                     row["command_id"], row["body_hash"]),
            )
        except KeyRingError:
            raise CommandRefused("retained worker command cannot be read") from None
        message = decode_control(plaintext)
        if any(message.get(field) != value for field, value in (
            ("worker_id", worker_id),
            ("incarnation", str(incarnation)),
            ("owner_epoch", authority["owner_epoch"]),
            ("lease_id", str(authority["lease_id"])),
        )):
            raise CommandRefused("pending worker command authority is stale")
        digest = hashlib.sha256(canonical_bytes({
            key: value for key, value in message.items() if key != "body_hash"
        })).hexdigest()
        if digest != row["body_hash"]:
            raise CommandRefused("retained worker command failed its body hash")
        subject_id, subject_sequence = _command_identity(message["type"], message)
        if (row["account_user_id"] is None or row["subject_id"] != subject_id
                or row["subject_sequence"] != subject_sequence
                or row["ack_identity_hash"] != _ack_identity_hash(message["type"], message)):
            raise CommandRefused("retained worker command identity is unavailable")
        return StoredCommand(
            worker_id,
            incarnation,
            row["command_sequence"],
            row["command_id"],
            row["body_hash"],
            plaintext,
            row["created_at"],
            row["account_user_id"],
            row["subject_id"],
            row["subject_sequence"],
            row["ack_identity_hash"],
        )


async def _erase_pending_rows(
    session, statement: str, values: dict,
    fence_workers: set[tuple[str, uuid.UUID]] | None = None,
    locked_rows=None,
) -> int:
    rows = locked_rows
    if rows is None:
        rows = (await session.execute(
            text(
                "SELECT worker_id, incarnation, command_sequence, state FROM worker_commands "
                f"WHERE {statement} ORDER BY worker_id, incarnation, command_sequence FOR UPDATE"
            ),
            values,
        )).mappings().all()
    pending = [row for row in rows if row["state"] == "pending"]
    workers = sorted(
        {(row["worker_id"], row["incarnation"]) for row in pending}
        | (fence_workers or set())
    )
    if not workers:
        return 0
    if pending:
        await session.execute(
            text(
                "UPDATE worker_commands SET state = 'erased', encrypted_body = NULL, "
                "key_version = NULL, complete_bytes = 0, acknowledged_at = NULL, "
                "ack_status = NULL, ack_code = NULL, expected_sequence = NULL, ack_body = NULL "
                f"WHERE {statement} AND state = 'pending'"
            ),
            values,
        )
    regions = (await session.execute(
        text(
            "SELECT DISTINCT region FROM worker_connections WHERE "
            + " OR ".join(
                f"(worker_id = :worker_{i} AND incarnation = :incarnation_{i})"
                for i in range(len(workers))
            ) + " ORDER BY region"
        ),
        {
            **{f"worker_{i}": worker for i, (worker, _) in enumerate(workers)},
            **{f"incarnation_{i}": incarnation for i, (_, incarnation) in enumerate(workers)},
        },
    )).scalars().all()
    for region in regions:
        await session.execute(
            text("SELECT region FROM scheduler_leases WHERE region = :region FOR UPDATE"),
            {"region": region},
        )
    for worker_id, incarnation in workers:
        await session.execute(
            text(
                "UPDATE worker_connections SET closed_at = coalesce(closed_at, clock_timestamp()) "
                "WHERE worker_id = :worker_id AND incarnation = :incarnation"
            ),
            {"worker_id": worker_id, "incarnation": incarnation},
        )
    return len(pending)


async def erase_account_commands(session, account_user_ids: Collection[uuid.UUID]) -> int:
    if not isinstance(account_user_ids, Collection) or any(
        not isinstance(user_id, uuid.UUID) for user_id in account_user_ids
    ):
        raise CommandRefused("command erasure account identity is invalid")
    return await _erase_pending_rows(
        session, *await account_erasure_selection(session, tuple(account_user_ids))
    )


async def erase_realtime_commands(
    session,
    account_user_id: uuid.UUID,
    *,
    auth_session_ids: set[uuid.UUID] | None = None,
    keep_auth_session_id: uuid.UUID | None = None,
) -> int:
    selection = await realtime_erasure_selection(
        session, account_user_id, auth_session_ids=auth_session_ids,
        keep_auth_session_id=keep_auth_session_id,
    )
    if selection is None:
        return 0
    return await _erase_pending_rows(session, *selection)


async def erase_subject_commands(
    session,
    account_user_id: uuid.UUID,
    *,
    kind: str,
    subject_id: uuid.UUID,
    subject_sequence: int | None = None,
) -> int:
    selection = await subject_erasure_selection(
        session, account_user_id, kind=kind, subject_id=subject_id,
        subject_sequence=subject_sequence,
    )
    return await _erase_pending_rows(session, *selection)


async def erase_reported_subject_command(
    session, account_user_id: uuid.UUID, *, kind: str, subject_id: uuid.UUID,
    subject_sequence: int, worker_id: str, incarnation: uuid.UUID,
    range_id: uuid.UUID, attempt_id: uuid.UUID | None, report_sequence: int,
    report_hash: str,
) -> int:
    rows = await lock_reported_command(
        session, account_user_id, kind=kind, subject_id=subject_id,
        subject_sequence=subject_sequence, worker_id=worker_id,
        incarnation=incarnation, range_id=range_id, attempt_id=attempt_id,
        report_sequence=report_sequence, report_hash=report_hash,
    )
    if len(rows) != 1:
        return 0
    row = rows[0]
    if row["ack_identity_hash"] is None:
        raise CommandRefused("pending command identity is unavailable")
    if (not isinstance(row["ack_identity_hash"], str)
            or len(row["ack_identity_hash"]) != 64
            or any(char not in "0123456789abcdef" for char in row["ack_identity_hash"])):
        raise CommandRefused("reported pending command identity changed")
    await session.execute(
        text(
            "UPDATE worker_commands SET state = 'erased', encrypted_body = NULL, "
            "key_version = NULL, complete_bytes = 0, acknowledged_at = NULL, "
            "ack_status = NULL, ack_code = NULL, expected_sequence = NULL, ack_body = NULL "
            "WHERE worker_id = :worker_id AND incarnation = :incarnation "
            "AND command_sequence = :sequence AND state = 'pending'"
        ),
        {"worker_id": worker_id, "incarnation": incarnation,
         "sequence": row["command_sequence"]},
    )
    return 1


async def register_delivery(
    command: StoredCommand,
    *,
    session_factory,
    worker_id: str,
    incarnation: uuid.UUID,
    transport_owner_id: uuid.UUID,
    register,
) -> bool:
    if (db.session_factory is None or session_factory is not db.session_factory
            or command.worker_id != worker_id or command.incarnation != incarnation
            or command.account_user_id is None or not command.body):
        raise CommandRefused("command delivery identity changed")
    try:
        body = decode_control(command.body)
        if (body.get("worker_id") != worker_id or body.get("incarnation") != str(incarnation)
                or hashlib.sha256(canonical_bytes({k: v for k, v in body.items()
                                                  if k != "body_hash"})).hexdigest()
                != command.body_hash
                or _ack_identity_hash(body["type"], body) != command.ack_identity_hash):
            raise CommandRefused("command delivery body changed")
    except (KeyError, ValueError):
        raise CommandRefused("command delivery body is invalid") from None
    registered = False
    binding_deadlines = None
    async with session_factory() as session:
        async with session.begin():
            await hold_the_account(session, command.account_user_id, wait=True)
            binding_deadlines = await lock_delivery_subject(
                session, command, body, worker_id=worker_id, incarnation=incarnation,
                transport_owner_id=transport_owner_id,
            )
            journal = (await session.execute(
                text(
                    "SELECT command_sequence, state, command_id, body_hash, account_user_id, "
                    "subject_id, subject_sequence, ack_identity_hash FROM worker_commands "
                    "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                    "ORDER BY command_sequence FOR UPDATE"
                ), {"worker_id": worker_id, "incarnation": incarnation},
            )).mappings().all()
            head = next((row for row in journal if row["command_sequence"] == command.sequence), None)
            if (head is None or head["state"] != "pending"
                    or head["command_id"] != command.command_id
                    or head["body_hash"] != command.body_hash
                    or head["account_user_id"] != command.account_user_id
                    or head["subject_id"] != command.subject_id
                    or head["subject_sequence"] != command.subject_sequence
                    or head["ack_identity_hash"] != command.ack_identity_hash):
                raise CommandRefused("command is no longer pending")
            hint = (await session.execute(
                text("SELECT region FROM worker_connections WHERE worker_id = :worker_id "
                     "AND incarnation = :incarnation"),
                {"worker_id": worker_id, "incarnation": incarnation},
            )).mappings().one_or_none()
            if hint is None:
                raise CommandRefused("worker connection is unavailable")
            lease = (await session.execute(
                text(
                    "SELECT region, owner_id, owner_epoch, lease_id, expires_at FROM scheduler_leases "
                    "WHERE region = :region FOR UPDATE"
                ), {"region": hint["region"]},
            )).mappings().one_or_none()
            worker = (await session.execute(
                text(
                    "SELECT transport_owner_id, region, owner_epoch, lease_id, grant_nonce, "
                    "grant_expires_at, grant_ready, lifecycle, closed_at, acknowledged_floor "
                    "FROM worker_connections "
                    "WHERE worker_id = :worker_id AND incarnation = :incarnation FOR UPDATE"
                ), {"worker_id": worker_id, "incarnation": incarnation},
            )).mappings().one_or_none()
            if (lease is None or worker is None or worker["closed_at"] is not None
                    or worker["transport_owner_id"] != transport_owner_id
                    or worker["region"] != lease["region"]
                    or lease["owner_id"] != SCHEDULER_OWNER_ID
                    or worker["owner_epoch"] != lease["owner_epoch"]
                    or worker["lease_id"] != lease["lease_id"]
                    or not worker["grant_ready"] or worker["lifecycle"] != "ready"):
                raise CommandRefused("worker delivery authority is stale")
            if command.sequence != worker["acknowledged_floor"] + 1:
                raise CommandRefused("command is not the unresolved journal head")
            absolute_deadline = binding_deadlines[1] if binding_deadlines else None
            idle_deadline = binding_deadlines[0] if binding_deadlines else None
            live = (await session.execute(
                text("SELECT :lease_expires_at > clock_timestamp() AS lease_live, "
                     ":grant_expires_at > clock_timestamp() AS grant_live, "
                     "(CAST(:absolute_expires_at AS timestamptz) IS NULL OR "
                     "CAST(:absolute_expires_at AS timestamptz) > clock_timestamp()) AND "
                     "(CAST(:idle_expires_at AS timestamptz) IS NULL OR "
                     "CAST(:idle_expires_at AS timestamptz) > clock_timestamp()) "
                     "AS binding_live"),
                {"lease_expires_at": lease["expires_at"],
                 "grant_expires_at": worker["grant_expires_at"],
                 "absolute_expires_at": absolute_deadline,
                 "idle_expires_at": idle_deadline},
            )).mappings().one()
            if not live["lease_live"] or not live["grant_live"] or not live["binding_live"]:
                raise CommandRefused("worker delivery authority or browser binding expired")
            outcome = register()
            if hasattr(outcome, "__await__") or not outcome:
                raise CommandRefused("command delivery registration refused")
            registered = True
    return registered


async def acknowledge(
    message: dict,
    *,
    expected_worker_id: str,
    expected_incarnation: uuid.UUID,
    transport_owner_id: uuid.UUID,
) -> CommandAckResult:
    if db.session_factory is None:
        raise CommandRefused("durable command store unavailable")
    try:
        validate_message(
            message,
            expected_worker_id=expected_worker_id,
            expected_incarnation=expected_incarnation,
        )
        if message["type"] != "command_ack":
            raise ValueError
        worker_id = message["worker_id"]
        incarnation = uuid.UUID(message["incarnation"])
        sequence = message["command_sequence"]
        command_id = uuid.UUID(message["command_id"])
        body_hash = message["body_hash"]
        ack_body = canonical_bytes(message)
        if len(ack_body) > 16 * 1024:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise CommandRefused("command acknowledgement is invalid") from None
    if worker_id != expected_worker_id or incarnation != expected_incarnation:
        raise CommandRefused("command acknowledgement source is not bound")
    if (message["status"] != "accepted"
            and message.get("code") not in CONSUMING_REFUSALS):
        return CommandAckResult(False)

    result = CommandAckResult(False)
    async with db.session_factory() as session:
        async with session.begin():
            hint = (await session.execute(
                text(
                    "SELECT account_user_id, kind, subject_id, subject_sequence, state "
                    "FROM worker_commands WHERE worker_id = :worker_id "
                    "AND incarnation = :incarnation AND command_sequence = :sequence"
                ),
                {"worker_id": worker_id, "incarnation": incarnation, "sequence": sequence},
            )).mappings().one_or_none()
            owner = hint["account_user_id"] if hint is not None else None
            if owner is not None:
                await hold_the_account(session, owner, wait=True)
            update_entity, update_attempt, update_range = await lock_update_ack_subject(
                session, hint
            )
            journal = (await session.execute(
                text(
                    "SELECT worker_id, incarnation, command_sequence, command_id, kind, body_hash, "
                    "acknowledged_at, ack_status, ack_code, expected_sequence, account_user_id, "
                    "subject_id, subject_sequence, ack_identity_hash, state, key_version, "
                    "encrypted_body, ack_body FROM worker_commands "
                    "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                    "ORDER BY command_sequence FOR UPDATE"
                ),
                {"worker_id": worker_id, "incarnation": incarnation},
            )).mappings().all()
            worker_hint = (await session.execute(
                text(
                    "SELECT region FROM worker_connections WHERE worker_id = :worker_id "
                    "AND incarnation = :incarnation"
                ),
                {"worker_id": worker_id, "incarnation": incarnation},
            )).mappings().one_or_none()
            if worker_hint is None:
                return CommandAckResult(False)
            await session.execute(
                text("SELECT region FROM scheduler_leases WHERE region = :region FOR UPDATE"),
                {"region": worker_hint["region"]},
            )
            worker = (await session.execute(
                text(
                    "SELECT acknowledged_floor, next_command_sequence, transport_owner_id "
                    "FROM worker_connections WHERE worker_id = :worker_id "
                    "AND incarnation = :incarnation FOR UPDATE"
                ),
                {"worker_id": worker_id, "incarnation": incarnation},
            )).mappings().one_or_none()
            if worker is None or worker["transport_owner_id"] != transport_owner_id:
                raise CommandRefused("command acknowledgement transport is stale")
            rows = {row["command_sequence"]: row for row in journal}
            row = rows.get(sequence)
            if row is None:
                if sequence <= worker["acknowledged_floor"]:
                    return CommandAckResult(False, obsolete=True)
                return CommandAckResult(False)
            if row["command_id"] != command_id or row["body_hash"] != body_hash:
                return CommandAckResult(False)
            if (hint is not None and (row["kind"] != hint["kind"]
                    or row["account_user_id"] != hint["account_user_id"]
                    or row["subject_id"] != hint["subject_id"]
                    or row["subject_sequence"] != hint["subject_sequence"])):
                return CommandAckResult(False)
            if row["ack_identity_hash"] is None:
                return CommandAckResult(False)
            try:
                identity_hash = _ack_identity_hash(row["kind"], message)
            except CommandRefused:
                return CommandAckResult(False)
            if not hmac.compare_digest(row["ack_identity_hash"], identity_hash):
                return CommandAckResult(False)
            publication = None
            missing_key = False
            if row["state"] == "acknowledged":
                if not _known_acknowledged(row) or row["ack_body"] != ack_body:
                    return CommandAckResult(False)
            elif row["state"] == "erased":
                await session.execute(
                    text(
                        "UPDATE worker_commands SET state = 'acknowledged', "
                        "acknowledged_at = clock_timestamp(), ack_status = :status, "
                        "ack_code = :code, ack_body = :ack_body WHERE worker_id = :worker_id "
                        "AND incarnation = :incarnation AND command_sequence = :sequence"
                    ),
                    {"worker_id": worker_id, "incarnation": incarnation,
                     "sequence": sequence, "status": message["status"],
                     "code": message.get("code"), "ack_body": ack_body},
                )
            elif row["state"] == "pending":
                if row["account_user_id"] is None:
                    return CommandAckResult(False)
                try:
                    ring = get_key_ring()
                except KeyRingError:
                    ring = None
                if ring is None or row["key_version"] not in ring.versions:
                    command = None
                    missing_key = True
                else:
                    try:
                        command_body = ring.decrypt(
                            "worker-command", row["encrypted_body"],
                            _aad(worker_id, incarnation, sequence, command_id, body_hash),
                        )
                        command = decode_control(command_body)
                    except (KeyRingError, ValueError):
                        raise CommandRefused("pending command cannot be verified") from None
                    if not _ack_matches(command, message):
                        return CommandAckResult(False)
                if (command is not None and message["status"] == "accepted"
                        and row["kind"] == "update_session"
                        and update_entity is not None and update_attempt is not None
                        and update_range is not None
                        and update_entity["user_id"] == row["account_user_id"]
                        and update_entity["control_generation"] == row["subject_sequence"]
                        and update_entity["model_id"] == command["model_id"]
                        and update_entity["state"] in {"live", "idle"}
                        and update_entity["browser_owner_id"] == transport_owner_id
                        and await _current_command_principal(
                            session, row["account_user_id"], update_entity["auth_session_id"]
                        )
                        and update_entity["params_revision"] < command["params_revision"]
                        and update_attempt["worker_id"] == worker_id
                        and update_attempt["incarnation"] == incarnation
                        and update_attempt["attempt_id"] == uuid.UUID(command["attempt_id"])
                        and update_attempt["state"] == "running"
                        and update_range["account_user_id"] == row["account_user_id"]
                        and update_range["state"] == "outstanding"):
                    await session.execute(
                        text(
                            "UPDATE realtime_sessions SET effective_params = CAST(:params AS jsonb), "
                            "params_revision = :revision WHERE id = :session_id "
                            "AND control_generation = :generation "
                            "AND params_revision < :revision"
                        ),
                        {"params": json.dumps(command["params"]),
                         "revision": command["params_revision"],
                         "session_id": row["subject_id"],
                         "generation": row["subject_sequence"]},
                    )
                    publication = {
                        "kind": "update_session",
                        "account_user_id": row["account_user_id"],
                        "subject_id": row["subject_id"],
                        "subject_sequence": row["subject_sequence"],
                        "model_id": command["model_id"],
                        "params": command["params"],
                        "params_revision": command["params_revision"],
                        "attempt_id": command["attempt_id"],
                    }
                await session.execute(
                    text(
                        "UPDATE worker_commands SET state = 'acknowledged', encrypted_body = NULL, "
                        "key_version = NULL, complete_bytes = 0, acknowledged_at = clock_timestamp(), "
                        "ack_status = :status, ack_code = :code, expected_sequence = NULL, "
                        "ack_body = :ack_body WHERE worker_id = :worker_id "
                        "AND incarnation = :incarnation AND command_sequence = :sequence"
                    ),
                    {"worker_id": worker_id, "incarnation": incarnation,
                     "sequence": sequence, "status": message["status"],
                     "code": message.get("code"), "ack_body": ack_body},
                )
                if missing_key:
                    await session.execute(
                        text(
                            "UPDATE worker_connections SET lifecycle = 'ended', "
                            "closed_at = coalesce(closed_at, clock_timestamp()) "
                            "WHERE worker_id = :worker_id AND incarnation = :incarnation"
                        ), {"worker_id": worker_id, "incarnation": incarnation},
                    )
            else:
                return CommandAckResult(False)

            next_sequence = worker["next_command_sequence"]
            floor = worker["acknowledged_floor"]
            while floor + 1 < next_sequence:
                current = floor + 1
                candidate = rows.get(current)
                if candidate is None or candidate["ack_identity_hash"] is None:
                    break
                newly_known = current == sequence
                if (not newly_known and not _known_acknowledged(candidate)):
                    break
                if (newly_known and message["status"] == "refused"
                        and message.get("code") not in CONSUMING_REFUSALS):
                    break
                floor += 1
            if floor > worker["acknowledged_floor"]:
                await session.execute(
                    text(
                        "UPDATE worker_connections SET acknowledged_floor = :floor "
                        "WHERE worker_id = :worker_id AND incarnation = :incarnation"
                    ),
                    {"worker_id": worker_id, "incarnation": incarnation, "floor": floor},
                )
                await session.execute(
                    text(
                        "DELETE FROM worker_commands WHERE worker_id = :worker_id "
                        "AND incarnation = :incarnation AND command_sequence <= :floor "
                        "AND state = 'acknowledged'"
                    ),
                    {"worker_id": worker_id, "incarnation": incarnation, "floor": floor},
                )
            result = CommandAckResult(True, publication=publication)
    return result
