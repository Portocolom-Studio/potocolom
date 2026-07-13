"""Benchmark GPU lifecycle endpoints.

The benchmark script drives explicit load / unload so timings are not mixed
with warm-cache state from earlier jobs. Commands are forwarded to a connected
worker over the fleet socket (docs/connection-handling.md).
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.realtime import gpu_command, pick_any_worker, pick_worker_for_model

router = APIRouter()

LOAD_TIMEOUT = 1800.0
GPU_TIMEOUT = 120.0


@router.get("/api/v1/benchmark/models")
async def benchmark_models() -> list[dict]:
    """All worker manifests, including benchmark_only (hidden from the studio UI)."""
    from app import registry

    return [manifest.model_dump() for _, manifest in sorted(registry.available().items())]


@router.get("/api/v1/benchmark/gpu")
async def gpu_status() -> dict:
    worker = pick_any_worker()
    if worker is None:
        raise HTTPException(status_code=503, detail="no worker connected")
    return await gpu_command(worker, {"type": "gpu_status"}, timeout=GPU_TIMEOUT)


class LoadRequest(BaseModel):
    model_id: str


@router.post("/api/v1/benchmark/gpu/load")
async def gpu_load(request: LoadRequest) -> dict:
    worker = pick_worker_for_model(request.model_id)
    if worker is None:
        raise HTTPException(status_code=503,
                            detail=f"no worker serves model {request.model_id}")
    result = await gpu_command(worker, {"type": "load_model",
                                        "model_id": request.model_id},
                               timeout=LOAD_TIMEOUT)
    if result.get("type") == "gpu_error":
        raise HTTPException(status_code=500, detail=result.get("reason", "load failed"))
    return result


class UnloadRequest(BaseModel):
    model_id: str | None = None


@router.post("/api/v1/benchmark/gpu/unload")
async def gpu_unload(request: UnloadRequest = UnloadRequest()) -> dict:
    worker = pick_any_worker()
    if worker is None:
        raise HTTPException(status_code=503, detail="no worker connected")
    command: dict = {"type": "unload_model"}
    if request.model_id:
        command["model_id"] = request.model_id
    result = await gpu_command(worker, command, timeout=GPU_TIMEOUT)
    if result.get("type") == "gpu_error":
        raise HTTPException(status_code=500, detail=result.get("reason", "unload failed"))
    return result
