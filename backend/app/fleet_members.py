"""GPU machines an administrator connects, each with a credential of its own (issue #885).

An administrator mints a one-use join token for a named machine. The machine
spends it once at the enroll route for a credential that is its own. Nothing
durable holds a token or a credential, only its SHA-256.
"""

import asyncio
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from starlette.responses import Response

from app import audit, auth_mode, db, realtime, sessions
from app.auth import current_principal, origin_listed, require_role
from app.manifests import StorableStr
from app.rate_limit import charge_address
from app.tables import FleetMember, User

logger = logging.getLogger("potocolom.fleet_members")
JOIN_TTL = timedelta(hours=1)
CREDENTIAL_PREFIX = "ptc_fleet_"
# One answer for unknown, spent, expired and revoked, so the route is not an
# oracle for which of those a token was.
INVALID_JOIN = "invalid or expired join token"
MAX_BASE_URL = 255
ENROLL_BUCKET = "fleet-enroll"
# A last-seen write that takes longer than this is dropped. Its callers are a
# registration and a socket's cleanup, which must not wait on the database.
LAST_SEEN_TIMEOUT_SECONDS = 2.0


def _worker_id(member_id: uuid.UUID) -> str:
    return f"{realtime.MEMBER_WORKER_ID_PREFIX}{member_id.hex}"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


router = APIRouter()


class CreateRequest(BaseModel):
    label: StorableStr = Field(min_length=1, max_length=100)
    device: Literal["cuda", "rocm"]


@router.post("/api/v1/admin/fleet/members", status_code=201)
async def create_member(
    body: CreateRequest,
    http: Request,
    admin: User = Depends(require_role("admin")),
) -> dict:
    if auth_mode.current() == auth_mode.ACCOUNTS:
        # Creating a member is a credential change, so it needs the same recent
        # sign-in as an administrator invitation. None mode has no session to be
        # recent: the Origin check every unsafe request gets there is its guard.
        principal = await current_principal(http)
        if not sessions.is_recent(principal.session):
            raise HTTPException(status_code=403, detail="recent authentication required")
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    member_id = uuid.uuid4()
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + JOIN_TTL
    async with db.session_factory() as session:
        async with session.begin():
            session.add(FleetMember(
                id=member_id,
                worker_id=_worker_id(member_id),
                label=body.label,
                device=body.device,
                join_token_hash=sessions.token_hash(token),
                join_expires_at=expires_at,
                created_by=admin.id,
            ))
    await audit.record("fleet.member_created", actor=admin, object_ids=[str(member_id)])
    return {
        "id": str(member_id),
        "label": body.label,
        "device": body.device,
        "join_token": token,
        "expires_at": expires_at.isoformat(),
    }


def _state(row: FleetMember, now: datetime) -> str:
    if row.revoked_at is not None:
        return "revoked"
    if row.credential_hash is None:
        return "expired" if row.join_expires_at <= now else "waiting"
    return "connected" if row.worker_id in realtime.workers else "offline"


@router.get("/api/v1/admin/fleet/members")
async def list_members(_admin: User = Depends(require_role("admin"))) -> list[dict]:
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        rows = (await session.execute(
            select(FleetMember).order_by(FleetMember.created_at, FleetMember.id)
        )).scalars().all()
    now = datetime.now(timezone.utc)
    return [
        {
            "id": str(row.id),
            "worker_id": row.worker_id,
            "label": row.label,
            "device": row.device,
            "state": _state(row, now),
            "paused": row.paused,
            "created_at": _iso(row.created_at),
            "enrolled_at": _iso(row.enrolled_at),
            "last_seen_at": _iso(row.last_seen_at),
            "join_expires_at": _iso(row.join_expires_at),
        }
        for row in rows
    ]


@router.delete("/api/v1/admin/fleet/members/{member_id}", status_code=204)
async def revoke_member(
    member_id: uuid.UUID,
    admin: User = Depends(require_role("admin")),
) -> Response:
    """The row stays as the record. A live socket of the member is closed here."""
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            revoked = (await session.execute(
                update(FleetMember)
                .where(FleetMember.id == member_id, FleetMember.revoked_at.is_(None))
                .values(revoked_at=func.now(), join_token_hash=None)
                .returning(FleetMember.id)
            )).first()
            known = revoked is not None or (await session.execute(
                select(FleetMember.id).where(FleetMember.id == member_id)
            )).first() is not None
    if not known:
        raise HTTPException(status_code=404, detail="Not Found")
    if revoked is not None:
        # Only the delete that revoked: a repeat changes nothing to record. The
        # socket close is the same call the reaper makes, and the fleet
        # handler's cleanup does the rest. Only this process's registry is
        # looked at, so with several API processes a socket held by another
        # one stays open; the refusal at the next handshake still holds.
        worker = realtime.workers.get(_worker_id(member_id))
        if worker is not None:
            try:
                await asyncio.wait_for(realtime.safe_send(worker.ws.close()),
                                       realtime.CLOSE_TIMEOUT)
            except TimeoutError:
                logger.warning("revoked fleet member %s did not close in time", member_id)
        await audit.record("fleet.member_revoked", actor=admin, object_ids=[str(member_id)])
    return Response(status_code=204)


# ponytail: one lock for the process, since a pause is a rare admin action. It
# orders requests on this process only; the live flag of a worker connected to
# another API process changes at its next connection.
_PAUSE_LOCK = asyncio.Lock()


class PauseRequest(BaseModel):
    paused: bool


@router.patch("/api/v1/admin/fleet/members/{member_id}", status_code=204)
async def set_member_paused(
    member_id: uuid.UUID,
    body: PauseRequest,
    admin: User = Depends(require_role("admin")),
) -> Response:
    """A paused member keeps its socket and its current work. It is given no new job or session.

    The row is locked for the change, so two pauses cannot both record an event
    for one change. The flag on the live worker is set after the commit, so the
    scheduler never sees a pause the database does not hold, and under one lock
    with the commit, so an earlier request cannot set it after a later one.
    """
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with _PAUSE_LOCK:
        changed, worker_id = await _store_pause(member_id, body.paused)
        # The live flag is set even when nothing changed, so a worker that disagrees
        # with the row is corrected. Resume wakes the admission queue at once, since
        # a session waiting for this member would otherwise wait for the next sweep.
        worker = realtime.workers.get(worker_id)
        if worker is not None:
            worker.paused = body.paused
            if not body.paused:
                realtime.admit_queued()
    if changed:
        action = "fleet.member_paused" if body.paused else "fleet.member_resumed"
        await audit.record(action, actor=admin, object_ids=[str(member_id)])
    return Response(status_code=204)


async def _store_pause(member_id: uuid.UUID, paused: bool) -> tuple[bool, str]:
    """Write the flag and answer whether it changed and the member's worker id."""
    assert db.session_factory is not None
    async with db.session_factory() as session:
        async with session.begin():
            row = (await session.execute(
                select(FleetMember.worker_id, FleetMember.paused, FleetMember.revoked_at)
                .where(FleetMember.id == member_id)
                .with_for_update()
            )).first()
            if row is None:
                raise HTTPException(status_code=404, detail="Not Found")
            if row.revoked_at is not None:
                raise HTTPException(status_code=409, detail="fleet member is revoked")
            changed = row.paused != paused
            if changed:
                await session.execute(
                    update(FleetMember).where(FleetMember.id == member_id)
                    .values(paused=paused)
                )
    return changed, row.worker_id


def _bare_origin(value: str) -> str:
    """Scheme, host and port: the address a worker is reached at, and nothing under it.

    The server never fetches this address, it only hands it to that worker as
    the base for its upload and source-image links, so a refusal here is about
    what those links can later be made to mean, not about a request the server
    sends.
    """
    parts = urlsplit(value)
    if (len(value) > MAX_BASE_URL or not value.isascii() or not value.isprintable()
            or " " in value or parts.scheme not in ("http", "https") or not parts.hostname
            or "@" in parts.netloc or parts.path not in ("", "/")
            or "?" in value or "#" in value):
        raise ValueError("base_url must be an http or https origin, with no userinfo, "
                         "path, query or fragment")
    parts.port  # a port out of range or not a number raises ValueError here
    return f"{parts.scheme}://{parts.netloc}"


class EnrollRequest(BaseModel):
    join_token: StorableStr = Field(min_length=1, max_length=256)
    base_url: StorableStr

    @field_validator("base_url")
    @classmethod
    def _checked_base_url(cls, value: str) -> str:
        return _bare_origin(value)


async def enroll(join_token: str, base_url: str) -> tuple[uuid.UUID, str]:
    """Spends the join token and returns the member id and its credential.

    One conditional UPDATE does the whole spend, so two requests with one token
    cannot both win: the second waits on the row and then finds it no longer
    matches. Raising the refusal rolls back, and there is nothing else to undo.
    """
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    credential = CREDENTIAL_PREFIX + secrets.token_urlsafe(32)
    async with db.session_factory() as session:
        async with session.begin():
            spent = (await session.execute(
                update(FleetMember)
                .where(
                    FleetMember.join_token_hash == sessions.token_hash(join_token),
                    FleetMember.credential_hash.is_(None),
                    FleetMember.revoked_at.is_(None),
                    FleetMember.join_expires_at > func.now(),
                )
                .values(credential_hash=sessions.token_hash(credential),
                        base_url=base_url,
                        enrolled_at=func.now(),
                        join_token_hash=None)
                .returning(FleetMember.id)
            )).first()
    if spent is None:
        raise HTTPException(status_code=403, detail=INVALID_JOIN)
    return spent.id, credential


async def member_for_credential(credential: str):
    """The member row a credential belongs to while it is not revoked, else None.

    Raises when the database cannot answer, and the handshake reads that as an
    outage. Its own short session, so the handshake holds no lock that a
    registration takes.
    """
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    async with db.session_factory() as session:
        return (await session.execute(
            select(FleetMember.id, FleetMember.worker_id, FleetMember.base_url,
                   FleetMember.paused).where(
                FleetMember.credential_hash == sessions.token_hash(credential),
                FleetMember.revoked_at.is_(None),
            )
        )).first()


async def record_last_seen(member_id: uuid.UUID) -> None:
    """Best effort. A failed or slow write must not stop a registration or a cleanup."""
    try:
        await asyncio.wait_for(_write_last_seen(member_id), LAST_SEEN_TIMEOUT_SECONDS)
    except Exception as error:
        logger.warning("could not record last seen for fleet member %s: %s",
                       member_id, type(error).__name__)


async def _write_last_seen(member_id: uuid.UUID) -> None:
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            await session.execute(
                update(FleetMember)
                .where(FleetMember.id == member_id)
                .values(last_seen_at=func.now())
            )


@router.post("/api/v1/fleet/enroll")
async def enroll_route(body: EnrollRequest, http: Request) -> dict:
    """No session: the join token is the proof. A browser's Origin is still held to
    the allowlist, and an absent Origin is what a worker sends, so it passes."""
    origin = http.headers.get("origin")
    if origin is not None and not origin_listed(origin):
        raise HTTPException(status_code=403, detail="origin not allowed")
    await charge_address(http, ENROLL_BUCKET)
    member_id, credential = await enroll(body.join_token, body.base_url)
    peer = http.client.host if http.client else ""
    await audit.record("fleet.member_enrolled", object_ids=[str(member_id), peer])
    return {"worker_id": _worker_id(member_id), "credential": credential}
