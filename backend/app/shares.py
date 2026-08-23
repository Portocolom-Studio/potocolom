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
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app import db, keyring
from app.auth import current_user
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


def picture_signature(asset_id: uuid.UUID, expires: int) -> str:
    """Signed, not stored: any instance behind the load balancer can check it,
    and an expired address leaves nothing to clean up."""
    key = keyring.get_key_ring().derive(PICTURE_PURPOSE)
    return hmac.new(key, f"{asset_id}:{expires}".encode(), hashlib.sha256).hexdigest()


def picture_url(asset_id: uuid.UUID) -> str:
    expires = int(time.time()) + PICTURE_TTL
    public = get_settings().public_url.rstrip("/")
    return (f"{public}/api/v1/shared-picture?asset={asset_id}&expires={expires}"
            f"&signature={picture_signature(asset_id, expires)}")


def picture_authorized(asset_id: uuid.UUID, expires: int, signature: str) -> bool:
    if expires <= int(time.time()) or not signature.isascii():
        return False
    return hmac.compare_digest(signature, picture_signature(asset_id, expires))


class ShareRequest(BaseModel):
    asset_id: uuid.UUID
    days: Literal[1, 7, 30]


class ResolveRequest(BaseModel):
    token: str


@router.post("/api/v1/shares", status_code=201)
async def share(
    request: ShareRequest,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(db.get_session),
) -> dict:
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
    await session.commit()
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
    now = datetime.now(timezone.utc)
    found = (await session.execute(
        select(Asset, Job)
        .join(AssetShare, AssetShare.asset_id == Asset.id)
        .outerjoin(Job, Job.id == Asset.job_id)
        .where(AssetShare.token_hash == token_hash(request.token),
               AssetShare.revoked_at.is_(None),
               AssetShare.expires_at > now)
    )).first()
    if found is None:
        raise NO_SUCH_SHARE
    asset, job = found
    if asset.expires_at is not None and asset.expires_at <= now:
        raise NO_SUCH_SHARE
    return {
        "asset": {"id": str(asset.id), "width": asset.width, "height": asset.height,
                  "mime": asset.mime},
        "prompt": (job.params or {}).get("prompt") if job is not None else None,
        "model": job.model_id if job is not None else None,
        "url": picture_url(asset.id),
    }
