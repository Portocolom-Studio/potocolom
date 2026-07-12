"""Model manifests (docs/architecture.md). A model becomes usable by dropping
a manifest into the models directory; `source` names the Hugging Face
repository or local path the pipeline loads from and never crosses the wire.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("potocolom.worker")


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    capabilities: list[str]  # text_to_image, image_to_image, realtime
    parameters: dict = {}  # JSON Schema for the model's call parameters
    min_vram_gb: int = 0
    default: bool = False  # preselected by clients when nothing is pinned
    source: str = ""  # weights location, worker side only
    vae: str = ""  # optional fp16-safe VAE replacement, worker side only
    scheduler: str = ""  # optional scheduler override, worker side only

    def wire(self) -> dict:
        return self.model_dump(exclude={"source", "vae", "scheduler"})

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
SIMULATED_MANIFEST = Manifest(
    id="sd-sim",
    name="Simulated",
    capabilities=["text_to_image", "image_to_image", "realtime"],
    parameters={"type": "object", "properties": {"prompt": {"type": "string"}}},
)


def load_manifests(models_dir: str) -> list[Manifest]:
    """Operator errors here should be loud, not degrade into an empty fleet."""
    files = sorted(Path(models_dir).glob("*.json"))
    if not files:
        raise ValueError(f"no manifests found in {models_dir}")
    manifests = [Manifest.model_validate_json(file.read_text()) for file in files]
    logger.info("loaded %d manifests from %s: %s",
                len(manifests), models_dir, [m.id for m in manifests])
    return manifests
