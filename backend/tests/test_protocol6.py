import hashlib
import json
from pathlib import Path

import pytest

from app import realtime
from app.protocol6 import (
    canonical_bytes,
    decode_control,
    preflight_worker_control,
    validate_message,
)

# Candidate tests left out of this file, and why:
# - test_physical_inventory_preflight_matches_frozen_complete_envelopes needs
#   worker_authority.preflight_physical_inventory, which belongs to the drain
#   and physical-range slice.
# - test_protocol_like_names_in_schema_approved_params_remain_data needs
#   app.schema_work.validate_params, which this slice does not port.
# - test_protocol6_fleet_manifest_compile_runs_in_bounded_work_owner needs
#   realtime.parse_worker_manifests and the schema work pool. The $ref refusal
#   it asserts is covered end to end by the fleet socket test instead.

VECTORS = Path(__file__).resolve().parents[2] / "docs" / "protocol6-vectors.json"


@pytest.fixture(scope="module")
def vectors():
    return json.loads(VECTORS.read_text())


def test_protocol6_accepts_frozen_paired_messages(vectors):
    for vector in vectors["paired_messages"]:
        raw = bytes.fromhex(vector["canonical_complete_utf8_hex"])
        control = decode_control(raw)
        validate_message(control)
        assert len(raw) == vector["complete_bytes"], vector["name"]
        assert hashlib.sha256(raw).hexdigest() == vector["complete_sha256"], vector["name"]


def test_protocol6_rejects_every_frozen_strict_decoder_negative(vectors):
    assert len(vectors["strict_decoder_negatives"]) == 380
    for vector in vectors["strict_decoder_negatives"]:
        encoded = vector.get("canonical_complete_utf8_hex", vector.get("input_utf8_hex"))
        raw = bytes.fromhex(encoded)
        with pytest.raises(ValueError):
            validate_message(
                decode_control(raw),
                expected_worker_id="worker-rocm" if vector["name"] == "open_wrong_target" else None,
            )


def test_protocol6_command_hash_covers_source_body(vectors):
    for vector in vectors["commands"]:
        control = vector["control"]
        without_hash = canonical_bytes({key: value for key, value in control.items()
                                        if key != "body_hash"})
        assert hashlib.sha256(without_hash).hexdigest() == vector["body_sha256"]
        assert canonical_bytes(control) == bytes.fromhex(vector["canonical_complete_utf8_hex"])


def test_protocol6_preflight_enforces_the_frozen_control_byte_limits(vectors):
    # The half of the candidate's inventory test that only needs protocol6:
    # the byte limits on a hello-sized control, from the same frozen vectors.
    boundary = vectors["physical_inventory_boundary"]
    at_limit = boundary["at_limit"]
    next_candidate = boundary["next_candidate"]
    assert at_limit["complete_bytes"] == 16_121
    assert next_candidate["complete_bytes"] == 16_454
    assert len(preflight_worker_control(at_limit["control"])) == at_limit["complete_bytes"]
    with pytest.raises(ValueError, match="byte limit"):
        preflight_worker_control(next_candidate["control"])


def test_protocol6_fleet_parser_rejects_duplicate_keys_before_effects():
    raw = '{"type":"heartbeat","slots_in_use":1,"slots_in_use":2}'
    with pytest.raises(realtime.ProtocolError):
        realtime.parse_control(raw, protocol_version=6)


def test_legacy_fleet_parser_keeps_existing_json_compatibility():
    assert realtime.parse_control('{"type":"legacy","n":1,"n":2}') == {
        "type": "legacy",
        "n": 2,
    }
