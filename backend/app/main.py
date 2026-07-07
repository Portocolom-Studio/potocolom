import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.realtime import reap_dead_workers
from app.realtime import router as realtime_router
from app.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    reaper = asyncio.create_task(reap_dead_workers())
    yield
    reaper.cancel()


app = FastAPI(title="potocolom", lifespan=lifespan)
app.include_router(realtime_router)


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
