"""Invitation-only registration for an install that has no mail service.

The link is handed over out of band, so the plaintext token lives only in the
response that mints it. Nothing durable holds it, only its SHA-256.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, update
from starlette.responses import Response

from app import audit, db, mail, sessions
from app.accounts import issue_session
from app.enable import _checked_email
from app.auth import current_principal, require_accounts_mode, require_role
from app.passwords import PasswordRejected, hash_password
from app.settings import get_settings
from app.tables import AuthIdentity, Invitation, User

INVITATION_TTL = timedelta(hours=72)
INVALID_INVITATION = "invalid or expired invitation"

router = APIRouter(dependencies=[Depends(require_accounts_mode)])


def _invitation_link(token: str) -> str:
    return f"{get_settings().public_url.rstrip('/')}/join#{token}"


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _minted(
    invitation_id: uuid.UUID,
    email: str,
    role: str,
    token: str,
    expires_at: datetime,
) -> dict:
    return {
        "id": str(invitation_id),
        "email": email,
        "role": role,
        "token": token,
        "expires_at": expires_at.isoformat(),
    }


class InviteRequest(BaseModel):
    email: str
    role: Literal["viewer", "user", "admin"]


@router.post("/api/v1/invitations", status_code=201)
async def invite(
    request: InviteRequest,
    admin: User = Depends(require_role("admin")),
    principal: sessions.Resolved = Depends(current_principal),
) -> dict:
    if request.role == "admin" and not sessions.is_recent(principal.session):
        # This route reaches the same end state as a promotion: a live
        # administrator. Charging only the promotion for recent authentication
        # would leave this one as the cheaper way to the same place.
        raise HTTPException(status_code=403, detail="recent authentication required")
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    address = _checked_email(request.email)
    normalized = address.lower()
    invitation_id = uuid.uuid4()
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + INVITATION_TTL
    async with db.session_factory() as session:
        async with session.begin():
            held = (await session.execute(
                select(User.id).where(func.lower(func.btrim(User.email)) == normalized)
            )).first()
            if held is not None:
                raise HTTPException(status_code=409, detail="that address already has an account")
            open_already = (await session.execute(
                select(Invitation.id).where(
                    func.lower(func.btrim(Invitation.email)) == normalized,
                    Invitation.accepted_at.is_(None),
                    Invitation.revoked_at.is_(None),
                )
            )).first()
            if open_already is not None:
                raise HTTPException(status_code=409,
                                    detail="that address already has an open invitation")
            session.add(Invitation(
                id=invitation_id,
                email=address,
                role=request.role,
                invited_by=admin.id,
                token_hash=_token_hash(token),
                expires_at=expires_at,
            ))
            # In the same transaction as the capability it carries: an
            # invitation that exists but was never queued, or a queued mail
            # for an invitation that rolled back, are both worse than either
            # happening or neither. With no mail backend this queues nothing
            # and the link is copied by hand, which is the default.
            await mail.queue(session, address, "invitation",
                             {"link": _invitation_link(token), "role": request.role,
                              "expires_at": expires_at.isoformat()})
    await audit.record("invitation.created", actor=admin,
                       object_ids=[address, request.role],
                       severity="high" if request.role == "admin" else "info")
    return _minted(invitation_id, address, request.role, token, expires_at)


@router.get("/api/v1/invitations", dependencies=[Depends(require_role("admin"))])
async def open_invitations() -> list[dict]:
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        rows = (await session.execute(
            select(Invitation)
            .where(Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None))
            .order_by(Invitation.created_at)
        )).scalars().all()
    return [
        {
            "id": str(row.id),
            "email": row.email,
            "role": row.role,
            "expires_at": row.expires_at.isoformat(),
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@router.delete("/api/v1/invitations/{invitation_id}", status_code=204,
               dependencies=[Depends(require_role("admin"))])
async def revoke(invitation_id: uuid.UUID) -> Response:
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            revoked = (await session.execute(
                update(Invitation)
                .where(Invitation.id == invitation_id, Invitation.accepted_at.is_(None))
                .values(revoked_at=func.now())
                .returning(Invitation.id)
            )).first()
    if revoked is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return Response(status_code=204)


@router.post("/api/v1/invitations/{invitation_id}/reveal",
             dependencies=[Depends(require_role("admin"))])
async def reveal(invitation_id: uuid.UUID) -> dict:
    """Re-mints rather than shows: a link nobody can see may have leaked on the
    way, so the copy it replaces has to stop working."""
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    token = secrets.token_urlsafe(32)
    async with db.session_factory() as session:
        async with session.begin():
            row = (await session.execute(
                update(Invitation)
                .where(
                    Invitation.id == invitation_id,
                    Invitation.accepted_at.is_(None),
                    Invitation.revoked_at.is_(None),
                )
                .values(token_hash=_token_hash(token))
                .returning(Invitation.email, Invitation.role, Invitation.expires_at)
            )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return _minted(invitation_id, row.email, row.role, token, row.expires_at)


class RegisterRequest(BaseModel):
    token: str
    password: str


@router.post("/api/v1/auth/register", status_code=204)
async def register(request: RegisterRequest) -> Response:
    """Claims an invitation and returns a clean session.

    The invitation carries the address, so a claimant cannot redirect it, and
    unknown, expired, revoked and already accepted all answer alike so the
    route is not an oracle. Argon2id is paid only behind a valid invitation,
    and raising here rolls the consumption back, so a rejected password leaves
    the invitation usable. The session carries no recent-authentication grant:
    the link proved a capability, not a person.
    """
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    user_id = uuid.uuid4()
    async with db.session_factory() as session:
        async with session.begin():
            claimed = (await session.execute(
                update(Invitation)
                .where(
                    Invitation.token_hash == _token_hash(request.token),
                    Invitation.accepted_at.is_(None),
                    Invitation.revoked_at.is_(None),
                    Invitation.expires_at > func.now(),
                )
                .values(accepted_at=func.now())
                .returning(Invitation.id, Invitation.email, Invitation.role)
            )).first()
            if claimed is None:
                raise HTTPException(status_code=403, detail=INVALID_INVITATION)
            try:
                password_hash = await to_thread.run_sync(hash_password, request.password)
            except PasswordRejected as rejected:
                raise HTTPException(status_code=400,
                                    detail="password does not meet the policy") from rejected
            session.add(User(id=user_id, email=claimed.email, role=claimed.role,
                             state="active", mail_verified=False))
            await session.flush()
            session.add(AuthIdentity(user_id=user_id, provider="password",
                                     subject=claimed.email.lower(),
                                     password_hash=password_hash))
            await session.execute(
                update(Invitation)
                .where(Invitation.id == claimed.id)
                .values(accepted_user_id=user_id)
            )
    async with db.session_factory() as session:
        owner = await session.get(User, user_id)
    if owner is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    response = Response(status_code=204)
    issue_session(response, await sessions.mint(owner, remember_me=False))
    return response
