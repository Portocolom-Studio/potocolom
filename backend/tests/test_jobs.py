"""The job dispatch and history flow (#16), driven with a fake worker over the
real fleet WebSocket. Real inference is the worker's side (worker/tests)."""

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import delete, func, select
from fastapi.testclient import TestClient

from app import db, jobs, realtime
from app.jobs import generation_download_name
from app.main import app
from app.realtime import PROTOCOL_VERSION
from app.tables import Asset, Job, Model, UsageEvent, User

MANIFEST = {
    "id": "sd-test",
    "name": "SD Test",
    "capabilities": ["text_to_image", "image_to_image"],
    "parameters": {
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
        "required": ["prompt"],
    },
    "min_vram_gb": 0,
    "prompt_token_limit": 77,
}

MANIFEST_T2I_ONLY = {
    **MANIFEST,
    "id": "sd-t2i",
    "capabilities": ["text_to_image"],
}

MANIFEST_WITH_RT = {
    **MANIFEST,
    "capabilities": ["text_to_image", "image_to_image", "realtime"],
}

HOSTILE_PROMPT = 'A "lighthouse"\n; ../../ caf\u00e9'


def test_generation_download_name_falls_back_to_model():
    job = Job(
        model_id="sd-test",
        params={"prompt": " \n "},
        created_at=datetime(2026, 7, 29, 14, 25, 30, tzinfo=timezone.utc),
    )
    asset = Asset(storage_key="user/generation.png", mime="image/png")
    assert generation_download_name(job, asset) == "potocolom-20260729-142530-sd-test.png"


def test_generation_download_name_uses_webp_master_extension():
    job = Job(
        model_id="sd-test",
        params={"prompt": "A lighthouse"},
        created_at=datetime(2026, 7, 29, 14, 25, 30, tzinfo=timezone.utc),
    )
    asset = Asset(storage_key="user/generation.webp", mime="image/webp")
    assert generation_download_name(job, asset) == (
        "potocolom-20260729-142530-a-lighthouse.webp"
    )


def test_generation_download_name_uses_mime_for_extensionless_key():
    job = Job(
        model_id="sd-test",
        params={"prompt": "A lighthouse"},
        created_at=datetime(2026, 7, 29, 14, 25, 30, tzinfo=timezone.utc),
    )
    asset = Asset(storage_key="user/generation", mime="image/webp")
    assert generation_download_name(job, asset) == (
        "potocolom-20260729-142530-a-lighthouse.webp"
    )


@pytest.mark.parametrize(
    ("storage_key", "mime", "expected_extension"),
    [
        ("user/generation.PNG", "image/png", "png"),
        ("user/generation", "image/WEBP", "webp"),
    ],
)
def test_generation_download_name_normalizes_extension(
    storage_key,
    mime,
    expected_extension,
):
    job = Job(
        model_id="sd-test",
        params={"prompt": "A lighthouse"},
        created_at=datetime(2026, 7, 29, 14, 25, 30, tzinfo=timezone.utc),
    )
    asset = Asset(storage_key=storage_key, mime=mime)

    assert generation_download_name(job, asset) == (
        f"potocolom-20260729-142530-a-lighthouse.{expected_extension}"
    )


def test_generation_download_name_includes_batch_position():
    job = Job(
        model_id="sd-test",
        params={"prompt": "A lighthouse"},
        created_at=datetime(2026, 7, 29, 14, 25, 30, tzinfo=timezone.utc),
    )
    asset = Asset(storage_key="user/generation.webp", mime="image/webp")
    assert generation_download_name(job, asset, position=2) == (
        "potocolom-20260729-142530-a-lighthouse-2.webp"
    )


def fleet_hello(ws, worker_id, manifest=MANIFEST):
    ws.send_json({"type": "hello", "protocol_version": PROTOCOL_VERSION,
                  "worker_id": worker_id, "models": [manifest], "realtime_slots": 1})
    assert ws.receive_json()["type"] == "registered"


def poll_until(client, job_id, state, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/generations/{job_id}").json()
        if job["state"] == state:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached {state}")


@pytest.mark.db
def test_generation_download_names_count_only_visible_masters():
    created_at = datetime(2026, 7, 29, 14, 25, 30, tzinfo=timezone.utc)

    async def seed_generation() -> uuid.UUID:
        assert db.local_user_id is not None
        assert db.session_factory is not None
        job_id = uuid.uuid4()
        async with db.session_factory() as session:
            if await session.get(Model, "sd-test") is None:
                session.add(Model(
                    id="sd-test",
                    name="SD Test",
                    capabilities=["text_to_image"],
                    parameters_schema=MANIFEST["parameters"],
                    min_vram_gb=0,
                ))
            # The models table is deliberately not truncated, so an existing row
            # can hide this model-to-job ordering requirement. Flush the model
            # before the job on a pristine database, then flush the job before its
            # asset because jobs.source_asset_id and assets.job_id form a cycle
            # that SQLAlchemy cannot order.
            await session.flush()
            session.add(Job(
                id=job_id,
                user_id=db.local_user_id,
                model_id="sd-test",
                params={"prompt": "A lighthouse"},
                state="succeeded",
                created_at=created_at,
            ))
            await session.flush()
            session.add(Asset(
                user_id=db.local_user_id,
                job_id=job_id,
                storage_key=f"{db.local_user_id}/{job_id}.webp",
                mime="image/webp",
                width=512,
                height=512,
            ))
            await session.commit()
        return job_id

    async def add_second_asset(job_id: uuid.UUID) -> None:
        assert db.local_user_id is not None
        assert db.session_factory is not None
        async with db.session_factory() as session:
            session.add(Asset(
                user_id=db.local_user_id,
                job_id=job_id,
                storage_key=f"{db.local_user_id}/{job_id}-second.png",
                mime="image/png",
                width=512,
                height=512,
            ))
            await session.commit()

    async def seed_generation_with_expired_first() -> uuid.UUID:
        assert db.local_user_id is not None
        assert db.session_factory is not None
        job_id = uuid.uuid4()
        async with db.session_factory() as session:
            if await session.get(Model, "sd-test") is None:
                session.add(Model(
                    id="sd-test",
                    name="SD Test",
                    capabilities=["text_to_image"],
                    parameters_schema=MANIFEST["parameters"],
                    min_vram_gb=0,
                ))
            await session.flush()
            session.add(Job(
                id=job_id,
                user_id=db.local_user_id,
                model_id="sd-test",
                params={"prompt": "A lighthouse"},
                state="succeeded",
                created_at=created_at,
            ))
            await session.flush()
            session.add(Asset(
                user_id=db.local_user_id,
                job_id=job_id,
                storage_key=f"{db.local_user_id}/{job_id}-expired.png",
                mime="image/png",
                width=512,
                height=512,
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            ))
            await session.flush()
            session.add(Asset(
                user_id=db.local_user_id,
                job_id=job_id,
                storage_key=f"{db.local_user_id}/{job_id}-survivor.webp",
                mime="image/webp",
                width=512,
                height=512,
            ))
            await session.commit()
        return job_id

    base_name = "potocolom-20260729-142530-a-lighthouse"
    with TestClient(app) as client:
        job_id = asyncio.run(seed_generation())
        single_asset = client.get(f"/api/v1/generations/{job_id}").json()["assets"][0]
        assert parse_qs(urlsplit(single_asset["download_url"]).query) == {
            "download": [f"{base_name}.webp"],
        }

        asyncio.run(add_second_asset(job_id))
        batch_assets = client.get(f"/api/v1/generations/{job_id}").json()["assets"]
        assert len(batch_assets) == 2
        for position, asset in enumerate(batch_assets, start=1):
            extension = asset["url"].rsplit(".", 1)[-1]
            assert parse_qs(urlsplit(asset["download_url"]).query) == {
                "download": [f"{base_name}-{position}.{extension}"],
            }

        expired_first_job_id = asyncio.run(seed_generation_with_expired_first())
        surviving_assets = client.get(
            f"/api/v1/generations/{expired_first_job_id}"
        ).json()["assets"]
        assert len(surviving_assets) == 1
        assert parse_qs(urlsplit(surviving_assets[0]["download_url"]).query) == {
            "download": [f"{base_name}.webp"],
        }


@pytest.mark.db
def test_generation_end_to_end():
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-jobs")

            models = client.get("/api/v1/models").json()
            assert any(m["id"] == "sd-test" for m in models)
            # The studio's prompt warning (issue #148) reads the window here,
            # so it has to survive the worker hello and reach the browser.
            listed = next(m for m in models if m["id"] == "sd-test")
            assert listed["prompt_token_limit"] == 77

            async def job_events() -> int:
                assert db.session_factory is not None
                async with db.session_factory() as session:
                    return int(await session.scalar(
                        select(func.count()).select_from(UsageEvent).where(
                            UsageEvent.model_id == "sd-test",
                            UsageEvent.kind == "job",
                            UsageEvent.action == "generate",
                        )
                    ) or 0)

            # usage_events carries no job id, and the database is truncated once
            # per session, so this job's row is identified by the count rising
            # rather than by any matching row existing.
            events_before = asyncio.run(job_events())

            created = client.post("/api/v1/generations",
                                  json={"model_id": "sd-test",
                                        "params": {"prompt": HOSTILE_PROMPT}})
            assert created.status_code == 202
            job_id = created.json()["job_id"]

            dispatch = worker.receive_json()
            assert dispatch["type"] == "dispatch_job"
            assert dispatch["job_id"] == job_id
            assert dispatch["params"] == {"prompt": HOSTILE_PROMPT}
            assert dispatch["upload"]["url"].endswith(f"/{job_id}.png")
            assert dispatch["upload"]["headers"] == {"Content-Type": "image/png"}
            assert dispatch["thumb_upload"]["url"].endswith(f"/{job_id}-thumb.webp")
            assert dispatch["thumb_upload"]["headers"] == {"Content-Type": "image/webp"}

            upload_path = urlsplit(dispatch["upload"]["url"]).path
            assert client.put(upload_path, content=b"png-bytes").status_code == 200
            thumb_path = urlsplit(dispatch["thumb_upload"]["url"]).path
            assert client.put(thumb_path, content=b"thumb-bytes").status_code == 200

            worker.send_json({"type": "job_progress", "job_id": job_id, "progress": 0.5})
            worker.send_json({"type": "job_done", "job_id": job_id,
                              "gpu_ms": 1234, "width": 512, "height": 512,
                              "has_thumbnail": True})

            job = poll_until(client, job_id, "succeeded")
            assert job["gpu_ms"] == 1234
            asset = job["assets"][0]
            assert asset["width"] == 512
            assert asset["mime"] == "image/png"
            assert asset["url"].endswith(f"/{job_id}.png")
            created_stamp = datetime.fromisoformat(job["created_at"]).strftime("%Y%m%d-%H%M%S")
            expected_name = f"potocolom-{created_stamp}-a-lighthouse-cafe.png"
            download_url = urlsplit(asset["download_url"])
            assert parse_qs(download_url.query) == {"download": [expected_name]}
            download_response = client.get(f"{download_url.path}?{download_url.query}")
            assert download_response.content == b"png-bytes"
            assert download_response.headers["content-disposition"] == (
                f'attachment; filename="{expected_name}"'
            )
            assert asset["thumbnail_url"] is not None
            assert client.get(urlsplit(asset["url"]).path).content == b"png-bytes"
            assert client.get(urlsplit(asset["thumbnail_url"]).path).content == b"thumb-bytes"

            history = client.get("/api/v1/generations").json()
            assert any(entry["id"] == job_id for entry in history)

            assert client.post(f"/api/v1/generations/{job_id}/star").status_code == 204
            assert client.post(f"/api/v1/generations/{job_id}/star").status_code == 204
            favorites = client.get("/api/v1/generations?starred=true").json()
            assert [entry["id"] for entry in favorites] == [job_id]
            assert favorites[0]["starred_at"] is not None

            async def expire_asset() -> None:
                assert db.session_factory is not None
                async with db.session_factory() as session:
                    row = await session.get(Asset, uuid.UUID(asset["id"]))
                    assert row is not None
                    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                    await session.commit()

            asyncio.run(expire_asset())
            expired = client.get("/api/v1/generations?starred=true").json()[0]
            assert expired["assets"] == []
            assert expired["expired_favorite"] is True

            assert client.delete(f"/api/v1/generations/{job_id}/star").status_code == 204
            assert client.delete(f"/api/v1/generations/{job_id}/star").status_code == 204
            assert client.get("/api/v1/generations?starred=true").json() == []
            assert client.post(f"/api/v1/generations/{uuid.uuid4()}/star").status_code == 404

            # The event stream replays the terminal state and ends.
            events = client.get(f"/api/v1/generations/{job_id}/events")
            assert "succeeded" in events.text

            def usage_written() -> bool:
                return asyncio.run(job_events()) > events_before

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not usage_written():
                time.sleep(0.05)
            assert usage_written()


@pytest.mark.db
def test_benchmark_only_model_hidden_without_benchmark_api():
    bench_manifest = {**MANIFEST, "id": "bench-only", "benchmark_only": True}
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-bench", manifest=bench_manifest)
            missing = client.post("/api/v1/generations",
                                  json={"model_id": "bench-only",
                                        "params": {"prompt": "hidden"}})
            assert missing.status_code == 404


@pytest.mark.db
def test_benchmark_only_model_allowed_when_benchmark_api_enabled(monkeypatch):
    monkeypatch.setenv("BENCHMARK_API", "1")
    from app.settings import get_settings

    get_settings.cache_clear()
    bench_manifest = {**MANIFEST, "id": "bench-only", "benchmark_only": True}
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-bench", manifest=bench_manifest)
            created = client.post("/api/v1/generations",
                                  json={"model_id": "bench-only",
                                        "params": {"prompt": "benchmark"}})
            assert created.status_code == 202


@pytest.mark.db
def test_unknown_model_and_invalid_params():
    with TestClient(app) as client:
        missing = client.post("/api/v1/generations", json={"model_id": "nope", "params": {}})
        assert missing.status_code == 404
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-validate")
            invalid = client.post("/api/v1/generations",
                                  json={"model_id": "sd-test", "params": {}})
            assert invalid.status_code == 422  # prompt is required by the manifest schema


@pytest.mark.db
def test_worker_loss_requeues_once():
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-dies")
            job_id = client.post("/api/v1/generations",
                                 json={"model_id": "sd-test",
                                       "params": {"prompt": "retry me"}}).json()["job_id"]
            assert worker.receive_json()["job_id"] == job_id
        # The worker died mid job: the job gets its one retry on the next worker.
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-heals")
            redispatch = worker.receive_json()
            assert redispatch["job_id"] == job_id
            worker.send_json({"type": "job_failed", "job_id": job_id, "reason": "boom"})
            poll_until(client, job_id, "failed")


async def _seed_recover_jobs() -> tuple[uuid.UUID, uuid.UUID]:
    assert db.local_user_id is not None
    assert db.session_factory is not None
    queued_id = uuid.uuid4()
    running_id = uuid.uuid4()
    async with db.session_factory() as session:
        # recover() dispatches everything queued or running, so a leftover job
        # from an earlier case in this session would arrive at the worker here
        # and break the assertion about which two were dispatched. The database
        # is truncated once per session, not per test.
        await session.execute(delete(Job).where(Job.state.in_(("queued", "running"))))
        if await session.get(Model, "sd-test") is None:
            session.add(Model(
                id="sd-test",
                name="SD Test",
                capabilities=["text_to_image"],
                parameters_schema=MANIFEST["parameters"],
                min_vram_gb=0,
            ))
        # Keep the model-to-job foreign key order explicit on a pristine database.
        await session.flush()
        session.add(Job(
            id=queued_id,
            user_id=db.local_user_id,
            model_id="sd-test",
            params={"prompt": "queued"},
            state="queued",
            attempt=1,
        ))
        session.add(Job(
            id=running_id,
            user_id=db.local_user_id,
            model_id="sd-test",
            params={"prompt": "running"},
            state="running",
            attempt=1,
        ))
        await session.commit()
    return queued_id, running_id


def poll_until_attempt(client, job_id, attempt: int, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/generations/{job_id}").json()
        if job.get("attempt") == attempt:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached attempt {attempt}")


@pytest.mark.db
def test_stalled_job_requeues_once(monkeypatch):
    monkeypatch.setenv("JOB_STALL_SECONDS", "0.05")
    from app.settings import get_settings
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/fleet") as worker:
                fleet_hello(worker, "w-stall")
                job_id = client.post("/api/v1/generations",
                                     json={"model_id": "sd-test",
                                           "params": {"prompt": "stall"}}).json()["job_id"]
                assert worker.receive_json()["job_id"] == job_id
                # Worker stays connected but sends no progress; stall requeues once
                # and the retry is dispatched back to the same connected worker.
                poll_until_attempt(client, job_id, 2, timeout=3.0)
                redispatch = worker.receive_json()
                assert redispatch["type"] == "dispatch_job"
                assert redispatch["job_id"] == job_id
    finally:
        monkeypatch.delenv("JOB_STALL_SECONDS", raising=False)
        get_settings.cache_clear()


@pytest.mark.db
def test_recover_requeues_running_and_dispatches_queued():
    async def prepare() -> tuple[uuid.UUID, uuid.UUID]:
        if not await db.connect():
            pytest.skip("database unavailable")
        ids = await _seed_recover_jobs()
        await db.dispose()
        return ids

    queued_id, running_id = asyncio.run(prepare())

    with TestClient(app) as client:
        requeued = client.get(f"/api/v1/generations/{running_id}").json()
        assert requeued["state"] == "queued"

        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-recover")

            first = worker.receive_json()
            assert first["type"] == "dispatch_job"
            first_id = first["job_id"]

            upload_path = urlsplit(first["upload"]["url"]).path
            assert client.put(upload_path, content=b"png-bytes").status_code == 200
            thumb_path = urlsplit(first["thumb_upload"]["url"]).path
            assert client.put(thumb_path, content=b"thumb-bytes").status_code == 200
            worker.send_json({"type": "job_done", "job_id": first_id,
                              "gpu_ms": 1, "width": 512, "height": 512,
                              "has_thumbnail": True})
            poll_until(client, first_id, "succeeded")

            second = worker.receive_json()
            assert second["type"] == "dispatch_job"
            second_id = second["job_id"]
            assert {first_id, second_id} == {str(queued_id), str(running_id)}


@pytest.mark.db
def test_img2img_dispatch_includes_input_url():
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-i2i")

            created = client.post("/api/v1/generations",
                                  json={"model_id": "sd-test",
                                        "params": {"prompt": "a lighthouse"}})
            assert created.status_code == 202
            source_job_id = created.json()["job_id"]

            dispatch = worker.receive_json()
            upload_path = urlsplit(dispatch["upload"]["url"]).path
            assert client.put(upload_path, content=b"source-png").status_code == 200
            worker.send_json({"type": "job_done", "job_id": source_job_id,
                              "gpu_ms": 100, "width": 512, "height": 512})
            source_job = poll_until(client, source_job_id, "succeeded")
            source_asset_id = source_job["assets"][0]["id"]

            edit = client.post("/api/v1/generations",
                               json={"model_id": "sd-test",
                                     "params": {"prompt": "a red lighthouse"},
                                     "source_asset_id": source_asset_id})
            assert edit.status_code == 202
            edit_job_id = edit.json()["job_id"]

            i2i_dispatch = worker.receive_json()
            assert i2i_dispatch["type"] == "dispatch_job"
            assert i2i_dispatch["job_id"] == edit_job_id
            assert "input" in i2i_dispatch
            input_path = urlsplit(i2i_dispatch["input"]["url"]).path
            assert client.get(input_path).content == b"source-png"

            assert client.put(urlsplit(i2i_dispatch["upload"]["url"]).path,
                              content=b"edited-png").status_code == 200
            worker.send_json({"type": "job_done", "job_id": edit_job_id,
                              "gpu_ms": 200, "width": 512, "height": 512})
            edit_job = poll_until(client, edit_job_id, "succeeded")
            assert edit_job["assets"][0]["url"].endswith(".png")
            assert edit_job["source_asset_id"] == source_asset_id


@pytest.mark.db
def test_img2img_rejects_model_without_capability():
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-i2i-cap", MANIFEST_T2I_ONLY)

            created = client.post("/api/v1/generations",
                                  json={"model_id": "sd-t2i",
                                        "params": {"prompt": "seed"}})
            job_id = created.json()["job_id"]
            dispatch = worker.receive_json()
            upload_path = urlsplit(dispatch["upload"]["url"]).path
            assert client.put(upload_path, content=b"source").status_code == 200
            worker.send_json({"type": "job_done", "job_id": job_id,
                              "gpu_ms": 1, "width": 512, "height": 512})
            source_job = poll_until(client, job_id, "succeeded")
            source_asset_id = source_job["assets"][0]["id"]

            rejected = client.post("/api/v1/generations",
                                   json={"model_id": "sd-t2i",
                                         "params": {"prompt": "edit"},
                                         "source_asset_id": source_asset_id})
            assert rejected.status_code == 422
            detail = rejected.json()["detail"]
            assert "image_to_image" in detail or "upscale" in detail


@pytest.mark.db
def test_upscale_dispatch_includes_input_url():
    upscale_manifest = {
        "id": "realesrgan",
        "name": "Real-ESRGAN",
        "capabilities": ["upscale"],
        "parameters": {
            "type": "object",
            "properties": {"factor": {"type": "integer", "enum": [2, 4], "default": 2}},
            "required": ["factor"],
        },
        "min_vram_gb": 4,
    }
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-upscale-seed")
            created = client.post("/api/v1/generations",
                                  json={"model_id": "sd-test",
                                        "params": {"prompt": "seed"}})
            source_job_id = created.json()["job_id"]
            dispatch = worker.receive_json()
            assert client.put(urlsplit(dispatch["upload"]["url"]).path,
                              content=b"source-png").status_code == 200
            worker.send_json({"type": "job_done", "job_id": source_job_id,
                              "gpu_ms": 50, "width": 512, "height": 512})
            source_asset_id = poll_until(client, source_job_id, "succeeded")["assets"][0]["id"]

        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json({
                "type": "hello",
                "protocol_version": PROTOCOL_VERSION,
                "worker_id": "w-upscale",
                "models": [MANIFEST, upscale_manifest],
                "realtime_slots": 1,
            })
            assert worker.receive_json()["type"] == "registered"

            upscale = client.post("/api/v1/generations",
                                  json={"model_id": "realesrgan",
                                        "params": {"factor": 2},
                                        "source_asset_id": source_asset_id})
            assert upscale.status_code == 202
            upscale_job_id = upscale.json()["job_id"]

            up_dispatch = worker.receive_json()
            assert up_dispatch["type"] == "dispatch_job"
            assert up_dispatch["job_id"] == upscale_job_id
            assert up_dispatch["params"] == {"factor": 2}
            assert "input" in up_dispatch
            assert client.get(urlsplit(up_dispatch["input"]["url"]).path).content == b"source-png"

            assert client.put(urlsplit(up_dispatch["upload"]["url"]).path,
                              content=b"upscaled-png").status_code == 200
            worker.send_json({"type": "job_done", "job_id": upscale_job_id,
                              "gpu_ms": 400, "width": 1024, "height": 1024})
            done = poll_until(client, upscale_job_id, "succeeded")
            assert done["gpu_ms"] == 400
            assert done["assets"][0]["width"] == 1024


@pytest.mark.db
def test_models_expose_measured_upscale_estimates():
    fast_manifest = {
        "id": "realesrgan-fast",
        "name": "Real-ESRGAN Fast",
        "capabilities": ["upscale"],
        "parameters": {
            "type": "object",
            "properties": {"factor": {"type": "integer", "enum": [2, 4], "default": 2}},
            "required": ["factor"],
        },
        "min_vram_gb": 1,
    }
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json({
                "type": "hello",
                "protocol_version": PROTOCOL_VERSION,
                "worker_id": "w-upscale-estimates",
                "models": [MANIFEST, fast_manifest],
                "realtime_slots": 1,
            })
            assert worker.receive_json()["type"] == "registered"

            models = client.get("/api/v1/models").json()
            fast = next(m for m in models if m["id"] == "realesrgan-fast")
            assert fast["estimated_gpu_ms_by_factor"] == {"2": 711, "4": 532}
            assert fast["estimated_gpu_ms_default"] == 711
            diffusion = next(m for m in models if m["id"] == "sd-test")
            assert "estimated_gpu_ms_by_factor" not in diffusion


@pytest.mark.db
def test_upscale_rejects_without_source_asset():
    upscale_manifest = {
        "id": "realesrgan",
        "name": "Real-ESRGAN",
        "capabilities": ["upscale"],
        "parameters": {
            "type": "object",
            "properties": {"factor": {"type": "integer", "enum": [2, 4], "default": 2}},
            "required": ["factor"],
        },
        "min_vram_gb": 4,
    }
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-upscale-nosrc", upscale_manifest)
            rejected = client.post("/api/v1/generations",
                                   json={"model_id": "realesrgan",
                                         "params": {"factor": 2}})
            assert rejected.status_code == 422
            assert "source_asset_id" in rejected.json()["detail"]


@pytest.mark.db
def test_upscale_mixed_capabilities_rejected_at_hello():
    from starlette.websockets import WebSocketDisconnect

    bad = {
        "id": "bad-upscale",
        "name": "Bad",
        "capabilities": ["upscale", "text_to_image"],
        "parameters": {},
        "min_vram_gb": 0,
    }
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json({
                "type": "hello",
                "protocol_version": PROTOCOL_VERSION,
                "worker_id": "w-bad-upscale",
                "models": [bad],
                "realtime_slots": 0,
            })
            with pytest.raises(WebSocketDisconnect) as closed:
                worker.receive_json()
            assert closed.value.code == 4000


@pytest.mark.db
def test_job_phase_timings_persisted():
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-phases")

            created = client.post("/api/v1/generations",
                                  json={"model_id": "sd-test",
                                        "params": {"prompt": "timing"}})
            job_id = created.json()["job_id"]
            dispatch = worker.receive_json()
            upload_path = urlsplit(dispatch["upload"]["url"]).path
            assert client.put(upload_path, content=b"png-bytes").status_code == 200

            worker.send_json({"type": "job_done", "job_id": job_id,
                              "gpu_ms": 900, "input_fetch_ms": 50,
                              "load_ms": 1200, "postprocess_ms": 80,
                              "width": 512, "height": 512})

            job = poll_until(client, job_id, "succeeded")
            assert job["gpu_ms"] == 900
            assert job["input_fetch_ms"] == 50
            assert job["load_ms"] == 1200
            assert job["postprocess_ms"] == 80
            assert job["dispatched_at"] is not None
            assert job["finished_at"] is not None


@pytest.mark.db
def test_job_failure_reason_persisted():
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-fail-reason")

            created = client.post("/api/v1/generations",
                                  json={"model_id": "sd-test",
                                        "params": {"prompt": "fail"}})
            job_id = created.json()["job_id"]
            worker.receive_json()
            worker.send_json({"type": "job_failed", "job_id": job_id,
                              "reason": "CUDA OOM"})

            job = poll_until(client, job_id, "failed")
            assert job["failure_reason"] == "CUDA OOM"
            assert job["finished_at"] is not None

            failed = client.get("/api/v1/generations", params={"state": "failed", "limit": 20})
            assert failed.status_code == 200
            rows = failed.json()
            assert any(row["id"] == job_id for row in rows)
            assert all(row["state"] == "failed" for row in rows)
            assert next(row for row in rows if row["id"] == job_id)["failure_reason"] == "CUDA OOM"

            bad = client.get("/api/v1/generations", params={"state": "nope"})
            assert bad.status_code == 422


def _post_generation(client, prompt: str) -> str:
    created = client.post("/api/v1/generations",
                          json={"model_id": "sd-test", "params": {"prompt": prompt}})
    assert created.status_code == 202
    return created.json()["job_id"]


def _stall_safe(monkeypatch) -> None:
    monkeypatch.setenv("JOB_STALL_SECONDS", "600")
    from app.settings import get_settings
    get_settings.cache_clear()


def _wait_for_dispatch(worker, expected: set[str], timeout=5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = worker.receive_json()
        if msg["type"] == "dispatch_job" and msg["job_id"] in expected:
            return msg
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for dispatch_job in {expected}")


def _finish_job(client, worker, dispatch: dict) -> None:
    upload_path = urlsplit(dispatch["upload"]["url"]).path
    assert client.put(upload_path, content=b"png-bytes").status_code == 200
    worker.send_json({"type": "job_done", "job_id": dispatch["job_id"],
                      "gpu_ms": 1, "width": 512, "height": 512})
    poll_until(client, dispatch["job_id"], "succeeded")


@pytest.mark.db
def test_dispatch_depth_two_before_job_done(monkeypatch):
    """Depth 2: API dispatches a second job while the first is still uploading."""
    _stall_safe(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-depth2")

            first_id = _post_generation(client, "one")
            second_id = _post_generation(client, "two")
            expected = {first_id, second_id}
            time.sleep(0.35)

            first = _wait_for_dispatch(worker, expected)
            second = _wait_for_dispatch(worker, expected - {first["job_id"]})
            assert {first["job_id"], second["job_id"]} == expected

            # Leave no running jobs for the next test.
            _finish_job(client, worker, first)
            _finish_job(client, worker, second)


@pytest.mark.db
def test_dispatch_depth_blocks_third_until_slot_frees(monkeypatch):
    _stall_safe(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-depth-cap")

            id_a = _post_generation(client, "a")
            id_b = _post_generation(client, "b")
            expected = {id_a, id_b}
            time.sleep(0.35)
            d1 = _wait_for_dispatch(worker, expected)
            d2 = _wait_for_dispatch(worker, expected - {d1["job_id"]})
            worker.send_json({"type": "job_progress", "job_id": d1["job_id"], "progress": 0.5})
            worker.send_json({"type": "job_progress", "job_id": d2["job_id"], "progress": 0.5})

            third_id = _post_generation(client, "c")
            time.sleep(0.35)
            assert client.get(f"/api/v1/generations/{third_id}").json()["state"] == "queued"

            _finish_job(client, worker, d1)

            time.sleep(0.35)
            d3 = _wait_for_dispatch(worker, {third_id})
            assert d3["job_id"] == third_id

            _finish_job(client, worker, d2)
            _finish_job(client, worker, d3)


def test_pick_job_worker_prefers_least_loaded():
    from unittest.mock import MagicMock

    from app import jobs, realtime
    from app.manifests import Manifest

    manifest = Manifest(id="sd-test", name="SD Test", capabilities=["text_to_image"],
                        parameters={})
    busy = realtime.Worker(id="w-busy", ws=MagicMock(), manifests=[manifest],
                           realtime_slots=1, jobs_in_flight=1)
    idle = realtime.Worker(id="w-idle", ws=MagicMock(), manifests=[manifest],
                           realtime_slots=1, jobs_in_flight=0)
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers["w-busy"] = busy
        realtime.workers["w-idle"] = idle
        assert jobs.pick_job_worker("sd-test") is idle
    finally:
        realtime.workers.clear()
        # Skip disconnected TestClient sockets; restoring them leaves zombies
        # that make studio/gpu fail with ClosedResourceError instead of 503.
        from starlette.websockets import WebSocketState

        for worker_id, worker in saved.items():
            state = getattr(worker.ws, "client_state", None)
            if state is None or state == WebSocketState.CONNECTED:
                realtime.workers[worker_id] = worker


def test_job_dispatch_depth_one_while_realtime_live():
    from unittest.mock import MagicMock

    from app import jobs, realtime
    from app.manifests import Manifest

    manifest = Manifest(id="sd-test", name="SD Test", capabilities=["text_to_image"],
                        parameters={})
    drawing = realtime.Worker(id="w-drawing", ws=MagicMock(), manifests=[manifest],
                              realtime_slots=1, slots_in_use=1, jobs_in_flight=1)
    idle = realtime.Worker(id="w-idle", ws=MagicMock(), manifests=[manifest],
                           realtime_slots=1, jobs_in_flight=0)
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers["w-drawing"] = drawing
        realtime.workers["w-idle"] = idle
        assert jobs.job_dispatch_depth(drawing) == 1
        assert jobs.pick_job_worker("sd-test") is idle
    finally:
        realtime.workers.clear()
        from starlette.websockets import WebSocketState

        for worker_id, worker in saved.items():
            state = getattr(worker.ws, "client_state", None)
            if state is None or state == WebSocketState.CONNECTED:
                realtime.workers[worker_id] = worker


@pytest.mark.db
def test_dispatch_depth_one_while_realtime_session_open(monkeypatch):
    """Sessions-first: depth drops to 1 while a drawing slot is live."""
    _stall_safe(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-session-jobs", manifest=MANIFEST_WITH_RT)

            with client.websocket_connect("/api/v1/realtime") as browser:
                browser.send_json({"type": "open", "model_id": "sd-test",
                                   "params": {"prompt": "live drawing"}})
                opened = worker.receive_json()
                assert opened["type"] == "open_session"
                worker.send_json({"type": "session_ready", "session_id": opened["session_id"]})
                assert browser.receive_json()["type"] == "ready"

                first_id = _post_generation(client, "during-session-1")
                second_id = _post_generation(client, "during-session-2")
                time.sleep(0.35)
                first = _wait_for_dispatch(worker, {first_id})
                assert client.get(f"/api/v1/generations/{second_id}").json()["state"] == "queued"
                _finish_job(client, worker, first)


async def _seed_lineage_generation(
    session,
    *,
    user_id: uuid.UUID,
    model_id: str,
    capabilities: list[str],
    prompt: str,
    created_at: datetime,
    source_asset_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    if await session.get(Model, model_id) is None:
        session.add(Model(
            id=model_id,
            name=model_id,
            capabilities=capabilities,
            parameters_schema={},
            min_vram_gb=0,
        ))
        await session.flush()
    job_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    session.add(Job(
        id=job_id,
        user_id=user_id,
        model_id=model_id,
        params={"prompt": prompt},
        state="succeeded",
        attempt=1,
        source_asset_id=source_asset_id,
        created_at=created_at,
    ))
    await session.flush()
    session.add(Asset(
        id=asset_id,
        user_id=user_id,
        job_id=job_id,
        parent_asset_id=source_asset_id,
        storage_key=f"{user_id}/{job_id}.png",
        mime="image/png",
        width=512,
        height=512,
        expires_at=expires_at,
    ))
    await session.flush()
    session.add(Asset(
        user_id=user_id,
        job_id=job_id,
        parent_asset_id=asset_id,
        storage_key=f"{user_id}/{job_id}-thumb.webp",
        mime="image/webp",
        width=384,
        height=384,
    ))
    await session.flush()
    return job_id, asset_id


@pytest.mark.db
def test_generation_history_roots_only_filter_pages_roots():
    with TestClient(app) as client:
        async def seed() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            future = datetime.now(timezone.utc) + timedelta(days=365)
            async with db.session_factory() as session:
                newest_root_id, newest_root_asset_id = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="roots-filter-generate",
                    capabilities=["text_to_image"],
                    prompt="newest root",
                    created_at=future,
                )
                older_root_id, _ = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="roots-filter-generate",
                    capabilities=["text_to_image"],
                    prompt="older root",
                    created_at=future - timedelta(seconds=1),
                )
                child_id, _ = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="roots-filter-edit",
                    capabilities=["image_to_image"],
                    prompt="child",
                    created_at=future - timedelta(seconds=2),
                    source_asset_id=newest_root_asset_id,
                )
                await session.commit()
            return newest_root_id, older_root_id, child_id

        newest_root_id, older_root_id, child_id = asyncio.run(seed())

        first = client.get(
            "/api/v1/generations",
            params={"roots_only": "true", "limit": 1},
        )
        assert first.status_code == 200
        assert [row["id"] for row in first.json()] == [str(newest_root_id)]
        assert first.json()[0]["has_derivatives"] is True

        detail = client.get(f"/api/v1/generations/{newest_root_id}")
        assert detail.status_code == 200
        assert detail.json()["has_derivatives"] is True

        second = client.get(
            "/api/v1/generations",
            params={
                "roots_only": "true",
                "limit": 1,
                "cursor": str(newest_root_id),
            },
        )
        assert second.status_code == 200
        assert [row["id"] for row in second.json()] == [str(older_root_id)]
        assert second.json()[0]["has_derivatives"] is False

        derivatives = client.get(
            "/api/v1/generations",
            params={"roots_only": "false", "limit": 200},
        )
        assert derivatives.status_code == 200
        assert str(child_id) in [row["id"] for row in derivatives.json()]
        child = next(row for row in derivatives.json() if row["id"] == str(child_id))
        assert child["has_derivatives"] is False

        wrong_cursor = client.get(
            "/api/v1/generations",
            params={"roots_only": "true", "cursor": str(child_id)},
        )
        assert wrong_cursor.status_code == 404


@pytest.mark.db
def test_generation_lineage_chain_orders_ancestors_and_children():
    with TestClient(app) as client:
        async def seed() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            now = datetime.now(timezone.utc)
            async with db.session_factory() as session:
                root_id, root_asset_id = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="lineage-chain-generate",
                    capabilities=["text_to_image"],
                    prompt="root",
                    created_at=now,
                )
                edit_id, edit_asset_id = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="lineage-chain-edit",
                    capabilities=["image_to_image"],
                    prompt="edit",
                    created_at=now + timedelta(seconds=1),
                    source_asset_id=root_asset_id,
                )
                upscale_id, _ = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="lineage-chain-upscale",
                    capabilities=["upscale"],
                    prompt="upscale",
                    created_at=now + timedelta(seconds=2),
                    source_asset_id=edit_asset_id,
                )
                await session.commit()
            return root_id, edit_id, upscale_id

        root_id, edit_id, upscale_id = asyncio.run(seed())

        root = client.get(f"/api/v1/generations/{root_id}/lineage").json()
        assert root["ancestors"] == []
        assert [entry["job_id"] for entry in root["children"]] == [str(edit_id)]
        assert root["children"][0]["action"] == "image_to_image"

        edit = client.get(f"/api/v1/generations/{edit_id}/lineage").json()
        assert [entry["job_id"] for entry in edit["ancestors"]] == [str(root_id)]
        assert edit["ancestors"][0]["action"] == "generate"
        assert [entry["job_id"] for entry in edit["children"]] == [str(upscale_id)]
        assert edit["children"][0]["action"] == "upscale"

        upscale = client.get(f"/api/v1/generations/{upscale_id}/lineage").json()
        assert [entry["job_id"] for entry in upscale["ancestors"]] == [
            str(root_id),
            str(edit_id),
        ]
        assert [entry["action"] for entry in upscale["ancestors"]] == [
            "generate",
            "image_to_image",
        ]
        assert upscale["children"] == []


@pytest.mark.db
def test_generation_lineage_fanout_orders_children_by_created_at():
    with TestClient(app) as client:
        async def seed() -> tuple[uuid.UUID, list[uuid.UUID]]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            now = datetime.now(timezone.utc)
            async with db.session_factory() as session:
                root_id, root_asset_id = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="lineage-fanout-root",
                    capabilities=["text_to_image"],
                    prompt="base",
                    created_at=now,
                )
                children = []
                for offset, prompt in enumerate(("third prompt", "first prompt", "second prompt")):
                    child_id, _ = await _seed_lineage_generation(
                        session,
                        user_id=db.local_user_id,
                        model_id="lineage-fanout-edit",
                        capabilities=["image_to_image"],
                        prompt=prompt,
                        created_at=now + timedelta(seconds=(3, 1, 2)[offset]),
                        source_asset_id=root_asset_id,
                    )
                    children.append(child_id)
                await session.commit()
            return root_id, [children[1], children[2], children[0]]

        root_id, expected = asyncio.run(seed())
        lineage = client.get(f"/api/v1/generations/{root_id}/lineage").json()
        assert [entry["job_id"] for entry in lineage["children"]] == [
            str(job_id) for job_id in expected
        ]
        assert lineage["descendant_count"] == 3


@pytest.mark.db
def test_generation_lineage_root_counts_grandchildren():
    with TestClient(app) as client:
        async def seed() -> uuid.UUID:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            now = datetime.now(timezone.utc)
            async with db.session_factory() as session:
                root_id, root_asset_id = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="lineage-count-root",
                    capabilities=["text_to_image"],
                    prompt="root",
                    created_at=now,
                )
                _, child_asset_id = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="lineage-count-edit",
                    capabilities=["image_to_image"],
                    prompt="child",
                    created_at=now + timedelta(seconds=1),
                    source_asset_id=root_asset_id,
                )
                await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="lineage-count-upscale",
                    capabilities=["upscale"],
                    prompt="grandchild",
                    created_at=now + timedelta(seconds=2),
                    source_asset_id=child_asset_id,
                )
                await session.commit()
            return root_id

        root_id = asyncio.run(seed())
        lineage = client.get(f"/api/v1/generations/{root_id}/lineage").json()
        assert lineage["ancestors"] == []
        assert len(lineage["children"]) == 1
        assert lineage["descendant_count"] == 2


@pytest.mark.db
def test_generation_lineage_includes_upload_root():
    with TestClient(app) as client:
        async def seed() -> tuple[uuid.UUID, uuid.UUID]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            now = datetime.now(timezone.utc)
            upload_id = uuid.uuid4()
            async with db.session_factory() as session:
                session.add(Asset(
                    id=upload_id,
                    user_id=db.local_user_id,
                    job_id=None,
                    parent_asset_id=None,
                    storage_key=f"{db.local_user_id}/upload.png",
                    mime="image/png",
                    width=512,
                    height=512,
                ))
                await session.flush()
                session.add(Asset(
                    user_id=db.local_user_id,
                    job_id=None,
                    parent_asset_id=upload_id,
                    storage_key=f"{db.local_user_id}/upload-thumb.webp",
                    mime="image/webp",
                    width=384,
                    height=384,
                ))
                edit_id, edit_asset_id = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="lineage-upload-edit",
                    capabilities=["image_to_image"],
                    prompt="edit upload",
                    created_at=now,
                    source_asset_id=upload_id,
                )
                leaf_id, _ = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="lineage-upload-upscale",
                    capabilities=["upscale"],
                    prompt="upscale edit",
                    created_at=now + timedelta(seconds=1),
                    source_asset_id=edit_asset_id,
                )
                await session.commit()
            return leaf_id, edit_id

        leaf_id, edit_id = asyncio.run(seed())
        lineage = client.get(f"/api/v1/generations/{leaf_id}/lineage").json()
        assert [entry["job_id"] for entry in lineage["ancestors"]] == [None, str(edit_id)]
        upload = lineage["ancestors"][0]
        assert upload["action"] == "upload"
        assert upload["model_id"] is None
        assert upload["state"] is None
        assert upload["created_at"] is not None
        assert upload["thumbnail_url"] is not None


@pytest.mark.db
def test_generation_lineage_keeps_missing_middle_ancestor():
    with TestClient(app) as client:
        async def seed() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            now = datetime.now(timezone.utc)
            async with db.session_factory() as session:
                root_id, root_asset_id = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="lineage-missing-root",
                    capabilities=["text_to_image"],
                    prompt="root",
                    created_at=now,
                )
                middle_id, middle_asset_id = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="lineage-missing-edit",
                    capabilities=["image_to_image"],
                    prompt="missing",
                    created_at=now + timedelta(seconds=1),
                    source_asset_id=root_asset_id,
                    expires_at=now - timedelta(seconds=1),
                )
                leaf_id, _ = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="lineage-missing-upscale",
                    capabilities=["upscale"],
                    prompt="leaf",
                    created_at=now + timedelta(seconds=2),
                    source_asset_id=middle_asset_id,
                )
                await session.commit()
            return leaf_id, root_id, middle_id

        leaf_id, root_id, middle_id = asyncio.run(seed())
        lineage = client.get(f"/api/v1/generations/{leaf_id}/lineage").json()
        assert [entry["job_id"] for entry in lineage["ancestors"]] == [
            str(root_id),
            str(middle_id),
        ]
        assert lineage["ancestors"][0]["missing"] is False
        assert lineage["ancestors"][1]["missing"] is True
        assert lineage["ancestors"][1]["thumbnail_url"] is None


@pytest.mark.db
def test_generation_lineage_foreign_job_is_not_found():
    with TestClient(app) as client:
        async def seed() -> uuid.UUID:
            assert db.session_factory is not None
            foreign_user_id = uuid.uuid4()
            model_id = f"lineage-foreign-{uuid.uuid4()}"
            job_id = uuid.uuid4()
            async with db.session_factory() as session:
                session.add(User(
                    id=foreign_user_id,
                    email=f"{foreign_user_id}@example.com",
                    role="user",
                ))
                session.add(Model(
                    id=model_id,
                    name=model_id,
                    capabilities=["text_to_image"],
                    parameters_schema={},
                    min_vram_gb=0,
                ))
                await session.flush()
                session.add(Job(
                    id=job_id,
                    user_id=foreign_user_id,
                    model_id=model_id,
                    params={"prompt": "foreign"},
                    state="succeeded",
                    attempt=1,
                ))
                await session.commit()
            return job_id

        job_id = asyncio.run(seed())
        response = client.get(f"/api/v1/generations/{job_id}/lineage")
        assert response.status_code == 404
        assert response.json() == {"detail": "no such generation"}


@pytest.mark.db
def test_generation_subtree_bounds_depth_and_reports_truncation(monkeypatch):
    monkeypatch.setattr(jobs, "LINEAGE_SUBTREE_MAX_DEPTH", 2)
    with TestClient(app) as client:
        async def seed() -> tuple[uuid.UUID, list[uuid.UUID]]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            now = datetime.now(timezone.utc)
            async with db.session_factory() as session:
                root_id, source_id = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="subtree-depth-root",
                    capabilities=["text_to_image"],
                    prompt="root",
                    created_at=now,
                )
                ids = [root_id]
                for depth in range(1, 4):
                    child_id, source_id = await _seed_lineage_generation(
                        session,
                        user_id=db.local_user_id,
                        model_id="subtree-depth-edit",
                        capabilities=["image_to_image"],
                        prompt=f"depth {depth}",
                        created_at=now + timedelta(seconds=depth),
                        source_asset_id=source_id,
                    )
                    ids.append(child_id)
                await session.commit()
            return root_id, ids

        root_id, ids = asyncio.run(seed())
        response = client.get(f"/api/v1/generations/{root_id}/subtree")
        assert response.status_code == 200
        body = response.json()
        assert [node["generation"]["id"] for node in body["nodes"]] == [
            str(job_id) for job_id in ids[:3]
        ]
        assert body["max_depth"] == 2
        assert body["truncated"] is True
        assert body["remaining_count_lower_bound"] >= 1


@pytest.mark.db
def test_generation_subtree_caps_nodes_and_reports_truncation(monkeypatch):
    monkeypatch.setattr(jobs, "LINEAGE_SUBTREE_MAX_NODES", 3)
    with TestClient(app) as client:
        async def seed() -> uuid.UUID:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            now = datetime.now(timezone.utc)
            async with db.session_factory() as session:
                root_id, root_asset_id = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="subtree-cap-root",
                    capabilities=["text_to_image"],
                    prompt="root",
                    created_at=now,
                )
                for offset in range(4):
                    await _seed_lineage_generation(
                        session,
                        user_id=db.local_user_id,
                        model_id="subtree-cap-edit",
                        capabilities=["image_to_image"],
                        prompt=f"child {offset}",
                        created_at=now + timedelta(seconds=offset + 1),
                        source_asset_id=root_asset_id,
                    )
                await session.commit()
            return root_id

        root_id = asyncio.run(seed())
        response = client.get(f"/api/v1/generations/{root_id}/subtree")
        assert response.status_code == 200
        body = response.json()
        assert len(body["nodes"]) == 3
        assert body["max_nodes"] == 3
        assert body["truncated"] is True
        assert body["remaining_count_lower_bound"] >= 1


@pytest.mark.db
def test_generation_subtree_is_owned_and_excludes_thumbnail_edges():
    with TestClient(app) as client:
        async def seed() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            now = datetime.now(timezone.utc)
            foreign_user_id = uuid.uuid4()
            async with db.session_factory() as session:
                session.add(User(
                    id=foreign_user_id,
                    email=f"{foreign_user_id}@example.com",
                    role="user",
                ))
                root_id, root_asset_id = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="subtree-thumb-root",
                    capabilities=["text_to_image"],
                    prompt="root",
                    created_at=now,
                )
                thumbnail_id = await session.scalar(
                    select(Asset.id).where(
                        Asset.job_id == root_id,
                        Asset.storage_key.like("%-thumb.webp"),
                    )
                )
                assert thumbnail_id is not None
                child_id, _ = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="subtree-thumb-edit",
                    capabilities=["image_to_image"],
                    prompt="thumbnail child",
                    created_at=now + timedelta(seconds=1),
                    source_asset_id=thumbnail_id,
                )
                foreign_id, _ = await _seed_lineage_generation(
                    session,
                    user_id=foreign_user_id,
                    model_id="subtree-foreign-root",
                    capabilities=["text_to_image"],
                    prompt="foreign",
                    created_at=now,
                )
                await session.commit()
            return root_id, child_id, foreign_id

        root_id, child_id, foreign_id = asyncio.run(seed())
        subtree = client.get(f"/api/v1/generations/{root_id}/subtree")
        assert subtree.status_code == 200
        returned = [node["generation"]["id"] for node in subtree.json()["nodes"]]
        assert returned == [str(root_id)]
        assert str(child_id) not in returned
        assert client.get(f"/api/v1/generations/{foreign_id}/subtree").status_code == 404


@pytest.mark.db
def test_generation_subtree_and_descendant_count_are_cycle_safe():
    with TestClient(app) as client:
        async def seed() -> tuple[uuid.UUID, uuid.UUID]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            now = datetime.now(timezone.utc)
            async with db.session_factory() as session:
                root_id, root_asset_id = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="subtree-cycle-root",
                    capabilities=["text_to_image"],
                    prompt="root",
                    created_at=now,
                )
                child_id, child_asset_id = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="subtree-cycle-edit",
                    capabilities=["image_to_image"],
                    prompt="child",
                    created_at=now + timedelta(seconds=1),
                    source_asset_id=root_asset_id,
                )
                root = await session.get(Job, root_id)
                assert root is not None
                root.source_asset_id = child_asset_id
                await session.commit()
            return root_id, child_id

        root_id, child_id = asyncio.run(seed())
        subtree = client.get(f"/api/v1/generations/{root_id}/subtree")
        assert subtree.status_code == 200
        assert [node["generation"]["id"] for node in subtree.json()["nodes"]] == [
            str(root_id),
            str(child_id),
        ]
        assert subtree.json()["truncated"] is False

        lineage = client.get(f"/api/v1/generations/{root_id}/lineage")
        assert lineage.status_code == 200
        assert lineage.json()["descendant_count"] == 1
        assert lineage.json()["descendants_truncated"] is False


@pytest.mark.db
def test_generation_lineage_descendant_depth_is_bounded(monkeypatch):
    monkeypatch.setattr(jobs, "LINEAGE_SUBTREE_MAX_DEPTH", 1)
    with TestClient(app) as client:
        async def seed() -> uuid.UUID:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            now = datetime.now(timezone.utc)
            async with db.session_factory() as session:
                root_id, root_asset_id = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="lineage-depth-root",
                    capabilities=["text_to_image"],
                    prompt="root",
                    created_at=now,
                )
                _, child_asset_id = await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="lineage-depth-edit",
                    capabilities=["image_to_image"],
                    prompt="child",
                    created_at=now + timedelta(seconds=1),
                    source_asset_id=root_asset_id,
                )
                await _seed_lineage_generation(
                    session,
                    user_id=db.local_user_id,
                    model_id="lineage-depth-upscale",
                    capabilities=["upscale"],
                    prompt="grandchild",
                    created_at=now + timedelta(seconds=2),
                    source_asset_id=child_asset_id,
                )
                await session.commit()
            return root_id

        root_id = asyncio.run(seed())
        lineage = client.get(f"/api/v1/generations/{root_id}/lineage")
        assert lineage.status_code == 200
        assert lineage.json()["descendant_count"] == 1
        assert lineage.json()["descendants_truncated"] is True


@pytest.mark.db
def test_generation_serializer_never_signs_foreign_assets():
    with TestClient(app) as client:
        async def seed() -> uuid.UUID:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            foreign_user_id = uuid.uuid4()
            model_id = f"serializer-owner-{uuid.uuid4()}"
            job_id = uuid.uuid4()
            foreign_asset_id = uuid.uuid4()
            async with db.session_factory() as session:
                session.add(User(
                    id=foreign_user_id,
                    email=f"{foreign_user_id}@example.com",
                    role="user",
                ))
                session.add(Model(
                    id=model_id,
                    name=model_id,
                    capabilities=["text_to_image"],
                    parameters_schema={},
                    min_vram_gb=0,
                ))
                await session.flush()
                session.add(Job(
                    id=job_id,
                    user_id=db.local_user_id,
                    model_id=model_id,
                    params={"prompt": "local job"},
                    state="succeeded",
                    attempt=1,
                ))
                await session.flush()
                session.add(Asset(
                    id=foreign_asset_id,
                    user_id=foreign_user_id,
                    job_id=job_id,
                    storage_key=f"{foreign_user_id}/foreign.png",
                    mime="image/png",
                    width=512,
                    height=512,
                ))
                await session.flush()
                session.add(Job(
                    user_id=foreign_user_id,
                    model_id=model_id,
                    params={"prompt": "foreign child"},
                    state="succeeded",
                    attempt=1,
                    source_asset_id=foreign_asset_id,
                ))
                await session.commit()
            return job_id

        job_id = asyncio.run(seed())
        response = client.get(f"/api/v1/generations/{job_id}")
        assert response.status_code == 200
        assert response.json()["assets"] == []
        assert response.json()["has_derivatives"] is False
        assert "foreign.png" not in response.text


@pytest.mark.db
def test_generation_cursor_anchor_must_match_every_filter():
    states = ("queued", "running", "succeeded", "failed")
    with TestClient(app) as client:
        async def seed() -> dict[tuple[str, bool, bool], uuid.UUID]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            now = datetime.now(timezone.utc)
            model_id = f"cursor-matrix-{uuid.uuid4()}"
            source_id = uuid.uuid4()
            ids = {}
            async with db.session_factory() as session:
                session.add(Model(
                    id=model_id,
                    name=model_id,
                    capabilities=["image_to_image"],
                    parameters_schema={},
                    min_vram_gb=0,
                ))
                session.add(Asset(
                    id=source_id,
                    user_id=db.local_user_id,
                    job_id=None,
                    storage_key=f"{db.local_user_id}/cursor-source.png",
                    mime="image/png",
                    width=512,
                    height=512,
                ))
                await session.flush()
                offset = 0
                for state in states:
                    for starred in (False, True):
                        for roots_only in (False, True):
                            job_id = uuid.uuid4()
                            session.add(Job(
                                id=job_id,
                                user_id=db.local_user_id,
                                model_id=model_id,
                                params={},
                                state=state,
                                attempt=1,
                                source_asset_id=None if roots_only else source_id,
                                starred_at=now if starred else None,
                                created_at=now + timedelta(seconds=offset),
                            ))
                            ids[(state, starred, roots_only)] = job_id
                            offset += 1
                await session.commit()
            return ids

        ids = asyncio.run(seed())
        for state in states:
            for starred in (False, True):
                for roots_only in (False, True):
                    params = {
                        "state": state,
                        "starred": str(starred).lower(),
                        "roots_only": str(roots_only).lower(),
                    }
                    matching = client.get(
                        "/api/v1/generations",
                        params={**params, "cursor": str(ids[(state, starred, roots_only)])},
                    )
                    assert matching.status_code == 200
                    mismatches = (
                        ids[("failed" if state != "failed" else "succeeded", starred, roots_only)],
                        ids[(state, not starred, roots_only)],
                        ids[(state, starred, not roots_only)],
                    )
                    for cursor in mismatches:
                        response = client.get(
                            "/api/v1/generations",
                            params={**params, "cursor": str(cursor)},
                        )
                        assert response.status_code == 404


@pytest.mark.db
def test_thumbnail_source_is_rejected_and_not_counted_as_derivative():
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-thumbnail-source")

            async def seed() -> tuple[uuid.UUID, uuid.UUID]:
                assert db.local_user_id is not None
                assert db.session_factory is not None
                now = datetime.now(timezone.utc)
                async with db.session_factory() as session:
                    root_id, _ = await _seed_lineage_generation(
                        session,
                        user_id=db.local_user_id,
                        model_id="sd-test",
                        capabilities=["text_to_image", "image_to_image"],
                        prompt="root",
                        created_at=now,
                    )
                    thumbnail_id = await session.scalar(
                        select(Asset.id).where(
                            Asset.job_id == root_id,
                            Asset.storage_key.like("%-thumb.webp"),
                        )
                    )
                    assert thumbnail_id is not None
                    await _seed_lineage_generation(
                        session,
                        user_id=db.local_user_id,
                        model_id="sd-test",
                        capabilities=["text_to_image", "image_to_image"],
                        prompt="corrupt child",
                        created_at=now + timedelta(seconds=1),
                        source_asset_id=thumbnail_id,
                    )
                    await session.commit()
                return root_id, thumbnail_id

            root_id, thumbnail_id = asyncio.run(seed())
            detail = client.get(f"/api/v1/generations/{root_id}")
            assert detail.status_code == 200
            assert detail.json()["has_derivatives"] is False

            response = client.post(
                "/api/v1/generations",
                json={
                    "model_id": "sd-test",
                    "params": {"prompt": "thumbnail source"},
                    "source_asset_id": str(thumbnail_id),
                },
            )
            assert response.status_code == 422
            assert response.json()["detail"] == "source asset cannot be a thumbnail"

def test_non_finite_progress_is_ignored():
    # A stored NaN breaks every generation response that carries it, and
    # publish() would emit the non-standard NaN token into SSE (issue #203).
    worker = realtime.Worker(id="w-nan", ws=None, manifests=[], realtime_slots=1)
    job_id = uuid.uuid4()
    jobs.inflight[job_id] = jobs.InFlight(
        worker=worker, storage_key="k", thumb_storage_key="t", user_id=uuid.uuid4(),
    )
    try:
        for unusable in (float("nan"), float("inf"), 10 ** 400, [1], {"a": 1}, None):
            asyncio.run(jobs.on_worker_message(worker, {
                "type": "job_progress", "job_id": str(job_id), "progress": unusable,
            }))
            assert job_id not in jobs.live_progress, f"{unusable!r} was stored"
        asyncio.run(jobs.on_worker_message(worker, {
            "type": "job_progress", "job_id": str(job_id), "progress": 0.5,
        }))
        assert jobs.live_progress[job_id] == 0.5
    finally:
        jobs.inflight.pop(job_id, None)
        jobs.live_progress.pop(job_id, None)
        jobs.last_progress_at.pop(job_id, None)


def test_job_done_survives_unusable_worker_numbers():
    # json.loads admits NaN and Infinity, and a bare int() raises three ways on
    # what a worker can send: ValueError, OverflowError, TypeError. Only the
    # first was in the fleet handler's except tuple (issue #203).
    from app.jobs import _worker_int

    assert _worker_int(1500) == 1500
    assert _worker_int(1500.7) == 1500
    for unusable in (float("nan"), float("inf"), float("-inf"), None, "12", True, {}):
        assert _worker_int(unusable) == 0
    # json.loads yields arbitrary-precision ints from ordinary JSON, and
    # math.isfinite() raises OverflowError on one. The columns are int4, so a
    # value merely past that range fails the insert instead.
    assert _worker_int(10 ** 400) == 0
    assert _worker_int(3_000_000_000) == 0
    assert _worker_int(2 ** 31 - 1) == 2 ** 31 - 1
    assert _worker_int(None, default=7) == 7


def test_job_done_with_unusable_numbers_leaves_the_job_recoverable():
    """Drive the real control path, not just the helper.

    on_worker_message de-tracks the job before converting these fields, so a
    raise here escapes the fleet handler's except tuple and strands the row in
    `running`: on_worker_lost and sweep_stalled_jobs both only walk inflight.
    Unit tests on the helper missed exactly this (issue #203).
    """
    worker = realtime.Worker(id="w-huge", ws=None, manifests=[], realtime_slots=1)
    for unusable in (10 ** 400, 3_000_000_000, float("inf"), None, [1]):
        job_id = uuid.uuid4()
        jobs.inflight[job_id] = jobs.InFlight(
            worker=worker, storage_key="k", thumb_storage_key="t", user_id=uuid.uuid4(),
        )
        try:
            asyncio.run(jobs.on_worker_message(worker, {
                "type": "job_done", "job_id": str(job_id), "gpu_ms": unusable,
                "width": unusable, "height": unusable,
            }))
        except Exception as error:  # noqa: BLE001 - the point is that none escape
            raise AssertionError(f"gpu_ms={unusable!r} escaped as {type(error).__name__}")
        finally:
            jobs.inflight.pop(job_id, None)


def test_generation_params_must_be_json_storable():
    # jobs.params is JSONB, which has no NaN or Infinity; the shipped schemas
    # do not set additionalProperties: false, so an unconstrained property
    # carried one straight to the insert (issue #232).
    from app.manifests import json_finite

    assert json_finite({"prompt": "x", "steps": 4})
    assert not json_finite({"prompt": "x", "extra": float("inf")})
    assert not json_finite({"nested": [{"deep": float("nan")}]})


def test_peer_uuid_rejects_a_non_string_id():
    # uuid.UUID raises AttributeError on an int and TypeError on null. Widening
    # the fleet handler's except tuple to catch those would relabel an internal
    # bug as a protocol violation, so the parse is guarded instead (issue #232).
    assert realtime.peer_uuid(str(uuid.uuid4()))
    for bad in (5, None, [1], {"a": 1}):
        try:
            realtime.peer_uuid(bad)
        except realtime.ProtocolError:
            continue
        raise AssertionError(f"{bad!r} was accepted as an id")
