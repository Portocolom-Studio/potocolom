#!/usr/bin/env bash
# Unattended: wait for full-run, restart API+worker, benchmark new models, merge, publish.
set +e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LOG="$ROOT/data/benchmark/supplement-pipeline.log"
LOCK="$ROOT/data/benchmark/supplement-pipeline.lock"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >>"$LOG"; }

exec 9>"$LOCK"
if ! flock -n 9; then
  log "another supplement pipeline is already running — exiting"
  exit 0
fi

log "=== supplement pipeline started ==="

PYTHON="$ROOT/backend/.venv/bin/python"
BENCH_SCRIPT="$ROOT/scripts/benchmark.py"
MERGE_SCRIPT="$ROOT/scripts/benchmark-merge.py"
API="http://localhost:8000"
FULL_RUN="$ROOT/data/benchmark/full-run/results.json"
MAX_WAIT_HOURS="${MAX_WAIT_HOURS:-10}"

wait_for_file() {
  local path="$1" label="$2"
  local deadline=$((SECONDS + MAX_WAIT_HOURS * 3600))
  log "waiting for $label ($path), up to ${MAX_WAIT_HOURS}h"
  while [[ ! -f "$path" ]]; do
    if (( SECONDS > deadline )); then
      log "timed out waiting for $path"
      exit 1
    fi
    if ! pgrep -f "scripts/benchmark.py --out-dir data/benchmark/full-run" >/dev/null 2>&1; then
      if [[ ! -f "$path" ]]; then
        log "full-run exited without writing $path"
        exit 1
      fi
      break
    fi
    sleep 120
  done
  log "$label ready"
}

api_up() {
  curl -sf --max-time 5 "$API/api/v1/models" >/dev/null 2>&1
}

worker_models() {
  curl -sf --max-time 5 "$API/api/v1/benchmark/models" 2>/dev/null \
    || curl -sf --max-time 5 "$API/api/v1/models"
}

has_model() {
  local model_id="$1"
  worker_models | "$PYTHON" -c 'import json,sys; ids={m["id"] for m in json.load(sys.stdin)}; sys.exit(0 if sys.argv[1] in ids else 1)' "$model_id"
}

restart_stack() {
  if api_up && curl -sf --max-time 5 "$API/api/v1/benchmark/gpu" >/dev/null 2>&1; then
    log "API and worker already up — skipping restart"
    if ! has_model krea-2-turbo; then
      log "restarting worker only (krea-2-turbo not registered)"
      for pid in $(pgrep -f "worker/\.venv/bin/python -m worker"); do kill "$pid" 2>/dev/null; done
      sleep 10
      nohup bash -c "cd '$ROOT/worker' && MODELS_DIR=models DEVICE=rocm .venv/bin/python -m worker" \
        >>"$ROOT/data/benchmark/worker-overnight.log" 2>&1 &
      log "worker pid $!"
      for _ in $(seq 1 180); do
        if curl -sf --max-time 5 "$API/api/v1/benchmark/gpu" >/dev/null 2>&1; then
          log "worker reconnected"
          break
        fi
        sleep 10
      done
    fi
    return 0
  fi

  log "=== restarting API and worker ==="
  for pid in $(pgrep -f "worker/\.venv/bin/python -m worker"); do kill "$pid" 2>/dev/null; done
  if command -v fuser >/dev/null 2>&1; then
    fuser -k 8000/tcp 2>/dev/null || true
  else
    for pid in $(pgrep -f "uvicorn app.main:app"); do kill "$pid" 2>/dev/null; done
  fi
  sleep 15

  nohup bash -c "cd '$ROOT/backend' && STORAGE_LOCAL_PATH='$ROOT/data' .venv/bin/uvicorn app.main:app --port 8000" \
    >>"$ROOT/data/benchmark/api-overnight.log" 2>&1 &
  log "api pid $!"

  local ok=0
  for _ in $(seq 1 90); do
    if api_up; then ok=1; break; fi
    sleep 5
  done
  if [[ "$ok" -ne 1 ]]; then
    log "WARNING: API not responding; supplements may fail"
  else
    log "API up"
  fi

  nohup bash -c "cd '$ROOT/worker' && MODELS_DIR=models DEVICE=rocm .venv/bin/python -m worker" \
    >>"$ROOT/data/benchmark/worker-overnight.log" 2>&1 &
  log "worker pid $!"

  log "waiting for worker (up to 30 min for first model download) ..."
  for _ in $(seq 1 180); do
    if curl -sf --max-time 5 "$API/api/v1/benchmark/gpu" >/dev/null 2>&1; then
      log "worker connected"
      return 0
    fi
    sleep 10
  done
  log "WARNING: worker gpu endpoint not responding after 30 min; continuing"
}

run_bench() {
  local out_dir="$1"
  shift
  log "=== benchmark $* -> $out_dir ==="
  "$PYTHON" "$BENCH_SCRIPT" --out-dir "$out_dir" "$@" --continue-on-error --force >>"$LOG" 2>&1
  local rc=$?
  if [[ $rc -eq 0 ]]; then log "ok: $out_dir"; else log "WARNING: benchmark exit $rc for $out_dir"; fi
}

wait_for_file "$FULL_RUN" "full-run"
log "pausing 30s for GPU cleanup"
sleep 30
restart_stack

run_bench "$ROOT/data/benchmark/supplement-sdxl-fast" --models sdxl-fast

for model in sd-turbo sdxl-turbo krea-2-turbo; do
  run_bench "$ROOT/data/benchmark/supplement-$model" --models "$model"
done

log "=== merge ==="
MERGE_SOURCES=("$FULL_RUN" "$ROOT/data/benchmark/supplement-sdxl-fast/results.json")
for model in sd-turbo sdxl-turbo krea-2-turbo; do
  path="$ROOT/data/benchmark/supplement-$model/results.json"
  if [[ -f "$path" ]]; then
    MERGE_SOURCES+=("$path")
  else
    echo "skipping missing $path in merge"
  fi
done
"$PYTHON" "$MERGE_SCRIPT" "${MERGE_SOURCES[@]}" -o "$ROOT/data/benchmark/combined"

log "=== publish ==="
make benchmark-publish BENCHMARK_PUBLISH="$ROOT/data/benchmark/combined" >>"$LOG" 2>&1

log "=== supplement pipeline finished ==="
