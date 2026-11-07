#!/usr/bin/env bash
# Upgrade correctness for scripts/install.sh: the saved profile, stage then
# swap, the backup before the API restarts, and the downgrade guard, which
# judges the database by its own Alembic revision. Docker, curl, nvidia-smi,
# df, id, rocm-smi and sleep are stubbed. install.sh, preflight.sh, backup.sh
# and restore.sh run for real against a small state machine in $STATE_DIR. The
# revision probe is install.sh's own Python, and it runs for real against an
# Alembic fixture whose revisions are the ones the stubbed image knows.
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
REAL_CURL="$(command -v curl)"
REAL_ID="$(command -v id)"
REAL_DATE="$(command -v date)"
RELEASE_TEST_AWK="$(type -P awk)"
failures=0


pass_case() { printf 'ok %s\n' "$1"; }

fail_case() {
	printf 'FAIL %s\n' "$1"
	if [[ -n "${2:-}" ]]; then printf '  %s\n' "$2"; fi
	failures=$((failures + 1))
}

# ------------------------------------------------------------------ stubs

mkdir -p "$tmp/stubs"

cat >"$tmp/stubs/curl" <<'EOF'
#!/usr/bin/env bash
if [[ -n "${CURL_LOG:-}" ]]; then printf '%s\n' "$*" >>"$CURL_LOG.args"; fi
while (($#)); do
	if [[ "$1" == -o ]]; then
		out="$2"
		shift 2
	elif [[ "$1" == http://* || "$1" == https://* || "$1" == file://* ]]; then
		url="$1"
		shift
	else
		shift
	fi
done
if [[ -z "${out:-}" ]]; then
	if [[ "${url:-}" == */api/v1/ready ]]; then
		printf '%s\n' "$url" >>"${CURL_LOG:?}"
		count=0
		if [[ -f "${READY_COUNT_FILE:?}" ]]; then count="$(cat "$READY_COUNT_FILE")"; fi
		count=$((count + 1))
		printf '%s\n' "$count" >"$READY_COUNT_FILE"
		if [[ "${READY_FAIL:-0}" == 1 || "$count" -le "${READY_AFTER:-0}" ]]; then exit 22; fi
		exit 0
	fi
	printf '{"status":"ok"}\n'
	exit 0
fi
printf '%s\n' "${url:-}" >>"${CURL_LOG:?}"
if [[ "${url:-}" == file://* ]]; then
	exec "$REAL_CURL" -fsSL -o "$out" "$url"
fi
case "$out" in
	*SHA256SUMS) cp "$BUNDLE_DIR/SHA256SUMS" "$out" ;;
	*) cp "$BUNDLE_DIR/bundle.tar.gz" "$out" ;;
esac
EOF

cat >"$tmp/stubs/awk" <<'EOF'
#!/usr/bin/env bash
if [[ $# == 2 && "$2" == /proc/meminfo ]]; then
	printf '33554432\n'
else
	exec "${RELEASE_TEST_AWK:?}" "$@"
fi
EOF

cat >"$tmp/stubs/df" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${STATE_DIR:?}/df.log"
if [[ "$*" == *-B1* ]]; then
	printf 'Avail\n%s\n' "${AVAIL_BYTES:-1000000000000}"
else
	printf 'Avail\n100G\n'
fi
EOF

cat >"$tmp/stubs/nvidia-smi" <<'EOF'
#!/usr/bin/env bash
if [[ "${FAKE_NVIDIA:-ok}" == fail ]]; then exit 9; fi
if [[ "$*" == '--query-gpu=name --format=csv,noheader' ]]; then
	if [[ "${DETECT_MISS:-0}" == 1 ]]; then exit 1; fi
	printf 'NVIDIA Test GPU\n'
elif [[ "$*" == *'--query-gpu=name,memory.total,driver_version'* ]]; then
	printf 'NVIDIA Test GPU, 8192 MiB, 595.91.07\n'
elif [[ "$*" == *'--query-gpu=name'* ]]; then
	printf 'NVIDIA Test GPU\n'
else
	printf 'CUDA Version: 13.2\n'
fi
EOF

cat >"$tmp/stubs/nvidia-ctk" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

cat >"$tmp/stubs/hostname" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == -I ]]; then printf '192.168.1.10 10.0.0.2\n'; fi
EOF

cat >"$tmp/stubs/rocm-smi" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

cat >"$tmp/stubs/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

cat >"$tmp/stubs/id" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == -nG ]]; then printf 'video render\n'; exit 0; fi
exec "${REAL_ID:?}" "$@"
EOF

# FAKE_STAMP replaces the UTC stamp that names a backup, so a test can make one collide.
cat >"$tmp/stubs/date" <<'EOF'
#!/usr/bin/env bash
if [[ -n "${FAKE_STAMP:-}" && "$*" == '-u +%Y%m%dT%H%M%SZ' ]]; then printf '%s\n' "$FAKE_STAMP"; exit 0; fi
exec "${REAL_DATE:?}" "$@"
EOF

# The docker state machine. Containers are "ID SERVICE STATE" lines in
# $STATE_DIR/containers. The database exists when $STATE_DIR/pgdata exists, and
# its Alembic revision is the content of $STATE_DIR/revision (absent means no
# alembic_version table). Every call is logged to $DOCKER_LOG with the
# COMPOSE_PROFILES the call saw.
cat >"$tmp/stubs/docker" <<'EOF'
#!/usr/bin/env bash
state="${STATE_DIR:?}"
printf '%s | COMPOSE_PROFILES=%s\n' "$*" "${COMPOSE_PROFILES-unset}" >>"${DOCKER_LOG:?}"

has_service() {
	local id service cur
	while read -r id service cur; do
		if [[ "$service" == "$1" ]]; then return 0; fi
	done <"$state/containers"
	return 1
}

# set_container COLUMN KEY STATE. Column 1 matches the id, 2 the service.
set_container() {
	local column="$1" key="$2" value="$3" id service cur out=""
	while read -r id service cur; do
		if [[ -z "$id" ]]; then continue; fi
		if [[ "$column" == 1 && "$id" == "$key" ]] || [[ "$column" == 2 && "$service" == "$key" ]]; then
			cur="$value"
		fi
		out+="$id $service $cur"$'\n'
	done <"$state/containers"
	printf '%s' "$out" >"$state/containers"
}

remove_container() {
	local column="$1" key="$2" id service cur out=""
	while read -r id service cur; do
		if [[ -z "$id" ]]; then continue; fi
		if [[ "$column" == 2 && "$service" == "$key" ]]; then continue; fi
		out+="$id $service $cur"$'\n'
	done <"$state/containers"
	printf '%s' "$out" >"$state/containers"
}

kill_installer() {
	# backup.sh is this process's parent and install.sh is backup.sh's parent.
	local stat rest ppid
	read -r stat </proc/"$PPID"/stat
	rest="${stat##*) }"
	read -r _ ppid _ <<<"$rest"
	kill -TERM "$ppid"
}

psql_stub() {
	local sql="$1" n
	case "$sql" in
		*pg_terminate_backend*)
			# The rows this statement would end: the stuck client session, the
			# installer's own connection unless the statement has pid <> pg_backend_pid(),
			# and background processes unless it has backend_type = 'client backend'.
			# The log records the counts, so a test can see what was ended.
			local ended_client=0 ended_self=0 ended_background=0
			if [[ -f "$state/sessions_stuck" ]]; then
				ended_client=1
			else
				ended_client="$(cat "$state/sessions" 2>/dev/null || true)"
				ended_client="${ended_client:-0}"
			fi
			if [[ "$sql" != *"pid <> pg_backend_pid()"* ]]; then ended_self=1; fi
			if [[ "$sql" != *"backend_type = 'client backend'"* ]]; then
				ended_background="$(cat "$state/background" 2>/dev/null || echo 0)"
			fi
			printf 'terminated client=%s self=%s background=%s\n' "$ended_client" "$ended_self" "$ended_background" >>"$DOCKER_LOG"
			if [[ "${SESSIONS_SURVIVE:-0}" != 1 ]]; then
				rm -f "$state/sessions_stuck"
				printf '0\n' >"$state/sessions"
			fi
			printf '1\n'
			;;
		*pg_stat_activity*)
			# The rows the query sees: client sessions (one ends per poll;
			# sessions_stuck keeps one open), the installer's own connection
			# unless the query excludes it with pid <> pg_backend_pid(), and
			# background processes unless the query keeps only client backends.
			local client self=1 background=0
			if [[ -f "$state/sessions_stuck" ]]; then
				client=1
			else
				client="$(cat "$state/sessions" 2>/dev/null || true)"
				client="${client:-0}"
				if ((client > 0)); then printf '%s\n' "$((client - 1))" >"$state/sessions"; fi
			fi
			if [[ "$sql" == *"pid <> pg_backend_pid()"* ]]; then self=0; fi
			if [[ "$sql" != *"backend_type = 'client backend'"* ]]; then
				background="$(cat "$state/background" 2>/dev/null || echo 0)"
			fi
			printf '%s\n' "$((client + self + background))"
			;;
		*to_regclass*)
			if [[ -f "$state/revision" ]]; then printf 't\n'; else printf 'f\n'; fi
			;;
		*pg_tables*)
			# Public tables of the database: $state/tables holds the count, 0 when absent.
			cat "$state/tables" 2>/dev/null || printf '0\n'
			;;
		*version_num*)
			local stuck=0
			if [[ -f "$state/sessions_stuck" ]]; then stuck=1; fi
			printf 'revision-read sessions=%s stuck=%s\n' "$(cat "$state/sessions" 2>/dev/null || echo 0)" "$stuck" >>"$DOCKER_LOG"
			if [[ "${REVISION_FAIL:-0}" == 1 ]]; then return 1; fi
			cat "$state/revision"
			;;
		*pg_database_size*)
			printf '%s\n' "${DB_BYTES:-1000000}"
			;;
	esac
}

postgres_exec() {
	local cmd="$1"
	shift
	case "$cmd" in
		psql) psql_stub "${*: -1}" ;;
		pg_dump)
			if [[ "${BACKUP_KILL:-0}" == 1 ]]; then
				kill_installer
				return 1
			fi
			if [[ "${BACKUP_FAIL:-0}" == 1 ]]; then return 1; fi
			printf 'DATABASE-DUMP\n'
			;;
		pg_restore) cat >/dev/null ;;
	esac
}

api_exec() {
	case "${SETUP_LINK_MODE:-link}" in
		link) printf 'http://192.168.1.10:8080/setup#installer-fixture-token\n' ;;
		decided)
			echo 'installation already claimed' >&2
			return 3
			;;
		fail)
			echo 'boom' >&2
			return 1
			;;
	esac
}

compose_cmd() {
	local env_file="" profile_arg="" sub arg rev image saved known probe fixture known_rev
	while (($#)); do
		case "$1" in
			-p | --env-file | -f | --profile)
				if [[ "$1" == --env-file ]]; then env_file="$2"; fi
				if [[ "$1" == --profile ]]; then profile_arg="$2"; fi
				shift 2
				;;
			*) break ;;
		esac
	done
	sub="${1:-}"
	shift || true
	case "$sub" in
		version) printf '2.30.0\n' ;;
		config)
			if [[ "${1:-}" == --environment ]]; then
				if [[ -f "$env_file" ]]; then grep -v '^[[:space:]]*#' "$env_file" || true; fi
				return 0
			fi
			local profile="${profile_arg:-${COMPOSE_PROFILES:-}}" version="${POTOCOLOM_VERSION:-}"
			# Compose takes the shell's POTOCOLOM_VERSION, else the env file's, else dev.
			if [[ -z "$version" && -f "$env_file" ]]; then
				version="$(sed -n 's/^POTOCOLOM_VERSION=//p' "$env_file" | tail -n 1)"
			fi
			version="${version:-dev}"
			printf 'postgres:16\nghcr.io/portocolom-studio/potocolom-api:%s\n' "$version"
			case "$profile" in
				gpu) printf 'ghcr.io/portocolom-studio/potocolom-worker:%s-cuda\n' "$version" ;;
				rocm) printf 'ghcr.io/portocolom-studio/potocolom-worker:%s-rocm\n' "$version" ;;
			esac
			;;
		pull)
			if [[ "${DOCKER_FAIL_PULL:-}" == 1 ]]; then return 1; fi
			return 0
			;;
		up)
			arg=" $* "
			# Record the release this up starts, resolved as Compose resolves it: the
			# shell's POTOCOLOM_VERSION, else the env file's, else dev.
			local up_version="${POTOCOLOM_VERSION:-}"
			if [[ -z "$up_version" && -f "$env_file" ]]; then
				up_version="$(sed -n 's/^POTOCOLOM_VERSION=//p' "$env_file" | tail -n 1)"
			fi
			printf 'up-release %s profiles=%s\n' "${up_version:-dev}" "${profile_arg:-${COMPOSE_PROFILES:-}}" >>"$DOCKER_LOG"
			if [[ "$arg" == *" --no-start "* ]]; then return 0; fi
			if [[ "$arg" == *" postgres "* ]]; then
				if has_service postgres; then
					set_container 2 postgres running
				else
					printf 'pg-created postgres running\n' >>"$state/containers"
				fi
				return 0
			fi
			if [[ "$arg" == *" --no-build "* ]]; then
				if [[ -n "${MIGRATE_TO:-}" ]]; then
					printf '%s\n' "$MIGRATE_TO" >"$state/revision"
					touch "$state/pgdata"
				fi
				return 0
			fi
			return 0
			;;
		stop | start)
			for arg in "$@"; do
				if [[ "$arg" != -* ]]; then
					if [[ "$sub" == stop ]]; then set_container 2 "$arg" exited; else set_container 2 "$arg" running; fi
				fi
			done
			;;
		rm)
			if [[ "${RM_FAIL:-0}" == 1 ]]; then return 1; fi
			for arg in "$@"; do
				if [[ "$arg" != -* ]]; then remove_container 2 "$arg"; fi
			done
			;;
		down) : >"$state/containers" ;;
		ps)
			for arg in "$@"; do rev="$arg"; done
			local id service cur
			while read -r id service cur; do
				if [[ "$service" == "$rev" && "$cur" == running ]]; then printf '%s\n' "$id"; fi
			done <"$state/containers"
			;;
		exec)
			if [[ "${1:-}" == -T ]]; then shift; fi
			case "${1:-}" in
				postgres)
					shift
					postgres_exec "$@"
					return $?
					;;
				api)
					shift
					api_exec "$@"
					return $?
					;;
			esac
			;;
		run)
			# The probe answers for the image compose resolves: the shell's
			# POTOCOLOM_VERSION, else the one the --env-file names. SAVED_IMAGE_KNOWN
			# is the answer of the image the saved .env names; TARGET_KNOWN the answer
			# of every other image.
			rev="${*: -1}"
			if [[ -n "${TARGET_FAIL:-}" ]]; then return 1; fi
			image="${POTOCOLOM_VERSION:-}"
			saved="$(sed -n 's/^POTOCOLOM_VERSION=//p' "$env_file" 2>/dev/null)"
			if [[ -z "$image" ]]; then image="$saved"; fi
			known="${TARGET_KNOWN:-}"
			if [[ "$image" == "$saved" ]]; then known="${SAVED_IMAGE_KNOWN:-$known}"; fi
			printf 'probe image=%s revision=%s\n' "$image" "$rev" >>"$DOCKER_LOG"
			# The probe script itself is run against real Alembic scripts by
			# backend/tests/test_install_revision_probe.py; here the image answers.
			if [[ " $known " == *" $rev "* ]]; then printf 'known\n'; else printf 'unknown\n'; fi
			;;
	esac
	return 0
}

case "${1:-}" in
	--version) printf 'Docker version 28.0.0, build test\n' ;;
	info) ;;
	ps) cat "$state/containers" ;;
	stop)
		shift
		for id in "$@"; do set_container 1 "$id" exited; done
		;;
	start)
		shift
		for id in "$@"; do set_container 1 "$id" running; done
		;;
	volume)
		case "${2:-}" in
			inspect)
				[[ "${3:-}" == "${DB_PROJECT:-potocolom}_pgdata" && -f "$state/pgdata" ]]
				exit $?
				;;
			ls)
				# Lists every volume: the database when $state/pgdata exists, the assets
				# volume unless $state/no_assets exists, and a name that only contains the
				# database name when DECOY_VOLUME is set.
				if [[ -n "${DOCKER_VOLUME_LS_FAIL:-}" ]]; then exit 1; fi
				if [[ -f "$state/pgdata" ]]; then printf '%s_pgdata\n' "${DB_PROJECT:-potocolom}"; fi
				if [[ ! -f "$state/no_assets" ]]; then printf '%s_assets\n' "${DB_PROJECT:-potocolom}"; fi
				if [[ -n "${DECOY_VOLUME:-}" ]]; then printf '%s_pgdata_copy\n' "${DB_PROJECT:-potocolom}"; fi
				exit 0
				;;
			rm)
				rm -f "$state/pgdata" "$state/revision"
				;;
		esac
		;;
	image) [[ "${DOCKER_IMAGES_CACHED:-}" == 1 ]]; exit $? ;;
	run)
		case "$*" in
			*" du -sb /src"*) printf '%s\t/src\n' "${ASSET_BYTES:-1000}" ;;
			*" tar -C /src -cf -"*) printf 'ASSETS-TAR\n' ;;
		esac
		;;
	compose)
		shift
		compose_cmd "$@"
		exit $?
		;;
esac
exit 0
EOF

chmod +x "$tmp"/stubs/*

# ---------------------------------------------------------------- fixtures

new_case() {
	case_dir="$tmp/$1"
	mkdir -p "$case_dir/bin" "$case_dir/state" "$case_dir/source" "$case_dir/tmp" \
		"$case_dir/kfd/0" "$case_dir/dri" "$case_dir/pci"
	: >"$case_dir/docker.log"
	: >"$case_dir/curl.log"
	: >"$case_dir/state/containers"
	printf 'gfx_target_version 110002\n' >"$case_dir/kfd/0/properties"
	: >"$case_dir/kfd-dev"
	local name
	for name in curl awk df nvidia-smi nvidia-ctk hostname rocm-smi sleep id date docker; do
		ln -s "$tmp/stubs/$name" "$case_dir/bin/$name"
	done
	# Each case gets its own bundle directory, so SHA256SUMS can name any version.
	rm -f "$case_dir/source/bundle.tar.gz"
}

make_bundle() {  # CASE_DIR VERSION
	local case_dir="$1" version="$2" src="$1/bundle-src"
	if [[ ! -d "$src" ]]; then
		mkdir -p "$src/deploy/compose" "$src/scripts"
		cp "$repo/deploy/compose/compose.yml" "$repo/deploy/compose/.env.example" "$src/deploy/compose/"
		cp "$repo/scripts/preflight.sh" "$repo/scripts/ensure-env.sh" "$repo/scripts/install-lock.sh" \
			"$repo/scripts/gpu-profile.sh" "$repo/scripts/backup.sh" "$repo/scripts/restore.sh" "$src/scripts/"
		chmod +x "$src"/scripts/*.sh
		(cd "$src" && tar -czf "$case_dir/source/bundle.tar.gz" deploy scripts)
	fi
	(cd "$case_dir/source" && sha256sum bundle.tar.gz | sed "s#bundle.tar.gz#potocolom-$version-selfhost.tar.gz#" >SHA256SUMS)
}

# A live install left by an earlier release. The optional third argument is
# the COMPOSE_PROFILES value saved in its .env.
seed_live() {  # CASE_DIR VERSION [PROFILE]
	local dir="$1/install"
	mkdir -p "$dir/deploy/compose" "$dir/scripts"
	cp "$repo/deploy/compose/compose.yml" "$dir/deploy/compose/"
	cp "$repo/scripts/backup.sh" "$repo/scripts/restore.sh" "$dir/scripts/"
	sed -e "s/^POTOCOLOM_VERSION=.*/POTOCOLOM_VERSION=$2/" \
		-e 's/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=keep-db/' \
		-e 's/^FLEET_SECRET=.*/FLEET_SECRET=keep-fleet/' \
		-e 's/^ROOT_KEYS=.*/ROOT_KEYS=1:keep/' \
		"$repo/deploy/compose/.env.example" >"$dir/deploy/compose/.env"
	if [[ -n "${3:-}" ]]; then printf 'COMPOSE_PROFILES=%s\n' "$3" >>"$dir/deploy/compose/.env"; fi
	chmod 600 "$dir/deploy/compose/.env"
}

# The database: a volume that exists, and the revision its alembic_version holds.
# A revision of none means the volume exists but has no alembic_version table.
seed_db() {  # CASE_DIR REVISION|none
	: >"$1/state/pgdata"
	if [[ "$2" == none ]]; then
		rm -f "$1/state/revision"
	else
		printf '%s\n' "$2" >"$1/state/revision"
	fi
}

# The container-state snapshot: each container's ID, service and state, in order.
container_states() {  # CASE_DIR
	cat "$1/state/containers"
}

seed_containers() {  # CASE_DIR LINE...
	local case_dir="$1" line
	shift
	: >"$case_dir/state/containers"
	for line in "$@"; do printf '%s\n' "$line" >>"$case_dir/state/containers"; done
}

# An install that has been running: API, database and a GPU worker.
seed_running() {  # CASE_DIR
	seed_containers "$1" "api1 api running" "pg1 postgres running" "wk1 worker running"
}

tree_hash() {
	(cd "$1" && { find . -print | LC_ALL=C sort; find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum; }) | sha256sum | cut -d' ' -f1
}

# Runs install.sh for a case. Knobs are KEY=VALUE words after the case dir.
run_install() {
	local case_dir="$1"
	shift
	env -u DISPLAY -u WAYLAND_DISPLAY -u POTOCOLOM_PROFILE -u COMPOSE_PROFILES -u POTOCOLOM_PUBLIC_URL \
		PATH="$case_dir/bin:$PATH" TMPDIR="$case_dir/tmp" STATE_DIR="$case_dir/state" \
		DOCKER_LOG="$case_dir/docker.log" CURL_LOG="$case_dir/curl.log" BUNDLE_DIR="$case_dir/source" \
		READY_COUNT_FILE="$case_dir/ready-count" REAL_CURL="$REAL_CURL" REAL_ID="$REAL_ID" REAL_DATE="$REAL_DATE" \
		RELEASE_TEST_AWK="$RELEASE_TEST_AWK" POTOCOLOM_SKIP_GPU_TEST=1 \
		POTOCOLOM_KFD="$case_dir/no-kfd" POTOCOLOM_KFD_TOPOLOGY="$case_dir/kfd" \
		POTOCOLOM_DRI="$case_dir/dri" POTOCOLOM_PCI_DEVICES="$case_dir/pci" \
		POTOCOLOM_VERSION=v9.9.9 POTOCOLOM_DIR="$case_dir/install" \
		"$@" bash "${INSTALLER_SCRIPT:-$repo/scripts/install.sh}" 2>&1
}

# Line number of the first docker call whose text contains $1, or 0.
log_line() {
	grep -n -F -- "$1" "$2/docker.log" | head -n 1 | cut -d: -f1
}

backup_dirs() {  # CASE_DIR PATTERN: names of backups matching PATTERN
	local dir="$1/install/backups"
	[[ -d "$dir" ]] || return 0
	ls -1 "$dir" | grep -E -- "$2" || true
}

# ----------------------------------------------------------------- cases

# 1. The saved profile wins over a detection that now disagrees.
new_case profile-kept-when-detection-flips
seed_live "$case_dir" v8.8.8 gpu
seed_containers "$case_dir" "wk1 worker running"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" DETECT_MISS=1)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$output" == *'keeps gpu'* && "$output" == *'POTOCOLOM_PROFILE=api'* \
	&& "$(sed -n 's/^COMPOSE_PROFILES=//p' "$case_dir/install/deploy/compose/.env")" == gpu \
	&& "$(grep -c -- 'rm -sf' "$case_dir/docker.log")" -eq 0 ]]; then
	pass_case profile-kept-when-detection-flips
else
	fail_case profile-kept-when-detection-flips "rc=$rc $output"
fi

# 2. No saved profile: the project's existing worker container decides.
new_case profile-recovered-from-containers
seed_live "$case_dir" v8.8.8
seed_containers "$case_dir" "wk1 worker exited"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" DETECT_MISS=1)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$output" == *'keeps gpu'* \
	&& "$(sed -n 's/^COMPOSE_PROFILES=//p' "$case_dir/install/deploy/compose/.env")" == gpu \
	&& "$(grep -c -- 'rm -sf' "$case_dir/docker.log")" -eq 0 ]]; then
	pass_case profile-recovered-from-containers
else
	fail_case profile-recovered-from-containers "rc=$rc $output"
fi

new_case profile-recovered-rocm-from-containers
seed_live "$case_dir" v8.8.8
seed_containers "$case_dir" "wr worker-rocm exited"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" POTOCOLOM_KFD="$case_dir/kfd-dev")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$output" == *'keeps rocm'* && "$(sed -n 's/^COMPOSE_PROFILES=//p' "$case_dir/install/deploy/compose/.env")" == rocm \
	&& "$(log_line 'up -d --no-build' "$case_dir")" -gt 0 \
	&& "$(grep 'up -d --no-build' "$case_dir/docker.log")" == *'--profile rocm'* ]]; then
	pass_case profile-recovered-rocm-from-containers
else
	fail_case profile-recovered-rocm-from-containers "rc=$rc $output"
fi

# 3. Only an explicit POTOCOLOM_PROFILE removes a worker.
new_case explicit-profile-change-removes-old-worker
seed_live "$case_dir" v8.8.8 gpu
seed_containers "$case_dir" "wk1 worker running"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" POTOCOLOM_PROFILE=api)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(sed -n 's/^COMPOSE_PROFILES=//p' "$case_dir/install/deploy/compose/.env")" == api \
	&& "$(grep -c -- '--profile gpu rm -sf worker |' "$case_dir/docker.log")" -eq 1 \
	&& "$(grep -c -- 'rm -sf worker-rocm' "$case_dir/docker.log")" -eq 1 ]]; then
	pass_case explicit-profile-change-removes-old-worker
else
	fail_case explicit-profile-change-removes-old-worker "rc=$rc $output"
fi

# 4. A saved value that is not a release profile stops the install before any pull.
new_case saved-profile-must-be-a-release-profile
seed_live "$case_dir" v8.8.8 smoke
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir")" && rc=0 || rc=$?
if [[ "$rc" -ne 0 && "$output" == *'COMPOSE_PROFILES in'* \
	&& "$(grep -c -- '--images\| pull\| up -d' "$case_dir/docker.log")" -eq 0 ]]; then
	pass_case saved-profile-must-be-a-release-profile
else
	fail_case saved-profile-must-be-a-release-profile "rc=$rc $output"
fi

# 5. A shell COMPOSE_PROFILES is not inherited, and a fresh install saves the detected profile.
new_case inherited-compose-profiles-dropped-and-saved
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" COMPOSE_PROFILES=rocm)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(sed -n 's/^COMPOSE_PROFILES=//p' "$case_dir/install/deploy/compose/.env")" == gpu \
	&& "$(grep -c -- 'COMPOSE_PROFILES=rocm' "$case_dir/docker.log")" -eq 0 ]]; then
	pass_case inherited-compose-profiles-dropped-and-saved
else
	fail_case inherited-compose-profiles-dropped-and-saved "rc=$rc $output"
fi

# 6. The version line names install, reinstall, upgrade and downgrade.
new_case version-line-install
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir")" && rc=0 || rc=$?
first="$output"
output="$(run_install "$case_dir")" && rc2=0 || rc2=$?
if [[ "$rc" -eq 0 && "$rc2" -eq 0 && "$first" == *'Installing v9.9.9'* && "$output" == *'Reinstalling v9.9.9'* ]]; then
	pass_case version-line-install-and-reinstall
else
	fail_case version-line-install-and-reinstall "rc=$rc/$rc2 $output"
fi

new_case version-line-upgrade-and-downgrade
seed_live "$case_dir" v8.8.8
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir")" && rc=0 || rc=$?
seed_live "$case_dir" v10.0.0
output2="$(run_install "$case_dir")" && rc2=0 || rc2=$?
if [[ "$rc" -eq 0 && "$output" == *'Upgrading v8.8.8 to v9.9.9'* \
	&& "$rc2" -eq 0 && "$output2" == *'Downgrading v10.0.0 to v9.9.9'* ]]; then
	pass_case version-line-upgrade-and-downgrade
else
	fail_case version-line-upgrade-and-downgrade "rc=$rc/$rc2 $output $output2"
fi

# 7. A failed pull leaves the live install byte for byte as it was, .env included.
new_case failed-pull-leaves-tree-identical
seed_live "$case_dir" v8.8.8
make_bundle "$case_dir" v9.9.9
before="$(tree_hash "$case_dir/install")"
output="$(run_install "$case_dir" DOCKER_FAIL_PULL=1)" && rc=0 || rc=$?
after="$(tree_hash "$case_dir/install")"
if [[ "$rc" -ne 0 && "$before" == "$after" && ! -e "$case_dir/install/backups" ]]; then
	pass_case failed-pull-leaves-tree-identical
else
	fail_case failed-pull-leaves-tree-identical "rc=$rc $output"
fi

# 8. An upgrade stops the services, backs up, and only then restarts them.
new_case upgrade-backs-up-before-the-api-restarts
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" r1
seed_running "$case_dir"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" TARGET_KNOWN="r1 r2")" && rc=0 || rc=$?
backup="$(backup_dirs "$case_dir" '^v8\.8\.8-r1-[0-9]{8}T[0-9]{6}Z$')"
stop_line="$(log_line 'stop api1 wk1' "$case_dir")"
version_line="$(log_line 'version_num' "$case_dir")"
dump_line="$(log_line 'pg_dump' "$case_dir")"
up_line="$(log_line 'up -d --no-build' "$case_dir")"
if [[ "$rc" -eq 0 && -n "$backup" ]] \
	&& [[ -f "$case_dir/install/backups/$backup/database.dump" && -f "$case_dir/install/backups/$backup/assets.tar" \
		&& -f "$case_dir/install/backups/$backup/env" && -f "$case_dir/install/backups/$backup/compose.yml" ]] \
	&& (cd "$case_dir/install/backups/$backup" && sha256sum --check --strict SHA256SUMS >/dev/null 2>&1) \
	&& ((stop_line > 0 && stop_line < version_line && version_line < dump_line && dump_line < up_line)) \
	&& [[ -z "$(ls -A "$case_dir/tmp")" ]]; then
	pass_case upgrade-backs-up-before-the-api-restarts
else
	fail_case upgrade-backs-up-before-the-api-restarts "rc=$rc backup=$backup order=$stop_line/$version_line/$dump_line/$up_line $output"
fi

# 9. A reinstall of the same release takes no backup.
new_case reinstall-takes-no-backup
seed_live "$case_dir" v9.9.9
seed_db "$case_dir" r1
seed_running "$case_dir"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" TARGET_KNOWN="r1")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && ! -e "$case_dir/install/backups" && "$(log_line 'pg_dump' "$case_dir")" -eq 0 ]]; then
	pass_case reinstall-takes-no-backup
else
	fail_case reinstall-takes-no-backup "rc=$rc $output"
fi

# 10. POTOCOLOM_SKIP_BACKUP=1 is named, and it takes no backup.
new_case skip-backup-variable-takes-none
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" r1
seed_running "$case_dir"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" TARGET_KNOWN="r1" POTOCOLOM_SKIP_BACKUP=1)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$output" == *'POTOCOLOM_SKIP_BACKUP=1'* && ! -e "$case_dir/install/backups" \
	&& "$(log_line 'pg_dump' "$case_dir")" -eq 0 ]]; then
	pass_case skip-backup-variable-takes-none
else
	fail_case skip-backup-variable-takes-none "rc=$rc $output"
fi

# 11. Not enough space refuses with the numbers, and the running services run again.
new_case space-refusal-restores-services
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" r1
seed_running "$case_dir"
make_bundle "$case_dir" v9.9.9
before="$(tree_hash "$case_dir/install")"
states_before="$(container_states "$case_dir")"
output="$(run_install "$case_dir" TARGET_KNOWN="r1 r2" DB_BYTES=100000000000 ASSET_BYTES=1000 AVAIL_BYTES=1000)" && rc=0 || rc=$?
after="$(tree_hash "$case_dir/install")"
states_after="$(container_states "$case_dir")"
if [[ "$rc" -ne 0 && "$output" == *'not enough free space'* && "$output" == *'POTOCOLOM_SKIP_BACKUP=1'* \
	&& "$output" == *'Need 111073742924 bytes'* && "$before" == "$after" && "$states_before" == "$states_after" \
	&& "$(grep -c '^api1 api running$' "$case_dir/state/containers")" -eq 1 \
	&& ! -e "$case_dir/install/backups" && -z "$(ls -A "$case_dir/tmp")" ]]; then
	pass_case space-refusal-restores-services
else
	fail_case space-refusal-restores-services "rc=$rc $output"
fi

# 12. A failed backup leaves $DIR, the backups and postgres as they were.
new_case failed-backup-leaves-dir-and-postgres
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" r1
seed_running "$case_dir"
make_bundle "$case_dir" v9.9.9
before="$(tree_hash "$case_dir/install")"
states_before="$(container_states "$case_dir")"
output="$(run_install "$case_dir" TARGET_KNOWN="r1 r2" BACKUP_FAIL=1)" && rc=0 || rc=$?
after="$(tree_hash "$case_dir/install")"
states_after="$(container_states "$case_dir")"
if [[ "$rc" -ne 0 && "$output" == *'the backup failed'* && "$before" == "$after" && "$states_before" == "$states_after" \
	&& ! -e "$case_dir/install/backups" \
	&& "$(grep -c '^api1 api running$' "$case_dir/state/containers")" -eq 1 \
	&& "$(grep -c '^pg1 postgres running$' "$case_dir/state/containers")" -eq 1 \
	&& -z "$(ls -A "$case_dir/tmp")" ]]; then
	pass_case failed-backup-leaves-dir-and-postgres
else
	fail_case failed-backup-leaves-dir-and-postgres "rc=$rc $output"
fi

# 13. An interrupted backup is cleaned up the same way.
new_case interrupted-backup-cleans-up
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" r1
seed_running "$case_dir"
make_bundle "$case_dir" v9.9.9
before="$(tree_hash "$case_dir/install")"
states_before="$(container_states "$case_dir")"
output="$(run_install "$case_dir" TARGET_KNOWN="r1 r2" BACKUP_KILL=1)" && rc=0 || rc=$?
after="$(tree_hash "$case_dir/install")"
states_after="$(container_states "$case_dir")"
if [[ "$rc" -ne 0 && "$before" == "$after" && "$states_before" == "$states_after" && ! -e "$case_dir/install/backups" \
	&& "$(grep -c '^api1 api running$' "$case_dir/state/containers")" -eq 1 \
	&& -z "$(ls -A "$case_dir/tmp")" ]]; then
	pass_case interrupted-backup-cleans-up
else
	fail_case interrupted-backup-cleans-up "rc=$rc $output"
fi

# 14. A database a newer release migrated is refused, and $DIR stays unchanged.
new_case downgrade-refused-leaves-dir-unchanged
seed_live "$case_dir" v10.0.0
seed_db "$case_dir" r2
seed_running "$case_dir"
make_bundle "$case_dir" v9.9.9
before="$(tree_hash "$case_dir/install")"
states_before="$(container_states "$case_dir")"
output="$(run_install "$case_dir" TARGET_KNOWN="r1")" && rc=0 || rc=$?
after="$(tree_hash "$case_dir/install")"
states_after="$(container_states "$case_dir")"
if [[ "$rc" -ne 0 && "$output" == *'Downgrading v10.0.0 to v9.9.9'* && "$output" == *'does not know'* \
	&& "$output" == *'forward fix'* && "$output" != *'Or restore'* && "$before" == "$after" \
	&& "$states_before" == "$states_after" \
	&& "$(grep -c '^api1 api running$' "$case_dir/state/containers")" -eq 1 \
	&& ! -e "$case_dir/install/backups" && -z "$(ls -A "$case_dir/tmp")" ]]; then
	pass_case downgrade-refused-leaves-dir-unchanged
else
	fail_case downgrade-refused-leaves-dir-unchanged "rc=$rc $output"
fi

# 14b. The revision probe runs on the installer's own image. The installer is stamped
# v9.9.9 with no shell version, and the saved .env names v10.0.0, which knows r2. Only
# the installer's image may decide, so it refuses and the database is left alone.
new_case downgrade-probe-asks-the-installer-image
seed_live "$case_dir" v10.0.0
seed_db "$case_dir" r2
seed_running "$case_dir"
make_bundle "$case_dir" v9.9.9
mkdir -p "$case_dir/stamped"
sed 's/VERSION="__POTOCOLOM_VERSION__"/VERSION="v9.9.9"/' "$repo/scripts/install.sh" >"$case_dir/stamped/install.sh"
before="$(tree_hash "$case_dir/install")"
states_before="$(container_states "$case_dir")"
output="$(INSTALLER_SCRIPT="$case_dir/stamped/install.sh" run_install "$case_dir" POTOCOLOM_VERSION= TARGET_KNOWN="r1" SAVED_IMAGE_KNOWN="r1 r2")" && rc=0 || rc=$?
after="$(tree_hash "$case_dir/install")"
states_after="$(container_states "$case_dir")"
if [[ "$rc" -ne 0 && "$output" == *'Downgrading v10.0.0 to v9.9.9'* && "$output" == *'does not know'* \
	&& "$(grep -c 'probe image=v9.9.9 revision=r2' "$case_dir/docker.log")" -eq 1 \
	&& "$before" == "$after" && "$states_before" == "$states_after" && ! -e "$case_dir/install/backups" ]]; then
	pass_case downgrade-probe-asks-the-installer-image
else
	fail_case downgrade-probe-asks-the-installer-image "rc=$rc $output"
fi

# 15. A revision that cannot be read refuses.
new_case unreadable-revision-refuses
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" r1
seed_running "$case_dir"
make_bundle "$case_dir" v9.9.9
before="$(tree_hash "$case_dir/install")"
states_before="$(container_states "$case_dir")"
output="$(run_install "$case_dir" TARGET_KNOWN="r1 r2" REVISION_FAIL=1)" && rc=0 || rc=$?
after="$(tree_hash "$case_dir/install")"
states_after="$(container_states "$case_dir")"
if [[ "$rc" -ne 0 && "$output" == *'could not read the database revision'* && "$before" == "$after" && "$states_before" == "$states_after" \
	&& "$(grep -c '^api1 api running$' "$case_dir/state/containers")" -eq 1 ]]; then
	pass_case unreadable-revision-refuses
else
	fail_case unreadable-revision-refuses "rc=$rc $output"
fi

# 16. A failure to ask the release whether it knows the revision refuses.
new_case target-check-failure-refuses
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" r1
seed_running "$case_dir"
make_bundle "$case_dir" v9.9.9
before="$(tree_hash "$case_dir/install")"
states_before="$(container_states "$case_dir")"
output="$(run_install "$case_dir" TARGET_KNOWN="r1 r2" TARGET_FAIL=1)" && rc=0 || rc=$?
after="$(tree_hash "$case_dir/install")"
states_after="$(container_states "$case_dir")"
if [[ "$rc" -ne 0 && "$output" == *'could not ask release v9.9.9'* && "$before" == "$after" && "$states_before" == "$states_after" ]]; then
	pass_case target-check-failure-refuses
else
	fail_case target-check-failure-refuses "rc=$rc $output"
fi

# 17. A database volume with no alembic_version table is empty, so it is not guarded.
new_case empty-database-is-not-guarded
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" none
seed_running "$case_dir"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" TARGET_KNOWN="")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && -n "$(backup_dirs "$case_dir" '^v8\.8\.8-none-[0-9]{8}T[0-9]{6}Z$')" ]]; then
	pass_case empty-database-is-not-guarded
else
	fail_case empty-database-is-not-guarded "rc=$rc $output"
fi

# 18. A stack stopped with down: postgres is created for the checks and removed on refusal.
new_case downgrade-refused-after-down
seed_live "$case_dir" v10.0.0
seed_db "$case_dir" r2
seed_containers "$case_dir"
make_bundle "$case_dir" v9.9.9
before="$(tree_hash "$case_dir/install")"
states_before="$(container_states "$case_dir")"
output="$(run_install "$case_dir" TARGET_KNOWN="r1")" && rc=0 || rc=$?
after="$(tree_hash "$case_dir/install")"
states_after="$(container_states "$case_dir")"
if [[ "$rc" -ne 0 && "$before" == "$after" && "$states_before" == "$states_after" && "$(log_line 'up -d --no-deps --wait --wait-timeout 120 postgres' "$case_dir")" -gt 0 \
	&& "$(log_line 'rm -sf postgres' "$case_dir")" -gt 0 && "$(grep -c postgres "$case_dir/state/containers")" -eq 0 ]]; then
	pass_case downgrade-refused-after-down
else
	fail_case downgrade-refused-after-down "rc=$rc $output"
fi

# 18b. A stack stopped with stop: postgres is started for the checks, and a refused downgrade stops it again.
new_case downgrade-refused-after-stop
seed_live "$case_dir" v10.0.0
seed_db "$case_dir" r2
seed_containers "$case_dir" "api1 api exited" "pg1 postgres exited" "wk1 worker exited"
make_bundle "$case_dir" v9.9.9
before="$(tree_hash "$case_dir/install")"
states_before="$(container_states "$case_dir")"
output="$(run_install "$case_dir" TARGET_KNOWN="r1")" && rc=0 || rc=$?
after="$(tree_hash "$case_dir/install")"
states_after="$(container_states "$case_dir")"
if [[ "$rc" -ne 0 && "$output" == *'does not know'* && "$before" == "$after" && "$states_before" == "$states_after" \
	&& "$(log_line 'up -d --no-deps --wait --wait-timeout 120 postgres' "$case_dir")" -gt 0 \
	&& "$(log_line 'stop postgres' "$case_dir")" -gt 0 && "$(log_line 'rm -sf postgres' "$case_dir")" -eq 0 ]]; then
	pass_case downgrade-refused-after-stop
else
	fail_case downgrade-refused-after-stop "rc=$rc $output"
fi

# 19. A stack stopped with stop: postgres is started for the checks, and the upgrade backs up.
new_case upgrade-after-stop-takes-backup
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" r1
seed_containers "$case_dir" "api1 api exited" "pg1 postgres exited" "wk1 worker exited"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" TARGET_KNOWN="r1 r2")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && -n "$(backup_dirs "$case_dir" '^v8\.8\.8-r1-')" \
	&& "$(log_line 'up -d --no-deps --wait --wait-timeout 120 postgres' "$case_dir")" -gt 0 \
	&& "$(log_line 'rm -sf postgres' "$case_dir")" -eq 0 && "$(log_line 'docker start' "$case_dir")" -eq 0 ]]; then
	pass_case upgrade-after-stop-takes-backup
else
	fail_case upgrade-after-stop-takes-backup "rc=$rc $output"
fi

# 19b. A stack stopped with down: postgres is created for the checks, the upgrade backs up, and nothing is removed.
new_case upgrade-after-down-takes-backup
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" r1
seed_containers "$case_dir"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" TARGET_KNOWN="r1 r2")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && -n "$(backup_dirs "$case_dir" '^v8\.8\.8-r1-')" \
	&& "$(log_line 'up -d --no-deps --wait --wait-timeout 120 postgres' "$case_dir")" -gt 0 \
	&& "$(log_line 'rm -sf postgres' "$case_dir")" -eq 0 && "$(log_line 'docker start' "$case_dir")" -eq 0 ]]; then
	pass_case upgrade-after-down-takes-backup
else
	fail_case upgrade-after-down-takes-backup "rc=$rc $output"
fi

# 20. The database session wait ends before the revision is read.
new_case session-wait-before-revision-read
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" r1
seed_running "$case_dir"
printf '2\n' >"$case_dir/state/sessions"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" TARGET_KNOWN="r1 r2")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(grep -c 'revision-read sessions=0 stuck=0' "$case_dir/docker.log")" -eq 1 \
	&& "$(log_line 'stop api1 wk1' "$case_dir")" -lt "$(log_line 'version_num' "$case_dir")" ]]; then
	pass_case session-wait-before-revision-read
else
	fail_case session-wait-before-revision-read "rc=$rc $output"
fi

# 21. A session that outlives the deadline is ended, and then the revision is read.
new_case session-wait-ends-stuck-backend
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" r1
seed_running "$case_dir"
touch "$case_dir/state/sessions_stuck"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" TARGET_KNOWN="r1 r2")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(log_line 'pg_terminate_backend' "$case_dir")" -gt 0 \
	&& "$(grep -c 'revision-read sessions=0 stuck=0' "$case_dir/docker.log")" -eq 1 ]]; then
	pass_case session-wait-ends-stuck-backend
else
	fail_case session-wait-ends-stuck-backend "rc=$rc $output"
fi

# 21b. The termination ends the stuck client session only: not the installer's own
# connection, and not the background processes.
new_case session-wait-ends-only-client-sessions
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" r1
seed_running "$case_dir"
touch "$case_dir/state/sessions_stuck"
printf '2\n' >"$case_dir/state/background"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" TARGET_KNOWN="r1 r2")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(grep -c 'terminated client=1 self=0 background=0' "$case_dir/docker.log")" -eq 1 ]]; then
	pass_case session-wait-ends-only-client-sessions
else
	fail_case session-wait-ends-only-client-sessions "rc=$rc $output"
fi

# 22. A backend that survives the termination refuses, and the services run again.
new_case session-wait-refuses-surviving-backend
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" r1
seed_running "$case_dir"
touch "$case_dir/state/sessions_stuck"
make_bundle "$case_dir" v9.9.9
before="$(tree_hash "$case_dir/install")"
states_before="$(container_states "$case_dir")"
output="$(run_install "$case_dir" TARGET_KNOWN="r1 r2" SESSIONS_SURVIVE=1)" && rc=0 || rc=$?
after="$(tree_hash "$case_dir/install")"
states_after="$(container_states "$case_dir")"
if [[ "$rc" -ne 0 && "$output" == *'client sessions are still open'* && "$before" == "$after" && "$states_before" == "$states_after" \
	&& "$(grep -c '^api1 api running$' "$case_dir/state/containers")" -eq 1 ]]; then
	pass_case session-wait-refuses-surviving-backend
else
	fail_case session-wait-refuses-surviving-backend "rc=$rc $output"
fi

# 23. A migration that commits before readiness fails blocks the older release.
new_case migration-then-readiness-failure-blocks-old-release
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" r1
seed_running "$case_dir"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" TARGET_KNOWN="r1 r2" MIGRATE_TO=r2 READY_FAIL=1)" && rc=0 || rc=$?
before="$(tree_hash "$case_dir/install")"
states_before="$(container_states "$case_dir")"
make_bundle "$case_dir" v8.8.8
output2="$(run_install "$case_dir" POTOCOLOM_VERSION=v8.8.8 TARGET_KNOWN="r1")" && rc2=0 || rc2=$?
after="$(tree_hash "$case_dir/install")"
states_after="$(container_states "$case_dir")"
if [[ "$rc" -ne 0 && "$output" == *'did not become ready'* && "$(cat "$case_dir/state/revision")" == r2 \
	&& "$rc2" -ne 0 && "$output2" == *'Downgrading v9.9.9 to v8.8.8'* && "$output2" == *'does not know'* \
	&& "$before" == "$after" && "$states_before" == "$states_after" ]]; then
	pass_case migration-then-readiness-failure-blocks-old-release
else
	fail_case migration-then-readiness-failure-blocks-old-release "rc=$rc/$rc2 $output $output2"
fi

# 24. A migration that rolled back leaves a revision the older release knows, so it is allowed.
new_case rolled-back-migration-allows-old-release
seed_live "$case_dir" v9.9.9
seed_db "$case_dir" r1
seed_running "$case_dir"
make_bundle "$case_dir" v8.8.8
output="$(run_install "$case_dir" POTOCOLOM_VERSION=v8.8.8 TARGET_KNOWN="r1")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$output" == *'Downgrading v9.9.9 to v8.8.8'* && -n "$(backup_dirs "$case_dir" '^v9\.9\.9-r1-')" ]]; then
	pass_case rolled-back-migration-allows-old-release
else
	fail_case rolled-back-migration-allows-old-release "rc=$rc $output"
fi

# 25. The release-check rehearsal shape: candidate, down with volumes removed,
# previous release on an empty database, then the candidate again.
new_case release-check-downgrade-shape
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" TARGET_KNOWN="r1 r2" MIGRATE_TO=r2)" && rc=0 || rc=$?
step1=$rc
seed_containers "$case_dir"
rm -f "$case_dir/state/pgdata" "$case_dir/state/revision"
make_bundle "$case_dir" v8.8.8
output2="$(run_install "$case_dir" POTOCOLOM_VERSION=v8.8.8 TARGET_KNOWN="r0 r1" MIGRATE_TO=r1)" && rc2=0 || rc2=$?
seed_containers "$case_dir" "api1 api running" "pg1 postgres running"
make_bundle "$case_dir" v9.9.9
output3="$(run_install "$case_dir" TARGET_KNOWN="r1 r2" MIGRATE_TO=r2)" && rc3=0 || rc3=$?
if [[ "$step1" -eq 0 && "$rc2" -eq 0 && "$rc3" -eq 0 && "$output2" == *'Downgrading v9.9.9 to v8.8.8'* \
	&& -n "$(backup_dirs "$case_dir" '^v8\.8\.8-r1-')" && "$(cat "$case_dir/state/revision")" == r2 ]]; then
	pass_case release-check-downgrade-shape
else
	fail_case release-check-downgrade-shape "rc=$step1/$rc2/$rc3 $output $output2 $output3"
fi

# 26. A refused downgrade prints a restore command that runs as printed, starts the
# worker of the release being installed, and needs no COMPOSE_PROFILES in the backup's
# own env file. The printed line sets POTOCOLOM_VERSION to that release, which knows the
# backup's revision, so the restored project starts it and not the release the backup names.
new_case printed-restore-command-starts-installing-release
seed_live "$case_dir" v10.0.0
seed_db "$case_dir" r2
seed_running "$case_dir"
bdir="$case_dir/install/backups/v8.8.8-r1-20260101T000000Z"
mkdir -p "$bdir"
printf 'DATABASE-DUMP\n' >"$bdir/database.dump"
printf 'ASSETS-TAR\n' >"$bdir/assets.tar"
printf 'POTOCOLOM_VERSION=v8.8.8\nPOSTGRES_PASSWORD=keep-db\nFLEET_SECRET=keep-fleet\n' >"$bdir/env"
cp "$repo/deploy/compose/compose.yml" "$bdir/compose.yml"
(cd "$bdir" && sha256sum database.dump assets.tar env >SHA256SUMS)
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" TARGET_KNOWN="r1")" && rc=0 || rc=$?
line="$(printf '%s\n' "$output" | grep -E '^    COMPOSE_PROFILES=.*restore\.sh' | head -n 1 | sed 's/^ *//')"
restore_out=""
restore_rc=0
if [[ -n "$line" ]]; then
	restore_out="$(env -u COMPOSE_PROFILES -u POTOCOLOM_VERSION PATH="$case_dir/bin:$PATH" STATE_DIR="$case_dir/state" \
		DOCKER_LOG="$case_dir/docker.log" RELEASE_TEST_AWK="$RELEASE_TEST_AWK" bash -c "$line" 2>&1)" || restore_rc=$?
fi
# The release the restore's own up started, as recorded by the docker stub.
started="$(grep '^up-release ' "$case_dir/docker.log" | tail -n 1)"
if [[ "$rc" -ne 0 && -n "$line" && "$restore_rc" -eq 0 && "$restore_out" == *'restored potocolom-restored'* \
	&& "$started" == 'up-release v9.9.9 profiles=gpu' \
	&& "$(grep -E 'potocolom-restored .*up -d \|' "$case_dir/docker.log" | tail -n 1)" == *'COMPOSE_PROFILES=gpu'* \
	&& "$(grep -E 'potocolom-restored .*up -d \|' "$case_dir/docker.log" | tail -n 1)" == *"$bdir/compose.yml"* ]]; then
	pass_case printed-restore-command-starts-installing-release
else
	fail_case printed-restore-command-starts-installing-release "rc=$rc restore_rc=$restore_rc line=$line $output $restore_out"
fi

# 26b. A refused downgrade names the shared host cache when the backup's env names
# HF_CACHE_DIR, and says the models volume is empty when the env does not name it.
seed_qualifying_backup() {  # CASE_DIR EXTRA_ENV_LINE
	local bdir="$1/install/backups/v8.8.8-r1-20260101T000000Z"
	mkdir -p "$bdir"
	printf 'DATABASE-DUMP\n' >"$bdir/database.dump"
	printf 'ASSETS-TAR\n' >"$bdir/assets.tar"
	printf 'POTOCOLOM_VERSION=v8.8.8\nPOSTGRES_PASSWORD=keep-db\nFLEET_SECRET=keep-fleet\n' >"$bdir/env"
	if [[ -n "$2" ]]; then printf '%s\n' "$2" >>"$bdir/env"; fi
	cp "$repo/deploy/compose/compose.yml" "$bdir/compose.yml"
	(cd "$bdir" && sha256sum database.dump assets.tar env >SHA256SUMS)
}

new_case restore-message-names-shared-cache
seed_live "$case_dir" v10.0.0
seed_db "$case_dir" r2
seed_running "$case_dir"
seed_qualifying_backup "$case_dir" "HF_CACHE_DIR=$case_dir/hub"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" TARGET_KNOWN="r1")" && rc=0 || rc=$?
if [[ "$rc" -ne 0 && "$output" == *'    COMPOSE_PROFILES='*'restore.sh'* \
	&& "$output" == *"The restored project shares the host model cache $case_dir/hub again, so removing a model there deletes its files in that cache."* \
	&& "$output" != *'empty models volume'* ]]; then
	pass_case restore-message-names-shared-cache
else
	fail_case restore-message-names-shared-cache "rc=$rc $output"
fi

new_case restore-message-without-shared-cache
seed_live "$case_dir" v10.0.0
seed_db "$case_dir" r2
seed_running "$case_dir"
seed_qualifying_backup "$case_dir" ""
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" TARGET_KNOWN="r1")" && rc=0 || rc=$?
if [[ "$rc" -ne 0 && "$output" == *'    COMPOSE_PROFILES='*'restore.sh'* \
	&& "$output" == *'The restored project starts with an empty models volume.'* \
	&& "$output" != *'shares the host model cache'* ]]; then
	pass_case restore-message-without-shared-cache
else
	fail_case restore-message-without-shared-cache "rc=$rc $output"
fi

# 27. Preflight takes the disk it measures from the install directory it is given.
new_case preflight-measures-the-install-dir
seed_live "$case_dir" v8.8.8
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" TARGET_KNOWN="")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 ]] && grep -qx -- "-BG --output=avail $case_dir/install" "$case_dir/state/df.log"; then
	pass_case preflight-measures-the-install-dir
else
	fail_case preflight-measures-the-install-dir "rc=$rc $(cat "$case_dir/state/df.log" 2>/dev/null)"
fi

# 28. Preflight reads PUBLIC_URL from the env file it is given, not from its own root.
new_case preflight-reads-the-env-file-it-is-given
root="$case_dir/preroot"
mkdir -p "$root/scripts" "$root/deploy/compose"
cp "$repo/scripts/preflight.sh" "$repo/scripts/ensure-env.sh" "$repo/scripts/install-lock.sh" \
	"$repo/scripts/gpu-profile.sh" "$root/scripts/"
cp "$repo/deploy/compose/compose.yml" "$repo/deploy/compose/.env.example" "$root/deploy/compose/"
printf 'PUBLIC_URL=http://localhost:8080\n' >"$case_dir/elsewhere.env"
output="$(env -u POTOCOLOM_PUBLIC_URL PATH="$case_dir/bin:$PATH" STATE_DIR="$case_dir/state" \
	DOCKER_LOG="$case_dir/docker.log" RELEASE_TEST_AWK="$RELEASE_TEST_AWK" REAL_ID="$REAL_ID" \
	POTOCOLOM_KFD="$case_dir/no-kfd" POTOCOLOM_PROFILE=api ENV_FILE="$case_dir/elsewhere.env" \
	bash "$root/scripts/preflight.sh" 2>&1)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$output" == *'PUBLIC_URL is http://localhost:8080'* && ! -e "$root/deploy/compose/.env" ]]; then
	pass_case preflight-reads-the-env-file-it-is-given
else
	fail_case preflight-reads-the-env-file-it-is-given "rc=$rc $output"
fi

# 29. A restarting API is stopped for the checks, and a refusal starts it again.
new_case restarting-api-stopped-and-restored
seed_live "$case_dir" v10.0.0
seed_db "$case_dir" r2
seed_containers "$case_dir" "api1 api restarting" "pg1 postgres running" "wk1 worker running"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" TARGET_KNOWN="r1")" && rc=0 || rc=$?
if [[ "$rc" -ne 0 && "$output" == *'does not know'* && "$(log_line 'stop api1 wk1' "$case_dir")" -gt 0 \
	&& "$(grep -c '^api1 api running$' "$case_dir/state/containers")" -eq 1 \
	&& "$(grep -c '^wk1 worker running$' "$case_dir/state/containers")" -eq 1 ]]; then
	pass_case restarting-api-stopped-and-restored
else
	fail_case restarting-api-stopped-and-restored "rc=$rc $output"
fi

# 30. Background processes and the installer's own connection do not hold up the session wait.
new_case session-wait-ignores-own-and-background
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" r1
seed_running "$case_dir"
printf '2\n' >"$case_dir/state/background"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" TARGET_KNOWN="r1 r2")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(log_line 'pg_terminate_backend' "$case_dir")" -eq 0 \
	&& "$(grep -c 'revision-read sessions=0 stuck=0' "$case_dir/docker.log")" -eq 1 ]]; then
	pass_case session-wait-ignores-own-and-background
else
	fail_case session-wait-ignores-own-and-background "rc=$rc $output"
fi

# 31. A volume whose name only contains the database volume name is not the database.
new_case volume-name-match-is-exact
seed_live "$case_dir" v8.8.8
seed_containers "$case_dir" "api1 api running" "wk1 worker running"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" DECOY_VOLUME=1)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(log_line 'stop api1 wk1' "$case_dir")" -eq 0 && ! -e "$case_dir/install/backups" ]]; then
	pass_case volume-name-match-is-exact
else
	fail_case volume-name-match-is-exact "rc=$rc $output"
fi

# 32. A failed volume listing refuses before anything is stopped or changed.
new_case volume-listing-failure-refuses
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" r1
seed_running "$case_dir"
make_bundle "$case_dir" v9.9.9
before="$(tree_hash "$case_dir/install")"
states_before="$(container_states "$case_dir")"
output="$(run_install "$case_dir" DOCKER_VOLUME_LS_FAIL=1)" && rc=0 || rc=$?
after="$(tree_hash "$case_dir/install")"
if [[ "$rc" -ne 0 && "$output" == *'could not list the Docker volumes'* && "$before" == "$after" \
	&& "$states_before" == "$(container_states "$case_dir")" && "$(log_line 'stop api1' "$case_dir")" -eq 0 \
	&& ! -e "$case_dir/install/backups" ]]; then
	pass_case volume-listing-failure-refuses
else
	fail_case volume-listing-failure-refuses "rc=$rc $output"
fi

# 33. The same failure on a fresh directory refuses before a .env is written.
new_case volume-listing-failure-refuses-before-env
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" DOCKER_VOLUME_LS_FAIL=1)" && rc=0 || rc=$?
if [[ "$rc" -ne 0 && "$output" == *'could not list the Docker volumes'* && ! -e "$case_dir/install/deploy/compose/.env" ]]; then
	pass_case volume-listing-failure-refuses-before-env
else
	fail_case volume-listing-failure-refuses-before-env "rc=$rc $output"
fi

# 34. The printed restore command quotes the install directory, so a $ in the path survives a paste.
new_case printed-restore-command-quotes-the-path
seed_live "$case_dir" v10.0.0
seed_db "$case_dir" r2
seed_running "$case_dir"
bdir="$case_dir/install/backups/v8.8.8-r1-20260101T000000Z"
mkdir -p "$bdir"
printf 'DATABASE-DUMP\n' >"$bdir/database.dump"
printf 'ASSETS-TAR\n' >"$bdir/assets.tar"
printf 'POSTGRES_PASSWORD=keep-db\nFLEET_SECRET=keep-fleet\n' >"$bdir/env"
cp "$repo/deploy/compose/compose.yml" "$bdir/compose.yml"
(cd "$bdir" && sha256sum database.dump assets.tar env >SHA256SUMS)
make_bundle "$case_dir" v9.9.9
ln -s "$case_dir/install" "$case_dir/poto\$cash"
output="$(run_install "$case_dir" POTOCOLOM_DIR="$case_dir/poto\$cash" TARGET_KNOWN="r1")" && rc=0 || rc=$?
line="$(printf '%s\n' "$output" | grep -E '^    COMPOSE_PROFILES=.*restore\.sh' | head -n 1 | sed 's/^ *//')"
restore_out=""
restore_rc=0
if [[ -n "$line" ]]; then
	restore_out="$(env -u COMPOSE_PROFILES PATH="$case_dir/bin:$PATH" STATE_DIR="$case_dir/state" \
		DOCKER_LOG="$case_dir/docker.log" RELEASE_TEST_AWK="$RELEASE_TEST_AWK" bash -c "$line" 2>&1)" || restore_rc=$?
fi
if [[ "$rc" -ne 0 && -n "$line" && "$restore_rc" -eq 0 && "$restore_out" == *'restored potocolom-restored'* \
	&& "$(grep -E 'potocolom-restored .*up -d \|' "$case_dir/docker.log" | tail -n 1)" == *"$case_dir/poto\$cash/backups/v8.8.8-r1-20260101T000000Z/compose.yml"* ]]; then
	pass_case printed-restore-command-quotes-the-path
else
	fail_case printed-restore-command-quotes-the-path "rc=$rc restore_rc=$restore_rc line=$line $output $restore_out"
fi

# 35. A database volume with no .env: a failed volume listing refuses before a .env is written.
new_case volume-listing-failure-refuses-with-database-and-no-env
seed_db "$case_dir" r1
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" DOCKER_VOLUME_LS_FAIL=1)" && rc=0 || rc=$?
if [[ "$rc" -ne 0 && "$output" == *'could not list the Docker volumes'* && ! -e "$case_dir/install/deploy/compose/.env" ]]; then
	pass_case volume-listing-failure-refuses-with-database-and-no-env
else
	fail_case volume-listing-failure-refuses-with-database-and-no-env "rc=$rc $output"
fi

# 36. A database with tables but no alembic_version table is refused, not read as empty.
new_case database-without-alembic-table-refuses
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" none
printf '3\n' >"$case_dir/state/tables"
seed_running "$case_dir"
make_bundle "$case_dir" v9.9.9
before="$(tree_hash "$case_dir/install")"
states_before="$(container_states "$case_dir")"
output="$(run_install "$case_dir" TARGET_KNOWN="")" && rc=0 || rc=$?
after="$(tree_hash "$case_dir/install")"
states_after="$(container_states "$case_dir")"
if [[ "$rc" -ne 0 && "$output" == *'no alembic_version table'* && "$before" == "$after" \
	&& "$states_before" == "$states_after" && ! -e "$case_dir/install/backups" \
	&& "$(log_line 'pg_dump' "$case_dir")" -eq 0 \
	&& "$(grep -c '^api1 api running$' "$case_dir/state/containers")" -eq 1 ]]; then
	pass_case database-without-alembic-table-refuses
else
	fail_case database-without-alembic-table-refuses "rc=$rc $output"
fi

# 37. A missing assets volume measures as 0, and measuring it does not create it.
new_case missing-assets-volume-measures-zero
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" r1
seed_running "$case_dir"
touch "$case_dir/state/no_assets"
make_bundle "$case_dir" v9.9.9
before="$(tree_hash "$case_dir/install")"
states_before="$(container_states "$case_dir")"
output="$(run_install "$case_dir" TARGET_KNOWN="r1 r2" DB_BYTES=100000000000 AVAIL_BYTES=1000)" && rc=0 || rc=$?
after="$(tree_hash "$case_dir/install")"
states_after="$(container_states "$case_dir")"
if [[ "$rc" -ne 0 && "$output" == *'Need 111073741824 bytes: database 100000000000, assets 0,'* \
	&& "$(grep -c 'du -sb /src' "$case_dir/docker.log")" -eq 0 \
	&& "$(grep -c 'src=potocolom_assets' "$case_dir/docker.log")" -eq 0 \
	&& "$before" == "$after" && "$states_before" == "$states_after" ]]; then
	pass_case missing-assets-volume-measures-zero
else
	fail_case missing-assets-volume-measures-zero "rc=$rc $output"
fi

# 38. A backup destination that already exists is refused, and the recovery point in it is kept.
new_case existing-backup-destination-refuses
seed_live "$case_dir" v8.8.8
seed_db "$case_dir" r1
seed_running "$case_dir"
make_bundle "$case_dir" v9.9.9
mkdir -p "$case_dir/install/backups/v8.8.8-r1-20260101T000000Z"
printf 'EARLIER\n' >"$case_dir/install/backups/v8.8.8-r1-20260101T000000Z/database.dump"
states_before="$(container_states "$case_dir")"
output="$(run_install "$case_dir" TARGET_KNOWN="r1 r2" FAKE_STAMP=20260101T000000Z BACKUP_FAIL=1)" && rc=0 || rc=$?
states_after="$(container_states "$case_dir")"
if [[ "$rc" -ne 0 && "$output" == *'already exists'* \
	&& "$(cat "$case_dir/install/backups/v8.8.8-r1-20260101T000000Z/database.dump")" == EARLIER \
	&& "$(log_line 'pg_dump' "$case_dir")" -eq 0 && "$states_before" == "$states_after" \
	&& "$(grep -c '^api1 api running$' "$case_dir/state/containers")" -eq 1 ]]; then
	pass_case existing-backup-destination-refuses
else
	fail_case existing-backup-destination-refuses "rc=$rc $output"
fi

# 39. A rerun after an upgrade that never became ready takes a backup, even at the same version.
new_case rerun-after-unready-upgrade-takes-backup
seed_live "$case_dir" v9.9.9
seed_db "$case_dir" r1
seed_running "$case_dir"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" TARGET_KNOWN="r1 r2" MIGRATE_TO=r2 READY_FAIL=1)" && rc=0 || rc=$?
first_marker=no
if [[ -e "$case_dir/install/.upgrade-incomplete" ]]; then first_marker=yes; fi
first_backups=no
if [[ -e "$case_dir/install/backups" ]]; then first_backups=yes; fi
output2="$(run_install "$case_dir" TARGET_KNOWN="r2")" && rc2=0 || rc2=$?
if [[ "$rc" -ne 0 && "$output" == *'did not become ready'* && "$first_marker" == yes && "$first_backups" == no \
	&& "$rc2" -eq 0 && -n "$(backup_dirs "$case_dir" '^v9\.9\.9-r2-[0-9]{8}T[0-9]{6}Z$')" \
	&& ! -e "$case_dir/install/.upgrade-incomplete" ]]; then
	pass_case rerun-after-unready-upgrade-takes-backup
else
	fail_case rerun-after-unready-upgrade-takes-backup "rc=$rc/$rc2 $output $output2"
fi

# 40. A worker removal that fails after the swap starts again the services that were stopped.
new_case failed-worker-removal-restores-services
seed_live "$case_dir" v8.8.8 gpu
seed_db "$case_dir" r1
seed_running "$case_dir"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" POTOCOLOM_PROFILE=api TARGET_KNOWN="r1 r2" RM_FAIL=1)" && rc=0 || rc=$?
if [[ "$rc" -ne 0 && "$(log_line 'up -d --no-build' "$case_dir")" -eq 0 \
	&& "$(grep -c '^api1 api running$' "$case_dir/state/containers")" -eq 1 \
	&& "$(grep -c '^wk1 worker running$' "$case_dir/state/containers")" -eq 1 \
	&& -z "$(ls -A "$case_dir/tmp")" ]]; then
	pass_case failed-worker-removal-restores-services
else
	fail_case failed-worker-removal-restores-services "rc=$rc $output"
fi

# 41. The printed Stop command names the project and the env file, each quoted for a paste.
new_case stop-line-names-project-and-env-file
seed_live "$case_dir" v8.8.8
printf 'COMPOSE_PROJECT_NAME=my-studio\n' >>"$case_dir/install/deploy/compose/.env"
ln -s "$case_dir/install" "$case_dir/poto\$cash"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" POTOCOLOM_DIR="$case_dir/poto\$cash" TARGET_KNOWN="")" && rc=0 || rc=$?
dir="$case_dir/poto\$cash"
expected="Stop: docker compose -p my-studio --env-file $(printf '%q' "$dir/deploy/compose/.env") -f $(printf '%q' "$dir/deploy/compose/compose.yml") --profile gpu down"
if [[ "$rc" -eq 0 ]] && grep -qxF -- "$expected" <<<"$output"; then
	pass_case stop-line-names-project-and-env-file
else
	fail_case stop-line-names-project-and-env-file "rc=$rc expected=$expected $output"
fi

if ((failures > 0)); then
	printf '%d upgrade check(s) failed\n' "$failures"
	exit 1
fi
