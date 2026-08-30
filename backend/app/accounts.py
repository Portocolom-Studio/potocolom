"""Signing in, signing out, and what an account can see about itself."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from anyio import to_thread
from sqlalchemy import func, or_, select
from starlette.responses import Response

from app import db, factors, rate_limit, sessions
from app.auth import current_principal, require_accounts_mode
from app.passwords import ABSENT_ACCOUNT_HASH, verify_password
from app.settings import get_settings
from app.tables import AuthIdentity, Session, User

router = APIRouter(dependencies=[Depends(require_accounts_mode)])

# One answer for an address nobody holds and a password that does not match,
# so the response cannot be used to learn which accounts exist.
REFUSED = HTTPException(status_code=401, detail="invalid email or password")


async def _presented_session(http: Request) -> sessions.Resolved | None:
    session_name, _ = sessions.cookie_names(get_settings().public_url)
    token = http.cookies.get(session_name)
    return await sessions.resolve(token) if token else None


class LoginRequest(BaseModel):
    email: str
    password: str
    remember_me: bool = False


def issue_session(response: Response, issued: sessions.Issued) -> None:
    public_url = get_settings().public_url
    session_name, csrf_name = sessions.cookie_names(public_url)
    secure = sessions.is_secure(public_url)
    # Host-only on purpose: no domain, so a sibling host cannot be handed it.
    response.set_cookie(session_name, issued.token, path="/", samesite="lax",
                        secure=secure, httponly=True, max_age=issued.max_age)
    # The browser has to read this one to echo it back on unsafe requests.
    response.set_cookie(csrf_name, issued.csrf, path="/", samesite="lax",
                        secure=secure, httponly=False, max_age=issued.max_age)


def _clear(response: Response) -> None:
    public_url = get_settings().public_url
    session_name, csrf_name = sessions.cookie_names(public_url)
    # Secure matters on the way out too: a __Host- cookie cleared without it
    # breaks the prefix rules, so the browser drops the whole Set-Cookie and
    # the credential the user asked to remove stays on disk.
    secure = sessions.is_secure(public_url)
    response.delete_cookie(session_name, path="/", samesite="lax", secure=secure,
                           httponly=True)
    response.delete_cookie(csrf_name, path="/", samesite="lax", secure=secure)


@router.post("/api/v1/auth/login", status_code=204)
async def login(request: LoginRequest, http: Request) -> Response:
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    subject = request.email.strip().lower()
    await rate_limit.charge_login(subject, http)
    presented = await _presented_session(http)
    async with db.session_factory() as session:
        found = (await session.execute(
            select(AuthIdentity, User)
            .join(User, User.id == AuthIdentity.user_id)
            .where(AuthIdentity.provider == "password", AuthIdentity.subject == subject)
        )).first()
    stored = ABSENT_ACCOUNT_HASH if found is None else (found[0].password_hash or "")
    matched = await to_thread.run_sync(verify_password, stored, request.password)
    if found is None or not matched:
        raise REFUSED
    _, user = found
    if user.state in {"disabled", "deletion_pending", "purging"}:
        raise REFUSED
    # Session fixation: a token planted in this browser before authentication
    # must not be the token that comes out of it. Only the session presented
    # here is retired, so signing in on one device does not sign out another.
    if presented is not None:
        await sessions.revoke(presented.session.id)
    async with db.session_factory() as session:
        gated = await factors.enrolled_factor(session, user.id)
    if gated is not None:
        # The password was right, and that is not enough for this account. What
        # comes back is a capability to answer a challenge, never a session.
        return await factors.begin_challenge(user, request.remember_me)
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
            .where(Session.user_id == principal.user.id, Session.revoked_at.is_(None),
                   Session.absolute_expires_at > func.now(),
                   or_(Session.idle_expires_at.is_(None), Session.idle_expires_at > func.now()))
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
