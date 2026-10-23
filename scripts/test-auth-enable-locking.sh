#!/usr/bin/env bash
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
auth_source="${AUTH_SCRIPT:-$repo/scripts/auth-enable.sh}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
failures=0
REAL_MKTEMP="$(command -v mktemp)"
REAL_MV="$(command -v mv)"
REAL_AWK="$(command -v awk)"

fail_case() {
	printf 'FAIL %s\n%s\n' "$1" "${2:-}"
	failures=$((failures + 1))
}

new_case() {
	case_dir="$tmp/$1"
	mkdir -p "$case_dir/root/scripts" "$case_dir/root/backend" "$case_dir/root/deploy/compose" "$case_dir/bin"
	cp "$auth_source" "$case_dir/root/scripts/auth-enable.sh"
	cp "$repo/scripts/install-lock.sh" "$case_dir/root/scripts/install-lock.sh"
	cp "$repo/scripts/ensure-env.sh" "$case_dir/root/scripts/ensure-env.sh"
	cp "$repo/deploy/compose/.env.example" "$case_dir/root/deploy/compose/.env.example"
	cp "$repo/deploy/compose/compose.yml" "$case_dir/root/deploy/compose/compose.yml"
	cat >"$case_dir/bin/docker" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == compose && "$2" == version ]]; then exit 0; fi
if [[ "$1" == compose ]]; then
	shift
	project=''
	while [[ "$1" == -p || "$1" == --env-file || "$1" == -f ]]; do
		if [[ "$1" == -p ]]; then project="$2"; fi
		shift 2
	done
	if [[ "$1" == config && "$2" == --environment ]]; then
		[[ "${COMPOSE_FAIL:-0}" != 1 ]] || exit 1
		cat "${COMPOSE_VALUES:?}"
		exit 0
	fi
	if [[ "$1" == ps ]]; then printf '%s\n' "$project" >"${DOCKER_PROJECT:?}"; exit 0; fi
	if [[ "$1" == run ]]; then exit 0; fi
fi
if [[ "$1" == info ]]; then exit 0; fi
exit 1
EOF
	cat >"$case_dir/bin/python" <<'EOF'
#!/usr/bin/env bash
if [[ ! -e "${ENABLE_STARTED:?}" ]]; then touch "$ENABLE_STARTED"; else touch "$ENABLE_STARTED.again"; fi
printf '%s' "${ROOT_KEYS:-}" >"${ENABLE_ROOT_KEYS:?}"
printf '%s' "${PUBLIC_URL:-}" >"${ENABLE_PUBLIC_URL:?}"
printf '%s' "${DATABASE_URL:-}" >"${ENABLE_DATABASE_URL:?}"
deadline=$((SECONDS + 10))
while [[ "${ENABLE_BLOCK:-0}" == 1 && ! -e "${ENABLE_STARTED}.again" && ! -e "${ENABLE_RELEASE:?}" ]]; do
	((SECONDS < deadline)) || exit 124
	sleep 0.02
done
touch "${ENABLE_FINISHED:?}"
EOF
	cat >"$case_dir/bin/mktemp" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$1" >>"${MKTEMP_LOG:?}"
exec "$REAL_MKTEMP" "$@"
EOF
	cat >"$case_dir/bin/mv" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" != -f ]] || shift
[[ "${1:-}" != -- ]] || shift
[[ "${AUTH_WRITE_FAIL:-}" != mv ]] || exit 1
source_file="$1"
destination="$2"
printf '%s|%s\n' "$(dirname "$source_file")" "$(stat -c '%a' "$source_file")" >>"${MV_LOG:?}"
exec "$REAL_MV" -- "$source_file" "$destination"
EOF
	cat >"$case_dir/bin/awk" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == -v && "${2:-}" == k=* ]]; then
	[[ "${AUTH_WRITE_FAIL:-}" != awk ]] || exit 1
	[[ "$(stat -Lc '%a' "/proc/$$/fd/1")" == 600 ]] || exit 1
fi
exec "$REAL_AWK" "$@"
EOF
	chmod +x "$case_dir/bin/"*
	cat >"$case_dir/root/deploy/compose/.env" <<'EOF'
POSTGRES_PASSWORD=keep-db
FLEET_SECRET=keep-fleet
DATABASE_URL=postgresql://potocolom:pw@postgres/potocolom
PUBLIC_URL=http://localhost:8080
AUTH_MODE=none
EOF
	cat >"$case_dir/values" <<'EOF'
COMPOSE_PROJECT_NAME=potocolom
ROOT_KEYS=
PUBLIC_URL=http://localhost:8080
DATABASE_URL=postgresql://potocolom:pw@postgres/potocolom
POSTGRES_PASSWORD=keep-db
FLEET_SECRET=keep-fleet
EOF
	: >"$case_dir/mktemp.log"
	: >"$case_dir/mv.log"
	case_env=(PATH="$case_dir/bin:$PATH" REAL_MKTEMP="$REAL_MKTEMP" REAL_MV="$REAL_MV" REAL_AWK="$REAL_AWK"
		ENV_FILE="$case_dir/root/deploy/compose/.env" COMPOSE_FILE="$case_dir/root/deploy/compose/compose.yml"
		ENV_EXAMPLE="$case_dir/root/deploy/compose/.env.example" COMPOSE_VALUES="$case_dir/values"
		DOCKER_PROJECT="$case_dir/docker-project"
		PY="$case_dir/bin/python" ENABLE_STARTED="$case_dir/enable-started" ENABLE_FINISHED="$case_dir/enable-finished"
		ENABLE_ROOT_KEYS="$case_dir/enable-root-keys" ENABLE_PUBLIC_URL="$case_dir/enable-public-url"
		ENABLE_DATABASE_URL="$case_dir/enable-database-url" ENABLE_RELEASE="$case_dir/enable-release"
		MKTEMP_LOG="$case_dir/mktemp.log" MV_LOG="$case_dir/mv.log")
}

run_auth() {
	env "${case_env[@]}" bash "$case_dir/root/scripts/auth-enable.sh" </dev/null
}

wait_for_file() {
	local path="$1"; local i
	for ((i = 0; i < 200; i++)); do [[ -e "$path" ]] && return 0; sleep 0.02; done
	return 1
}

start_holder() {
	local root="$1" env_file="$2" project="$3" ready="$4" release="$5"
	INSTALL_LOCK_HELPER="$case_dir/root/scripts/install-lock.sh" \
		bash -c '
		source "$INSTALL_LOCK_HELPER"
		install_acquire_locks "$1" "$2" "$3" "$4"
		unset POTOCOLOM_PATH_LOCK_FD POTOCOLOM_PATH_LOCK_KEY POTOCOLOM_PROJECT_LOCK_FD POTOCOLOM_PROJECT_LOCK_KEY
		env -u POTOCOLOM_PATH_LOCK_FD -u POTOCOLOM_PATH_LOCK_KEY -u POTOCOLOM_PROJECT_LOCK_FD -u POTOCOLOM_PROJECT_LOCK_KEY touch "$5"
		deadline=$((SECONDS + 10))
		while [[ ! -e "$6" ]]; do
			((SECONDS < deadline)) || exit 124
			sleep 0.02
		done
	' _ "$root" "$env_file" "$case_dir/root/deploy/compose/compose.yml" "$project" "$ready" "$release" >"$case_dir/holder.out" 2>&1 &
	holder_pid=$!
	if ! wait_for_file "$ready"; then
		touch "$release"
		wait "$holder_pid" || true
		return 1
	fi
}

expect_auth_refused() {
	local label="$1"
	if run_auth >"$case_dir/auth.out" 2>&1; then
		fail_case "$label" 'auth-enable succeeded while an installer lock was held'
	elif cmp -s "$case_dir/before.env" "$case_dir/root/deploy/compose/.env" && [[ ! -e "$case_dir/enable-started" ]]; then
		printf 'ok %s\n' "$label"
	else
		fail_case "$label" 'the environment changed or app.enable ran'
	fi
}

new_case directory-lock
printf 'COMPOSE_PROJECT_NAME=different-project\n' >>"$case_dir/values"
cp "$case_dir/root/deploy/compose/.env" "$case_dir/before.env"
if start_holder "$case_dir/root" "$case_dir/root/deploy/compose/.env" held-project "$case_dir/ready" "$case_dir/release"; then
	expect_auth_refused directory-lock-refused-before-change
	touch "$case_dir/release"
	wait "$holder_pid" || true
else fail_case directory-lock 'lock holder did not start'; fi

new_case project-lock
mkdir -p "$case_dir/other"
cp "$case_dir/root/deploy/compose/.env" "$case_dir/other/.env"
printf 'COMPOSE_PROJECT_NAME=shared-project\n' >>"$case_dir/other/.env"
printf 'COMPOSE_PROJECT_NAME=env-project\n' >>"$case_dir/root/deploy/compose/.env"
printf 'COMPOSE_PROJECT_NAME=env-project\n' >>"$case_dir/values"
case_env+=(COMPOSE_PROJECT_NAME=shared-project)
cp "$case_dir/root/deploy/compose/.env" "$case_dir/before.env"
if start_holder "$case_dir/other" "$case_dir/other/.env" shared-project "$case_dir/ready" "$case_dir/release"; then
	expect_auth_refused shared-project-lock-across-installations
	touch "$case_dir/release"
	wait "$holder_pid" || true
else fail_case project-lock 'lock holder did not start'; fi

new_case blocked-enable
sed -i '/^ROOT_KEYS=/s/$/existing-key-ring/' "$case_dir/values"
printf 'ROOT_KEYS=existing-key-ring\n' >>"$case_dir/root/deploy/compose/.env"
case_env+=(ENABLE_BLOCK=1)
env "${case_env[@]}" bash "$case_dir/root/scripts/auth-enable.sh" </dev/null >"$case_dir/first.out" 2>&1 &
first_pid=$!
if wait_for_file "$case_dir/enable-started"; then
	cp "$case_dir/root/deploy/compose/.env" "$case_dir/before.env"
	if run_auth >"$case_dir/second.out" 2>&1; then
		fail_case lock-held-through-enable 'second auth-enable succeeded while app.enable was blocked'
	elif cmp -s "$case_dir/before.env" "$case_dir/root/deploy/compose/.env" && [[ "$(grep -c '^AUTH_MODE=accounts$' "$case_dir/root/deploy/compose/.env")" -eq 0 ]]; then
		printf 'ok lock-held-through-enable-and-final-write\n'
	else fail_case lock-held-through-enable 'environment changed before app.enable completed'; fi
	touch "$case_dir/enable-release"
	if wait "$first_pid" && [[ "$(grep -c '^AUTH_MODE=accounts$' "$case_dir/root/deploy/compose/.env")" -eq 1 ]]; then
		:
	else fail_case lock-held-through-enable 'first auth-enable did not write accounts mode'; fi
else
	touch "$case_dir/enable-release"
	wait "$first_pid" || true
	fail_case lock-held-through-enable 'app.enable did not start'
fi

new_case ensure-env-contention
sed -i '/^FLEET_SECRET=/d' "$case_dir/root/deploy/compose/.env"
sed -i '/^FLEET_SECRET=/d' "$case_dir/values"
sed -i '/^ROOT_KEYS=/s/$/existing-key-ring/' "$case_dir/values"
printf 'ROOT_KEYS=existing-key-ring\n' >>"$case_dir/root/deploy/compose/.env"
case_env+=(ENABLE_BLOCK=1)
env "${case_env[@]}" bash "$case_dir/root/scripts/auth-enable.sh" </dev/null >"$case_dir/auth.out" 2>&1 &
first_pid=$!
if wait_for_file "$case_dir/enable-started"; then
	cp "$case_dir/root/deploy/compose/.env" "$case_dir/before.env"
	if env "${case_env[@]}" bash "$case_dir/root/scripts/ensure-env.sh" >"$case_dir/ensure.out" 2>&1; then
		fail_case auth-versus-ensure-env 'ensure-env succeeded while auth-enable held the install locks'
	elif cmp -s "$case_dir/before.env" "$case_dir/root/deploy/compose/.env"; then
		printf 'ok auth-versus-ensure-env-contention\n'
	else fail_case auth-versus-ensure-env 'ensure-env changed the environment'; fi
	touch "$case_dir/enable-release"
	wait "$first_pid" || fail_case auth-versus-ensure-env 'auth-enable failed after ensure-env contention'
else
	touch "$case_dir/enable-release"
	wait "$first_pid" || true
	fail_case auth-versus-ensure-env 'app.enable did not start'
fi

for format in canonical export-quoted; do
	new_case "root-key-$format"
	if [[ "$format" == canonical ]]; then
		root_line='ROOT_KEYS=canonical-ring'
		root_value=canonical-ring
		expected_project=potocolom
	else
		root_line='export ROOT_KEYS = "quoted-ring" # operator comment'
		root_value=quoted-ring
		expected_project=ambient-project
		sed -i '/^AUTH_MODE=/d' "$case_dir/root/deploy/compose/.env"
		printf 'COMPOSE_PROJECT_NAME=env-project\n' >>"$case_dir/root/deploy/compose/.env"
		printf 'COMPOSE_PROJECT_NAME=env-project\n' >>"$case_dir/values"
		case_env+=(COMPOSE_PROJECT_NAME=ambient-project)
	fi
	printf '%s\n' "$root_line" >>"$case_dir/root/deploy/compose/.env"
	printf 'ROOT_KEYS=%s\n' "$root_value" >>"$case_dir/values"
	if run_auth >"$case_dir/auth.out" 2>&1 && grep -Fxq "$root_line" "$case_dir/root/deploy/compose/.env" \
		&& [[ "$(cat "$case_dir/enable-root-keys")" == "$root_value" ]] \
		&& [[ "$(cat "$case_dir/enable-database-url")" == 'postgresql://potocolom:pw@postgres/potocolom' ]] \
		&& [[ "$(cat "$case_dir/enable-public-url")" == 'http://localhost:8080' ]] \
		&& awk -v prefix="$case_dir/root/deploy/compose/.env." 'index($0, prefix) != 1 { bad=1 } END { exit bad || NR != 2 }' "$case_dir/mktemp.log" \
		&& awk -F '|' -v dir="$case_dir/root/deploy/compose" 'NF != 2 || $1 != dir || $2 != "600" { bad=1 } END { exit bad || NR != 2 }' "$case_dir/mv.log" \
		&& grep -qx 'AUTH_MODE=accounts' "$case_dir/root/deploy/compose/.env" \
		&& [[ "$(cat "$case_dir/docker-project")" == "$expected_project" ]] \
		&& [[ "$(stat -c '%a' "$case_dir/root/deploy/compose/.env")" == 600 ]]; then
		printf 'ok compose-root-key-%s-and-private-local-atomic-writes\n' "$format"
	else fail_case "root-key-$format" "$(cat "$case_dir/auth.out") temp=$(cat "$case_dir/mktemp.log") move=$(cat "$case_dir/mv.log")"; fi
done

new_case compose-read-failure
printf 'ROOT_KEYS=existing-ring\n' >>"$case_dir/root/deploy/compose/.env"
printf 'ROOT_KEYS=existing-ring\n' >>"$case_dir/values"
cp "$case_dir/root/deploy/compose/.env" "$case_dir/before.env"
if env "${case_env[@]}" COMPOSE_FAIL=1 bash "$case_dir/root/scripts/auth-enable.sh" >"$case_dir/auth.out" 2>&1; then
	fail_case compose-read-failure 'auth-enable accepted a failed Compose read'
elif cmp -s "$case_dir/before.env" "$case_dir/root/deploy/compose/.env" && [[ ! -e "$case_dir/enable-started" ]]; then
	printf 'ok failed-compose-read-changes-nothing\n'
else fail_case compose-read-failure 'the environment changed or app.enable ran'; fi


for failure in awk mv; do
	new_case "failed-write-$failure"
	printf 'ROOT_KEYS=existing-key-ring\n' >>"$case_dir/root/deploy/compose/.env"
	printf 'ROOT_KEYS=existing-key-ring\n' >>"$case_dir/values"
	cp "$case_dir/root/deploy/compose/.env" "$case_dir/before.env"
	case_env+=(AUTH_WRITE_FAIL="$failure")
	if run_auth >"$case_dir/auth.out" 2>&1; then
		fail_case "failed-$failure-write" 'auth-enable accepted a failed environment replacement'
	elif cmp -s "$case_dir/before.env" "$case_dir/root/deploy/compose/.env" \
		&& [[ ! -e "$case_dir/enable-started" ]] \
		&& [[ -z "$(find "$case_dir/root/deploy/compose" -maxdepth 1 -name '.env.*' ! -name '.env.example' -print -quit)" ]]; then
		printf 'ok failed-%s-write-preserves-env-and-cleans-own-temp\n' "$failure"
	else fail_case "failed-$failure-write" 'the environment changed, app.enable ran, or an owned temp remains'; fi
done

if ((failures)); then
	printf 'auth-enable locking: %s failure(s)\n' "$failures" >&2
	exit 1
fi
printf 'auth-enable locking: all cases passed\n'
