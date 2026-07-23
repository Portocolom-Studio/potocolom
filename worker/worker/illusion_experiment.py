"""Illusion reliability experiment harness (PR #118).

Records manifests, phase-qualified SDS checkpoints, CLIP 2x2 margins, blind
contact sheets, and acceptance-gate evaluation. Not imported by the online
worker path.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import subprocess
import sys
import time
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

CLIP_MODEL_ID = "openai/clip-vit-large-patch14"

FINAL_PAIRS: list[tuple[str, str, str]] = [
    (
        "dog_sloth",
        "an oil painting of a dog sitting in a misty forest",
        "an oil painting of a sloth hanging from a branch",
    ),
    (
        "elephant_swan",
        "an oil painting of an elephant",
        "an oil painting of a swan on a lake",
    ),
    (
        "moose_butterfly",
        "an oil painting of a moose by a lake",
        "an oil painting of a monarch butterfly",
    ),
    (
        "fox_rabbit",
        "an oil painting of a red fox portrait",
        "an oil painting of a rabbit in a meadow",
    ),
    (
        "squirrel_pelican",
        "an oil painting of a red squirrel",
        "an oil painting of a pelican in flight",
    ),
    (
        "gorilla_starfish",
        "an oil painting of a gorilla portrait",
        "an oil painting of a starfish on sand",
    ),
    (
        "walrus_ladybug",
        "an oil painting of a walrus on ice",
        "an oil painting of a ladybug on a leaf",
    ),
    (
        "mountain_valley",
        "an oil painting of a snowy mountain peak at dawn",
        "an oil painting of a pine valley reflected in an alpine lake",
    ),
]

_SCREEN_IDS = frozenset({"dog_sloth", "fox_rabbit", "walrus_ladybug", "mountain_valley"})
SCREEN_PAIRS: list[tuple[str, str, str]] = [p for p in FINAL_PAIRS if p[0] in _SCREEN_IDS]

PAIR_BY_ID: dict[str, tuple[str, str, str]] = {p[0]: p for p in FINAL_PAIRS}

INSTRUMENTED_CHECKPOINT_STEPS = (60, 125, 250, 500)

CONTROL_PAIR_IDS = frozenset({"elephant_swan", "moose_butterfly"})

GATE_MIN_KEEPERS = 16
GATE_MIN_PAIRS_TWO_THIRDS = 6
GATE_MIN_NET_CONVERSIONS = 4
GATE_MAX_RUNTIME_S = 3600.0

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
    return (run_dir / "derived_1.png").is_file() and (run_dir / "derived_2.png").is_file()


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


def load_clip(device: str = "cpu") -> tuple[Any, Any, str | None]:
    from transformers import CLIPModel, CLIPProcessor

    revision = model_revision(CLIP_MODEL_ID)
    kwargs: dict[str, Any] = {}
    if revision:
        kwargs["revision"] = revision
    model = cast(Any, CLIPModel.from_pretrained(CLIP_MODEL_ID, **kwargs))
    model = model.to(device).eval()
    processor = CLIPProcessor.from_pretrained(CLIP_MODEL_ID, **kwargs)
    return model, processor, revision


@torch_no_grad_safe
def clip_similarity_matrix(
    images: list[Any],
    prompts: list[str],
    *,
    model: Any = None,
    processor: Any = None,
    device: str = "cpu",
) -> list[list[float]]:
    """Return matrix[view_i][prompt_j] CLIP cosine similarities."""
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

    inputs = processor(text=prompts, images=pil_images, return_tensors="pt", padding=True)
    inputs = {key: value.to(device) for key, value in inputs.items()}
    outputs = model(**inputs)
    image_embeds = outputs.image_embeds / outputs.image_embeds.norm(dim=-1, keepdim=True)
    text_embeds = outputs.text_embeds / outputs.text_embeds.norm(dim=-1, keepdim=True)
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


def score_images_for_prompts(
    derived_paths: list[Path],
    prompts: list[str],
    *,
    style: str | None,
    clip_model: Any = None,
    clip_processor: Any = None,
    device: str = "cpu",
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
    )
    margins, score = pair_margins(sims)
    return {
        "clip_model_id": CLIP_MODEL_ID,
        "clip_model_revision": model_revision(CLIP_MODEL_ID),
        "clip_matrix": sims,
        "clip_margins": margins,
        "clip_pair_score": score,
    }


def score_run_dir(
    run_dir: Path, *, device: str = "cpu", update_manifest: bool = True
) -> dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"missing manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    prompts = manifest.get("config", {}).get("prompts") or manifest.get("prompts")
    if not prompts or len(prompts) < 2:
        raise ValueError(f"run {run_dir} has no prompts in manifest")
    style = manifest.get("config", {}).get("style")

    clip_model, clip_processor, revision = load_clip(device)
    scored: dict[str, Any] = {"clip_model_id": CLIP_MODEL_ID, "clip_model_revision": revision}

    for ckpt_dir in sorted(run_dir.glob("ckpt_*")):
        derived = [ckpt_dir / "derived_1.png", ckpt_dir / "derived_2.png"]
        if not all(path.is_file() for path in derived):
            continue
        entry = score_images_for_prompts(
            derived,
            list(prompts),
            style=style,
            clip_model=clip_model,
            clip_processor=clip_processor,
            device=device,
        )
        (ckpt_dir / "scores.json").write_text(json.dumps(entry, indent=2) + "\n")
        scored[ckpt_dir.name] = entry

    final_derived = [run_dir / "derived_1.png", run_dir / "derived_2.png"]
    if all(path.is_file() for path in final_derived):
        final_entry = score_images_for_prompts(
            final_derived,
            list(prompts),
            style=style,
            clip_model=clip_model,
            clip_processor=clip_processor,
            device=device,
        )
        scored["final"] = final_entry
        manifest["final"] = {**(manifest.get("final") or {}), **final_entry}

    manifest["clip_scored_at"] = datetime.now(timezone.utc).isoformat()
    manifest["checkpoints"] = {
        **(manifest.get("checkpoints") or {}),
        **{name: entry for name, entry in scored.items() if name.startswith("ckpt_")},
    }
    if update_manifest:
        write_manifest_atomic(manifest_path, manifest)
    return scored


def score_tree(root: Path, *, device: str = "cpu") -> list[Path]:
    scored_dirs: list[Path] = []
    for manifest_path in sorted(root.rglob("manifest.json")):
        run_dir = manifest_path.parent
        if not (run_dir / "derived_1.png").is_file():
            continue
        score_run_dir(run_dir, device=device, update_manifest=True)
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
    for pair_id, _prompt_a, _prompt_b in FINAL_PAIRS:
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


def evaluate_ratings(
    ratings_path: Path,
    answer_key_path: Path,
    final_root: Path,
) -> dict[str, Any]:
    ratings = {row["case_id"]: row for row in _load_jsonl(ratings_path)}
    answer = json.loads(answer_key_path.read_text())
    cases = answer["cases"]

    finalist_keepers = 0
    pair_finalist_kept: dict[str, list[bool]] = {pair_id: [] for pair_id, _, _ in FINAL_PAIRS}
    conversions = 0
    regressions = 0
    runtime_violations: list[str] = []
    oom_runs: list[str] = []

    for case in cases:
        case_id = case["case_id"]
        rating = ratings.get(case_id)
        if rating is None:
            continue
        keep_a = rating.get("keep_a")
        keep_b = rating.get("keep_b")
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
                continue
            manifest = json.loads(manifest_path.read_text())
            total_s = (manifest.get("phase_timings") or {}).get("total_s")
            if total_s is not None and float(total_s) > GATE_MAX_RUNTIME_S:
                runtime_violations.append(f"{tag}:{case_id}:{total_s:.0f}s")
            error = manifest.get("error") or ""
            status = manifest.get("status")
            if status == "failed" or "oom" in str(error).lower():
                oom_runs.append(f"{tag}:{case_id}")

    pairs_two_thirds = sum(
        1 for pair_id, _, _ in FINAL_PAIRS if sum(pair_finalist_kept[pair_id]) >= 2
    )
    control_results = {
        pair_id: sum(pair_finalist_kept[pair_id]) for pair_id in sorted(CONTROL_PAIR_IDS)
    }
    controls_pass = all(count >= 2 for count in control_results.values())
    net_conversion_ok = conversions - regressions >= GATE_MIN_NET_CONVERSIONS
    runtime_ok = not runtime_violations and not oom_runs
    gate_pass = (
        finalist_keepers >= GATE_MIN_KEEPERS
        and pairs_two_thirds >= GATE_MIN_PAIRS_TWO_THIRDS
        and controls_pass
        and net_conversion_ok
        and runtime_ok
    )

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

    if len(checkpoint_scores) >= 2:
        result["checkpoint_final_spearman"] = _rank_correlation(checkpoint_scores, final_scores)
    else:
        result["checkpoint_final_spearman"] = None

    return result


def _manual_roc_auc(y_true: list[int], y_score: list[float]) -> float:
    pairs = sorted(zip(y_score, y_true, strict=True), reverse=True)
    n_pos = sum(y_true)
    n_neg = len(y_true) - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5
    rank_sum = 0.0
    for rank, (_score, label) in enumerate(pairs, start=1):
        if label == 1:
            rank_sum += rank
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
    from worker.illusions import IllusionConfig

    prompts = list(args.prompt) if args.prompt else []
    if args.pair_id:
        _pair_id, prompt_a, prompt_b = PAIR_BY_ID[args.pair_id]
        prompts = [prompt_a, prompt_b]
    if len(prompts) < 2:
        raise ValueError("need two prompts or --pair-id")

    checkpoint_steps: tuple[int, ...] = ()
    if args.collect_diagnostics:
        checkpoint_steps = INSTRUMENTED_CHECKPOINT_STEPS

    kwargs: dict[str, Any] = {
        "illusion": args.type,
        "prompts": prompts,
        "model_id": args.model,
        "dream_model_id": None if args.dream_model.lower() == "none" else args.dream_model,
        "sds_steps": args.sds_steps,
        "sds_guidance": args.sds_guidance,
        "sds_objective": args.sds_objective,
        "dream_rounds": args.dream_rounds,
        "dream_steps": args.dream_steps,
        "dream_joint": args.dream_joint,
        "sds_lr": args.sds_lr,
        "dream_lr": args.dream_lr,
        "learning_rate": 1e-3,
        "seed": args.seed,
        "device": args.device,
        "use_hifa_schedule": args.hifa_schedule,
        "round_robin": args.round_robin,
        "view_batch_size": args.view_batch_size,
        "style": None if args.style == "none" else args.style,
        "checkpoint_steps": checkpoint_steps,
        "enable_vae_slicing": args.enable_vae_slicing,
        "channels_last": args.channels_last,
    }
    field_names = _illusion_config_field_names()
    if "collect_diagnostics" in field_names:
        kwargs["collect_diagnostics"] = args.collect_diagnostics
    return IllusionConfig(**kwargs), prompts


def run_single_experiment(args: argparse.Namespace) -> int:
    import torch

    from worker.illusions import optimize_illusion, save_image, warn_low_clip_margins

    requested = Path(args.out)
    out = resolve_run_out(requested)
    if out is None:
        print(f"skip completed run at {requested}")
        return 0
    out.mkdir(parents=True, exist_ok=True)

    config, prompts = _build_illusion_config(args)
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
        "name": args.name,
        "pair_id": args.pair_id,
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
    saved_sds_steps: set[int] = set()
    t0 = time.perf_counter()
    sds_wall_end = t0

    def on_checkpoint(phase: str, primes, derived, diagnostics: dict) -> None:
        # optimize_illusion already emits phase-qualified names (sds_0060..final).
        step: int | None = None
        if phase.startswith("sds_"):
            try:
                step = int(phase.split("_", 1)[1])
                saved_sds_steps.add(step)
            except ValueError:
                step = None
        ck_dir = out / f"ckpt_{phase}"
        ck_dir.mkdir(parents=True, exist_ok=True)
        for index, prime in enumerate(primes, start=1):
            save_image(prime, ck_dir / f"prime_{index}.png")
        for index, image in enumerate(derived, start=1):
            save_image(image, ck_dir / f"derived_{index}.png")
        entry: dict[str, Any] = {
            "phase": phase,
            "step": step,
            "wall_s": time.perf_counter() - t0,
        }
        if clip_model is not None and len(derived) >= 2 and len(prompts) >= 2:
            sims = clip_similarity_matrix(
                derived[:2],
                styled_prompts(prompts[:2], config.style),
                model=clip_model,
                processor=clip_processor,
                device="cpu",
            )
            margins, score = pair_margins(sims)
            warn_low_clip_margins(margins)
            entry.update(
                {
                    "clip_model_id": CLIP_MODEL_ID,
                    "clip_model_revision": clip_revision,
                    "clip_matrix": sims,
                    "clip_margins": margins,
                    "clip_pair_score": score,
                }
            )
        if diagnostics.get("conflict") and step is not None:
            entry["conflict"] = [
                conflict for conflict in diagnostics["conflict"] if conflict.get("step") == step
            ]
        losses = diagnostics.get("losses") or []
        if step is not None:
            entry["loss_tail"] = [row for row in losses if row.get("step") == step]
            grad_norms = diagnostics.get("grad_norms") or []
            entry["grad_norm"] = next(
                (row.get("norm") for row in grad_norms if row.get("step") == step),
                None,
            )
        checkpoint_summaries[phase] = entry
        write_manifest_atomic(ck_dir / "scores.json", entry)
        nonlocal sds_wall_end
        if phase.startswith("sds_"):
            sds_wall_end = time.perf_counter()

    def progress(fraction: float) -> None:
        print(f"\rprogress {fraction:6.1%}", end="", flush=True)

    try:
        result = optimize_illusion(config, progress=progress, on_checkpoint=on_checkpoint)
        print()
        total_s = time.perf_counter() - t0
        phase_timings["sds_s"] = sds_wall_end - t0
        phase_timings["dream_s"] = total_s - phase_timings["sds_s"]
        phase_timings["total_s"] = total_s

        for index, prime in enumerate(result.primes, start=1):
            save_image(prime, out / f"prime_{index}.png")
        for index, image in enumerate(result.derived, start=1):
            save_image(image, out / f"derived_{index}.png")

        final_entry: dict[str, Any] = {"wall_s": phase_timings["total_s"]}
        if clip_model is not None and len(result.derived) >= 2 and len(prompts) >= 2:
            sims = clip_similarity_matrix(
                result.derived[:2],
                styled_prompts(prompts[:2], config.style),
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
    pair_id, prompt_a, prompt_b = PAIR_BY_ID["dog_sloth"]
    root = out_root / "variance_dog_sloth_seed2"
    for repeat in range(3):
        out = root / f"repeat_{repeat}"
        print(
            f"{_plan_prefix()} "
            f"--name variance_r{repeat} --type flip "
            f'--prompt "{prompt_a}" --prompt "{prompt_b}" '
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
        ("06_csd_cfg7.5", "--sds-objective csd --sds-guidance 7.5"),
        ("07_csd_cfg20", "--sds-objective csd --sds-guidance 20"),
        ("08_nfsd_cfg7.5", "--sds-objective nfsd --sds-guidance 7.5"),
    ]
    for pair_id, _prompt_a, _prompt_b in SCREEN_PAIRS:
        for name, flags in profiles:
            out = root / name / pair_id
            print(
                f"{_plan_prefix()} "
                f"--name {name}_{pair_id} --type flip "
                f"--pair-id {pair_id} --seed 2 --skip-clip --collect-diagnostics {flags} --out {out}"
            )


def print_final_plan(out_root: Path, finalist_flags: str) -> None:
    root = out_root / "final"
    for pair_id, _prompt_a, _prompt_b in FINAL_PAIRS:
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
    pair_id, prompt_a, prompt_b = SCREEN_PAIRS[0]
    experiments = [
        ("hifa", "--hifa-schedule"),
        ("round_robin", "--round-robin"),
        ("combined_csd_lcm", "--sds-objective csd --sds-guidance 7.5"),
        ("style_oil", "--style oil"),
        ("style_pencil", "--style pencil"),
        ("style_editorial", "--style editorial"),
    ]
    for name, extra in experiments:
        out = root / name / pair_id
        print(
            f"{_plan_prefix()} "
            f"--name stage2_{name}_{pair_id} --type flip "
            f'--prompt "{prompt_a}" --prompt "{prompt_b}" '
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
    run.add_argument("--sds-steps", type=int, default=500)
    run.add_argument("--sds-guidance", type=float, default=100.0)
    run.add_argument("--sds-objective", default="legacy")
    run.add_argument("--sds-lr", type=float, default=1e-3)
    run.add_argument("--dream-lr", type=float, default=1e-3)
    run.add_argument("--dream-rounds", type=int, default=8)
    run.add_argument("--dream-steps", type=int, default=300)
    run.add_argument("--dream-joint", action="store_true")
    run.add_argument("--hifa-schedule", action="store_true")
    run.add_argument("--round-robin", action="store_true")
    run.add_argument("--view-batch-size", type=int, default=None)
    run.add_argument("--enable-vae-slicing", action="store_true")
    run.add_argument("--channels-last", action="store_true")
    run.add_argument("--collect-diagnostics", action="store_true")
    run.add_argument("--seed", type=int, default=2)
    run.add_argument("--device", default="cuda")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--skip-clip", action="store_true")

    score_run = sub.add_parser("score-run", help="post-hoc CLIP score one run directory")
    score_run.add_argument("run_dir", type=Path)
    score_run.add_argument("--device", default="cpu")

    score_tree_cmd = sub.add_parser("score-tree", help="post-hoc CLIP score all runs under a tree")
    score_tree_cmd.add_argument("root", type=Path)
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
    calibrate_cmd.add_argument("corpus", type=Path)

    blind = sub.add_parser("blind-sheet", help="legacy simple blind sheet from a cases JSON")
    blind.add_argument("--cases", type=Path, required=True)
    blind.add_argument("--out", type=Path, required=True)
    blind.add_argument("--seed", type=int, default=0)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.cmd == "run":
        if not args.prompt and not args.pair_id:
            parser.error("run requires --prompt twice or --pair-id")
        raise SystemExit(run_single_experiment(args))

    if args.cmd == "score-run":
        scored = score_run_dir(args.run_dir, device=args.device)
        print(json.dumps(scored, indent=2))
        return

    if args.cmd == "score-tree":
        dirs = score_tree(args.root, device=args.device)
        print(f"scored {len(dirs)} runs under {args.root}")
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
        report = calibrate(args.corpus)
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
