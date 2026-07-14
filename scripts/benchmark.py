"""Multi-model prompt benchmark with parameter variants and HTML report.

    backend/.venv/bin/python scripts/benchmark.py

Requires the API and a worker (docs/local-development.md). Configuration:
  benchmark-prompts.json  - 24 curated prompts
  benchmark-matrix.json   - models, 5 parameter variants each

See scripts/BENCHMARK.md for 16 GB VRAM model research and usage notes.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

from generate import generate_image

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_PATH = Path(__file__).with_name("benchmark-prompts.json")
MATRIX_PATH = Path(__file__).with_name("benchmark-matrix.json")


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or "item"


def parse_ids(raw: str | None, available: set[int]) -> list[int]:
    if not raw:
        return sorted(available)
    chosen: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            chosen.update(range(int(start_text), int(end_text) + 1))
        else:
            chosen.add(int(part))
    missing = sorted(chosen - available)
    if missing:
        raise SystemExit(f"unknown prompt id(s): {', '.join(str(i) for i in missing)}")
    return sorted(chosen)


def parse_csv(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def load_prompts(ids: list[int]) -> list[dict]:
    by_id = {entry["id"]: entry for entry in json.loads(PROMPTS_PATH.read_text())}
    return [by_id[i] for i in ids]


def load_matrix(model_filter: list[str] | None, quick: bool, include_capped: bool) -> dict:
    matrix = json.loads(MATRIX_PATH.read_text())
    capped = matrix.get("capped_commercial", [])
    if model_filter:
        wanted = set(model_filter)
        pool = matrix["models"] + capped
        models = [m for m in pool if m["id"] in wanted]
        missing = wanted - {m["id"] for m in models}
        if missing:
            raise SystemExit(f"unknown model(s) in matrix: {', '.join(sorted(missing))}")
    else:
        models = list(matrix["models"])
        if include_capped:
            models.extend(capped)
    if quick:
        models = [
            {**m, "variants": m["variants"][:1]}
            for m in models
        ]
    return {**matrix, "models": models}


def default_out_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return ROOT / "data" / "benchmark" / stamp


def variant_key(model_id: str, label: str) -> str:
    return f"{model_id}__{label}"


def params_summary(params: dict) -> str:
    keys = ("width", "height", "steps", "guidance", "seed")
    parts = [f"{k}={params[k]}" for k in keys if k in params]
    return ", ".join(parts) if parts else "defaults"


def fetch_registered_models(api: str, client: httpx.Client) -> set[str]:
    response = client.get(f"{api}/api/v1/benchmark/models")
    response.raise_for_status()
    return {m["id"] for m in response.json()}


LOAD_TIMEOUT = 1800.0
GPU_TIMEOUT = 120.0


def gpu_status(client: httpx.Client, api: str) -> list[str]:
    response = client.get(f"{api}/api/v1/benchmark/gpu", timeout=GPU_TIMEOUT)
    response.raise_for_status()
    return response.json().get("loaded_models", [])


def require_clean_gpu(client: httpx.Client, api: str, force: bool) -> None:
    loaded = gpu_status(client, api)
    if not loaded:
        return
    if force:
        print(f"GPU not clean ({', '.join(loaded)}); unloading (--force)")
        unload_all(client, api)
        loaded = gpu_status(client, api)
        if loaded:
            raise SystemExit(f"could not clear GPU: still loaded: {loaded}")
        return
    raise SystemExit(
        f"GPU not clean - loaded: {', '.join(loaded)}. "
        "Restart the worker or re-run with --force to unload first."
    )


def load_model(client: httpx.Client, api: str, model_id: str) -> int:
    response = client.post(f"{api}/api/v1/benchmark/gpu/load",
                           json={"model_id": model_id}, timeout=LOAD_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if data.get("type") == "gpu_error":
        raise RuntimeError(data.get("reason", "load failed"))
    return int(data.get("load_ms", 0))


def unload_model(client: httpx.Client, api: str, model_id: str) -> list[str]:
    response = client.post(f"{api}/api/v1/benchmark/gpu/unload",
                           json={"model_id": model_id}, timeout=GPU_TIMEOUT)
    response.raise_for_status()
    return response.json().get("loaded_models", [])


def unload_all(client: httpx.Client, api: str) -> list[str]:
    response = client.post(f"{api}/api/v1/benchmark/gpu/unload",
                           json={}, timeout=GPU_TIMEOUT)
    response.raise_for_status()
    return response.json().get("loaded_models", [])


def write_report_md(out_dir: Path, summary: dict) -> Path:
    lines = [
        "# Benchmark report",
        "",
        f"- **Created:** {summary['created_at']}",
        f"- **Prompts:** {summary['prompt_count']}",
        f"- **Models:** {', '.join(summary['models'])}",
        f"- **Variants per prompt:** {summary['variants_per_prompt']}",
        f"- **Images:** {summary['succeeded']} succeeded, {summary['failed']} failed",
        "",
        "## Model summary",
        "",
        "| Model | OK | Fail | load_ms | Avg gpu_ms | Median gpu_ms | Avg wall_s |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summary["model_stats"]:
        avg_gpu = f"{row['avg_gpu_ms']:.0f}" if row["avg_gpu_ms"] is not None else "-"
        med_gpu = f"{row['median_gpu_ms']:.0f}" if row["median_gpu_ms"] is not None else "-"
        load_ms = str(row["load_ms"]) if row.get("load_ms") is not None else "-"
        lines.append(
            f"| {row['model_id']} | {row['succeeded']} | {row['failed']} | {load_ms} "
            f"| {avg_gpu} | {med_gpu} | {row['avg_wall_s']:.1f} |"
        )
    lines.extend(["", "## Per-prompt results", ""])
    for prompt in summary["prompts"]:
        lines.append(f"### {prompt['id']:02d}. {prompt['title']} ({prompt['category']})")
        lines.append("")
        lines.append("| Model | Variant | gpu_ms | wall_s | File |")
        lines.append("| --- | --- | ---: | ---: | --- |")
        for row in prompt["runs"]:
            gpu = str(row["gpu_ms"]) if row.get("gpu_ms") is not None else "-"
            state = row.get("state", "succeeded")
            file_cell = row.get("file", state)
            if state != "succeeded":
                file_cell = f"**{state}**: {row.get('error', '')}"
            lines.append(
                f"| {row['model_id']} | {row['variant']} | {gpu} "
                f"| {row.get('wall_s', '-')} | {file_cell} |"
            )
        lines.append("")
    path = out_dir / "report.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def write_report_html(out_dir: Path, summary: dict, prompts: list[dict],
                      matrix_models: list[dict]) -> Path:
    variant_columns: list[tuple[str, str, str]] = []
    for model in matrix_models:
        for variant in model["variants"]:
            variant_columns.append((model["id"], variant["label"], variant_key(model["id"], variant["label"])))

    by_prompt_cell: dict[int, dict[str, dict]] = defaultdict(dict)
    for row in summary["results"]:
        if row.get("state") != "succeeded":
            continue
        by_prompt_cell[row["prompt_id"]][row["cell_key"]] = row

    model_stats = {row["model_id"]: row for row in summary["model_stats"]}

    css = """
    body { font-family: system-ui, sans-serif; margin: 1.5rem; background: #111; color: #eee; }
    h1, h2 { font-weight: 600; }
    table { border-collapse: collapse; width: 100%; margin-bottom: 2rem; }
    th, td { border: 1px solid #333; padding: 0.4rem 0.6rem; vertical-align: top; }
    th { background: #1a1a1a; position: sticky; top: 0; }
    .prompt-col { min-width: 12rem; background: #161616; }
    img { max-width: 140px; max-height: 140px; display: block; border-radius: 4px; }
    .meta { font-size: 0.75rem; color: #aaa; margin-top: 0.25rem; }
    .fail { color: #f88; font-size: 0.8rem; }
    .stats td, .stats th { text-align: right; }
    .stats th:first-child, .stats td:first-child { text-align: left; }
    .note { color: #999; font-size: 0.9rem; max-width: 48rem; }
    """

    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        f"<title>Benchmark {html.escape(summary['created_at'])}</title>",
        f"<style>{css}</style></head><body>",
        "<h1>Image generation benchmark</h1>",
        f"<p class='note'>{summary['succeeded']} images, "
        f"{summary['prompt_count']} prompts, "
        f"{len(matrix_models)} models, "
        f"{summary['variants_per_prompt']} variants each. "
        f"Target GPU: {summary.get('target_vram_gb', '?')} GB VRAM.</p>",
        "<h2>Model summary</h2>",
        "<table class='stats'><thead><tr>",
        "<th>Model</th><th>OK</th><th>Fail</th><th>Avg gpu_ms</th>",
        "<th>Median gpu_ms</th><th>Avg wall_s</th></tr></thead><tbody>",
    ]
    for model in matrix_models:
        stats = model_stats.get(model["id"], {})
        avg_gpu = stats.get("avg_gpu_ms")
        med_gpu = stats.get("median_gpu_ms")
        parts.append(
            "<tr>"
            f"<td>{html.escape(model['id'])}</td>"
            f"<td>{stats.get('succeeded', 0)}</td>"
            f"<td>{stats.get('failed', 0)}</td>"
        f"<td>{'-' if avg_gpu is None else f'{avg_gpu:.0f}'}</td>"
        f"<td>{'-' if med_gpu is None else f'{med_gpu:.0f}'}</td>"
            f"<td>{stats.get('avg_wall_s', 0):.1f}</td>"
            "</tr>"
        )
    parts.append("</tbody></table>")

    for model in matrix_models:
        parts.append(f"<h2>{html.escape(model['id'])}</h2>")
        if model.get("notes"):
            parts.append(f"<p class='note'>{html.escape(model['notes'])}</p>")
        parts.append("<table><thead><tr><th class='prompt-col'>Prompt</th>")
        model_variants = [(mid, label, key) for mid, label, key in variant_columns if mid == model["id"]]
        for _, label, _ in model_variants:
            parts.append(f"<th>{html.escape(label)}</th>")
        parts.append("</tr></thead><tbody>")
        for prompt in prompts:
            parts.append("<tr>")
            parts.append(
                f"<td class='prompt-col'><strong>{prompt['id']:02d}</strong> "
                f"{html.escape(prompt['title'])}<br>"
                f"<span class='meta'>{html.escape(prompt['category'])}</span></td>"
            )
            cells = by_prompt_cell.get(prompt["id"], {})
            for _, _, key in model_variants:
                row = cells.get(key)
                if row is None:
                    parts.append("<td class='fail'>skipped</td>")
                    continue
                if row.get("state") != "succeeded":
                    parts.append(f"<td class='fail'>{html.escape(row.get('error', 'failed'))}</td>")
                    continue
                rel = html.escape(row["file"])
                gpu = row.get("gpu_ms")
                meta = html.escape(params_summary(row.get("params", {})))
                gpu_text = f"{gpu} ms" if gpu is not None else ""
                parts.append(
                    f"<td><a href='{rel}'><img src='{rel}' alt=''></a>"
                    f"<div class='meta'>{gpu_text}<br>{meta}</div></td>"
                )
            parts.append("</tr>")
        parts.append("</tbody></table>")

    parts.append("</body></html>")
    path = out_dir / "report.html"
    path.write_text("".join(parts))
    return path


def compute_model_stats(rows: list[dict]) -> list[dict]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        buckets[row["model_id"]].append(row)

    stats = []
    for model_id in sorted(buckets):
        bucket = buckets[model_id]
        ok = [r for r in bucket if r.get("state") == "succeeded"]
        fail = [r for r in bucket if r.get("state") != "succeeded"]
        gpu_values = [r["gpu_ms"] for r in ok if r.get("gpu_ms") is not None]
        wall_values = [r["wall_s"] for r in ok if r.get("wall_s") is not None]
        stats.append({
            "model_id": model_id,
            "succeeded": len(ok),
            "failed": len(fail),
            "load_ms": ok[0].get("model_load_ms") if ok else None,
            "avg_gpu_ms": statistics.mean(gpu_values) if gpu_values else None,
            "median_gpu_ms": statistics.median(gpu_values) if gpu_values else None,
            "avg_wall_s": statistics.mean(wall_values) if wall_values else 0.0,
        })
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="default: data/benchmark/<utc timestamp>")
    parser.add_argument("--ids", default=None,
                        help="comma-separated prompt ids and/or ranges, e.g. 1,5,10-12")
    parser.add_argument("--models", default=None,
                        help="comma-separated model ids from benchmark-matrix.json")
    parser.add_argument("--quick", action="store_true",
                        help="one variant per model (smoke test)")
    parser.add_argument("--continue-on-error", action="store_true",
                        help="keep going after a failed generation")
    parser.add_argument("--force", action="store_true",
                        help="unload resident models instead of aborting preflight")
    parser.add_argument("--include-capped", action="store_true",
                        help="include capped_commercial models (Stability)")
    args = parser.parse_args()

    all_prompts = json.loads(PROMPTS_PATH.read_text())
    available_ids = {entry["id"] for entry in all_prompts}
    ids = parse_ids(args.ids, available_ids)
    prompts = load_prompts(ids)

    model_filter = parse_csv(args.models)
    matrix = load_matrix(model_filter, args.quick, args.include_capped)
    matrix_models = matrix["models"]
    variants_per_prompt = len(matrix_models[0]["variants"]) if matrix_models else 0

    out_dir = args.out_dir or default_out_dir()
    images_dir = out_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    total_jobs = len(prompts) * sum(len(m["variants"]) for m in matrix_models)
    print(f"benchmark: {total_jobs} jobs ({len(prompts)} prompts, "
          f"{len(matrix_models)} models, {variants_per_prompt} variants) -> {out_dir}")

    rows: list[dict] = []
    failures = 0
    job_no = 0

    with httpx.Client(base_url=args.api, timeout=30) as client:
        registered = fetch_registered_models(args.api, client)
        planned = {m["id"] for m in matrix_models}
        missing = sorted(planned - registered)
        if missing:
            print(f"warning: not listed on API registry: {', '.join(missing)}",
                  file=sys.stderr)
            print("  attempting GPU load anyway (worker may still serve them)",
                  file=sys.stderr)

        require_clean_gpu(client, args.api, args.force)

        try:
            for model in matrix_models:
                print(f"\n=== model: {model['id']} ===")
                try:
                    load_ms = load_model(client, args.api, model["id"])
                except httpx.HTTPError as error:
                    raise SystemExit(f"failed to load {model['id']}: {error}") from error
                print(f"loaded in {load_ms} ms")

                for entry in prompts:
                    prompt_dir = images_dir / f"{entry['id']:02d}-{slugify(entry['title'])}"
                    prompt_dir.mkdir(parents=True, exist_ok=True)
                    print(f"\n-- prompt {entry['id']:02d}: {entry['title']} --")

                    for variant in model["variants"]:
                        job_no += 1
                        label = variant["label"]
                        cell = variant_key(model["id"], label)
                        params = dict(variant["params"])
                        w = params.get("width", "")
                        h = params.get("height", "")
                        steps = params.get("steps", "")
                        filename = f"{cell}__{w}x{h}-s{steps}.webp"
                        rel_file = f"images/{prompt_dir.name}/{filename}"
                        out_path = prompt_dir / filename

                        print(f"[{job_no}/{total_jobs}] {model['id']} / {label}")
                        started = time.perf_counter()
                        base_row = {
                            "prompt_id": entry["id"],
                            "title": entry["title"],
                            "category": entry["category"],
                            "model_id": model["id"],
                            "variant": label,
                            "cell_key": cell,
                            "params": params,
                            "model_load_ms": load_ms,
                        }
                        try:
                            result = generate_image(
                                entry["prompt"],
                                params=params,
                                api=args.api,
                                model=model["id"],
                                out=out_path,
                                client=client,
                                quiet=True,
                            )
                        except RuntimeError as error:
                            failures += 1
                            print(f"  failed: {error}", file=sys.stderr)
                            rows.append({
                                **base_row,
                                "state": "failed",
                                "error": str(error),
                                "wall_s": round(time.perf_counter() - started, 2),
                            })
                            if not args.continue_on_error:
                                break
                            continue

                        wall_s = round(time.perf_counter() - started, 2)
                        print(f"  {result.width}x{result.height} "
                              f"gpu_ms={result.gpu_ms} wall={wall_s}s")
                        rows.append({
                            **base_row,
                            "state": "succeeded",
                            "job_id": result.job_id,
                            "file": rel_file,
                            "width": result.width,
                            "height": result.height,
                            "gpu_ms": result.gpu_ms,
                            "wall_s": wall_s,
                        })
                    else:
                        continue
                    break

                remaining = unload_model(client, args.api, model["id"])
                if remaining:
                    print(f"warning: after unload expected empty GPU, got {remaining}",
                          file=sys.stderr)
                else:
                    print(f"unloaded {model['id']}")
        finally:
            leftover = gpu_status(client, args.api)
            if leftover:
                print(f"cleaning up GPU: {', '.join(leftover)}")
                unload_all(client, args.api)

    model_stats = compute_model_stats(rows)
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "api": args.api,
        "target_vram_gb": matrix.get("target_vram_gb"),
        "out_dir": str(out_dir),
        "prompt_count": len(prompts),
        "models": [m["id"] for m in matrix_models],
        "variants_per_prompt": variants_per_prompt,
        "total_jobs": total_jobs,
        "succeeded": sum(1 for row in rows if row.get("state") == "succeeded"),
        "failed": sum(1 for row in rows if row.get("state") != "succeeded"),
        "model_stats": model_stats,
        "results": rows,
    }

    prompt_sections = []
    for entry in prompts:
        prompt_sections.append({
            "id": entry["id"],
            "title": entry["title"],
            "category": entry["category"],
            "runs": [row for row in rows if row["prompt_id"] == entry["id"]],
        })
    summary["prompts"] = prompt_sections

    results_path = out_dir / "results.json"
    results_path.write_text(json.dumps(summary, indent=2) + "\n")
    md_path = write_report_md(out_dir, summary)
    html_path = write_report_html(out_dir, summary, prompts, matrix_models)

    print("\n--- model summary ---")
    for row in model_stats:
        avg = f"{row['avg_gpu_ms']:.0f}" if row["avg_gpu_ms"] is not None else "-"
        load = f"{row['load_ms']}" if row.get("load_ms") is not None else "-"
        print(f"  {row['model_id']:16s}  load_ms={load:>6}  "
              f"ok={row['succeeded']:3d}  fail={row['failed']:2d}  avg_gpu_ms={avg}")

    print(f"\nresults.json -> {results_path}")
    print(f"report.md    -> {md_path}")
    print(f"report.html  -> {html_path}")

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
