"""Upload and serving routes for the local storage backend.

With STORAGE_BACKEND=s3 these routes answer 404: uploads go straight to the
bucket via presigned URLs and never pass through the API.

Keys are minted server side at dispatch time, but a key is not authority: every
part of it is derivable by any worker that was ever dispatched the job, so the
PUT carries the dispatch token as well (issue #247). Uploads are write-once:
the object the API verified must still be that object when a client reads it
(issue #249). The local backend publishes by linking a temporary into place,
so STORAGE_LOCAL_PATH must be on a filesystem that supports hard links.
"""

import asyncio
import logging
import os
import tempfile
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.auth import current_user
from app.storage import (
    UPLOAD_TOKEN_HEADER, LocalStorage, get_storage, validate_download_name,
)
from app.tables import Asset, User

logger = logging.getLogger(__name__)

router = APIRouter()

# Lossless masters are far larger than the WebP this route used to carry, and
# the biggest one the fleet can produce is a 4x upscale of a 1024 px image. A
# real 1024 px generation re-encoded to PNG at 4096 px measures about 19 MB,
# and 4096 px of incompressible detail is 50 MB, so the old 20 MB ceiling
# would have started refusing upscale uploads with a 413 (issue #125).
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


def local_storage() -> LocalStorage:
    storage = get_storage()
    if not isinstance(storage, LocalStorage):
        raise HTTPException(status_code=404, detail="local storage backend not in use")
    return storage


@router.put("/api/v1/files/{key:path}")
async def upload(key: str, request: Request) -> dict:
    from app import jobs

    if not jobs.upload_authorized(key, request.headers.get(UPLOAD_TOKEN_HEADER)):
        raise HTTPException(status_code=403, detail="upload not authorized")

    storage = local_storage()
    try:
        path = storage.path(key)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="upload too large")
        body.extend(chunk)

    def write_file() -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A process death between write and link leaves this file behind; the
        # .upload- prefix marks it as debris, and nothing collects it,
        # deliberately.
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".upload-")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
        except BaseException:
            os.unlink(temporary)
            raise
        return temporary

    temporary = await asyncio.to_thread(write_file)
    try:
        # The first check ran before the body arrived and reading it can take
        # a long time, so a stall could hold this request open while a retry
        # supersedes it; re-check now that the bytes are on disk, and link
        # with no await in between so the authority cannot change under the
        # publication.
        if not jobs.upload_authorized(key, request.headers.get(UPLOAD_TOKEN_HEADER)):
            raise HTTPException(status_code=403, detail="upload not authorized")
        # Publish atomically: writing the key directly would expose a truncated
        # prefix while the body is still landing, and a job_done racing its own
        # PUT could have that prefix inspected and approved. Link refuses to
        # replace an existing key, which also keeps a second PUT from
        # overwriting bytes the API verified. One syscall does not need a
        # thread, and a thread here would reopen the window the re-check just
        # closed.
        os.link(temporary, path)
    except FileExistsError:
        # Either a retry whose first upload actually landed, which is benign,
        # or a replay. Refusing costs the caller nothing either way: a real
        # retry is a new attempt and writes to a new key.
        logger.warning("refused a second upload of %s", key)
        raise HTTPException(status_code=409, detail="already uploaded") from None
    finally:
        # Only the temporary this request created is removed; the
        # destination may belong to a different upload.
        os.unlink(temporary)

    return {"stored": key}


@router.get("/api/v1/files/{key:path}")
async def serve_retired() -> None:
    raise HTTPException(status_code=404, detail="file route retired")


@router.get("/api/v1/assets/{asset_id}")
async def asset(
    asset_id: uuid.UUID,
    download: str | None = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(db.get_session),
):
    row = await session.get(Asset, asset_id)
    if row is None or (row.user_id != user.id and user.role != "admin"):
        raise HTTPException(status_code=404, detail="no such asset")
    try:
        download_name = validate_download_name(download) if download is not None else None
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    storage = get_storage()
    if not isinstance(storage, LocalStorage):
        return RedirectResponse(await storage.url(row.storage_key, download_name))
    try:
        path = storage.path(row.storage_key)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such file")
    if download_name is not None:
        return FileResponse(path, filename=download_name)
    return FileResponse(path)


@router.get("/api/v1/worker-input")
async def worker_input(token: str | None = None, expires: int | None = None) -> FileResponse:
    from app import jobs

    key = jobs.resolve_input_capability(token, expires)
    if key is None:
        raise HTTPException(status_code=403, detail="input not authorized")
    storage = local_storage()
    try:
        path = storage.path(key)
    except ValueError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    if not path.is_file():
        raise HTTPException(status_code=403, detail="input not authorized")
    return FileResponse(path)
