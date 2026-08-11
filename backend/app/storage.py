"""The storage seam, docs/blueprint.md: local filesystem or S3 compatible.

Workers never talk to a Storage implementation directly. They receive an
UploadTarget in dispatch_job and PUT the result bytes to it: a presigned S3
URL in the cloud, an internal API route (app/files.py) when local.
"""

import asyncio
import re
import struct
import zlib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

from app.settings import Settings, get_settings

SIGNED_URL_TTL = 3600
PNG_CONTENT_TYPE = "image/png"
WEBP_CONTENT_TYPE = "image/webp"
DOWNLOAD_NAME_MAX_LENGTH = 96
DOWNLOAD_NAME_PATTERN = re.compile(
    r"[a-z0-9]+(?:-[a-z0-9]+)*\.[a-z0-9]+",
)


def validate_download_name(name: str) -> str:
    if len(name) > DOWNLOAD_NAME_MAX_LENGTH or DOWNLOAD_NAME_PATTERN.fullmatch(name) is None:
        raise ValueError("unsafe download filename")
    return name


def download_content_disposition(name: str) -> str:
    return f'attachment; filename="{validate_download_name(name)}"'


@dataclass
class UploadTarget:
    url: str
    headers: dict[str, str] = field(default_factory=dict)


# Outputs are one generated image; anything larger is not worth reading into
# the API process to inspect. The local upload route already caps at 20 MB,
# but a presigned S3 PUT carries no size condition.
MAX_VERIFY_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ImageInfo:
    width: int
    height: int
    size: int
    content_type: str


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("stored object is not a PNG")
    position = 8
    width = height = None
    idat = bytearray()
    saw_iend = False
    while position + 12 <= len(data):
        length = struct.unpack(">I", data[position:position + 4])[0]
        end = position + 12 + length
        if end > len(data):
            raise ValueError("stored object has a truncated PNG chunk")
        kind = data[position + 4:position + 8]
        body = data[position + 8:position + 8 + length]
        checksum = struct.unpack(">I", data[position + 8 + length:end])[0]
        if zlib.crc32(kind + body) & 0xffffffff != checksum:
            raise ValueError("stored object has a corrupt PNG chunk")
        if kind == b"IHDR":
            if position != 8 or length != 13:
                raise ValueError("stored object has no PNG dimensions")
            width, height = struct.unpack(">II", body[:8])
        elif kind == b"IDAT":
            idat.extend(body)
        elif kind == b"IEND":
            saw_iend = length == 0
            break
        position = end
    if width is None or height is None or width == 0 or height == 0:
        raise ValueError("stored PNG has empty dimensions")
    if width >= 2**31 or height >= 2**31:
        raise ValueError("stored PNG dimensions exceed the database limit")
    if not saw_iend or not idat:
        raise ValueError("stored object is not a complete PNG")
    # Decompress against a ceiling derived from the header, not to completion.
    # A few hundred KB of IDAT expands to hundreds of MB, and this runs on the
    # API event loop for output a worker chose, so an unbounded decompress here
    # is a denial of service with a small upload behind it. A row cannot exceed
    # one filter byte plus four channels of 16 bits per pixel.
    limit = height * (1 + width * 8) + 1
    stream = zlib.decompressobj()
    try:
        produced = len(stream.decompress(bytes(idat), limit))
    except zlib.error as error:
        raise ValueError("stored PNG data is not decodable") from error
    if produced >= limit:
        raise ValueError("stored PNG expands past its declared dimensions")
    return width, height


class Storage(Protocol):
    async def upload_target(self, key: str) -> UploadTarget: ...

    async def image_info(self, key: str) -> ImageInfo | None: ...

    async def url(self, key: str, download_name: str | None = None) -> str: ...

    async def worker_fetch_url(self, key: str) -> str: ...

    async def delete(self, key: str) -> None: ...


class LocalStorage:
    """Files under STORAGE_LOCAL_PATH, uploaded and served through the API."""

    def __init__(self, root: str, public_url: str, worker_url: str):
        self.root = Path(root)
        self.public_url = public_url.rstrip("/")
        self.worker_url = worker_url.rstrip("/")

    def path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("storage key escapes the storage root")
        return path

    async def upload_target(self, key: str) -> UploadTarget:
        content_type = PNG_CONTENT_TYPE if key.endswith(".png") else WEBP_CONTENT_TYPE
        return UploadTarget(
            url=f"{self.worker_url}/api/v1/files/{key}",
            headers={"Content-Type": content_type},
        )

    async def image_info(self, key: str) -> ImageInfo | None:
        path = self.path(key)
        try:
            if path.stat().st_size > MAX_VERIFY_BYTES:
                return None
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            width, height = _png_dimensions(data)
        except ValueError:
            return None
        return ImageInfo(width=width, height=height, size=len(data),
                         content_type=PNG_CONTENT_TYPE)

    async def url(self, key: str, download_name: str | None = None) -> str:
        url = f"{self.public_url}/api/v1/files/{key}"
        if download_name is None:
            return url
        return f"{url}?{urlencode({'download': validate_download_name(download_name)})}"

    async def worker_fetch_url(self, key: str) -> str:
        return f"{self.worker_url}/api/v1/files/{key}"

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
        content_type = PNG_CONTENT_TYPE if key.endswith(".png") else WEBP_CONTENT_TYPE
        url = self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=SIGNED_URL_TTL,
        )
        return UploadTarget(url=url, headers={"Content-Type": content_type})

    async def image_info(self, key: str) -> ImageInfo | None:
        from botocore.exceptions import ClientError

        def read_object() -> tuple[dict, bytes]:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            try:
                # amt bounds what a worker can make the API buffer: the
                # presigned PUT constrains bucket, key and content type only.
                return response, body.read(MAX_VERIFY_BYTES + 1)
            finally:
                body.close()

        try:
            response, data = read_object()
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        if len(data) > MAX_VERIFY_BYTES:
            return None
        try:
            width, height = _png_dimensions(data)
        except ValueError:
            return None
        return ImageInfo(
            width=width,
            height=height,
            size=response.get("ContentLength", len(data)),
            content_type=response.get("ContentType", PNG_CONTENT_TYPE),
        )

    async def url(self, key: str, download_name: str | None = None) -> str:
        params = {"Bucket": self.bucket, "Key": key}
        if download_name is not None:
            params["ResponseContentDisposition"] = download_content_disposition(download_name)
        return self.client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=SIGNED_URL_TTL,
        )

    async def worker_fetch_url(self, key: str) -> str:
        return await self.url(key)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)


@lru_cache
def get_storage() -> Storage:
    settings = get_settings()
    if settings.storage_backend == "s3":
        return S3Storage(settings)
    return LocalStorage(settings.storage_local_path, settings.public_url, settings.worker_url)
