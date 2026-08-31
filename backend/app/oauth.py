"""Signing in with a provider, and linking one to an account that exists.

Registration is invitation only, so a callback can only ever match an identity
somebody deliberately linked. Nothing here finds or creates an account by
address: a provider account that happens to know an address is not that person.

Unknown, expired, spent, mismatched provider, a provider that refused its own
claims and an identity nobody linked all answer alike, so a callback is not an
oracle for which of those it was.
"""

import base64
import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from json import loads
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from starlette.responses import RedirectResponse, Response

from app import db, factors, sessions
from app.accounts import issue_session
from app.auth import CANNOT_SIGN_IN, current_principal, require_accounts_mode
from app.settings import Settings, get_settings
from app.tables import AuthIdentity, OAuthFlow, User

FLOW_TTL = timedelta(minutes=10)
TIMEOUT = httpx.Timeout(10.0)
GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
AUTHORIZE = {
    "google": ("https://accounts.google.com/o/oauth2/v2/auth", "openid email"),
    "github": ("https://github.com/login/oauth/authorize", "read:user user:email"),
}

REFUSED = HTTPException(status_code=403, detail="invalid or expired sign-in attempt")

def check_configuration(settings: Settings) -> None:
    """Refuse a provider sign-in this install cannot carry safely.

    The authorization code comes back on the redirect URI, and over plain HTTP
    anyone on the path reads it and can spend it before the browser does. Mail
    refuses the same configuration for the same reason.
    """
    offered = [method for method in settings.auth_methods if method != "password"]
    if not offered:
        return
    parsed = urlsplit(settings.public_url)
    try:
        _port = parsed.port
    except ValueError:
        parsed = urlsplit("")
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.hostname and (parsed.scheme == "https"
                            or (parsed.scheme == "http" and loopback)):
        return
    raise RuntimeError(
        "OAuth needs an https PUBLIC_URL, because the authorization code comes "
        f"back on it; {settings.public_url} is not one"
    )


router = APIRouter(dependencies=[Depends(require_accounts_mode)])


class ProviderRefused(Exception):
    """The provider's answer was not acceptable."""


@dataclass
class ProviderIdentity:
    subject: str
    email: str


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _state_hash(state: str) -> bytes:
    return hashlib.sha256(state.encode()).digest()


def _normalized(email: str) -> str:
    return email.strip().lower()


def _redirect_uri(settings: Settings, provider: str) -> str:
    return f"{settings.public_url.rstrip('/')}/api/v1/auth/callback/{provider}"


async def _start(provider: str, link_user_id: uuid.UUID | None) -> tuple[str, str]:
    """Mints the flow and returns the provider URL to send the browser to.

    Only the hash of the state travels back into the database, and the verifier
    and the nonce never leave it, so a callback has to match a redirect this
    server started.
    """
    settings = get_settings()
    if provider not in AUTHORIZE or provider not in settings.auth_methods:
        raise HTTPException(status_code=404, detail="Not Found")
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    nonce = secrets.token_urlsafe(16)
    async with db.session_factory() as session:
        session.add(OAuthFlow(
            state_hash=_state_hash(state),
            provider=provider,
            verifier=verifier,
            nonce=nonce,
            link_user_id=link_user_id,
            expires_at=datetime.now(timezone.utc) + FLOW_TTL,
        ))
        await session.commit()
    endpoint, scope = AUTHORIZE[provider]
    query = urlencode({
        "response_type": "code",
        "client_id": getattr(settings, f"{provider}_client_id"),
        "redirect_uri": _redirect_uri(settings, provider),
        "scope": scope,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge_for(verifier),
        "code_challenge_method": "S256",
    })
    return f"{endpoint}?{query}", state


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as unreadable:
        raise ProviderRefused("the provider's answer was not JSON") from unreadable


def _claims(id_token: str) -> dict:
    """Reads the payload without checking the signature.

    The token came straight from Google's token endpoint over TLS, in a call
    this server made with its own client secret, so the transport already
    proves where it came from and there is no second party to authenticate.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ProviderRefused("the id token is not a JWT")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = loads(base64.urlsafe_b64decode(padded))
    except ValueError as unreadable:
        raise ProviderRefused("the id token payload is unreadable") from unreadable
    if not isinstance(payload, dict):
        raise ProviderRefused("the id token payload is unreadable")
    return payload


async def _google(code: str, verifier: str, nonce: str, settings: Settings) -> ProviderIdentity:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        granted = await client.post("https://oauth2.googleapis.com/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": _redirect_uri(settings, "google"),
        })
    id_token = _json(granted).get("id_token")
    if not isinstance(id_token, str):
        raise ProviderRefused("the token endpoint returned no id token")
    claims = _claims(id_token)
    expires = claims.get("exp")
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise ProviderRefused("the id token was not issued by Google")
    if claims.get("aud") != settings.google_client_id:
        raise ProviderRefused("the id token was issued for another client")
    if not isinstance(expires, (int, float)) or expires <= time.time():
        raise ProviderRefused("the id token has expired")
    if claims.get("nonce") != nonce:
        raise ProviderRefused("the nonce does not match this flow")
    if not claims.get("email_verified"):
        raise ProviderRefused("the provider has not verified this address")
    subject, email = claims.get("sub"), claims.get("email")
    if not subject or not email:
        raise ProviderRefused("the id token names no subject or address")
    return ProviderIdentity(subject=str(subject), email=str(email))


async def _github(code: str, verifier: str, settings: Settings) -> ProviderIdentity:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        granted = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "code": code,
                "code_verifier": verifier,
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "redirect_uri": _redirect_uri(settings, "github"),
            },
        )
        token = _json(granted).get("access_token")
        if not isinstance(token, str):
            raise ProviderRefused("the token endpoint returned no access token")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        account = _json(await client.get("https://api.github.com/user", headers=headers))
        addresses = _json(await client.get("https://api.github.com/user/emails",
                                           headers=headers))
    subject = account.get("id") if isinstance(account, dict) else None
    if not isinstance(subject, int):
        raise ProviderRefused("the provider named no account")
    if not isinstance(addresses, list):
        raise ProviderRefused("the provider listed no addresses")
    primary = next((entry.get("email") for entry in addresses
                    if isinstance(entry, dict) and entry.get("primary") and entry.get("verified")),
                   None)
    if not primary:
        raise ProviderRefused("the provider has not verified a primary address")
    return ProviderIdentity(subject=str(subject), email=str(primary))


async def exchange(provider: str, code: str, verifier: str, nonce: str,
                   settings: Settings) -> ProviderIdentity:
    """Trades the code for the provider's claims about one account.

    The provider's token is spent here and discarded when this returns: nothing
    in this install acts on the provider's behalf afterwards.
    """
    if provider == "google":
        return await _google(code, verifier, nonce, settings)
    return await _github(code, verifier, settings)


FLOW_COOKIE = "potocolom_oauth"


def _flow_cookie_name(settings: Settings) -> str:
    return f"__Host-{FLOW_COOKIE}" if sessions.is_secure(settings.public_url) else FLOW_COOKIE


def _bind_to_browser(response: Response, state: str, settings: Settings) -> None:
    """The state alone proves this server started a flow, not that this
    browser did.

    Without that second half, a callback captured from one browser can be
    completed in another: a sign-in flow plants the starter's session in
    somebody else's browser, and a link flow attaches somebody else's provider
    account to the starter's, permanently, because one provider account links
    to one account.
    """
    response.set_cookie(
        _flow_cookie_name(settings), state, path="/", samesite="lax",
        secure=sessions.is_secure(settings.public_url), httponly=True,
        max_age=int(FLOW_TTL.total_seconds()),
    )


def _started_here(request: Request, state: str, settings: Settings) -> bool:
    presented = request.cookies.get(_flow_cookie_name(settings), "")
    return bool(presented) and secrets.compare_digest(presented, state)


@router.get("/api/v1/auth/redirect/{provider}")
async def redirect(provider: str) -> Response:
    target, state = await _start(provider, None)
    response = RedirectResponse(target, status_code=307)
    _bind_to_browser(response, state, get_settings())
    return response


@router.get("/api/v1/auth/callback/{provider}")
async def callback(provider: str, state: str, code: str, request: Request) -> Response:
    settings = get_settings()
    if not _started_here(request, state, settings):
        raise REFUSED
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        flow = (await session.execute(
            update(OAuthFlow)
            .where(
                OAuthFlow.state_hash == _state_hash(state),
                OAuthFlow.provider == provider,
                OAuthFlow.consumed_at.is_(None),
                OAuthFlow.expires_at > func.now(),
            )
            .values(consumed_at=func.now())
            .returning(OAuthFlow.verifier, OAuthFlow.nonce, OAuthFlow.link_user_id)
        )).first()
        await session.commit()
    if flow is None:
        raise REFUSED
    try:
        identity = await exchange(provider, code, flow.verifier, flow.nonce, settings)
    except ProviderRefused as refused:
        raise REFUSED from refused
    if flow.link_user_id is None:
        response = await _sign_in(provider, identity, settings, request)
    else:
        response = await _link(provider, identity, flow.link_user_id, settings)
    response.delete_cookie(_flow_cookie_name(settings), path="/",
                           samesite="lax", secure=sessions.is_secure(settings.public_url),
                           httponly=True)
    return response


async def _sign_in(provider: str, identity: ProviderIdentity,
                   settings: Settings, request: Request) -> Response:
    """Matches a linked identity only. There is no lookup by address here."""
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        user = (await session.execute(
            select(User)
            .join(AuthIdentity, AuthIdentity.user_id == User.id)
            .where(AuthIdentity.provider == provider,
                   AuthIdentity.subject == identity.subject)
        )).scalar_one_or_none()
    if user is None or user.state in CANNOT_SIGN_IN:
        raise REFUSED
    # Parity with the password route: a token planted in this browser before
    # authentication must not be the token that comes out of it.
    presented = request.cookies.get(sessions.cookie_names(settings.public_url)[0])
    if presented:
        resolved = await sessions.resolve(presented)
        if resolved is not None:
            await sessions.revoke(resolved.session.id)
    # The same gate the password login passes, and read in the transaction that
    # mints for the same reason: a factor enrolled while this callback was in
    # flight revokes the account's sessions and cannot reach one that is not
    # there yet (issue #435).
    issued = await factors.mint_behind_the_gate(user, remember_me=False, expected=None)
    if issued is None:
        # Every primary login passes the same gate. A provider proving who
        # somebody is does not answer for the factor they enrolled.
        #
        # The browser arrived here by navigation, not by fetch, so it is
        # sent back to a page that can ask for the code. Answering with
        # JSON would leave the person looking at raw JSON with nowhere to
        # type it.
        return await factors.begin_challenge(
            user, remember_me=False,
            redirect_to=f"{settings.public_url.rstrip('/')}/?totp=required")
    response = RedirectResponse(settings.public_url, status_code=307)
    issue_session(response, issued)
    return response


async def _link(provider: str, identity: ProviderIdentity, user_id: uuid.UUID,
                settings: Settings) -> Response:
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            user = await session.get(User, user_id)
            if user is None:
                raise REFUSED
            session.add(AuthIdentity(user_id=user.id, provider=provider,
                                     subject=identity.subject, password_hash=None))
            try:
                await session.flush()
            except IntegrityError as taken:
                raise HTTPException(
                    status_code=409,
                    detail="that provider account is already linked to an account",
                ) from taken
            # Decided by the write, not by a read before it: a primary
            # address change committing in between would otherwise mark the
            # new, unproved address as proved, and assurance is what a
            # promotion reads.
            await session.execute(
                update(User)
                .where(User.id == user.id,
                       func.lower(func.btrim(User.email)) == _normalized(identity.email))
                .values(mail_verified=True)
            )
    return RedirectResponse(settings.public_url, status_code=307)


@router.post("/api/v1/account/identities/{provider}")
async def start_link(
    provider: str,
    response: Response,
    principal: sessions.Resolved = Depends(current_principal),
) -> dict:
    if not sessions.is_recent(principal.session):
        raise HTTPException(status_code=403, detail="recent authentication required")
    if principal.user.state != "active":
        # Suspended reads and settles its account, and changes nothing. A new
        # way to sign in is a change.
        raise HTTPException(status_code=403, detail="account suspended")
    target, state = await _start(provider, principal.user.id)
    _bind_to_browser(response, state, get_settings())
    return {"redirect": target}


async def prune() -> None:
    """Reclaim flows nobody can use any more.

    The redirect that creates them takes no credential, so these rows are the
    one thing here a stranger can pile up. A spent flow is finished and an
    expired one can never be spent, so neither is worth keeping.
    """
    if db.session_factory is None:
        return
    async with db.session_factory() as session:
        await session.execute(delete(OAuthFlow).where(OAuthFlow.expires_at < func.now()))
        await session.commit()
