"""PostgreSQL authority for regional leases and worker incarnations."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from app import db

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
        try:
            await asyncio.wait_for(
                acquire_scheduler_lease() if _lease is None else renew_scheduler_lease(),
                2,
            )
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
