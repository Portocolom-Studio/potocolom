"""GPU metrics persistence and history API (issue #94)."""

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db
from app.main import app
from app.realtime import PROTOCOL_VERSION
from app.tables import GpuSample, Job

MANIFEST = {
    "id": "sd-metrics",
    "name": "SD Metrics",
    "capabilities": ["text_to_image"],
    "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}},
    "min_vram_gb": 0,
}


def fleet_hello(ws, worker_id="w-metrics"):
    ws.send_json({
        "type": "hello",
        "protocol_version": PROTOCOL_VERSION,
        "worker_id": worker_id,
        "models": [MANIFEST],
        "realtime_slots": 1,
    })
    assert ws.receive_json()["type"] == "registered"


async def _wait_for_samples(count: int = 1, timeout: float = 3.0) -> list[GpuSample]:
    assert db.session_factory is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with db.session_factory() as session:
            rows = (await session.execute(select(GpuSample))).scalars().all()
            if len(rows) >= count:
                return rows
        await asyncio.sleep(0.05)
    return []


@pytest.mark.db
def test_heartbeat_persists_gpu_sample():
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker)
            worker.send_json({
                "type": "heartbeat",
                "slots_in_use": 0,
                "loaded_models": ["sd-metrics"],
                "gpu": {
                    "device": "rocm",
                    "available": True,
                    "util_pct": 42,
                    "vram_used_bytes": 4_000_000_000,
                    "vram_total_bytes": 8_000_000_000,
                    "temperature_c": 61.0,
                    "power_w": 120.0,
                },
            })
            rows = asyncio.run(_wait_for_samples())
            assert rows, "heartbeat sample was not persisted"
            row = rows[0]
            assert row.worker_id == "w-metrics"
            assert row.util_pct == 42
            assert row.loaded_models == ["sd-metrics"]


@pytest.mark.db
def test_gpu_history_round_trip():
    assert db.session_factory is not None
    now = datetime.now(timezone.utc)
    sample = GpuSample(
        worker_id="w-history",
        sampled_at=now - timedelta(minutes=10),
        util_pct=55,
        vram_used_bytes=3_000_000_000,
        vram_total_bytes=6_000_000_000,
        temperature_c=58.0,
        power_w=95.0,
        loaded_models=["sd-metrics"],
    )

    async def insert():
        async with db.session_factory() as session:
            session.add(sample)
            await session.commit()

    asyncio.run(insert())

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/metrics/gpu/history",
            params={
                "from": int((now - timedelta(hours=1)).timestamp() * 1000),
                "to": int(now.timestamp() * 1000),
                "rollup": "raw",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["rollup"] == "raw"
        assert len(body["samples"]) == 1
        point = body["samples"][0]
        assert point["util_pct"] == 55
        assert point["vram_used_pct"] == 50


@pytest.mark.db
def test_job_dispatch_and_finish_timestamps():
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-ts")

            created = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-metrics", "params": {"prompt": "metrics"}},
            )
            assert created.status_code == 202
            job_id = uuid.UUID(created.json()["job_id"])

            dispatch = worker.receive_json()
            assert dispatch["type"] == "dispatch_job"

            assert db.session_factory is not None

            async def read_job():
                async with db.session_factory() as session:
                    return await session.get(Job, job_id)

            job = asyncio.run(read_job())
            assert job is not None
            assert job.dispatched_at is not None
            assert job.state == "running"

            worker.send_json({
                "type": "job_done",
                "job_id": str(job_id),
                "gpu_ms": 900,
                "width": 512,
                "height": 512,
            })

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                job = asyncio.run(read_job())
                if job.state == "succeeded":
                    break
                time.sleep(0.05)
            assert job.finished_at is not None
