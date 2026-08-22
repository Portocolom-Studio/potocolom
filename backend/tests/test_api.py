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
    assert Settings(auth_mode="accounts").auth_methods == ["password"]
    # Naming a provider is not the same as being able to use one. The frontend
    # renders a button for every method here, and a button that cannot
    # complete is worse than no button.
    assert Settings(auth_mode="accounts", oauth_providers="google,github").auth_methods == [
        "password",
    ]
    configured = {
        "google_client_id": "g", "google_client_secret": "gs",
        "github_client_id": "h", "github_client_secret": "hs",
    }
    assert Settings(auth_mode="accounts", oauth_providers="google,github",
                    **configured).auth_methods == ["password", "google", "github"]
    # An unsupported provider is dropped rather than advertised.
    assert Settings(auth_mode="accounts", oauth_providers="google,facebook",
                    **configured).auth_methods == ["password", "google"]
    # Whitespace around commas must not leak into method names.
    assert Settings(auth_mode="accounts", oauth_providers="google, github, ",
                    **configured).auth_methods == ["password", "google", "github"]
    # Half a configuration is not a configuration.
    assert Settings(auth_mode="accounts", oauth_providers="google",
                    google_client_id="g").auth_methods == ["password"]


def test_telemetry_setting():
    assert Settings.model_fields["telemetry"].default is True
    assert Settings(telemetry=True).telemetry is True
    assert Settings(telemetry=False).telemetry is False
