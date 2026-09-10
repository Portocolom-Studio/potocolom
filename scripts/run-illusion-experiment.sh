#!/usr/bin/env bash
# Wrap an illusion experiment with the cooperative GPU lock.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec "$ROOT/scripts/gpu-lock.sh" "$@"
