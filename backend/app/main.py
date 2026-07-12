import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app import db, jobs
from app.files import router as files_router
from app.jobs import router as jobs_router
from app.logs import setup_logging
from app.realtime import reap_dead_workers
from app.realtime import router as realtime_router
from app.registry import router as registry_router
from app.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging(get_settings().log_format)
    if await db.connect():
        await jobs.recover()
    tasks = [
        asyncio.create_task(reap_dead_workers()),
        asyncio.create_task(jobs.dispatch_loop()),
    ]
    yield
    for task in tasks:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await db.dispose()


app = FastAPI(title="potocolom", lifespan=lifespan)
app.include_router(realtime_router)
app.include_router(registry_router)
app.include_router(jobs_router)
app.include_router(files_router)


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
