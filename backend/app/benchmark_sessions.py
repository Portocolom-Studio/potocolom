"""Durable benchmark suite history (issue #107)."""

from __future__ import annotations

import statistics
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.auth import current_user, require_role
from app.settings import get_settings
from app.tables import BenchmarkMeasurement, BenchmarkSession, User

router = APIRouter()


# Every int below lands in an int4 column, so a value past that is a 422 from
# the model rather than a DataError from the insert.
Int4 = Field(default=None, ge=-(2**31), lt=2**31)


class MeasurementInput(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    prompt_id: int = Field(ge=-(2**31), lt=2**31)
    title: str
    category: str
    model_id: str
    variant: str
    cell_key: str
    params: dict = Field(default_factory=dict)
    model_load_ms: int | None = Int4
    state: Literal["succeeded", "failed"]
    gpu_ms: int | None = Int4
    wall_s: float | None = None
    width: int | None = Int4
    height: int | None = Int4
    job_id: str | None = None
    file: str | None = None
    error: str | None = None


class BenchmarkInput(BaseModel):
    created_at: datetime
    target_vram_gb: float | None = None
    prompt_count: int = Field(ge=0, lt=2**31)
    models: list[str]
    variants_per_prompt: int = Field(ge=0, lt=2**31)
    total_jobs: int = Field(ge=0, lt=2**31)
    succeeded: int = Field(ge=0, lt=2**31)
    failed: int = Field(ge=0, lt=2**31)
    results: list[MeasurementInput]


def _measurement(row: BenchmarkMeasurement) -> dict:
    result = {
        "prompt_id": row.prompt_id,
        "title": row.title,
        "category": row.category,
        "model_id": row.model_id,
        "variant": row.variant,
        "cell_key": row.cell_key,
        "params": row.params,
        "model_load_ms": row.model_load_ms,
        "state": row.state,
        "gpu_ms": row.gpu_ms,
        "wall_s": row.wall_s,
        "width": row.width,
        "height": row.height,
        "job_id": row.job_id,
        "file": row.file,
        "error": row.error,
    }
    return {key: value for key, value in result.items() if value is not None}


def _model_stats(rows: list[dict]) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        buckets.setdefault(row["model_id"], []).append(row)
    stats = []
    for model_id in sorted(buckets):
        bucket = buckets[model_id]
        succeeded = [row for row in bucket if row["state"] == "succeeded"]
        gpu = [row["gpu_ms"] for row in succeeded if row.get("gpu_ms") is not None]
        wall = [row["wall_s"] for row in succeeded if row.get("wall_s") is not None]
        stats.append({
            "model_id": model_id,
            "succeeded": len(succeeded),
            "failed": len(bucket) - len(succeeded),
            "load_ms": succeeded[0].get("model_load_ms") if succeeded else None,
            "avg_gpu_ms": statistics.mean(gpu) if gpu else None,
            "median_gpu_ms": statistics.median(gpu) if gpu else None,
            "avg_wall_s": statistics.mean(wall) if wall else 0.0,
        })
    return stats


async def _report(session: AsyncSession, row: BenchmarkSession) -> dict:
    measurements = (
        await session.execute(
            select(BenchmarkMeasurement)
            .where(BenchmarkMeasurement.session_id == row.id)
            .order_by(BenchmarkMeasurement.position)
        )
    ).scalars().all()
    results = [_measurement(item) for item in measurements]
    prompts = []
    for prompt_id in dict.fromkeys(item["prompt_id"] for item in results):
        runs = [item for item in results if item["prompt_id"] == prompt_id]
        prompts.append({
            "id": prompt_id,
            "title": runs[0]["title"],
            "category": runs[0]["category"],
            "runs": runs,
        })
    return {
        "id": str(row.id),
        "created_at": row.created_at.isoformat(),
        "target_vram_gb": row.target_vram_gb,
        "prompt_count": row.prompt_count,
        "models": row.models,
        "variants_per_prompt": row.variants_per_prompt,
        "total_jobs": row.total_jobs,
        "succeeded": row.succeeded,
        "failed": row.failed,
        "model_stats": _model_stats(results),
        "results": results,
        "prompts": prompts,
    }


def require_benchmark_api() -> None:
    if not get_settings().benchmark_api:
        raise HTTPException(status_code=404, detail="benchmark API disabled")


@router.post("/api/v1/benchmark/sessions", status_code=201)
async def create_benchmark_session(
    report: BenchmarkInput,
    _: None = Depends(require_benchmark_api),
    user: User = Depends(require_role("member")),
    session: AsyncSession = Depends(db.get_session),
) -> dict:
    row = BenchmarkSession(
        user_id=user.id,
        created_at=report.created_at,
        target_vram_gb=report.target_vram_gb,
        prompt_count=report.prompt_count,
        models=report.models,
        variants_per_prompt=report.variants_per_prompt,
        total_jobs=report.total_jobs,
        succeeded=report.succeeded,
        failed=report.failed,
    )
    session.add(row)
    await session.flush()
    session.add_all([
        BenchmarkMeasurement(
            session_id=row.id,
            position=position,
            **measurement.model_dump(),
        )
        for position, measurement in enumerate(report.results)
    ])
    await session.commit()
    return {"id": str(row.id)}


@router.get("/api/v1/benchmark/sessions")
async def list_benchmark_sessions(
    limit: int = 50,
    cursor: uuid.UUID | None = None,
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(db.get_session),
) -> list[dict]:
    query = select(BenchmarkSession)
    if cursor is not None:
        anchor = await session.get(BenchmarkSession, cursor)
        if anchor is None:
            raise HTTPException(status_code=404, detail="unknown cursor")
        query = query.where(
            or_(
                BenchmarkSession.created_at < anchor.created_at,
                and_(
                    BenchmarkSession.created_at == anchor.created_at,
                    BenchmarkSession.id < anchor.id,
                ),
            )
        )
    rows = (
        await session.execute(
            query
            .order_by(BenchmarkSession.created_at.desc(), BenchmarkSession.id.desc())
            .limit(min(max(limit, 1), 200))
        )
    ).scalars().all()
    return [
        {
            "id": str(row.id),
            "created_at": row.created_at.isoformat(),
            "models": row.models,
            "total_jobs": row.total_jobs,
            "succeeded": row.succeeded,
            "failed": row.failed,
        }
        for row in rows
    ]


@router.get("/api/v1/benchmark/sessions/{session_id}")
async def get_benchmark_session(
    session_id: uuid.UUID,
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(db.get_session),
) -> dict:
    row = await session.get(BenchmarkSession, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such benchmark session")
    return await _report(session, row)
