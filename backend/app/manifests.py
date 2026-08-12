"""Model manifests as they appear on the wire (docs/architecture.md).

Workers describe their models with these in the fleet hello; the registry
persists them and GET /api/v1/models exposes them to the frontend.
"""

import json
import logging
import math
from functools import lru_cache

import jsonschema
from jsonschema import Draft202012Validator
from referencing.exceptions import Unresolvable
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger("potocolom.manifests")

DIFFUSION_CAPABILITIES = frozenset({"text_to_image", "image_to_image"})

# One bound for both wire sources of a per-model p95: hello carries it on the
# manifest and heartbeats carry it per model, and the two must agree on the
# ceiling or the same measurement could be accepted in one and refused in the
# other. The ceiling only needs to catch absurd values: a slow frame is still
# an honest measurement, while a number past this means a broken or hostile
# worker.
FRAME_P95_MAX_MS = 60_000


class Manifest(BaseModel):
    # Worker-side manifest files may carry extra fields (weight sources); only
    # this surface crosses the wire and reaches the frontend.
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    capabilities: list[str]
    parameters: dict = Field(default_factory=dict)  # JSON Schema for the model's call parameters
    # int4 in the models table; a worker-supplied value past it fails the
    # upsert, and that runs before the fleet handler's cleanup can see it.
    min_vram_gb: int = Field(default=0, ge=0, lt=2**31)
    prompt_token_limit: int = 0  # text encoder window; 0 means the studio stays quiet
    default: bool = False  # preselected by clients when nothing is pinned
    license_id: str = ""
    license_url: str = ""
    commercial_max_revenue_usd: int | None = None
    license_registration_url: str = ""
    requires_attribution: str = ""
    benchmark_only: bool = False  # reference benchmarks; omitted from the studio UI
    # Studio-visible subset of capabilities; None means all of them. A model
    # measured on only one path (e.g. sdxl-turbo, realtime only) can stay out
    # of the queued pickers while its verified path stays offered.
    studio_capabilities: list[str] | None = None
    # Measured single-frame p95 on the reporting worker's card; None until a
    # worker has calibrated the model, absent on the simulated worker. The
    # bounds match the heartbeat's ceiling (FRAME_P95_MAX_MS), so a value the
    # manifest accepts is a value the heartbeat would accept too.
    realtime_p95_ms: int | None = Field(default=None, ge=1, le=FRAME_P95_MAX_MS)


def validate_capability_exclusivity(manifest: Manifest) -> None:
    """Upscale models must not also declare diffusion capabilities (issue #90)."""
    caps = set(manifest.capabilities)
    if "upscale" in caps and caps & DIFFUSION_CAPABILITIES:
        raise ValueError(
            f"manifest {manifest.id}: upscale cannot combine with "
            f"{sorted(caps & DIFFUSION_CAPABILITIES)}"
        )


@lru_cache(maxsize=128)
def _params_validator(schema_json: str) -> Draft202012Validator:
    schema = json.loads(schema_json)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


# Deeper than any real parameter set or schema, and far below the depth at
# which walking the structure would exhaust the stack. json.loads accepts ~993
# levels and this predicate costs three frames per level, so without a cap it
# would raise where json.loads succeeded: a guard failing inside the guard.
JSON_MAX_DEPTH = 64


def json_finite(value: object, depth: int = 0) -> bool:
    """Whether a decoded JSON value is storable in a JSONB column.

    jsonb has no NaN or Infinity and json.loads produces both by default, so
    this has to be checked at the edge: rejecting them in the engine's
    serializer only turns the DataError into a ValueError, and both are a 500.
    Over-deep structures are rejected here for the same reason.
    """
    if depth > JSON_MAX_DEPTH:
        return False
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(json_finite(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        return all(json_finite(item, depth + 1) for item in value)
    return True


def validate_params(manifest: Manifest, params: dict) -> str | None:
    """Return a validation error message, or None when params are acceptable."""
    if not json_finite(params):
        return "params are too deeply nested or contain a value JSON storage cannot hold"
    try:
        # dumps walks the schema too, so it raises before check_schema can.
        schema_json = json.dumps(manifest.parameters, sort_keys=True)
        validator = _params_validator(schema_json)
    except jsonschema.SchemaError:
        logger.warning("model %s has an invalid parameter schema; accepting params unchecked",
                       manifest.id)
        return None
    except RecursionError:
        # check_schema walks the schema, so it raises this before validate can.
        return "schema nests too deeply to validate"
    try:
        validator.validate(params)
    except jsonschema.ValidationError as error:
        return error.message
    except RecursionError:
        # Returning None here would mean "params acceptable", so a schema too
        # deep to walk would silently skip the only validation gate the API
        # has. Fail closed: not validated is not the same as valid.
        return "params or schema nest too deeply to validate"
    except Unresolvable:
        # A $ref naming something the schema does not define raises past
        # ValidationError, so it would reach the request handler as a 500.
        # A manifest is worker-supplied, not user-supplied, so treat it like
        # the invalid-schema case above and blame the log, not the caller.
        logger.warning("model %s has an unusable schema reference; "
                       "accepting params unchecked", manifest.id)
        return None
    return None


def validate_param_update(manifest: Manifest, params: dict) -> str | None:
    """Return a validation error message, or None when an update is acceptable.

    The same validation as validate_params, except that nothing is required:
    an update carries a subset of the session's params, so the schema is the
    manifest's with the `required` list removed rather than a second schema
    the two could drift from. An empty update is a client bug, not a no-op.
    """
    if not params:
        return "params update is empty"
    if not json_finite(params):
        return "params are too deeply nested or contain a value JSON storage cannot hold"
    try:
        # dumps walks the schema too, so it raises before check_schema can.
        # The required list is dropped so a subset can validate; everything
        # else about the schema, bounds and extras included, still applies.
        schema = dict(manifest.parameters)
        schema.pop("required", None)
        schema_json = json.dumps(schema, sort_keys=True)
        validator = _params_validator(schema_json)
    except jsonschema.SchemaError:
        logger.warning("model %s has an invalid parameter schema; accepting params unchecked",
                       manifest.id)
        return None
    except RecursionError:
        # check_schema walks the schema, so it raises this before validate can.
        return "schema nests too deeply to validate"
    try:
        validator.validate(params)
    except jsonschema.ValidationError as error:
        return error.message
    except RecursionError:
        # Returning None here would mean "params acceptable", so a schema too
        # deep to walk would silently skip the only validation gate the API
        # has. Fail closed: not validated is not the same as valid.
        return "params or schema nest too deeply to validate"
    except Unresolvable:
        # A $ref naming something the schema does not define raises past
        # ValidationError, so it would reach the request handler as a 500.
        # A manifest is worker-supplied, not user-supplied, so treat it like
        # the invalid-schema case above and blame the log, not the caller.
        logger.warning("model %s has an unusable schema reference; "
                       "accepting params unchecked", manifest.id)
        return None
    return None


def parse_manifests(raw: object) -> list[Manifest]:
    """Validate the hello models field; ValueError means protocol violation."""
    if not isinstance(raw, list):
        raise ValueError("models must be a list of manifests")
    try:
        manifests = [Manifest.model_validate(entry) for entry in raw]
    except ValidationError as error:
        raise ValueError(f"invalid manifest: {error}") from error
    for manifest in manifests:
        validate_capability_exclusivity(manifest)
        # parameters is persisted to a JSONB column, which has no NaN or
        # Infinity; without this the upsert kills the socket on every reconnect.
        if not json_finite(manifest.parameters):
            raise ValueError(
                f"manifest {manifest.id}: parameters are too deeply nested or contain "
                "a value JSON storage cannot hold"
            )
    return manifests
