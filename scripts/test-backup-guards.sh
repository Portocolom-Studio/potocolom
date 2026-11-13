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

# Both scripts must pass docker compose -p the name Compose itself reads from
# the env file, for every line form Compose accepts. The fake docker has no
# Compose plugin, so the scripts take the no-Docker reader; each run logs the
# docker argv and the project is read back from that log.
fake_docker() {
	local bin="$1"
	mkdir -p "$bin"
	cat >"$bin/docker" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${DOCKER_LOG:?}"
case "$*" in
	"compose version") exit 1 ;;
	*"config --volumes")
		if [[ -n "${FAKE_CONFIG_FAILS:-}" ]]; then exit 1; fi
		printf '%s\n' pgdata assets models hf-cache ;;
	"volume inspect"*"${FAKE_EXISTING_VOLUME:-@none}") exit 0 ;;
	"volume inspect"*) exit 1 ;;
esac
exit 0
EOF
	chmod +x "$bin/docker"
}

project_passed_to_compose() {
	sed -n 's/^compose -p \(.*\) --env-file .*/\1/p' "$1" | head -n 1
}

backup_project_for() {
	local line="$1" dir
	dir="$(mktemp -d "$tmp/backup-project.XXXXXX")"
	fake_docker "$dir/bin"
	printf '%s\n' "$line" >"$dir/env"
	env -u COMPOSE_PROJECT_NAME PATH="$dir/bin:$PATH" DOCKER_LOG="$dir/docker.log" \
		ENV_FILE="$dir/env" COMPOSE_FILE="$repo/deploy/compose/compose.yml" \
		bash "$repo/scripts/backup.sh" "$dir/out" >/dev/null 2>&1
	project_passed_to_compose "$dir/docker.log"
}

restore_project_for() {
	local line="$1" dir backup name
	dir="$(mktemp -d "$tmp/restore-project.XXXXXX")"
	backup="$dir/backup"
	fake_docker "$dir/bin"
	mkdir -p "$backup"
	for name in database.dump assets.tar; do printf 'previous\n' >"$backup/$name"; done
	printf '%s\n' "$line" >"$backup/env"
	(cd "$backup" && sha256sum database.dump assets.tar env >SHA256SUMS)
	env -u COMPOSE_PROJECT_NAME PATH="$dir/bin:$PATH" DOCKER_LOG="$dir/docker.log" \
		COMPOSE_FILE="$repo/deploy/compose/compose.yml" \
		bash "$repo/scripts/restore.sh" "$backup" >/dev/null 2>&1
	project_passed_to_compose "$dir/docker.log"
}

# Refusal before anything is created: restore.sh must exit non-zero and never
# reach compose up. Arguments after the label are VAR=value pairs for the fake.
refuses_before_create() {
	local label="$1" dir backup rc=0
	shift
	dir="$(mktemp -d "$tmp/refuse.XXXXXX")"
	backup="$dir/backup"
	fake_docker "$dir/bin"
	mkdir -p "$backup"
	for name in database.dump assets.tar; do printf 'previous\n' >"$backup/$name"; done
	printf 'COMPOSE_PROJECT_NAME=guard-project\n' >"$backup/env"
	(cd "$backup" && sha256sum database.dump assets.tar env >SHA256SUMS)
	env -u COMPOSE_PROJECT_NAME PATH="$dir/bin:$PATH" DOCKER_LOG="$dir/docker.log" \
		COMPOSE_FILE="$repo/deploy/compose/compose.yml" "$@" \
		bash "$repo/scripts/restore.sh" "$backup" >/dev/null 2>&1 || rc=$?
	if [[ "$rc" -eq 0 ]] || grep -q -- ' up ' "$dir/docker.log"; then
		printf 'FAIL %s: restore.sh went on to create the project (exit %s)\n' "$label" "$rc"
		failed=1
		return
	fi
	printf 'ok %s\n' "$label"
}

# label | env file line | project Compose uses. The plain line is the control;
# the directory name fallback is "compose", so every other want differs from it.
forms=(
	'plain|COMPOSE_PROJECT_NAME=env-plain|env-plain'
	'export|export COMPOSE_PROJECT_NAME=env-export|env-export'
	'indented|  COMPOSE_PROJECT_NAME=env-indented|env-indented'
	'quoted|COMPOSE_PROJECT_NAME="env-quoted"|env-quoted'
)
failed=0
for form in "${forms[@]}"; do
	IFS='|' read -r label line want <<<"$form"
	for script in backup restore; do
		got="$(${script}_project_for "$line")"
		if [[ "$got" != "$want" ]]; then
			printf 'FAIL %s-project-%s: expected %s, docker got %s\n' "$script" "$label" "$want" "$got"
			failed=1
			continue
		fi
		printf 'ok %s-project-%s\n' "$script" "$label"
	done
done
refuses_before_create restore-refuses-models-only FAKE_EXISTING_VOLUME=_models
refuses_before_create restore-refuses-unreadable-volume-list FAKE_CONFIG_FAILS=1
exit "$failed"
