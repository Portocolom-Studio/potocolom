#!/usr/bin/env python3
"""Import local images into studio history so they can be upscaled.

Dropping files into data/<user>/ alone is not enough: the history strip reads
Postgres jobs ordered by created_at. This script writes WebP + thumbnail under
STORAGE_LOCAL_PATH and inserts succeeded job/asset rows with created_at = now,
so imports land at the front of the strip.

    mkdir -p data/import
    # put PNG/JPEG/WebP files in data/import/
    STORAGE_LOCAL_PATH=$PWD/data \\
      backend/.venv/bin/python scripts/import-images.py data/import/*

Requires: make deps (Postgres), and a model row already registered (start the
worker once). Pillow comes from worker/.venv via a short subprocess.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import db  # noqa: E402
from app.settings import get_settings  # noqa: E402
from app.tables import Asset, Job, Model, User  # noqa: E402

THUMB_EDGE = 384
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}


def encode_webp_pair(src: Path) -> tuple[bytes, bytes, int, int, int, int]:
    """Return (full_webp, thumb_webp, width, height, thumb_w, thumb_h)."""
    helper = r"""
import json, sys
from io import BytesIO
from PIL import Image

src = sys.argv[1]
edge = int(sys.argv[2])
image = Image.open(src).convert("RGB")
width, height = image.size
full = BytesIO()
image.save(full, format="WEBP", quality=90, method=4)
thumb = image.copy()
if max(width, height) > edge:
    scale = edge / max(width, height)
    thumb = thumb.resize((int(width * scale), int(height * scale)), Image.Resampling.LANCZOS)
thumb_buf = BytesIO()
thumb.save(thumb_buf, format="WEBP", quality=80, method=4)
meta = {"width": width, "height": height, "tw": thumb.size[0], "th": thumb.size[1],
        "full_len": full.tell(), "thumb_len": thumb_buf.tell()}
sys.stdout.buffer.write(json.dumps(meta).encode() + b"\n")
sys.stdout.buffer.write(full.getvalue())
sys.stdout.buffer.write(thumb_buf.getvalue())
"""
    proc = subprocess.run(
        [str(ROOT / "worker/.venv/bin/python"), "-c", helper, str(src), str(THUMB_EDGE)],
        check=True,
        capture_output=True,
    )
    line, _, rest = proc.stdout.partition(b"\n")
    meta = json.loads(line)
    full = rest[: meta["full_len"]]
    thumb = rest[meta["full_len"] : meta["full_len"] + meta["thumb_len"]]
    return full, thumb, meta["width"], meta["height"], meta["tw"], meta["th"]


async def pick_model_id(session, preferred: str | None) -> str:
    if preferred:
        row = await session.get(Model, preferred)
        if row is None:
            raise SystemExit(f"model not registered: {preferred}")
        return preferred
    row = (
        await session.execute(select(Model.id).order_by(Model.id).limit(1))
    ).scalar_one_or_none()
    if row is None:
        raise SystemExit("no models in the database; start a worker once so manifests register")
    return row


async def import_one(
    session,
    *,
    user_id: uuid.UUID,
    model_id: str,
    path: Path,
    created_at: datetime,
) -> uuid.UUID:
    full_bytes, thumb_bytes, width, height, tw, th = encode_webp_pair(path)
    job_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    thumb_id = uuid.uuid4()
    settings = get_settings()
    root = Path(settings.storage_local_path)
    storage_key = f"{user_id}/{job_id}.webp"
    thumb_key = f"{user_id}/{job_id}-thumb.webp"
    full_path = root / storage_key
    thumb_path = root / thumb_key
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(full_bytes)
    thumb_path.write_bytes(thumb_bytes)

    job = Job(
        id=job_id,
        user_id=user_id,
        model_id=model_id,
        params={"prompt": f"imported: {path.name}", "imported": True},
        state="succeeded",
        attempt=1,
        gpu_ms=0,
        finished_at=created_at,
        created_at=created_at,
    )
    session.add(job)
    await session.flush()
    full = Asset(
        id=asset_id,
        user_id=user_id,
        job_id=job_id,
        storage_key=storage_key,
        mime="image/webp",
        width=width,
        height=height,
    )
    session.add(full)
    await session.flush()
    session.add(
        Asset(
            id=thumb_id,
            user_id=user_id,
            job_id=job_id,
            parent_asset_id=asset_id,
            storage_key=thumb_key,
            mime="image/webp",
            width=tw,
            height=th,
        )
    )
    return job_id


async def main(paths: list[Path], model: str | None) -> None:
    images = [p for p in paths if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES]
    if not images:
        raise SystemExit("no image files to import")
    if not await db.connect():
        raise SystemExit("database unavailable (make deps?)")
    settings = get_settings()
    print(f"storage: {settings.storage_local_path}")
    async with db.session_factory() as session:
        user_id = await session.scalar(select(User.id).order_by(User.created_at).limit(1))
        if user_id is None:
            raise SystemExit("no local user row")
        model_id = await pick_model_id(session, model)
        # Last path argument becomes the newest (front of the strip).
        base = datetime.now(timezone.utc)
        for index, path in enumerate(images):
            created_at = base - timedelta(milliseconds=(len(images) - 1 - index))
            job_id = await import_one(
                session, user_id=user_id, model_id=model_id, path=path, created_at=created_at
            )
            print(f"imported {path.name} -> {job_id}")
        await session.commit()
    await db.dispose()
    print(f"done: {len(images)} image(s) at front of history; refresh /app")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="image files or globs already expanded")
    parser.add_argument("--model", default=None, help="model_id to attach (default: first registered)")
    args = parser.parse_args()
    asyncio.run(main(args.paths, args.model))
