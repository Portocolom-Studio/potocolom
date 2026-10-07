"""Bounded encrypted journal for exact protocol6 worker commands."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from app import db
from app.account_lock import hold_the_account
from app.keyring import KeyRingError, get_key_ring
from app.protocol6 import canonical_bytes, decode_control, preflight_worker_control, validate_message
from app.worker_authority import REGION, SCHEDULER_OWNER_ID, TRANSPORT_OWNER_ID

MAX_RETAINED_COMMANDS = 256
MAX_PENDING_BYTES = 4 * 1024 * 1024
MAX_SMALL_COMMAND_BYTES = 16 * 1024
MAX_PARAMETER_COMMAND_BYTES = 1024 * 1024


class CommandRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredCommand:
    worker_id: str
    incarnation: uuid.UUID
    sequence: int
    command_id: uuid.UUID
    body_hash: str
    body: bytes
    created_at: datetime


def _aad(worker_id: str, incarnation: uuid.UUID, sequence: int,
         command_id: uuid.UUID, body_hash: str) -> bytes:
    return canonical_bytes({
        "body_hash": body_hash,
        "command_id": str(command_id),
        "command_sequence": sequence,
        "incarnation": str(incarnation),
        "worker_id": worker_id,
    })


def _size_limit(kind: str) -> int:
    if kind == "dispatch_job":
        return MAX_PARAMETER_COMMAND_BYTES
    return MAX_SMALL_COMMAND_BYTES


def _ack_matches(command: dict, ack: dict) -> bool:
    common = ("worker_id", "incarnation", "region", "owner_epoch", "lease_id",
              "grant_nonce", "command_sequence", "command_id", "body_hash")
    subjects = {
        "dispatch_job": ("job_id", "dispatch_sequence"),
        "cancel_job": ("job_id", "dispatch_sequence"),
    }
    fields = common + subjects.get(command["type"], ())
    if any(ack.get(field) != command.get(field) for field in fields):
        return False
    return "lease_expires_at" not in ack or ack["lease_expires_at"] == command["lease_expires_at"]


async def commit_command(
    worker_id: str,
    incarnation: uuid.UUID,
    command_type: str,
    fields: dict,
    *,
    grant_nonce: uuid.UUID,
    transport_owner_id: uuid.UUID | None = None,
    account_user_id: uuid.UUID | None = None,
    job_context: dict | None = None,
) -> StoredCommand:
    if command_type not in {"dispatch_job", "cancel_job"}:
        raise CommandRefused("command type is not supported")
    if db.session_factory is None:
        raise CommandRefused("durable command store unavailable")
    if not isinstance(fields, dict) or any(
        key in fields for key in {
            "type", "worker_id", "incarnation", "region", "owner_epoch", "lease_id",
            "lease_expires_at", "grant_nonce", "command_sequence", "command_id", "body_hash",
        }
    ):
        raise CommandRefused("command fields are invalid")
    command_id = uuid.uuid4()
    expected_transport_owner = transport_owner_id or TRANSPORT_OWNER_ID
    async with db.session_factory() as session:
        async with session.begin():
            claim = None
            cancel_attempt = None
            message_fields = fields
            if command_type == "dispatch_job":
                if not isinstance(account_user_id, uuid.UUID):
                    raise CommandRefused("job claim account is unavailable")
                await hold_the_account(session, account_user_id, wait=True)
                job_id = uuid.UUID(fields["job_id"])
                job = (await session.execute(
                    text(
                        "SELECT user_id, model_id, params, state, attempt, "
                        "current_dispatch_sequence, source_asset_id FROM jobs "
                        "WHERE id = :job_id FOR UPDATE"
                    ),
                    {"job_id": job_id},
                )).mappings().one_or_none()
                if (job is None or job["user_id"] != account_user_id
                        or job["state"] != "queued" or job["model_id"] != fields["model_id"]
                        or job["params"] != fields["params"]):
                    raise CommandRefused("job claim is stale")
                if isinstance(job_context, dict) and (
                    job_context.get("source_asset_id") != job["source_asset_id"]
                ):
                    raise CommandRefused("job input changed before claim")
                if job["source_asset_id"] is not None:
                    asset = await session.scalar(
                        text(
                            "SELECT id FROM assets WHERE id = :asset_id AND user_id = :user_id "
                            "FOR UPDATE"
                        ),
                        {"asset_id": job["source_asset_id"], "user_id": account_user_id},
                    )
                    if asset is None:
                        raise CommandRefused("job input is unavailable")
                dispatch_sequence = job["current_dispatch_sequence"] + 1
                if dispatch_sequence > 2**63 - 1:
                    raise CommandRefused("job dispatch sequence is exhausted")
                message_fields = {**fields, "dispatch_sequence": dispatch_sequence}
                claim = {
                    "subject_id": job_id,
                    "previous_dispatch_sequence": job["current_dispatch_sequence"],
                    "dispatch_sequence": dispatch_sequence,
                    "range_id": uuid.UUID(fields["work_budget"]["range_id"]),
                    "dispatch_token": fields["dispatch_token"],
                }
            elif command_type == "cancel_job":
                if (not isinstance(account_user_id, uuid.UUID)
                        or not isinstance(fields.get("dispatch_sequence"), int)
                        or isinstance(fields.get("dispatch_sequence"), bool)
                        or not isinstance(fields.get("dispatch_token"), str)
                        or not fields["dispatch_token"].isascii()):
                    raise CommandRefused("job cancellation identity is unavailable")
                await hold_the_account(session, account_user_id, wait=True)
                job_id = uuid.UUID(fields["job_id"])
                job = (await session.execute(
                    text(
                        "SELECT user_id, state, current_dispatch_sequence FROM jobs "
                        "WHERE id = :job_id FOR UPDATE"
                    ),
                    {"job_id": job_id},
                )).mappings().one_or_none()
                if (job is None or job["user_id"] != account_user_id
                        or job["state"] != "cancelled"
                        or job["current_dispatch_sequence"] != fields["dispatch_sequence"]):
                    raise CommandRefused("job cancellation is stale")
                cancel_attempt = (await session.execute(
                    text(
                        "SELECT worker_id, incarnation, owner_epoch, dispatch_token_hash, state "
                        "FROM job_attempts WHERE job_id = :job_id "
                        "AND dispatch_sequence = :dispatch_sequence FOR UPDATE"
                    ),
                    {"job_id": job_id, "dispatch_sequence": fields["dispatch_sequence"]},
                )).mappings().one_or_none()
                token_hash = hashlib.sha256(fields["dispatch_token"].encode("ascii")).hexdigest()
                if (cancel_attempt is None
                        or not hmac.compare_digest(cancel_attempt["dispatch_token_hash"], token_hash)):
                    raise CommandRefused("job cancellation token is stale")
            lease = (await session.execute(
                text(
                    "SELECT owner_id, owner_epoch, lease_id, expires_at FROM scheduler_leases "
                    "WHERE region = :region FOR UPDATE"
                ),
                {"region": REGION},
            )).mappings().one_or_none()
            if lease is None or lease["owner_id"] != SCHEDULER_OWNER_ID:
                raise CommandRefused("regional scheduler lease is unavailable")
            worker = (await session.execute(
                text(
                    "SELECT region, owner_epoch, lease_id, lease_expires_at, grant_nonce, "
                    "grant_expires_at, grant_ready, next_command_sequence, acknowledged_floor, "
                    "transport_owner_id "
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
            try:
                body = preflight_worker_control(
                    message,
                    expected_worker_id=worker_id,
                    expected_incarnation=incarnation,
                )
            except ValueError:
                raise CommandRefused("command does not match protocol6") from None
            if len(body) > _size_limit(command_type):
                raise CommandRefused("command exceeds protocol6 size limit")
            retained, pending_bytes = (await session.execute(
                text(
                    "SELECT count(*) AS retained, "
                    "coalesce(sum(cmd.complete_bytes) FILTER (WHERE cmd.state = 'pending'),0) "
                    "AS pending FROM worker_commands AS cmd "
                    "WHERE cmd.worker_id = :worker_id AND cmd.incarnation = :incarnation "
                    "AND cmd.command_sequence > :floor"
                ),
                {
                    "worker_id": worker_id,
                    "incarnation": incarnation,
                    "floor": worker["acknowledged_floor"],
                },
            )).one()
            if retained >= MAX_RETAINED_COMMANDS or pending_bytes + len(body) > MAX_PENDING_BYTES:
                raise CommandRefused("worker command journal is full")
            try:
                keyring = get_key_ring()
                aad = _aad(worker_id, incarnation, sequence, command_id, body_hash)
                encrypted = keyring.encrypt("worker-command", body, aad)
                key_version = keyring.version_of(encrypted)
            except KeyRingError:
                raise CommandRefused("worker command key is unavailable") from None
            if claim is not None:
                await session.execute(
                    text(
                        "INSERT INTO job_attempts "
                        "(job_id, dispatch_sequence, command_id, worker_id, incarnation, owner_epoch, "
                        "state, dispatch_token_hash, range_id, created_at) VALUES (:job_id, "
                        ":dispatch_sequence, :command_id, :worker_id, :incarnation, :owner_epoch, "
                        "'dispatching', :token_hash, :range_id, clock_timestamp())"
                    ),
                    {
                        "job_id": claim["subject_id"],
                        "dispatch_sequence": claim["dispatch_sequence"],
                        "command_id": command_id,
                        "worker_id": worker_id,
                        "incarnation": incarnation,
                        "owner_epoch": worker["owner_epoch"],
                        "token_hash": hashlib.sha256(claim["dispatch_token"].encode()).hexdigest(),
                        "range_id": claim["range_id"],
                    },
                )
                updated = await session.execute(
                    text(
                        "UPDATE jobs SET state = 'running', current_dispatch_sequence = :sequence, "
                        "dispatched_at = clock_timestamp() WHERE id = :job_id AND state = 'queued' "
                        "AND current_dispatch_sequence = :previous_sequence RETURNING id"
                    ),
                    {
                        "sequence": claim["dispatch_sequence"],
                        "job_id": claim["subject_id"],
                        "previous_sequence": claim["previous_dispatch_sequence"],
                    },
                )
                if updated.scalar_one_or_none() is None:
                    raise CommandRefused("job claim changed before commit")
            created_at = await session.scalar(
                text(
                    "INSERT INTO worker_commands "
                    "(worker_id, incarnation, command_sequence, command_id, kind, body_hash, "
                    "key_version, encrypted_body, complete_bytes, state) "
                    "VALUES (:worker_id, :incarnation, :sequence, :command_id, :kind, :body_hash, "
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
        return StoredCommand(worker_id, incarnation, sequence, command_id,
                             body_hash, body, created_at)


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
                "SELECT worker.owner_epoch, worker.lease_id "
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
            return None
        row = (await session.execute(
            text(
                "SELECT command_sequence, command_id, body_hash, key_version, encrypted_body, "
                "created_at FROM worker_commands WHERE worker_id = :worker_id "
                "AND incarnation = :incarnation AND state = 'pending' "
                "ORDER BY command_sequence LIMIT 1"
            ),
            {"worker_id": worker_id, "incarnation": incarnation},
        )).mappings().one_or_none()
        if row is None:
            return None
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
        return StoredCommand(
            worker_id,
            incarnation,
            row["command_sequence"],
            row["command_id"],
            row["body_hash"],
            plaintext,
            row["created_at"],
        )


async def acknowledge(message: dict) -> bool:
    if db.session_factory is None:
        raise CommandRefused("durable command store unavailable")
    try:
        validate_message(message)
        worker_id = message["worker_id"]
        incarnation = uuid.UUID(message["incarnation"])
        sequence = message["command_sequence"]
        command_id = uuid.UUID(message["command_id"])
        body_hash = message["body_hash"]
        ack_body = canonical_bytes(message)
    except (KeyError, ValueError, TypeError):
        raise CommandRefused("command acknowledgement is invalid") from None
    if message["status"] == "refused" and message.get("code") == "sequence_gap":
        return False
    async with db.session_factory() as session:
        async with session.begin():
            row = (await session.execute(
                text(
                    "SELECT state, command_id, body_hash, key_version, encrypted_body, ack_body "
                    "FROM worker_commands "
                    "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                    "AND command_sequence = :sequence FOR UPDATE"
                ),
                {"worker_id": worker_id, "incarnation": incarnation, "sequence": sequence},
            )).mappings().one_or_none()
            if row is None or row["command_id"] != command_id or row["body_hash"] != body_hash:
                return False
            if row["state"] == "acknowledged":
                return row["ack_body"] == ack_body
            if row["state"] != "pending":
                return False
            try:
                command_body = get_key_ring().decrypt(
                    "worker-command",
                    row["encrypted_body"],
                    _aad(worker_id, incarnation, sequence, command_id, body_hash),
                )
                command = decode_control(command_body)
            except (KeyRingError, ValueError):
                raise CommandRefused("pending command cannot be verified") from None
            if not _ack_matches(command, message):
                return False
            await session.execute(
                text(
                    "UPDATE worker_commands SET state = 'acknowledged', encrypted_body = NULL, "
                    "key_version = NULL, acknowledged_at = clock_timestamp(), ack_status = :status, "
                    "ack_code = :code, expected_sequence = :expected_sequence, ack_body = :ack_body "
                    "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                    "AND command_sequence = :sequence"
                ),
                {
                    "worker_id": worker_id,
                    "incarnation": incarnation,
                    "sequence": sequence,
                    "status": message["status"],
                    "code": message.get("code"),
                    "expected_sequence": message.get("expected_sequence"),
                    "ack_body": ack_body,
                },
            )
            # The connection row first: commit_command holds it while it
            # allocates the next sequence, so the pending minimum read after
            # this lock cannot miss a command committed in between.
            next_sequence = await session.scalar(
                text(
                    "SELECT next_command_sequence FROM worker_connections "
                    "WHERE worker_id = :worker_id AND incarnation = :incarnation FOR UPDATE"
                ),
                {"worker_id": worker_id, "incarnation": incarnation},
            )
            first_pending = await session.scalar(
                text(
                    "SELECT min(command_sequence) FROM worker_commands "
                    "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                    "AND state = 'pending'"
                ),
                {"worker_id": worker_id, "incarnation": incarnation},
            )
            floor = (first_pending - 1) if first_pending is not None else next_sequence - 1
            await session.execute(
                text(
                    "UPDATE worker_connections SET acknowledged_floor = greatest(acknowledged_floor, "
                    ":floor) WHERE worker_id = :worker_id AND incarnation = :incarnation"
                ),
                {"worker_id": worker_id, "incarnation": incarnation, "floor": floor},
            )
            await session.execute(
                text(
                    "DELETE FROM worker_commands "
                    "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                    "AND state = 'acknowledged' AND command_sequence <= :floor"
                ),
                {"worker_id": worker_id, "incarnation": incarnation, "floor": floor},
            )
    return True
