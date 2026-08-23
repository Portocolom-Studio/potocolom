from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings

# The full configuration surface is specified in docs/blueprint.md. Settings are
# added here as the features that read them land; nothing is declared speculatively.


class Settings(BaseSettings):
    auth_mode: Literal["none", "accounts"] = "none"
    oauth_providers: str = ""
    billing_enabled: bool = False
    log_format: Literal["plain", "json"] = "plain"
    fleet_token_key: str = ""

    # Versioned root key ring, comma separated version:base64key entries, newest first.
    # The first entry is the active write key; every entry stays readable so rotation is
    # active write, multi read, re-encrypt, then remove. Separate from FLEET_TOKEN_KEY.
    root_keys: str = ""

    # PUBLIC_URL is where browsers reach this API; asset URLs in responses use it.
    public_url: str = "http://localhost:8000"

    # INTERNAL_URL is where workers reach the API inside a container network.
    # Defaults to PUBLIC_URL for native dev (docs/blueprint.md).
    internal_url: str = ""

    # Extra browser origins allowed to open the WebSocket endpoints, comma
    # separated. PUBLIC_URL is always allowed; this is for the dev loop, where
    # the vite server proxies /api/v1 and the browser's origin is its own
    # (issue #201).
    allowed_origins: str = ""

    # Defaults match deploy/compose/dev.yml for the native dev loop.
    database_url: str = "postgresql://potocolom:potocolom@localhost:5432/potocolom"

    storage_backend: Literal["local", "s3"] = "local"
    storage_local_path: str = "/data/assets"
    storage_s3_bucket: str = "potocolom"
    storage_s3_region: str = "us-east-1"
    storage_s3_endpoint: str = ""  # empty: real AWS; MinIO in development
    storage_s3_access_key: str = ""
    storage_s3_secret_key: str = ""

    # Mail is optional. EMAIL_BACKEND=none is the self-hosted default: an
    # invitation link is copied by hand, so nothing here is required to add
    # people to an install.
    email_backend: Literal["none", "smtp", "ses"] = "none"
    mail_from: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    ses_region: str = ""

    benchmark_api: bool = False  # expose /api/v1/benchmark/* for scripts/benchmark.py
    telemetry: bool = True

    # When set, the API serves the built SPA from this directory (self-hosted
    # profile; docs/blueprint.md).
    frontend_dist: str = ""

    # Requeue or fail running jobs with no dispatch/progress for this long (issue #61).
    job_stall_seconds: float = 600.0

    @property
    def worker_url(self) -> str:
        return (self.internal_url or self.public_url).rstrip("/")

    @property
    def auth_methods(self) -> list[str]:
        if self.auth_mode == "none":
            return []
        methods = ["password"]
        methods += [
            provider for provider in (part.strip() for part in self.oauth_providers.split(","))
            if provider in {"google", "github"}
        ]
        return methods


@lru_cache
def get_settings() -> Settings:
    return Settings()
