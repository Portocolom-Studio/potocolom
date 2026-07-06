from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings

# Device targets and the fleet connection are specified in docs/architecture.md
# and docs/blueprint.md. Inference dependencies (diffusers, torch) arrive with
# issue #15; this skeleton deliberately stays installable without a GPU.


class Settings(BaseSettings):
    device: Literal["cuda", "rocm", "cpu"] = "cpu"
    api_url: str = "ws://localhost:8000/api/v1/fleet"
    models_dir: str = "/models"


@lru_cache
def get_settings() -> Settings:
    return Settings()
