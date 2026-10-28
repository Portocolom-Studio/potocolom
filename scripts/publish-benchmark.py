#!/usr/bin/env python3
"""Sanitize a benchmark report for publication.

The public schema allows: created_at, target_vram_gb, prompt_count, models,
variants_per_prompt, total_jobs, completed_cells, complete, succeeded, failed,
skipped, model_stats, results, conditions, run_context, prompts,
model_revisions, auxiliary_revisions, manifests, samples, and publication.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PUBLIC_FIELDS = frozenset({
    "created_at",
    "target_vram_gb",
    "prompt_count",
    "models",
    "variants_per_prompt",
    "total_jobs",
    "completed_cells",
    "complete",
    "succeeded",
    "failed",
    "skipped",
    "model_stats",
    "results",
    "conditions",
    "run_context",
    "prompts",
    "model_revisions",
    "auxiliary_revisions",
    "manifests",
    "samples",
    "publication",
})
OPERATIONAL_FIELDS = frozenset({"api", "out_dir", "job_id", "cell_key"})


def sanitize_value(value: Any) -> Any:
    """Copy report data while dropping operational keys and private file paths."""
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if not isinstance(value, dict):
        return value

    clean = {}
    for key, item in value.items():
        if key in OPERATIONAL_FIELDS:
            continue
        if key == "file":
            if (
                isinstance(item, str)
                and item.startswith("/benchmark/")
                and ".." not in item.split("/")
            ):
                clean[key] = item
            continue
        clean[key] = sanitize_value(item)
    return clean


def sanitize_public_report(report: dict[str, Any]) -> dict[str, Any]:
    return {
        key: sanitize_value(value)
        for key, value in report.items()
        if key in PUBLIC_FIELDS
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="input benchmark results.json")
    parser.add_argument("destination", type=Path, help="public results.json")
    args = parser.parse_args()

    try:
        report = json.loads(args.source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read benchmark JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(report, dict):
        print("Benchmark JSON must be a top-level object.", file=sys.stderr)
        return 1

    public_report = sanitize_public_report(report)
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    args.destination.write_text(
        json.dumps(public_report, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
