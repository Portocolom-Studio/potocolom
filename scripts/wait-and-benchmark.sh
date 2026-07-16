#!/usr/bin/env bash
# Wait for GPU headroom, then run the full benchmark matrix and publish results.
# Usage: WAIT_SEC=3600 ./scripts/wait-and-benchmark.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

WAIT_SEC="${WAIT_SEC:-3600}"
POLL_SEC="${POLL_SEC:-120}"
MAX_IDLE_WAIT_SEC="${MAX_IDLE_WAIT_SEC:-7200}"
GPU_USE_MAX="${GPU_USE_MAX:-20}"
LOG_DIR="$ROOT/data/benchmark"
LOG_FILE="$LOG_DIR/scheduled-run.log"

mkdir -p "$LOG_DIR"

log() {
	echo "$(date -Is) $*" | tee -a "$LOG_FILE"
}

gpu_use_percent() {
	if command -v rocm-smi >/dev/null 2>&1; then
		rocm-smi --showuse 2>/dev/null | rg -o '[0-9]+ %' | head -1 | tr -d ' %' || echo 100
	elif command -v nvidia-smi >/dev/null 2>&1; then
		nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ' || echo 100
	else
		echo 0
	fi
}

wait_for_idle_gpu() {
	local elapsed=0
	while [ "$elapsed" -lt "$MAX_IDLE_WAIT_SEC" ]; do
		local use
		use="$(gpu_use_percent)"
		log "GPU use ${use}% (threshold ${GPU_USE_MAX}%)"
		if [ "${use:-100}" -le "$GPU_USE_MAX" ]; then
			return 0
		fi
		sleep "$POLL_SEC"
		elapsed=$((elapsed + POLL_SEC))
	done
	return 1
}

log "scheduled benchmark: initial wait ${WAIT_SEC}s"
sleep "$WAIT_SEC"

log "waiting for GPU to go idle"
if ! wait_for_idle_gpu; then
	log "GPU still busy after ${MAX_IDLE_WAIT_SEC}s; aborting scheduled benchmark"
	exit 1
fi

log "starting dev stack"
make stack-up WORKER=rocm >>"$LOG_FILE" 2>&1 || make dev-start WORKER=rocm >>"$LOG_FILE" 2>&1

log "waiting for API"
for _ in $(seq 1 60); do
	if curl -sf http://127.0.0.1:8000/api/v1/models >/dev/null; then
		break
	fi
	sleep 5
done

STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT_DIR="$ROOT/data/benchmark/$STAMP"
log "running full benchmark -> $OUT_DIR"

set +e
BENCHMARK_OUT="$OUT_DIR" BENCHMARK_INCLUDE_CAPPED=1 BENCHMARK_CONTINUE=1 make benchmark >>"$LOG_FILE" 2>&1
bench_status=$?
set -e

if [ "$bench_status" -ne 0 ]; then
	log "benchmark failed with status $bench_status"
	exit "$bench_status"
fi

BENCHMARK_PUBLISH="$OUT_DIR" make benchmark-publish >>"$LOG_FILE" 2>&1
log "benchmark complete and published from $OUT_DIR"
