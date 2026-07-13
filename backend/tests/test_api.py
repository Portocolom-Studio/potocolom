from fastapi.testclient import TestClient

from app.main import app
from app.settings import Settings

client = TestClient(app)


def test_health():
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_config_defaults():
    response = client.get("/api/v1/config")
    assert response.status_code == 200
    body = response.json()
    assert body["auth_methods"] == []
    assert body["billing_enabled"] is False
    assert body["languages"] == ["en", "es"]


def test_auth_methods_by_mode():
    assert Settings(auth_mode="none").auth_methods == []
    assert Settings(auth_mode="local").auth_methods == ["local"]
    assert Settings(auth_mode="oauth", oauth_providers="google,github").auth_methods == [
        "local",
        "google",
        "github",
    ]
    # Whitespace around commas must not leak into method names.
    assert Settings(auth_mode="oauth", oauth_providers="google, github, ").auth_methods == [
        "local",
        "google",
        "github",
    ]
