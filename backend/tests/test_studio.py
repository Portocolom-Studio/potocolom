from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_studio_gpu_requires_worker():
    response = client.get("/api/v1/studio/gpu")
    assert response.status_code == 503


def test_studio_gpu_returns_worker_snapshot(monkeypatch):
    async def fake_gpu_command(worker, command, timeout=15.0):
        return {
            "loaded_models": ["sd-sim"],
            "gpu": {
                "device": "cpu",
                "available": False,
            },
        }

    monkeypatch.setattr("app.studio.gpu_command", fake_gpu_command)
    monkeypatch.setattr("app.studio.pick_any_worker", lambda: object())

    response = client.get("/api/v1/studio/gpu")
    assert response.status_code == 200
    body = response.json()
    assert body["loaded_models"] == ["sd-sim"]
    assert body["gpu"]["device"] == "cpu"
