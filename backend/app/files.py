"""Upload and serving routes for the local storage backend.

With STORAGE_BACKEND=s3 these routes answer 404: uploads go straight to the
bucket via presigned URLs and never pass through the API.

Keys are minted server side at dispatch time and contain a job UUID, so an
upload URL is unguessable; fleet authentication tightens this further when it
lands (docs/blueprint.md, FLEET_TOKEN_KEY).
"""

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.storage import LocalStorage, get_storage

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

    if not jobs.storage_key_in_flight(key):
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

    def write_file() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    try:
        await asyncio.to_thread(write_file)
    except Exception:
        path.unlink(missing_ok=True)
        raise

    return {"stored": key}


@router.get("/api/v1/files/{key:path}")
async def serve(key: str) -> FileResponse:
    storage = local_storage()
    try:
        path = storage.path(key)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such file")
    return FileResponse(path)
