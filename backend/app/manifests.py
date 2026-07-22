"""Model manifests as they appear on the wire (docs/architecture.md).

Workers describe their models with these in the fleet hello; the registry
persists them and GET /api/v1/models exposes them to the frontend.
"""

import json
import logging
from functools import lru_cache

import jsonschema
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger("potocolom.manifests")

DIFFUSION_CAPABILITIES = frozenset({"text_to_image", "image_to_image"})


class Manifest(BaseModel):
    # Worker-side manifest files may carry extra fields (weight sources); only
    # this surface crosses the wire and reaches the frontend.
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    capabilities: list[str]
    parameters: dict = Field(default_factory=dict)  # JSON Schema for the model's call parameters
    min_vram_gb: int = 0
    default: bool = False  # preselected by clients when nothing is pinned
    license_id: str = ""
    license_url: str = ""
    commercial_max_revenue_usd: int | None = None
    license_registration_url: str = ""
    requires_attribution: str = ""
    benchmark_only: bool = False  # reference benchmarks; omitted from the studio UI


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


def validate_params(manifest: Manifest, params: dict) -> str | None:
    """Return a validation error message, or None when params are acceptable."""
    schema_json = json.dumps(manifest.parameters, sort_keys=True)
    try:
        validator = _params_validator(schema_json)
    except jsonschema.SchemaError:
        logger.warning("model %s has an invalid parameter schema; accepting params unchecked",
                       manifest.id)
        return None
    try:
        validator.validate(params)
    except jsonschema.ValidationError as error:
        return error.message
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
    return manifests
