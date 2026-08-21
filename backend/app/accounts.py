"""Signing in, signing out, and what an account can see about itself."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from starlette.responses import Response

from app import db, sessions
from app.auth import current_principal, require_accounts_mode
from app.passwords import verify_password
from app.settings import get_settings
from app.tables import AuthIdentity, Session, User

router = APIRouter(dependencies=[Depends(require_accounts_mode)])

# One answer for an address nobody holds and a password that does not match,
# so the response cannot be used to learn which accounts exist.
REFUSED = HTTPException(status_code=401, detail="invalid email or password")


class LoginRequest(BaseModel):
    email: str
    password: str
    remember_me: bool = False


def issue_session(response: Response, issued: sessions.Issued) -> None:
    public_url = get_settings().public_url
    session_name, csrf_name = sessions.cookie_names(public_url)
    secure = public_url.startswith("https")
    # Host-only on purpose: no domain, so a sibling host cannot be handed it.
    response.set_cookie(session_name, issued.token, path="/", samesite="lax",
                        secure=secure, httponly=True)
    # The browser has to read this one to echo it back on unsafe requests.
    response.set_cookie(csrf_name, issued.csrf, path="/", samesite="lax",
                        secure=secure, httponly=False)


def _clear(response: Response) -> None:
    session_name, csrf_name = sessions.cookie_names(get_settings().public_url)
    response.delete_cookie(session_name, path="/")
    response.delete_cookie(csrf_name, path="/")


@router.post("/api/v1/auth/login", status_code=204)
async def login(request: LoginRequest) -> Response:
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    subject = request.email.strip().lower()
    async with db.session_factory() as session:
        found = (await session.execute(
            select(AuthIdentity, User)
            .join(User, User.id == AuthIdentity.user_id)
            .where(AuthIdentity.provider == "password", AuthIdentity.subject == subject)
        )).first()
    if found is None:
        raise REFUSED
    identity, user = found
    if identity.password_hash is None or not verify_password(identity.password_hash,
                                                             request.password):
        raise REFUSED
    if user.state in {"disabled", "deletion_pending", "purging"}:
        raise REFUSED
    # Everything this account already held is retired: authentication is the
    # boundary a session before it must not cross.
    await sessions.revoke_all(user.id)
    issued = await sessions.mint(user, remember_me=request.remember_me, authenticated=True)
    response = Response(status_code=204)
    issue_session(response, issued)
    return response


@router.post("/api/v1/auth/logout", status_code=204)
async def logout(principal: sessions.Resolved = Depends(current_principal)) -> Response:
    await sessions.revoke(principal.session.id)
    response = Response(status_code=204)
    _clear(response)
    return response


@router.get("/api/v1/account")
async def account(principal: sessions.Resolved = Depends(current_principal)) -> dict:
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        rows = (await session.execute(
            select(Session)
            .where(Session.user_id == principal.user.id, Session.revoked_at.is_(None))
            .order_by(Session.created_at)
        )).scalars().all()
    return {
        "id": str(principal.user.id),
        "email": principal.user.email,
        "role": principal.user.role,
        "state": principal.user.state,
        "mail_verified": principal.user.mail_verified,
        "recent_auth": sessions.is_recent(principal.session),
        "sessions": [
            {
                "id": str(row.id),
                "current": row.id == principal.session.id,
                "remember_me": row.remember_me,
                "created_at": row.created_at.isoformat(),
                "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
                "absolute_expires_at": row.absolute_expires_at.isoformat(),
            }
            for row in rows
        ],
    }


@router.delete("/api/v1/account/sessions/{session_id}", status_code=204)
async def revoke_session(
    session_id: uuid.UUID,
    request: Request,
    principal: sessions.Resolved = Depends(current_principal),
) -> Response:
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        row = (await session.execute(
            select(Session).where(Session.id == session_id,
                                  Session.user_id == principal.user.id)
        )).scalar_one_or_none()
    # 404 rather than 403: whether a session id exists is not this caller's
    # business unless it is theirs.
    if row is None:
        raise HTTPException(status_code=404, detail="Not Found")
    await sessions.revoke(row.id)
    response = Response(status_code=204)
    if row.id == principal.session.id:
        _clear(response)
    return response
