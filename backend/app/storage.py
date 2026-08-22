"""The storage seam, docs/blueprint.md: local filesystem or S3 compatible.

Workers never talk to a Storage implementation directly. They receive an
UploadTarget in dispatch_job and PUT the result bytes to it: a presigned S3
URL in the cloud, an internal API route (app/files.py) when local.
"""

import asyncio
import os
import re
import secrets
import struct
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache, partial
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

from app.settings import Settings, get_settings

SIGNED_URL_TTL = 300
WORKER_URL_TTL = 900
OUTPUT_UPLOAD_URL_TTL = 3600
# Deletes get their own small pool: a wedged filesystem leaks one thread per
# attempt for good, and the default executor is shared with every other
# off-loop read this app makes.
DELETE_THREADS = 4
# Readiness gets its own pool for the same reason, and a smaller one: the probe
# is unauthenticated, so its rate is set by whoever is calling it.
READY_THREADS = 2
# Carries the dispatch token on a local upload (app/files.py, issue #247).
UPLOAD_TOKEN_HEADER = "X-Upload-Token"
PNG_CONTENT_TYPE = "image/png"
WEBP_CONTENT_TYPE = "image/webp"
DOWNLOAD_NAME_MAX_LENGTH = 96


def stored_content_type(key: str) -> str:
    """Content-Type stored for this key, from the suffix the API assigned.

    Upload already signs this on PUT. Presigned GET repeats it as
    ResponseContentType so a store that dropped object metadata still tells
    the browser this is an image (issue #324).
    """
    return PNG_CONTENT_TYPE if key.endswith(".png") else WEBP_CONTENT_TYPE
DOWNLOAD_NAME_PATTERN = re.compile(
    r"[a-z0-9]+(?:-[a-z0-9]+)*\.[a-z0-9]+",
)


@lru_cache
def _delete_threads() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=DELETE_THREADS, thread_name_prefix="storage-delete")


@lru_cache
def _ready_threads() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=READY_THREADS, thread_name_prefix="storage-ready")


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
# Enough for any real encoder, and it bounds both walks below.
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
    view = memoryview(data)
    position = 8
    width = height = 0
    chunks = 0
    saw_idat = False
    while position + 12 <= len(data):
        chunks += 1
        if chunks > MAX_PNG_CHUNKS:
            raise ValueError("stored PNG has too many chunks")
        length = struct.unpack_from(">I", view, position)[0]
        end = position + 12 + length
        if end > len(data):
            raise ValueError("stored object has a truncated PNG chunk")
        kind = bytes(view[position + 4:position + 8])
        # Seed the CRC with the kind and feed the body as a view. Slicing the
        # body and concatenating it copied the chunk twice, so a 60 MB IDAT
        # cost about 180 MB live for a 64 MB input, and a few concurrent
        # completions would then exhaust the API task.
        checksum = zlib.crc32(view[position + 8:position + 8 + length],
                              zlib.crc32(kind)) & 0xffffffff
        if checksum != struct.unpack_from(">I", view, end - 4)[0]:
            raise ValueError("stored object has a corrupt PNG chunk")
        if chunks == 1:
            if kind != b"IHDR" or length != 13:
                raise ValueError("stored object does not start with a PNG header")
            header = bytes(view[position + 8:position + 21])
            width, height = struct.unpack_from(">II", header, 0)
            depth, colour, compression, filtering, interlace = header[8:13]
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


def _webp_dimensions(data: bytes) -> tuple[int, int]:
    """Dimensions of a stored WebP, or ValueError if it is not a usable one.

    Thumbnails are the only WebP the fleet uploads, and until now nothing read
    them: job_done created the thumbnail row on the worker's word alone. This
    walks the chunk list like the PNG walk above and never decodes a pixel: a
    simple file is one VP8 or VP8L frame, an extended one starts with a VP8X
    header whose canvas the frame must match, and a chunk whose declared
    length does not fit the RIFF payload is a fabricated container, not a
    thumbnail.

    What it does not prove is that the frame decodes. A VP8 chunk needs ten
    header bytes and a VP8L five, so a container carrying a header and no
    compressed data passes here and gets a thumbnail row for a file no decoder
    will render. That is a broken image in the gallery rather than active
    content, because the API serves it with nosniff, and decoding here to catch
    it was twice a denial of service (see the PNG walk above). The limit is
    accepted and recorded in SECURITY.md; issue #281 holds the argument for
    moving a real decoder off this path.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("stored object is not a WebP")
    # The RIFF length counts everything after it, so it must fit the object.
    riff_size = struct.unpack_from("<I", data, 4)[0]
    if riff_size + 8 > len(data):
        raise ValueError("stored WebP is truncated")
    view = memoryview(data)
    payload_end = riff_size + 8
    position = 12
    width = height = 0
    chunks = 0
    saw_vp8x = False
    saw_image = False
    while position + 8 <= payload_end:
        chunks += 1
        if chunks > MAX_PNG_CHUNKS:
            raise ValueError("stored WebP has too many chunks")
        length = struct.unpack_from("<I", view, position + 4)[0]
        # Chunk payloads are padded to an even length.
        end = position + 8 + length + (length & 1)
        if end > payload_end:
            raise ValueError("stored WebP has a truncated chunk")
        kind = bytes(view[position:position + 4])
        if kind == b"VP8X":
            # The extended form only exists as the first chunk, declared
            # exactly 10 bytes, and its header states the canvas for the file.
            if chunks != 1 or length != 10:
                raise ValueError("stored WebP has a misplaced VP8X header")
            saw_vp8x = True
            width = int.from_bytes(view[position + 12:position + 15], "little") + 1
            height = int.from_bytes(view[position + 15:position + 18], "little") + 1
        else:
            # A simple file is exactly one VP8 or VP8L chunk; any chunk after
            # one is neither simple nor extended.
            if not saw_vp8x and chunks != 1:
                raise ValueError("stored WebP has a chunk after a simple image")
            if kind == b"VP8 ":
                if length < 10:
                    raise ValueError("stored WebP has a truncated frame")
                if bytes(view[position + 11:position + 14]) != b"\x9d\x01\x2a":
                    raise ValueError("stored WebP has no lossy keyframe")
                frame_width = struct.unpack_from("<H", view, position + 14)[0] & 0x3fff
                frame_height = struct.unpack_from("<H", view, position + 16)[0] & 0x3fff
                if saw_image:
                    raise ValueError("stored WebP has a second bitstream")
                if saw_vp8x and (width != frame_width or height != frame_height):
                    raise ValueError("stored WebP frame contradicts its VP8X canvas")
                width, height = frame_width, frame_height
                saw_image = True
            elif kind == b"VP8L":
                if length < 5:
                    raise ValueError("stored WebP has a truncated lossless frame")
                if view[position + 8] != 0x2f:
                    raise ValueError("stored WebP has no lossless signature")
                bits = int.from_bytes(view[position + 9:position + 13], "little")
                if bits >> 29:
                    raise ValueError("stored WebP has an unknown lossless version")
                frame_width = (bits & 0x3fff) + 1
                frame_height = ((bits >> 14) & 0x3fff) + 1
                if saw_image:
                    raise ValueError("stored WebP has a second bitstream")
                if saw_vp8x and (width != frame_width or height != frame_height):
                    raise ValueError("stored WebP frame contradicts its VP8X canvas")
                width, height = frame_width, frame_height
                saw_image = True
            else:
                # Animations are refused as well: the fleet uploads a still
                # thumbnail, and accepting an animation would mean validating
                # the whole nested frame structure for no use case.
                if kind not in (b"ALPH", b"ICCP", b"EXIF", b"XMP "):
                    raise ValueError("stored WebP has an unknown chunk")
        position = end
    if position != payload_end:
        raise ValueError("stored WebP has a truncated chunk")
    if not saw_image:
        raise ValueError("stored WebP carries no image data")
    if width == 0 or height == 0:
        raise ValueError("stored WebP has empty dimensions")
    if width > MAX_IMAGE_EDGE or height > MAX_IMAGE_EDGE:
        raise ValueError("stored WebP dimensions are implausible")
    return width, height


def _inspect(data: bytes) -> tuple[int, int, str] | None:
    """Dimensions and content type of stored bytes, or None if unusable.

    The type is decided by the bytes, never by what the peer declared: an
    uploader controls its own Content-Type header.
    """
    for parse, content_type in ((_png_dimensions, PNG_CONTENT_TYPE),
                                (_webp_dimensions, WEBP_CONTENT_TYPE)):
        try:
            width, height = parse(data)
        except ValueError:
            continue
        return width, height, content_type
    return None


class Storage(Protocol):
    async def ready(self) -> bool: ...

    async def upload_target(self, key: str, token: str | None = None) -> UploadTarget: ...

    async def image_info(self, key: str) -> ImageInfo | None: ...

    async def url(self, key: str, download_name: str | None = None) -> str: ...

    async def worker_fetch_url(self, key: str) -> str: ...

    async def promote(self, source_key: str, dest_key: str) -> None: ...

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

    async def ready(self) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_ready_threads(), self.root.is_dir)

    async def upload_target(self, key: str, token: str | None = None) -> UploadTarget:
        content_type = PNG_CONTENT_TYPE if key.endswith(".png") else WEBP_CONTENT_TYPE
        headers = {"Content-Type": content_type}
        if token:
            # The local route has no signature to check, so the dispatch token
            # is what proves this upload belongs to the dispatch that minted
            # the key rather than to anyone who can derive it (issue #247).
            headers[UPLOAD_TOKEN_HEADER] = token
        return UploadTarget(
            url=f"{self.worker_url}/api/v1/files/{key}",
            headers=headers,
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
            inspected = _inspect(data)
            if inspected is None:
                return None
            width, height, content_type = inspected
            return ImageInfo(width=width, height=height, size=len(data),
                             content_type=content_type)

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
        from app import jobs

        capability = secrets.token_urlsafe(32)
        expires = int(time.time()) + WORKER_URL_TTL
        jobs.register_input_capability(capability, key, expires)
        return f"{self.worker_url}/api/v1/worker-input?token={capability}&expires={expires}"

    async def promote(self, source_key: str, dest_key: str) -> None:
        def copy_once() -> None:
            source = self.path(source_key)
            dest = self.path(dest_key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                # A promote that landed, then a failed commit, retries
                # job_done for the same attempt. Overwriting would break
                # write-once; leaving the existing object is the retry.
                return
            descriptor, temporary = tempfile.mkstemp(dir=dest.parent, prefix=".promote-")
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(source.read_bytes())
            except BaseException:
                os.unlink(temporary)
                raise
            try:
                os.link(temporary, dest)
            except FileExistsError:
                # dest.exists() was false, then a concurrent promote published
                # first. The destination is already the write-once object.
                pass
            finally:
                os.unlink(temporary)

        await asyncio.to_thread(copy_once)

    async def delete(self, key: str) -> None:
        # Off the loop, and in a pool of its own. unlink blocks forever on a
        # wedged mount; on the loop that stops every socket in the process, and
        # in the default executor it eventually starves every other to_thread
        # call the app makes, because a caller's timeout abandons the await
        # while the thread stays wedged for good. Here the damage is bounded to
        # DELETE_THREADS stuck threads and deletes failing.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_delete_threads(), self.path(key).unlink, True)


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

    async def ready(self) -> bool:
        loop = asyncio.get_running_loop()
        probe = loop.run_in_executor(
            _ready_threads(), partial(self.client.head_bucket, Bucket=self.bucket)
        )
        try:
            await asyncio.wait_for(probe, timeout=2)
        except Exception:
            return False
        return True

    async def upload_target(self, key: str, token: str | None = None) -> UploadTarget:
        # Presigning is local computation, no network round trip. The token is
        # the local backend's capability; here the signed URL is one already.
        content_type = stored_content_type(key)
        url = self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentType": content_type,
                # Signed so the bucket refuses a second write with 412: a
                # presigned PUT stays valid for its whole TTL and would
                # otherwise replace bytes the API has already verified
                # (issue #249).
                "IfNoneMatch": "*",
            },
            ExpiresIn=OUTPUT_UPLOAD_URL_TTL,
        )
        return UploadTarget(url=url, headers={"Content-Type": content_type,
                                              "If-None-Match": "*"})

    async def image_info(self, key: str) -> ImageInfo | None:
        from botocore.exceptions import ClientError

        def read_object() -> tuple[dict, bytes]:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            try:
                # read(amt) returns at most amt, not exactly amt, so a short
                # read would truncate a valid PNG into a rejection and let an
                # oversized object slip past the length check below. Loop to
                # EOF or one byte past the bound, which is what makes the
                # bound mean anything: the presigned PUT constrains bucket,
                # key and content type only, never size.
                chunks = []
                remaining = MAX_VERIFY_BYTES + 1
                while remaining > 0:
                    chunk = body.read(remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                return response, b"".join(chunks)
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
        inspected = _inspect(data)
        if inspected is None:
            return None
        width, height, content_type = inspected
        return ImageInfo(
            width=width,
            height=height,
            size=response.get("ContentLength", len(data)),
            # Not response ContentType: that is what the uploader declared.
            content_type=content_type,
        )

    async def url(self, key: str, download_name: str | None = None) -> str:
        params = {
            "Bucket": self.bucket,
            "Key": key,
            "ResponseContentType": stored_content_type(key),
            "ResponseCacheControl": "no-store",
        }
        if download_name is not None:
            params["ResponseContentDisposition"] = download_content_disposition(download_name)
        return self.client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=SIGNED_URL_TTL,
        )

    async def worker_fetch_url(self, key: str) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key, "ResponseContentType": stored_content_type(key)},
            ExpiresIn=WORKER_URL_TTL,
        )

    async def promote(self, source_key: str, dest_key: str) -> None:
        from botocore.exceptions import ClientError

        def copy_once() -> None:
            try:
                self.client.head_object(Bucket=self.bucket, Key=dest_key)
                return
            except ClientError as error:
                code = error.response.get("Error", {}).get("Code")
                if code not in ("404", "NoSuchKey", "NotFound"):
                    raise
            self.client.copy_object(
                Bucket=self.bucket,
                Key=dest_key,
                CopySource={"Bucket": self.bucket, "Key": source_key},
                ContentType=stored_content_type(dest_key),
                MetadataDirective="REPLACE",
            )

        await asyncio.to_thread(copy_once)

    async def delete(self, key: str) -> None:
        """Remove the object and, on a versioned bucket, its history.

        delete_object without a VersionId writes a delete marker and keeps the
        bytes as a noncurrent version, so on the cloud bucket, which has
        versioning on, a plain delete hides the key and keeps billing for it.
        The terminal paths in jobs.py delete to reclaim storage from a peer
        that can upload whatever it likes, so hiding is not enough.

        Listing by exact prefix and filtering is deliberate: list_object_versions
        takes a prefix, not a key, so anything that extends this key, such as a
        u/j-attempt-1.png.bak left by an operator, lists under it and must not
        be deleted with it.
        """
        def purge() -> None:
            # The walk runs to the end of the prefix rather than stopping at
            # the first page carrying another key. Versions of one key are
            # contiguous and this key sorts before anything extending it, so an
            # early break is available, and it buys nothing here: the app's own
            # keys diverge before the extension, so a purge sees one page.
            paginator = self.client.get_paginator("list_object_versions")
            failed = 0
            first_code = None
            first_message = None
            for page in paginator.paginate(Bucket=self.bucket, Prefix=key):
                doomed = [
                    {"Key": entry["Key"], "VersionId": entry["VersionId"]}
                    for group in ("Versions", "DeleteMarkers")
                    for entry in page.get(group, [])
                    if entry.get("Key") == key
                ]
                for start in range(0, len(doomed), 1000):  # DeleteObjects caps at 1000
                    response = self.client.delete_objects(
                        Bucket=self.bucket,
                        Delete={"Objects": doomed[start:start + 1000], "Quiet": True},
                    )
                    # Quiet suppresses the successes, not the failures, and the
                    # call answers 200 either way. Discarding this reports a
                    # reclaim that did not happen. Counting instead of raising
                    # here keeps the traversal going: a key past a thousand
                    # versions takes several batches, and one transient error
                    # in the first would otherwise abandon all of the rest.
                    errors = response.get("Errors") or []
                    if errors:
                        failed += len(errors)
                        if first_code is None:
                            # Both, because the caller only logs this line: the
                            # code is what an operator greps for and the message
                            # is what tells them which permission is missing.
                            first_code = errors[0].get("Code")
                            first_message = errors[0].get("Message")
            if failed:
                raise RuntimeError(
                    f"could not delete {failed} version(s) of {key}: "
                    f"{first_code} {first_message}".rstrip()
                )

        await asyncio.to_thread(purge)


@lru_cache
def get_storage() -> Storage:
    settings = get_settings()
    if settings.storage_backend == "s3":
        return S3Storage(settings)
    return LocalStorage(settings.storage_local_path, settings.public_url, settings.worker_url)
