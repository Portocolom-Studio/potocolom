import hashlib
import json
from pathlib import Path

import pytest

from worker import protocol6

VECTORS = Path(__file__).resolve().parents[2] / "docs" / "protocol6-vectors.json"


@pytest.fixture(scope="module")
def vectors():
    return json.loads(VECTORS.read_text())


def test_protocol6_accepts_frozen_paired_messages(vectors):
    for vector in vectors["paired_messages"]:
        raw = bytes.fromhex(vector["canonical_complete_utf8_hex"])
        control = protocol6.decode_control(raw)
        protocol6.validate_message(control)
        assert len(raw) == vector["complete_bytes"], vector["name"]
        assert hashlib.sha256(raw).hexdigest() == vector["complete_sha256"], vector["name"]


def test_protocol6_rejects_every_frozen_strict_decoder_negative(vectors):
    assert len(vectors["strict_decoder_negatives"]) == 380
    for vector in vectors["strict_decoder_negatives"]:
        encoded = vector.get("canonical_complete_utf8_hex", vector.get("input_utf8_hex"))
        raw = bytes.fromhex(encoded)
        with pytest.raises(ValueError):
            protocol6.validate_message(
                protocol6.decode_control(raw),
                expected_worker_id="worker-rocm" if vector["name"] == "open_wrong_target" else None,
            )


def test_worker_protocol6_matches_backend_byte_for_byte():
    backend_path = Path(__file__).resolve().parents[2] / "backend" / "app" / "protocol6.py"
    worker_path = Path(__file__).resolve().parents[1] / "worker" / "protocol6.py"
    assert backend_path.read_bytes() == worker_path.read_bytes()
