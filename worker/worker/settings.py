import uuid
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings

# Device targets and the fleet connection are specified in docs/architecture.md
# and docs/connection-handling.md. Inference dependencies (diffusers, torch)
# arrive with issue #15; this package stays installable without a GPU.


class Settings(BaseSettings):
    device: Literal["cuda", "rocm", "cpu"] = "cpu"
    api_url: str = "ws://localhost:8000/api/v1/fleet"
    worker_id: str = Field(default_factory=lambda: f"worker-{uuid.uuid4().hex[:8]}")
    realtime_slots: int = 2
    heartbeat_seconds: float = 30.0
    inference_seconds: float = 0.15  # simulated GPU time until issue #15


@lru_cache
def get_settings() -> Settings:
    return Settings()
