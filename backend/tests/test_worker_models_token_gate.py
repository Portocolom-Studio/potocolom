"""The install route passes the worker's `needs_token` flag through (WS1.6).

The worker decides `needs_token` per repository from the files a model still has to fetch.
The API only reads that flag. This test sends a row that reports no token needed, and checks
that the install is sent to the worker.

One case is enough here, because the API does not look at the cache. The two cache states
that set the flag are proven at worker level in worker/tests/test_model_store.py, where
`probe_model` must return needs_token False for each real cache state: a complete gated cache
(test_a_complete_gated_cache_installs_without_a_token) and a gated model whose only missing
files are ungated companions (test_a_gated_model_missing_only_ungated_companions_installs_without_a_token).
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.test_fleet_protocol6 import FLEET_HEADERS, drop_worker_rows
from tests.test_totp_flow import ORIGIN, _csrf, accounts
from tests.test_worker_models import (
    _admin_as,
    _wait_until_manageable,
    hello_for,
    inventory_for,
    manifest_row,
)

__all__ = ["accounts"]


@pytest.mark.db
def test_a_gated_model_the_worker_reports_as_tokenless_is_sent_to_the_worker(accounts):
    worker_id, model_id = "w-token-companion", "gated-companion-missing"
    row = manifest_row(model_id, needs_token=False)
    with TestClient(app, base_url=ORIGIN, headers=FLEET_HEADERS) as client:
        _admin_as(client, f"{worker_id}-admin@example.com")
        with client.websocket_connect("/api/v1/fleet", headers=FLEET_HEADERS) as ws:
            ws.send_json(hello_for(worker_id, capabilities=["model_store"]))
            assert ws.receive_json()["type"] == "registered"
            ws.send_json(inventory_for([row]))
            _wait_until_manageable(client, worker_id)
            response = client.post(
                f"/api/v1/admin/workers/{worker_id}/models/{model_id}/install",
                headers=_csrf(client))
            assert response.status_code == 202
            command = ws.receive_json()
            assert command["type"] == "install_model"
            assert command["model_id"] == model_id
            client.portal.call(drop_worker_rows, worker_id)
