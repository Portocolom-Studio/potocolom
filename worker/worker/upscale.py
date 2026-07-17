"""Pixel upscale via Real-ESRGAN class weights (issue #89).

Loads architecture through spandrel (not the unmaintained realesrgan/basicsr
packages). Inference is always tiled so VRAM stays bounded at 1024 x4 -> 4096.
"""

from __future__ import annotations

import logging
import os
import shutil
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image

logger = logging.getLogger("potocolom.worker")

UPSCALE_TILE = 512
UPSCALE_OVERLAP = 32

# Official xinntao Real-ESRGAN release weights (BSD-3-Clause). Factor selects
# the matching RRDBNet checkpoint; both live under the same release-host
# prefix named by the manifest `source` field.
WEIGHT_FILES = {
    2: "RealESRGAN_x2plus.pth",
    4: "RealESRGAN_x4plus.pth",
}
WEIGHT_RELEASE_PATHS = {
    2: "v0.2.1/RealESRGAN_x2plus.pth",
    4: "v0.1.0/RealESRGAN_x4plus.pth",
}


def weight_url(source: str, factor: int) -> str:
    """Resolve a download URL for the factor's weight file."""
    if factor not in WEIGHT_RELEASE_PATHS:
        raise ValueError(f"unsupported upscale factor: {factor}")
    return f"{source.rstrip('/')}/{WEIGHT_RELEASE_PATHS[factor]}"


def weight_cache_path(models_dir: str, model_id: str, factor: int) -> Path:
    if factor not in WEIGHT_FILES:
        raise ValueError(f"unsupported upscale factor: {factor}")
    filename = WEIGHT_FILES[factor]
    safe_id = "".join(c if c.isalnum() or c in "._-" else "-" for c in model_id)
    return Path(models_dir or ".") / ".cache" / "upscale" / safe_id / filename


def ensure_weights(source: str, models_dir: str, model_id: str, factor: int) -> Path:
    path = weight_cache_path(models_dir, model_id, factor)
    if path.is_file() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    url = weight_url(source, factor)
    # Per-process temp name: workers sharing one models_dir (one process per
    # GPU on the same host) must not clobber each other's partial download.
    tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
    logger.info("downloading upscale weights factor=%s from %s", factor, url)
    try:
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
            with tmp.open("wb") as out:
                shutil.copyfileobj(response, out)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def load_upscale_model(path: Path, device: str, dtype: Any) -> Any:
    from spandrel import ImageModelDescriptor, ModelLoader

    descriptor = ModelLoader().load_from_file(str(path))
    if not isinstance(descriptor, ImageModelDescriptor):
        raise ValueError(f"upscale weights are not an image model: {path}")
    return descriptor.model.to(device=device, dtype=dtype).eval()


def _tile_starts(length: int, tile: int, overlap: int) -> list[int]:
    if length <= tile:
        return [0]
    step = max(tile - overlap, 1)
    starts = list(range(0, length - overlap, step))
    last = length - tile
    if starts[-1] != last:
        starts.append(last)
    return starts


def iter_tiles(
    width: int, height: int, *, tile: int = UPSCALE_TILE, overlap: int = UPSCALE_OVERLAP,
) -> list[tuple[int, int, int, int]]:
    """Return (x0, y0, x1, y1) input-space tiles covering the image."""
    tiles: list[tuple[int, int, int, int]] = []
    for y0 in _tile_starts(height, tile, overlap):
        for x0 in _tile_starts(width, tile, overlap):
            x1 = min(x0 + tile, width)
            y1 = min(y0 + tile, height)
            if x1 - x0 < tile and width >= tile:
                x0 = max(0, x1 - tile)
            if y1 - y0 < tile and height >= tile:
                y0 = max(0, y1 - tile)
            tiles.append((x0, y0, x1, y1))
    seen: set[tuple[int, int, int, int]] = set()
    unique: list[tuple[int, int, int, int]] = []
    for tile_box in tiles:
        if tile_box not in seen:
            seen.add(tile_box)
            unique.append(tile_box)
    return unique


def _pil_to_tensor(image: Image.Image, torch: Any, device: str, dtype: Any) -> Any:
    width, height = image.size
    raw = torch.tensor(bytearray(image.tobytes()), dtype=torch.uint8)
    return (
        raw.view(height, width, 3).permute(2, 0, 1).to(device=device, dtype=dtype) / 255.0
    ).unsqueeze(0)


def _tensor_to_pil(tensor: Any, torch: Any) -> Image.Image:
    clamped = tensor.squeeze(0).clamp(0, 1).mul(255).round().to(torch.uint8).cpu()
    channels, height, width = clamped.shape
    if channels != 3:
        raise ValueError(f"expected 3-channel upscale output, got {channels}")
    hwc = clamped.permute(1, 2, 0).contiguous()
    return Image.frombytes("RGB", (width, height), hwc.numpy().tobytes())


def _blend_weights(
    tile_h: int, tile_w: int, overlap: int, torch: Any, device: str, dtype: Any,
) -> Any:
    fade = max(min(overlap, tile_h // 2, tile_w // 2), 1)
    wy = torch.ones(tile_h, device=device, dtype=dtype)
    wx = torch.ones(tile_w, device=device, dtype=dtype)
    # Never exactly zero: normalization reconstructs single-coverage pixels
    # for any positive weight, but a zero-weight border pixel divides to
    # black at the image edges where no neighboring tile compensates.
    ramp = torch.linspace(0, 1, fade + 1, device=device, dtype=dtype)[1:]
    wy[:fade] = ramp
    wy[-fade:] = ramp.flip(0)
    wx[:fade] = ramp
    wx[-fade:] = ramp.flip(0)
    return wy[:, None] * wx[None, :]


def upscale_tiled(
    model: Any,
    image: Image.Image,
    factor: int,
    *,
    device: str,
    dtype: Any,
    progress: Callable[[float], None] | None = None,
    tile: int = UPSCALE_TILE,
    overlap: int = UPSCALE_OVERLAP,
) -> Image.Image:
    """Run `model` over `image` in tiles; returns an RGB PIL image."""
    import torch
    import torch.nn.functional as F

    rgb = image.convert("RGB")
    width, height = rgb.size
    out_w, out_h = width * factor, height * factor
    tiles = iter_tiles(width, height, tile=tile, overlap=overlap)
    if not tiles:
        raise ValueError("empty tile grid")

    src = _pil_to_tensor(rgb, torch, device, dtype)
    accumulator = torch.zeros((1, 3, out_h, out_w), device=device, dtype=torch.float32)
    weight_map = torch.zeros((1, 1, out_h, out_w), device=device, dtype=torch.float32)

    with torch.no_grad():
        for index, (x0, y0, x1, y1) in enumerate(tiles):
            patch = src[:, :, y0:y1, x0:x1]
            ph, pw = patch.shape[-2], patch.shape[-1]
            pad_h = max(tile - ph, 0) if height >= tile else 0
            pad_w = max(tile - pw, 0) if width >= tile else 0
            if pad_h or pad_w:
                patch = F.pad(patch, (0, pad_w, 0, pad_h), mode="reflect")
            out = model(patch).float()
            use_h, use_w = (y1 - y0) * factor, (x1 - x0) * factor
            out = out[:, :, :use_h, :use_w]
            mask = _blend_weights(
                use_h, use_w, overlap * factor, torch, device, torch.float32,
            ).view(1, 1, use_h, use_w)
            oy, ox = y0 * factor, x0 * factor
            accumulator[:, :, oy:oy + use_h, ox:ox + use_w] += out * mask
            weight_map[:, :, oy:oy + use_h, ox:ox + use_w] += mask
            if progress is not None:
                progress((index + 1) / len(tiles))

    merged = accumulator / weight_map.clamp_min(1e-8)
    return _tensor_to_pil(merged, torch)
