#!/usr/bin/env bash
# Smoke-test the self-hosted compose stack with a simulated worker (no GPU).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_DIR="$ROOT/deploy/compose"
cd "$COMPOSE_DIR"

PROJECT="${COMPOSE_SMOKE_PROJECT:-potocolom-smoke}"
COMPOSE=(docker compose -p "$PROJECT" -f compose.smoke.yml)
BUILD=(--build)
OVERRIDE=""
REALTIME_PID=""
REALTIME_LOG=""
if [[ -n "${COMPOSE_SMOKE_API_IMAGE:-}${COMPOSE_SMOKE_WORKER_IMAGE:-}" ]]; then
  : "${COMPOSE_SMOKE_API_IMAGE:?both smoke images must be set}"
  : "${COMPOSE_SMOKE_WORKER_IMAGE:?both smoke images must be set}"
  OVERRIDE=$(mktemp)
  cat >"$OVERRIDE" <<'YAML'
services:
  api:
    image: ${COMPOSE_SMOKE_API_IMAGE}
  worker-sim:
    image: ${COMPOSE_SMOKE_WORKER_IMAGE}
    environment:
      DEVICE: cpu
      MODELS_DIR: ""
YAML
  COMPOSE+=(-f "$OVERRIDE")
  BUILD=(--no-build --pull never)
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

port_free() {
  ! (echo >/dev/tcp/127.0.0.1/"$1") 2>/dev/null
}

free_port() {
  python3 -c "
import socket
s = socket.socket()
s.bind(('127.0.0.1', 0))
print(s.getsockname()[1])
s.close()
"
}

REQUESTED_PORT="${COMPOSE_SMOKE_PORT:-}"
if [[ -n "$REQUESTED_PORT" ]]; then
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
    PORT="$(free_port)"
  fi
fi

export COMPOSE_SMOKE_PORT="$PORT"
base="http://localhost:${PORT}"

cleanup() {
  if [[ -n "$REALTIME_PID" ]] && kill -0 "$REALTIME_PID" 2>/dev/null; then
    kill "$REALTIME_PID" || true
    wait "$REALTIME_PID" || true
  fi
  "${COMPOSE[@]}" down -v --remove-orphans || true
  if [[ -n "$OVERRIDE" ]]; then rm -f "$OVERRIDE"; fi
  if [[ -n "$REALTIME_LOG" ]]; then rm -f "$REALTIME_LOG"; fi
}
trap cleanup EXIT

# Probing a port and then binding it are two steps, and another job can take
# it in between. Retry on the bind failure rather than pretending the probe
# was a reservation. PUBLIC_URL has to be settled before the API starts,
# because it reads it at boot, so the port cannot simply be left to Docker.
for attempt in 1 2 3; do
  if "${COMPOSE[@]}" up -d "${BUILD[@]}" --remove-orphans; then
    break
  fi
  if [[ "$attempt" == 3 ]]; then
    echo "could not start the smoke stack after 3 attempts" >&2
    exit 1
  fi
  "${COMPOSE[@]}" down -v --remove-orphans || true
  if [[ -n "$REQUESTED_PORT" ]]; then
    # The probe at the top already refused a busy requested port. Moving off it
    # now would pass the smoke test on a port the caller never asked for, and
    # whatever took it would go unreported.
    echo "could not start the smoke stack on requested port ${PORT}" >&2
    exit 1
  fi
  echo "start failed on port ${PORT}; taking another and retrying" >&2
  PORT="$(free_port)"
  export COMPOSE_SMOKE_PORT="$PORT"
  base="http://localhost:${PORT}"
done

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

# Health answers before the simulated worker registers, and a job for a model
# no worker offers yet is a 404, so wait until sd-sim is listed.
model_listed=0
for _ in $(seq 1 60); do
  if curl -sf "${base}/api/v1/models" | grep -q '"id":"sd-sim"'; then
    model_listed=1
    break
  fi
  sleep 1
done
if [[ "$model_listed" != 1 ]]; then
  echo "the simulated worker never offered sd-sim" >&2
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
    break
  fi
  sleep 1
done

if [[ "$state" != "succeeded" ]]; then
  echo "job ${job_id} did not reach succeeded" >&2
  exit 1
fi

primary_worker=$("${COMPOSE[@]}" ps -q worker-sim)
if [[ -z "$primary_worker" ]] || [[ "$primary_worker" == *$'\n'* ]]; then
  echo "expected exactly one primary worker before failover smoke" >&2
  exit 1
fi

REALTIME_LOG=$(mktemp)
"${COMPOSE[@]}" exec -T api python - < "$ROOT/scripts/smoke-realtime.py" \
  >"$REALTIME_LOG" 2>&1 &
REALTIME_PID=$!

first_frame_deadline=$((SECONDS + 30))
until grep -q '^FIRST_FRAME ' "$REALTIME_LOG"; do
  if ! kill -0 "$REALTIME_PID" 2>/dev/null; then
    cat "$REALTIME_LOG" >&2
    wait "$REALTIME_PID" || true
    REALTIME_PID=""
    exit 1
  fi
  if (( SECONDS >= first_frame_deadline )); then
    echo "realtime smoke did not produce its first frame" >&2
    exit 1
  fi
  sleep 0.1
done

"${COMPOSE[@]}" up -d --no-build --no-deps --no-recreate --scale worker-sim=2 worker-sim
registered=0
registration_deadline=$((SECONDS + 30))
while (( SECONDS < registration_deadline )); do
  registered=$("${COMPOSE[@]}" exec -T postgres psql -At -U potocolom -d potocolom \
    -c "SELECT count(*) FROM audit_events WHERE action = 'fleet.worker_registered'")
  if (( registered >= 2 )); then break; fi
  sleep 0.1
done
if (( registered < 2 )); then
  echo "replacement worker did not register" >&2
  exit 1
fi

docker kill "$primary_worker" >/dev/null
if ! wait "$REALTIME_PID"; then
  cat "$REALTIME_LOG" >&2
  exit 1
fi
REALTIME_PID=""
cat "$REALTIME_LOG"
grep -q '^FAILOVER_PASSED ' "$REALTIME_LOG"

"${COMPOSE[@]}" exec -T postgres createdb -U potocolom upgrade_smoke
"${COMPOSE[@]}" run --rm --no-deps -T api sh -c \
  'DATABASE_URL="${DATABASE_URL%/*}/upgrade_smoke" alembic upgrade 0023'
"${COMPOSE[@]}" exec -T postgres psql -v ON_ERROR_STOP=1 -U potocolom -d upgrade_smoke \
  < "$ROOT/scripts/smoke-upgrade-seed.sql"
"${COMPOSE[@]}" run --rm --no-deps -T api sh -c \
  'DATABASE_URL="${DATABASE_URL%/*}/upgrade_smoke" alembic upgrade head'
"${COMPOSE[@]}" exec -T postgres psql -v ON_ERROR_STOP=1 -U potocolom -d upgrade_smoke \
  < "$ROOT/scripts/smoke-upgrade-check.sql"
"${COMPOSE[@]}" exec -T postgres createdb -U potocolom restore_smoke
"${COMPOSE[@]}" exec -T postgres pg_dump -U potocolom -Fc upgrade_smoke \
  | "${COMPOSE[@]}" exec -T postgres pg_restore -U potocolom --exit-on-error -d restore_smoke
"${COMPOSE[@]}" exec -T postgres psql -v ON_ERROR_STOP=1 -U potocolom -d restore_smoke \
  < "$ROOT/scripts/smoke-upgrade-check.sql"

echo "compose smoke passed: generation ${job_id}, realtime, schema upgrade and restore"
