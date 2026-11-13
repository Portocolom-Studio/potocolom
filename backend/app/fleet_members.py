"""GPU machines an administrator connects, each with a credential of its own (issue #885).

An administrator mints a one-use join token for a named machine. The machine
spends it once at the enroll route for a credential that is its own. Nothing
durable holds a token or a credential, only its SHA-256.
"""

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
from app.auth import check_origin, current_principal, origin_listed, require_role
from app.manifests import StorableStr
from app.rate_limit import charge_address
from app.tables import FleetMember, User

JOIN_TTL = timedelta(hours=1)
CREDENTIAL_PREFIX = "ptc_fleet_"
# One answer for unknown, spent, expired and revoked, so the route is not an
# oracle for which of those a token was.
INVALID_JOIN = "invalid or expired join token"
MAX_BASE_URL = 255
ENROLL_BUCKET = "fleet-enroll"


def _worker_id(member_id: uuid.UUID) -> str:
    return f"m-{member_id.hex}"


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
    check_origin(http)
    if auth_mode.current() == auth_mode.ACCOUNTS:
        # Creating a member is a credential change, so it needs the same recent
        # sign-in as an administrator invitation. None mode has no session to be
        # recent: the origin check above is its only guard.
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
    http: Request,
    admin: User = Depends(require_role("admin")),
) -> Response:
    """The row stays as the record. Closing a socket it still holds is a later change."""
    check_origin(http)
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
        # Only the delete that revoked: a repeat changes nothing to record.
        await audit.record("fleet.member_revoked", actor=admin, object_ids=[str(member_id)])
    return Response(status_code=204)


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
