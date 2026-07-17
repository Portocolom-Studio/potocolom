"""GPU time estimates from curated benchmark baselines (issue #47)."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger("potocolom.estimates")

TIMINGS_PATH = Path(__file__).with_name("model_timings.json")


@lru_cache(maxsize=1)
def _load_timings() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(TIMINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        # Cached, so this logs once per process.
        logger.warning("model_timings.json missing or invalid; estimates disabled")
        return {}
    if not isinstance(raw, dict):
        return {}
    timings: dict[str, dict[str, Any]] = {}
    for model_id, entry in raw.items():
        if model_id.startswith("_") or not isinstance(entry, dict):
            continue
        try:
            gpu_ms = int(entry["gpu_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        if gpu_ms <= 0:
            continue
        # Diffusion baselines need steps/width/height for pixel scaling.
        # Upscale baselines are factor-keyed (flat or scale by factor^2).
        if "factor" in entry:
            try:
                factor = int(entry["factor"])
            except (TypeError, ValueError):
                continue
            if factor <= 0:
                continue
            timings[model_id] = {
                "gpu_ms": gpu_ms,
                "factor": factor,
                "flat": bool(entry.get("flat", False)),
            }
            continue
        try:
            candidate = {
                "gpu_ms": gpu_ms,
                "width": int(entry["width"]),
                "height": int(entry["height"]),
                "steps": int(entry["steps"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
        if any(value <= 0 for value in candidate.values()):
            continue  # a zero baseline would divide by zero downstream
        timings[model_id] = candidate
    return timings


def schema_defaults(parameters: dict[str, Any]) -> dict[str, Any]:
    """Pull JSON Schema property defaults from a manifest parameters block."""
    props = parameters.get("properties", {})
    if not isinstance(props, dict):
        return {}
    defaults: dict[str, Any] = {}
    for name, spec in props.items():
        if isinstance(spec, dict) and "default" in spec:
            defaults[name] = spec["default"]
    return defaults


def estimate_gpu_ms(model_id: str, params: dict[str, Any]) -> int | None:
    """Estimate GPU milliseconds for model_id at the given generation params."""
    baseline = _load_timings().get(model_id)
    if baseline is None:
        return None

    if "factor" in baseline:
        if baseline.get("flat"):
            return max(1, int(baseline["gpu_ms"]))
        if "factor" not in params:
            return max(1, int(baseline["gpu_ms"]))
        try:
            factor = int(params["factor"])
        except (TypeError, ValueError):
            return None
        if factor <= 0:
            return None
        scale = (factor / baseline["factor"]) ** 2
        return max(1, round(baseline["gpu_ms"] * scale))

    if not all(key in params for key in ("steps", "width", "height")):
        return None

    try:
        steps = int(params["steps"])
        width = int(params["width"])
        height = int(params["height"])
    except (TypeError, ValueError):
        return None

    if steps <= 0 or width <= 0 or height <= 0:
        return None

    step_scale = steps / baseline["steps"]
    pixel_scale = (width * height) / (baseline["width"] * baseline["height"])
    return max(1, round(baseline["gpu_ms"] * step_scale * pixel_scale))
