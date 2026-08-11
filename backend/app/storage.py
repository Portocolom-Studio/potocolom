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


# Both bounds are fixed here rather than derived from the object, because the
# object is what a worker chose to upload: a ceiling computed from a declared
# width and height is a ceiling the peer sets. 4096 is the real maximum, a
# factor-4 upscale of the largest shipped generation size.
MAX_IMAGE_EDGE = 4096
# Enough for any real encoder, and it bounds the walk below.
MAX_PNG_CHUNKS = 4096

# Bit depths each PNG colour type allows (PNG spec, table 11.1).
_PNG_DEPTHS = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Dimensions of a stored PNG, or ValueError if it is not a usable one.

    Walks the chunk structure and checks every CRC, but never decompresses.
    An earlier version did, to prove the image decoded, and that was twice a
    denial of service: unbounded it turned 400 KB into 831 MB, and bounded by
    the declared dimensions it granted an 80 GB ceiling to a 65 KB object
    claiming 100000x100000. Deciding how much memory to spend from a number the
    peer supplied cannot be made safe, and a real decoder belongs off this path
    entirely, so the guarantee here is that the bytes are a structurally
    complete PNG of a plausible size, not that the pixels decode.
    """
    if len(data) < 45 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("stored object is not a PNG")
    position = 8
    width = height = 0
    chunks = 0
    saw_idat = False
    while position + 12 <= len(data):
        chunks += 1
        if chunks > MAX_PNG_CHUNKS:
            raise ValueError("stored PNG has too many chunks")
        length = struct.unpack(">I", data[position:position + 4])[0]
        end = position + 12 + length
        if end > len(data):
            raise ValueError("stored object has a truncated PNG chunk")
        kind = data[position + 4:position + 8]
        body = data[position + 8:position + 8 + length]
        if zlib.crc32(kind + body) & 0xffffffff != struct.unpack(">I", data[end - 4:end])[0]:
            raise ValueError("stored object has a corrupt PNG chunk")
        if chunks == 1:
            if kind != b"IHDR" or length != 13:
                raise ValueError("stored object does not start with a PNG header")
            width, height = struct.unpack(">II", body[:8])
            depth, colour, compression, filtering, interlace = body[8:13]
            if colour not in _PNG_DEPTHS or depth not in _PNG_DEPTHS[colour]:
                raise ValueError("stored PNG has an illegal colour type or bit depth")
            if compression != 0 or filtering != 0 or interlace not in (0, 1):
                raise ValueError("stored PNG declares an unknown encoding")
        elif kind == b"IHDR":
            raise ValueError("stored PNG repeats its header")
        elif kind == b"IDAT":
            saw_idat = True
        elif kind == b"IEND":
            if length != 0 or end != len(data):
                raise ValueError("stored PNG does not end at its IEND")
            break
        position = end
    else:
        raise ValueError("stored object is not a complete PNG")
    if not saw_idat:
        raise ValueError("stored PNG carries no image data")
    if width == 0 or height == 0:
        raise ValueError("stored PNG has empty dimensions")
    if width > MAX_IMAGE_EDGE or height > MAX_IMAGE_EDGE:
        raise ValueError("stored PNG dimensions are implausible")
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
        def inspect() -> ImageInfo | None:
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

        # Off the loop: the read is up to MAX_VERIFY_BYTES of peer-supplied
        # bytes and every millisecond of it would otherwise stall every other
        # request and socket on this process.
        return await asyncio.to_thread(inspect)

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
            response, data = await asyncio.to_thread(read_object)
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
