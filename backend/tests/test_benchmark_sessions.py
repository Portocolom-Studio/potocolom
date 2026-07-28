import asyncio
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete

from app import db
from app.main import app
from app.settings import get_settings
from app.tables import BenchmarkSession, User


REPORT = {
    "created_at": "2026-07-28T10:00:00+00:00",
    "api": "http://localhost:8000",
    "out_dir": "data/benchmark/test",
    "target_vram_gb": 16,
    "prompt_count": 1,
    "models": ["sd-test"],
    "variants_per_prompt": 1,
    "total_jobs": 1,
    "succeeded": 1,
    "failed": 0,
    "results": [{
        "prompt_id": 1,
        "title": "Test",
        "category": "art",
        "model_id": "sd-test",
        "variant": "default",
        "cell_key": "sd-test__default",
        "params": {"steps": 2},
        "model_load_ms": 10,
        "state": "succeeded",
        "job_id": "job-1",
        "file": "images/test.webp",
        "width": 512,
        "height": 512,
        "gpu_ms": 100,
        "wall_s": 0.2,
    }],
}


@pytest.mark.db
def test_benchmark_session_ingest_list_and_read(monkeypatch):
    monkeypatch.setenv("BENCHMARK_API", "1")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            created = client.post("/api/v1/benchmark/sessions", json=REPORT)
            assert created.status_code == 201
            session_id = created.json()["id"]

            listed = client.get("/api/v1/benchmark/sessions")
            assert listed.status_code == 200
            assert listed.json()[0]["id"] == session_id

            detail = client.get(f"/api/v1/benchmark/sessions/{session_id}")
            assert detail.status_code == 200
            body = detail.json()
            assert body["results"][0]["cell_key"] == "sd-test__default"
            assert body["results"][0]["file"] == "images/test.webp"
            assert body["model_stats"][0]["avg_gpu_ms"] == 100
            assert "api" not in body
            assert "out_dir" not in body
            assert client.get(
                "/api/v1/benchmark/sessions/00000000-0000-0000-0000-000000000000"
            ).status_code == 404
    finally:
        get_settings.cache_clear()


@pytest.mark.db
def test_benchmark_session_list_uses_cursor_paging(monkeypatch):
    monkeypatch.setenv("BENCHMARK_API", "1")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            ids = []
            for day in (1, 2, 3):
                report = {**REPORT, "created_at": f"2099-01-0{day}T10:00:00+00:00"}
                created = client.post("/api/v1/benchmark/sessions", json=report)
                assert created.status_code == 201
                ids.append(created.json()["id"])

            first = client.get("/api/v1/benchmark/sessions?limit=2")
            assert first.status_code == 200
            assert [row["id"] for row in first.json()] == [ids[2], ids[1]]

            second = client.get(
                f"/api/v1/benchmark/sessions?limit=2&cursor={first.json()[-1]['id']}"
            )
            assert second.status_code == 200
            assert second.json()[0]["id"] == ids[0]
    finally:
        get_settings.cache_clear()


@pytest.mark.db
def test_benchmark_session_ingest_is_gated(monkeypatch):
    monkeypatch.delenv("BENCHMARK_API", raising=False)
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            assert client.post("/api/v1/benchmark/sessions", json=REPORT).status_code == 404
            assert client.get("/api/v1/benchmark/sessions").status_code == 200
    finally:
        get_settings.cache_clear()


@pytest.mark.db
def test_benchmark_session_reads_are_install_scoped(monkeypatch):
    monkeypatch.setenv("BENCHMARK_API", "1")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            created = client.post("/api/v1/benchmark/sessions", json=REPORT)
            assert created.status_code == 201
            session_id = uuid.UUID(created.json()["id"])

            async def change_provenance() -> None:
                assert db.session_factory is not None
                other_id = uuid.uuid4()
                async with db.session_factory() as session:
                    session.add(User(id=other_id, email=f"{other_id}@example.test"))
                    await session.flush()
                    row = await session.get(BenchmarkSession, session_id)
                    assert row is not None
                    row.user_id = other_id
                    await session.commit()

            asyncio.run(change_provenance())
            listed = client.get("/api/v1/benchmark/sessions")
            assert session_id in {uuid.UUID(row["id"]) for row in listed.json()}
            assert client.get(
                f"/api/v1/benchmark/sessions/{session_id}"
            ).status_code == 200
    finally:
        get_settings.cache_clear()


@pytest.mark.db
def test_benchmark_history_survives_deleting_the_account_that_ran_it(monkeypatch):
    """Provenance, not ownership: the install keeps its own hardware history."""
    monkeypatch.setenv("BENCHMARK_API", "1")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            created = client.post("/api/v1/benchmark/sessions", json=REPORT)
            assert created.status_code == 201
            session_id = uuid.UUID(created.json()["id"])

            async def delete_the_runner() -> None:
                assert db.session_factory is not None
                runner_id = uuid.uuid4()
                async with db.session_factory() as session:
                    session.add(User(id=runner_id, email=f"{runner_id}@example.test"))
                    await session.flush()
                    row = await session.get(BenchmarkSession, session_id)
                    assert row is not None
                    row.user_id = runner_id
                    await session.commit()
                async with db.session_factory() as session:
                    await session.execute(delete(User).where(User.id == runner_id))
                    await session.commit()

            asyncio.run(delete_the_runner())
            assert client.get(f"/api/v1/benchmark/sessions/{session_id}").status_code == 200

            async def provenance_cleared() -> None:
                assert db.session_factory is not None
                async with db.session_factory() as session:
                    row = await session.get(BenchmarkSession, session_id)
                    assert row is not None and row.user_id is None

            asyncio.run(provenance_cleared())
    finally:
        get_settings.cache_clear()
