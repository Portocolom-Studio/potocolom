from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from typing import Any

MAX_HELLO_BYTES = 2 * 1024 * 1024
MAX_PARAMETER_CONTROL_BYTES = 1024 * 1024
MAX_WORKER_CONTROL_BYTES = 16 * 1024
MAX_COUNTER = 2**63 - 1

# Closed fields copied from the frozen C2-wire-v4 message_shapes catalogue.
_SHAPE_ROWS = (
    ('open_session', 'attempt_id,body_hash,command_id,command_sequence,control_generation,grant_nonce,incarnation,lease_expires_at,lease_id,model_id,owner_epoch,params,region,session_id,type,work_budget,worker_id', ''),
    ('update_session', 'attempt_id,body_hash,command_id,command_sequence,control_generation,grant_nonce,incarnation,lease_expires_at,lease_id,model_id,owner_epoch,params,params_revision,region,session_id,type,worker_id', ''),
    ('close_session', 'attempt_id,body_hash,command_id,command_sequence,control_generation,grant_nonce,incarnation,lease_expires_at,lease_id,owner_epoch,region,session_id,type,worker_id', ''),
    ('dispatch_job', 'body_hash,command_id,command_sequence,dispatch_sequence,dispatch_token,grant_nonce,incarnation,job_id,lease_expires_at,lease_id,model_id,owner_epoch,params,region,type,upload,work_budget,worker_id', 'input,thumb_upload'),
    ('cancel_job', 'body_hash,command_id,command_sequence,dispatch_sequence,dispatch_token,grant_nonce,incarnation,job_id,lease_expires_at,lease_id,owner_epoch,region,type,worker_id', ''),
    ('pause_job', 'body_hash,command_id,command_sequence,dispatch_sequence,dispatch_token,grant_nonce,incarnation,job_id,lease_expires_at,lease_id,owner_epoch,region,type,worker_id', ''),
    ('resume_job', 'body_hash,checkpoint,command_id,command_sequence,dispatch_sequence,dispatch_token,grant_nonce,incarnation,job_id,lease_expires_at,lease_id,model_id,owner_epoch,params,parent_dispatch_sequence,region,type,upload,work_budget,worker_id', 'input,thumb_upload'),
    ('drain_worker', 'body_hash,command_id,command_sequence,grant_nonce,incarnation,lease_expires_at,lease_id,owner_epoch,region,type,worker_id', ''),
    ('hello', 'capabilities,compatible_versions,device,grant_nonce,incarnation,memory_mode,models,protocol_version,realtime_slots,type,worker_id', 'previous_incarnation,realtime_batch_ms,realtime_p95_ms'),
    ('registered', 'grant_nonce,incarnation,lease_expires_at,lease_id,owner_epoch,protocol_version,ready,region,remaining_ms,transport_owner_id,type,worker_id', ''),
    ('grant_request', 'grant_nonce,incarnation,region,type,worker_id', ''),
    ('work_grant', 'grant_nonce,incarnation,lease_expires_at,lease_id,owner_epoch,ready,region,remaining_ms,type,worker_id', ''),
    ('command_ack', 'attempt_id,body_hash,command_id,command_sequence,control_generation,grant_nonce,incarnation,lease_expires_at,lease_id,owner_epoch,region,session_id,status,type,worker_id', ''),
    ('checkpoint', 'attempt_id,control_generation,duration_ms,frames,gpu_ms,incarnation,owner_epoch,range_id,region,report_hash,report_sequence,session_id,type,worker_id', ''),
    ('checkpoint_ack', 'attempt_id,control_generation,duration_ms,frames,gpu_ms,incarnation,owner_epoch,range_id,region,report_hash,report_sequence,session_id,status,type,worker_id', ''),
    ('rejected', 'min_supported_version,reason,type', ''),
    ('heartbeat', 'frame_p95_ms,gpu,incarnation,loaded_models,region,slots_in_use,type,worker_id', 'memory_mode'),
    ('session_ready', 'attempt_id,control_generation,grant_nonce,incarnation,lease_expires_at,lease_id,owner_epoch,params_revision,region,session_id,type,worker_id', ''),
    ('session_refused', 'attempt_id,code,control_generation,grant_nonce,incarnation,lease_expires_at,lease_id,owner_epoch,region,session_id,type,worker_id', ''),
    ('session_closed', 'attempt_id,category,control_generation,duration_ms,frames,gpu_ms,grant_nonce,incarnation,lease_expires_at,lease_id,owner_epoch,physical_complete,range_id,region,report_hash,report_sequence,session_id,type,worker_id', 'category_score'),
    ('job_done', 'category,dispatch_sequence,dispatch_token,duration_ms,gpu_ms,grant_nonce,height,images,incarnation,job_id,lease_expires_at,lease_id,owner_epoch,physical_complete,range_id,region,report_hash,report_sequence,type,width,worker_id', 'category_score,has_thumbnail,input_fetch_ms,load_ms,postprocess_ms'),
    ('job_failed', 'dispatch_sequence,dispatch_token,duration_ms,failure_code,gpu_ms,grant_nonce,images,incarnation,job_id,lease_expires_at,lease_id,owner_epoch,physical_complete,range_id,reason,region,report_hash,report_sequence,type,worker_id', ''),
    ('job_cancelled', 'dispatch_sequence,dispatch_token,duration_ms,gpu_ms,grant_nonce,images,incarnation,job_id,lease_expires_at,lease_id,owner_epoch,physical_complete,range_id,region,report_hash,report_sequence,type,worker_id', ''),
    ('job_paused', 'checkpoint,dispatch_sequence,dispatch_token,duration_ms,gpu_ms,grant_nonce,images,incarnation,job_id,lease_expires_at,lease_id,owner_epoch,physical_complete,range_id,region,report_hash,report_sequence,type,worker_id', ''),
    ('job_progress', 'dispatch_sequence,dispatch_token,grant_nonce,incarnation,job_id,lease_expires_at,lease_id,owner_epoch,phase,progress,region,step,steps,type,worker_id', ''),
    ('worker_drained', 'active_attempt_count,drain_id,final_reports,incarnation,owner_epoch,physical_complete,region,type,worker_id', ''),
    ('predecessor_report', 'incarnation,previous_incarnation,region,report,type,worker_id', ''),
    ('prepare_realtime_class', 'attempt_id,control_generation,envelope_revision,grant_nonce,incarnation,lease_expires_at,lease_id,model_id,model_revision,owner_epoch,params,params_hash,params_revision,region,request_id,session_id,type,worker_id', ''),
    ('class_prepared', 'attempt_id,class_components,class_id,control_generation,envelope_revision,grant_nonce,incarnation,lease_expires_at,lease_id,measured_envelope_id,model_id,model_revision,owner_epoch,params_hash,params_revision,region,request_id,serial_cost_ms,session_id,type,worker_id', ''),
    ('class_refused', 'attempt_id,code,control_generation,envelope_revision,grant_nonce,incarnation,lease_expires_at,lease_id,model_id,model_revision,owner_epoch,params_hash,params_revision,region,request_id,session_id,type,worker_id', ''),
    ('gpu_status', 'incarnation,region,request_id,type,worker_id', ''),
    ('gpu_status', 'envelope_revision,gpu,incarnation,loaded_models,region,request_id,type,worker_id', ''),
    ('load_model', 'body_hash,command_id,command_sequence,grant_nonce,incarnation,lease_expires_at,lease_id,model_id,model_revision,owner_epoch,profile,region,request_id,type,worker_id', ''),
    ('unload_model', 'body_hash,command_id,command_sequence,grant_nonce,incarnation,lease_expires_at,lease_id,model_id,owner_epoch,region,request_id,type,worker_id', ''),
    ('model_loaded', 'envelope_revision,grant_nonce,incarnation,lease_expires_at,lease_id,loaded_models,model_id,owner_epoch,region,request_id,type,worker_id', 'load_ms'),
    ('model_unloaded', 'envelope_revision,grant_nonce,incarnation,lease_expires_at,lease_id,loaded_models,model_id,owner_epoch,region,request_id,type,worker_id', ''),
    ('gpu_error', 'envelope_revision,failure_code,grant_nonce,incarnation,lease_expires_at,lease_id,loaded_models,model_id,owner_epoch,reason,region,request_id,type,worker_id', ''),
    ('checkpoint_ack', 'attempt_id,code,control_generation,duration_ms,frames,gpu_ms,incarnation,owner_epoch,range_id,region,report_hash,report_sequence,session_id,status,type,worker_id', ''),
    ('command_ack', 'attempt_id,body_hash,code,command_id,command_sequence,control_generation,grant_nonce,incarnation,lease_expires_at,lease_id,owner_epoch,region,session_id,status,type,worker_id', ''),
    ('command_ack', 'attempt_id,body_hash,code,command_id,command_sequence,control_generation,expected_sequence,grant_nonce,incarnation,lease_expires_at,lease_id,owner_epoch,region,session_id,status,type,worker_id', ''),
    ('registered', 'protocol_version,type', ''),
    ('checkpoint', 'dispatch_sequence,duration_ms,gpu_ms,images,incarnation,job_id,owner_epoch,range_id,region,report_hash,report_sequence,type,worker_id', ''),
    ('checkpoint_ack', 'dispatch_sequence,duration_ms,gpu_ms,images,incarnation,job_id,owner_epoch,range_id,region,report_hash,report_sequence,status,type,worker_id', ''),
    ('checkpoint_ack', 'code,dispatch_sequence,duration_ms,gpu_ms,images,incarnation,job_id,owner_epoch,range_id,region,report_hash,report_sequence,status,type,worker_id', ''),
    ('command_ack', 'body_hash,command_id,command_sequence,dispatch_sequence,grant_nonce,incarnation,job_id,lease_expires_at,lease_id,owner_epoch,region,status,type,worker_id', ''),
    ('command_ack', 'body_hash,code,command_id,command_sequence,dispatch_sequence,grant_nonce,incarnation,job_id,lease_expires_at,lease_id,owner_epoch,region,status,type,worker_id', ''),
    ('command_ack', 'body_hash,command_id,command_sequence,grant_nonce,incarnation,lease_expires_at,lease_id,model_id,owner_epoch,region,request_id,status,type,worker_id', ''),
    ('command_ack', 'body_hash,code,command_id,command_sequence,grant_nonce,incarnation,lease_expires_at,lease_id,model_id,owner_epoch,region,request_id,status,type,worker_id', ''),
    ('command_ack', 'body_hash,code,command_id,command_sequence,dispatch_sequence,expected_sequence,grant_nonce,incarnation,job_id,lease_expires_at,lease_id,owner_epoch,region,status,type,worker_id', ''),
    ('command_ack', 'body_hash,code,command_id,command_sequence,expected_sequence,grant_nonce,incarnation,lease_expires_at,lease_id,model_id,owner_epoch,region,request_id,status,type,worker_id', ''),
    ('command_ack', 'body_hash,command_id,command_sequence,grant_nonce,incarnation,lease_expires_at,lease_id,owner_epoch,region,status,type,worker_id', ''),
    ('command_ack', 'body_hash,code,command_id,command_sequence,grant_nonce,incarnation,lease_expires_at,lease_id,owner_epoch,region,status,type,worker_id', ''),
    ('command_ack', 'body_hash,code,command_id,command_sequence,expected_sequence,grant_nonce,incarnation,lease_expires_at,lease_id,owner_epoch,region,status,type,worker_id', ''),
)
_SHAPES: dict[str, tuple[tuple[frozenset[str], frozenset[str]], ...]] = {}
for _type, _required, _optional in _SHAPE_ROWS:
    _SHAPES.setdefault(_type, ())
    _SHAPES[_type] += ((frozenset(_required.split(",")), frozenset(_optional.split(","))),)


class Protocol6Error(ValueError):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Protocol6Error("duplicate JSON object key")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise Protocol6Error("non-finite JSON number")


def _check_json(value: Any, depth: int = 0) -> None:
    if depth > 64:
        raise Protocol6Error("JSON value is too deeply nested")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise Protocol6Error("non-finite JSON number")
        return
    if isinstance(value, str):
        try:
            value.encode("utf-8", "strict")
        except UnicodeError:
            raise Protocol6Error("invalid Unicode") from None
        return
    if isinstance(value, list):
        for item in value:
            _check_json(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise Protocol6Error("object keys must be strings")
            _check_json(key, depth + 1)
            _check_json(item, depth + 1)
        return
    raise Protocol6Error("value is not JSON")


def canonical_bytes(value: Any) -> bytes:
    _check_json(value)
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise Protocol6Error("value has no canonical JSON encoding") from None


def preflight_worker_control(
    control: dict[str, Any],
    *,
    expected_worker_id: str | None = None,
    expected_incarnation: uuid.UUID | None = None,
) -> bytes:
    validate_message(
        control,
        expected_worker_id=expected_worker_id,
        expected_incarnation=expected_incarnation,
    )
    body = canonical_bytes(control)
    if len(body) > message_limit(control):
        raise Protocol6Error("control exceeds byte limit")
    return body


def message_limit(control: Mapping[str, Any]) -> int:
    kind = control.get("type")
    if kind == "hello":
        return MAX_HELLO_BYTES
    if kind in {"open_session", "update_session", "dispatch_job", "resume_job",
                "prepare_realtime_class"}:
        return MAX_PARAMETER_CONTROL_BYTES
    return MAX_WORKER_CONTROL_BYTES


def decode_control(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8", "strict")
        except UnicodeError:
            raise Protocol6Error("invalid UTF-8") from None
        data = raw
    elif isinstance(raw, str):
        try:
            data = raw.encode("utf-8", "strict")
        except UnicodeError:
            raise Protocol6Error("invalid Unicode") from None
        text = raw
    else:
        raise Protocol6Error("control must be UTF-8 text")
    if not data or len(data) > MAX_HELLO_BYTES:
        raise Protocol6Error("control size is invalid")
    try:
        control = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except Protocol6Error:
        raise
    except (ValueError, RecursionError):
        raise Protocol6Error("malformed JSON") from None
    if not isinstance(control, dict):
        raise Protocol6Error("control must be an object")
    _check_json(control)
    if len(data) > message_limit(control):
        raise Protocol6Error("control exceeds its byte limit")
    return control


_UUID_FIELDS = frozenset({"incarnation", "previous_incarnation", "grant_nonce", "lease_id",
                          "transport_owner_id", "command_id", "session_id", "attempt_id",
                          "job_id", "range_id", "drain_id", "request_id", "measured_envelope_id"})
_POSITIVE = frozenset({"protocol_version", "owner_epoch", "command_sequence", "control_generation",
                       "params_revision", "dispatch_sequence", "parent_dispatch_sequence",
                       "report_sequence", "expected_sequence", "envelope_revision", "schema_version",
                       "max_bytes", "width", "height", "steps", "serial_cost_ms"})
_NONNEGATIVE = frozenset({"min_supported_version", "realtime_slots", "slots_in_use", "gpu_ms", "frames",
                          "images", "duration_ms", "active_attempt_count", "input_fetch_ms", "load_ms",
                          "postprocess_ms", "vram_used_bytes", "vram_total_bytes", "util_pct",
                          "vram_used_pct", "remaining_ms", "start_gpu_ms", "byte_size"})
_HASH_FIELDS = frozenset({"body_hash", "report_hash", "params_hash", "class_id", "checkpoint_hash", "sha256"})
_STRING_FIELDS = frozenset({"worker_id", "region", "device", "model_id", "model_revision", "memory_mode",
                            "reason", "status", "code", "category", "phase", "failure_code",
                            "dispatch_token", "profile", "selection", "dtype", "guidance_mode", "kind",
                            "compatibility_id", "url", "name", "type"})
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_RFC3339 = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z")


def _integer(value: Any, positive: bool = False) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)
            and (value > 0 if positive else value >= 0) and value <= MAX_COUNTER)


def _uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _validate_types(value: Any, key: str = "") -> None:
    if key in _UUID_FIELDS and not _uuid(value):
        raise Protocol6Error("identity must be a canonical UUID")
    if key in _POSITIVE and not _integer(value, True):
        raise Protocol6Error("counter must be a positive signed63 integer")
    if key in _NONNEGATIVE and not _integer(value):
        raise Protocol6Error("counter must be a nonnegative signed63 integer")
    if key in _HASH_FIELDS and (not isinstance(value, str) or not _HEX64.fullmatch(value)):
        raise Protocol6Error("hash must be lowercase SHA256 hex")
    if key in _STRING_FIELDS and not isinstance(value, str):
        raise Protocol6Error("field must be a string")
    if key == "lease_expires_at" and (not isinstance(value, str) or not _RFC3339.fullmatch(value)):
        raise Protocol6Error("lease expiry must be UTC RFC3339")
    if key in {"gpu_ms_limit", "remaining_wall_ms"} and value is not None and not _integer(value):
        raise Protocol6Error("work limit must be a nonnegative signed63 integer or null")
    if key == "category_score" and (isinstance(value, bool) or not isinstance(value, (int, float))
                                     or not math.isfinite(value) or not 0 <= value <= 1):
        raise Protocol6Error("category score is invalid")
    if key == "progress" and (isinstance(value, bool) or not isinstance(value, (int, float))
                               or not math.isfinite(value) or not 0 <= value <= 1):
        raise Protocol6Error("progress is invalid")
    if key in {"ready", "available", "has_thumbnail", "physical_complete", "batch_eligible"} \
            and not isinstance(value, bool):
        raise Protocol6Error("observation must be a boolean")
    if isinstance(value, dict) and key in {"", "class_components", "checkpoint", "gpu", "work_budget"}:
        for child_key, child in value.items():
            _validate_types(child, child_key)


def _digest(value: dict[str, Any], field: str) -> None:
    source = {key: item for key, item in value.items() if key != field}
    if value.get(field) != hashlib.sha256(canonical_bytes(source)).hexdigest():
        raise Protocol6Error(f"{field} does not match canonical source bytes")


def _inventory(value: dict[str, Any]) -> None:
    reports = value.get("final_reports")
    if not isinstance(reports, list):
        raise Protocol6Error("final reports must be an array")
    order = []
    seen = set()
    for report in reports:
        if not isinstance(report, dict):
            raise Protocol6Error("final report must be an object")
        kind = report.get("kind")
        fields = ({"kind", "session_id", "attempt_id", "control_generation", "range_id",
                   "report_sequence", "report_hash"} if kind == "session" else
                  {"kind", "job_id", "dispatch_sequence", "range_id", "report_sequence",
                   "report_hash"} if kind == "job" else set())
        if not fields or set(report) != fields:
            raise Protocol6Error("final report shape is invalid")
        _validate_types(report)
        subject = report.get("session_id", report.get("job_id"))
        attempt = report.get("attempt_id", "")
        dispatch = report.get("dispatch_sequence", 0)
        identity = (kind, subject, attempt, report["range_id"])
        if identity in seen:
            raise Protocol6Error("final report identity is duplicated")
        seen.add(identity)
        order.append((kind, subject, attempt or dispatch, report["range_id"]))
    if order != sorted(order):
        raise Protocol6Error("final reports are not in canonical order")


def validate_message(
    control: dict[str, Any],
    *,
    expected_worker_id: str | None = None,
    expected_incarnation: uuid.UUID | None = None,
) -> None:
    if not isinstance(control, dict) or not isinstance(control.get("type"), str):
        raise Protocol6Error("control type is missing")
    _check_json(control)
    variants = _SHAPES.get(control["type"])
    actual = set(control)
    if variants is None or not any(req <= actual and actual <= req | optional for req, optional in variants):
        raise Protocol6Error("control fields do not match a closed message shape")
    _validate_types(control)
    if expected_worker_id is not None and control.get("worker_id") != expected_worker_id:
        raise Protocol6Error("control targets a different worker")
    if (expected_incarnation is not None and control.get("type") != "hello"
            and control.get("incarnation") != str(expected_incarnation)):
        raise Protocol6Error("control incarnation does not match connection")
    kind = control["type"]
    if kind in {"open_session", "update_session", "close_session", "dispatch_job", "cancel_job",
                "pause_job", "resume_job", "drain_worker", "load_model", "unload_model"}:
        _digest(control, "body_hash")
    if kind in {"checkpoint", "session_closed", "job_done", "job_failed", "job_cancelled", "job_paused"}:
        _digest(control, "report_hash")
    if kind == "command_ack":
        status, code, expected = control.get("status"), control.get("code"), control.get("expected_sequence")
        if status not in {"accepted", "refused"}:
            raise Protocol6Error("command ACK status is invalid")
        if ((status == "accepted" and (code is not None or expected is not None))
                or (status == "refused" and not isinstance(code, str))
                or ((code == "sequence_gap") != (expected is not None))):
            raise Protocol6Error("command ACK conditional fields are invalid")
        if ("job_id" in control and ("session_id" in control or "request_id" in control)
                or "session_id" in control and "job_id" in control
                or "request_id" in control and ("session_id" in control or "job_id" in control)):
            raise Protocol6Error("command ACK subject is ambiguous")
    if kind == "checkpoint_ack" and ((control.get("status") == "refused"
                                       and not isinstance(control.get("code"), str))
                                      or (control.get("status") != "refused" and "code" in control)):
        raise Protocol6Error("checkpoint ACK conditional fields are invalid")
    if kind == "heartbeat":
        samples = control.get("frame_p95_ms")
        if not isinstance(samples, dict) or any(
            not isinstance(name, str) or not _integer(number, True) or number > 60_000
            for name, number in samples.items()
        ):
            raise Protocol6Error("frame p95 observation is invalid")
        gpu = control.get("gpu")
        allowed = {"device", "available", "util_pct", "vram_used_pct", "vram_used_bytes",
                   "vram_total_bytes", "temperature_c", "power_w"}
        if gpu is not None and (not isinstance(gpu, dict) or "device" not in gpu or not set(gpu) <= allowed):
            raise Protocol6Error("GPU observation shape is invalid")
        if isinstance(gpu, dict):
            for key, lower in (("power_w", 0), ("temperature_c", None)):
                if key in gpu and (isinstance(gpu[key], bool) or not isinstance(gpu[key], (int, float))
                                   or not math.isfinite(gpu[key]) or (lower is not None and gpu[key] < lower)):
                    raise Protocol6Error("GPU measurement is invalid")
    if kind == "worker_drained":
        _inventory(control)
    if kind == "predecessor_report":
        report = control["report"]
        if not isinstance(report, dict) or report.get("type") not in {"checkpoint", "worker_drained"}:
            raise Protocol6Error("predecessor report type is invalid")
        try:
            previous = uuid.UUID(control["previous_incarnation"])
        except (KeyError, ValueError, TypeError):
            raise Protocol6Error("predecessor incarnation is invalid") from None
        validate_message(
            report,
            expected_worker_id=control["worker_id"],
            expected_incarnation=previous,
        )
    if kind in {"open_session", "dispatch_job", "resume_job"}:
        _validate_types(control["work_budget"])
    if kind in {"resume_job", "job_paused"} and "checkpoint" in control:
        _validate_types(control["checkpoint"])
    if kind == "class_prepared":
        components = control["class_components"]
        if not isinstance(components, dict) or control["class_id"] != hashlib.sha256(canonical_bytes(components)).hexdigest():
            raise Protocol6Error("class identity is invalid")
        for key in ("positive_chunks", "negative_chunks", "edit_chunks"):
            shape = components[key]
            if not isinstance(shape, list) or len(shape) > 8 or any(not _integer(part, True) for part in shape):
                raise Protocol6Error("class chunk shape is invalid")
