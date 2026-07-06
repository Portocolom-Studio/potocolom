from worker.settings import Settings


def test_defaults():
    settings = Settings()
    assert settings.device == "cpu"
    assert settings.api_url == "ws://localhost:8000/api/v1/fleet"


def test_env_override(monkeypatch):
    monkeypatch.setenv("DEVICE", "rocm")
    monkeypatch.setenv("API_URL", "ws://api:8080/api/v1/fleet")
    settings = Settings()
    assert settings.device == "rocm"
    assert settings.api_url == "ws://api:8080/api/v1/fleet"
