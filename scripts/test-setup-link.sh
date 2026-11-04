#!/usr/bin/env bash
# `make setup-link` and `make dev-setup-link` (issue #792). The compose target mints
# inside the running compose API with the same compose invocation compose-up uses,
# and refuses when that API is not running. The dev target runs the host venv against
# the native dev database, with PUBLIC_URL from this checkout's API port unless one
# is exported. Docker and the venv's python are stubs, so this runs without either.
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
failures=0

fail_case() {
	printf 'FAIL %s %s\n' "$1" "${2:-}"
	failures=$((failures + 1))
}

link="http://studio.test:8080/setup#setup-token-from-the-stub-0123456789abcdefghij"

mkdir -p "$tmp/bin"
cat >"$tmp/bin/docker" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$DOCKER_LOG"
case "$*" in
	*" ps --status running -q api")
		[[ "${STUB_DAEMON_DOWN:-0}" == 1 ]] && exit 1
		[[ "${STUB_RUNNING:-0}" == 1 ]] && echo stub-api-container
		exit 0 ;;
	*" exec -T api python -m app.operator setup-link")
		[[ -n "${STUB_EXEC_MESSAGE:-}" ]] && echo "$STUB_EXEC_MESSAGE" >&2
		[[ -n "${STUB_LINK:-}" ]] && echo "$STUB_LINK"
		exit "${STUB_EXEC_STATUS:-0}" ;;
esac
exit 0
EOF
chmod +x "$tmp/bin/docker"

# The backend venv's python, as a stub: it records how it was run and its environment.
cat >"$tmp/venv-python" <<'EOF'
#!/usr/bin/env bash
printf 'argv=%s PUBLIC_URL=%s DATABASE_URL=%s\n' "$*" "${PUBLIC_URL:-}" "${DATABASE_URL:-}" >>"$PY_LOG"
echo "$STUB_LINK"
EOF
chmod +x "$tmp/venv-python"

# A tree with the Makefile, the helpers it reads at parse time, the compose file and
# the venv stub. git is initialised so a linked worktree of it has its own checkout id.
make_tree() {
	local root="$1"
	mkdir -p "$root/scripts" "$root/deploy/compose" "$root/backend/.venv/bin"
	cp "$repo/scripts/checkout-ports.sh" "$repo/scripts/checkout-id.sh" "$repo/scripts/gpu-profile.sh" "$root/scripts/"
	cp "$repo/Makefile" "$root/Makefile"
	cp "$repo/deploy/compose/compose.yml" "$root/deploy/compose/"
	cp "$tmp/venv-python" "$root/backend/.venv/bin/python"
	# git does not track empty directories, and the dev target changes into backend/.
	touch "$root/backend/.keep"
	(cd "$root" && git init -q . && git add -A &&
		git -c user.name=test -c user.email=test@example.invalid commit -qm init)
}

reset_logs() {
	: >"$tmp/docker.log"
	: >"$tmp/py.log"
}

ROOT="$tmp/root"
make_tree "$ROOT"
export DOCKER_LOG="$tmp/docker.log" PY_LOG="$tmp/py.log" STUB_LINK="$link"
export PATH="$tmp/bin:$PATH"
# A conflicting ambient project and a conflicting PUBLIC_URL in every case below.
export COMPOSE_PROJECT_NAME=ambient-other-project

# The prefix compose-up uses, so the setup target can be checked against it.
compose_up_prefix="$(make -n -C "$ROOT" compose-up | grep 'up -d --build' | tr -d '"' | sed 's/ up -d --build$//')"
expected_exec="${compose_up_prefix#docker } exec -T api python -m app.operator setup-link"

# 1. The compose install is running: the link comes from the API container, through
# exactly the invocation compose-up uses.
reset_logs
out="$(env -u PUBLIC_URL STUB_RUNNING=1 make -s -C "$ROOT" setup-link 2>"$tmp/err")"
status=$?
if [[ $status -ne 0 || "$(printf '%s\n' "$out" | tail -n 1)" != "$link" ]]; then
	fail_case compose-running-prints-the-container-link "exit $status, stdout: $out; stderr: $(cat "$tmp/err")"
elif ! grep -qxF "$expected_exec" "$tmp/docker.log"; then
	fail_case compose-running-uses-the-compose-up-invocation "expected: $expected_exec; calls: $(cat "$tmp/docker.log")"
elif [[ -s "$tmp/py.log" ]]; then
	fail_case compose-target-never-uses-the-host-venv "the venv ran"
else
	printf 'ok compose-running-execs-with-the-compose-up-invocation\n'
fi

# 2. The compose API is stopped: refuse, name make compose-up, and mint nothing.
reset_logs
out="$(env -u PUBLIC_URL STUB_RUNNING=0 make -s -C "$ROOT" setup-link 2>"$tmp/err")"
status=$?
if [[ $status -eq 0 || -n "$out" ]]; then
	fail_case stopped-api-is-refused "exit $status, stdout: $out"
elif ! grep -q "make compose-up" "$tmp/err"; then
	fail_case stopped-api-names-make-compose-up "stderr was: $(cat "$tmp/err")"
elif grep -q "exec" "$tmp/docker.log" || [[ -s "$tmp/py.log" ]]; then
	fail_case stopped-api-mints-nothing "calls: $(cat "$tmp/docker.log"); py: $(cat "$tmp/py.log")"
else
	printf 'ok stopped-api-is-refused-and-names-make-compose-up\n'
fi

# 3. Docker cannot report the service: refuse, and say so.
reset_logs
out="$(env -u PUBLIC_URL STUB_DAEMON_DOWN=1 make -s -C "$ROOT" setup-link 2>"$tmp/err")"
status=$?
if [[ $status -eq 0 || -n "$out" ]] || ! grep -q "no link was made" "$tmp/err" || grep -q "exec" "$tmp/docker.log"; then
	fail_case docker-down-refuses "exit $status, stdout: $out; stderr: $(cat "$tmp/err")"
else
	printf 'ok docker-down-refuses-without-a-link\n'
fi

# 4. The install already chose: the message reaches the user, and no link is printed.
reset_logs
out="$(env -u PUBLIC_URL STUB_RUNNING=1 STUB_LINK= STUB_EXEC_STATUS=3 \
	STUB_EXEC_MESSAGE='installation already claimed' make -s -C "$ROOT" setup-link 2>"$tmp/err")"
status=$?
if [[ $status -eq 0 || "$out" == *setup* ]] || ! grep -q "installation already claimed" "$tmp/err"; then
	fail_case already-decided-shows-the-message "exit $status, stdout: $out; stderr: $(cat "$tmp/err")"
else
	printf 'ok already-decided-shows-the-message-and-no-link\n'
fi

# 5. The native dev stack: the host venv, the checkout's dev database, and the default
# PUBLIC_URL on this checkout's API port. Docker is not consulted at all.
reset_logs
api_port="$(cd "$ROOT" && bash scripts/checkout-ports.sh | sed -n 's/^API_PORT=//p')"
out="$(env -u DATABASE_URL -u PUBLIC_URL STUB_LINK="$link" make -s -C "$ROOT" dev-setup-link 2>"$tmp/err")"
status=$?
if [[ $status -ne 0 || "$(printf '%s\n' "$out" | tail -n 1)" != "$link" ]]; then
	fail_case dev-prints-the-link "exit $status, stdout: $out; stderr: $(cat "$tmp/err")"
elif [[ -s "$tmp/docker.log" ]]; then
	fail_case dev-does-not-consult-docker "docker calls: $(cat "$tmp/docker.log")"
elif ! grep -qxF "argv=-m app.operator setup-link PUBLIC_URL=http://localhost:${api_port} DATABASE_URL=postgresql://potocolom:potocolom@localhost:5432/potocolom" "$tmp/py.log"; then
	fail_case dev-uses-the-native-url-and-database "py log: $(cat "$tmp/py.log")"
else
	printf 'ok dev-uses-the-checkout-port-url-and-dev-database\n'
fi

# 6. An exported PUBLIC_URL wins over the checkout's default.
reset_logs
out="$(env -u DATABASE_URL PUBLIC_URL=http://native.test:9000 make -s -C "$ROOT" dev-setup-link 2>"$tmp/err")"
if [[ "$(printf '%s\n' "$out" | tail -n 1)" != "$link" ]] \
	|| ! grep -qxF "argv=-m app.operator setup-link PUBLIC_URL=http://native.test:9000 DATABASE_URL=postgresql://potocolom:potocolom@localhost:5432/potocolom" "$tmp/py.log"; then
	fail_case dev-keeps-an-exported-public-url "stdout: $out; py log: $(cat "$tmp/py.log")"
else
	printf 'ok dev-keeps-an-exported-public-url\n'
fi

# 7. A linked worktree: its own database suffix and its own API port.
linked="$tmp/linked"
(cd "$ROOT" && git worktree add -q "$linked" -b linked-setup-link-test)
suffix="$(cd "$linked" && bash scripts/checkout-ports.sh | sed -n 's/^DB_SUFFIX=//p')"
linked_port="$(cd "$linked" && bash scripts/checkout-ports.sh | sed -n 's/^API_PORT=//p')"
reset_logs
out="$(env -u DATABASE_URL -u PUBLIC_URL STUB_LINK="$link" make -s -C "$linked" dev-setup-link 2>"$tmp/err")"
status=$?
if [[ -z "$suffix" ]]; then
	fail_case linked-worktree-has-a-suffix "checkout-ports.sh gave no DB_SUFFIX"
elif [[ $status -ne 0 || "$(printf '%s\n' "$out" | tail -n 1)" != "$link" ]]; then
	fail_case linked-dev-prints-the-link "exit $status, stdout: $out; stderr: $(cat "$tmp/err")"
elif ! grep -qxF "argv=-m app.operator setup-link PUBLIC_URL=http://localhost:${linked_port} DATABASE_URL=postgresql://potocolom:potocolom@localhost:5432/potocolom${suffix}" "$tmp/py.log"; then
	fail_case linked-dev-uses-its-own-port-and-database "expected suffix $suffix, port $linked_port; py log: $(cat "$tmp/py.log")"
else
	printf 'ok linked-worktree-dev-uses-its-own-port-and-database\n'
fi

# 8. The first-run pair: make api FIRST_RUN=1 leaves the mode to the database, and a
# plain make api keeps the native default of none. dev-setup-link then mints for the
# same database and port.
first_run="$(make -n -C "$ROOT" api FIRST_RUN=1 2>/dev/null | grep 'uvicorn app.main:app')"
plain="$(make -n -C "$ROOT" api 2>/dev/null | grep 'uvicorn app.main:app')"
if [[ "$first_run" != *'AUTH_MODE= BENCHMARK_API=1'* ]]; then
	fail_case first-run-leaves-the-mode-to-the-database "$first_run"
elif [[ "$plain" != *'AUTH_MODE="${AUTH_MODE:-none}" BENCHMARK_API=1'* ]]; then
	fail_case plain-api-keeps-the-none-default "$plain"
else
	printf 'ok first-run-api-leaves-the-mode-to-the-database-and-plain-api-keeps-none\n'
fi

if ((failures > 0)); then
	printf '%d failure(s)\n' "$failures" >&2
	exit 1
fi
printf 'setup link targets ok\n'
