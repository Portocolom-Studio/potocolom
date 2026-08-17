"""The job dispatch and history flow (#16), driven with a fake worker over the
real fleet WebSocket. Real inference is the worker's side (worker/tests)."""

import asyncio
import base64
import struct
import threading
import time
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import delete, func, select
from fastapi.testclient import TestClient

from app import db, jobs, realtime, registry
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

# Studio-narrowed but still admitting a prompt-only job: the narrowing
# (capabilities intersect studio_capabilities) must keep text_to_image, or
# the capability gate refuses before the persist path is reached. The model
# row the persist writes must carry the unnarrowed list.
MANIFEST_NARROWED_WITH_T2I = {
    **MANIFEST,
    "id": "sd-narrow",
    "capabilities": ["text_to_image", "image_to_image", "realtime"],
    "studio_capabilities": ["text_to_image", "realtime"],
}

HOSTILE_PROMPT = 'A "lighthouse"\n; ../../ caf\u00e9'


# A real lossless 320x200 WebP, encoded with
# `ffmpeg -f lavfi -i color=c=red:s=320x200 -lossless 1` and embedded as
# base64 so the module stays ASCII: CI has no ffmpeg, and a hand-built header
# is a replica of the parser, so it proves nothing about real files.
_WEBP_BYTES = base64.b64decode(
    "UklGRiQAAABXRUJQVlA4TBcAAAAvP8ExAAcQ9Y/+BwAU6f9/iuh/6v+fAQA="
)

# The same red canvas at 400x300: past THUMBNAIL_MAX_EDGE on its longest edge.
_WEBP_BIG_BYTES = base64.b64decode(
    "UklGRiYAAABXRUJQVlA4TBoAAAAvj8FKAAcQ9Y/+BwQkSf//kxH9z/jPf/6fKQ=="
)


def webp_bytes():
    return _WEBP_BYTES


def dispatch_for(worker, job_id, limit=5):
    """The dispatch for this job, skipping any the suite left requeued.

    A job another test left running is requeued when its socket closes, so a
    fresh socket can be handed that dispatch before this test's own.
    """
    for _ in range(limit):
        message = worker.receive_json()
        if message.get("type") == "dispatch_job" and message["job_id"] == job_id:
            return message
    raise AssertionError(f"no dispatch for job {job_id}")


def put_upload(client, spec, content):
    """PUT the way a worker does: the dispatch headers carry the upload token."""
    return client.put(urlsplit(spec["url"]).path, content=content,
                      headers=spec.get("headers") or {})


def png_bytes(width=512, height=512):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\0" + b"\0" * (width * 3) for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


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


def fleet_hello(ws, worker_id, manifest=MANIFEST, version=PROTOCOL_VERSION):
    ws.send_json({"type": "hello", "protocol_version": version,
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
            assert dispatch["upload"]["url"].endswith(f"/{job_id}-attempt-1.png")
            assert dispatch["upload"]["headers"]["Content-Type"] == "image/png"
            # The token authorises the PUT: the key alone is derivable by any
            # worker that ever held this job (issue #247).
            token = dispatch["upload"]["headers"]["X-Upload-Token"]
            assert token and dispatch["thumb_upload"]["headers"]["X-Upload-Token"] == token
            assert dispatch["dispatch_token"] == token
            assert dispatch["thumb_upload"]["url"].endswith(
                f"/{job_id}-attempt-1-thumb.webp"
            )
            assert dispatch["thumb_upload"]["headers"]["Content-Type"] == "image/webp"

            assert put_upload(client, dispatch["upload"],
                              png_bytes(320, 240)).status_code == 200
            assert put_upload(client, dispatch["thumb_upload"],
                              webp_bytes()).status_code == 200

            worker.send_json({"type": "job_progress", "job_id": job_id,
                              "progress": 0.5,
                              "dispatch_token": dispatch["dispatch_token"]})
            worker.send_json({"type": "job_done", "job_id": job_id,
                              "gpu_ms": 1234, "width": 512, "height": 512,
                              "has_thumbnail": True,
                              "dispatch_token": dispatch["dispatch_token"]})

            job = poll_until(client, job_id, "succeeded")
            assert job["gpu_ms"] == 1234
            asset = job["assets"][0]
            assert asset["width"] == 320
            assert asset["height"] == 240
            assert asset["mime"] == "image/png"
            assert asset["url"].endswith(f"/{job_id}-attempt-1.png")
            created_stamp = datetime.fromisoformat(job["created_at"]).strftime("%Y%m%d-%H%M%S")
            expected_name = f"potocolom-{created_stamp}-a-lighthouse-cafe.png"
            download_url = urlsplit(asset["download_url"])
            assert parse_qs(download_url.query) == {"download": [expected_name]}
            download_response = client.get(f"{download_url.path}?{download_url.query}")
            assert download_response.content == png_bytes(320, 240)
            assert download_response.headers["content-disposition"] == (
                f'attachment; filename="{expected_name}"'
            )
            assert asset["thumbnail_url"] is not None
            assert client.get(urlsplit(asset["url"]).path).content == png_bytes(320, 240)
            assert client.get(urlsplit(asset["thumbnail_url"]).path).content == webp_bytes()

            async def recorded_thumb_dimensions() -> tuple[int, int]:
                assert db.session_factory is not None
                async with db.session_factory() as session:
                    row = await session.scalar(
                        select(Asset).where(Asset.parent_asset_id == uuid.UUID(asset["id"]))
                    )
                    assert row is not None
                    return row.width, row.height

            # The thumbnail row carries the inspected dimensions, not a
            # derivation from the master's: the object is the truth.
            assert asyncio.run(recorded_thumb_dimensions()) == (320, 200)

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
def test_generation_persists_the_full_capability_list_for_a_narrowed_model(
        monkeypatch):
    # The models row records what the model can do, not what the studio
    # chooses to offer (usage_events and job-history classification read it),
    # so a prompt-only POST for a studio-narrowed model must persist the
    # unnarrowed manifest. Reverting the persist lookup to
    # persist_manifests([manifest]) leaves the rest of the suite
    # byte-identical, so the row is read back here to pin the behaviour.
    from app.settings import get_settings

    # get_settings is a global cache, and the benchmark-mode test above this
    # one cached benchmark_api=True; re-read from the restored environment
    # so this test runs narrowed regardless of neighbour order.
    monkeypatch.delenv("BENCHMARK_API", raising=False)
    get_settings.cache_clear()
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-narrow", manifest=MANIFEST_NARROWED_WITH_T2I)
            # The premise: queued generations see the narrowed list.
            assert registry.for_jobs()["sd-narrow"].capabilities == [
                "text_to_image", "realtime",
            ]

            async def drop_model_row() -> None:
                assert db.session_factory is not None
                async with db.session_factory() as session:
                    await session.execute(delete(Model).where(Model.id == "sd-narrow"))
                    await session.commit()

            # The row may be missing when the worker registered while the
            # database was down; drop it so this POST is the write under test.
            asyncio.run(drop_model_row())
            created = client.post("/api/v1/generations",
                                  json={"model_id": "sd-narrow",
                                        "params": {"prompt": "x"}})
            assert created.status_code == 202

            async def row_capabilities():
                assert db.session_factory is not None
                async with db.session_factory() as session:
                    row = await session.get(Model, "sd-narrow")
                    return list(row.capabilities) if row is not None else None

            capabilities = asyncio.run(row_capabilities())
    assert capabilities == ["text_to_image", "image_to_image", "realtime"]


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
            worker.send_json({"type": "job_failed", "job_id": job_id, "reason": "boom",
                              "dispatch_token": redispatch["dispatch_token"]})
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


def disarm_the_sweeper(monkeypatch) -> None:
    """Stop the stall sweeper once a test has the retry it was waiting for.

    These tests set JOB_STALL_SECONDS to 0.05 to force one requeue, and then
    keep asserting against the attempt it produced. The sweeper is still armed
    while they do, so on a loaded machine it fires again, fails the row past
    its one retry, and the assertions see a 403 on an upload key that was just
    issued: the #280 symptom, from load rather than from a second run.
    """
    from app.settings import get_settings

    monkeypatch.setenv("JOB_STALL_SECONDS", "600")
    get_settings.cache_clear()


def poll_until_attempt(client, job_id, attempt: int, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/generations/{job_id}").json()
        if job.get("attempt") == attempt:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached attempt {attempt}")


def _wait_for_slots(worker, expected: int, timeout=5.0):
    """The slot release follows the committed row in the handler, so an API
    poll can observe the row before the slot moves; wait for the count."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and worker.jobs_in_flight != expected:
        time.sleep(0.02)
    assert worker.jobs_in_flight == expected


def _dirty_for_job(session, job_id) -> bool:
    """Whether this session is mid-transaction for the job. Scopes a test
    seam to the terminal transaction: every other writer touches other rows."""
    return any(isinstance(obj, Job) and obj.id == job_id for obj in session.dirty)


class _FailingCommitSession:
    """Wraps a session so the commit of this job's terminal transaction raises
    once, as a lock timeout or flush error would; later commits pass through,
    which is what lets the recovery path run."""

    def __init__(self, real, owner):
        self._real = real
        self._owner = owner

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def __aenter__(self):
        await self._real.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return await self._real.__aexit__(exc_type, exc, tb)

    async def _fail_once(self):
        if not self._owner.fired and _dirty_for_job(self._real, self._owner.job_id):
            self._owner.fired = True
            self._owner.failure.set()
            raise RuntimeError("simulated commit failure")

    async def flush(self):
        # The success path flushes before it commits, so the terminal write is
        # pending at either call; fail whichever comes first.
        await self._fail_once()
        await self._real.flush()

    async def commit(self):
        await self._fail_once()
        await self._real.commit()


class _FailingCommitFactory:
    def __init__(self, real, job_id, failure):
        self._real = real
        self.job_id = job_id
        self.failure = failure
        self.fired = False

    def __call__(self):
        return _FailingCommitSession(self._real(), self)


class _SweepingSession:
    """Wraps a session so the commit of this job's terminal transaction first
    does what the stall sweeper would: pop the entry and release the slot,
    then let the verdict's commit complete."""

    def __init__(self, real, owner):
        self._real = real
        self._owner = owner

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def __aenter__(self):
        await self._real.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return await self._real.__aexit__(exc_type, exc, tb)

    async def _sweep_once(self):
        if not self._owner.swept and _dirty_for_job(self._real, self._owner.job_id):
            self._owner.swept = True
            jobs.inflight.pop(self._owner.job_id, None)
            jobs.live_progress.pop(self._owner.job_id, None)
            jobs.last_progress_at.pop(self._owner.job_id, None)
            jobs.release_job_slot(self._owner.worker)
            self._owner.swept_event.set()

    async def flush(self):
        # The success path flushes before it commits, so the race lands at
        # whichever call carries the pending terminal write.
        await self._sweep_once()
        await self._real.flush()

    async def commit(self):
        await self._sweep_once()
        await self._real.commit()


class _SweepingFactory:
    def __init__(self, real, job_id, worker, swept_event):
        self._real = real
        self.job_id = job_id
        self.worker = worker
        self.swept_event = swept_event
        self.swept = False

    def __call__(self):
        return _SweepingSession(self._real(), self)


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
                first = worker.receive_json()
                assert first["job_id"] == job_id
                # Worker stays connected but sends no progress; stall requeues once
                # and the retry is dispatched back to the same connected worker.
                poll_until_attempt(client, job_id, 2, timeout=3.0)
                disarm_the_sweeper(monkeypatch)
                redispatch = worker.receive_json()
                assert redispatch["type"] == "dispatch_job"
                assert redispatch["job_id"] == job_id
                # A requeue that reused the token would let attempt one publish
                # to attempt two's keys, so the token must be fresh per
                # dispatch (issue #247).
                assert redispatch["dispatch_token"] != first["dispatch_token"]
    finally:
        monkeypatch.delenv("JOB_STALL_SECONDS", raising=False)
        get_settings.cache_clear()


@pytest.mark.db
def test_retried_attempt_uses_a_new_upload_key_and_rejects_the_old_key(monkeypatch):
    monkeypatch.setenv("JOB_STALL_SECONDS", "0.05")
    from app.settings import get_settings
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/fleet") as worker:
                fleet_hello(worker, "w-attempt-keys")
                job_id = client.post(
                    "/api/v1/generations",
                    json={"model_id": "sd-test", "params": {"prompt": "keys"}},
                ).json()["job_id"]
                first = worker.receive_json()
                first_path = urlsplit(first["upload"]["url"]).path

                poll_until_attempt(client, job_id, 2, timeout=3.0)
                disarm_the_sweeper(monkeypatch)
                second = worker.receive_json()
                second_path = urlsplit(second["upload"]["url"]).path
                assert first_path != second_path
                assert put_upload(client, first["upload"], b"stale").status_code == 403
                assert put_upload(client, second["upload"],
                                  png_bytes()).status_code == 200
    finally:
        monkeypatch.delenv("JOB_STALL_SECONDS", raising=False)
        get_settings.cache_clear()


@pytest.mark.db
def test_a_retry_does_not_leave_the_earlier_attempt_behind(monkeypatch):
    """Per-attempt keys stop a stale worker overwriting the winner, and start
    leaking: the earlier attempt's blobs are no longer overwritten and nothing
    else collects them, because the asset row only ever names the winning key.
    """
    monkeypatch.setenv("JOB_STALL_SECONDS", "0.05")
    from app.settings import get_settings
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/fleet") as worker:
                fleet_hello(worker, "w-attempt-orphans")
                job_id = client.post(
                    "/api/v1/generations",
                    json={"model_id": "sd-test", "params": {"prompt": "orphans"}},
                ).json()["job_id"]
                first = worker.receive_json()
                first_path = urlsplit(first["upload"]["url"]).path
                first_thumb = urlsplit(first["thumb_upload"]["url"]).path

                poll_until_attempt(client, job_id, 2, timeout=3.0)
                disarm_the_sweeper(monkeypatch)
                second = worker.receive_json()
                # The first attempt uploaded before it stalled.
                storage = jobs.get_storage()
                first_key = first_path.rsplit("/api/v1/files/", 1)[-1]
                first_thumb_key = first_thumb.rsplit("/api/v1/files/", 1)[-1]
                asyncio.run(_write_blob(storage, first_key, png_bytes()))
                asyncio.run(_write_blob(storage, first_thumb_key, png_bytes()))

                assert put_upload(client, second["upload"],
                                  png_bytes()).status_code == 200
                worker.send_json({"type": "job_done", "job_id": job_id, "gpu_ms": 1,
                                  "width": 512, "height": 512,
                                  "dispatch_token": second["dispatch_token"]})
                poll_until(client, job_id, "succeeded")

                assert asyncio.run(storage.image_info(first_key)) is None
                assert asyncio.run(storage.image_info(first_thumb_key)) is None
    finally:
        monkeypatch.delenv("JOB_STALL_SECONDS", raising=False)
        get_settings.cache_clear()


@pytest.mark.db
def test_a_reported_failure_does_not_leave_its_upload_behind():
    """job_failed never calls image_info, so nothing bounds or collects it.

    A worker can upload a master and a thumbnail and then report a failure. No
    asset row names those objects and the success path never runs, so the
    terminal failure path is their only collector.
    """
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-failed-upload")
            job_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "failed"}},
            ).json()["job_id"]
            dispatch = worker.receive_json()
            upload_path = urlsplit(dispatch["upload"]["url"]).path
            key = upload_path.rsplit("/api/v1/files/", 1)[-1]
            assert put_upload(client, dispatch["upload"], png_bytes()).status_code == 200
            storage = jobs.get_storage()
            assert storage.path(key).exists()

            worker.send_json({"type": "job_failed", "job_id": job_id,
                              "reason": "worker said no",
                              "dispatch_token": dispatch["dispatch_token"]})
            poll_until(client, job_id, "failed")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and storage.path(key).exists():
                time.sleep(0.05)
            assert not storage.path(key).exists(), "a reported failure kept its upload"


@pytest.mark.db
def test_a_late_verdict_does_not_fail_the_attempt_that_replaced_it(monkeypatch):
    """image_info awaits a thread, so the attempt can change under it.

    A stalled attempt one whose inspection finally returns invalid would
    otherwise pop attempt two, fail the authoritative row, and delete attempt
    two's objects. The replacement is simulated inside image_info rather than
    by timing, so the ordering is deterministic.
    """
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-late-verdict")
            job_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "late"}},
            ).json()["job_id"]
            dispatch = worker.receive_json()
            assert put_upload(client, dispatch["upload"],
                              png_bytes()).status_code == 200
            key = uuid.UUID(job_id)
            superseded = jobs.inflight[key]
            replacement = jobs.InFlight(
                worker=superseded.worker, storage_key="replacement.png",
                thumb_storage_key="replacement-thumb.webp",
                user_id=superseded.user_id, dispatch_token="replacement-token",
                attempt=2,
            )
            real_storage = jobs.get_storage()

            class Replacing:
                """Stands in for storage; swaps the entry mid-inspection."""

                def __getattr__(self, name):
                    return getattr(real_storage, name)

                async def image_info(self, storage_key):
                    jobs.inflight[key] = replacement
                    try:
                        return None  # the late verdict: attempt one was invalid
                    finally:
                        inspected.set()

            inspected = threading.Event()
            monkeypatch.setattr(jobs, "get_storage", lambda: Replacing())
            worker.send_json({"type": "job_done", "job_id": job_id, "gpu_ms": 1,
                              "width": 512, "height": 512,
                              "dispatch_token": dispatch["dispatch_token"]})

            # The stand-in gives a deterministic hook, so wait on the verdict
            # rather than polling a negative for two seconds on every run.
            assert inspected.wait(timeout=5), "the output was never inspected"
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and jobs.inflight.get(key) is replacement:
                time.sleep(0.02)
            assert jobs.inflight.get(key) is replacement, "the replacement was popped"
            state = client.get(f"/api/v1/generations/{job_id}").json()["state"]
            assert state != "failed", "a superseded attempt failed the live row"

            # Let the job finish so it does not sit running in the shared
            # database and starve the dispatch loop for every test after this.
            monkeypatch.undo()
            jobs.inflight[key] = superseded
            worker.send_json({"type": "job_done", "job_id": job_id, "gpu_ms": 1,
                              "width": 512, "height": 512,
                              "dispatch_token": dispatch["dispatch_token"]})
            poll_until(client, job_id, "succeeded")


@pytest.mark.db
def test_a_success_commit_failure_leaves_the_job_recoverable(monkeypatch):
    """The success transaction now precedes the in-memory de-tracking, so a
    commit failure must leave the row running and the entry tracked, and the
    stall sweeper must then requeue it (issue #248)."""
    _stall_safe(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-commit-fail")
            job_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "commit fail"}},
            ).json()["job_id"]
            dispatch = worker.receive_json()
            assert put_upload(client, dispatch["upload"],
                              png_bytes()).status_code == 200

            key = uuid.UUID(job_id)
            entry = jobs.inflight[key]
            real_factory = db.session_factory
            assert real_factory is not None
            failed = threading.Event()
            monkeypatch.setattr(
                db, "session_factory", _FailingCommitFactory(real_factory, key, failed)
            )
            worker.send_json({"type": "job_done", "job_id": job_id, "gpu_ms": 1,
                              "width": 512, "height": 512,
                              "dispatch_token": dispatch["dispatch_token"]})
            assert failed.wait(timeout=5), "the terminal commit never ran"

            assert client.get(f"/api/v1/generations/{job_id}").json()["state"] == "running"
            assert jobs.inflight.get(key) is entry
            assert realtime.workers["w-commit-fail"].jobs_in_flight == 1

            # Recovery: the entry is still tracked with a stale progress stamp,
            # so the sweeper requeues it and the retry reaches a terminal state.
            jobs.last_progress_at[key] = 0.0
            asyncio.run(jobs.sweep_stalled_jobs())
            poll_until_attempt(client, job_id, 2, timeout=3.0)
            redispatch = worker.receive_json()
            assert redispatch["type"] == "dispatch_job"
            assert redispatch["job_id"] == job_id
            worker.send_json({"type": "job_failed", "job_id": job_id,
                              "reason": "boom",
                              "dispatch_token": redispatch["dispatch_token"]})
            poll_until(client, job_id, "failed")


@pytest.mark.db
def test_a_failure_commit_failure_leaves_the_job_recoverable(monkeypatch):
    """The failure path keeps the same ordering: mark_failed's commit precedes
    the de-tracking, so a commit failure leaves the job recoverable."""
    _stall_safe(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-fail-commit")
            job_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "fail commit"}},
            ).json()["job_id"]
            dispatch = worker.receive_json()

            key = uuid.UUID(job_id)
            entry = jobs.inflight[key]
            real_factory = db.session_factory
            assert real_factory is not None
            failed = threading.Event()
            monkeypatch.setattr(
                db, "session_factory", _FailingCommitFactory(real_factory, key, failed)
            )
            worker.send_json({"type": "job_failed", "job_id": job_id,
                              "reason": "boom",
                              "dispatch_token": dispatch["dispatch_token"]})
            assert failed.wait(timeout=5), "the terminal commit never ran"

            assert client.get(f"/api/v1/generations/{job_id}").json()["state"] == "running"
            assert jobs.inflight.get(key) is entry
            assert realtime.workers["w-fail-commit"].jobs_in_flight == 1

            jobs.last_progress_at[key] = 0.0
            asyncio.run(jobs.sweep_stalled_jobs())
            poll_until_attempt(client, job_id, 2, timeout=3.0)
            redispatch = worker.receive_json()
            worker.send_json({"type": "job_failed", "job_id": job_id,
                              "reason": "boom again",
                              "dispatch_token": redispatch["dispatch_token"]})
            poll_until(client, job_id, "failed")


@pytest.mark.db
def test_a_failed_recovery_keeps_the_lost_job_retryable(monkeypatch):
    """A failed requeue_or_fail must leave the job in lost_jobs: the pop used
    to precede the await, so the raise destroyed the only reference to the row
    and nothing retried it (issue #248). The entry rotates to the tail rather
    than holding the head, which the second half of this test covers."""
    _stall_safe(monkeypatch)
    with TestClient(app) as client:
        async def seed() -> uuid.UUID:
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
                    params={"prompt": "lost job"},
                    state="running",
                    # Past the one retry, so the recovery fails the row and the
                    # test sees a terminal state rather than a requeue.
                    attempt=2,
                ))
                await session.commit()
            return job_id

        job_id = asyncio.run(seed())
        lost = [job_id]
        real_requeue_or_fail = jobs.requeue_or_fail
        real_dispatch_step = jobs.dispatch_step
        reasons = []

        async def flaky_requeue(job_id, reason):
            reasons.append(reason)
            if len(reasons) == 1:
                raise RuntimeError("simulated lock timeout")
            await real_requeue_or_fail(job_id, reason)

        async def parked():
            # The live dispatch loop drains lost_jobs on its own tick and
            # would race the assertions below; park it for the duration.
            return None

        monkeypatch.setattr(jobs, "dispatch_step", parked)
        monkeypatch.setattr(jobs, "lost_jobs", lost)
        monkeypatch.setattr(jobs, "requeue_or_fail", flaky_requeue)
        try:
            asyncio.run(real_dispatch_step())
        except RuntimeError:
            pass  # the first recovery failed; the job must stay at the head
        assert lost == [job_id], "a failed recovery dropped the only reference"

        asyncio.run(real_dispatch_step())
        assert lost == []
        assert reasons == ["worker disconnected", "worker disconnected"]
        assert client.get(f"/api/v1/generations/{job_id}").json()["state"] == "failed"


@pytest.mark.db
def test_a_failing_recovery_does_not_starve_the_jobs_behind_it(monkeypatch):
    """Holding the head would stop the sweep and the dispatch behind it for as
    long as one entry keeps raising, so a failure rotates to the tail."""
    _stall_safe(monkeypatch)
    with TestClient(app):
        stuck, healthy = uuid.uuid4(), uuid.uuid4()
        lost = [stuck, healthy]
        recovered = []
        swept = []

        async def flaky_requeue(job_id, reason):
            if job_id == stuck:
                raise RuntimeError("simulated permanent lock timeout")
            recovered.append(job_id)

        async def counted_sweep():
            swept.append(True)

        async def parked():
            return None

        real_dispatch_step = jobs.dispatch_step
        monkeypatch.setattr(jobs, "dispatch_step", parked)
        monkeypatch.setattr(jobs, "lost_jobs", lost)
        monkeypatch.setattr(jobs, "requeue_or_fail", flaky_requeue)
        monkeypatch.setattr(jobs, "sweep_stalled_jobs", counted_sweep)

        asyncio.run(real_dispatch_step())

        assert recovered == [healthy], "the job behind the failing one never ran"
        assert lost == [stuck], "the failing entry must stay, at the tail"
        assert swept == [True], "the tick stopped before the stall sweep"


@pytest.mark.db
def test_a_signing_failure_does_not_lose_the_terminal_event(monkeypatch):
    """The URL is signed after the commit and the de-tracking, so a signing
    failure must not swallow the terminal event or the usage event: nothing
    tracks the job any more and nothing would retry either (issue #248)."""
    _stall_safe(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-sign-fail")
            job_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "sign fail"}},
            ).json()["job_id"]
            dispatch = worker.receive_json()
            assert put_upload(client, dispatch["upload"],
                              png_bytes()).status_code == 200

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
            # per session, so this job's row is identified by the count rising.
            events_before = asyncio.run(job_events())
            published = []
            monkeypatch.setattr(
                jobs, "publish",
                lambda job_id, event: published.append((job_id, event)),
            )
            master_key = urlsplit(dispatch["upload"]["url"]).path.rsplit(
                "/api/v1/files/", 1
            )[-1]
            real_storage = jobs.get_storage()

            class FailingUrl:
                """Fails the first signing of the master key: the success path
                is its first caller, and the studio's later refetch still works."""

                def __init__(self):
                    self.fired = False

                def __getattr__(self, name):
                    return getattr(real_storage, name)

                async def url(self, key, download_name=None):
                    if not self.fired and key == master_key:
                        self.fired = True
                        raise RuntimeError("simulated signing failure")
                    return await real_storage.url(key, download_name)

            # get_storage() is called once per request, so one shared instance
            # carries the fired flag across the success path and the refetch.
            failing_url = FailingUrl()
            monkeypatch.setattr(jobs, "get_storage", lambda: failing_url)
            worker.send_json({"type": "job_done", "job_id": job_id, "gpu_ms": 1,
                              "width": 512, "height": 512,
                              "dispatch_token": dispatch["dispatch_token"]})
            poll_until(client, job_id, "succeeded")

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not any(
                event.get("state") == "succeeded" for _, event in published
            ):
                time.sleep(0.05)
            succeeded = next(
                event for _, event in published if event.get("state") == "succeeded"
            )
            assert succeeded.get("url") is None

            def usage_written() -> bool:
                return asyncio.run(job_events()) > events_before

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not usage_written():
                time.sleep(0.05)
            assert usage_written()


@pytest.mark.db
def test_a_requeue_during_a_terminal_transaction_keeps_its_entry(monkeypatch):
    """A stall requeue can land while the verdict's transaction is open: the
    row check must refuse the late verdict, which then must not take the
    replacement's entry or slot (issue #248)."""
    _stall_safe(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-verdict-race")
            job_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "verdict race"}},
            ).json()["job_id"]
            dispatch = dispatch_for(worker, job_id)
            assert put_upload(client, dispatch["upload"],
                              png_bytes()).status_code == 200

            key = uuid.UUID(job_id)
            superseded = jobs.inflight[key]
            second_keys = jobs.storage_keys_for_attempt(superseded.user_id, key, 2)
            replacement = jobs.InFlight(
                worker=superseded.worker, storage_key=second_keys[0],
                thumb_storage_key=second_keys[1], user_id=superseded.user_id,
                dispatch_token="second-attempt-token", attempt=2,
            )
            real_factory = db.session_factory
            assert real_factory is not None
            real_locked_job = jobs.locked_job
            settled = threading.Event()
            state = {"replayed": False, "in_replay": False}

            async def replay_requeue() -> None:
                # The sweeper's DB half: attempt two owns the row before the
                # verdict's transaction reads it.
                async with real_factory() as session:
                    job = await real_locked_job(session, key)
                    assert job is not None
                    job.attempt = 2
                    job.state = "queued"
                    await session.commit()
                jobs.inflight[key] = replacement

            async def locked_job_after_requeue(session, job_id):
                # Only the verdict's own transaction for this job: the
                # requeue lands between the transaction opening and its read
                # of the row, exactly when a sweeper that won the race would.
                if (job_id == key and not state["replayed"]
                        and not state["in_replay"]):
                    state["replayed"] = True
                    state["in_replay"] = True
                    try:
                        await replay_requeue()
                    finally:
                        state["in_replay"] = False
                    settled.set()
                return await real_locked_job(session, job_id)

            monkeypatch.setattr(jobs, "locked_job", locked_job_after_requeue)
            worker.send_json({"type": "job_done", "job_id": job_id, "gpu_ms": 1,
                              "width": 512, "height": 512,
                              "dispatch_token": dispatch["dispatch_token"]})
            assert settled.wait(timeout=5), "the verdict never settled"

            assert jobs.inflight.get(key) is replacement
            assert realtime.workers["w-verdict-race"].jobs_in_flight == 1
            row = client.get(f"/api/v1/generations/{job_id}").json()
            assert row["state"] == "queued"
            assert row["attempt"] == 2

            # Let the job finish so it does not sit queued in the shared
            # database and starve the dispatch loop for every test after this.
            worker.send_json({"type": "job_failed", "job_id": job_id,
                              "reason": "cleanup",
                              "dispatch_token": "second-attempt-token"})
            poll_until(client, job_id, "failed")


@pytest.mark.db
def test_terminal_verdicts_clear_inflight_exactly_once(monkeypatch):
    """The clearing moved after the commit and must still run exactly once per
    job: the slot count is the canary, because a release that runs twice on a
    two-job worker steals the other job's slot."""
    _stall_safe(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-exact-once")
            real_worker = realtime.workers["w-exact-once"]

            first_id = _post_generation(client, "exact-one")
            second_id = _post_generation(client, "exact-two")
            expected = {first_id, second_id}
            time.sleep(0.35)
            first = _wait_for_dispatch(worker, expected)
            second = _wait_for_dispatch(worker, expected - {first["job_id"]})
            _wait_for_slots(real_worker, 2)
            worker.send_json({"type": "job_progress", "job_id": first["job_id"],
                              "progress": 0.5,
                              "dispatch_token": first["dispatch_token"]})
            worker.send_json({"type": "job_progress", "job_id": second["job_id"],
                              "progress": 0.5,
                              "dispatch_token": second["dispatch_token"]})

            assert put_upload(client, first["upload"], png_bytes()).status_code == 200
            worker.send_json({"type": "job_done", "job_id": first["job_id"], "gpu_ms": 1,
                              "width": 512, "height": 512,
                              "dispatch_token": first["dispatch_token"]})
            poll_until(client, first["job_id"], "succeeded")
            _wait_for_slots(real_worker, 1)
            first_key = uuid.UUID(first["job_id"])
            assert first_key not in jobs.inflight
            assert first_key not in jobs.live_progress
            assert first_key not in jobs.last_progress_at

            worker.send_json({"type": "job_failed", "job_id": second["job_id"],
                              "reason": "boom",
                              "dispatch_token": second["dispatch_token"]})
            poll_until(client, second["job_id"], "failed")
            _wait_for_slots(real_worker, 0)
            second_key = uuid.UUID(second["job_id"])
            assert second_key not in jobs.inflight
            assert second_key not in jobs.live_progress
            assert second_key not in jobs.last_progress_at

            # A sweep that pops the entry while the verdict's transaction is
            # open must not double-release the slot: the post-commit clearing
            # only runs for the entry that still owns the job.
            third_id = _post_generation(client, "exact-three")
            fourth_id = _post_generation(client, "exact-four")
            expected = {third_id, fourth_id}
            time.sleep(0.35)
            third = _wait_for_dispatch(worker, expected)
            fourth = _wait_for_dispatch(worker, expected - {third["job_id"]})
            _wait_for_slots(real_worker, 2)
            assert put_upload(client, third["upload"], png_bytes()).status_code == 200

            third_key = uuid.UUID(third["job_id"])
            real_factory = db.session_factory
            assert real_factory is not None
            swept = threading.Event()
            monkeypatch.setattr(
                db, "session_factory",
                _SweepingFactory(real_factory, third_key, real_worker, swept),
            )
            worker.send_json({"type": "job_done", "job_id": third["job_id"], "gpu_ms": 1,
                              "width": 512, "height": 512,
                              "dispatch_token": third["dispatch_token"]})
            assert swept.wait(timeout=5), "the sweeper never raced the verdict"
            poll_until(client, third["job_id"], "succeeded")
            _wait_for_slots(real_worker, 1)
            assert third_key not in jobs.inflight
            assert uuid.UUID(fourth["job_id"]) in jobs.inflight

            _finish_job(client, worker, fourth)


@pytest.mark.db
def test_a_rejected_upload_is_not_left_in_storage():
    """Verification rejecting the output must not leave the object behind.

    No asset row ever names it, and the success path only collects earlier
    attempts, so nothing else would. A worker can push an arbitrarily large
    invalid object through the presigned PUT, which carries no size condition.
    """
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-rejected-upload")
            job_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "rejected"}},
            ).json()["job_id"]
            dispatch = worker.receive_json()
            upload_path = urlsplit(dispatch["upload"]["url"]).path
            key = upload_path.rsplit("/api/v1/files/", 1)[-1]
            assert put_upload(client, dispatch["upload"],
                              b"not a png at all").status_code == 200
            storage = jobs.get_storage()
            assert storage.path(key).exists()

            worker.send_json({"type": "job_done", "job_id": job_id, "gpu_ms": 1,
                              "width": 512, "height": 512,
                              "dispatch_token": dispatch["dispatch_token"]})
            poll_until(client, job_id, "failed")
            assert not storage.path(key).exists(), "rejected upload was left behind"


@pytest.mark.db
def test_a_rejected_retry_collects_every_attempt(monkeypatch):
    """Per-attempt keys stop a stale worker overwriting the winning output, and
    start leaking: a stalled attempt's blobs are overwritten by nothing and
    named by no asset row, and when the retry is then rejected, the success
    path never runs. The rejection path is the only collector those blobs have,
    so it must walk every attempt, not just the rejected one; a first-attempt
    test proves only the loop's last iteration.
    """
    monkeypatch.setenv("JOB_STALL_SECONDS", "0.05")
    from app.settings import get_settings
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/fleet") as worker:
                fleet_hello(worker, "w-rejected-retry")
                job_id = client.post(
                    "/api/v1/generations",
                    json={"model_id": "sd-test", "params": {"prompt": "rejected retry"}},
                ).json()["job_id"]
                first = worker.receive_json()
                first_path = urlsplit(first["upload"]["url"]).path
                first_thumb = urlsplit(first["thumb_upload"]["url"]).path

                # The first attempt uploaded before it stalled.
                poll_until_attempt(client, job_id, 2, timeout=3.0)
                disarm_the_sweeper(monkeypatch)
                second = worker.receive_json()
                storage = jobs.get_storage()
                first_key = first_path.rsplit("/api/v1/files/", 1)[-1]
                first_thumb_key = first_thumb.rsplit("/api/v1/files/", 1)[-1]
                asyncio.run(_write_blob(storage, first_key, png_bytes()))
                asyncio.run(_write_blob(storage, first_thumb_key, png_bytes()))

                # The retry's output is rejected: the key is still inflight so
                # the PUT succeeds, and the invalid bytes fail verification.
                second_path = urlsplit(second["upload"]["url"]).path
                assert put_upload(client, second["upload"],
                                  b"not a png at all").status_code == 200
                second_key = second_path.rsplit("/api/v1/files/", 1)[-1]

                worker.send_json({"type": "job_done", "job_id": job_id, "gpu_ms": 1,
                                  "width": 512, "height": 512,
                                  "dispatch_token": second["dispatch_token"]})
                poll_until(client, job_id, "failed")
                deadline = time.monotonic() + 3
                keys = (first_key, first_thumb_key, second_key)
                while time.monotonic() < deadline and any(storage.path(k).exists()
                                                          for k in keys):
                    time.sleep(0.05)

                assert not storage.path(first_key).exists(), "stalled attempt left behind"
                assert not storage.path(first_thumb_key).exists(), \
                    "stalled attempt's thumbnail left behind"
                assert not storage.path(second_key).exists(), "rejected upload left behind"
    finally:
        monkeypatch.delenv("JOB_STALL_SECONDS", raising=False)
        get_settings.cache_clear()


async def _write_blob(storage, key, data):
    storage.path(key).parent.mkdir(parents=True, exist_ok=True)
    storage.path(key).write_bytes(data)


@pytest.mark.db
def test_malformed_completion_is_recoverable_through_the_fleet_socket(monkeypatch):
    original_worker_int = jobs._worker_int

    def raise_for_mapping(value, default=0):
        if isinstance(value, dict):
            raise ValueError("malformed worker number")
        return original_worker_int(value, default)

    monkeypatch.setattr(jobs, "_worker_int", raise_for_mapping)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-malformed-done")
            job_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "malformed"}},
            ).json()["job_id"]
            dispatch = worker.receive_json()
            worker.send_json({
                "type": "job_done",
                "job_id": job_id,
                "dispatch_token": dispatch["dispatch_token"],
                "gpu_ms": {"not": "a number"},
                "width": 512,
                "height": 512,
            })

            job = poll_until_attempt(client, job_id, 2, timeout=3.0)
            disarm_the_sweeper(monkeypatch)
            assert job["state"] == "queued"
            assert uuid.UUID(job_id) not in jobs.inflight


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

            assert put_upload(client, first["upload"], png_bytes()).status_code == 200
            assert put_upload(client, first["thumb_upload"],
                              webp_bytes()).status_code == 200
            worker.send_json({"type": "job_done", "job_id": first_id,
                              "gpu_ms": 1, "width": 512, "height": 512,
                              "has_thumbnail": True,
                              "dispatch_token": first["dispatch_token"]})
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
            assert put_upload(client, dispatch["upload"], png_bytes()).status_code == 200
            worker.send_json({"type": "job_done", "job_id": source_job_id,
                              "gpu_ms": 100, "width": 512, "height": 512,
                              "dispatch_token": dispatch["dispatch_token"]})
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
            assert client.get(input_path).content == png_bytes()

            assert put_upload(client, i2i_dispatch["upload"],
                              png_bytes()).status_code == 200
            worker.send_json({"type": "job_done", "job_id": edit_job_id,
                              "gpu_ms": 200, "width": 512, "height": 512,
                              "dispatch_token": i2i_dispatch["dispatch_token"]})
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
            assert put_upload(client, dispatch["upload"], png_bytes()).status_code == 200
            worker.send_json({"type": "job_done", "job_id": job_id,
                              "gpu_ms": 1, "width": 512, "height": 512,
                              "dispatch_token": dispatch["dispatch_token"]})
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
            assert put_upload(client, dispatch["upload"],
                              png_bytes()).status_code == 200
            worker.send_json({"type": "job_done", "job_id": source_job_id,
                              "gpu_ms": 50, "width": 512, "height": 512,
                              "dispatch_token": dispatch["dispatch_token"]})
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
            assert client.get(urlsplit(up_dispatch["input"]["url"]).path).content == png_bytes()

            # Upload the real upscaled image: the asset row takes its
            # dimensions from the object now, not from the worker's claim.
            assert put_upload(client, up_dispatch["upload"],
                              png_bytes(1024, 1024)).status_code == 200
            worker.send_json({"type": "job_done", "job_id": upscale_job_id,
                              "gpu_ms": 400, "width": 1024, "height": 1024,
                              "dispatch_token": up_dispatch["dispatch_token"]})
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
            assert put_upload(client, dispatch["upload"], png_bytes()).status_code == 200

            worker.send_json({"type": "job_done", "job_id": job_id,
                              "gpu_ms": 900, "input_fetch_ms": 50,
                              "load_ms": 1200, "postprocess_ms": 80,
                              "width": 512, "height": 512,
                              "dispatch_token": dispatch["dispatch_token"]})

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
            dispatch = worker.receive_json()
            worker.send_json({"type": "job_failed", "job_id": job_id,
                              "reason": "CUDA OOM",
                              "dispatch_token": dispatch["dispatch_token"]})

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
    assert put_upload(client, dispatch["upload"], png_bytes()).status_code == 200
    worker.send_json({"type": "job_done", "job_id": dispatch["job_id"],
                      "gpu_ms": 1, "width": 512, "height": 512,
                      "dispatch_token": dispatch["dispatch_token"]})
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
            worker.send_json({"type": "job_progress", "job_id": d1["job_id"], "progress": 0.5,
                              "dispatch_token": d1["dispatch_token"]})
            worker.send_json({"type": "job_progress", "job_id": d2["job_id"], "progress": 0.5,
                              "dispatch_token": d2["dispatch_token"]})

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
        dispatch_token="token",
    )
    try:
        for unusable in (float("nan"), float("inf"), 10 ** 400, [1], {"a": 1}, None):
            asyncio.run(jobs.on_worker_message(worker, {
                "type": "job_progress", "job_id": str(job_id), "progress": unusable,
                "dispatch_token": "token",
            }))
            assert job_id not in jobs.live_progress, f"{unusable!r} was stored"
        asyncio.run(jobs.on_worker_message(worker, {
            "type": "job_progress", "job_id": str(job_id), "progress": 0.5,
            "dispatch_token": "token",
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
            dispatch_token="token",
        )
        try:
            asyncio.run(jobs.on_worker_message(worker, {
                "type": "job_done", "job_id": str(job_id), "gpu_ms": unusable,
                "width": unusable, "height": unusable,
                # Without the token this worker is at the current protocol and
                # the message is ignored, which would test nothing.
                "dispatch_token": "token",
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


@pytest.mark.db
def test_a_stale_dispatch_token_cannot_speak_for_the_current_attempt():
    """A stall requeue can hand a job back to the same Worker object, so the
    `current.worker is worker` identity check cannot separate attempt one from
    attempt two. Only the per-dispatch token can (issue #247).

    The requeue itself is driven here rather than waited for: the sweeper's
    window is milliseconds wide and the point of the test is the token, not
    the timing. test_stalled_job_requeues_once covers the real requeue.
    """
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-stale-token")
            job_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "token"}},
            ).json()["job_id"]
            dispatch_for(worker, job_id)

            # What a requeue to the same worker leaves behind: a new entry,
            # same Worker object, new key, new token.
            key = uuid.UUID(job_id)
            superseded = jobs.inflight[key]
            second_keys = jobs.storage_keys_for_attempt(superseded.user_id, key, 2)
            jobs.inflight[key] = jobs.InFlight(
                worker=superseded.worker, storage_key=second_keys[0],
                thumb_storage_key=second_keys[1], user_id=superseded.user_id,
                dispatch_token="second-attempt-token", attempt=2,
            )
            current = jobs.inflight[key]
            assert current.dispatch_token != superseded.dispatch_token

            # The superseded attempt reports success. It knows the job id and
            # holds the same socket, and must still not be believed.
            asyncio.run(jobs.on_worker_message(current.worker, {
                "type": "job_done", "job_id": job_id,
                "dispatch_token": superseded.dispatch_token,
                "gpu_ms": 1, "width": 512, "height": 512,
            }))
            assert jobs.inflight.get(key) is current
            assert client.get(f"/api/v1/generations/{job_id}").json()["state"] == "running"

            # The live attempt's own token is believed.
            put = client.put(
                f"/api/v1/files/{current.storage_key}", content=png_bytes(),
                headers={"X-Upload-Token": current.dispatch_token},
            )
            assert put.status_code == 200
            worker.send_json({"type": "job_done", "job_id": job_id,
                              "dispatch_token": current.dispatch_token,
                              "gpu_ms": 2, "width": 512, "height": 512})
            assert poll_until(client, job_id, "succeeded")["gpu_ms"] == 2


@pytest.mark.db
def test_a_worker_that_sends_no_dispatch_token_is_still_believed():
    """The N-1 promise: a protocol 2 worker echoes no token and must still be
    able to finish a job (docs/connection-handling.md)."""
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-no-token", version=PROTOCOL_VERSION - 1)
            job_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "n-1"}},
            ).json()["job_id"]
            dispatch = dispatch_for(worker, job_id)
            assert put_upload(client, dispatch["upload"], png_bytes()).status_code == 200
            worker.send_json({"type": "job_done", "job_id": job_id,
                              "gpu_ms": 7, "width": 512, "height": 512})
            assert poll_until(client, job_id, "succeeded")["gpu_ms"] == 7


@pytest.mark.db
def test_a_current_worker_that_omits_the_dispatch_token_is_ignored():
    """The token is required from a protocol 3 worker, not merely echoed: an
    omission is as stale as a wrong token, so the job must neither succeed
    nor record an asset (issue #247)."""
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-missing-token")
            job_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "missing token"}},
            ).json()["job_id"]
            dispatch_for(worker, job_id)

            key = uuid.UUID(job_id)
            current = jobs.inflight[key]
            asyncio.run(jobs.on_worker_message(current.worker, {
                "type": "job_done", "job_id": job_id,
                "gpu_ms": 7, "width": 512, "height": 512,
            }))
            assert jobs.inflight.get(key) is current
            assert client.get(f"/api/v1/generations/{job_id}").json()["state"] == "running"

            async def recorded_assets():
                assert db.session_factory is not None
                async with db.session_factory() as session:
                    result = await session.execute(
                        select(Asset).where(Asset.job_id == key)
                    )
                    return list(result.scalars())

            assert asyncio.run(recorded_assets()) == []

            # Finish the job with its own token, as the stale-token test
            # does: a job left running is requeued when this socket closes.
            assert client.put(
                f"/api/v1/files/{current.storage_key}", content=png_bytes(),
                headers={"X-Upload-Token": current.dispatch_token},
            ).status_code == 200
            worker.send_json({"type": "job_done", "job_id": job_id,
                              "dispatch_token": current.dispatch_token,
                              "gpu_ms": 8, "width": 512, "height": 512})
            assert poll_until(client, job_id, "succeeded")["gpu_ms"] == 8


@pytest.mark.db
def test_an_upload_needs_the_token_of_its_own_dispatch():
    """The key is derivable by anyone who was ever dispatched the job; the
    token is not (issue #247)."""
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-upload-token")
            first_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "one"}},
            ).json()["job_id"]
            first = dispatch_for(worker, first_id)
            second_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "two"}},
            ).json()["job_id"]
            second = dispatch_for(worker, second_id)
            assert first_id != second_id

            path = urlsplit(first["upload"]["url"]).path
            assert client.put(path, content=png_bytes()).status_code == 403
            assert client.put(path, content=png_bytes(),
                              headers={"X-Upload-Token": ""}).status_code == 403
            # Another live dispatch's token does not carry to this key.
            assert client.put(path, content=png_bytes(), headers={
                "X-Upload-Token": second["upload"]["headers"]["X-Upload-Token"],
            }).status_code == 403
            assert put_upload(client, first["upload"], png_bytes()).status_code == 200

            # Finish both: a job left running is requeued when this socket
            # closes and its dispatch would arrive in the next test's socket.
            for job, ident in ((first, first_id), (second, second_id)):
                if job is second:
                    assert put_upload(client, job["upload"],
                                      png_bytes()).status_code == 200
                worker.send_json({"type": "job_done", "job_id": ident,
                                  "dispatch_token": job["dispatch_token"],
                                  "gpu_ms": 1, "width": 512, "height": 512})
                poll_until(client, ident, "succeeded")


@pytest.mark.db
def test_an_output_is_written_once():
    """Verification proves what the object was at inspection time; a second
    write would make that proof worthless (issue #249)."""
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-write-once")
            job_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "once"}},
            ).json()["job_id"]
            dispatch = dispatch_for(worker, job_id)
            assert put_upload(client, dispatch["upload"],
                              png_bytes(320, 240)).status_code == 200
            assert put_upload(client, dispatch["upload"],
                              png_bytes(64, 64)).status_code == 409
            served = client.get(urlsplit(dispatch["upload"]["url"]).path)
            assert served.content == png_bytes(320, 240)
            worker.send_json({"type": "job_done", "job_id": dispatch["job_id"],
                              "dispatch_token": dispatch["dispatch_token"],
                              "gpu_ms": 1, "width": 320, "height": 240})
            poll_until(client, dispatch["job_id"], "succeeded")


@pytest.mark.db
def test_a_thumbnail_that_is_not_a_webp_is_dropped_rather_than_served():
    """has_thumbnail used to create the row on the worker's word alone, so a
    worker could have the studio serve arbitrary bytes as an image."""
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-bad-thumb")
            job_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "thumb"}},
            ).json()["job_id"]
            dispatch = dispatch_for(worker, job_id)
            assert put_upload(client, dispatch["upload"], png_bytes()).status_code == 200
            assert put_upload(client, dispatch["thumb_upload"],
                              b"not a webp at all").status_code == 200
            worker.send_json({"type": "job_done", "job_id": job_id,
                              "dispatch_token": dispatch["dispatch_token"],
                              "gpu_ms": 3, "width": 512, "height": 512,
                              "has_thumbnail": True})

            job = poll_until(client, job_id, "succeeded")
            assert len(job["assets"]) == 1
            assert job["assets"][0]["thumbnail_url"] is None
            # The rejected object is collected, not left addressable.
            assert client.get(urlsplit(dispatch["thumb_upload"]["url"]).path).status_code == 404


@pytest.mark.db
def test_an_oversized_thumbnail_is_dropped_rather_than_scaled_down():
    """An oversized WebP used to pass inspection and was then recorded with
    the master's dimensions scaled to the thumbnail cap, so a huge object was
    served to gallery views as a small thumbnail."""
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-big-thumb")
            job_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "big thumb"}},
            ).json()["job_id"]
            dispatch = dispatch_for(worker, job_id)
            assert put_upload(client, dispatch["upload"], png_bytes()).status_code == 200
            assert put_upload(client, dispatch["thumb_upload"],
                              _WEBP_BIG_BYTES).status_code == 200
            worker.send_json({"type": "job_done", "job_id": job_id,
                              "dispatch_token": dispatch["dispatch_token"],
                              "gpu_ms": 4, "width": 512, "height": 512,
                              "has_thumbnail": True})

            job = poll_until(client, job_id, "succeeded")
            assert len(job["assets"]) == 1
            assert job["assets"][0]["thumbnail_url"] is None
            assert client.get(urlsplit(dispatch["thumb_upload"]["url"]).path).status_code == 404


def test_a_non_ascii_dispatch_token_is_ignored():
    """compare_digest raises TypeError on a non-ASCII str, and TypeError is
    not in the fleet handler's except tuple, so such a token used to raise out
    of the socket; it is stale, not a crash."""
    worker = realtime.Worker(id="w-non-ascii", ws=None, manifests=[], realtime_slots=1)
    job_id = uuid.uuid4()
    jobs.inflight[job_id] = jobs.InFlight(
        worker=worker, storage_key="k", thumb_storage_key="t", user_id=uuid.uuid4(),
        dispatch_token="token",
    )
    try:
        asyncio.run(jobs.on_worker_message(worker, {
            "type": "job_progress", "job_id": str(job_id), "progress": 0.5,
            "dispatch_token": "caf\u00e9",
        }))
        assert job_id in jobs.inflight
        assert job_id not in jobs.live_progress
    finally:
        jobs.inflight.pop(job_id, None)
        jobs.live_progress.pop(job_id, None)
        jobs.last_progress_at.pop(job_id, None)


def test_a_non_ascii_upload_token_fails_closed():
    """compare_digest raises TypeError on a non-ASCII str, which used to make
    the upload route answer 500; a token that cannot be compared is wrong, not
    a bug (TestClient cannot even send such a header, so the guard itself is
    exercised here)."""
    worker = realtime.Worker(id="w-non-ascii-upload", ws=None, manifests=[],
                             realtime_slots=1)
    job_id = uuid.uuid4()
    jobs.inflight[job_id] = jobs.InFlight(
        worker=worker, storage_key="k", thumb_storage_key="t", user_id=uuid.uuid4(),
        dispatch_token="token",
    )
    try:
        assert not jobs.upload_authorized("k", "caf\u00e9")
        # The ASCII token of its own dispatch is still believed.
        assert jobs.upload_authorized("k", "token")
    finally:
        jobs.inflight.pop(job_id, None)


@pytest.mark.db
def test_authorization_is_rechecked_after_the_body(monkeypatch):
    """The first check runs before the body arrives, and reading it can take
    long enough for a retry to supersede this attempt; the second check must
    stop the stale bytes from being published under the winning key."""
    from starlette.requests import Request

    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-recheck")
            job_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "recheck"}},
            ).json()["job_id"]
            dispatch = dispatch_for(worker, job_id)
            key = uuid.UUID(job_id)
            current = jobs.inflight[key]
            replacement = jobs.InFlight(
                worker=current.worker, storage_key=current.storage_key,
                thumb_storage_key=current.thumb_storage_key,
                user_id=current.user_id, dispatch_token="replacement-token",
                attempt=2,
            )
            real_stream = Request.stream

            def superseding_stream(self):
                # The requeue lands while the body is being read, between the
                # two checks.
                jobs.inflight[key] = replacement
                return real_stream(self)

            monkeypatch.setattr(Request, "stream", superseding_stream)
            response = client.put(urlsplit(dispatch["upload"]["url"]).path,
                                  content=png_bytes(),
                                  headers=dispatch["upload"]["headers"])
            assert response.status_code == 403
            storage = jobs.get_storage()
            assert not storage.path(current.storage_key).exists()
            assert jobs.inflight.get(key) is replacement

            # Finish the job so it does not sit running in the shared database.
            # The live entry is the replacement, so its token is the one that
            # speaks; the missing object then fails the job.
            worker.send_json({"type": "job_done", "job_id": job_id, "gpu_ms": 1,
                              "width": 512, "height": 512,
                              "dispatch_token": "replacement-token"})
            poll_until(client, job_id, "failed")


@pytest.mark.db
def test_authorization_is_rechecked_between_write_and_link(monkeypatch):
    """The write happens in a thread, and the re-check only counts if the
    link follows it on the loop: a requeue that lands while the temporary is
    being written must leave nothing behind and publish nothing."""
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-write-link")
            job_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "write-link"}},
            ).json()["job_id"]
            dispatch = dispatch_for(worker, job_id)
            key = uuid.UUID(job_id)
            current = jobs.inflight[key]
            replacement = jobs.InFlight(
                worker=current.worker, storage_key=current.storage_key,
                thumb_storage_key=current.thumb_storage_key,
                user_id=current.user_id, dispatch_token="replacement-token",
                attempt=2,
            )
            real_to_thread = asyncio.to_thread

            def superseding_to_thread(function):
                def swap_after_write():
                    try:
                        return function()
                    finally:
                        # The requeue lands once the temporary exists, between
                        # the off-thread write and the on-loop link.
                        jobs.inflight[key] = replacement

                return real_to_thread(swap_after_write)

            monkeypatch.setattr(asyncio, "to_thread", superseding_to_thread)
            response = client.put(urlsplit(dispatch["upload"]["url"]).path,
                                  content=png_bytes(),
                                  headers=dispatch["upload"]["headers"])
            assert response.status_code == 403
            storage = jobs.get_storage()
            assert not storage.path(current.storage_key).exists()
            leftovers = [
                entry.name for entry in storage.path(current.storage_key).parent.iterdir()
                if entry.name.startswith(".upload-")
            ]
            assert not leftovers, "a refused upload left its temporary behind"
            assert jobs.inflight.get(key) is replacement

            # Finish the job so it does not sit running in the shared database.
            # The live entry is the replacement, so its token is the one that
            # speaks; the missing object then fails the job.
            worker.send_json({"type": "job_done", "job_id": job_id, "gpu_ms": 1,
                              "width": 512, "height": 512,
                              "dispatch_token": "replacement-token"})
            poll_until(client, job_id, "failed")


@pytest.mark.db
def test_a_failed_write_leaves_no_partial_upload(monkeypatch):
    """Publishing is atomic: a write that fails partway must not leave a
    truncated object at the key, because nothing would replace it (uploads are
    write-once) and the API could inspect and approve a prefix."""
    import os

    with TestClient(app, raise_server_exceptions=False) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-partial-write")
            job_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "partial"}},
            ).json()["job_id"]
            dispatch = dispatch_for(worker, job_id)
            path = urlsplit(dispatch["upload"]["url"]).path
            key = path.rsplit("/api/v1/files/", 1)[-1]
            storage = jobs.get_storage()

            real_fdopen = os.fdopen

            def failing_fdopen(fd, mode):
                handle = real_fdopen(fd, mode)

                class Failing:
                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        return handle.__exit__(*args)

                    def write(self, data):
                        handle.write(data[:16])
                        raise OSError("simulated disk failure")

                return Failing()

            monkeypatch.setattr("app.files.os.fdopen", failing_fdopen)
            assert client.put(path, content=png_bytes(),
                              headers=dispatch["upload"]["headers"]).status_code == 500
            assert not storage.path(key).exists(), "a failed write left a partial object"
            leftovers = [
                entry.name for entry in storage.path(key).parent.iterdir()
                if entry.name.startswith(".upload-")
            ]
            assert not leftovers, "a failed write left temporary files behind"

            monkeypatch.undo()
            # The retry is a fresh attempt on the same key here, and it must
            # not find a corpse from the failed write.
            assert put_upload(client, dispatch["upload"], png_bytes()).status_code == 200
            worker.send_json({"type": "job_done", "job_id": job_id, "gpu_ms": 1,
                              "width": 512, "height": 512,
                              "dispatch_token": dispatch["dispatch_token"]})
            poll_until(client, job_id, "succeeded")


@pytest.mark.db
def test_upload_temporaries_carry_a_debris_prefix(monkeypatch):
    """A process death between write and link leaves the temporary behind, so
    its name must read as debris rather than content; the destination names
    itself, and the directory is already per user and per job."""
    import tempfile

    real_mkstemp = tempfile.mkstemp
    prefixes = []

    def recording_mkstemp(*args, **kwargs):
        prefixes.append(kwargs.get("prefix"))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", recording_mkstemp)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-temp-prefix")
            job_id = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-test", "params": {"prompt": "temp prefix"}},
            ).json()["job_id"]
            dispatch = dispatch_for(worker, job_id)
            assert put_upload(client, dispatch["upload"], png_bytes()).status_code == 200
            assert prefixes == [".upload-"]


def test_a_worker_built_without_a_registration_is_not_lenient():
    """protocol_version defaults to the current protocol, not to None.

    Two gates read it. This one requires the token from a worker at 3 or
    newer, so an unregistered worker must be held to it rather than handed
    the leniency that exists only for an older one (issue #282). The
    update_session gate in realtime.py reads the same field the other way
    round, and the same default is right there: it sends the update rather
    than withholding it, which is what a current worker should get.
    """
    worker = realtime.Worker(id="w-default", ws=None, manifests=[], realtime_slots=1)
    assert worker.protocol_version == realtime.PROTOCOL_VERSION

    job_id = uuid.uuid4()
    jobs.inflight[job_id] = jobs.InFlight(
        worker=worker, storage_key="k", thumb_storage_key="t", user_id=uuid.uuid4(),
        dispatch_token="token",
    )
    try:
        asyncio.run(jobs.on_worker_message(worker, {
            "type": "job_progress", "job_id": str(job_id), "progress": 0.5,
        }))
        assert job_id not in jobs.live_progress
    finally:
        jobs.inflight.pop(job_id, None)
        jobs.live_progress.pop(job_id, None)
        jobs.last_progress_at.pop(job_id, None)
