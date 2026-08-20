#!/usr/bin/env bash
# Background API + studio + worker with reliable stop/start.
# Workers do not listen on a TCP port, so port fuser alone cannot reap them;
# this script also path-matches this repo's venv binaries.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEV_DIR="${DEV_DIR:-$REPO/data/dev}"
API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-5173}"
# Default to whatever this machine can actually run. A hardcoded default sends
# every contributor without that vendor's GPU into a worker that cannot start.
# /dev/kfd is the AMD compute device, absent on display-only amdgpu; NVIDIA
# requires nvidia-smi to actually enumerate a GPU, not just driver files:
# a laptop dGPU idles in D3cold with its modules unloaded, so lsmod is
# silent on a working install, but nvidia-smi must still report a device.
detect_worker() {
	if [[ -e /dev/kfd ]]; then
		echo rocm
	elif nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1; then
		echo cuda
	else
		echo sim
	fi
}
WORKER="${WORKER:-$(detect_worker)}"
DATABASE_URL="${DATABASE_URL:-postgresql://potocolom:potocolom@localhost:5432/potocolom}"

mkdir -p "$DEV_DIR"

dump_logs() {
	local name f
	for name in api web worker; do
		f="$DEV_DIR/${name}.log"
		[[ -f "$f" ]] || continue
		echo "----- $f -----" >&2
		tail -n 40 "$f" >&2 || true
	done
}

check_prereqs() {
	if [[ ! -x "$REPO/backend/.venv/bin/uvicorn" ]]; then
		echo "error: backend/.venv is missing uvicorn; run make setup" >&2
		exit 1
	fi
	if [[ ! -d "$REPO/frontend/node_modules" ]]; then
		echo "error: frontend/node_modules is missing; run make setup" >&2
		exit 1
	fi
	if [[ "$WORKER" != "off" ]]; then
		if ! "$REPO/worker/.venv/bin/python" -c 'import httpx' >/dev/null 2>&1; then
			echo "error: worker/.venv is missing packages; run make setup" >&2
			exit 1
		fi
	fi
	if [[ "$WORKER" == "cuda" || "$WORKER" == "rocm" ]]; then
		if ! "$REPO/worker/.venv/bin/python" -c 'import torch' >/dev/null 2>&1; then
			echo "error: worker/.venv cannot import torch; run make setup-$WORKER" >&2
			echo "  or start the simulated worker: make dev-start WORKER=sim" >&2
			exit 1
		fi
	fi
}

wait_http() {
	local url="$1" name="$2"
	local i=0
	while ((i < 60)); do
		if curl -sf -m 1 "$url" >/dev/null 2>&1; then
			return 0
		fi
		sleep 0.5
		i=$((i + 1))
	done
	echo "error: $name did not become ready at $url" >&2
	return 1
}

# Any HTTP status means the server accepted a TCP connection. Used for Vite,
# whose first compile can take a while and which may 404 / meanwhile.
wait_listening() {
	local url="$1" name="$2"
	local i=0 code
	while ((i < 60)); do
		code="$(curl -s -m 1 -o /dev/null -w '%{http_code}' "$url" || true)"
		if [[ "$code" =~ ^[1-5][0-9][0-9]$ ]]; then
			return 0
		fi
		sleep 0.5
		i=$((i + 1))
	done
	echo "error: $name did not become ready at $url" >&2
	return 1
}

kill_pidfile() {
	local pidfile="$1"
	[[ -f "$pidfile" ]] || return 0
	local pid
	pid="$(tr -d '[:space:]' <"$pidfile" || true)"
	rm -f "$pidfile"
	[[ -n "${pid:-}" ]] || return 0
	if kill -0 "$pid" 2>/dev/null; then
		# Children first (npm -> vite), then the recorded pid.
		pkill -P "$pid" 2>/dev/null || true
		kill "$pid" 2>/dev/null || true
		sleep 0.2
		kill -9 "$pid" 2>/dev/null || true
	fi
}

# Reap leftovers even when pid files are wrong or missing.
kill_repo_procs() {
	# Exact app.main uvicorn for this checkout (absolute or relative venv path).
	# pkill -f treats the pattern as an ERE: escape dots so path segments and
	# "app.main" cannot match unintended command lines.
	local repo_ere api_py api_py_ere pid cwd
	repo_ere="$(printf '%s' "$REPO" | sed 's/\./\\./g')"
	pkill -f "${repo_ere}/backend/\\.venv/bin/.*uvicorn app\\.main:app" 2>/dev/null || true
	api_py="$REPO/backend/.venv/bin/python"
	if [[ -x "$api_py" ]]; then
		api_py_ere="$(printf '%s' "$api_py" | sed 's/\./\\./g')"
		pkill -f "${api_py_ere} \\.venv/bin/uvicorn app\\.main:app" 2>/dev/null || true
	fi
	# start_one launches `exec .venv/bin/uvicorn` from backend/ (relative argv0).
	for pid in $(pgrep -f '\\.venv/bin/uvicorn app\\.main:app' 2>/dev/null || true); do
		cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
		[[ "$cwd" == "$REPO/backend" ]] || continue
		kill "$pid" 2>/dev/null || true
		sleep 0.1
		kill -9 "$pid" 2>/dev/null || true
	done
	# Workers have no listen port. Match by /proc/pid/cwd so we never kill a
	# worker from another checkout or `python -m worker.illusion`.
	for pid in $(pgrep -f 'python -m worker' 2>/dev/null || true); do
		cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
		[[ "$cwd" == "$REPO/worker" ]] || continue
		# argv must end with "-m worker", not "-m worker.something".
		tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -Eq ' -m worker( |$)' || continue
		tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -Eq ' -m worker\.' && continue
		kill "$pid" 2>/dev/null || true
		sleep 0.1
		kill -9 "$pid" 2>/dev/null || true
	done
}

cmd_stop() {
	kill_pidfile "$DEV_DIR/api.pid"
	kill_pidfile "$DEV_DIR/web.pid"
	kill_pidfile "$DEV_DIR/worker.pid"
	kill_repo_procs
	fuser -k "${API_PORT}/tcp" 2>/dev/null || true
	fuser -k "${WEB_PORT}/tcp" 2>/dev/null || true
	# Keep worker.lock: flock is tied to the inode. Unlinking while a holder is
	# still alive would let a new worker create a fresh inode and take a second
	# lock. The flock itself drops when the process exits.
	rm -f "$DEV_DIR/api.pid" "$DEV_DIR/web.pid" "$DEV_DIR/worker.pid"
}

start_one() {
	local name="$1" pidfile="$2"
	shift 2
	# Studio workers must reach the Hub (or at least local cache metadata).
	# Illusion/campaign shells often export HF_HUB_OFFLINE=1; inheriting that
	# makes every from_pretrained fail with "not cached locally".
	# setsid -f starts a new session so Cursor/agent cgroup teardown does not
	# reap the stack when the shell that ran make dev-start exits. The child
	# writes its own pid after the session fork (setsid -f does not set $!).
	setsid -f env -u HF_HUB_OFFLINE -u TRANSFORMERS_OFFLINE -u HF_DATASETS_OFFLINE \
		bash -c "echo \$\$ >\"$pidfile\"; exec bash -c $(printf '%q' "$*")" \
		>>"$DEV_DIR/${name}.log" 2>&1
	# Wait briefly for the child to write the pidfile.
	local i=0
	while [[ ! -s "$pidfile" && $i -lt 50 ]]; do
		sleep 0.02
		i=$((i + 1))
	done
}

cmd_start() {
	case "$WORKER" in
	rocm | cuda | sim | off) ;;
	*)
		echo "Unknown WORKER=$WORKER; use rocm, cuda, sim, or off" >&2
		exit 1
		;;
	esac

	if [[ -f "$REPO/deploy/compose/.env" ]]; then
		set -a
		# shellcheck disable=SC1091
		. "$REPO/deploy/compose/.env"
		set +a
		if [[ -n "${FLEET_SECRET:-}" ]]; then
			export FLEET_TOKEN_KEY="${FLEET_TOKEN_KEY:-$FLEET_SECRET}"
			export FLEET_TOKEN="${FLEET_TOKEN:-$FLEET_SECRET}"
		fi
	fi
	if [[ -z "${FLEET_TOKEN_KEY:-}" ]]; then
		echo "error: FLEET_TOKEN_KEY is unset; run make init (fills FLEET_SECRET in deploy/compose/.env)" >&2
		exit 1
	fi

	check_prereqs

	cmd_stop
	: >"$DEV_DIR/api.log"
	: >"$DEV_DIR/web.log"
	: >"$DEV_DIR/worker.log"

	echo "Starting API on :$API_PORT..."
	start_one api "$DEV_DIR/api.pid" \
		"cd $(printf '%q' "$REPO/backend") && STORAGE_LOCAL_PATH=$(printf '%q' "$REPO/data") \
		ALLOWED_ORIGINS=\"http://localhost:$WEB_PORT\" \
		PUBLIC_URL=\"http://localhost:$API_PORT\" \
		DATABASE_URL=$(printf '%q' "$DATABASE_URL") \
		exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $API_PORT"

	echo "Starting frontend on :$WEB_PORT..."
	start_one web "$DEV_DIR/web.pid" \
		"cd $(printf '%q' "$REPO/frontend") && \
		exec npm run dev -- --host 127.0.0.1 --port $WEB_PORT"

	if [[ "$WORKER" == "rocm" || "$WORKER" == "cuda" ]]; then
		echo "Starting worker ($WORKER, MODELS_DIR=models)..."
		start_one worker "$DEV_DIR/worker.pid" \
			"cd $(printf '%q' "$REPO/worker") && MODELS_DIR=models DEVICE=$WORKER \
			API_URL=ws://127.0.0.1:$API_PORT/api/v1/fleet \
			WORKER_LOCK=$(printf '%q' "$DEV_DIR/worker.lock") \
			exec .venv/bin/python -m worker"
	elif [[ "$WORKER" == "sim" ]]; then
		echo "Starting worker (simulated engine)..."
		start_one worker "$DEV_DIR/worker.pid" \
			"cd $(printf '%q' "$REPO/worker") && \
			API_URL=ws://127.0.0.1:$API_PORT/api/v1/fleet \
			WORKER_LOCK=$(printf '%q' "$DEV_DIR/worker.lock") \
			exec .venv/bin/python -m worker"
	fi

	echo "API log:    $DEV_DIR/api.log"
	echo "Web log:    $DEV_DIR/web.log"
	if [[ "$WORKER" != "off" ]]; then
		echo "Worker log: $DEV_DIR/worker.log"
	fi

	local fail=0 pid i
	if ! wait_http "http://127.0.0.1:$API_PORT/api/v1/health" API; then
		fail=1
	fi
	if ! wait_listening "http://127.0.0.1:$WEB_PORT/" studio; then
		fail=1
	fi
	if [[ "$WORKER" != "off" ]]; then
		i=0
		pid="$(tr -d '[:space:]' <"$DEV_DIR/worker.pid" 2>/dev/null || true)"
		while ((i < 10)); do
			pid="$(tr -d '[:space:]' <"$DEV_DIR/worker.pid" 2>/dev/null || true)"
			if [[ -n "$pid" ]] && ps -p "$pid" >/dev/null 2>&1; then
				break
			fi
			sleep 0.2
			i=$((i + 1))
		done
		if [[ -z "$pid" ]] || ! ps -p "$pid" >/dev/null 2>&1; then
			echo "error: worker exited during startup" >&2
			fail=1
		fi
	fi
	if ((fail)); then
		dump_logs
		cmd_stop
		exit 1
	fi
	echo "API:        http://localhost:$API_PORT"
	echo "Studio:     http://localhost:$WEB_PORT"
}

cmd_status() {
	echo "pid files:"
	for name in api web worker; do
		local f="$DEV_DIR/${name}.pid"
		if [[ -f "$f" ]]; then
			local pid
			pid="$(tr -d '[:space:]' <"$f")"
			if [[ -n "$pid" ]] && ps -p "$pid" >/dev/null 2>&1; then
				echo "  $name: $pid (alive)"
			else
				echo "  $name: ${pid:-?} (dead)"
			fi
		else
			echo "  $name: (no pid file)"
		fi
	done
	echo "ports:"
	ss -ltn 2>/dev/null | grep -E ":($API_PORT|$WEB_PORT)\\s" || echo "  (none listening)"
	echo "repo workers:"
	local found=0 pid cwd
	for pid in $(pgrep -f 'python -m worker' 2>/dev/null || true); do
		cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
		[[ "$cwd" == "$REPO/worker" ]] || continue
		tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -Eq ' -m worker\.' && continue
		tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -Eq ' -m worker( |$)' || continue
		ps -p "$pid" -o pid=,cmd=
		found=1
	done
	[[ "$found" -eq 1 ]] || echo "  (none)"
	if curl -sf -m 2 "http://127.0.0.1:$API_PORT/api/v1/health" >/dev/null 2>&1; then
		echo -n "models: "
		curl -sf -m 2 "http://127.0.0.1:$API_PORT/api/v1/models" \
			| python3 -c "import json,sys; print(', '.join(sorted(m['id'] for m in json.load(sys.stdin))) or '(none)')" \
			2>/dev/null || echo "(unreachable)"
	else
		echo "API: down"
	fi
}

usage() {
	echo "Usage: $0 {start|stop|restart|status}" >&2
	echo "  WORKER=rocm|cuda|sim|off (detected: $(detect_worker))" >&2
	echo "  API_PORT WEB_PORT DEV_DIR optional" >&2
	exit 1
}

main() {
	local action="${1:-}"
	case "$action" in
	start) cmd_start ;;
	stop) cmd_stop ;;
	restart)
		cmd_stop
		cmd_start
		;;
	status) cmd_status ;;
	*) usage ;;
	esac
}

main "${1:-}"
