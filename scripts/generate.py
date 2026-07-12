"""Generate one image end to end against a running API and worker.

    backend/.venv/bin/python scripts/generate.py "a castle on a hill at sunset"

Requires the backend and a worker running (docs/local-development.md), e.g.:

    cd backend && STORAGE_LOCAL_PATH=../data .venv/bin/uvicorn app.main:app
    cd worker  && MODELS_DIR=models DEVICE=rocm .venv/bin/python -m worker

Without MODELS_DIR the worker runs the simulated engine and this script
produces a flat colored image, which still exercises the whole path.
"""

import argparse
import json
import sys

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--model", default=None, help="default: first registered model")
    parser.add_argument("--out", default=None, help="default: <job id>.webp")
    args = parser.parse_args()

    with httpx.Client(base_url=args.api, timeout=30) as client:
        models = client.get("/api/v1/models").json()
        if not models:
            sys.exit("no models registered; is a worker connected?")
        preferred = next((m for m in models if m.get("default")), models[0])
        model_id = args.model or preferred["id"]

        created = client.post("/api/v1/generations",
                              json={"model_id": model_id, "params": {"prompt": args.prompt}})
        if created.status_code != 202:
            sys.exit(f"{created.status_code}: {created.text}")
        job_id = created.json()["job_id"]
        print(f"job {job_id} on {model_id}")

        with client.stream("GET", f"/api/v1/generations/{job_id}/events",
                           timeout=600) as stream:
            for line in stream.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[len("data: "):])
                progress = event.get("progress")
                detail = f" {progress:.0%}" if progress is not None else ""
                print(f"  {event['state']}{detail}")
                if event["state"] == "failed":
                    sys.exit(f"failed: {event.get('reason', 'see the worker log')}")
                if event["state"] == "succeeded":
                    break

        asset = client.get(f"/api/v1/generations/{job_id}").json()["assets"][0]
        image = httpx.get(asset["url"], timeout=30)
        image.raise_for_status()
        out = args.out or f"{job_id}.webp"
        with open(out, "wb") as file:
            file.write(image.content)
        print(f"{asset['width']}x{asset['height']} -> {out}")


if __name__ == "__main__":
    main()
