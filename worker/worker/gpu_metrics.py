"""GPU hardware samples for heartbeats and gpu_status (docs/metrics.md)."""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any

_GPU_USE_RE = re.compile(r"(\d+)\s*%")
_TEMP_RE = re.compile(r"(\d+(?:\.\d+)?)\s*c", re.IGNORECASE)
_POWER_RE = re.compile(r"(\d+(?:\.\d+)?)\s*w", re.IGNORECASE)
_VRAM_PAIR_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
_VRAM_USED_B_RE = re.compile(r"VRAM\s+Total\s+Used\s+Memory\s+\(B\):\s*(\d+)", re.IGNORECASE)
_VRAM_TOTAL_B_RE = re.compile(r"VRAM\s+Total\s+Memory\s+\(B\):\s*(\d+)", re.IGNORECASE)


def _run_smi(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout


def _parse_util(text: str) -> int | None:
    percent = _GPU_USE_RE.search(text)
    if percent:
        return int(percent.group(1))
    bare = re.search(r"use\s*\(%\):\s*(\d+)", text, re.IGNORECASE)
    if bare:
        return int(bare.group(1))
    return None


def _parse_temperature(text: str) -> float | None:
    match = _TEMP_RE.search(text)
    if match:
        return float(match.group(1))
    celsius = re.search(r"\(C\):\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if celsius:
        return float(celsius.group(1))
    return None


def _parse_power(text: str) -> float | None:
    match = _POWER_RE.search(text)
    if match:
        return float(match.group(1))
    watts = re.search(r"\(W\):\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if watts:
        return float(watts.group(1))
    return None


def _torch_vram_bytes() -> tuple[int, int] | None:
    try:
        import torch
    except ImportError:
        return None
    if not torch.cuda.is_available():
        return None
    free, total = torch.cuda.mem_get_info()
    used = int(total) - int(free)
    return used, int(total)


def _nvidia_metrics() -> dict[str, Any]:
    text = _run_smi(
        [
            "nvidia-smi",
            "--query-gpu=utilization.gpu,temperature.gpu,power.draw,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ]
    )
    if not text.strip():
        return {}
    parts = [part.strip() for part in text.split(",")]
    if len(parts) < 5:
        return {}
    util = _parse_util(parts[0])
    temp = _parse_temperature(parts[1])
    power = _parse_power(parts[2] + " w")
    used_mib = int(float(parts[3])) if parts[3] not in ("", "[N/A]") else None
    total_mib = int(float(parts[4])) if parts[4] not in ("", "[N/A]") else None
    metrics: dict[str, Any] = {}
    if util is not None:
        metrics["util_pct"] = util
    if temp is not None:
        metrics["temperature_c"] = temp
    if power is not None:
        metrics["power_w"] = power
    if used_mib is not None and total_mib is not None and total_mib > 0:
        metrics["vram_used_bytes"] = used_mib * 1024 * 1024
        metrics["vram_total_bytes"] = total_mib * 1024 * 1024
    return metrics


def _rocm_gpu0_vram_bytes(mem_text: str) -> tuple[int, int] | None:
    used = total = None
    for line in mem_text.splitlines():
        if "GPU[0]" not in line:
            continue
        if "VRAM Total Used Memory (B):" in line:
            match = re.search(r"VRAM Total Used Memory \(B\):\s*(\d+)", line)
            if match:
                used = int(match.group(1))
        elif "VRAM Total Memory (B):" in line and "Used" not in line:
            match = re.search(r"VRAM Total Memory \(B\):\s*(\d+)", line)
            if match:
                total = int(match.group(1))
    if used is not None and total is not None and total > 0:
        return used, total
    return None


def _rocm_metrics() -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    use_text = _run_smi(["rocm-smi", "--showuse"])
    util = _parse_util(use_text)
    if util is not None:
        metrics["util_pct"] = util

    temp_text = _run_smi(["rocm-smi", "--showtemp"])
    temp = _parse_temperature(temp_text)
    if temp is not None:
        metrics["temperature_c"] = temp

    power_text = _run_smi(["rocm-smi", "--showpower"])
    power = _parse_power(power_text)
    if power is not None:
        metrics["power_w"] = power

    mem_text = _run_smi(["rocm-smi", "--showmeminfo", "vram"])
    gpu0_vram = _rocm_gpu0_vram_bytes(mem_text)
    if gpu0_vram is not None:
        used, total = gpu0_vram
        metrics["vram_used_bytes"] = used
        metrics["vram_total_bytes"] = total
    else:
        pair = _VRAM_PAIR_RE.search(mem_text.replace(",", " "))
        if pair:
            used = int(pair.group(1))
            total = int(pair.group(2))
            if total > 0:
                metrics["vram_used_bytes"] = used
                metrics["vram_total_bytes"] = total
        else:
            used_match = _VRAM_USED_B_RE.search(mem_text)
            total_match = _VRAM_TOTAL_B_RE.search(mem_text)
            if used_match and total_match:
                used = int(used_match.group(1))
                total = int(total_match.group(1))
                if total > 0:
                    metrics["vram_used_bytes"] = used
                    metrics["vram_total_bytes"] = total
    return metrics


def _attach_vram_pct(metrics: dict[str, Any]) -> None:
    used = metrics.get("vram_used_bytes")
    total = metrics.get("vram_total_bytes")
    if isinstance(used, int) and isinstance(total, int) and total > 0:
        metrics["vram_used_pct"] = round(used * 100 / total)


def sample_gpu(device: str) -> dict[str, Any]:
    """Return a JSON-serializable GPU snapshot for the configured device."""
    metrics: dict[str, Any] = {"device": device}

    if device == "cpu":
        metrics["available"] = False
        return metrics

    if device == "rocm" and shutil.which("rocm-smi"):
        metrics.update(_rocm_metrics())
    elif device == "cuda" and shutil.which("nvidia-smi"):
        metrics.update(_nvidia_metrics())

    vram = _torch_vram_bytes()
    if vram is not None:
        used, total = vram
        metrics.setdefault("vram_used_bytes", used)
        metrics.setdefault("vram_total_bytes", total)

    _attach_vram_pct(metrics)
    metrics["available"] = any(
        key in metrics for key in ("util_pct", "vram_used_bytes", "vram_total_bytes")
    )
    return metrics
