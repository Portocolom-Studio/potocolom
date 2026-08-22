#!/usr/bin/env bash
# Smoke-test the self-hosted compose stack with a simulated worker (no GPU).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_DIR="$ROOT/deploy/compose"
cd "$COMPOSE_DIR"

PROJECT="${COMPOSE_SMOKE_PROJECT:-potocolom-smoke}"
COMPOSE=(docker compose -p "$PROJECT" -f compose.smoke.yml)

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

port_free() {
  ! (echo >/dev/tcp/127.0.0.1/"$1") 2>/dev/null
}

if [[ -n "${COMPOSE_SMOKE_PORT:-}" ]]; then
  # Asked for explicitly, so a clash is the operator's to resolve.
  PORT="$COMPOSE_SMOKE_PORT"
  if ! port_free "$PORT"; then
    echo "port ${PORT} is already in use; stop the conflicting service or set COMPOSE_SMOKE_PORT" >&2
    exit 1
  fi
else
  # Several self-hosted runners share one machine, so a fixed default meant two
  # smoke tests at once fought over one port. Take 18080 when it is free and
  # let the OS name one when it is not.
  PORT=18080
  if ! port_free "$PORT"; then
    PORT="$(python3 -c "
import socket
s = socket.socket()
s.bind(('127.0.0.1', 0))
print(s.getsockname()[1])
s.close()
")"
  fi
fi

export COMPOSE_SMOKE_PORT="$PORT"

cleanup() {
  "${COMPOSE[@]}" down -v --remove-orphans || true
}
trap cleanup EXIT

"${COMPOSE[@]}" up -d --build --remove-orphans

base="http://localhost:${PORT}"
for _ in $(seq 1 90); do
  if curl -sf "${base}/api/v1/health" >/dev/null; then
    break
  fi
  sleep 2
done
curl -sf "${base}/api/v1/health"

# The stack runs keyed, so an unauthenticated upgrade must be refused. Without
# this the smoke run only proves that a matching secret works, and a check that
# went permissive would still pass everything below.
fleet_code=$(curl -s -o /dev/null -w '%{http_code}' \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  "${base}/api/v1/fleet")
if [[ "$fleet_code" != "403" ]]; then
  echo "expected an untokened fleet upgrade to return 403, got ${fleet_code}" >&2
  exit 1
fi

app_code=$(curl -s -o /dev/null -w '%{http_code}' "${base}/app")
if [[ "$app_code" != "200" ]]; then
  echo "expected /app to return 200, got ${app_code}" >&2
  exit 1
fi
if ! curl -sfD - -o /dev/null "${base}/app" \
  | tr -d '\r' \
  | awk 'BEGIN { found = 0 }
         tolower($0) ~ /^cache-control:/ && tolower($0) ~ /no-cache/ { found = 1 }
         END { exit !found }'; then
  echo "expected /app to return Cache-Control: no-cache" >&2
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
