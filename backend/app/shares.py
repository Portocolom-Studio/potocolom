"""Links that show one picture to whoever holds them.

The token travels in the URL fragment, which browsers never send to a server,
and comes back in a POST body. A token in a path or a query would sit in every
access log, proxy trace, and Referer header between the viewer and here, so
there is no GET that takes one (docs/decisions.md).

One active share per asset. Sharing again mints a new link and revokes the old
one in the same transaction, so revoking the link somebody can see can never
leave an older one alive behind it.
"""

import hashlib
import hmac
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app import db, keyring
from app.auth import current_user, require_role
from app.settings import get_settings
from app.tables import Asset, AssetShare, Job, User

router = APIRouter()

# Long enough to show the picture and short enough that a copied address is
# useless by the time it is pasted anywhere. The share itself is the durable
# thing; this is only the address of the bytes.
PICTURE_TTL = 60
PICTURE_PURPOSE = "asset-share-picture"

NO_SUCH_SHARE = HTTPException(status_code=404, detail="no such share")


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


# Used only to sign an address that lives for a minute, never to protect
# anything at rest, so an install with no key ring can hold this in memory.
_PROCESS_PICTURE_KEY = secrets.token_bytes(32)


def _picture_key() -> bytes:
    """The key ring where there is one, and a key that dies with this process
    where there is not.

    Sharing works in the default none mode, which has no ROOT_KEYS at all, and
    that mode is one process by definition. A restart there invalidates the
    addresses already handed out, which is a link that expired a little early.
    """
    try:
        return keyring.get_key_ring().derive(PICTURE_PURPOSE)
    except keyring.KeyRingError:
        return _PROCESS_PICTURE_KEY


def picture_signature(share_id: uuid.UUID, expires: int) -> str:
    """Signed, not stored: any instance behind the load balancer can check it,
    and an expired address leaves nothing to clean up."""
    return hmac.new(_picture_key(), f"{share_id}:{expires}".encode(),
                    hashlib.sha256).hexdigest()


def picture_url(share_id: uuid.UUID) -> str:
    expires = int(time.time()) + PICTURE_TTL
    public = get_settings().public_url.rstrip("/")
    return (f"{public}/api/v1/shared-picture?share={share_id}&expires={expires}"
            f"&signature={picture_signature(share_id, expires)}")


async def pictured_asset(
    session: AsyncSession, share_id: uuid.UUID, expires: int, signature: str,
) -> Asset | None:
    """The asset this address stands for, if the share behind it is still live.

    The signature names the share rather than the asset, so revoking a share
    stops the addresses already handed out as well as the next resolve. The
    minute of life is a bound on a leaked address, not on revocation.
    """
    if expires <= int(time.time()) or not signature.isascii():
        return None
    if not hmac.compare_digest(signature, picture_signature(share_id, expires)):
        return None
    return await _live_asset(session, AssetShare.id == share_id)


def _live_share(*conditions):
    """One place decides what a live share is: not revoked, not run out, on an
    asset that has not expired under it, and belonging to an account that is
    still speaking.

    A share is the account addressing the public, so a suspended account has
    no live shares. Paused rather than revoked: restoring the account restores
    the links it handed out, which a revocation could not undo.
    """
    now = datetime.now(timezone.utc)
    return (
        select(Asset)
        .join(AssetShare, AssetShare.asset_id == Asset.id)
        .join(User, User.id == Asset.user_id)
        .where(*conditions,
               AssetShare.revoked_at.is_(None),
               AssetShare.expires_at > now,
               (Asset.expires_at.is_(None)) | (Asset.expires_at > now),
               User.state == "active")
    )


async def _live_asset(session: AsyncSession, *conditions) -> Asset | None:
    return (await session.execute(_live_share(*conditions))).scalars().first()


class ShareRequest(BaseModel):
    asset_id: uuid.UUID
    days: Literal[1, 7, 30]


class ResolveRequest(BaseModel):
    token: str


@router.post("/api/v1/shares", status_code=201)
async def share(
    request: ShareRequest,
    user: User = Depends(require_role("member")),
    session: AsyncSession = Depends(db.get_session),
) -> dict:
    """Minting a public link is a mutation, so it belongs to the user tier.

    Revoking one stays open to a viewer: an account demoted while a link was
    live must still be able to take it down.
    """
    asset = await session.get(Asset, request.asset_id)
    if asset is None or asset.user_id != user.id:
        raise HTTPException(status_code=404, detail="no such asset")
    await session.execute(
        update(AssetShare)
        .where(AssetShare.asset_id == asset.id, AssetShare.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    token = secrets.token_urlsafe(32)
    row = AssetShare(
        id=uuid.uuid4(),
        asset_id=asset.id,
        token_hash=token_hash(token),
        expires_at=datetime.now(timezone.utc) + timedelta(days=request.days),
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as raced:
        # Two requests can both revoke what they read and insert their own,
        # and the partial unique index decides between them. The one that
        # loses is a conflict, not a fault of this install.
        raise HTTPException(status_code=409, detail="that asset was shared again") from raced
    return {"id": str(row.id), "url": f"{get_settings().public_url.rstrip('/')}/shared#{token}"}


@router.delete("/api/v1/shares/{share_id}", status_code=204)
async def revoke(
    share_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(db.get_session),
) -> Response:
    revoked = (await session.execute(
        update(AssetShare)
        .where(AssetShare.id == share_id, AssetShare.revoked_at.is_(None),
               AssetShare.asset_id.in_(select(Asset.id).where(Asset.user_id == user.id)))
        .values(revoked_at=func.now())
        .returning(AssetShare.id)
    )).first()
    if revoked is None:
        raise NO_SUCH_SHARE
    await session.commit()
    return Response(status_code=204)


@router.post("/api/v1/shared")
async def resolve(
    request: ResolveRequest,
    session: AsyncSession = Depends(db.get_session),
) -> dict:
    """The link is the credential, so this route takes no other one.

    Every refusal is the same 404: whether a token was never minted, was
    revoked, or ran out is not something a holder of the wrong token gets to
    learn.
    """
    found = (await session.execute(
        _live_share(AssetShare.token_hash == token_hash(request.token))
        .add_columns(AssetShare.id, Job.model_id, Job.params)
        .outerjoin(Job, Job.id == Asset.job_id)
    )).first()
    if found is None:
        raise NO_SUCH_SHARE
    asset, share_id, model_id, params = found
    return {
        "asset": {"id": str(asset.id), "width": asset.width, "height": asset.height,
                  "mime": asset.mime},
        "prompt": (params or {}).get("prompt"),
        "model": model_id,
        "url": picture_url(share_id),
    }
