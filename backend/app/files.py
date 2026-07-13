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

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


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
