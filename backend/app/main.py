import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from app import db, jobs
from app.benchmark import router as benchmark_router
from app.benchmark_sessions import router as benchmark_sessions_router
from app.files import router as files_router
from app.gpu_samples import maintain_loop
from app.jobs import router as jobs_router
from app.logs import setup_logging
from app.metrics import router as metrics_router
from app.realtime import forwarding_trusts_any_peer, reap_dead_workers
from app.realtime import router as realtime_router
from app.registry import router as registry_router
from app.security import SecurityHeadersMiddleware, unhandled_exception_response
from app.settings import get_settings
from app.studio import router as studio_router
from app.telemetry import DESTINATION, telemetry_loop
from app.telemetry import router as telemetry_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_format)
    if settings.auth_mode != "none":
        # An install configured for authentication that performs none is worse
        # than one that will not start: nothing in the running system reveals
        # the difference, so the operator would never find out.
        raise RuntimeError(
            f"AUTH_MODE={settings.auth_mode} is not implemented and would "
            "authenticate nobody: every request would resolve to the local "
            "admin user. Set AUTH_MODE=none until issues #5 and #9 land local "
            "and OAuth accounts."
        )
    if not settings.fleet_token_key:
        logging.getLogger("potocolom.realtime").warning(
            "FLEET_TOKEN_KEY is unset; fleet authentication is permissive for "
            "workers whose address cannot route from the internet"
        )
        # Permissive mode decides on the peer address, and uvicorn overwrites
        # that from X-Forwarded-For for any peer it is told to trust. Trusting
        # every peer hands the decision to the client, which can then claim a
        # loopback address and register from anywhere. The pair is what is
        # dangerous, so warn only when both halves are present.
        #
        # The environment variable is what the shipped Docker command uses, and
        # it is all this can see: a --forwarded-allow-ips flag or a programmatic
        # Config would set the same thing somewhere unreachable from here, so
        # this warning can miss a dangerous launch and cannot be the control.
        if forwarding_trusts_any_peer(os.environ.get("FORWARDED_ALLOW_IPS", "")):
            logging.getLogger("potocolom.realtime").warning(
                "FORWARDED_ALLOW_IPS trusts forwarded headers from every peer while "
                "FLEET_TOKEN_KEY is unset: a client can present any address and "
                "register as a worker. Set FLEET_TOKEN_KEY."
            )
    elif not settings.fleet_token_key.isascii():
        # HTTP headers are latin-1 on the wire, so a non-ASCII secret may not
        # survive the trip intact. Say so here rather than let the operator
        # debug a worker that reconnects forever against a correct secret.
        logging.getLogger("potocolom.realtime").warning(
            "FLEET_TOKEN_KEY contains non-ASCII characters; use an ASCII secret"
        )
    if settings.telemetry:
        logging.getLogger("potocolom.telemetry").info(
            "anonymous daily telemetry destination=%s payload=aggregate usage counts and "
            "worker device/memory mode; set TELEMETRY=false to disable",
            DESTINATION,
        )
    else:
        logging.getLogger("potocolom.telemetry").info(
            "anonymous daily telemetry disabled by TELEMETRY=false"
        )
    if await db.connect():
        await jobs.recover()
    tasks = [
        asyncio.create_task(reap_dead_workers()),
        asyncio.create_task(jobs.dispatch_loop()),
        asyncio.create_task(maintain_loop()),
        asyncio.create_task(telemetry_loop()),
    ]
    yield
    for task in tasks:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await db.dispose()


app = FastAPI(
    title="potocolom",
    lifespan=lifespan,
    # Swagger UI / ReDoc load JS and CSS from cdn.jsdelivr.net; that violates
    # script-src/style-src 'self'. Interactive docs are not a supported
    # production endpoint - keep /openapi.json for tooling.
    docs_url=None,
    redoc_url=None,
)
# User middleware covers API, static, SPA fallbacks, and handled HTTP errors.
# Unhandled 500s are emitted by ServerErrorMiddleware outside that stack, so
# they get headers from the Exception handler below instead.
app.add_middleware(SecurityHeadersMiddleware)
app.add_exception_handler(Exception, unhandled_exception_response)
app.include_router(realtime_router)
if get_settings().benchmark_api:
    app.include_router(benchmark_router)
app.include_router(benchmark_sessions_router)
app.include_router(registry_router)
app.include_router(jobs_router)
app.include_router(files_router)
app.include_router(studio_router)
app.include_router(metrics_router)
app.include_router(telemetry_router)


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

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        if Path(full_path).name == "index.html":
            response.headers["Cache-Control"] = "no-cache"
        return response

    def _may_fall_back(self, path: str, scope) -> bool:
        # Unknown API paths must stay 404s; only page routes fall back.
        return scope["method"] == "GET" and path != "api" and not path.startswith("api/")

    async def get_response(self, path: str, scope):
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and self._may_fall_back(path, scope):
                return await super().get_response("index.html", scope)
            raise
        # With html=True, StaticFiles answers a miss with 404.html when the build
        # ships one instead of raising, which would leave every client-side route
        # (/app, /benchmark) serving the error page in the self-hosted container.
        if response.status_code == 404 and self._may_fall_back(path, scope):
            return await super().get_response("index.html", scope)
        return response


_settings = get_settings()
if _settings.frontend_dist:
    app.mount(
        "/",
        SPAStaticFiles(directory=Path(_settings.frontend_dist), html=True),
        name="frontend",
    )
