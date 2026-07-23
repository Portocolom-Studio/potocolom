#!/usr/bin/env bash
# Wrap an illusion experiment with the cooperative GPU lock.
# Usage: scripts/run-illusion-experiment.sh [--force] -- <python -m worker.illusion_experiment ...>
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec "$ROOT/scripts/gpu-lock.sh" "$@"
