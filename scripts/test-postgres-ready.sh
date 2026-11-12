#!/usr/bin/env bash
# Prove that the postgres healthcheck in deploy/compose/compose.yml does not
# pass while the first-start temporary server runs. On a new data volume the
# official image runs that server on the unix socket only, to run its init
# scripts, so a healthcheck that connects over the socket passes against it.
# The image, the user and the healthcheck command are read from compose.yml,
# so the test runs whatever the file says. Issue #882.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="$ROOT/deploy/compose/compose.yml"
NAME="potocolom-pgready-${CI_RUN_TAG:-$$}"
WORK="$(mktemp -d)"
MARKER=/tmp/potocolom-pgready-init
CHECKS=0
FAILURES=0

pass() {
	CHECKS=$((CHECKS + 1))
	echo "PASS: $1"
}

fail() {
	CHECKS=$((CHECKS + 1))
	FAILURES=$((FAILURES + 1))
	echo "FAIL: $1"
}

abort() {
	echo "FAIL: $1" >&2
	if [[ -n "${2:-}" ]]; then printf '%s\n' "$2" >&2; fi
	exit 1
}

cleanup() {
	status=$?
	trap - EXIT
	docker rm -f "$NAME" >/dev/null 2>&1 || true
	rm -rf "$WORK"
	exit "$status"
}
trap cleanup EXIT

command -v docker >/dev/null 2>&1 || abort "docker is not installed"

# The postgres service as compose.yml resolves it: image, user, database and
# the healthcheck command, which docker runs with /bin/sh -c.
if ! SPEC="$(docker compose -f "$COMPOSE_FILE" config --format json 2>"$WORK/config.err" \
	| python3 -c '
import json, sys
svc = json.load(sys.stdin)["services"]["postgres"]
test = svc["healthcheck"]["test"]
if len(test) != 2 or test[0] != "CMD-SHELL":
    sys.exit("the postgres healthcheck is not a CMD-SHELL string")
env = svc["environment"]
print("\t".join([svc["image"], env["POSTGRES_USER"], env["POSTGRES_DB"], test[1]]))')"; then
	cat "$WORK/config.err" >&2
	abort "could not read the postgres service from $COMPOSE_FILE"
fi
IFS=$'\t' read -r IMAGE PG_USER PG_DB HEALTH <<<"$SPEC"
echo "image: $IMAGE"
echo "healthcheck: $HEALTH"

# The init script runs while the temporary server is up. It marks the window
# on entry and clears the mark on exit, so the checks below can tell they ran
# inside it.
mkdir "$WORK/initdb"
chmod 755 "$WORK/initdb"
cat >"$WORK/initdb/01-hold.sh" <<SH
#!/bin/sh
touch "$MARKER"
sleep 20
rm -f "$MARKER"
SH
chmod 755 "$WORK/initdb/01-hold.sh"

if ! docker run -d --name "$NAME" \
	-e POSTGRES_USER="$PG_USER" -e POSTGRES_PASSWORD=pgready-test-password \
	-e POSTGRES_DB="$PG_DB" \
	-v "$WORK/initdb:/docker-entrypoint-initdb.d:ro" \
	"$IMAGE" >/dev/null; then
	abort "could not start $IMAGE"
fi

in_window() { docker exec "$NAME" test -f "$MARKER" >/dev/null 2>&1; }
health_passes() { docker exec "$NAME" /bin/sh -c "$HEALTH" >/dev/null 2>&1; }

reached=0
for _ in $(seq 1 60); do
	if in_window; then reached=1; break; fi
	sleep 1
done
[[ "$reached" == 1 ]] || abort "did not reach the window: the init script never started"

# The socket-only check must pass here. If it does not, the temporary server
# is not up and the healthcheck result below would prove nothing.
if docker exec "$NAME" pg_isready -U "$PG_USER" >/dev/null 2>&1 && in_window; then
	pass "socket-only pg_isready passes while only the temporary server runs"
else
	abort "did not reach the window: socket-only pg_isready does not pass during init"
fi

if health_passes; then
	fail "the healthcheck passes while only the temporary server runs"
else
	pass "the healthcheck fails while only the temporary server runs"
fi
in_window || abort "did not reach the window: init ended while the healthcheck ran"

healthy=0
for _ in $(seq 1 120); do
	if health_passes; then healthy=1; break; fi
	sleep 1
done
if [[ "$healthy" == 1 ]]; then
	pass "the healthcheck passes once the real server accepts connections"
else
	fail "the healthcheck never passed after init"
fi

echo
if ((FAILURES > 0)); then
	echo "postgres readiness test FAILED: $FAILURES of $CHECKS checks did not pass"
	exit 1
fi
echo "postgres readiness test passed: $CHECKS checks"
