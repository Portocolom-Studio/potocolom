from worker.settings import Settings
from pydantic import ValidationError
import pytest


def test_defaults():
    settings = Settings()
    assert settings.device == "cpu"
    assert settings.api_url == "ws://localhost:8000/api/v1/fleet"
    assert settings.realtime_slots == 4
    assert settings.torch_compile is False
    assert settings.attention_backend == ""
    assert settings.vram_reserve_mb == 1536


def test_env_override(monkeypatch):
    monkeypatch.setenv("DEVICE", "rocm")
    monkeypatch.setenv("MEMORY_MODE", "model_offload")
    monkeypatch.setenv("API_URL", "ws://api:8080/api/v1/fleet")
    monkeypatch.setenv("FLEET_TOKEN", "fleet-secret")
    monkeypatch.setenv("TORCH_COMPILE", "1")
    monkeypatch.setenv("ATTENTION_BACKEND", "_native_efficient")
    monkeypatch.setenv("VRAM_RESERVE_MB", "2048")
    settings = Settings()
    assert settings.device == "rocm"
    assert settings.memory_mode == "model_offload"
    assert settings.api_url == "ws://api:8080/api/v1/fleet"
    assert settings.fleet_token == "fleet-secret"
    assert settings.torch_compile is True
    assert settings.attention_backend == "_native_efficient"
    assert settings.vram_reserve_mb == 2048


def test_vram_reserve_rejects_negative_value():
    with pytest.raises(ValidationError) as error:
        Settings(vram_reserve_mb=-1)
    assert error.value.errors()[0]["type"] == "greater_than_equal"
