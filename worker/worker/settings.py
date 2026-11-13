import ipaddress
import uuid
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings

# Device targets and the fleet connection are specified in docs/architecture.md
# and docs/connection-handling.md. Inference dependencies (diffusers, torch)
# arrive with issue #15; this package stays installable without a GPU.


class Settings(BaseSettings):
    device: Literal["cuda", "rocm", "cpu"] = "cpu"
    memory_mode: Literal["auto", "full", "model_offload", "group_offload"] = "auto"
    # Above the 1.18 GiB margin a resident realtime model left for an 8 GiB load
    # on a 16 GB card that also drives a display, measured in issue #753.
    vram_reserve_mb: int = Field(default=1536, ge=0)
    api_url: str = "ws://localhost:8000/api/v1/fleet"
    fleet_token: str = ""
    # One-use token from the studio. The first start exchanges it for a credential
    # and keeps that credential in state_dir (run() in client.py).
    join_token: str = ""
    state_dir: str = ""
    # Opt-in for a private test network: allow plain ws:// to a remote host.
    allow_insecure_fleet: bool = False
    log_format: Literal["plain", "json"] = "plain"
    worker_id: str = Field(default_factory=lambda: f"worker-{uuid.uuid4().hex[:8]}")
    realtime_slots: int = 4  # upper bound; DiffusersEngine calibrates down at warmup
    heartbeat_seconds: float = 30.0
    models_dir: str = ""  # manifests directory; empty runs the simulated engine
    # false: serve only the operator manifests in models_dir (an upscale-only worker).
    bundled_manifests: bool = True
    # Exclusive flock path for a single local worker (make dev-start sets this).
    worker_lock: str = ""
    inference_seconds: float = 0.15  # simulated engine only
    # Opt-in: ROCm A/B on the reference card showed ~0-7% warm denoise gain
    # against multi-minute cold loads (PR #141). CUDA fleet bake-off may flip.
    torch_compile: bool = False
    # Diffusers set_attention_backend name; empty string skips the call.
    attention_backend: str = ""


def fleet_url_error(api_url: str, allow_insecure: bool) -> str | None:
    """Return why the fleet URL must not be dialed, or None when it may be.

    The fleet token is sent in clear text over ws://, so a plain ws:// address
    is accepted only for a loopback address, localhost, or a single-label name
    such as the compose service "api", unless allow_insecure is set.
    """
    parts = urlsplit(api_url)
    if parts.scheme != "ws" or allow_insecure:
        return None
    host = parts.hostname or ""
    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        # A label that starts with a digit is refused: the resolver reads
        # "134744072" and "0x08080808" as the address 8.8.8.8. So is a host that
        # is not ASCII: the client applies IDNA before it connects, which reads
        # U+3002 and U+FF0E as a period, so such a host is not one label.
        if host == "localhost" or (
                host and host.isascii() and "." not in host and not host[0].isdigit()):
            return None
    return (
        f"refusing plain ws:// to remote host {host!r}: the fleet token would be sent "
        "in clear text; use wss:// or set ALLOW_INSECURE_FLEET=true for a private network"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
