"""PostgreSQL authority for regional leases and worker incarnations."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from app import db
from app.account_lock import hold_the_account
from app.protocol6 import preflight_worker_control

logger = logging.getLogger("potocolom.worker_authority")

REGION = "local"
LEASE_SECONDS = 10
HEARTBEAT_SECONDS = 90
SCHEDULER_OWNER_ID = uuid.uuid4()
TRANSPORT_OWNER_ID = uuid.uuid4()


class AuthorityUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class SchedulerLease:
    region: str
    owner_id: uuid.UUID
    owner_epoch: int
    lease_id: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True)
class WorkGrant:
    worker_id: str
    incarnation: uuid.UUID
    region: str
    owner_epoch: int
    lease_id: uuid.UUID
    lease_expires_at: datetime
    grant_nonce: uuid.UUID
    ready: bool
    remaining_ms: int


_lease: SchedulerLease | None = None


async def acquire_scheduler_lease(region: str = REGION) -> SchedulerLease:
    global _lease
    if db.session_factory is None:
        raise AuthorityUnavailable("durable authority unavailable")
    async with db.session_factory() as session:
        result = await session.execute(
            text(
                "INSERT INTO scheduler_leases "
                "(region, owner_id, owner_epoch, lease_id, expires_at) "
                "VALUES (:region, :owner_id, 1, :lease_id, "
                "clock_timestamp() + (:lease_seconds * interval '1 second')) "
                "ON CONFLICT (region) DO UPDATE SET "
                "owner_id = EXCLUDED.owner_id, "
                "owner_epoch = CASE WHEN scheduler_leases.owner_id = EXCLUDED.owner_id "
                "AND scheduler_leases.expires_at > clock_timestamp() "
                "THEN scheduler_leases.owner_epoch ELSE scheduler_leases.owner_epoch + 1 END, "
                "lease_id = CASE WHEN scheduler_leases.owner_id = EXCLUDED.owner_id "
                "AND scheduler_leases.expires_at > clock_timestamp() "
                "THEN scheduler_leases.lease_id ELSE EXCLUDED.lease_id END, "
                "expires_at = clock_timestamp() + (:lease_seconds * interval '1 second') "
                "WHERE scheduler_leases.owner_id = EXCLUDED.owner_id "
                "OR scheduler_leases.expires_at <= clock_timestamp() "
                "RETURNING region, owner_id, owner_epoch, lease_id, expires_at"
            ),
            {
                "region": region,
                "owner_id": SCHEDULER_OWNER_ID,
                "lease_id": uuid.uuid4(),
                "lease_seconds": LEASE_SECONDS,
            },
        )
        row = result.mappings().one_or_none()
        if row is None:
            await session.rollback()
            raise AuthorityUnavailable("regional scheduler lease is held")
        await session.execute(
            text(
                "UPDATE worker_connections SET lease_expires_at = :expires_at "
                "WHERE region = :region AND owner_epoch = :owner_epoch AND lease_id = :lease_id "
                "AND closed_at IS NULL"
            ),
            {
                "expires_at": row["expires_at"],
                "region": row["region"],
                "owner_epoch": row["owner_epoch"],
                "lease_id": row["lease_id"],
            },
        )
        await session.commit()
    _lease = SchedulerLease(**row)
    return _lease


async def renew_scheduler_lease() -> SchedulerLease:
    global _lease
    lease = _lease
    if lease is None or db.session_factory is None:
        raise AuthorityUnavailable("regional scheduler lease is unavailable")
    async with db.session_factory() as session:
        result = await session.execute(
            text(
                "UPDATE scheduler_leases SET "
                "expires_at = clock_timestamp() + (:lease_seconds * interval '1 second') "
                "WHERE region = :region AND owner_id = :owner_id "
                "AND owner_epoch = :owner_epoch AND lease_id = :lease_id "
                "AND expires_at > clock_timestamp() "
                "RETURNING region, owner_id, owner_epoch, lease_id, expires_at"
            ),
            {
                "region": lease.region,
                "owner_id": SCHEDULER_OWNER_ID,
                "owner_epoch": lease.owner_epoch,
                "lease_id": lease.lease_id,
                "lease_seconds": LEASE_SECONDS,
            },
        )
        row = result.mappings().one_or_none()
        if row is None:
            await session.rollback()
            _lease = None
            raise AuthorityUnavailable("regional scheduler lease was lost")
        await session.execute(
            text(
                "UPDATE worker_connections SET lease_expires_at = :expires_at "
                "WHERE region = :region AND owner_epoch = :owner_epoch AND lease_id = :lease_id "
                "AND closed_at IS NULL"
            ),
            {
                "expires_at": row["expires_at"],
                "region": row["region"],
                "owner_epoch": row["owner_epoch"],
                "lease_id": row["lease_id"],
            },
        )
        await session.commit()
    _lease = SchedulerLease(**row)
    return _lease


def current_lease() -> SchedulerLease | None:
    return _lease


async def maintain_scheduler_lease() -> None:
    while True:
        if db.session_factory is None:
            await asyncio.sleep(3)
            continue
        # Any failure, a hung query included, must leave the loop running:
        # once it stops, the lease lapses and every v6 worker is fenced out
        # until the process restarts. The 2 s bound and the 1 s retry keep
        # three attempts inside one LEASE_SECONDS window after a success. A
        # lease held by another owner is the normal standby state, so it
        # waits the full interval.
        # asyncio.timeout, not wait_for: on Python 3.11 wait_for returns the
        # result when a cancel lands as the call completes, which swallows
        # shutdown's cancel and leaves this loop running forever.
        try:
            async with asyncio.timeout(2):
                if _lease is None:
                    await acquire_scheduler_lease()
                else:
                    await renew_scheduler_lease()
        except AuthorityUnavailable as error:
            logger.warning("regional scheduler authority unavailable: %s", error)
        except Exception as error:
            logger.warning("scheduler lease maintenance failed: %s", type(error).__name__)
            await asyncio.sleep(1)
            continue
        await asyncio.sleep(3)


async def register_worker(
    *,
    worker_id: str,
    incarnation: uuid.UUID,
    protocol_version: int,
    grant_nonce: uuid.UUID,
    capabilities: list[str],
    manifests: list[dict],
    device: str | None,
    memory_mode: str | None,
    realtime_slots: int = 1,
    realtime_p95_ms: dict | None = None,
    realtime_batch_ms: dict | None = None,
) -> SchedulerLease:
    if db.session_factory is None:
        raise AuthorityUnavailable("durable authority unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            current = (await session.execute(
                text(
                    "SELECT owner_id, region, owner_epoch, lease_id, expires_at "
                    "FROM scheduler_leases WHERE region = :region "
                    "AND expires_at > clock_timestamp() FOR UPDATE"
                ),
                {"region": REGION},
            )).mappings().one_or_none()
            if current is None:
                raise AuthorityUnavailable("regional scheduler lease was lost")
            if current["owner_id"] != SCHEDULER_OWNER_ID:
                # Only the lease holder can commit work to this worker; one
                # registered elsewhere would wait for work that never comes.
                raise AuthorityUnavailable("regional scheduler lease is held by another owner")
            lease = SchedulerLease(**current)
            if await session.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM worker_connections "
                    "WHERE incarnation = :incarnation)"
                ),
                {"incarnation": incarnation},
            ):
                raise AuthorityUnavailable("worker incarnation was already used")
            # An open row this process left behind, or one fenced out by a
            # newer lease, belongs to a connection that is gone: the caller
            # already refused a worker id that is live here. Left open, it
            # would block this worker id until someone closed it by hand.
            await session.execute(
                text(
                    "UPDATE worker_connections SET lifecycle = 'ended', "
                    "closed_at = clock_timestamp() "
                    "WHERE worker_id = :worker_id AND closed_at IS NULL "
                    "AND (transport_owner_id = :transport_owner_id "
                    "OR owner_epoch <> :owner_epoch OR lease_id <> :lease_id)"
                ),
                {"worker_id": worker_id, "transport_owner_id": TRANSPORT_OWNER_ID,
                 "owner_epoch": lease.owner_epoch, "lease_id": lease.lease_id},
            )
            if await session.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM worker_connections "
                    "WHERE worker_id = :worker_id AND closed_at IS NULL)"
                ),
                {"worker_id": worker_id},
            ):
                raise AuthorityUnavailable("worker id is connected to another owner")
            envelope = {
                "device": device,
                "memory_mode": memory_mode,
                "models": manifests,
                "realtime_p95_ms": realtime_p95_ms or {},
                "realtime_batch_ms": realtime_batch_ms or {},
            }
            envelope_ready = bool(manifests) and isinstance(device, str) and bool(device) \
                and isinstance(memory_mode, str) and bool(memory_mode)
            await session.execute(
                text(
                    "INSERT INTO worker_connections "
                    "(worker_id, incarnation, transport_owner_id, region, owner_epoch, lease_id, "
                    "lease_expires_at, grant_nonce, grant_expires_at, protocol_version, "
                    "realtime_slots, capabilities, manifests, device, memory_mode, execution_envelope, "
                    "envelope_revision, lifecycle, last_seen_at, expires_at) "
                    "VALUES (:worker_id, :incarnation, :transport_owner_id, :region, :owner_epoch, "
                    ":lease_id, :lease_expires_at, :grant_nonce, :grant_expires_at, "
                    ":protocol_version, :realtime_slots, CAST(:capabilities AS jsonb), "
                    "CAST(:manifests AS jsonb), :device, :memory_mode, CAST(:envelope AS jsonb), "
                    ":envelope_revision, 'connecting', clock_timestamp(), "
                    "clock_timestamp() + (:heartbeat_seconds * interval '1 second'))"
                ),
                {
                    "worker_id": worker_id,
                    "incarnation": incarnation,
                    "transport_owner_id": TRANSPORT_OWNER_ID,
                    "region": lease.region,
                    "owner_epoch": lease.owner_epoch,
                    "lease_id": lease.lease_id,
                    "lease_expires_at": lease.expires_at,
                    "grant_nonce": grant_nonce,
                    "grant_expires_at": lease.expires_at,
                    "protocol_version": protocol_version,
                    "realtime_slots": realtime_slots,
                    "capabilities": json.dumps(capabilities),
                    "manifests": json.dumps(manifests),
                    "device": device,
                    "memory_mode": memory_mode,
                    "envelope": json.dumps(envelope),
                    "envelope_revision": 1 if envelope_ready else 0,
                    "heartbeat_seconds": HEARTBEAT_SECONDS,
                },
            )
    return lease


async def activate_initial_worker(worker_id: str, incarnation: uuid.UUID) -> bool:
    if db.session_factory is None:
        return False
    async with db.session_factory() as session:
        async with session.begin():
            lease = (await session.execute(
                text(
                    "SELECT owner_epoch, lease_id FROM scheduler_leases WHERE region = :region "
                    "AND expires_at > clock_timestamp() FOR UPDATE"
                ),
                {"region": REGION},
            )).mappings().one_or_none()
            if lease is None:
                return False
            worker = (await session.execute(
                text(
                    "SELECT owner_epoch, lease_id, envelope_revision, manifests, lifecycle "
                    "FROM worker_connections WHERE worker_id = :worker_id "
                    "AND incarnation = :incarnation "
                    "AND transport_owner_id = :transport_owner_id AND closed_at IS NULL FOR UPDATE"
                ),
                {"worker_id": worker_id, "incarnation": incarnation,
                 "transport_owner_id": TRANSPORT_OWNER_ID},
            )).mappings().one_or_none()
            if (worker is None or worker["lifecycle"] != "connecting"
                    or worker["envelope_revision"] < 1
                    or not worker["manifests"]
                    or worker["owner_epoch"] != lease["owner_epoch"]
                    or worker["lease_id"] != lease["lease_id"]):
                return False
            blocked = await session.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM jobs AS job "
                    "JOIN job_attempts AS attempt ON attempt.job_id = job.id "
                    "AND attempt.dispatch_sequence = job.current_dispatch_sequence "
                    "WHERE job.state = 'running' AND attempt.worker_id = :worker_id "
                    "AND attempt.incarnation <> :incarnation)"
                ),
                {"worker_id": worker_id, "incarnation": incarnation},
            )
            if blocked:
                return False
            result = await session.execute(
                text(
                    "UPDATE worker_connections SET lifecycle = 'ready' "
                    "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                    "AND transport_owner_id = :transport_owner_id AND closed_at IS NULL "
                    "AND lifecycle = 'connecting' AND envelope_revision > 0 RETURNING worker_id"
                ),
                {"worker_id": worker_id, "incarnation": incarnation,
                 "transport_owner_id": TRANSPORT_OWNER_ID},
            )
    return result.scalar_one_or_none() is not None


async def close_worker(worker_id: str, incarnation: uuid.UUID) -> None:
    if db.session_factory is None:
        return
    async with db.session_factory() as session:
        await session.execute(
            text(
                "UPDATE worker_connections SET lifecycle = 'ended', closed_at = clock_timestamp() "
                "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                "AND transport_owner_id = :owner_id AND closed_at IS NULL"
            ),
            {
                "worker_id": worker_id,
                "incarnation": incarnation,
                "owner_id": TRANSPORT_OWNER_ID,
            },
        )
        await session.commit()


async def touch_worker(
    worker_id: str, incarnation: uuid.UUID, frame_p95_ms: dict[str, int] | None = None
) -> None:
    if db.session_factory is None:
        raise AuthorityUnavailable("durable authority unavailable")
    async with db.session_factory() as session:
        result = await session.execute(
            text(
                "UPDATE worker_connections SET last_seen_at = clock_timestamp(), "
                "expires_at = clock_timestamp() + (:heartbeat_seconds * interval '1 second'), "
                "observations = CASE WHEN CAST(:frame_p95_ms AS jsonb) IS NULL "
                "THEN observations ELSE "
                "jsonb_set(observations, '{frame_p95_ms}', "
                "coalesce(observations->'frame_p95_ms', '{}'::jsonb) || "
                "CAST(:frame_p95_ms AS jsonb), true) END "
                "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                "AND transport_owner_id = :transport_owner_id AND closed_at IS NULL "
                "AND lifecycle <> 'ended' AND EXISTS (SELECT 1 FROM scheduler_leases "
                "WHERE region = worker_connections.region "
                "AND owner_epoch = worker_connections.owner_epoch "
                "AND lease_id = worker_connections.lease_id "
                "AND expires_at > clock_timestamp()) RETURNING worker_id"
            ),
            {
                "worker_id": worker_id,
                "incarnation": incarnation,
                "transport_owner_id": TRANSPORT_OWNER_ID,
                "heartbeat_seconds": HEARTBEAT_SECONDS,
                "frame_p95_ms": json.dumps(frame_p95_ms) if frame_p95_ms else None,
            },
        )
        if result.scalar_one_or_none() is None:
            await session.rollback()
            raise AuthorityUnavailable("worker authority is no longer current")
        await session.commit()


async def renew_worker_grant(
    worker_id: str,
    incarnation: uuid.UUID,
    grant_nonce: uuid.UUID,
) -> WorkGrant:
    if db.session_factory is None:
        raise AuthorityUnavailable("durable authority unavailable")
    try:
        await activate_initial_worker(worker_id, incarnation)
    except Exception:
        logger.warning("worker activation could not be retried for %s", worker_id)
    async with db.session_factory() as session:
        result = await session.execute(
            text(
                "UPDATE worker_connections AS worker SET "
                "grant_nonce = :grant_nonce, "
                "lease_expires_at = lease.expires_at, "
                "grant_ready = (worker.lifecycle = 'ready' "
                "AND worker.envelope_revision > 0 "
                "AND lease.owner_epoch = worker.owner_epoch AND lease.lease_id = worker.lease_id "
                "AND lease.expires_at > clock_timestamp()), "
                "grant_expires_at = CASE WHEN worker.lifecycle = 'ready' "
                "AND worker.envelope_revision > 0 "
                "AND lease.owner_epoch = worker.owner_epoch "
                "AND lease.lease_id = worker.lease_id AND lease.expires_at > clock_timestamp() "
                "THEN least(lease.expires_at, clock_timestamp() + interval '10 seconds') "
                "ELSE clock_timestamp() END "
                "FROM scheduler_leases AS lease WHERE worker.worker_id = :worker_id "
                "AND worker.incarnation = :incarnation AND worker.transport_owner_id = :transport_owner_id "
                "AND worker.closed_at IS NULL AND worker.region = lease.region "
                "AND worker.grant_nonce <> :grant_nonce "
                "RETURNING worker.worker_id, worker.incarnation, worker.region, worker.owner_epoch, "
                "worker.lease_id, worker.lease_expires_at, worker.grant_nonce, "
                "worker.grant_ready AS ready, "
                "CASE WHEN worker.grant_ready THEN floor(extract(epoch from "
                "(worker.grant_expires_at - clock_timestamp())) * 1000)::bigint ELSE 0 END AS remaining_ms"
            ),
            {
                "worker_id": worker_id,
                "incarnation": incarnation,
                "transport_owner_id": TRANSPORT_OWNER_ID,
                "grant_nonce": grant_nonce,
            },
        )
        row = result.mappings().one_or_none()
        if row is None:
            await session.rollback()
            raise AuthorityUnavailable("worker connection is no longer current")
        await session.commit()
    return WorkGrant(**row)


async def commit_worker_report(
    transport_worker_id: str,
    transport_incarnation: uuid.UUID,
    report: dict,
) -> bytes:
    if db.session_factory is None:
        raise AuthorityUnavailable("worker report authority is unavailable")
    worker_id = report["worker_id"]
    incarnation = uuid.UUID(report["incarnation"])
    if worker_id != transport_worker_id or incarnation != transport_incarnation:
        raise AuthorityUnavailable("worker report identity is not current")
    if "session_id" in report:
        raise AuthorityUnavailable("worker report attempt is unknown")
    subject_id = uuid.UUID(report["job_id"])
    dispatch_sequence = report.get("dispatch_sequence")
    range_id = uuid.UUID(report["range_id"])
    sequence = report["report_sequence"]
    report_hash = report["report_hash"]
    gpu_ms = report["gpu_ms"]
    frames = report.get("frames", report.get("images", 0))
    duration_ms = report["duration_ms"]
    ack_type = {
        "type": "checkpoint_ack",
        "worker_id": worker_id,
        "incarnation": str(incarnation),
        "region": report["region"],
        "owner_epoch": report["owner_epoch"],
        "range_id": str(range_id),
        "report_sequence": sequence,
        "report_hash": report_hash,
        "gpu_ms": gpu_ms,
        "duration_ms": duration_ms,
        "status": "accepted",
        "job_id": str(subject_id),
        "dispatch_sequence": dispatch_sequence,
        "images": frames,
    }
    async with db.session_factory() as session:
        async with session.begin():
            user_id = await session.scalar(
                text("SELECT user_id FROM jobs WHERE id = :id"),
                {"id": subject_id},
            )
            if user_id is None:
                raise AuthorityUnavailable("worker report attempt is unknown")
            await hold_the_account(session, user_id, wait=True)
            entity = (await session.execute(
                text(
                    "SELECT id, state, current_dispatch_sequence FROM jobs "
                    "WHERE id = :id AND user_id = :user_id FOR UPDATE"
                ),
                {"id": subject_id, "user_id": user_id},
            )).mappings().one_or_none()
            if entity is None:
                raise AuthorityUnavailable("worker report attempt is unknown")
            attempt = (await session.execute(
                text(
                    "SELECT worker_id, incarnation, owner_epoch, dispatch_token_hash, range_id, "
                    "gpu_ms, frames, duration_ms, report_sequence FROM job_attempts "
                    "WHERE job_id = :job_id AND dispatch_sequence = :dispatch_sequence FOR UPDATE"
                ),
                {"job_id": subject_id, "dispatch_sequence": dispatch_sequence},
            )).mappings().one_or_none()
            if (attempt is None or attempt["worker_id"] != worker_id
                    or attempt["incarnation"] != incarnation
                    or attempt["owner_epoch"] != report["owner_epoch"]
                    or attempt["range_id"] != range_id):
                raise AuthorityUnavailable("worker report range identity is unknown")
            if report["type"] in {"job_done", "job_failed", "job_cancelled"}:
                token = report.get("dispatch_token")
                if (not isinstance(token, str) or not token.isascii()
                        or not hmac.compare_digest(
                            attempt["dispatch_token_hash"],
                            hashlib.sha256(token.encode("ascii")).hexdigest(),
                        )):
                    raise AuthorityUnavailable("worker report dispatch token is stale")
            job_is_current = entity["current_dispatch_sequence"] == dispatch_sequence
            physical = (await session.execute(
                text(
                    "SELECT kind, subject_id, attempt_id, control_generation, dispatch_sequence, "
                    "state, report_sequence, report_floor, report_hash, gpu_ms, frames, duration_ms "
                    "FROM worker_physical_ranges WHERE range_id = :range_id AND worker_id = :worker_id "
                    "AND incarnation = :incarnation FOR UPDATE"
                ),
                {"range_id": range_id, "worker_id": worker_id, "incarnation": incarnation},
            )).mappings().one_or_none()
            if (physical is None or physical["kind"] != "job"
                    or physical["subject_id"] != subject_id
                    or physical["attempt_id"] is not None
                    or physical["control_generation"] is not None
                    or physical["dispatch_sequence"] != dispatch_sequence):
                raise AuthorityUnavailable("worker report range does not match")
            lease = (await session.execute(
                text(
                    "SELECT owner_id, owner_epoch, lease_id, expires_at FROM scheduler_leases "
                    "WHERE region = :region FOR UPDATE"
                ),
                {"region": report["region"]},
            )).mappings().one_or_none()
            current = (await session.execute(
                text(
                    "SELECT region, owner_epoch, lease_id, grant_ready FROM worker_connections "
                    "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                    "AND transport_owner_id = :transport_owner_id AND closed_at IS NULL FOR UPDATE"
                ),
                {"worker_id": transport_worker_id, "incarnation": transport_incarnation,
                 "transport_owner_id": TRANSPORT_OWNER_ID},
            )).mappings().one_or_none()
            if (lease is None or current is None
                    or lease["owner_epoch"] != current["owner_epoch"]
                    or lease["lease_id"] != current["lease_id"]
                    or current["region"] != report["region"]):
                raise AuthorityUnavailable("worker report authority is stale")
            lease_live = await session.scalar(
                text("SELECT :expires_at > clock_timestamp()"),
                {"expires_at": lease["expires_at"]},
            )
            if not lease_live:
                raise AuthorityUnavailable("worker report authority has expired")
            receipt = (await session.execute(
                text(
                    "SELECT report_hash, ack_body FROM worker_report_receipts "
                    "WHERE range_id = :range_id AND report_sequence = :sequence FOR UPDATE"
                ),
                {"range_id": range_id, "sequence": sequence},
            )).mappings().one_or_none()
            if receipt is not None:
                if receipt["report_hash"] != report_hash:
                    raise AuthorityUnavailable("worker report sequence changed bytes")
                return receipt["ack_body"]
            if sequence <= physical["report_floor"]:
                ack_type.update({"status": "refused", "code": "stale_report",
                                 "gpu_ms": physical["gpu_ms"],
                                 "images": physical["frames"],
                                 "duration_ms": physical["duration_ms"]})
                return preflight_worker_control(ack_type)
            maxima = {
                "gpu_ms": max(physical["gpu_ms"], gpu_ms),
                "frames": max(physical["frames"], frames),
                "duration_ms": max(physical["duration_ms"], duration_ms),
            }
            ack_type.update({
                "gpu_ms": maxima["gpu_ms"],
                "images": maxima["frames"],
                "duration_ms": maxima["duration_ms"],
            })
            ack_body = preflight_worker_control(ack_type)
            await session.execute(
                text(
                    "UPDATE worker_physical_ranges SET report_sequence = :sequence, "
                    "report_hash = :report_hash, gpu_ms = :gpu_ms, frames = :frames, "
                    "duration_ms = :duration_ms WHERE range_id = :range_id"
                ),
                {"sequence": sequence, "report_hash": report_hash, **maxima,
                 "range_id": range_id},
            )
            terminal_state = {
                "job_done": "completed",
                "job_failed": "failed",
                "job_cancelled": "cancelled",
            }.get(report["type"])
            await session.execute(
                text(
                    "UPDATE job_attempts SET report_sequence = :sequence, gpu_ms = :gpu_ms, "
                    "frames = :frames, duration_ms = :duration_ms, "
                    "state = CASE WHEN :complete AND CAST(:terminal_state AS text) IS NOT NULL "
                    "THEN CAST(:terminal_state AS text) ELSE state END, "
                    "terminal_at = CASE WHEN :complete THEN clock_timestamp() ELSE terminal_at END, "
                    "input_fetch_ms = CASE WHEN :complete AND :is_done AND :is_current "
                    "AND :has_input_fetch_ms "
                    "THEN :input_fetch_ms ELSE input_fetch_ms END, "
                    "load_ms = CASE WHEN :complete AND :is_done AND :is_current AND :has_load_ms "
                    "THEN :load_ms ELSE load_ms END, "
                    "postprocess_ms = CASE WHEN :complete AND :is_done AND :is_current "
                    "AND :has_postprocess_ms "
                    "THEN :postprocess_ms ELSE postprocess_ms END, "
                    "terminal_category = CASE WHEN :complete AND :is_done AND :is_current "
                    "THEN :category ELSE terminal_category END, "
                    "terminal_category_score = CASE WHEN :complete AND :is_done AND :is_current "
                    "THEN :category_score ELSE terminal_category_score END, "
                    "terminal_has_thumbnail = CASE WHEN :complete AND :is_done AND :is_current "
                    "THEN :has_thumbnail ELSE terminal_has_thumbnail END, "
                    "terminal_failure_code = CASE WHEN :complete AND :is_failed AND :is_current "
                    "THEN :failure_code ELSE terminal_failure_code END "
                    "WHERE job_id = :subject_id AND dispatch_sequence = :identity"
                ),
                {"sequence": sequence, **maxima, "subject_id": subject_id,
                 "complete": report.get("physical_complete", False),
                 "identity": dispatch_sequence,
                 "terminal_state": terminal_state,
                 "is_done": report["type"] == "job_done",
                 "is_failed": report["type"] == "job_failed",
                 "is_current": job_is_current,
                 "has_input_fetch_ms": "input_fetch_ms" in report,
                 "input_fetch_ms": report.get("input_fetch_ms"),
                 "has_load_ms": "load_ms" in report,
                 "load_ms": report.get("load_ms"),
                 "has_postprocess_ms": "postprocess_ms" in report,
                 "postprocess_ms": report.get("postprocess_ms"),
                 "category": report.get("category"),
                 "category_score": report.get("category_score"),
                 "has_thumbnail": report.get("has_thumbnail", False),
                 "failure_code": report.get("failure_code")},
            )
            await session.execute(
                text(
                    "INSERT INTO worker_report_receipts "
                    "(range_id, report_sequence, report_hash, gpu_ms, frames, duration_ms, ack_body) "
                    "VALUES (:range_id, :sequence, :report_hash, :gpu_ms, :frames, :duration_ms, :ack_body)"
                ),
                {"range_id": range_id, "sequence": sequence, "report_hash": report_hash,
                 **maxima, "ack_body": ack_body},
            )
            evicted_floor = await session.scalar(
                text(
                    "WITH old AS (SELECT report_sequence FROM worker_report_receipts "
                    "WHERE range_id = :range_id ORDER BY report_sequence DESC OFFSET 256), "
                    "removed AS (DELETE FROM worker_report_receipts receipt USING old "
                    "WHERE receipt.range_id = :range_id AND receipt.report_sequence = old.report_sequence "
                    "RETURNING receipt.report_sequence) SELECT max(report_sequence) FROM removed"
                ),
                {"range_id": range_id},
            )
            if evicted_floor is not None:
                await session.execute(
                    text(
                        "UPDATE worker_physical_ranges SET report_floor = greatest(report_floor, :floor) "
                        "WHERE range_id = :range_id"
                    ),
                    {"floor": evicted_floor, "range_id": range_id},
                )
            return ack_body
