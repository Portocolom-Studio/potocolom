#!/usr/bin/env bash
# Smoke-test the self-hosted compose stack with a simulated worker (no GPU).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_DIR="$ROOT/deploy/compose"
cd "$COMPOSE_DIR"

PORT="${COMPOSE_SMOKE_PORT:-18080}"
PROJECT="${COMPOSE_SMOKE_PROJECT:-potocolom-smoke}"
COMPOSE=(docker compose -p "$PROJECT" -f compose.smoke.yml)

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

if (echo >/dev/tcp/127.0.0.1/"$PORT") 2>/dev/null; then
  echo "port ${PORT} is already in use; stop the conflicting service or set COMPOSE_SMOKE_PORT" >&2
  exit 1
fi

export COMPOSE_SMOKE_PORT="$PORT"
"${COMPOSE[@]}" up -d --build --remove-orphans

cleanup() {
  "${COMPOSE[@]}" down -v --remove-orphans
}
trap cleanup EXIT

base="http://localhost:${PORT}"
for _ in $(seq 1 90); do
  if curl -sf "${base}/api/v1/health" >/dev/null; then
    break
  fi
  sleep 2
done
curl -sf "${base}/api/v1/health"

app_code=$(curl -s -o /dev/null -w '%{http_code}' "${base}/app")
if [[ "$app_code" != "200" ]]; then
  echo "expected /app to return 200, got ${app_code}" >&2
  exit 1
fi

job_id=$(curl -sf -X POST "${base}/api/v1/generations" \
  -H 'Content-Type: application/json' \
  -d '{"model_id":"sd-sim","params":{"prompt":"compose smoke test"}}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['job_id'])")

for _ in $(seq 1 60); do
  state=$(curl -sf "${base}/api/v1/generations/${job_id}" \
    | python3 -c "import sys, json; print(json.load(sys.stdin)['state'])")
  if [[ "$state" == "succeeded" ]]; then
    echo "compose smoke test passed (job ${job_id} on :${PORT})"
    exit 0
  fi
  sleep 1
done

echo "job ${job_id} did not reach succeeded" >&2
exit 1
