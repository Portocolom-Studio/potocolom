"""The storage seam, docs/blueprint.md: local filesystem or S3 compatible.

Workers never talk to a Storage implementation directly. They receive an
UploadTarget in dispatch_job and PUT the result bytes to it: a presigned S3
URL in the cloud, an internal API route (app/files.py) when local.
"""

import asyncio
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.settings import Settings, get_settings

SIGNED_URL_TTL = 3600


@dataclass
class UploadTarget:
    url: str
    headers: dict[str, str] = field(default_factory=dict)


class Storage(Protocol):
    async def upload_target(self, key: str) -> UploadTarget: ...

    async def url(self, key: str) -> str: ...

    async def delete(self, key: str) -> None: ...


class LocalStorage:
    """Files under STORAGE_LOCAL_PATH, uploaded and served through the API."""

    def __init__(self, root: str, public_url: str):
        self.root = Path(root)
        self.public_url = public_url.rstrip("/")

    def path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("storage key escapes the storage root")
        return path

    async def upload_target(self, key: str) -> UploadTarget:
        return UploadTarget(url=f"{self.public_url}/api/v1/files/{key}")

    async def url(self, key: str) -> str:
        return f"{self.public_url}/api/v1/files/{key}"

    async def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)


class S3Storage:
    """S3 compatible bucket with presigned PUT and GET URLs (MinIO in development)."""

    def __init__(self, settings: Settings):
        import boto3
        from botocore.config import Config

        self.bucket = settings.storage_s3_bucket
        self.client = boto3.client(
            "s3",
            region_name=settings.storage_s3_region,
            endpoint_url=settings.storage_s3_endpoint or None,
            aws_access_key_id=settings.storage_s3_access_key or None,
            aws_secret_access_key=settings.storage_s3_secret_key or None,
            config=Config(signature_version="s3v4"),  # MinIO requires SigV4
        )

    async def upload_target(self, key: str) -> UploadTarget:
        # Presigning is local computation, no network round trip.
        url = self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=SIGNED_URL_TTL,
        )
        return UploadTarget(url=url)

    async def url(self, key: str) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=SIGNED_URL_TTL,
        )

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)


@lru_cache
def get_storage() -> Storage:
    settings = get_settings()
    if settings.storage_backend == "s3":
        return S3Storage(settings)
    return LocalStorage(settings.storage_local_path, settings.public_url)
