#!/usr/bin/env bash
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
failures=0
REAL_DOCKER="$(command -v docker)"
REAL_OPENSSL="$(command -v openssl)"
REAL_MKTEMP="$(command -v mktemp)"
REAL_MV="$(command -v mv)"

fail_case() {
	printf 'FAIL %s\n%s\n' "$1" "${2:-}"
	failures=$((failures + 1))
}

compose_value() {
	docker compose --env-file "$1" -f "$repo/deploy/compose/compose.yml" config --environment |
		awk -F= -v key="$2" '$1 == key { value = substr($0, index($0, "=") + 1) } END { print value }'
}

make_env() {
	local path="$1" format="$2"
	mkdir -p "$(dirname "$path")"
	awk -v format="$format" '
		/^POSTGRES_PASSWORD=/ {
			if (format == "export") print "export POSTGRES_PASSWORD = \"keep-db\" # operator value";
			else if (format == "spaced") print " POSTGRES_PASSWORD : keep-db # operator value";
			else if (format == "duplicates") print "POSTGRES_PASSWORD=old-db\nexport POSTGRES_PASSWORD: \"keep-db\" # last wins";
			else print "POSTGRES_PASSWORD=keep-db";
			next
		}
		/^FLEET_SECRET=/ {
			if (format == "export") print "export FLEET_SECRET : \"keep-fleet\" # operator value";
			else if (format == "spaced") print " FLEET_SECRET = keep-fleet # operator value";
			else if (format == "duplicates") print "FLEET_SECRET=old-fleet\nexport FLEET_SECRET = \"keep-fleet\" # last wins";
			else print "FLEET_SECRET=keep-fleet";
			next
		}
		{ print }
	' "$repo/deploy/compose/.env.example" >"$path"
	[[ "$format" != crlf ]] || sed -i 's/$/\r/' "$path"
}

new_case() {
	case_dir="$tmp/$1"
	mkdir -p "$case_dir/bin" "$case_dir/envdir" "$case_dir/offline"
	cat >"$case_dir/bin/docker" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == compose ]]; then exec "$REAL_DOCKER" "$@"; fi
if [[ "$1" == info ]]; then exit 0; fi
if [[ "$1" == volume && "$2" == inspect ]]; then
	[[ "${TEST_DB_EXISTS:-0}" != 1 ]] && exit 1
	exit 0
fi
exit 0
EOF
	chmod +x "$case_dir/bin/docker"
}

for format in plain export spaced duplicates crlf; do
	env_file="$tmp/$format/.env"
	make_env "$env_file" "$format"
	before_db="$(compose_value "$env_file" POSTGRES_PASSWORD)"
	before_fleet="$(compose_value "$env_file" FLEET_SECRET)"
	if ! ENV_FILE="$env_file" ENV_EXAMPLE="$repo/deploy/compose/.env.example" bash "$repo/scripts/ensure-env.sh" >/dev/null 2>&1; then
		fail_case "ensure-$format-runs"
		continue
	fi
	after_db="$(compose_value "$env_file" POSTGRES_PASSWORD)"
	after_fleet="$(compose_value "$env_file" FLEET_SECRET)"
	if [[ "$before_db" == "$after_db" && "$before_fleet" == "$after_fleet" ]]; then
		printf 'ok preserve-compose-secrets-%s\n' "$format"
	else
		fail_case "preserve-compose-secrets-$format" 'the effective database or fleet secret changed'
	fi
done

new_case missing-fleet-preserves-quoted-dollar-db
cp "$repo/deploy/compose/.env.example" "$case_dir/envdir/.env"
sed -i 's/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD='"'"'db-$literal'"'"'/; /^FLEET_SECRET=/d' "$case_dir/envdir/.env"
before_db="$(compose_value "$case_dir/envdir/.env" POSTGRES_PASSWORD)"
if REAL_DOCKER="$REAL_DOCKER" PATH="$case_dir/bin:$PATH" ENV_FILE="$case_dir/envdir/.env" \
	ENV_EXAMPLE="$repo/deploy/compose/.env.example" bash "$repo/scripts/ensure-env.sh" >/dev/null 2>&1; then
	after_db="$(compose_value "$case_dir/envdir/.env" POSTGRES_PASSWORD)"
	if [[ "$before_db" == "$after_db" ]]; then
		printf 'ok missing-fleet-preserves-quoted-dollar-db\n'
	else
		fail_case missing-fleet-preserves-quoted-dollar-db 'the effective database password changed'
	fi
else
	fail_case missing-fleet-preserves-quoted-dollar-db 'ensure-env failed'
fi

new_case missing-db-preserves-quoted-dollar-fleet
cp "$repo/deploy/compose/.env.example" "$case_dir/envdir/.env"
sed -i '/^POSTGRES_PASSWORD=/d; s/^FLEET_SECRET=.*/FLEET_SECRET='"'"'fleet-$literal'"'"'/' "$case_dir/envdir/.env"
before_fleet="$(compose_value "$case_dir/envdir/.env" FLEET_SECRET)"
if REAL_DOCKER="$REAL_DOCKER" PATH="$case_dir/bin:$PATH" ENV_FILE="$case_dir/envdir/.env" \
	ENV_EXAMPLE="$repo/deploy/compose/.env.example" bash "$repo/scripts/ensure-env.sh" >/dev/null 2>&1; then
	after_fleet="$(compose_value "$case_dir/envdir/.env" FLEET_SECRET)"
	if [[ "$before_fleet" == "$after_fleet" ]]; then
		printf 'ok missing-db-preserves-quoted-dollar-fleet\n'
	else
		fail_case missing-db-preserves-quoted-dollar-fleet 'the effective fleet secret changed'
	fi
else
	fail_case missing-db-preserves-quoted-dollar-fleet 'ensure-env failed'
fi

new_case offline-compose-fallback
make_env "$case_dir/offline/.env" export
cat >"$case_dir/bin/docker" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$case_dir/bin/docker"
before_db="$(grep 'POSTGRES_PASSWORD' "$case_dir/offline/.env")"
before_fleet="$(grep 'FLEET_SECRET' "$case_dir/offline/.env")"
if PATH="$case_dir/bin:$PATH" ENV_FILE="$case_dir/offline/.env" ENV_EXAMPLE="$repo/deploy/compose/.env.example" \
	bash "$repo/scripts/ensure-env.sh" >/dev/null 2>&1 \
	&& [[ "$(grep 'POSTGRES_PASSWORD' "$case_dir/offline/.env")" == "$before_db" \
		&& "$(grep 'FLEET_SECRET' "$case_dir/offline/.env")" == "$before_fleet" ]]; then
	printf 'ok offline-compose-fallback-preserves-secrets\n'
else
	fail_case offline-compose-fallback-preserves-secrets 'saved values changed or ensure-env failed'
fi

new_case offline-compose-uncertain-value
make_env "$case_dir/offline/.env" plain
sed -i 's/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=${SAVED_PASSWORD}/' "$case_dir/offline/.env"
before_file="$(cat "$case_dir/offline/.env")"
cat >"$case_dir/bin/docker" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$case_dir/bin/docker"
if PATH="$case_dir/bin:$PATH" ENV_FILE="$case_dir/offline/.env" ENV_EXAMPLE="$repo/deploy/compose/.env.example" \
	bash "$repo/scripts/ensure-env.sh" >"$case_dir/output" 2>&1; then
	fail_case offline-compose-uncertain-value 'uncertain Compose expansion was accepted'
elif [[ "$(cat "$case_dir/offline/.env")" == "$before_file" && "$(cat "$case_dir/output")" == *'Compose could not read'* ]]; then
	printf 'ok offline-compose-uncertain-value-refused\n'
else
	fail_case offline-compose-uncertain-value "the file changed or no clear error was printed: $(cat "$case_dir/output")"
fi

new_case missing-password-next-to-db
make_env "$case_dir/envdir/.env" plain
sed -i '/^POSTGRES_PASSWORD=/d' "$case_dir/envdir/.env"
if output="$(REAL_DOCKER="$REAL_DOCKER" TEST_DB_EXISTS=1 PATH="$case_dir/bin:$PATH" ENV_FILE="$case_dir/envdir/.env" ENV_EXAMPLE="$repo/deploy/compose/.env.example" bash "$repo/scripts/ensure-env.sh" 2>&1)"; then
	fail_case missing-password-next-to-db "ensure-env accepted a missing password beside an existing database"
elif [[ "$output" == *'POSTGRES_PASSWORD is missing'* ]] && ! grep -q '^POSTGRES_PASSWORD=' "$case_dir/envdir/.env"; then
	printf 'ok missing-password-next-to-db-refused\n'
else
	fail_case missing-password-next-to-db "$output"
fi

new_case placeholder-with-existing-db
cp "$repo/deploy/compose/.env.example" "$case_dir/envdir/.env"
sed -i '/^FLEET_SECRET=/d' "$case_dir/envdir/.env"
if REAL_DOCKER="$REAL_DOCKER" TEST_DB_EXISTS=1 PATH="$case_dir/bin:$PATH" ENV_FILE="$case_dir/envdir/.env" \
	ENV_EXAMPLE="$repo/deploy/compose/.env.example" bash "$repo/scripts/ensure-env.sh" >"$case_dir/output" 2>&1 \
	&& grep -qx 'POSTGRES_PASSWORD=change-me' "$case_dir/envdir/.env" \
	&& grep -q '^FLEET_SECRET=.' "$case_dir/envdir/.env"; then
	printf 'ok placeholder-with-existing-db-keeps-password-and-fills-fleet\n'
else
	fail_case placeholder-with-existing-db "$(cat "$case_dir/output")"
fi

new_case local-private-temp
make_env "$case_dir/envdir/.env" plain
sed -i '/^FLEET_SECRET=/d' "$case_dir/envdir/.env"
mkdir -p "$case_dir/bin"
cat >"$case_dir/bin/mktemp" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$1" >"${MK_TEMP_LOG:?}"
exec "$REAL_MKTEMP" "$@"
EOF
cat >"$case_dir/bin/mv" <<'EOF'
#!/usr/bin/env bash
while (($#)); do
	[[ "$1" == -- ]] && { shift; break; }
	shift
done
printf '%s|%s\n' "$(dirname "$1")" "$(stat -c '%a' "$1")" >"${MV_TEMP_LOG:?}"
exec "$REAL_MV" "$@"
EOF
chmod +x "$case_dir/bin/mktemp" "$case_dir/bin/mv"
if REAL_DOCKER="$REAL_DOCKER" REAL_MKTEMP="$REAL_MKTEMP" REAL_MV="$REAL_MV" MK_TEMP_LOG="$case_dir/mktemp.log" MV_TEMP_LOG="$case_dir/mv.log" \
	PATH="$case_dir/bin:$PATH" TMPDIR="${TMPDIR:-/dev/shm}" ENV_FILE="$case_dir/envdir/.env" ENV_EXAMPLE="$repo/deploy/compose/.env.example" \
	bash "$repo/scripts/ensure-env.sh" >/dev/null 2>&1; then
	local_tmp="$(cat "$case_dir/mktemp.log")"
	local_move="$(cat "$case_dir/mv.log")"
	if [[ "$local_tmp" == "$case_dir/envdir/"* && "$local_move" == "$case_dir/envdir|600" && "$(stat -c '%a' "$case_dir/envdir/.env")" == 600 ]]; then
		printf 'ok secret-temp-is-local-and-private\n'
	else
		fail_case secret-temp-is-local-and-private "mktemp=$local_tmp mv=$local_move mode=$(stat -c '%a' "$case_dir/envdir/.env")"
	fi
else
	fail_case secret-temp-is-local-and-private 'ensure-env failed'
fi

new_case direct-ensure-env-race
cat >"$case_dir/bin/openssl" <<'EOF'
#!/usr/bin/env bash
touch "${SECRET_START:?}"
while [[ ! -e "${SECRET_RELEASE:?}" ]]; do sleep 0.02; done
exec "$REAL_OPENSSL" "$@"
EOF
chmod +x "$case_dir/bin/openssl"
export REAL_DOCKER REAL_OPENSSL
ENV_FILE="$case_dir/envdir/.env" ENV_EXAMPLE="$repo/deploy/compose/.env.example" \
	SECRET_START="$case_dir/secret-start" SECRET_RELEASE="$case_dir/secret-release" PATH="$case_dir/bin:$PATH" \
	bash "$repo/scripts/ensure-env.sh" >"$case_dir/first.out" 2>&1 &
first_pid=$!
for _ in {1..200}; do [[ -e "$case_dir/secret-start" ]] && break; sleep 0.02; done
if [[ ! -e "$case_dir/secret-start" ]]; then
	touch "$case_dir/secret-release"
	wait "$first_pid" || true
	fail_case direct-ensure-env-race 'first ensure-env did not reach secret generation'
else
	if ENV_FILE="$case_dir/envdir/.env" ENV_EXAMPLE="$repo/deploy/compose/.env.example" PATH="$case_dir/bin:$PATH" \
		bash "$repo/scripts/ensure-env.sh" >"$case_dir/second.out" 2>&1; then
		direct_second_rc=0
	else
		direct_second_rc=$?
	fi
	touch "$case_dir/secret-release"
	if wait "$first_pid"; then direct_first_rc=0; else direct_first_rc=$?; fi
	if [[ "$direct_second_rc" -ne 0 && "$direct_first_rc" -eq 0 && "$(grep -c '^POSTGRES_PASSWORD=' "$case_dir/envdir/.env")" -eq 1 \
		&& "$(grep -c '^FLEET_SECRET=' "$case_dir/envdir/.env")" -eq 1 && "$(cat "$case_dir/envdir/.env" | wc -c)" -gt 0 \
		&& "$(cat "$case_dir/second.out")" == *'another potocolom install or preflight'* ]]; then
		printf 'ok direct-ensure-env-race-serialized\n'
	else
		fail_case direct-ensure-env-race "first=$direct_first_rc second=$direct_second_rc $(cat "$case_dir/second.out")"
	fi
fi

new_case shared-project-lock
mkdir -p "$case_dir/first/.envdir" "$case_dir/second/.envdir"
make_env "$case_dir/first/.envdir/.env" plain
make_env "$case_dir/second/.envdir/.env" plain
printf 'COMPOSE_PROJECT_NAME=shared-safety-test\n' >>"$case_dir/second/.envdir/.env"
cat >"$case_dir/hold-lock.sh" <<'EOF'
#!/usr/bin/env bash
source "$INSTALL_LOCK_HELPER"
install_acquire_locks "$1" "$2" "$3" shared-safety-test
touch "$LOCK_READY"
while [[ ! -e "$LOCK_RELEASE" ]]; do sleep 0.02; done
EOF
chmod +x "$case_dir/hold-lock.sh"
INSTALL_LOCK_HELPER="$repo/scripts/install-lock.sh" LOCK_READY="$case_dir/ready" LOCK_RELEASE="$case_dir/release" \
	bash "$case_dir/hold-lock.sh" "$case_dir/first/.envdir" "$case_dir/first/.envdir/.env" "$repo/deploy/compose/compose.yml" &
holder_pid=$!
for _ in {1..200}; do [[ -e "$case_dir/ready" ]] && break; sleep 0.02; done
if [[ ! -e "$case_dir/ready" ]]; then
	touch "$case_dir/release"
	wait "$holder_pid" || true
	fail_case shared-project-lock 'lock holder did not start'
else
	if REAL_DOCKER="$REAL_DOCKER" PATH="$case_dir/bin:$PATH" ENV_FILE="$case_dir/second/.envdir/.env" \
		ENV_EXAMPLE="$repo/deploy/compose/.env.example" bash "$repo/scripts/ensure-env.sh" >"$case_dir/second.out" 2>&1; then
		shared_rc=0
	else
		shared_rc=$?
	fi
	touch "$case_dir/release"
	wait "$holder_pid" || true
	if [[ "$shared_rc" -ne 0 && "$(cat "$case_dir/second.out")" == *'another potocolom install or preflight'* ]]; then
		printf 'ok shared-project-lock-across-directories\n'
	else
		fail_case shared-project-lock "exit=$shared_rc $(cat "$case_dir/second.out")"
	fi
fi

new_case lock-symlink-refused-without-truncation
lock_project="lock-symlink-$BASHPID"
lock_item="$(bash -c 'source "$1"; install_lock_key project "$2"' _ "$repo/scripts/install-lock.sh" "$lock_project")"
printf '%s\n' 'do not truncate' >"$case_dir/target"
ln -s "$case_dir/target" "$lock_item"
if INSTALL_LOCK_HELPER="$repo/scripts/install-lock.sh" bash -c \
	'source "$INSTALL_LOCK_HELPER"; install_acquire_locks "$1" "$2" "$3" "$4"' _ \
	"$case_dir/root" "$case_dir/root/.env" "$repo/deploy/compose/compose.yml" "$lock_project" \
	>"$case_dir/output" 2>&1; then
	symlink_rc=0
else
	symlink_rc=$?
fi
if [[ "$symlink_rc" -ne 0 && "$(cat "$case_dir/target")" == 'do not truncate' \
	&& "$(cat "$case_dir/output")" == *'unsafe installer lock file'* ]]; then
	printf 'ok lock-symlink-refused-without-truncation\n'
else
	fail_case lock-symlink-refused-without-truncation "exit=$symlink_rc $(cat "$case_dir/output")"
fi

new_case canonical-lock-key-before-target-exists
mkdir -p "$case_dir/real-parent"
ln -s "$case_dir/real-parent" "$case_dir/alias"
real_key="$(bash -c 'source "$1"; install_lock_path_key "$2"' _ "$repo/scripts/install-lock.sh" "$case_dir/real-parent/new-install")"
alias_key="$(bash -c 'source "$1"; install_lock_path_key "$2"' _ "$repo/scripts/install-lock.sh" "$case_dir/alias/new-install")"
if [[ "$real_key" == "$alias_key" ]]; then
	printf 'ok canonical-lock-key-before-target-exists\n'
else
	fail_case canonical-lock-key-before-target-exists 'symlinked parent produced a different lock key'
fi

new_case lock-spoofed-key-does-not-bypass-contention
spoof_project="lock-spoof-$$"
cat >"$case_dir/hold-lock.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
source "$INSTALL_LOCK_HELPER"
install_acquire_locks "$1" "$2" "$3" "$4"
touch "$LOCK_READY"
while [[ ! -e "$LOCK_RELEASE" ]]; do sleep 0.02; done
EOF
chmod +x "$case_dir/hold-lock.sh"
INSTALL_LOCK_HELPER="$repo/scripts/install-lock.sh" LOCK_READY="$case_dir/ready" LOCK_RELEASE="$case_dir/release" \
	bash "$case_dir/hold-lock.sh" "$case_dir/root" "$case_dir/root/.env" "$repo/deploy/compose/compose.yml" \
	"$spoof_project" &
lock_holder_pid=$!
for _ in {1..200}; do [[ -e "$case_dir/ready" ]] && break; sleep 0.02; done
if [[ ! -e "$case_dir/ready" ]]; then
	touch "$case_dir/release"
	wait "$lock_holder_pid" || true
	fail_case lock-spoofed-key-does-not-bypass-contention 'lock holder did not start'
else
	project_key="$(bash -c 'source "$1"; install_lock_key project "$2"' _ "$repo/scripts/install-lock.sh" "$spoof_project")"
	if POTOCOLOM_PROJECT_LOCK_KEY="$project_key" INSTALL_LOCK_HELPER="$repo/scripts/install-lock.sh" \
		bash -c 'source "$INSTALL_LOCK_HELPER"; install_acquire_locks "$1" "$2" "$3" "$4"' _ \
		"$case_dir/contender" "$case_dir/contender/.env" "$repo/deploy/compose/compose.yml" "$spoof_project" \
		>"$case_dir/spoof.out" 2>&1; then
		spoof_rc=0
	else
		spoof_rc=$?
	fi
	printf 'unrelated descriptor target\n' >"$case_dir/unrelated"
	if POTOCOLOM_PROJECT_LOCK_FD=9 POTOCOLOM_PROJECT_LOCK_KEY="$project_key" \
		INSTALL_LOCK_HELPER="$repo/scripts/install-lock.sh" bash -c \
		'exec 9<"$5"; source "$INSTALL_LOCK_HELPER"; install_acquire_locks "$1" "$2" "$3" "$4"' _ \
		"$case_dir/contender" "$case_dir/contender/.env" "$repo/deploy/compose/compose.yml" "$spoof_project" \
		"$case_dir/unrelated" >"$case_dir/stale-fd.out" 2>&1; then
		stale_fd_rc=0
	else
		stale_fd_rc=$?
	fi
	touch "$case_dir/release"
	wait "$lock_holder_pid" || true
	if [[ "$spoof_rc" -ne 0 && "$stale_fd_rc" -ne 0 \
		&& "$(cat "$case_dir/spoof.out")" == *'another potocolom install or preflight'* \
		&& "$(cat "$case_dir/stale-fd.out")" == *'another potocolom install or preflight'* ]]; then
		printf 'ok lock-spoofed-key-does-not-bypass-contention\n'
	else
		fail_case lock-spoofed-key-does-not-bypass-contention "bare=$spoof_rc fd=$stale_fd_rc $(cat "$case_dir/spoof.out") $(cat "$case_dir/stale-fd.out")"
	fi
fi

if ((failures > 0)); then exit 1; fi
