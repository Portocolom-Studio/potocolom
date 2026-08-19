"""Model manifests (docs/architecture.md). A model becomes usable by dropping
a manifest into the models directory; `source` names the Hugging Face
repository or local path the pipeline loads from and never crosses the wire.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("potocolom.worker")


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    capabilities: list[str]  # text_to_image, image_to_image, realtime, upscale
    parameters: dict = Field(default_factory=dict)  # JSON Schema for the model's call parameters
    # Matches the API's bound, so a bad value fails here where the operator
    # can see it rather than silently at registration.
    min_vram_gb: int = Field(default=0, ge=0, lt=2**31)
    # Native text encoder window in tokens. The worker chunks declared CLIP
    # prompts past this window, while the studio still warns that later chunks
    # influence the image weakly. Left at 0 rather than 77 on purpose: a
    # manifest that forgets to declare it stays silent instead of promising a
    # CLIP limit a T5 based model does not have.
    prompt_token_limit: int = 0
    default: bool = False  # preselected by clients when nothing is pinned
    source: str = ""  # weights location, worker side only
    vae: str = ""  # optional fp16-safe VAE replacement, worker side only
    preview_decoder: str = ""  # optional distilled frame decoder, worker side only
    scheduler: str = ""  # optional scheduler override, worker side only
    lora: str = ""  # optional distillation LoRA to fuse, worker side only
    t2i_adapter: str = ""  # optional sketch adapter for realtime conditioning, worker side only
    quantize: str = Field(
        default="",
        pattern=r"^(?:[A-Za-z_][A-Za-z0-9_]*:int8)?$",
    )  # optional component:scheme, worker side only
    license_id: str = ""  # e.g. stability-ai-community, apache-2.0
    license_url: str = ""
    commercial_max_revenue_usd: int | None = None  # None = no cap
    license_registration_url: str = ""
    requires_attribution: str = ""  # e.g. "Powered by Stability AI"
    benchmark_only: bool = False  # benchmark reference; hidden from GET /api/v1/models
    # Studio-visible subset of capabilities; None means all of them. A model
    # measured on only one path (e.g. sdxl-turbo, realtime only) can stay out
    # of the queued pickers while its verified path stays offered.
    studio_capabilities: list[str] | None = None

    def wire(self) -> dict:
        return self.model_dump(exclude={
            "source", "vae", "preview_decoder", "scheduler", "lora", "quantize",
            "t2i_adapter",
        })

    def with_defaults(self, params: dict) -> dict:
        """Fill missing keys from the schema's declared defaults, so a bare
        prompt renders with the model's intended settings."""
        filled = dict(params)
        for key, prop in self.parameters.get("properties", {}).items():
            if key not in filled and isinstance(prop, dict) and "default" in prop:
                filled[key] = prop["default"]
        return filled


# What the worker serves when no models directory is configured: every
# protocol path stays runnable without a GPU (scripts/simulate.py, CI).
#
# The prompt is required here because every shipped realtime manifest requires
# it. A permissive schema makes this the easier contract to build a client
# against, and the API then refuses that client 4000 for params it never
# learned to send.
SIMULATED_MANIFEST = Manifest(
    id="sd-sim",
    name="Simulated",
    capabilities=["text_to_image", "image_to_image", "realtime"],
    parameters={"type": "object",
                "properties": {"prompt": {"type": "string"}},
                "required": ["prompt"]},
)


DIFFUSION_CAPABILITIES = frozenset({"text_to_image", "image_to_image"})


def validate_capability_exclusivity(manifest: Manifest) -> None:
    """Upscale models must not also declare diffusion capabilities (issue #90).

    Routing is capability-driven: source + upscale = pixel upscale, source +
    image_to_image = edit. Mixing them on one manifest makes that ambiguous.
    """
    caps = set(manifest.capabilities)
    if "upscale" in caps and caps & DIFFUSION_CAPABILITIES:
        raise ValueError(
            f"manifest {manifest.id}: upscale cannot combine with "
            f"{sorted(caps & DIFFUSION_CAPABILITIES)}"
        )


def load_manifests(models_dir: str) -> list[Manifest]:
    """Operator errors here should be loud, not degrade into an empty fleet."""
    files = sorted(Path(models_dir).glob("*.json"))
    if not files:
        raise ValueError(f"no manifests found in {models_dir}")
    manifests = [Manifest.model_validate_json(file.read_text()) for file in files]
    seen: set[str] = set()
    for manifest in manifests:
        if manifest.id in seen:
            raise ValueError(f"duplicate manifest id: {manifest.id}")
        validate_capability_exclusivity(manifest)
        seen.add(manifest.id)
    logger.info("loaded %d manifests from %s: %s",
                len(manifests), models_dir, [m.id for m in manifests])
    return manifests
