"""Generate a hero-image batch with sdxl-base 1024 high-guidance.

    backend/.venv/bin/python scripts/generate-hero-batch.py

Requires API + worker (make dev-start). Writes to data/hero-batch/<id>-<slug>/
using the same filename shape as the benchmark so generate-hero-images.mjs can
ingest them. Re-runs skip existing outputs unless --force.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

from generate import generate_image

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_PATH = Path(__file__).with_name("hero-batch-prompts.json")
DEFAULT_OUT = ROOT / "data" / "hero-batch"

MODEL_ID = "sdxl-base"
VARIANT = "1024-high-guidance"
PARAMS = {
    "width": 1024,
    "height": 1024,
    "steps": 20,
    "guidance": 8.0,
}


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or "item"


def variant_filename() -> str:
    p = PARAMS
    return f"{MODEL_ID}__{VARIANT}__{p['width']}x{p['height']}-s{p['steps']}.webp"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--force", action="store_true", help="regenerate existing files")
    parser.add_argument(
        "--ids",
        default=None,
        help="comma/range list of prompt ids, e.g. 1-10,15",
    )
    parser.add_argument("--seed-base", type=int, default=1000, help="seed = base + id")
    args = parser.parse_args()

    prompts = json.loads(PROMPTS_PATH.read_text())
    by_id = {entry["id"]: entry for entry in prompts}
    if args.ids:
        chosen: set[int] = set()
        for part in args.ids.split(","):
            part = part.strip()
            if "-" in part:
                a, b = part.split("-", 1)
                chosen.update(range(int(a), int(b) + 1))
            else:
                chosen.add(int(part))
        missing = sorted(chosen - set(by_id))
        if missing:
            raise SystemExit(f"unknown prompt id(s): {missing}")
        prompts = [by_id[i] for i in sorted(chosen)]

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = variant_filename()
    print(
        f"hero-batch: {len(prompts)} prompts -> {out_dir}\n"
        f"  model={MODEL_ID} variant={VARIANT} file={filename}"
    )

    ok = 0
    skipped = 0
    failed = 0
    t0 = time.monotonic()

    with httpx.Client(base_url=args.api, timeout=30) as client:
        models = client.get("/api/v1/models").json()
        ids = {m["id"] for m in models}
        if MODEL_ID not in ids:
            raise SystemExit(
                f"{MODEL_ID} not registered; connected models: {sorted(ids)}"
            )

        for entry in prompts:
            slug = f"{entry['id']:02d}-{slugify(entry['title'])}"
            dest_dir = out_dir / slug
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / filename
            if dest.exists() and not args.force:
                print(f"skip {slug} (exists)")
                skipped += 1
                continue

            seed = args.seed_base + int(entry["id"])
            params = {**PARAMS, "seed": seed}
            print(f"\n[{ok + skipped + failed + 1}/{len(prompts)}] {slug} seed={seed}")
            try:
                result = generate_image(
                    entry["prompt"],
                    params=params,
                    api=args.api,
                    model=MODEL_ID,
                    out=dest,
                    client=client,
                )
                print(f"  ok gpu_ms={result.gpu_ms} -> {dest}")
                ok += 1
            except Exception as error:  # noqa: BLE001 - keep the batch going
                failed += 1
                print(f"  FAIL: {error}", file=sys.stderr)

    elapsed = time.monotonic() - t0
    print(
        f"\ndone: ok={ok} skipped={skipped} failed={failed} "
        f"elapsed={elapsed / 60:.1f}min"
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
