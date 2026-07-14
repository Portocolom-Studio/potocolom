from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings

# The full configuration surface is specified in docs/blueprint.md. Settings are
# added here as the features that read them land; nothing is declared speculatively.


class Settings(BaseSettings):
    auth_mode: Literal["none", "local", "oauth"] = "none"
    oauth_providers: str = ""  # comma separated, read only when auth_mode is oauth
    billing_enabled: bool = False
    log_format: Literal["plain", "json"] = "plain"

    # PUBLIC_URL is where browsers reach this API; asset URLs in responses use it.
    public_url: str = "http://localhost:8000"

    # INTERNAL_URL is where workers reach the API inside a container network.
    # Defaults to PUBLIC_URL for native dev (docs/blueprint.md).
    internal_url: str = ""

    # Defaults match deploy/compose/dev.yml for the native dev loop.
    database_url: str = "postgresql://potocolom:potocolom@localhost:5432/potocolom"

    storage_backend: Literal["local", "s3"] = "local"
    storage_local_path: str = "/data/assets"
    storage_s3_bucket: str = "potocolom"
    storage_s3_region: str = "us-east-1"
    storage_s3_endpoint: str = ""  # empty: real AWS; MinIO in development
    storage_s3_access_key: str = ""
    storage_s3_secret_key: str = ""

    benchmark_api: bool = False  # expose /api/v1/benchmark/* for scripts/benchmark.py

    # Requeue or fail running jobs with no dispatch/progress for this long (issue #61).
    job_stall_seconds: float = 600.0

    @property
    def worker_url(self) -> str:
        return (self.internal_url or self.public_url).rstrip("/")

    @property
    def auth_methods(self) -> list[str]:
        if self.auth_mode == "none":
            return []
        methods = ["local"]
        if self.auth_mode == "oauth":
            methods += [p.strip() for p in self.oauth_providers.split(",") if p.strip()]
        return methods


@lru_cache
def get_settings() -> Settings:
    return Settings()
