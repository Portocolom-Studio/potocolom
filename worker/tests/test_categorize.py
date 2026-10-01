import io
import json
import logging

from PIL import Image

from worker import categorize as categorizer
from worker.categorize import LABELS, categorize_output
from worker.client import build_runtime
from worker.engine import SimulatedEngine
from worker.settings import Settings


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def _manifest_json() -> str:
    return json.dumps({
        "id": "local-model",
        "name": "Local model",
        "capabilities": ["text_to_image"],
        "source": "/models/local-model",
    })


def test_labels_are_the_fixed_set():
    assert LABELS == ("art", "photo_edit", "design", "character", "nsfw", "other")


def test_disabled_categorizer_answers_other_without_loading(monkeypatch):
    def explode():
        raise AssertionError("the simulated path must not load the categorizer")

    monkeypatch.setattr(categorizer, "_enabled", False)
    monkeypatch.setattr(categorizer, "_load", explode)
    assert categorize_output(_png()) == ("other", None)


def test_no_image_answers_other_even_when_enabled(monkeypatch):
    monkeypatch.setattr(categorizer, "_enabled", True)
    assert categorize_output(None) == ("other", None)


def test_a_top_score_above_the_threshold_is_its_label(monkeypatch):
    pictures = []

    def fake_score(_state, picture):
        pictures.append(picture)
        return {"art": 0.87123, "photo_edit": 0.4, "design": 0.3,
                "character": 0.2, "nsfw": 0.1}

    monkeypatch.setattr(categorizer, "_enabled", True)
    monkeypatch.setattr(categorizer, "_load", lambda: object())
    monkeypatch.setattr(categorizer, "_score", fake_score)

    assert categorize_output(_png()) == ("art", 0.8712)
    assert pictures[0].mode == "RGB"


def test_a_top_score_below_the_threshold_is_other(monkeypatch):
    def fake_score(_state, _picture):
        return {"art": 0.0421, "photo_edit": 0.03, "design": 0.02,
                "character": 0.01, "nsfw": 0.005}

    monkeypatch.setattr(categorizer, "_enabled", True)
    monkeypatch.setattr(categorizer, "_load", lambda: object())
    monkeypatch.setattr(categorizer, "_score", fake_score)

    assert categorize_output(_png()) == ("other", 0.0421)


def test_undecodable_bytes_answer_other_without_loading(monkeypatch):
    def explode():
        raise AssertionError("an image that will not decode must not reach the model")

    monkeypatch.setattr(categorizer, "_enabled", True)
    monkeypatch.setattr(categorizer, "_load", explode)
    assert categorize_output(b"not an image") == ("other", None)


def test_a_loader_failure_answers_other_and_logs_once(monkeypatch, caplog):
    def fail():
        raise RuntimeError("weights unavailable")

    monkeypatch.setattr(categorizer, "_enabled", True)
    monkeypatch.setattr(categorizer, "_load_failure_logged", False)
    monkeypatch.setattr(categorizer, "_load", fail)

    with caplog.at_level(logging.ERROR, logger="potocolom.worker"):
        first = categorize_output(_png())
        second = categorize_output(_png())

    assert first == ("other", None)
    assert second == ("other", None)
    failures = [record for record in caplog.records
                if "categorizer model load failed" in record.getMessage()]
    assert len(failures) == 1


def test_a_failed_load_is_not_retried_until_the_wait_has_passed(monkeypatch):
    """An offline install must not pay a failing download on every job."""
    calls = []

    def fail():
        calls.append(1)
        raise RuntimeError("offline")

    clock = [1000.0]
    monkeypatch.setattr(categorizer, "_enabled", True)
    monkeypatch.setattr(categorizer, "_loaded", None)
    monkeypatch.setattr(categorizer, "_retry_after", 0.0)
    monkeypatch.setattr(categorizer, "_load_failure_logged", True)
    monkeypatch.setattr(categorizer, "_load_model", fail)
    monkeypatch.setattr(categorizer.time, "monotonic", lambda: clock[0])

    categorize_output(_png())
    categorize_output(_png())
    assert len(calls) == 1
    clock[0] += categorizer.LOAD_RETRY_SECONDS + 1
    categorize_output(_png())
    assert len(calls) == 2


def test_the_simulated_engine_path_never_loads_the_model(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("the simulated engine must not enable the categorizer")

    monkeypatch.setattr(categorizer, "_enabled", False)
    monkeypatch.setattr(categorizer, "_load", explode)

    _manifests, engine = build_runtime(Settings(worker_id="w-sim"))

    assert isinstance(engine, SimulatedEngine)
    assert categorizer._enabled is False
    assert categorize_output(_png()) == ("other", None)


def test_building_the_real_engine_enables_the_categorizer(monkeypatch, tmp_path):
    class FakeDiffusersEngine:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "local-model.json").write_text(_manifest_json())
    monkeypatch.setattr("worker.engine.DiffusersEngine", FakeDiffusersEngine)
    monkeypatch.setattr(categorizer, "_enabled", False)

    _manifests, engine = build_runtime(Settings(worker_id="w-real",
                                                models_dir=str(models_dir)))

    assert isinstance(engine, FakeDiffusersEngine)
    assert categorizer._enabled is True
