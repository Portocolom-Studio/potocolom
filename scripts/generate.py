"""Generate one image end to end against a running API and worker.

    backend/.venv/bin/python scripts/generate.py "a castle on a hill at sunset"

Requires the backend and a worker running (docs/local-development.md), e.g.:

    cd backend && STORAGE_LOCAL_PATH=../data .venv/bin/uvicorn app.main:app
    cd worker  && MODELS_DIR=models DEVICE=rocm .venv/bin/python -m worker

Without MODELS_DIR the worker runs the simulated engine and this script
produces a flat colored image, which still exercises the whole path.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx


@dataclass(frozen=True)
class GenerateResult:
    job_id: str
    model_id: str
    out_path: Path
    width: int
    height: int
    gpu_ms: int | None


def generate_image(
    prompt: str,
    *,
    params: dict | None = None,
    api: str = "http://localhost:8000",
    model: str | None = None,
    out: str | Path | None = None,
    client: httpx.Client | None = None,
    quiet: bool = False,
) -> GenerateResult:
    """Submit one generation job, wait for completion, and save the image."""

    def log(message: str) -> None:
        if not quiet:
            print(message)

    own_client = client is None
    http = client or httpx.Client(base_url=api, timeout=30)
    try:
        models = http.get("/api/v1/models").json()
        if not models:
            raise RuntimeError("no models registered; is a worker connected?")
        preferred = next((m for m in models if m.get("default")), models[0])
        model_id = model or preferred["id"]

        job_params = {"prompt": prompt, **(params or {})}
        created = http.post("/api/v1/generations",
                            json={"model_id": model_id, "params": job_params})
        if created.status_code != 202:
            raise RuntimeError(f"{created.status_code}: {created.text}")
        job_id = created.json()["job_id"]
        log(f"job {job_id} on {model_id}")

        with http.stream("GET", f"/api/v1/generations/{job_id}/events",
                         timeout=600) as stream:
            for line in stream.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[len("data: "):])
                progress = event.get("progress")
                detail = f" {progress:.0%}" if progress is not None else ""
                log(f"  {event['state']}{detail}")
                if event["state"] == "failed":
                    raise RuntimeError(event.get("reason", "see the worker log"))
                if event["state"] == "succeeded":
                    break

        job = http.get(f"/api/v1/generations/{job_id}").json()
        asset = job["assets"][0]
        image = httpx.get(asset["url"], timeout=30)
        image.raise_for_status()
        out_path = Path(out or f"{job_id}.webp")
        out_path.write_bytes(image.content)
        log(f"{asset['width']}x{asset['height']} -> {out_path}")
        return GenerateResult(
            job_id=job_id,
            model_id=model_id,
            out_path=out_path,
            width=asset["width"],
            height=asset["height"],
            gpu_ms=job.get("gpu_ms"),
        )
    finally:
        if own_client:
            http.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--model", default=None, help="default: first registered model")
    parser.add_argument("--out", default=None, help="default: <job id>.webp")
    args = parser.parse_args()

    try:
        generate_image(args.prompt, api=args.api, model=args.model, out=args.out)
    except RuntimeError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
