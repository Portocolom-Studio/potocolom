from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings

# The full configuration surface is specified in docs/blueprint.md. Settings are
# added here as the features that read them land; nothing is declared speculatively.


class Settings(BaseSettings):
    auth_mode: Literal["none", "local", "oauth"] = "none"
    oauth_providers: str = ""  # comma separated, read only when auth_mode is oauth
    billing_enabled: bool = False
    public_url: str = "http://localhost:8000"

    @property
    def auth_methods(self) -> list[str]:
        if self.auth_mode == "none":
            return []
        methods = ["local"]
        if self.auth_mode == "oauth":
            methods += [p for p in self.oauth_providers.split(",") if p]
        return methods


@lru_cache
def get_settings() -> Settings:
    return Settings()
