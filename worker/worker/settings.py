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
    memory_mode: Literal["auto", "full", "model_offload", "group_offload"] = "auto"
    api_url: str = "ws://localhost:8000/api/v1/fleet"
    log_format: Literal["plain", "json"] = "plain"
    worker_id: str = Field(default_factory=lambda: f"worker-{uuid.uuid4().hex[:8]}")
    realtime_slots: int = 2  # upper bound; DiffusersEngine calibrates down at warmup
    heartbeat_seconds: float = 30.0
    models_dir: str = ""  # manifests directory; empty runs the simulated engine
    inference_seconds: float = 0.15  # simulated engine only
    # Opt-in: ROCm A/B on the reference card showed ~0-7% warm denoise gain
    # against multi-minute cold loads (PR #141). CUDA fleet bake-off may flip.
    torch_compile: bool = False
    # Diffusers set_attention_backend name; empty string skips the call.
    attention_backend: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
