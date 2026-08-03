"""Illusion reliability experiment harness (PR #118).

Records manifests, phase-qualified SDS checkpoints, CLIP 2x2 margins, blind
contact sheets, and acceptance-gate evaluation. Not imported by the online
worker path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

CLIP_MODEL_ID = "openai/clip-vit-large-patch14"


@dataclass(frozen=True)
class PromptPair:
    """A corpus pair carrying both the semantic subjects (no style) and the
    exact legacy oil-painting prompts.

    ``prompt_a`` / ``prompt_b`` are the exact strings used by every prior
    run and must never be re-wrapped by a style template. ``subject_a`` /
    ``subject_b`` are the style-free subjects fed to ``apply_style_template``
    when a different style is requested.

    ``baked_style`` names the style already present in ``prompt_a`` /
    ``prompt_b``. Requesting it (or no style at all) returns those strings
    verbatim; requesting any other style wraps the subjects exactly once.
    Without this, asking a pencil-baked pair for oil silently returned the
    pencil prompts, which would make a style arm compare pencil to pencil.
    """

    pair_id: str
    subject_a: str
    subject_b: str
    prompt_a: str
    prompt_b: str
    baked_style: str = "oil"


FINAL_PAIRS: list[PromptPair] = [
    PromptPair(
        "dog_sloth",
        "a dog sitting in a misty forest",
        "a sloth hanging from a branch",
        "an oil painting of a dog sitting in a misty forest",
        "an oil painting of a sloth hanging from a branch",
    ),
    PromptPair(
        "elephant_swan",
        "an elephant",
        "a swan on a lake",
        "an oil painting of an elephant",
        "an oil painting of a swan on a lake",
    ),
    PromptPair(
        "moose_butterfly",
        "a moose by a lake",
        "a monarch butterfly",
        "an oil painting of a moose by a lake",
        "an oil painting of a monarch butterfly",
    ),
    PromptPair(
        "fox_rabbit",
        "a red fox portrait",
        "a rabbit in a meadow",
        "an oil painting of a red fox portrait",
        "an oil painting of a rabbit in a meadow",
    ),
    PromptPair(
        "squirrel_pelican",
        "a red squirrel",
        "a pelican in flight",
        "an oil painting of a red squirrel",
        "an oil painting of a pelican in flight",
    ),
    PromptPair(
        "gorilla_starfish",
        "a gorilla portrait",
        "a starfish on sand",
        "an oil painting of a gorilla portrait",
        "an oil painting of a starfish on sand",
    ),
    PromptPair(
        "walrus_ladybug",
        "a walrus on ice",
        "a ladybug on a leaf",
        "an oil painting of a walrus on ice",
        "an oil painting of a ladybug on a leaf",
    ),
    PromptPair(
        "mountain_valley",
        "a snowy mountain peak at dawn",
        "a pine valley reflected in an alpine lake",
        "an oil painting of a snowy mountain peak at dawn",
        "an oil painting of a pine valley reflected in an alpine lake",
    ),
]


def _styled_pair(pair_id: str, subject_a: str, subject_b: str, style: str) -> PromptPair:
    """A corpus pair whose prompts are one style template applied once."""
    from worker.illusions import apply_style_template

    return PromptPair(
        pair_id,
        subject_a,
        subject_b,
        apply_style_template(subject_a, style),
        apply_style_template(subject_b, style),
        baked_style=style,
    )


def _reference_pair(pair_id: str, subject_a: str, subject_b: str) -> PromptPair:
    return _styled_pair(pair_id, subject_a, subject_b, "reference_pencil")


# A topology-focused corpus for the author-reference experiment. It is kept
# separate from FINAL_PAIRS so it cannot silently alter the existing 24-case
# acceptance gate.
#
# NOTE the calibration pair carries "reference_sketch", the wording of the one
# cell that has been run and reviewed. Every other pair here carries the
# heavier "reference_pencil" scaffolding, which no GPU cell has validated. That
# difference is a confound, so it is named rather than buried.
REFERENCE_PAIRS: list[PromptPair] = [
    _styled_pair(
        "giraffe_penguin_calibration",
        "a giraffe head",
        "a penguin",
        "reference_sketch",
    ),
    _reference_pair("pine_chandelier", "a pine tree", "an ornate chandelier"),
    _reference_pair("crown_octopus", "a royal crown", "an octopus"),
    _reference_pair("volcano_bouquet", "an erupting volcano", "a bouquet of flowers"),
    _reference_pair("lighthouse_goblet", "a lighthouse", "a stemmed goblet"),
    _reference_pair("gown_jellyfish", "a flowing evening gown", "a jellyfish"),
    _reference_pair(
        "locomotive_eye_control",
        "a side-view steam locomotive",
        "a single human eye",
    ),
]


def _rules_pair(pair_id: str, subject_a: str, subject_b: str) -> PromptPair:
    return _styled_pair(pair_id, subject_a, subject_b, "oil")


# The curated next-batch list from issue #138, which encodes the pairing rules
# derived from the 2026-07-19 curation: one shared scene, frame-scale
# compatibility, and inversion-native anatomy. Never GPU-tested. The issue's
# entries 1 and 13 are dog_sloth and mountain_valley, already in FINAL_PAIRS,
# so they stay there and serve as the proven controls.
PAIRING_RULES_PAIRS: list[PromptPair] = [
    _rules_pair("stag_oak", "a stag standing in a forest clearing", "an ancient oak tree"),
    _rules_pair("eagle_phoenix", "an eagle diving with folded wings", "a phoenix rising in flames"),
    _rules_pair(
        "heron_swan",
        "a heron wading in a still lake",
        "a swan gliding, reflected in the water",
    ),
    _rules_pair(
        "bear_salmon",
        "a bear standing in a rushing river",
        "a salmon leaping up a waterfall",
    ),
    _rules_pair("galleon_whale", "a galleon riding a storm wave", "a whale breaching the surface"),
    _rules_pair("horse_wave", "a white horse rearing in sea mist", "a great ocean wave crashing"),
    _rules_pair(
        "hummingbird_fuchsia",
        "a hummingbird hovering at a blossom",
        "a fuchsia flower hanging from its stem",
    ),
    _rules_pair("candle_droplet", "a candle flame in the dark", "a single falling water drop"),
    _rules_pair("ballerina_leaf", "a ballerina in mid-leap", "an autumn leaf falling"),
    _rules_pair(
        "penguin_bat",
        "a standing emperor penguin on ice",
        "a fruit bat hanging in a wintry cave",
    ),
    _rules_pair("koi_moon", "two koi circling in a dark pond", "a crescent moon over water"),
    _rules_pair("wolf_raven", "a wolf howling, muzzle raised", "a raven perched, head bowed"),
    _rules_pair("fox_snowspiral", "a curled sleeping fox in snow", "a spiral of drifting snow"),
    _rules_pair(
        "octopus_camel",
        "an octopus resting on the seafloor, tentacles spread downward",
        "a camel lying at rest in the desert",
    ),
    _rules_pair(
        "rabbit_fox",
        "a rabbit sitting upright in a tall meadow, filling the frame",
        "a red fox curled at rest",
    ),
    _rules_pair(
        "deer_turtle",
        "a deer drinking at a forest pool",
        "a sea turtle gliding over a sunlit reef",
    ),
]

_SCREEN_IDS = frozenset({"dog_sloth", "fox_rabbit", "walrus_ladybug", "mountain_valley"})
SCREEN_PAIRS: list[PromptPair] = [p for p in FINAL_PAIRS if p.pair_id in _SCREEN_IDS]

PAIR_BY_ID: dict[str, PromptPair] = {
    p.pair_id: p for p in [*FINAL_PAIRS, *REFERENCE_PAIRS, *PAIRING_RULES_PAIRS]
}

_UNSTYLED = frozenset({None, "none"})


def resolve_pair_prompts(pair: PromptPair, style: str | None) -> tuple[list[str], list[str]]:
    """Return ``(subjects, effective_prompts)`` for a corpus pair.

    No style, or the pair's own ``baked_style``, uses its exact prompts
    verbatim (never double-wrapped). Any other style wraps each subject with
    ``apply_style_template`` exactly once.
    """
    subjects = [pair.subject_a, pair.subject_b]
    if style in _UNSTYLED or style == pair.baked_style:
        return subjects, [pair.prompt_a, pair.prompt_b]
    from worker.illusions import apply_style_template

    return subjects, [apply_style_template(subject, style) for subject in subjects]


INSTRUMENTED_CHECKPOINT_STEPS = (60, 125, 250, 500)

AUTHOR_REFERENCE_SDS_STEPS = 10_000
# The ladder the calibration smoke used, expressed as fractions of the SDS
# budget so a shorter arm still records where its own subjects form. At 10,000
# steps this reproduces (500, 2000, 5000, 10000) exactly.
_REFERENCE_CHECKPOINT_FRACTIONS = (0.05, 0.2, 0.5, 1.0)


def _or_default(value: int | None, default: int) -> int:
    return default if value is None else value


def reference_checkpoint_ladder(sds_steps: int) -> tuple[int, ...]:
    """Checkpoint steps for an author-reference run of ``sds_steps`` steps."""
    steps = {int(round(sds_steps * f)) for f in _REFERENCE_CHECKPOINT_FRACTIONS}
    return tuple(sorted(s for s in steps if s > 0))


CONTROL_PAIR_IDS = frozenset({"elephant_swan", "moose_butterfly"})

GATE_MIN_KEEPERS = 16
GATE_MIN_PAIRS_TWO_THIRDS = 6
GATE_MIN_NET_CONVERSIONS = 4
GATE_MAX_RUNTIME_S = 3600.0
GATE_REQUIRED_CASES = 24

_PY_CMD = "worker/.venv/bin/python -m worker.illusion_experiment"


def torch_no_grad_safe(fn):
    """Decorator that uses torch.no_grad when torch is available."""

    def wrapper(*args, **kwargs):
        try:
            import torch

            with torch.no_grad():
                return fn(*args, **kwargs)
        except ImportError:
            return fn(*args, **kwargs)

    return wrapper


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_root(), text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {
        "torch": None,
        "rocm": None,
        "diffusers": None,
        "transformers": None,
    }
    try:
        import torch

        versions["torch"] = torch.__version__
        versions["rocm"] = getattr(torch.version, "hip", None)
    except ImportError:
        pass
    try:
        import diffusers

        versions["diffusers"] = diffusers.__version__
    except ImportError:
        pass
    try:
        import transformers

        versions["transformers"] = transformers.__version__
    except ImportError:
        pass
    return versions


def gpu_name() -> str | None:
    try:
        import torch

        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["rocm-smi", "--showproductname"], text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            if "Card series" in line or "GPU" in line:
                return line.split(":", 1)[-1].strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return None


def model_revision(model_id: str) -> str | None:
    try:
        from huggingface_hub import model_info

        return model_info(model_id).sha
    except Exception:
        return None


def peak_vram_mb() -> float | None:
    try:
        import torch

        if torch.cuda.is_available():
            return float(torch.cuda.max_memory_allocated() / (1024**2))
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["rocm-smi", "--showmeminfo", "vram"], text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            if "Used" in line and "MB" in line:
                parts = line.replace(",", " ").split()
                for index, part in enumerate(parts):
                    if part == "MB" and index > 0:
                        try:
                            return float(parts[index - 1])
                        except ValueError:
                            continue
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return None


def write_manifest_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    tmp.replace(path)


def is_completed_run(run_dir: Path) -> bool:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError:
        return False
    if manifest.get("status") != "completed":
        return False
    d1 = run_dir / "derived_1.png"
    d2 = run_dir / "derived_2.png"
    if not d1.is_file() or not d2.is_file():
        return False
    # Reject all-black / NaN-cast PNGs so broken SDXL runs are retried.
    try:
        from PIL import Image

        for path in (d1, d2):
            extrema = Image.open(path).getextrema()
            channels = extrema if isinstance(extrema[0], tuple) else (extrema,)
            if not any(lo != hi or lo > 0 for lo, hi in channels):
                return False
    except OSError:
        return False
    return True


# Catastrophe floors for a derived image: a near-constant field, i.e. the
# optimizer produced nothing at all. Calibrated against this repository's own
# evidence, measured over every completed run under .local/:
#
#   worst non-catastrophic run (Wave 1 legacy)   std 0.101   detail 0.0054
#   human-approved calibration smoke             std 0.351   detail 0.0122
#   catastrophic SDXL P0 cell (uniform gray)     std 0.006   detail 0.0017
#
# These are DELIBERATELY far below any plausible quality bar. Measurement shows
# no simple image statistic separates the human-rejected SDXL pilot (median
# detail 0.0088) from the human-tolerated Wave 1 corpus (median 0.0097), so an
# automated quality gate is not available for this workload and is not
# attempted here. Clearing these floors means "the optimizer emitted an image",
# never "the image is good". Quality stays a human judgment.
DEGENERATE_STD_FLOOR = 0.05
DEGENERATE_DETAIL_FLOOR = 0.003
# Gradient is measured on a downscale: enough to detect a flat field, and cheap
# enough to run once per cell without touching the GPU.
_DEGENERATE_PROBE_SIZE = 128


def image_flatness(path: Path) -> tuple[float, float]:
    """Return ``(luma std, mean absolute gradient)`` in [0, 1] for an image.

    ``(0.0, 0.0)`` for a file that cannot be read, so unreadable output counts
    as catastrophic rather than silently passing.
    """
    try:
        from PIL import Image

        probe = Image.open(path).convert("L")
        probe = probe.resize((_DEGENERATE_PROBE_SIZE, _DEGENERATE_PROBE_SIZE))
    except OSError:
        return 0.0, 0.0
    width, height = probe.size
    flat = [b / 255.0 for b in probe.tobytes()]
    n = len(flat)
    if n == 0:
        return 0.0, 0.0
    mean = sum(flat) / n
    std = (sum((v - mean) ** 2 for v in flat) / n) ** 0.5
    horizontal = [
        abs(flat[y * width + x + 1] - flat[y * width + x])
        for y in range(height)
        for x in range(width - 1)
    ]
    vertical = [
        abs(flat[(y + 1) * width + x] - flat[y * width + x])
        for y in range(height - 1)
        for x in range(width)
    ]
    deltas = horizontal + vertical
    detail = sum(deltas) / len(deltas) if deltas else 0.0
    return std, detail


def local_snapshot(model_id: str) -> str:
    """Resolve a Hub id to its cached snapshot directory when one exists.

    ``HF_HUB_OFFLINE`` refuses an *incomplete* snapshot even when every file the
    pipeline actually needs is on disk. This machine's SD 1.5 cache is missing
    the safety checker and 22 other files, so the Hub-id form aborts before
    loading anything, while a snapshot path loads straight off disk.

    The 36-cell plan hardcoded absolute paths for exactly this reason. Resolving
    here means a plan cannot be built with the footgun still in it. Paths that
    are already directories pass through unchanged, so this is idempotent.
    """
    if os.path.isdir(model_id):
        return model_id
    org, _, name = model_id.partition("/")
    if not name:
        return model_id
    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    repo = hub / f"models--{org}--{name}"
    ref = repo / "refs" / "main"
    if ref.is_file():
        pinned = repo / "snapshots" / ref.read_text().strip()
        if pinned.is_dir():
            return str(pinned)
    snapshots = repo / "snapshots"
    found = sorted(p for p in snapshots.glob("*") if p.is_dir()) if snapshots.is_dir() else []
    # Only an unambiguous cache resolves; several snapshots means the caller has
    # to name the revision it wants rather than have one guessed.
    return str(found[0]) if len(found) == 1 else model_id


def degenerate_run(run_dir: Path) -> bool:
    """True when a completed run's derived views are near-constant fields.

    Used by the unattended campaign driver to stop an axis that is emitting
    nothing at all, which is what wasted the SDXL pilot night. It cannot and
    does not judge whether a formed image is a good illusion.
    """
    derived = sorted(run_dir.glob("derived_*.png"))
    if not derived:
        return True
    for path in derived:
        std, detail = image_flatness(path)
        if std < DEGENERATE_STD_FLOOR or detail < DEGENERATE_DETAIL_FLOOR:
            return True
    return False


def resolve_run_out(requested: Path) -> Path | None:
    """Pick an output directory; return None when the run is already complete."""
    if is_completed_run(requested):
        return None
    if not requested.exists():
        return requested
    parent = requested.parent
    stem = requested.name
    attempt = 1
    while True:
        candidate = parent / f"{stem}_attempt_{attempt}"
        if not candidate.exists():
            return candidate
        if is_completed_run(candidate):
            return None
        attempt += 1


def checkpoint_phase_name(
    step: int,
    *,
    sds_steps: int,
    checkpoint_steps: tuple[int, ...],
    saved_sds: set[int],
) -> str:
    if step in checkpoint_steps and step not in saved_sds:
        return f"sds_{step:04d}"
    if step == sds_steps:
        return "final"
    return f"sds_{step:04d}"


_CLIP_REVISION: str | None = None
_CLIP_REVISION_RESOLVED = False


def clip_model_revision() -> str | None:
    """Resolve and cache the pinned CLIP checkpoint revision once."""
    global _CLIP_REVISION, _CLIP_REVISION_RESOLVED
    if not _CLIP_REVISION_RESOLVED:
        _CLIP_REVISION = model_revision(CLIP_MODEL_ID)
        _CLIP_REVISION_RESOLVED = True
    return _CLIP_REVISION


def load_clip(device: str = "cpu") -> tuple[Any, Any, str | None]:
    from transformers import CLIPModel, CLIPProcessor

    revision = clip_model_revision()
    kwargs: dict[str, Any] = {}
    if revision:
        kwargs["revision"] = revision
    model = cast(Any, CLIPModel.from_pretrained(CLIP_MODEL_ID, **kwargs))
    model = model.to(device).eval()
    processor = CLIPProcessor.from_pretrained(CLIP_MODEL_ID, **kwargs)
    return model, processor, revision


def _clip_feature_tensor(feats: Any) -> Any:
    """Normalize CLIP get_*_features output to a 2D embedding tensor.

    transformers 5.x returns ``BaseModelOutputWithPooling`` (projected
    features in ``pooler_output``). Older releases returned the tensor
    directly.
    """
    pooler = getattr(feats, "pooler_output", None)
    if pooler is not None:
        return pooler
    return feats


def _clip_text_embeds(
    model: Any,
    processor: Any,
    prompts: list[str],
    device: str,
    cache: dict[str, Any] | None,
):
    """Normalized CLIP text embeddings, caching by exact prompt string."""
    import torch

    if cache is None:
        cache = {}
    missing = [prompt for prompt in prompts if prompt not in cache]
    if missing:
        inputs = processor(text=missing, return_tensors="pt", padding=True)
        text_inputs = {
            key: value.to(device)
            for key, value in inputs.items()
            if key in ("input_ids", "attention_mask")
        }
        feats = _clip_feature_tensor(model.get_text_features(**text_inputs))
        feats = feats / feats.norm(dim=-1, keepdim=True)
        for prompt, feat in zip(missing, feats, strict=True):
            cache[prompt] = feat
    return torch.stack([cache[prompt] for prompt in prompts])


@torch_no_grad_safe
def clip_similarity_matrix(
    images: list[Any],
    prompts: list[str],
    *,
    model: Any = None,
    processor: Any = None,
    device: str = "cpu",
    text_cache: dict[str, Any] | None = None,
) -> list[list[float]]:
    """Return matrix[view_i][prompt_j] CLIP cosine similarities.

    Text embeddings are cached via ``text_cache`` (keyed by prompt) and the
    whole image set is embedded in a single batch.
    """
    from PIL import Image

    if model is None or processor is None:
        model, processor, _revision = load_clip(device)

    pil_images = []
    for image in images:
        if hasattr(image, "squeeze"):
            array = (image.squeeze(0).permute(1, 2, 0).detach().cpu().numpy() * 255).clip(0, 255)
            pil_images.append(Image.fromarray(array.astype("uint8")))
        else:
            pil_images.append(image)

    text_embeds = _clip_text_embeds(model, processor, prompts, device, text_cache)
    image_inputs = processor(images=pil_images, return_tensors="pt")
    pixel_values = image_inputs["pixel_values"].to(device)
    image_embeds = _clip_feature_tensor(model.get_image_features(pixel_values=pixel_values))
    image_embeds = image_embeds / image_embeds.norm(dim=-1, keepdim=True)
    return (image_embeds @ text_embeds.T).detach().cpu().tolist()


def pair_margins(sim_matrix: list[list[float]]) -> tuple[list[float], float]:
    """margin_i = sim(i,i) - sim(i, other); pair score = min(margins)."""
    margins = []
    for index in range(len(sim_matrix)):
        correct = sim_matrix[index][index]
        others = [sim_matrix[index][j] for j in range(len(sim_matrix)) if j != index]
        margins.append(correct - max(others) if others else correct)
    return margins, min(margins) if margins else 0.0


def styled_prompts(prompts: list[str], style: str | None) -> list[str]:
    from worker.illusions import apply_style_template

    return [apply_style_template(prompt, style) for prompt in prompts]


def tensor_to_pil(image: Any):
    from PIL import Image

    if hasattr(image, "squeeze"):
        array = (image.squeeze(0).permute(1, 2, 0).detach().cpu().numpy() * 255).clip(0, 255)
        return Image.fromarray(array.astype("uint8"))
    return image.convert("RGB")


def make_contact_sheet(
    cells: list[tuple[Any, str]],
    out_path: Path,
    *,
    cols: int = 4,
    cell_size: int = 256,
) -> None:
    from PIL import Image, ImageDraw, ImageFont

    if not cells:
        raise ValueError("no cells")
    rows = (len(cells) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell_size, rows * cell_size), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None
    for index, (image, label) in enumerate(cells):
        pil = tensor_to_pil(image).resize((cell_size, cell_size))
        row, col = divmod(index, cols)
        sheet.paste(pil, (col * cell_size, row * cell_size))
        draw.text((col * cell_size + 4, row * cell_size + 4), label, fill=(255, 255, 0), font=font)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)


def _stage_dir(run_dir: Path, stage: str) -> Path | None:
    """Resolve a stage name to the directory holding its derived images."""
    if stage == "final":
        return run_dir
    if stage == "dream_d1":
        candidate = run_dir / "ckpt_dream_round_01"
        return candidate if candidate.is_dir() else None
    if stage == "sds_end":
        sds = sorted(
            run_dir.glob("ckpt_sds_[0-9][0-9][0-9][0-9]*"),
            key=lambda path: int(path.name.removeprefix("ckpt_sds_")),
        )
        return sds[-1] if sds else None
    raise ValueError(f"unknown stage {stage!r}")


YIELD_STAGES = ("final", "dream_d1", "sds_end")


def build_yield_sheets(root: Path, out: Path, stage: str = "final") -> dict[str, Any]:
    """One sheet per pair, all its seeds side by side, for reading yield.

    The blind stage sheets are the acceptance path and stay that way, but they
    shuffle every cell of every pair together. At a hundred-odd runs that is one
    very tall strip per stage, and the shuffle deliberately destroys the grouping
    that answers what a yield sweep asks: which PAIRS work, and how often across
    seeds.

    ``stage`` picks which images are shown, and it matters. On both pre-window
    pairs inspected, ``dream_d1`` held more tonal detail than the final, which
    round 8 flattened. Reading yield off finals alone would understate it.

    Nothing is blinded here because there is nothing to blind against. The
    window runs one recipe, so there is no A/B whose labels could bias a
    rating. Use the blind sheets before promoting anything.
    """
    if stage not in YIELD_STAGES:
        raise ValueError(f"unknown stage {stage!r}; choose from {list(YIELD_STAGES)}")
    by_pair: dict[str, list[tuple[int, Path, dict[str, Any]]]] = {}
    for manifest_path in sorted(root.rglob("manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("status") != "completed":
            continue
        pair_id = manifest.get("pair_id") or "unknown"
        seed = manifest.get("config", {}).get("seed", -1)
        by_pair.setdefault(pair_id, []).append((seed, manifest_path.parent, manifest))
    if not by_pair:
        raise ValueError(f"no completed runs under {root}")

    out.mkdir(parents=True, exist_ok=True)
    from PIL import Image

    summary: dict[str, Any] = {"stage": stage, "pairs": {}}
    for pair_id, runs in sorted(by_pair.items()):
        cells: list[tuple[Any, str]] = []
        alive = 0
        for seed, run_dir, _manifest in sorted(runs):
            source = _stage_dir(run_dir, stage)
            if source is not None:
                for view in (1, 2):
                    path = source / f"derived_{view}.png"
                    if path.is_file():
                        cells.append((Image.open(path).convert("RGB"), f"s{seed} v{view}"))
            if not degenerate_run(run_dir):
                alive += 1
        if cells:
            make_contact_sheet(cells, out / f"{pair_id}.png", cols=4)
        summary["pairs"][pair_id] = {
            "runs": len(runs),
            "non_degenerate": alive,
            "seeds": sorted(seed for seed, _dir, _m in runs),
        }

    rows = "\n".join(
        f'<li><a href="{pair_id}.png">{pair_id}</a> '
        f"<span>{info['non_degenerate']}/{info['runs']} produced an image</span></li>"
        for pair_id, info in summary["pairs"].items()
    )
    (out / "index.html").write_text(
        "<!doctype html><meta charset=utf-8>"
        f"<title>Illusion yield by pair ({stage})</title>"
        "<style>body{font:14px system-ui;background:#181818;color:#eee;margin:2rem}"
        "a{color:#8cf}li{margin:.35rem 0}span{color:#999}</style>"
        f"<h1>Illusion yield by pair: {stage}</h1>"
        "<p>Counts are cells that emitted an image, which is not the same as a "
        "readable illusion. Judge the sheets.</p>"
        f"<ul>{rows}</ul>\n"
    )
    write_manifest_atomic(out / "yield.json", summary)
    return summary


# Cells per blind sheet page. One sheet per stage does not survive scale: 180
# runs give 360 cells a stage, which at four columns is a 1024x23040 strip that
# nobody can rate. Paginating keeps the shuffle, so the review stays blind, and
# makes each page a screenful.
BLIND_CELLS_PER_PAGE = 24


def build_stage_blind_sheets(
    root: Path, out: Path, seed: int = 0, per_page: int = BLIND_CELLS_PER_PAGE
) -> dict[str, Any]:
    """Build separate SDS-end, Dream-d1, and final sheets with a rating template."""
    runs: list[tuple[Path, dict[str, Any]]] = []
    for manifest_path in sorted(root.rglob("manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("status") == "completed":
            runs.append((manifest_path.parent, manifest))
    if not runs:
        raise ValueError(f"no completed runs under {root}")

    rng = random.Random(seed)
    stage_rows: dict[str, list[dict[str, Any]]] = {"sds_end": [], "dream_d1": [], "final": []}
    for run_dir, manifest in runs:
        sds_dirs = sorted(
            run_dir.glob("ckpt_sds_[0-9][0-9][0-9][0-9]*"),
            key=lambda path: int(path.name.removeprefix("ckpt_sds_")),
        )
        stage_dirs: dict[str, Path] = {
            "final": run_dir,
            "dream_d1": run_dir / "ckpt_dream_round_01",
        }
        if sds_dirs:
            stage_dirs["sds_end"] = sds_dirs[-1]
        for stage, stage_dir in stage_dirs.items():
            paths = [stage_dir / "derived_1.png", stage_dir / "derived_2.png"]
            if not all(path.is_file() for path in paths):
                continue
            for view, path in enumerate(paths, start=1):
                stage_rows[stage].append(
                    {
                        "run_dir": str(run_dir),
                        "pair_id": manifest.get("pair_id"),
                        "seed": manifest.get("config", {}).get("seed"),
                        "stage": stage,
                        "view": view,
                        "path": str(path),
                    }
                )

    out.mkdir(parents=True, exist_ok=True)
    answer_rows: list[dict[str, Any]] = []
    ratings: list[dict[str, Any]] = []
    page_counts: dict[str, int] = {}
    from PIL import Image

    for stage, rows in stage_rows.items():
        rng.shuffle(rows)
        cells = []
        for index, row in enumerate(rows, start=1):
            code = f"{stage[:2].upper()}-{index:03d}"
            row["case_id"] = code
            cells.append((Image.open(row["path"]).convert("RGB"), code))
            answer_rows.append(row)
            ratings.append(
                {
                    "case_id": code,
                    "keep": None,
                    "subject_read": None,
                    "artifact": None,
                    "notes": "",
                }
            )
        if not cells:
            continue
        if per_page and len(cells) > per_page:
            pages = [cells[i : i + per_page] for i in range(0, len(cells), per_page)]
            for number, page in enumerate(pages, start=1):
                make_contact_sheet(page, out / f"{stage}-p{number:02d}.png", cols=4)
            page_counts[stage] = len(pages)
        else:
            make_contact_sheet(cells, out / f"{stage}.png", cols=4)
            page_counts[stage] = 1
    _write_blind_index(out, page_counts, stage_rows)
    write_manifest_atomic(out / "answer-key.json", {"seed": seed, "cases": answer_rows})
    # ratings.jsonl is edited BY HAND, so rebuilding must never silently destroy
    # a partial review. Rebuilds are routine (more cells complete, a new stage,
    # different pagination) and the cost of clobbering is a human's afternoon.
    ratings_path = out / "ratings.jsonl"
    if ratings_path.is_file():
        existing = [
            json.loads(line) for line in ratings_path.read_text().splitlines() if line.strip()
        ]
        if any(row.get("keep") is not None for row in existing):
            backup = ratings_path.with_suffix(".jsonl.rated")
            backup.write_text(ratings_path.read_text())
            raise FileExistsError(
                f"{ratings_path} already holds verdicts; copied to {backup.name} and "
                "refusing to overwrite. Move or delete the original to rebuild."
            )
    ratings_path.write_text("".join(json.dumps(row) + "\n" for row in ratings))
    summary = {
        "runs": len(runs),
        "cells": {stage: len(rows) for stage, rows in stage_rows.items()},
        "pages": page_counts,
    }
    write_manifest_atomic(out / "summary.json", summary)
    return summary


def _write_blind_index(
    out: Path, page_counts: dict[str, int], stage_rows: dict[str, list[dict[str, Any]]]
) -> None:
    """Index the paginated blind sheets, without leaking what anything is."""
    blocks = []
    for stage, pages in page_counts.items():
        names = (
            [f"{stage}-p{n:02d}.png" for n in range(1, pages + 1)]
            if pages > 1
            else [f"{stage}.png"]
        )
        links = " ".join(f'<a href="{n}">{i + 1}</a>' for i, n in enumerate(names))
        blocks.append(
            f"<h2>{stage}</h2><p>{len(stage_rows[stage])} cells over {pages} page(s)</p>"
            f"<p class=pages>{links}</p>"
        )
    (out / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><title>Blind stage review</title>"
        "<style>body{font:14px system-ui;background:#181818;color:#eee;margin:2rem;max-width:52rem}"
        "a{color:#8cf;margin-right:.5rem}h2{margin-top:1.5rem}code{color:#ffd}</style>"
        "<h1>Blind stage review</h1>"
        "<p>Rate each labelled cell in <code>ratings.jsonl</code>: set <code>keep</code> "
        "true or false, and optionally <code>subject_read</code> and "
        "<code>artifact</code>. Freeze the file before opening "
        "<code>answer-key.json</code>, which is what makes this blind.</p>"
        "<p>Rate the three stages separately. On this corpus no stage dominated: "
        "sds_end was best in 38% of cells, dream_d1 32%, final 31%.</p>" + "".join(blocks) + "\n"
    )


def score_images_for_prompts(
    derived_paths: list[Path],
    prompts: list[str],
    *,
    style: str | None,
    clip_model: Any = None,
    clip_processor: Any = None,
    clip_revision: str | None = None,
    device: str = "cpu",
    text_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from PIL import Image

    images = [Image.open(path) for path in derived_paths]
    styled = styled_prompts(prompts, style)
    sims = clip_similarity_matrix(
        images,
        styled[: len(images)],
        model=clip_model,
        processor=clip_processor,
        device=device,
        text_cache=text_cache,
    )
    margins, score = pair_margins(sims)
    return {
        "clip_model_id": CLIP_MODEL_ID,
        "clip_model_revision": clip_revision
        if clip_revision is not None
        else clip_model_revision(),
        "clip_matrix": sims,
        "clip_margins": margins,
        "clip_pair_score": score,
    }


def _run_prompts_and_style(manifest: dict[str, Any]) -> tuple[list[str], str | None]:
    """Prefer stored effective prompts (no re-styling); fall back to config."""
    effective = manifest.get("effective_prompts")
    if effective and len(effective) >= 2:
        return list(effective), None
    prompts = manifest.get("config", {}).get("prompts") or manifest.get("prompts")
    if not prompts or len(prompts) < 2:
        raise ValueError("manifest has no prompts")
    return list(prompts), manifest.get("config", {}).get("style")


def score_run_dir(
    run_dir: Path,
    *,
    device: str = "cpu",
    update_manifest: bool = False,
    clip_model: Any = None,
    clip_processor: Any = None,
    clip_revision: str | None = None,
    text_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"missing manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    try:
        prompts, style = _run_prompts_and_style(manifest)
    except ValueError as exc:
        raise ValueError(f"run {run_dir}: {exc}") from exc

    if clip_model is None or clip_processor is None:
        clip_model, clip_processor, clip_revision = load_clip(device)
    if clip_revision is None:
        clip_revision = clip_model_revision()
    if text_cache is None:
        text_cache = {}

    def _score(paths: list[Path]) -> dict[str, Any]:
        return score_images_for_prompts(
            paths,
            list(prompts),
            style=style,
            clip_model=clip_model,
            clip_processor=clip_processor,
            clip_revision=clip_revision,
            device=device,
            text_cache=text_cache,
        )

    scored: dict[str, Any] = {
        "clip_model_id": CLIP_MODEL_ID,
        "clip_model_revision": clip_revision,
        "scored_at": datetime.now(timezone.utc).isoformat(),
        "effective_prompts": list(prompts),
        "style": style,
        "checkpoints": {},
    }

    # Score the root derived images as the canonical final first, so a
    # redundant ckpt_final directory is not scored twice.
    final_scored = False
    final_derived = [run_dir / "derived_1.png", run_dir / "derived_2.png"]
    if all(path.is_file() for path in final_derived):
        final_entry = _score(final_derived)
        scored["final"] = final_entry
        scored["checkpoints"]["final"] = final_entry
        final_scored = True

    for ckpt_dir in sorted(run_dir.glob("ckpt_*")):
        phase = ckpt_dir.name[len("ckpt_") :]
        if phase == "final" and final_scored:
            continue
        derived = [ckpt_dir / "derived_1.png", ckpt_dir / "derived_2.png"]
        if not all(path.is_file() for path in derived):
            continue
        entry = _score(derived)
        # Sidecar only - never mutate raw optimizer scores.json / manifest.
        write_manifest_atomic(ckpt_dir / "clip_scores.json", entry)
        scored["checkpoints"][phase] = entry
        scored[phase] = entry

    write_manifest_atomic(run_dir / "clip_scores.json", scored)
    if update_manifest:
        # Explicit opt-in only; default path keeps raw manifests immutable.
        merged = dict(manifest)
        merged["clip_scored_at"] = scored["scored_at"]
        merged["final"] = {**(manifest.get("final") or {}), **(scored.get("final") or {})}
        ck = dict(manifest.get("checkpoints") or {})
        for phase, entry in scored["checkpoints"].items():
            ck[phase] = {**(ck.get(phase) or {}), **entry}
        merged["checkpoints"] = ck
        write_manifest_atomic(manifest_path, merged)
    return scored


def score_tree(root: Path, *, device: str = "cpu") -> list[Path]:
    clip_model, clip_processor, clip_revision = load_clip(device)
    text_cache: dict[str, Any] = {}
    scored_dirs: list[Path] = []
    for manifest_path in sorted(root.rglob("manifest.json")):
        run_dir = manifest_path.parent
        if not (run_dir / "derived_1.png").is_file():
            continue
        score_run_dir(
            run_dir,
            device=device,
            update_manifest=False,
            clip_model=clip_model,
            clip_processor=clip_processor,
            clip_revision=clip_revision,
            text_cache=text_cache,
        )
        scored_dirs.append(run_dir)
    return scored_dirs


def build_blind_sheet(
    cases: list[dict[str, Any]],
    out_dir: Path,
    *,
    seed: int = 0,
) -> Path:
    """Legacy simple blind sheet: one image per case."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    order = list(range(len(cases)))
    rng.shuffle(order)
    cells: list[tuple[Any, str]] = []
    key: list[dict[str, Any]] = []
    for blind_index, case_index in enumerate(order):
        case = cases[case_index]
        label = f"case-{blind_index:02d}"
        cells.append((case["image"], label))
        key.append(
            {
                "blind_label": label,
                "case_index": case_index,
                "config_name": case.get("config_name"),
                "pair": case.get("pair"),
                "seed": case.get("seed"),
                "orientation": case.get("orientation"),
                "path": str(case.get("path", "")),
            }
        )
    sheet_path = out_dir / "blind_sheet.png"
    make_contact_sheet(cells, sheet_path)
    write_manifest_atomic(out_dir / "answer_key.json", {"seed": seed, "cases": key})
    return sheet_path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity_from_run(run_dir: Path) -> dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            manifest = {}
    images = {}
    for name in ("derived_1.png", "derived_2.png"):
        path = run_dir / name
        if path.is_file():
            images[name] = file_sha256(path)
    return {
        "run_dir": str(run_dir),
        "spec_hash": manifest.get("spec_hash"),
        "optimizer_fingerprint": manifest.get("optimizer_fingerprint"),
        "campaign_id": manifest.get("campaign_id"),
        "plan_sha": manifest.get("plan_sha"),
        "git_sha": manifest.get("git_sha") or manifest.get("code_sha"),
        "model_id": (manifest.get("config") or {}).get("model_id") or manifest.get("model_id"),
        "dream_model_id": (manifest.get("config") or {}).get("dream_model_id")
        or manifest.get("dream_model_id"),
        "pair_id": manifest.get("pair_id") or (manifest.get("config") or {}).get("pair_id"),
        "seed": manifest.get("seed") or (manifest.get("config") or {}).get("seed"),
        "image_sha256": images,
    }


def _run_image_paths(run_dir: Path) -> tuple[Path, Path] | None:
    d1 = run_dir / "derived_1.png"
    d2 = run_dir / "derived_2.png"
    if d1.is_file() and d2.is_file():
        return d1, d2
    return None


def build_matched_blind(
    final_root: Path,
    out_dir: Path,
    *,
    seed: int = 0,
) -> Path:
    """Build 24 matched legacy-vs-finalist blind cases (8 pairs x 3 seeds)."""
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    cases: list[dict[str, Any]] = []
    cells: list[tuple[Any, str]] = []

    case_number = 0
    for pair in FINAL_PAIRS:
        pair_id = pair.pair_id
        for seed_value in (0, 1, 2):
            legacy_dir = final_root / "legacy" / f"{pair_id}_seed{seed_value}"
            finalist_dir = final_root / "finalist" / f"{pair_id}_seed{seed_value}"
            legacy_images = _run_image_paths(legacy_dir)
            finalist_images = _run_image_paths(finalist_dir)
            if legacy_images is None or finalist_images is None:
                raise FileNotFoundError(
                    f"missing derived images for {pair_id} seed {seed_value} under {final_root}"
                )
            case_id = f"case-{case_number:02d}"
            case_number += 1
            assign_a = rng.choice(["legacy", "finalist"])
            assign_b = "finalist" if assign_a == "legacy" else "legacy"
            col_a_dir = legacy_dir if assign_a == "legacy" else finalist_dir
            col_b_dir = finalist_dir if assign_b == "finalist" else legacy_dir
            case_cells = [
                (Image.open(col_a_dir / "derived_1.png"), f"{case_id} A upright"),
                (Image.open(col_a_dir / "derived_2.png"), f"{case_id} A flipped"),
                (Image.open(col_b_dir / "derived_1.png"), f"{case_id} B upright"),
                (Image.open(col_b_dir / "derived_2.png"), f"{case_id} B flipped"),
            ]
            rng.shuffle(case_cells)
            for image, label in case_cells:
                cells.append((image, label))
            cases.append(
                {
                    "case_id": case_id,
                    "pair_id": pair_id,
                    "seed": seed_value,
                    "column_a": assign_a,
                    "column_b": assign_b,
                    "legacy_dir": str(legacy_dir),
                    "finalist_dir": str(finalist_dir),
                    "legacy_identity": _identity_from_run(legacy_dir),
                    "finalist_identity": _identity_from_run(finalist_dir),
                }
            )

    sheet_path = out_dir / "matched_blind_sheet.png"
    make_contact_sheet(cells, sheet_path, cols=4)
    write_manifest_atomic(out_dir / "answer_key.json", {"seed": seed, "cases": cases})

    template_lines = []
    for case in cases:
        template_lines.append(
            json.dumps(
                {
                    "case_id": case["case_id"],
                    "keep_a": None,
                    "keep_b": None,
                    "notes": "",
                }
            )
        )
    (out_dir / "ratings_template.jsonl").write_text("\n".join(template_lines) + "\n")
    return sheet_path


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _keeper_from_rating(keep_a: Any, keep_b: Any, column: str) -> bool | None:
    if column == "a":
        return bool(keep_a) if keep_a is not None else None
    return bool(keep_b) if keep_b is not None else None


def _manifest_field(manifest: dict[str, Any], field: str) -> Any:
    if field == "seed":
        config = manifest.get("config") or {}
        if "seed" in config:
            return config["seed"]
    return manifest.get(field)


def evaluate_ratings(
    ratings_path: Path,
    answer_key_path: Path,
    final_root: Path,
) -> dict[str, Any]:
    rating_rows = _load_jsonl(ratings_path)
    ratings = {row["case_id"]: row for row in rating_rows if "case_id" in row}
    answer = json.loads(answer_key_path.read_text())
    cases = answer["cases"]

    failures: list[str] = []

    answer_id_set = {case["case_id"] for case in cases}
    rating_ids = [row.get("case_id") for row in rating_rows]
    unique_rating_ids = {cid for cid in rating_ids if cid is not None}

    if len(cases) != GATE_REQUIRED_CASES:
        failures.append(f"answer key has {len(cases)} cases, expected {GATE_REQUIRED_CASES}")
    if len(unique_rating_ids) != GATE_REQUIRED_CASES:
        failures.append(
            f"ratings have {len(unique_rating_ids)} unique case_ids, expected {GATE_REQUIRED_CASES}"
        )
    if len(rating_ids) != len(unique_rating_ids):
        failures.append("ratings contain duplicate case_ids")
    missing_ratings = sorted(answer_id_set - unique_rating_ids)
    if missing_ratings:
        failures.append(f"ratings missing case_ids: {missing_ratings}")
    unknown_ratings = sorted(unique_rating_ids - answer_id_set)
    if unknown_ratings:
        failures.append(f"ratings reference unknown case_ids: {unknown_ratings}")

    finalist_keepers = 0
    pair_finalist_kept: dict[str, list[bool]] = {pair.pair_id: [] for pair in FINAL_PAIRS}
    conversions = 0
    regressions = 0
    runtime_violations: list[str] = []
    oom_runs: list[str] = []
    missing_runs: list[str] = []

    for case in cases:
        case_id = case["case_id"]
        rating = ratings.get(case_id)
        keep_a = rating.get("keep_a") if rating else None
        keep_b = rating.get("keep_b") if rating else None
        if rating is None:
            failures.append(f"{case_id}: missing rating")
        else:
            if not isinstance(keep_a, bool):
                failures.append(f"{case_id}: keep_a must be a boolean (got {keep_a!r})")
            if not isinstance(keep_b, bool):
                failures.append(f"{case_id}: keep_b must be a boolean (got {keep_b!r})")

        finalist_col = "a" if case["column_a"] == "finalist" else "b"
        legacy_col = "b" if finalist_col == "a" else "a"
        finalist_keep = _keeper_from_rating(keep_a, keep_b, finalist_col)
        legacy_keep = _keeper_from_rating(keep_a, keep_b, legacy_col)
        if finalist_keep:
            finalist_keepers += 1
        if finalist_keep is not None:
            pair_finalist_kept[case["pair_id"]].append(finalist_keep)
        if legacy_keep is False and finalist_keep is True:
            conversions += 1
        if legacy_keep is True and finalist_keep is False:
            regressions += 1

        for tag, rel in (("legacy", case["legacy_dir"]), ("finalist", case["finalist_dir"])):
            run_dir = Path(rel)
            manifest_path = run_dir / "manifest.json"
            if not manifest_path.is_file():
                missing_runs.append(f"{tag}:{case_id}")
                failures.append(f"{tag}:{case_id}: missing manifest {manifest_path}")
                continue
            manifest = json.loads(manifest_path.read_text())
            status = manifest.get("status")
            if status != "completed":
                failures.append(f"{tag}:{case_id}: status={status!r} not completed")
            for field in ("pair_id", "seed", "git_sha", "spec"):
                if field not in case:
                    continue
                actual = _manifest_field(manifest, field)
                if actual != case[field]:
                    failures.append(
                        f"{tag}:{case_id}: {field} mismatch "
                        f"(answer={case[field]!r}, run={actual!r})"
                    )
            total_s = (manifest.get("phase_timings") or {}).get("total_s")
            if total_s is None:
                failures.append(f"{tag}:{case_id}: phase_timings.total_s missing")
            elif float(total_s) > GATE_MAX_RUNTIME_S:
                runtime_violations.append(f"{tag}:{case_id}:{float(total_s):.0f}s")
            error = manifest.get("error") or ""
            if status == "failed" or "oom" in str(error).lower():
                oom_runs.append(f"{tag}:{case_id}")

    if runtime_violations:
        failures.append(f"runtime over {GATE_MAX_RUNTIME_S:.0f}s: {runtime_violations}")
    if oom_runs:
        failures.append(f"failed/oom runs: {oom_runs}")

    pairs_two_thirds = sum(1 for pair in FINAL_PAIRS if sum(pair_finalist_kept[pair.pair_id]) >= 2)
    control_results = {
        pair_id: sum(pair_finalist_kept[pair_id]) for pair_id in sorted(CONTROL_PAIR_IDS)
    }
    controls_pass = all(count >= 2 for count in control_results.values())
    net_conversion_ok = conversions - regressions >= GATE_MIN_NET_CONVERSIONS
    metrics_pass = (
        finalist_keepers >= GATE_MIN_KEEPERS
        and pairs_two_thirds >= GATE_MIN_PAIRS_TWO_THIRDS
        and controls_pass
        and net_conversion_ok
    )
    gate_pass = metrics_pass and not failures

    return {
        "finalist_keepers": finalist_keepers,
        "finalist_keepers_required": GATE_MIN_KEEPERS,
        "pairs_two_thirds": pairs_two_thirds,
        "pairs_two_thirds_required": GATE_MIN_PAIRS_TWO_THIRDS,
        "control_results": control_results,
        "controls_pass": controls_pass,
        "conversions": conversions,
        "regressions": regressions,
        "net_conversions": conversions - regressions,
        "net_conversions_required": GATE_MIN_NET_CONVERSIONS,
        "runtime_violations": runtime_violations,
        "oom_runs": oom_runs,
        "missing_runs": missing_runs,
        "failures": failures,
        "gate_pass": gate_pass,
        "final_root": str(final_root),
    }


def calibrate(corpus_path: Path) -> dict[str, Any]:
    """Report CLIP ROC-AUC and checkpoint-to-final Spearman on a labelled corpus."""
    corpus = json.loads(corpus_path.read_text())
    labels = corpus.get("labels") or corpus.get("cases") or corpus
    if not isinstance(labels, list):
        raise ValueError("corpus must contain a list under labels or cases")

    y_true: list[int] = []
    y_score: list[float] = []
    checkpoint_scores: list[float] = []
    final_scores: list[float] = []

    for entry in labels:
        keep = entry.get("keep")
        if keep is None:
            keep = entry.get("label") == "keep"
        score = entry.get("clip_pair_score")
        if score is None and entry.get("run_dir"):
            run_dir = Path(entry["run_dir"])
            manifest = json.loads((run_dir / "manifest.json").read_text())
            score = (manifest.get("final") or {}).get("clip_pair_score")
        if score is not None:
            y_true.append(1 if keep else 0)
            y_score.append(float(score))
        ck = entry.get("checkpoint_clip_pair_score")
        final = entry.get("final_clip_pair_score")
        if ck is not None and final is not None:
            checkpoint_scores.append(float(ck))
            final_scores.append(float(final))

    result: dict[str, Any] = {"n_labels": len(y_true)}

    if y_true and len(set(y_true)) > 1:
        result["roc_auc"] = _manual_roc_auc(y_true, y_score)
    else:
        result["roc_auc"] = None

    # Hit-rate: fraction correct predicting keep when clip_pair_score > 0.
    if y_true:
        threshold = 0.0
        hits = sum(
            1
            for score, label in zip(y_score, y_true, strict=True)
            if (1 if score > threshold else 0) == label
        )
        result["hit_rate"] = hits / len(y_true)
        result["hit_rate_threshold"] = threshold
    else:
        result["hit_rate"] = None
        result["hit_rate_threshold"] = 0.0

    if len(checkpoint_scores) >= 2:
        result["checkpoint_final_spearman"] = _rank_correlation(checkpoint_scores, final_scores)
    else:
        result["checkpoint_final_spearman"] = None

    return result


def _manual_roc_auc(y_true: list[int], y_score: list[float]) -> float:
    """AUC via the Mann-Whitney U statistic with average ranks for ties.

    Scores are ranked in ascending order so that assigning higher scores to
    positives yields AUC ~ 1.0 (and anti-correlated scores ~ 0.0). Ties share
    the average rank, so all-equal scores give 0.5.
    """
    n = len(y_true)
    n_pos = sum(y_true)
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5
    order = sorted(range(n), key=lambda index: y_score[index])
    ranks = [0.0] * n
    index = 0
    while index < n:
        end = index
        while end + 1 < n and y_score[order[end + 1]] == y_score[order[index]]:
            end += 1
        avg_rank = (index + end) / 2.0 + 1.0
        for position in range(index, end + 1):
            ranks[order[position]] = avg_rank
        index = end + 1
    rank_sum = sum(ranks[i] for i in range(n) if y_true[i] == 1)
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def _rank_correlation(xs: list[float], ys: list[float]) -> float:
    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda index: values[index])
        out = [0.0] * len(values)
        index = 0
        while index < len(values):
            start = index
            while index + 1 < len(values) and values[order[index + 1]] == values[order[index]]:
                index += 1
            avg_rank = (start + index) / 2.0 + 1.0
            for position in range(start, index + 1):
                out[order[position]] = avg_rank
            index += 1
        return out

    rx = ranks(xs)
    ry = ranks(ys)
    n = len(xs)
    mean_x = sum(rx) / n
    mean_y = sum(ry) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(rx, ry, strict=True))
    den_x = sum((x - mean_x) ** 2 for x in rx) ** 0.5
    den_y = sum((y - mean_y) ** 2 for y in ry) ** 0.5
    if den_x == 0 or den_y == 0:
        return 0.0
    return num / (den_x * den_y)


def _illusion_config_field_names() -> set[str]:
    from worker.illusions import IllusionConfig

    return {field.name for field in fields(IllusionConfig)}


def _build_illusion_config(args: argparse.Namespace):
    """Build the IllusionConfig plus its (subjects, effective prompts).

    The style is applied here exactly once (oil/none keep the exact legacy
    prompts) and the resulting effective prompts are handed to the optimizer
    with ``style=None`` so ``targets_for`` never re-wraps them.
    """
    from worker.illusions import IllusionConfig

    style_requested = None if args.style in (None, "none") else args.style
    if args.pair_id:
        subjects, effective_prompts = resolve_pair_prompts(PAIR_BY_ID[args.pair_id], args.style)
    else:
        subjects = list(args.prompt) if args.prompt else []
        if len(subjects) < 2:
            raise ValueError("need two prompts or --pair-id")
        effective_prompts = styled_prompts(subjects, style_requested)
    if len(effective_prompts) < 2:
        raise ValueError("need two prompts or --pair-id")

    checkpoint_steps: tuple[int, ...] = ()
    if args.checkpoint_step:
        checkpoint_steps = tuple(sorted(set(args.checkpoint_step)))
    elif args.collect_diagnostics:
        checkpoint_steps = INSTRUMENTED_CHECKPOINT_STEPS

    kwargs: dict[str, Any] = {
        "illusion": args.type,
        "prompts": effective_prompts,
        "model_id": local_snapshot(args.model),
        "dream_model_id": (
            None if args.dream_model.lower() == "none" else local_snapshot(args.dream_model)
        ),
        "sds_steps": _or_default(args.sds_steps, 500),
        "sds_guidance": (
            1.0
            if args.sds_objective == "csd"
            else (100.0 if args.sds_guidance is None else args.sds_guidance)
        ),
        "sds_objective": args.sds_objective,
        "dream_rounds": _or_default(args.dream_rounds, 8),
        "dream_steps": _or_default(args.dream_steps, 300),
        "dream_joint": args.dream_joint,
        "sds_lr": args.sds_lr,
        "dream_lr": args.dream_lr,
        "learning_rate": 1e-3,
        "seed": args.seed,
        "device": args.device,
        "use_hifa_schedule": args.sqrt_timestep_anneal,
        "round_robin": args.round_robin,
        "view_batch_size": args.view_batch_size,
        # Style is already baked into effective_prompts; None avoids double-wrap.
        "style": None,
        "checkpoint_steps": checkpoint_steps,
        "enable_vae_slicing": args.enable_vae_slicing,
        "channels_last": args.channels_last,
        "strengths": list(args.dream_strength or []),
        "experimental_recipe": args.experimental_recipe,
    }
    if args.experimental_recipe == "author_reference":
        # Recipe identity stays fixed. Budget knobs yield to explicit flags, so
        # a shorter-SDS or joint-Dream arm is still the same recipe rather than
        # a fork of it.
        kwargs.update(
            {
                "sds_guidance": 60.0,
                "sds_objective": "weighted_sds",
                "sds_lr": 1e-4,
                "dream_lr": 3e-3,
                "use_hifa_schedule": False,
                "round_robin": False,
                "view_batch_size": None,
                "sds_gradient_scale": 0.1,
            }
        )
        kwargs["sds_steps"] = _or_default(args.sds_steps, AUTHOR_REFERENCE_SDS_STEPS)
        if not args.checkpoint_step:
            kwargs["checkpoint_steps"] = reference_checkpoint_ladder(int(kwargs["sds_steps"]))
    if "prime_resolution" in _illusion_config_field_names():
        kwargs["prime_resolution"] = args.prime_resolution
    field_names = _illusion_config_field_names()
    if "collect_diagnostics" in field_names:
        kwargs["collect_diagnostics"] = args.collect_diagnostics
    if "vae_id" in field_names and getattr(args, "vae", None):
        kwargs["vae_id"] = args.vae
    if "model_variant" in field_names and getattr(args, "model_variant", None):
        kwargs["model_variant"] = args.model_variant
    return IllusionConfig(**kwargs), effective_prompts, subjects, style_requested


def run_single_experiment(args: argparse.Namespace) -> int:
    import torch

    from worker.illusions import optimize_illusion, save_image, warn_low_clip_margins

    if args.sds_objective == "csd" and args.sds_guidance is not None:
        raise ValueError("CSD does not accept --sds-guidance")
    requested = Path(args.out)
    out = resolve_run_out(requested)
    if out is None:
        print(f"skip completed run at {requested}")
        return 0
    out.mkdir(parents=True, exist_ok=True)

    config, prompts, subjects, style_requested = _build_illusion_config(args)
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()

    clip_model = clip_processor = None
    clip_revision: str | None = None
    if not args.skip_clip:
        try:
            clip_model, clip_processor, clip_revision = load_clip("cpu")
        except Exception as exc:  # noqa: BLE001 - harness must still run
            print(f"CLIP unavailable ({exc}); continuing without CLIP scores")

    model_ids = {
        "sds_model_id": config.model_id,
        "sds_model_revision": model_revision(config.model_id),
        "dream_model_id": config.dream_model_id or config.model_id,
        "dream_model_revision": model_revision(config.dream_model_id or config.model_id),
        "clip_model_id": CLIP_MODEL_ID,
        "clip_model_revision": clip_revision,
    }

    manifest: dict[str, Any] = {
        "status": "running",
        "pid": os.getpid(),
        "error": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "git_sha": git_sha(),
        "campaign_id": args.campaign_id,
        "spec_hash": args.spec_hash,
        "plan_sha": args.plan_sha,
        "optimizer_fingerprint": args.optimizer_fingerprint,
        "name": args.name,
        "pair_id": args.pair_id,
        "subjects": subjects,
        "effective_prompts": prompts,
        "style_requested": style_requested,
        "hostname": platform.node(),
        "platform": platform.platform(),
        "env": {
            "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL": os.environ.get(
                "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL"
            ),
            "HIP_VISIBLE_DEVICES": os.environ.get("HIP_VISIBLE_DEVICES"),
        },
        "gpu_name": gpu_name(),
        "package_versions": package_versions(),
        "model_ids": model_ids,
        "config": asdict(config) if hasattr(config, "__dataclass_fields__") else {},
        "phase_timings": {},
        "peak_vram_mb": None,
        "checkpoints": {},
    }
    write_manifest_atomic(out / "manifest.json", manifest)

    phase_timings: dict[str, float] = {}
    checkpoint_summaries: dict[str, Any] = {}
    phase_wall: dict[str, float | None] = {"sds_end": None, "dream_begin": None, "dream_end": None}
    t0 = time.perf_counter()

    def _clip_entry(derived) -> dict[str, Any] | None:
        if clip_model is None or len(derived) < 2 or len(prompts) < 2:
            return None
        sims = clip_similarity_matrix(
            derived[:2],
            prompts[:2],
            model=clip_model,
            processor=clip_processor,
            device="cpu",
        )
        margins, score = pair_margins(sims)
        warn_low_clip_margins(margins)
        return {
            "clip_model_id": CLIP_MODEL_ID,
            "clip_model_revision": clip_revision,
            "clip_matrix": sims,
            "clip_margins": margins,
            "clip_pair_score": score,
        }

    def on_phase(event: Any) -> None:
        phase = event.phase
        # Phase boundaries: record wall clocks even when no images are saved.
        if phase in phase_wall:
            phase_wall[phase] = event.wall_s
            return
        # sds_begin carries no images and is not an image checkpoint.
        if phase == "sds_begin" or event.derived is None:
            return

        step = event.step
        ck_dir = out / f"ckpt_{phase}"
        ck_dir.mkdir(parents=True, exist_ok=True)
        for index, prime in enumerate(event.primes or [], start=1):
            save_image(prime, ck_dir / f"prime_{index}.png")
        for index, image in enumerate(event.derived, start=1):
            save_image(image, ck_dir / f"derived_{index}.png")
        for index, target in enumerate(event.targets or [], start=1):
            save_image(target, ck_dir / f"target_{index}.png")

        entry: dict[str, Any] = {"phase": phase, "step": step, "wall_s": event.wall_s}
        if event.round is not None:
            entry["round"] = event.round
        if event.strength is not None:
            entry["strength"] = event.strength
        if event.loss is not None:
            entry["loss"] = event.loss
        if event.loss_start is not None:
            entry["loss_start"] = event.loss_start
        if event.loss_end is not None:
            entry["loss_end"] = event.loss_end
        if event.grad_norm is not None:
            entry["grad_norm"] = event.grad_norm
        clip_scores = _clip_entry(event.derived)
        if clip_scores is not None:
            entry.update(clip_scores)
        diagnostics = event.diagnostics or {}
        if step is not None:
            if diagnostics.get("conflict"):
                entry["conflict"] = [
                    conflict for conflict in diagnostics["conflict"] if conflict.get("step") == step
                ]
            losses = diagnostics.get("losses") or []
            entry["loss_tail"] = [row for row in losses if row.get("step") == step]
            if "grad_norm" not in entry:
                grad_norms = diagnostics.get("grad_norms") or []
                entry["grad_norm"] = next(
                    (row.get("norm") for row in grad_norms if row.get("step") == step),
                    None,
                )
        checkpoint_summaries[phase] = entry
        write_manifest_atomic(ck_dir / "scores.json", entry)

    def progress(fraction: float) -> None:
        print(f"\rprogress {fraction:6.1%}", end="", flush=True)

    try:
        result = optimize_illusion(config, progress=progress, on_phase=on_phase)
        print()
        total_s = time.perf_counter() - t0
        sds_end_wall = phase_wall["sds_end"]
        dream_end_wall = phase_wall["dream_end"]
        sds_s = float(sds_end_wall) if sds_end_wall is not None else total_s
        if dream_end_wall is not None and sds_end_wall is not None:
            dream_s = max(float(dream_end_wall) - float(sds_end_wall), 0.0)
        else:
            dream_s = max(total_s - sds_s, 0.0)
        phase_timings["sds_s"] = sds_s
        phase_timings["dream_s"] = dream_s
        phase_timings["total_s"] = total_s

        for index, prime in enumerate(result.primes, start=1):
            save_image(prime, out / f"prime_{index}.png")
        for index, image in enumerate(result.derived, start=1):
            save_image(image, out / f"derived_{index}.png")

        final_entry: dict[str, Any] = {"wall_s": phase_timings["total_s"]}
        if clip_model is not None and len(result.derived) >= 2 and len(prompts) >= 2:
            sims = clip_similarity_matrix(
                result.derived[:2],
                prompts[:2],
                model=clip_model,
                processor=clip_processor,
                device="cpu",
            )
            margins, score = pair_margins(sims)
            warn_low_clip_margins(margins)
            final_entry.update(
                {
                    "clip_model_id": CLIP_MODEL_ID,
                    "clip_model_revision": clip_revision,
                    "clip_matrix": sims,
                    "clip_margins": margins,
                    "clip_pair_score": score,
                }
            )

        manifest.update(
            {
                "status": "completed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "phase_timings": phase_timings,
                "peak_vram_mb": peak_vram_mb(),
                "checkpoints": checkpoint_summaries,
                "final": final_entry,
                "diagnostics": {
                    "round_robin_exposures": result.diagnostics.get("round_robin_exposures"),
                    "conflict": result.diagnostics.get("conflict"),
                    "loss_tail": (result.diagnostics.get("losses") or [])[-8:],
                },
            }
        )
        write_manifest_atomic(out / "manifest.json", manifest)
        print(f"wrote experiment to {out} (peak_vram_mb={manifest['peak_vram_mb']})")
        return 0
    except Exception as exc:
        manifest.update(
            {
                "status": "failed",
                "error": repr(exc),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "phase_timings": phase_timings,
                "peak_vram_mb": peak_vram_mb(),
                "checkpoints": checkpoint_summaries,
            }
        )
        write_manifest_atomic(out / "manifest.json", manifest)
        raise


def _plan_prefix() -> str:
    return f"scripts/gpu-lock.sh -- env TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1 {_PY_CMD} run"


def print_variance_plan(out_root: Path) -> None:
    pair = PAIR_BY_ID["dog_sloth"]
    root = out_root / "variance_dog_sloth_seed2"
    for repeat in range(3):
        out = root / f"repeat_{repeat}"
        print(
            f"{_plan_prefix()} "
            f"--name variance_r{repeat} --type flip "
            f'--prompt "{pair.prompt_a}" --prompt "{pair.prompt_b}" '
            f"--seed 2 --sds-objective legacy --skip-clip --collect-diagnostics --out {out}"
        )


def print_screen_plan(out_root: Path) -> None:
    root = out_root / "screen"
    profiles = [
        ("01_legacy", "--sds-objective legacy"),
        ("02_weighted_sds", "--sds-objective weighted_sds"),
        ("03_dream_lr_3e-3", "--sds-objective legacy --dream-lr 3e-3"),
        ("04_dream_lr_1e-2", "--sds-objective legacy --dream-lr 1e-2"),
        ("05_dream_sd15", "--sds-objective legacy --dream-model none"),
        ("06_csd", "--sds-objective csd"),
        ("07_nfsd_cfg7.5", "--sds-objective nfsd --sds-guidance 7.5"),
    ]
    for pair in SCREEN_PAIRS:
        pair_id = pair.pair_id
        for name, flags in profiles:
            out = root / name / pair_id
            print(
                f"{_plan_prefix()} "
                f"--name {name}_{pair_id} --type flip "
                f"--pair-id {pair_id} --seed 2 --skip-clip --collect-diagnostics {flags} --out {out}"
            )


def print_final_plan(out_root: Path, finalist_flags: str) -> None:
    root = out_root / "final"
    for pair in FINAL_PAIRS:
        pair_id = pair.pair_id
        for seed_value in (0, 1, 2):
            for tag, flags in (
                ("legacy", "--sds-objective legacy"),
                ("finalist", finalist_flags),
            ):
                out = root / tag / f"{pair_id}_seed{seed_value}"
                print(
                    f"{_plan_prefix()} "
                    f"--name {tag}_{pair_id}_s{seed_value} --type flip "
                    f"--pair-id {pair_id} --seed {seed_value} --skip-clip --collect-diagnostics "
                    f"{flags} --out {out}"
                )


def print_stage2_plan(out_root: Path, profile_flags: str) -> None:
    root = out_root / "stage2"
    pair = SCREEN_PAIRS[0]
    pair_id = pair.pair_id
    experiments = [
        ("sqrt_anneal", "--sqrt-timestep-anneal"),
        ("round_robin", "--round-robin"),
        ("combined_csd_lcm", "--sds-objective csd"),
        ("style_coherent_oil", "--style coherent_oil"),
        ("style_pencil", "--style pencil"),
        ("style_editorial", "--style editorial"),
    ]
    for name, extra in experiments:
        out = root / name / pair_id
        print(
            f"{_plan_prefix()} "
            f"--name stage2_{name}_{pair_id} --type flip "
            f'--prompt "{pair.prompt_a}" --prompt "{pair.prompt_b}" '
            f"--pair-id {pair_id} --seed 2 --skip-clip --collect-diagnostics "
            f"{profile_flags} {extra} --out {out}"
        )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run one instrumented optimize_illusion")
    run.add_argument("--name", default="run")
    run.add_argument("--type", default="flip", choices=["flip", "rotate", "hidden"])
    run.add_argument("--prompt", action="append")
    run.add_argument("--pair-id", choices=sorted(PAIR_BY_ID))
    run.add_argument("--style", default="none")
    run.add_argument("--model", default="stable-diffusion-v1-5/stable-diffusion-v1-5")
    run.add_argument("--dream-model", default="lykon/dreamshaper-8-lcm")
    # None means "the profile default": 500 for legacy, 10,000 for
    # author_reference. An explicit value wins over both.
    run.add_argument("--sds-steps", type=int, default=None)
    run.add_argument("--sds-guidance", type=float, default=None)
    run.add_argument("--sds-objective", default="legacy")
    run.add_argument("--sds-lr", type=float, default=1e-3)
    run.add_argument("--dream-lr", type=float, default=1e-3)
    run.add_argument("--dream-rounds", type=int, default=None)
    run.add_argument("--dream-steps", type=int, default=None)
    run.add_argument(
        "--checkpoint-step",
        type=int,
        action="append",
        default=[],
        help="explicit checkpoint step; repeatable. Overrides the profile ladder.",
    )
    run.add_argument(
        "--prime-resolution",
        type=int,
        default=None,
        help="native prime render size. Default 256 for author_reference. "
        "The prime is the printable artifact, so this bounds print quality.",
    )
    run.add_argument(
        "--dream-strength",
        action="append",
        type=float,
        help="explicit Dream strength; repeat to replace the generated schedule",
    )
    run.add_argument("--dream-joint", action="store_true")
    run.add_argument("--sqrt-timestep-anneal", action="store_true")
    run.add_argument(
        "--hifa-schedule",
        action="store_true",
        dest="sqrt_timestep_anneal",
        help="deprecated alias for --sqrt-timestep-anneal",
    )
    run.add_argument("--round-robin", action="store_true")
    run.add_argument("--view-batch-size", type=int, default=None)
    run.add_argument("--enable-vae-slicing", action="store_true")
    run.add_argument("--channels-last", action="store_true")
    run.add_argument(
        "--vae",
        default=None,
        help="optional VAE id/path (e.g. madebyollin/sdxl-vae-fp16-fix for SDXL)",
    )
    run.add_argument(
        "--model-variant",
        default=None,
        help="diffusers weight variant (e.g. fp16 for SDXL Hub snapshots)",
    )
    run.add_argument("--collect-diagnostics", action="store_true")
    run.add_argument(
        "--experimental-recipe",
        choices=("legacy", "author_reference"),
        default="legacy",
        help="experiment-only frozen optimizer recipe; product default remains legacy",
    )
    run.add_argument("--seed", type=int, default=2)
    run.add_argument("--device", default="cuda")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--skip-clip", action="store_true")
    run.add_argument("--campaign-id")
    run.add_argument("--spec-hash")
    run.add_argument("--plan-sha")
    run.add_argument("--optimizer-fingerprint")

    score_run = sub.add_parser("score-run", help="post-hoc CLIP score one run directory")
    score_run.add_argument("run_dir", type=Path, nargs="?")
    score_run.add_argument("--run", type=Path, dest="run_flag", help="alias for the run directory")
    score_run.add_argument("--device", default="cpu")

    score_tree_cmd = sub.add_parser("score-tree", help="post-hoc CLIP score all runs under a tree")
    score_tree_cmd.add_argument("root", type=Path, nargs="?")
    score_tree_cmd.add_argument(
        "--root", type=Path, dest="root_flag", help="alias for the tree root"
    )
    score_tree_cmd.add_argument("--device", default="cpu")

    variance = sub.add_parser("variance-plan", help="print dog/sloth seed-2 x3 variance commands")
    variance.add_argument("--out-root", type=Path, default=Path("out/illusion-experiments"))

    screen = sub.add_parser("screen-plan", help="print seed-2 screening funnel commands")
    screen.add_argument("--out-root", type=Path, default=Path("out/illusion-experiments"))

    stage2 = sub.add_parser("stage2-plan", help="print stage-2 ablation commands")
    stage2.add_argument("--out-root", type=Path, default=Path("out/illusion-experiments"))
    stage2.add_argument(
        "--profile", required=True, help="flag string for the screened best profile"
    )

    final = sub.add_parser("final-plan", help="print the 24-case final validation plan")
    final.add_argument("--out-root", type=Path, default=Path("out/illusion-experiments"))
    final.add_argument("--finalist", required=True, help="finalist profile flag string")

    matched = sub.add_parser(
        "build-matched-blind", help="build 24 matched legacy/finalist blind cases"
    )
    matched.add_argument("--final-root", type=Path, required=True)
    matched.add_argument("--out", type=Path, required=True)
    matched.add_argument("--seed", type=int, default=0)

    evaluate = sub.add_parser("evaluate-ratings", help="evaluate frozen ratings against the gate")
    evaluate.add_argument("--ratings", type=Path, required=True)
    evaluate.add_argument("--answer-key", type=Path, required=True)
    evaluate.add_argument("--final-root", type=Path, required=True)

    calibrate_cmd = sub.add_parser("calibrate", help="CLIP calibration report on a labelled corpus")
    calibrate_cmd.add_argument("corpus", type=Path, nargs="?")
    calibrate_cmd.add_argument(
        "--corpus", type=Path, dest="corpus_flag", help="alias for the labelled corpus path"
    )

    blind = sub.add_parser("blind-sheet", help="legacy simple blind sheet from a cases JSON")
    blind.add_argument("--cases", type=Path, required=True)
    blind.add_argument("--out", type=Path, required=True)
    blind.add_argument("--seed", type=int, default=0)

    stage_blind = sub.add_parser(
        "build-stage-blind",
        help="build SDS-end, Dream-d1, and final blind sheets from a campaign tree",
    )
    stage_blind.add_argument("--root", type=Path, required=True)
    stage_blind.add_argument("--out", type=Path, required=True)
    stage_blind.add_argument("--seed", type=int, default=0)
    stage_blind.add_argument(
        "--per-page",
        type=int,
        default=BLIND_CELLS_PER_PAGE,
        help="cells per page; 0 for one sheet per stage. At 180 runs a single "
        "sheet is a 1024x23040 strip nobody can rate.",
    )

    yield_cmd = sub.add_parser(
        "build-yield",
        help="one sheet per pair, all seeds together, for reading yield by pair",
    )
    yield_cmd.add_argument("--root", type=Path, required=True)
    yield_cmd.add_argument("--out", type=Path, required=True)
    yield_cmd.add_argument(
        "--stage",
        default="final",
        choices=YIELD_STAGES,
        help="which stage to show. dream_d1 held more detail than final on both "
        "pre-window pairs inspected, so finals alone can understate yield.",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.cmd == "run":
        if not args.prompt and not args.pair_id:
            parser.error("run requires --prompt twice or --pair-id")
        raise SystemExit(run_single_experiment(args))

    if args.cmd == "score-run":
        run_dir = args.run_flag or args.run_dir
        if run_dir is None:
            parser.error("score-run requires a run directory (positional or --run)")
        scored = score_run_dir(run_dir, device=args.device)
        print(json.dumps(scored, indent=2))
        return

    if args.cmd == "score-tree":
        root = args.root_flag or args.root
        if root is None:
            parser.error("score-tree requires a tree root (positional or --root)")
        dirs = score_tree(root, device=args.device)
        print(f"scored {len(dirs)} runs under {root}")
        return

    if args.cmd == "build-stage-blind":
        print(
            json.dumps(
                build_stage_blind_sheets(args.root, args.out, args.seed, args.per_page),
                indent=2,
            )
        )
        return

    if args.cmd == "build-yield":
        print(json.dumps(build_yield_sheets(args.root, args.out, args.stage), indent=2))
        return

    if args.cmd == "blind-sheet":
        cases_raw = json.loads(args.cases.read_text())
        from PIL import Image

        cases = [{**item, "image": Image.open(item["path"])} for item in cases_raw]
        path = build_blind_sheet(cases, args.out, seed=args.seed)
        print(f"wrote {path}")
        return

    if args.cmd == "build-matched-blind":
        path = build_matched_blind(args.final_root, args.out, seed=args.seed)
        print(f"wrote {path}")
        return

    if args.cmd == "evaluate-ratings":
        report = evaluate_ratings(args.ratings, args.answer_key, args.final_root)
        print(json.dumps(report, indent=2))
        if not report["gate_pass"]:
            raise SystemExit(1)
        return

    if args.cmd == "calibrate":
        corpus = args.corpus_flag or args.corpus
        if corpus is None:
            parser.error("calibrate requires a corpus path (positional or --corpus)")
        report = calibrate(corpus)
        print(json.dumps(report, indent=2))
        return

    if args.cmd == "variance-plan":
        print_variance_plan(args.out_root)
        return

    if args.cmd == "screen-plan":
        print_screen_plan(args.out_root)
        return

    if args.cmd == "stage2-plan":
        print_stage2_plan(args.out_root, args.profile)
        return

    if args.cmd == "final-plan":
        print_final_plan(args.out_root, args.finalist)
        return


if __name__ == "__main__":
    main(sys.argv[1:])
