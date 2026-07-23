"""Illusion reliability experiment harness (PR #118).

Records manifests, SDS checkpoints, CLIP 2x2 margins, and blind contact
sheets. Not imported by the online worker path.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


# ---------------------------------------------------------------------------
# Pairs used by the screening funnel and final validation matrix
# ---------------------------------------------------------------------------

SCREEN_PAIRS: list[tuple[str, str, str]] = [
    ("dog_sloth", "a dog", "a sloth hanging from a branch"),
    ("fox_rabbit", "a fox", "a rabbit"),
    ("walrus_ladybug", "a walrus", "a ladybug"),
    ("mountain_valley", "a mountain landscape", "a valley landscape"),
]

FINAL_PAIRS: list[tuple[str, str, str]] = [
    ("dog_sloth", "a dog", "a sloth hanging from a branch"),
    ("elephant_swan", "an elephant", "a swan"),
    ("moose_butterfly", "a moose", "a butterfly"),
    ("fox_rabbit", "a fox", "a rabbit"),
    ("squirrel_pelican", "a squirrel", "a pelican"),
    ("gorilla_starfish", "a gorilla", "a starfish"),
    ("walrus_ladybug", "a walrus", "a ladybug"),
    ("mountain_valley", "a mountain landscape", "a valley landscape"),
]

CHECKPOINT_STEPS = (60, 125, 250, 500)


def git_sha(repo: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


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
        # Best-effort parse; may vary by rocm-smi version
        for line in out.splitlines():
            if "Used" in line and "MB" in line:
                parts = line.replace(",", " ").split()
                for i, part in enumerate(parts):
                    if part == "MB" and i > 0:
                        try:
                            return float(parts[i - 1])
                        except ValueError:
                            continue
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return None


def load_clip(device: str = "cpu"):
    """Lazy CLIP loader for the harness only (optional transformers)."""
    import torch
    from transformers import CLIPModel, CLIPProcessor

    model_id = "openai/clip-vit-base-patch32"
    model = CLIPModel.from_pretrained(model_id).to(device).eval()
    processor = CLIPProcessor.from_pretrained(model_id)
    return model, processor


@torch_no_grad_safe
def clip_similarity_matrix(
    images: list[Any],
    prompts: list[str],
    *,
    model=None,
    processor=None,
    device: str = "cpu",
) -> list[list[float]]:
    """Return matrix[view_i][prompt_j] CLIP cosine similarities."""
    import torch
    from PIL import Image

    if model is None or processor is None:
        model, processor = load_clip(device)

    pil_images = []
    for image in images:
        if hasattr(image, "squeeze"):
            array = (image.squeeze(0).permute(1, 2, 0).detach().cpu().numpy() * 255).clip(0, 255)
            import numpy as np

            pil_images.append(Image.fromarray(array.astype("uint8")))
        else:
            pil_images.append(image)

    inputs = processor(text=prompts, images=pil_images, return_tensors="pt", padding=True)
    inputs = {k: v.to(device) for k, v in inputs.items()}
    outputs = model(**inputs)
    image_embeds = outputs.image_embeds / outputs.image_embeds.norm(dim=-1, keepdim=True)
    text_embeds = outputs.text_embeds / outputs.text_embeds.norm(dim=-1, keepdim=True)
    sims = (image_embeds @ text_embeds.T).detach().cpu().tolist()
    return sims


def pair_margins(sim_matrix: list[list[float]]) -> tuple[list[float], float]:
    """margin_i = sim(i,i) - sim(i, other); pair score = min(margins)."""
    n = len(sim_matrix)
    margins = []
    for i in range(n):
        correct = sim_matrix[i][i]
        others = [sim_matrix[i][j] for j in range(n) if j != i]
        margins.append(correct - max(others) if others else correct)
    return margins, min(margins) if margins else 0.0


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n")


def make_contact_sheet(
    cells: list[tuple[Any, str]],
    out_path: Path,
    *,
    cols: int = 4,
    cell_size: int = 256,
) -> None:
    """Build a contact sheet. Cell labels must NOT include config names for blinds."""
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
        if hasattr(image, "squeeze"):
            import numpy as np

            array = (image.squeeze(0).permute(1, 2, 0).detach().cpu().numpy() * 255).clip(0, 255)
            pil = Image.fromarray(array.astype("uint8")).resize((cell_size, cell_size))
        else:
            pil = image.convert("RGB").resize((cell_size, cell_size))
        r, c = divmod(index, cols)
        sheet.paste(pil, (c * cell_size, r * cell_size))
        draw.text((c * cell_size + 4, r * cell_size + 4), label, fill=(255, 255, 0), font=font)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)


def build_blind_sheet(
    cases: list[dict[str, Any]],
    out_dir: Path,
    *,
    seed: int = 0,
) -> Path:
    """Write a randomized blind contact sheet and a separate answer key.

    Configuration names are only written to answer_key.json, never onto the sheet.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    order = list(range(len(cases)))
    rng.shuffle(order)
    cells = []
    key = []
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
    # Write answer key after the sheet so a crash mid-sheet never leaks names alone.
    write_manifest(out_dir / "answer_key.json", {"seed": seed, "cases": key})
    write_manifest(
        out_dir / "RATING_INSTRUCTIONS.md",
        {
            "instructions": (
                "Rate each case-NN cell keep/reject using the existing rubric: "
                "both subjects immediately readable, anatomically plausible, "
                "no obvious cross-subject feature borrowing, aesthetically coherent. "
                "Do not open answer_key.json until ratings are frozen."
            )
        },
    )
    return sheet_path


def run_single_experiment(args: argparse.Namespace) -> int:
    """Run one optimize_illusion with full instrumentation."""
    import torch

    from worker.illusions import IllusionConfig, optimize_illusion, save_image, warn_low_clip_margins

    repo = Path(__file__).resolve().parents[2]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()

    prompts = list(args.prompt)
    config = IllusionConfig(
        illusion=args.type,
        prompts=prompts,
        model_id=args.model,
        dream_model_id=None if args.dream_model.lower() == "none" else args.dream_model,
        sds_steps=args.sds_steps,
        sds_guidance=args.sds_guidance,
        sds_objective=args.sds_objective,
        dream_rounds=args.dream_rounds,
        dream_steps=args.dream_steps,
        dream_joint=args.dream_joint,
        sds_lr=args.sds_lr,
        dream_lr=args.dream_lr,
        learning_rate=args.sds_lr,
        seed=args.seed,
        device=args.device,
        use_hifa_schedule=args.hifa_schedule,
        round_robin=args.round_robin,
        view_batch_size=args.view_batch_size,
        style=None if args.style == "none" else args.style,
        checkpoint_steps=CHECKPOINT_STEPS,
    )

    clip_model = clip_processor = None
    if not args.skip_clip:
        try:
            clip_model, clip_processor = load_clip("cpu")
        except Exception as exc:  # noqa: BLE001 - harness must still run
            print(f"CLIP unavailable ({exc}); continuing without CLIP scores")

    phase_timings: dict[str, float] = {}
    checkpoint_scores: dict[str, Any] = {}
    t0 = time.perf_counter()

    def on_checkpoint(step: int, primes, derived, diagnostics: dict) -> None:
        ck_dir = out / f"ckpt_{step:04d}"
        ck_dir.mkdir(parents=True, exist_ok=True)
        for i, prime in enumerate(primes, start=1):
            save_image(prime, ck_dir / f"prime_{i}.png")
        for i, image in enumerate(derived, start=1):
            save_image(image, ck_dir / f"derived_{i}.png")
        entry: dict[str, Any] = {"step": step, "wall_s": time.perf_counter() - t0}
        if clip_model is not None and len(derived) >= 2 and len(prompts) >= 2:
            styled = list(prompts)
            if config.style:
                from worker.illusions import apply_style_template

                styled = [apply_style_template(p, config.style) for p in prompts]
            sims = clip_similarity_matrix(
                derived[:2],
                styled[:2],
                model=clip_model,
                processor=clip_processor,
                device="cpu",
            )
            margins, score = pair_margins(sims)
            warn_low_clip_margins(margins)
            entry["clip_matrix"] = sims
            entry["clip_margins"] = margins
            entry["clip_pair_score"] = score
        if diagnostics.get("conflict"):
            entry["conflict"] = [c for c in diagnostics["conflict"] if c.get("step") == step]
        checkpoint_scores[str(step)] = entry
        write_manifest(ck_dir / "scores.json", entry)

    manifest = {
        "git_sha": git_sha(repo),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "hostname": platform.node(),
        "platform": platform.platform(),
        "env": {
            "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL": os.environ.get(
                "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL"
            ),
            "HIP_VISIBLE_DEVICES": os.environ.get("HIP_VISIBLE_DEVICES"),
        },
        "config": {
            "illusion": config.illusion,
            "prompts": config.prompts,
            "style": config.style,
            "model_id": config.model_id,
            "dream_model_id": config.dream_model_id,
            "sds_steps": config.sds_steps,
            "sds_guidance": config.sds_guidance,
            "sds_objective": config.sds_objective,
            "sds_lr": config.sds_lr,
            "dream_lr": config.dream_lr,
            "dream_rounds": config.dream_rounds,
            "dream_steps": config.dream_steps,
            "dream_joint": config.dream_joint,
            "seed": config.seed,
            "use_hifa_schedule": config.use_hifa_schedule,
            "round_robin": config.round_robin,
            "view_batch_size": config.view_batch_size,
        },
        "name": args.name,
    }
    write_manifest(out / "manifest.json", manifest)

    sds_start = time.perf_counter()

    def progress(fraction: float) -> None:
        print(f"\rprogress {fraction:6.1%}", end="", flush=True)

    result = optimize_illusion(config, progress=progress, on_checkpoint=on_checkpoint)
    print()
    phase_timings["total_s"] = time.perf_counter() - t0
    phase_timings["optimize_s"] = time.perf_counter() - sds_start

    for i, prime in enumerate(result.primes, start=1):
        save_image(prime, out / f"prime_{i}.png")
    for i, image in enumerate(result.derived, start=1):
        save_image(image, out / f"derived_{i}.png")

    final_entry: dict[str, Any] = {"wall_s": phase_timings["total_s"]}
    if clip_model is not None and len(result.derived) >= 2 and len(prompts) >= 2:
        styled = list(prompts)
        if config.style:
            from worker.illusions import apply_style_template

            styled = [apply_style_template(p, config.style) for p in prompts]
        sims = clip_similarity_matrix(
            result.derived[:2],
            styled[:2],
            model=clip_model,
            processor=clip_processor,
            device="cpu",
        )
        margins, score = pair_margins(sims)
        warn_low_clip_margins(margins)
        final_entry["clip_matrix"] = sims
        final_entry["clip_margins"] = margins
        final_entry["clip_pair_score"] = score

    manifest.update(
        {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "phase_timings": phase_timings,
            "peak_vram_mb": peak_vram_mb(),
            "checkpoints": checkpoint_scores,
            "final": final_entry,
            "diagnostics": {
                "round_robin_exposures": result.diagnostics.get("round_robin_exposures"),
                "conflict": result.diagnostics.get("conflict"),
                "loss_tail": result.diagnostics.get("losses", [])[-8:],
            },
        }
    )
    write_manifest(out / "manifest.json", manifest)
    print(f"wrote experiment to {out} (peak_vram_mb={manifest['peak_vram_mb']})")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run one instrumented optimize_illusion")
    run.add_argument("--name", default="run")
    run.add_argument("--type", default="flip", choices=["flip", "rotate", "hidden"])
    run.add_argument("--prompt", action="append", required=True)
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
    run.add_argument("--seed", type=int, default=2)
    run.add_argument("--device", default="cuda")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--skip-clip", action="store_true")

    blind = sub.add_parser("blind-sheet", help="build a blind contact sheet from a cases JSON")
    blind.add_argument("--cases", type=Path, required=True, help="JSON list of {path,config_name,...}")
    blind.add_argument("--out", type=Path, required=True)
    blind.add_argument("--seed", type=int, default=0)

    variance = sub.add_parser(
        "variance-plan",
        help="print the dog/sloth seed-2 x3 same-seed ROCm variance commands",
    )
    variance.add_argument("--out-root", type=Path, default=Path("out/illusion-experiments"))

    screen = sub.add_parser("screen-plan", help="print the seed-2 screening funnel commands")
    screen.add_argument("--out-root", type=Path, default=Path("out/illusion-experiments"))

    final = sub.add_parser("final-plan", help="print the 24-case final validation plan")
    final.add_argument("--out-root", type=Path, default=Path("out/illusion-experiments"))
    final.add_argument("--finalist", required=True, help="finalist profile name / flag set")

    args = parser.parse_args()
    if args.cmd == "run":
        raise SystemExit(run_single_experiment(args))
    if args.cmd == "blind-sheet":
        cases_raw = json.loads(args.cases.read_text())
        from PIL import Image

        cases = []
        for item in cases_raw:
            cases.append({**item, "image": Image.open(item["path"])})
        path = build_blind_sheet(cases, args.out, seed=args.seed)
        print(f"wrote {path}")
        return
    if args.cmd == "variance-plan":
        root = args.out_root / "variance_dog_sloth_seed2"
        for repeat in range(3):
            out = root / f"repeat_{repeat}"
            print(
                "scripts/gpu-lock.sh -- "
                "env TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1 "
                "worker/.venv/bin/python -m worker.illusion_experiment run "
                f"--name variance_r{repeat} --type flip "
                '--prompt "a dog" --prompt "a sloth hanging from a branch" '
                f"--seed 2 --sds-objective legacy --out {out}"
            )
        return
    if args.cmd == "screen-plan":
        root = args.out_root / "screen"
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
        for pair_id, p1, p2 in SCREEN_PAIRS:
            for name, flags in profiles:
                out = root / name / pair_id
                print(
                    "scripts/gpu-lock.sh -- "
                    "env TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1 "
                    "worker/.venv/bin/python -m worker.illusion_experiment run "
                    f"--name {name}_{pair_id} --type flip "
                    f'--prompt "{p1}" --prompt "{p2}" --seed 2 {flags} --out {out}'
                )
        return
    if args.cmd == "final-plan":
        root = args.out_root / "final"
        for pair_id, p1, p2 in FINAL_PAIRS:
            for seed in (0, 1, 2):
                for tag, flags in (("legacy", "--sds-objective legacy"), ("finalist", args.finalist)):
                    out = root / tag / f"{pair_id}_seed{seed}"
                    print(
                        "scripts/gpu-lock.sh -- "
                        "env TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1 "
                        "worker/.venv/bin/python -m worker.illusion_experiment run "
                        f"--name {tag}_{pair_id}_s{seed} --type flip "
                        f'--prompt "{p1}" --prompt "{p2}" --seed {seed} {flags} --out {out}'
                    )
        return


# Allow `python -m worker.illusion_experiment`
if __name__ == "__main__":
    main()
