import importlib.util
import io
import uuid
from pathlib import Path
from types import ModuleType

from PIL import Image


def load_script() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "profile-two-session-e2e.py"
    spec = importlib.util.spec_from_file_location("profile_two_session_e2e", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCRIPT = load_script()


def test_canvas_bytes_is_a_512_png() -> None:
    payload = SCRIPT.canvas_bytes()
    with Image.open(io.BytesIO(payload)) as image:
        assert image.format == "PNG"
        assert image.size == (512, 512)
        assert image.convert("RGB").size == (512, 512)


def test_canvas_frame_keeps_image_openable() -> None:
    session_id = uuid.UUID("12345678-1234-1234-1234-123456789abc")
    image = SCRIPT.canvas_bytes()
    frame = SCRIPT.canvas_frame(session_id, image)
    assert frame[0] == SCRIPT.CANVAS_FRAME
    assert frame[1:SCRIPT.FRAME_HEADER_BYTES] == session_id.bytes
    with Image.open(io.BytesIO(frame[SCRIPT.FRAME_HEADER_BYTES:])) as opened:
        assert opened.size == (512, 512)


def test_percentile_nearest_rank_on_twenty_samples() -> None:
    values = [float(index) for index in range(1, 21)]
    assert SCRIPT.percentile(values, 95.0) == 19.0
    assert SCRIPT.percentile([], 95.0) == 0.0


def test_inside_bar_needs_every_session() -> None:
    assert SCRIPT.inside_bar([140.0, 155.0]) is True
    assert SCRIPT.inside_bar([500.0, 500.0]) is True
    assert SCRIPT.inside_bar([140.0, 501.0]) is False
    assert SCRIPT.inside_bar([]) is False


def test_recv_gaps_use_the_spread_in_each_round() -> None:
    gaps = SCRIPT.recv_gaps([
        [1.000, 2.000, 3.000],
        [1.010, 2.040, 3.005],
    ])
    assert [round(value, 1) for value in gaps] == [10.0, 40.0, 5.0]
    assert SCRIPT.recv_gaps([[1.0]]) == []
    assert SCRIPT.recv_gaps([[], [1.0]]) == []


def test_session_stats_round_trip() -> None:
    report = SCRIPT.session_stats([100.0, 120.0, 110.0], sent=3, received=3)
    assert report["sent"] == 3
    assert report["received"] == 3
    assert report["rtt_median_ms"] == 110.0
    assert report["rtt_max_ms"] == 120.0
    assert report["samples_rtt_ms"] == [100.0, 120.0, 110.0]


def test_advertised_slots_reads_the_last_warmup_line() -> None:
    log = (
        "INFO potocolom.worker: warmup realtime model=sdxl-turbo slots=1\n"
        "INFO potocolom.worker: warmup realtime model=vega-rt slots=2\n"
        "INFO potocolom.worker: warmup realtime model=sdxl-turbo slots=3\n"
    )
    assert SCRIPT.advertised_slots(log, "sdxl-turbo") == 3
    assert SCRIPT.advertised_slots(log, "vega-rt") == 2
    assert SCRIPT.advertised_slots(log, "missing") is None


def test_is_no_capacity() -> None:
    assert SCRIPT.is_no_capacity({"type": "error", "code": 4003}) is True
    assert SCRIPT.is_no_capacity({"type": "error", "code": 4004}) is False
    assert SCRIPT.is_no_capacity({"type": "ready"}) is False
    assert SCRIPT.is_no_capacity(type("Closed", (), {"code": 4003})()) is True
    assert SCRIPT.is_no_capacity("nope") is False
