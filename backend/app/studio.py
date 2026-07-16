"""Studio-facing endpoints (local dev metrics, not benchmark scripts)."""

from fastapi import APIRouter, HTTPException

from app.realtime import gpu_command, pick_any_worker

router = APIRouter()


@router.get("/api/v1/studio/gpu")
async def studio_gpu() -> dict:
    """Live GPU snapshot from the connected worker."""
    worker = pick_any_worker()
    if worker is None:
        raise HTTPException(status_code=503, detail="no worker connected")
    result = await gpu_command(worker, {"type": "gpu_status"}, timeout=15.0)
    gpu = result.get("gpu")
    if not isinstance(gpu, dict):
        raise HTTPException(status_code=503, detail="worker did not return gpu metrics")
    return {
        "loaded_models": result.get("loaded_models", []),
        "gpu": gpu,
    }
