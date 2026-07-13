#!/usr/bin/env python3
"""Delete failed generation jobs (and any linked asset rows) from PostgreSQL."""

import asyncio
import sys
from pathlib import Path

from sqlalchemy import delete, func, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import db  # noqa: E402
from app.tables import Asset, Job  # noqa: E402


async def main() -> None:
    if not await db.connect():
        raise SystemExit("database unavailable")
    async with db.session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(Job).where(Job.state == "failed")
        )
        print(f"deleting {count} failed jobs")
        failed_ids = (
            await session.execute(select(Job.id).where(Job.state == "failed"))
        ).scalars().all()
        if failed_ids:
            await session.execute(delete(Asset).where(Asset.job_id.in_(failed_ids)))
            await session.execute(delete(Job).where(Job.id.in_(failed_ids)))
            await session.commit()
        remaining = await session.scalar(
            select(func.count()).select_from(Job).where(Job.state == "failed")
        )
        print(f"remaining failed: {remaining}")
    await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
