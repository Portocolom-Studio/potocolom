"""The model registry (docs/architecture.md, adding a new model).

Availability comes from the connected workers' manifests, in memory; rows in
the models table exist so job history keeps referring to models whose worker
is currently offline.
"""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.dialects.postgresql import insert

from app import db, realtime
from app.auth import current_user
from app.estimates import estimate_gpu_ms, schema_defaults
from app.manifests import Manifest
from app.tables import Model, User

logger = logging.getLogger("potocolom.registry")

# public() runs on every /api/v1/models request and on every for_jobs()
# call, so a drop logged naively would repeat for every poll of the studio.
# Remembering the last-logged drop per model id logs a given drop once, and
# only again when the reason changes or a recovery ends and the drop
# returns; otherwise a pure function would not hold state.
_last_dropped_reason: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}

router = APIRouter()


def available() -> dict[str, Manifest]:
    manifests: dict[str, Manifest] = {}
    for worker in realtime.workers.values():
        for manifest in worker.manifests:
            # Prefer a studio-visible copy when stale workers still advertise
            # benchmark_only=true for the same id (first-connect used to win).
            existing = manifests.get(manifest.id)
            if existing is None or (existing.benchmark_only and not manifest.benchmark_only):
                # A live heartbeat measurement supersedes the calibration
                # estimate from hello, but only for the worker that supplied
                # this manifest. Copied rather than mutated so the worker's
                # manifest stays pristine. public() copies again for
                # capability narrowing, and the two must compose: a model
                # that is both narrowed and measured keeps both, because each
                # copy carries the other's change forward.
                live = worker.frame_p95_ms.get(manifest.id)
                if live is not None:
                    manifest = manifest.model_copy(update={"realtime_p95_ms": live})
                manifests[manifest.id] = manifest
    return manifests


def public() -> dict[str, Manifest]:
    published = {model_id: manifest for model_id, manifest in available().items()
                 if not manifest.benchmark_only}
    public_models: dict[str, Manifest] = {}
    for model_id, manifest in published.items():
        if manifest.studio_capabilities is None:
            _last_dropped_reason.pop(model_id, None)
            public_models[model_id] = manifest
            continue
        # The studio pickers filter on capabilities, so narrowing them here is
        # enough to place the model in only the pickers its measurement backs.
        # Preserve the order capabilities already has.
        narrowed = [cap for cap in manifest.capabilities
                    if cap in manifest.studio_capabilities]
        if not narrowed:
            # A model offered for nothing must not be advertised at all: the
            # studio would list it while no picker could ever offer it, which
            # reads as a removal the user never made. available() still serves
            # it to the benchmark page and the realtime session path.
            reason = (tuple(manifest.capabilities), tuple(manifest.studio_capabilities))
            if _last_dropped_reason.get(model_id) != reason:
                logger.warning(
                    "model %s dropped from the studio: advertised capabilities %s "
                    "narrow to nothing against studio_capabilities %s",
                    manifest.id, manifest.capabilities, manifest.studio_capabilities,
                )
                _last_dropped_reason[model_id] = reason
            continue
        _last_dropped_reason.pop(model_id, None)
        public_models[model_id] = manifest.model_copy(update={"capabilities": narrowed})
    return public_models


def for_jobs() -> dict[str, Manifest]:
    """Manifests eligible for POST /api/v1/generations."""
    from app.settings import get_settings

    if get_settings().benchmark_api:
        return available()
    return public()


@router.get("/api/v1/models")
async def list_models(_user: User = Depends(current_user)) -> list[dict]:
    models = []
    for _, manifest in sorted(public().items()):
        payload = manifest.model_dump()
        defaults = schema_defaults(manifest.parameters)
        payload["estimated_gpu_ms_default"] = estimate_gpu_ms(manifest.id, defaults)
        if "upscale" in manifest.capabilities:
            # Measured per-factor numbers for the studio's factor picker; the
            # default-params estimate cannot express factors with unequal cost.
            # A manifest is worker-supplied, so neither properties nor the
            # factor enum is guaranteed to be the shape it should be; a bare
            # `for` over a non-list 500s this endpoint for every caller.
            properties = manifest.parameters.get("properties")
            factor_spec = properties.get("factor", {}) if isinstance(properties, dict) else {}
            enum = factor_spec.get("enum") if isinstance(factor_spec, dict) else None
            by_factor = {
                str(factor): estimate_gpu_ms(manifest.id, {"factor": factor})
                for factor in (enum if isinstance(enum, list) else [])
            }
            payload["estimated_gpu_ms_by_factor"] = {
                factor: ms for factor, ms in by_factor.items() if ms is not None
            }
        models.append(payload)
    return models


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
