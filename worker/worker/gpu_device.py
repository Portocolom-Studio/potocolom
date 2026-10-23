"""Select an explicitly indexed torch device before model loading."""

import logging
import os
from collections.abc import Mapping
from typing import Any


logger = logging.getLogger("potocolom.worker")


def select_device(
    torch: Any,
    device_type: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return a concrete torch device, honoring operator visibility masks."""
    if device_type == "cpu":
        return "cpu"
    if device_type not in {"cuda", "rocm"}:
        raise ValueError(f"unknown device type: {device_type}")

    visible_count = int(torch.cuda.device_count())
    if visible_count == 0:
        if device_type == "rocm":
            raise RuntimeError("no visible ROCm GPUs; check HIP_VISIBLE_DEVICES and device passthrough")
        raise RuntimeError("no visible CUDA GPUs; check CUDA_VISIBLE_DEVICES and device passthrough")

    if device_type == "cuda":
        return "cuda:0"

    version = getattr(torch, "version", None)
    if getattr(version, "hip", None) is None:
        raise RuntimeError("DEVICE=rocm requires a ROCm build of PyTorch")

    visible_env = os.environ if environ is None else environ
    if "HIP_VISIBLE_DEVICES" in visible_env or "CUDA_VISIBLE_DEVICES" in visible_env:
        properties = torch.cuda.get_device_properties(0)
        name = str(getattr(properties, "name", "GPU 0"))
        target = str(getattr(properties, "gcnArchName", "")).split(":", 1)[0]
        logger.info(
            "selected ROCm GPU index=0 name=%s target=%s (visibility mask)",
            name, target or "unknown",
        )
        return "cuda:0"

    supported_arches = set(torch.cuda.get_arch_list())
    skipped: list[str] = []
    selected: tuple[int, str, str] | None = None
    for index in range(visible_count):
        properties = torch.cuda.get_device_properties(index)
        name = str(getattr(properties, "name", f"GPU {index}"))
        target = str(getattr(properties, "gcnArchName", "")).split(":", 1)[0]
        if target in supported_arches:
            if selected is None:
                selected = (index, name, target)
        else:
            skipped.append(f"index={index} name={name} target={target or 'unknown'}")
            logger.warning(
                "skipping unsupported ROCm GPU index=%d name=%s target=%s",
                index, name, target or "unknown",
            )

    if selected is not None:
        index, name, target = selected
        logger.info(
            "selected ROCm GPU index=%d name=%s target=%s",
            index, name, target,
        )
        return f"cuda:{index}"

    raise RuntimeError(
        "no visible ROCm GPU supports this PyTorch build; "
        f"visible devices: {', '.join(skipped)}; "
        f"supported targets: {', '.join(sorted(supported_arches)) or 'none'}"
    )
