#!/usr/bin/env python3
"""Merge benchmark results.json files from incremental runs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark import compute_model_stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", type=Path, help="results.json files in order")
    parser.add_argument("-o", "--out-dir", type=Path, required=True)
    args = parser.parse_args()

    base = json.loads(args.sources[0].read_text())
    rows: list[dict] = []
    for path in args.sources:
        if not path.exists():
            raise SystemExit(f"missing {path}")
        rows.extend(json.loads(path.read_text())["results"])

    args.out_dir.mkdir(parents=True, exist_ok=True)
    models = sorted({row["model_id"] for row in rows})
    base.update({
        "out_dir": str(args.out_dir),
        "models": models,
        "total_jobs": len(rows),
        "succeeded": sum(1 for row in rows if row.get("state") == "succeeded"),
        "failed": sum(1 for row in rows if row.get("state") != "succeeded"),
        "model_stats": compute_model_stats(rows),
        "results": rows,
    })
    out_path = args.out_dir / "results.json"
    out_path.write_text(json.dumps(base, indent=2) + "\n")
    print(f"wrote {out_path} ({len(models)} models, {len(rows)} jobs)")


if __name__ == "__main__":
    main()
