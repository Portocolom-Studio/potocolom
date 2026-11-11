from worker.settings import Settings, fleet_url_error
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


PUBLIC_WS = "ws://fleet.example.com:8000/api/v1/fleet"


def test_ws_to_public_host_is_refused():
    error = fleet_url_error(PUBLIC_WS, allow_insecure=False)
    assert error is not None
    assert "ALLOW_INSECURE_FLEET" in error


def test_ws_to_private_ip_literal_is_refused():
    assert fleet_url_error("ws://192.168.1.10:8000/api/v1/fleet", allow_insecure=False) is not None


def test_wss_to_public_host_is_accepted():
    assert fleet_url_error("wss://studio.example.com/api/v1/fleet", allow_insecure=False) is None


@pytest.mark.parametrize("url", [
    "ws://localhost:8000/api/v1/fleet",
    "ws://127.0.0.1:8000/api/v1/fleet",
    "ws://[::1]:8000/api/v1/fleet",
    "ws://api:8080/api/v1/fleet",
])
def test_ws_to_local_host_is_accepted(url):
    assert fleet_url_error(url, allow_insecure=False) is None


def test_opt_in_accepts_ws_to_public_host():
    assert fleet_url_error(PUBLIC_WS, allow_insecure=True) is None


def test_allow_insecure_fleet_defaults_off_and_reads_env(monkeypatch):
    assert Settings().allow_insecure_fleet is False
    monkeypatch.setenv("ALLOW_INSECURE_FLEET", "1")
    assert Settings().allow_insecure_fleet is True


@pytest.mark.parametrize("url", [
    "ws://134744072:8000/api/v1/fleet",  # 8.8.8.8 as one integer: the resolver takes it as an address
    "ws://0x08080808:8000/api/v1/fleet",
])
def test_ws_to_a_numeric_single_label_host_is_refused(url):
    assert fleet_url_error(url, allow_insecure=False) is not None


@pytest.mark.parametrize("url", [
    # IDNA, which the client applies before it connects, reads both as a period.
    "ws://fleet\u3002example\u3002com:8000/api/v1/fleet",
    "ws://fleet\uff0eexample\uff0ecom:8000/api/v1/fleet",
])
def test_ws_to_a_host_with_unicode_label_separators_is_refused(url):
    assert fleet_url_error(url, allow_insecure=False) is not None
