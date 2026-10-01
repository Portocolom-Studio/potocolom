"""Zero-shot output categorization for usage metrics (docs/metrics.md).

One label and score per output image. Real only where the worker enables it:
build_runtime calls enable_categorizer while it builds DiffusersEngine, so the
simulated engine, `make simulate` and the torch-free worker test suite keep the
answer ("other", None) and never download the model.
"""

import io
import logging
import threading
import time
from typing import Any

from PIL import Image

logger = logging.getLogger("potocolom.worker")

LABELS = ("art", "photo_edit", "design", "character", "nsfw", "other")

# The approved model (docs/third-party-models.md): Apache-2.0 declared in the
# weight repository's card, no restriction anywhere in the card. The revision pin keeps the labels one deployment reports from drifting
# when upstream moves its default branch.
SOURCE = "google/siglip2-base-patch16-224"
SOURCE_REVISION = "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2"

# "other" has no prompt: it is the answer when no label clears the threshold.
PROMPTS: dict[str, str] = {
    "art": "a painting, drawing or digital illustration",
    "photo_edit": "an edited photograph of a real scene",
    "design": "a graphic design, logo, poster or user interface",
    "character": "a portrait of a character or a person",
    "nsfw": "explicit sexual or nude content",
}

# Calibration knob: the top label's share of the softmax over the five label
# logits must reach this to become a label, so an output that resembles none
# of the prompts falls to "other" instead of being forced into the nearest one.
# SigLIP's own sigmoid gives these broad prompts absolute scores near zero
# (0.0002 to 0.01 on real outputs), so it cannot be thresholded; the softmax
# measured 0.77 to 0.99 on paintings, photos and cartoons and at most 0.51 on a
# flat grey frame and on noise.
CATEGORY_MIN_SCORE = 0.6

# A failed load is retried no sooner than this, so an offline install does not
# pay a failing download on every job.
LOAD_RETRY_SECONDS = 600.0

_enabled = False
_loaded: tuple[Any, Any, Any] | None = None
_load_failure_logged = False
_retry_after = 0.0
_inference_logged_at: float | None = None
# Two jobs can finish together, each in its own to_thread call; one load.
_load_lock = threading.Lock()


def enable_categorizer() -> None:
    """Turn on real categorization; only the DiffusersEngine build calls this."""
    global _enabled
    _enabled = True


def _load() -> tuple[Any, Any, Any]:
    """Model, processor and label text embeddings, fetched on first use.

    torch and transformers import here rather than at module top so the
    torch-free worker test suite can import this module.
    """
    global _loaded, _retry_after
    with _load_lock:
        if _loaded is not None:
            return _loaded
        if time.monotonic() < _retry_after:
            raise RuntimeError("categorizer load failed recently; waiting to retry")
        try:
            _loaded = _load_model()
        except Exception:
            _retry_after = time.monotonic() + LOAD_RETRY_SECONDS
            raise
        return _loaded


def _load_model() -> tuple[Any, Any, Any]:
    import torch
    from transformers import AutoModel, AutoProcessor

    with torch.inference_mode():
        processor = AutoProcessor.from_pretrained(SOURCE, revision=SOURCE_REVISION)
        model = AutoModel.from_pretrained(SOURCE, revision=SOURCE_REVISION)
        model.to(torch.device("cpu"), torch.float32)
        model.eval()
        text_inputs = processor(
            text=list(PROMPTS.values()),
            padding="max_length",
            truncation=True,
            return_tensors="pt",
        )
        text_embeds = model.text_model(**text_inputs).pooler_output
        text_embeds = text_embeds / text_embeds.norm(p=2, dim=-1, keepdim=True)
    return model, processor, text_embeds


def _score(state: tuple[Any, Any, Any], picture: Image.Image) -> dict[str, float]:
    """Each label's share of a softmax over the label logits."""
    import torch

    model, processor, text_embeds = state
    inputs = processor(images=picture, return_tensors="pt")
    with torch.inference_mode():
        image_embeds = model.vision_model(**inputs).pooler_output
        image_embeds = image_embeds / image_embeds.norm(p=2, dim=-1, keepdim=True)
        logits = (image_embeds @ text_embeds.T) * model.logit_scale.exp()
        logits = logits + model.logit_bias
        scores = torch.softmax(logits, dim=-1).squeeze(0)
    return {label: float(score) for label, score in zip(PROMPTS, scores)}


def categorize_output(image: bytes | None) -> tuple[str, float | None]:
    """Label one output image as (label, score in 0..1), and never raise.

    The score is None when there is none to report: the categorizer is not
    enabled, the bytes are not an image, or loading or inference failed. Typed
    optional rather than None so callers testing `score is not None` describe
    the protocol seam instead of a branch that cannot be taken.
    """
    global _load_failure_logged, _inference_logged_at
    if not _enabled or image is None:
        return "other", None
    try:
        with Image.open(io.BytesIO(image)) as decoded:
            picture = decoded.convert("RGB")
    except Exception:
        return "other", None
    try:
        state = _load()
    except Exception:
        # Once, not per output: a missing model is a deployment problem the
        # log should make visible without flooding every job after it.
        if not _load_failure_logged:
            _load_failure_logged = True
            logger.exception("categorizer model load failed; answering other until it succeeds")
        return "other", None
    try:
        scores = _score(state, picture)
        label = max(scores, key=lambda name: scores[name])
    except Exception:
        # A deterministic failure repeats on every output, so it is logged at
        # most once per retry window rather than once per job.
        now = time.monotonic()
        if _inference_logged_at is None or now - _inference_logged_at >= LOAD_RETRY_SECONDS:
            _inference_logged_at = now
            logger.exception("categorizer inference failed; answering other")
        return "other", None
    score = round(scores[label], 4)
    if score < CATEGORY_MIN_SCORE:
        return "other", score
    return label, score
