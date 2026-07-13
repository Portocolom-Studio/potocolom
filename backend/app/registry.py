"""The model registry (docs/architecture.md, adding a new model).

Availability comes from the connected workers' manifests, in memory; rows in
the models table exist so job history keeps referring to models whose worker
is currently offline.
"""

import logging

from fastapi import APIRouter
from sqlalchemy.dialects.postgresql import insert

from app import db, realtime
from app.manifests import Manifest
from app.tables import Model

logger = logging.getLogger("potocolom.registry")

router = APIRouter()


def available() -> dict[str, Manifest]:
    manifests: dict[str, Manifest] = {}
    for worker in realtime.workers.values():
        for manifest in worker.manifests:
            manifests.setdefault(manifest.id, manifest)
    return manifests


def public() -> dict[str, Manifest]:
    return {model_id: manifest for model_id, manifest in available().items()
            if not manifest.benchmark_only}


@router.get("/api/v1/models")
async def list_models() -> list[dict]:
    return [
        manifest.model_dump()
        for _, manifest in sorted(public().items())
    ]


async def persist_manifests(manifests: list[Manifest]) -> None:
    if db.session_factory is None or not manifests:
        return  # degraded mode: the in-memory registry still serves /models
    rows = [
        {
            "id": m.id,
            "name": m.name,
            "capabilities": m.capabilities,
            "parameters_schema": m.parameters,
            "min_vram_gb": m.min_vram_gb,
        }
        for m in manifests
    ]
    statement = insert(Model).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[Model.id],
        set_={
            "name": statement.excluded.name,
            "capabilities": statement.excluded.capabilities,
            "parameters_schema": statement.excluded.parameters_schema,
            "min_vram_gb": statement.excluded.min_vram_gb,
        },
    )
    async with db.session_factory() as session:
        await session.execute(statement)
        await session.commit()
