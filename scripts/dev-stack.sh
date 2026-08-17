#!/usr/bin/env bash
# Background API + studio + worker with reliable stop/start.
# Workers do not listen on a TCP port, so port fuser alone cannot reap them;
# this script also path-matches this repo's venv binaries.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEV_DIR="${DEV_DIR:-$REPO/data/dev}"
API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-5173}"
WORKER="${WORKER:-rocm}"
DATABASE_URL="${DATABASE_URL:-postgresql://potocolom:potocolom@localhost:5432/potocolom}"

mkdir -p "$DEV_DIR"

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
	echo "  WORKER=rocm|cuda|sim|off (default rocm)" >&2
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
