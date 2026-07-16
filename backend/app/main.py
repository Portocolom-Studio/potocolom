import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from app import db, jobs
from app.benchmark import router as benchmark_router
from app.files import router as files_router
from app.gpu_samples import maintain_loop
from app.jobs import router as jobs_router
from app.logs import setup_logging
from app.metrics import router as metrics_router
from app.realtime import reap_dead_workers
from app.realtime import router as realtime_router
from app.registry import router as registry_router
from app.settings import get_settings
from app.studio import router as studio_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging(get_settings().log_format)
    if await db.connect():
        await jobs.recover()
    tasks = [
        asyncio.create_task(reap_dead_workers()),
        asyncio.create_task(jobs.dispatch_loop()),
        asyncio.create_task(maintain_loop()),
    ]
    yield
    for task in tasks:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await db.dispose()


app = FastAPI(title="potocolom", lifespan=lifespan)
app.include_router(realtime_router)
if get_settings().benchmark_api:
    app.include_router(benchmark_router)
app.include_router(registry_router)
app.include_router(jobs_router)
app.include_router(files_router)
app.include_router(studio_router)
app.include_router(metrics_router)


@app.get("/api/v1/health")
async def health() -> dict:
    # Answers from process state only: the load balancer must not be convinced
    # to kill healthy tasks during a database incident (docs/blueprint.md).
    return {"status": "ok"}


@app.get("/api/v1/config")
async def config() -> dict:
    settings = get_settings()
    return {
        "auth_methods": settings.auth_methods,
        "billing_enabled": settings.billing_enabled,
        "languages": ["en", "es"],
    }


class SPAStaticFiles(StaticFiles):
    """Serve a built SPA: unknown GET paths fall back to index.html."""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # Unknown API paths must stay 404s; only page routes fall back.
            is_api = path == "api" or path.startswith("api/")
            if exc.status_code == 404 and scope["method"] == "GET" and not is_api:
                return await super().get_response("index.html", scope)
            raise


_settings = get_settings()
if _settings.frontend_dist:
    app.mount(
        "/",
        SPAStaticFiles(directory=Path(_settings.frontend_dist), html=True),
        name="frontend",
    )
