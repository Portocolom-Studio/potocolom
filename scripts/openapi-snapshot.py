#!/usr/bin/env python3
"""Write, or check, backend/openapi.json: the API schema the app serves by default (issue #984).

    backend/.venv/bin/python scripts/openapi-snapshot.py          # write the snapshot
    backend/.venv/bin/python scripts/openapi-snapshot.py --check  # fail, with a diff, when it is stale

Run with PYTHONPATH=backend (the Makefile target check-openapi does). The schema is
built with default settings: BENCHMARK_API is cleared, so /api/v1/benchmark/models is
left out, while the benchmark sessions routes are always mounted and therefore included.
"""

import argparse
import difflib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "backend" / "openapi.json"


def render() -> str:
    # app.main refuses to import without a fleet token key. The schema does not depend on its value.
    os.environ["FLEET_TOKEN_KEY"] = "openapi-snapshot"
    os.environ.pop("BENCHMARK_API", None)
    from app.main import app

    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 and print the diff when the committed snapshot is stale",
    )
    args = parser.parse_args(argv)
    live = render()
    shown = SNAPSHOT.relative_to(ROOT)
    if not args.check:
        SNAPSHOT.write_text(live, encoding="utf-8")
        print(f"wrote {shown}")
        return 0
    committed = SNAPSHOT.read_text(encoding="utf-8") if SNAPSHOT.is_file() else ""
    if committed == live:
        print(f"openapi: {shown} is current")
        return 0
    sys.stdout.writelines(
        difflib.unified_diff(
            committed.splitlines(keepends=True),
            live.splitlines(keepends=True),
            fromfile=f"{shown} (committed)",
            tofile=f"{shown} (generated)",
        )
    )
    print(
        f"error: {shown} is stale: a route or model changed. "
        "Run scripts/openapi-snapshot.py and commit the result.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
