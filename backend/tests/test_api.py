from fastapi.testclient import TestClient

from app import auth_mode
from app.auth_mode import sign_in_methods
from app.main import app
from app.settings import Settings

client = TestClient(app)


def test_health():
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_answers_head():
    # The load-balancer probe is a HEAD, not a GET (docs/api.md).
    response = client.head("/api/v1/health")
    assert response.status_code == 200


def test_head_is_not_every_get_route():
    # Only the health route takes HEAD; the rest keep the framework's 405.
    assert client.head("/api/v1/config").status_code == 405


def test_config_defaults():
    response = client.get("/api/v1/config")
    assert response.status_code == 200
    body = response.json()
    assert body["auth_methods"] == []
    assert body["billing_enabled"] is False
    assert body["languages"] == ["en", "es"]


def test_auth_methods_by_mode():
    try:
        auth_mode.load(auth_mode.NONE)
        assert sign_in_methods(Settings(oauth_providers="google,github")) == []
        auth_mode.load(auth_mode.UNCLAIMED)
        assert sign_in_methods(Settings(oauth_providers="google,github")) == []
        auth_mode.load(auth_mode.ACCOUNTS)
        assert sign_in_methods(Settings(oauth_providers="")) == ["password"]
        # Naming a provider is not the same as being able to use one. The frontend
        # renders a button for every method here, and a button that cannot
        # complete is worse than no button.
        assert sign_in_methods(Settings(oauth_providers="google,github")) == ["password"]
        configured = {
            "google_client_id": "g", "google_client_secret": "gs",
            "github_client_id": "h", "github_client_secret": "hs",
        }
        assert sign_in_methods(Settings(oauth_providers="google,github",
                                        **configured)) == ["password", "google", "github"]
        # An unsupported provider is dropped rather than advertised.
        assert sign_in_methods(Settings(oauth_providers="google,facebook",
                                        **configured)) == ["password", "google"]
        # Whitespace around commas must not leak into method names.
        assert sign_in_methods(Settings(oauth_providers="google, github, ",
                                        **configured)) == ["password", "google", "github"]
        # Half a configuration is not a configuration.
        assert sign_in_methods(Settings(oauth_providers="google",
                                        google_client_id="g")) == ["password"]
    finally:
        auth_mode.clear()


def test_telemetry_setting():
    assert Settings.model_fields["telemetry"].default is True
    assert Settings(telemetry=True).telemetry is True
    assert Settings(telemetry=False).telemetry is False
