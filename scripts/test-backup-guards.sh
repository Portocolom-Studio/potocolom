#!/usr/bin/env bash
# A backup stopped by a signal must fail and leave the previous recovery
# point whole (a fake docker holds pg_dump open long enough to send TERM),
# and a restore must refuse to run when the Docker daemon does not answer.
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/out"
cat >"$tmp/bin/docker" <<'EOF'
#!/usr/bin/env bash
case "$*" in
	*pg_dump*) sleep 3 ;;
esac
exit 0
EOF
chmod +x "$tmp/bin/docker"
printf 'COMPOSE_PROJECT_NAME=signal-test\n' >"$tmp/env"
for name in database.dump assets.tar env; do printf 'previous\n' >"$tmp/out/$name"; done
(cd "$tmp/out" && sha256sum database.dump assets.tar env >SHA256SUMS)

PATH="$tmp/bin:$PATH" ENV_FILE="$tmp/env" COMPOSE_FILE="$repo/deploy/compose/compose.yml" \
	bash "$repo/scripts/backup.sh" "$tmp/out" >/dev/null 2>&1 &
pid=$!
sleep 1
kill -TERM "$pid"
wait "$pid"
rc=$?

if [[ "$rc" -ne 143 ]]; then
	printf 'FAIL backup-term-exit: expected 143, got %s\n' "$rc"
	exit 1
fi
if ! (cd "$tmp/out" && sha256sum -c --quiet SHA256SUMS) || compgen -G "$tmp/out/*.new" >/dev/null; then
	printf 'FAIL backup-term-keeps-previous\n'
	exit 1
fi
printf 'ok backup-term-exit\nok backup-term-keeps-previous\n'

mkdir -p "$tmp/nodaemon/bin" "$tmp/restore-in"
cat >"$tmp/nodaemon/bin/docker" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$1" >>"${DOCKER_LOG:?}"
[[ "$1" == info ]] && exit 1
exit 0
EOF
chmod +x "$tmp/nodaemon/bin/docker"
cp "$tmp/out/"* "$tmp/restore-in/"
: >"$tmp/docker.log"
rc=0
PATH="$tmp/nodaemon/bin:$PATH" DOCKER_LOG="$tmp/docker.log" COMPOSE_FILE="$repo/deploy/compose/compose.yml" \
	bash "$repo/scripts/restore.sh" "$tmp/restore-in" >/dev/null 2>&1 || rc=$?
# The daemon check must be the first and only docker call.
if [[ "$rc" -eq 0 || "$(cat "$tmp/docker.log")" != info ]]; then
	printf 'FAIL restore-refuses-without-daemon: exit %s, docker calls:\n' "$rc"
	cat "$tmp/docker.log"
	exit 1
fi
printf 'ok restore-refuses-without-daemon\n'
