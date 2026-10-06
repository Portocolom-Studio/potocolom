"""What GET /api/v1/account says about how the account gets in."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.test_credentials import _link
from tests.test_totp_flow import ORIGIN, _csrf, _enrol, _login, _make, accounts

__all__ = ["accounts"]


@pytest.mark.db
def test_a_password_account_reports_one_identity_and_no_factor(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "overview@example.com")
        assert _login(client, "overview@example.com").status_code == 204
        me = client.get("/api/v1/account").json()
    assert me["identities"] == ["password"]
    assert me["totp"] is False


@pytest.mark.db
def test_an_enrolment_started_but_not_confirmed_stays_off(accounts):
    """Abandoned enrolment must not be reported as a factor, the way it does
    not gate the next sign-in."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "overviewpending@example.com")
        assert _login(client, "overviewpending@example.com").status_code == 204
        assert client.post("/api/v1/account/totp", headers=_csrf(client)).status_code == 200
        me = client.get("/api/v1/account").json()
    assert me["totp"] is False


@pytest.mark.db
def test_a_confirmed_factor_is_reported_as_on(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "overviewtotp@example.com")
        assert _login(client, "overviewtotp@example.com").status_code == 204
        _enrol(client)
        me = client.get("/api/v1/account").json()
    assert me["totp"] is True
    assert me["identities"] == ["password"]


@pytest.mark.db
def test_linked_providers_are_reported_sorted(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "overviewlinked@example.com")
        client.portal.call(_link, user.id, "github", "g-overview")
        assert _login(client, "overviewlinked@example.com").status_code == 204
        me = client.get("/api/v1/account").json()
    assert me["identities"] == ["github", "password"]
    assert me["totp"] is False


@pytest.mark.db
def test_a_provider_linked_twice_is_listed_once(accounts):
    """Two rows for one provider are two identities to the server but one
    door to the account, and the account view lists doors."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "overviewdupe@example.com")
        client.portal.call(_link, user.id, "github", "dupe-one")
        client.portal.call(_link, user.id, "github", "dupe-two")
        assert _login(client, "overviewdupe@example.com").status_code == 204
        me = client.get("/api/v1/account").json()
    assert me["identities"] == ["github", "password"]
    assert me["totp"] is False
