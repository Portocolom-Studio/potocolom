"""The committed OpenAPI snapshot matches the routes and models the app serves.

scripts/openapi-breaking.sh diffs this file against main to catch a response
or request change that breaks the previous release's SPA (issue #984). A stale
snapshot would make that diff compare the wrong API, so any route or model
change has to regenerate it in the same change.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "openapi-snapshot.py"


def test_committed_snapshot_matches_the_app():
    # A fresh process: the script builds the schema with default settings, as CI does.
    # Importing app.main here would take the settings of this test run, such as BENCHMARK_API.
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "backend/openapi.json is stale: a route or model changed. "
        "Regenerate it with scripts/openapi-snapshot.py and commit the result.\n"
        + result.stdout
        + result.stderr
    )
