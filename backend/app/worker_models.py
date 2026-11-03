"""Models installed on worker hardware: the admin view, its commands, and the worker's reports.

A worker that can install models lists "model_store" in its hello capabilities.
Only such a worker is sent install_model, cancel_install or remove_model, and
only such a worker's store_status and models_changed are read. The state is
live on the connection: a reconnect starts again from the worker's hello.
"""

from __future__ import annotations

import logging
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException
from starlette.websockets import WebSocketDisconnect

from app import realtime
from app.auth import require_role
from app.manifests import parse_manifests
from app.realtime import ProtocolError, Worker
from app.tables import User

logger = logging.getLogger(__name__)

CAPABILITY = "model_store"
GIB = 2**30
# The classes a studio picker offers, each suggested once per worker.
CLASSES = ("realtime", "text_to_image", "upscale")
DOWNLOAD_STATES = frozenset({"queued", "downloading", "done", "failed"})
CATALOG_FLAGS = ("installed", "needs_token", "download_bytes")
_SEND_FAILURES = (WebSocketDisconnect, RuntimeError, ConnectionError, BrokenPipeError)

router = APIRouter()


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_hardware(raw: object) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("hardware must be an object")
    vram = raw.get("vram_total_bytes")
    if (not isinstance(raw.get("gpu_name"), str)
            or not (vram is None or _count(vram))
            or not _count(raw.get("disk_total_bytes"))
            or not _count(raw.get("disk_free_bytes"))):
        raise ValueError("hardware fields have wrong types")
    return {
        "gpu_name": raw["gpu_name"],
        "vram_total_bytes": vram,
        "disk_total_bytes": raw["disk_total_bytes"],
        "disk_free_bytes": raw["disk_free_bytes"],
    }


def parse_catalog(raw: object) -> list[dict]:
    """Every bundled manifest with its install flags, validated like hello.models."""
    if not isinstance(raw, list):
        raise ValueError("catalog must be a list")
    rows = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("catalog entry must be an object")
        flags = {name: entry.get(name) for name in CATALOG_FLAGS}
        if (not isinstance(flags["installed"], bool)
                or not isinstance(flags["needs_token"], bool)
                or not (flags["download_bytes"] is None or _count(flags["download_bytes"]))):
            raise ValueError("catalog flags have wrong types")
        fields = {name: value for name, value in entry.items() if name not in CATALOG_FLAGS}
        (manifest,) = parse_manifests([fields])
        rows.append({**manifest.model_dump(mode="json"), **flags})
    return rows


def parse_downloads(raw: object) -> dict[str, dict]:
    if not isinstance(raw, list):
        raise ValueError("downloads must be a list")
    result: dict[str, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("download entry must be an object")
        model_id, state, error = entry.get("model_id"), entry.get("state"), entry.get("error")
        if (not isinstance(model_id, str) or state not in DOWNLOAD_STATES
                or not isinstance(entry.get("kind"), str)
                or entry["kind"] not in {"install", "remove", "cancel"}
                or not _count(entry.get("bytes_done")) or not _count(entry.get("bytes_total"))
                or not (error is None or (isinstance(error, str) and len(error) <= 64))
                or not (entry.get("op_id") is None or isinstance(entry["op_id"], str))):
            raise ValueError("download entry has wrong types")
        result[model_id] = {
            "model_id": model_id,
            "state": state,
            "kind": entry["kind"],
            "bytes_done": entry["bytes_done"],
            "bytes_total": entry["bytes_total"],
            "error": error,
            "op_id": entry.get("op_id"),
        }
    return result


def fits(entry: dict, vram_bytes: int | None) -> bool:
    return vram_bytes is not None and vram_bytes >= entry["min_vram_gb"] * GIB


def suggest(catalog: list[dict], vram_bytes: int | None) -> dict[str, str | None]:
    """The model to offer for each class: the default when it fits, else the fitting
    model with the largest min_vram_gb. Benchmark-only and token-gated models are
    never offered, and a class with no fitting model has no suggestion."""
    picks: dict[str, str | None] = {}
    for cls in CLASSES:
        members = [
            entry for entry in catalog
            if cls in entry["capabilities"] and not entry["benchmark_only"]
            and not entry["needs_token"] and fits(entry, vram_bytes)
        ]
        defaults = [entry for entry in members if entry["default"]]
        if defaults:
            picks[cls] = defaults[0]["id"]
        elif members:
            picks[cls] = min(members, key=lambda e: (-e["min_vram_gb"], e["id"]))["id"]
        else:
            picks[cls] = None
    return picks


def manageable(worker: Worker) -> bool:
    return CAPABILITY in worker.capabilities and worker.catalog is not None


async def apply(worker: Worker, control: dict) -> None:
    """Read one store report from a worker that advertised the capability."""
    if control["type"] == "store_inventory":
        op_id = control.get("op_id")
        if op_id is not None and not isinstance(op_id, str):
            raise ValueError("op_id must be a string or null")
        if (isinstance(op_id, str) and len(op_id) >= 21 and op_id[:20].isascii()
                and op_id[:20].isdigit() and op_id[20] == "-"):
            _observe_time(worker.id, int(op_id[:20]))
        worker.hardware = parse_hardware(control.get("hardware"))
        worker.catalog = parse_catalog(control.get("catalog"))
        return
    if control["type"] == "store_status":
        downloads = parse_downloads(control.get("downloads"))
        free = control.get("disk_free_bytes")
        if not _count(free):
            raise ValueError("disk_free_bytes must be a count")
        worker.store_downloads = downloads
        worker.disk_free_bytes = free
        return
    models = parse_manifests(control.get("models"))
    if worker.protocol_version == 6 and any(
        realtime._schema_carries_ref(manifest.parameters) for manifest in models
    ):
        raise ProtocolError("parameter schema must not use $ref")
    catalog = parse_catalog(control.get("catalog"))
    if worker.protocol_version == 6:
        from app import worker_authority

        await worker_authority.update_worker_models(
            worker.id, realtime._require_worker_incarnation(worker),
            [manifest.model_dump(mode="json") for manifest in models],
        )
    worker.catalog = catalog
    worker.manifests = models
    from app import registry  # late import; registry reads this module's state

    await registry.persist_manifests(worker.manifests)


def worker_view(worker: Worker) -> dict:
    catalog = worker.catalog or []
    if not manageable(worker):
        return {
            "worker_id": worker.id,
            "manageable": False,
            "reason": "worker predates model management",
            "hardware": worker.hardware,
            "disk_free_bytes": worker.disk_free_bytes,
            "suggested": {},
            "models": [],
        }
    vram = (worker.hardware or {}).get("vram_total_bytes")
    picks = suggest(catalog, vram)
    suggested = {model_id for model_id in picks.values() if model_id is not None}
    return {
        "worker_id": worker.id,
        "manageable": True,
        "hardware": worker.hardware,
        "disk_free_bytes": worker.disk_free_bytes,
        "suggested": picks,
        "models": [
            {
                **entry,
                "fits": fits(entry, vram),
                "suggested": entry["id"] in suggested,
                "download": worker.store_downloads.get(entry["id"]),
            }
            for entry in catalog
        ],
    }


def _managed(worker_id: str, model_id: str) -> tuple[Worker, dict]:
    worker = realtime.workers.get(worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="no such worker")
    if not manageable(worker):
        raise HTTPException(status_code=409, detail="worker predates model management")
    for entry in worker.catalog or []:
        if entry["id"] == model_id:
            return worker, entry
    raise HTTPException(status_code=404, detail="model is not in the worker's catalog")


_last_op_time: dict[str, int] = {}


def _observe_time(worker_id: str, reported: int) -> None:
    now = time.time_ns()
    if reported > now + 60_000_000_000:
        logger.warning("ignoring future model operation time from worker %s", worker_id)
        return
    _last_op_time[worker_id] = max(_last_op_time.get(worker_id, 0), reported)


async def _send(worker: Worker, kind: str, model_id: str) -> str:
    for row in worker.store_downloads.values():
        value = row.get("op_id")
        if (isinstance(value, str) and len(value) >= 21 and value[:20].isascii()
                and value[:20].isdigit() and value[20] == "-"):
            _observe_time(worker.id, int(value[:20]))
    stamp = max(time.time_ns(), _last_op_time.get(worker.id, 0) + 1)
    if stamp > 10**20 - 1:
        raise HTTPException(status_code=503, detail="operation clock exhausted")
    _last_op_time[worker.id] = stamp
    op_id = f"{stamp:020d}-{secrets.token_hex(4)}"
    command: dict = {"type": kind, "model_id": model_id, "op_id": op_id}
    if worker.protocol_version == 6:
        # A protocol 6 worker validates every control against its closed shape,
        # which names the worker and the connection it belongs to.
        command["worker_id"] = worker.id
        command["incarnation"] = str(worker.incarnation)
    try:
        await worker.ws.send_json(command)
    except _SEND_FAILURES as error:
        raise HTTPException(status_code=503, detail="worker disconnected") from error
    return op_id


@router.get("/api/v1/admin/models")
async def admin_models(_user: User = Depends(require_role("admin"))) -> list[dict]:
    return [worker_view(worker) for worker in list(realtime.workers.values())]


@router.post("/api/v1/admin/workers/{worker_id}/models/{model_id}/install", status_code=202)
async def install_model(worker_id: str, model_id: str,
                        _user: User = Depends(require_role("admin"))) -> dict:
    worker, entry = _managed(worker_id, model_id)
    if entry["needs_token"]:
        raise HTTPException(status_code=409, detail="needs_token")
    op_id = await _send(worker, "install_model", model_id)
    return {"op_id": op_id}


@router.post("/api/v1/admin/workers/{worker_id}/models/{model_id}/cancel", status_code=202)
async def cancel_install(worker_id: str, model_id: str,
                         _user: User = Depends(require_role("admin"))) -> dict:
    worker, _entry = _managed(worker_id, model_id)
    op_id = await _send(worker, "cancel_install", model_id)
    return {"op_id": op_id}


@router.delete("/api/v1/admin/workers/{worker_id}/models/{model_id}", status_code=202)
async def remove_model(worker_id: str, model_id: str,
                       _user: User = Depends(require_role("admin"))) -> dict:
    # The worker refuses a model that serves a live session or a running job; the
    # refusal arrives as a store_status entry with the reason "in_use".
    worker, _entry = _managed(worker_id, model_id)
    op_id = await _send(worker, "remove_model", model_id)
    return {"op_id": op_id}
