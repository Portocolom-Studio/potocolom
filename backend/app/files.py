"""Upload and serving routes for the local storage backend.

With STORAGE_BACKEND=s3 these routes answer 404: uploads go straight to the
bucket via presigned URLs and never pass through the API.

Keys are minted server side at dispatch time and contain a job UUID, so an
upload URL is unguessable; fleet authentication tightens this further when it
lands (docs/blueprint.md, FLEET_TOKEN_KEY).
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.storage import LocalStorage, get_storage

router = APIRouter()


def local_storage() -> LocalStorage:
    storage = get_storage()
    if not isinstance(storage, LocalStorage):
        raise HTTPException(status_code=404, detail="local storage backend not in use")
    return storage


@router.put("/api/v1/files/{key:path}")
async def upload(key: str, request: Request) -> dict:
    storage = local_storage()
    try:
        path = storage.path(key)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(await request.body())
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
