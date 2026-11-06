#!/usr/bin/env bash
# Install a released potocolom: download the self-host bundle from the GitHub
# release, verify it against SHA256SUMS, run preflight, then pull the published
# images and start the stack. The release workflow writes its tag into the
# VERSION below before uploading this file.
set -euo pipefail

# Piped from curl, bash runs lines as they arrive: wrapping everything in main
# means a cut-off download defines a function and runs nothing.
# Opens the setup link in a desktop browser. The token never goes on a command
# line: it is written into a page that only this user can read, and xdg-open is
# given that page's path. The page is removed after the browser has read it
# (POTOCOLOM_SETUP_PAGE_SECONDS, default 30). Without a desktop session or
# xdg-open, the link printed above is the only copy.
open_setup_link() {
	local link="$1" dir page safe
	command -v xdg-open >/dev/null 2>&1 || return 0
	[[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] || return 0
	# mktemp -d makes a directory only this user can enter.
	dir="$(mktemp -d 2>/dev/null)" || return 0
	page="$dir/setup.html"
	safe="${link//&/&amp;}"
	safe="${safe//\"/&quot;}"
	# The umask is set before the file exists, so it is never world-readable.
	if ! (
		umask 077
		printf '<!doctype html>\n<meta charset="utf-8">\n<meta http-equiv="refresh" content="0; url=%s">\n<title>potocolom setup</title>\n<a href="%s">Continue to setup</a>\n' "$safe" "$safe" >"$page"
	); then
		rm -rf -- "$dir"
		return 0
	fi
	xdg-open "$page" >/dev/null 2>&1 || true
	( sleep "${POTOCOLOM_SETUP_PAGE_SECONDS:-30}"; rm -rf -- "$dir" ) >/dev/null 2>&1 &
}

# Prints the worker profile that this project's existing worker containers
# imply. Installs made before the profile was saved in .env have no other record.
# Both workers at once is ambiguous, so it refuses.
project_worker_profile() {
	local project="$1" containers id service state has_gpu=false has_rocm=false
	containers="$(docker ps -a --filter "label=com.docker.compose.project=$project" \
		--format '{{.ID}} {{.Label "com.docker.compose.service"}} {{.State}}')" || return 1
	while read -r id service state; do
		if [[ "$service" == worker ]]; then has_gpu=true; fi
		if [[ "$service" == worker-rocm ]]; then has_rocm=true; fi
	done <<<"$containers"
	if [[ "$has_gpu" == true && "$has_rocm" == true ]]; then
		echo "error: $project has both the gpu and the rocm worker containers; set POTOCOLOM_PROFILE=gpu or rocm, then run this again" >&2
		return 1
	fi
	if [[ "$has_gpu" == true ]]; then
		printf 'gpu\n'
	elif [[ "$has_rocm" == true ]]; then
		printf 'rocm\n'
	fi
	return 0
}

# Checks that the database's Alembic revision is known to the release being
# installed. The release's own Alembic scripts answer, so no database connection
# is made. backend/tests/test_install_revision_probe.py runs it.
REVISION_PROBE="import sys
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import CommandError
script = ScriptDirectory.from_config(Config('/app/alembic.ini'))
try:
    script.get_revision(sys.argv[1])
except CommandError:
    print('unknown')
else:
    print('known')"

# Globals shared by the live-install steps below. QUIESCED is set while the
# running services are stopped; install_exit puts back what was recorded here.
QUIESCED=0
RUNNING_IDS=()
PG_STARTED=0
PG_CREATED=0
BACKUP_PARTIAL=""
BACKUPS_CREATED=0

live_compose() {
	docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f "$DIR/deploy/compose/compose.yml" "$@"
}

live_psql() {
	live_compose exec -T postgres psql -U potocolom -d potocolom -Atc "$1"
}

client_sessions() {
	live_psql "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()" 2>/dev/null || echo unknown
}

# Returns 0 when the database volume exists and 1 when it does not. A failed
# listing ends the install: an unanswered question must not read as "no database".
database_volume_exists() {
	local listed
	if ! listed="$(docker volume ls -q --filter "name=${PROJECT}_pgdata")"; then
		echo "error: could not list the Docker volumes to look for ${PROJECT}_pgdata; nothing was changed" >&2
		exit 1
	fi
	grep -Fxq -- "${PROJECT}_pgdata" <<<"$listed"
}

# Stops the running containers of this project except postgres, and starts
# postgres when it is down. What it changes is recorded, so install_exit can
# put it back when the upgrade is refused or fails before the swap.
quiesce_project() {
	local containers id service state pg_state=""
	QUIESCED=1
	trap install_exit EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM
	containers="$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" \
		--format '{{.ID}} {{.Label "com.docker.compose.service"}} {{.State}}')"
	while read -r id service state; do
		if [[ -z "$id" ]]; then continue; fi
		if [[ "$service" == postgres ]]; then pg_state="$state"; fi
		# A restarting container can run a migration between attempts, so it is stopped too.
		if [[ "$service" != postgres && ( "$state" == running || "$state" == restarting ) ]]; then RUNNING_IDS+=("$id"); fi
	done <<<"$containers"
	if ((${#RUNNING_IDS[@]})); then
		echo "stopping the API and workers of $PROJECT for the upgrade checks"
		docker stop "${RUNNING_IDS[@]}" >/dev/null
	fi
	if [[ "$pg_state" != running ]]; then
		if [[ -z "$pg_state" ]]; then PG_CREATED=1; fi
		PG_STARTED=1
		echo "starting postgres of $PROJECT for the upgrade checks"
		live_compose up -d --no-deps --wait --wait-timeout 120 postgres >/dev/null
	fi
}

# Puts back what quiesce_project changed: starts the containers it stopped,
# and removes or stops postgres when this run created or started it.
restore_quiesced() {
	local status=0
	if ((${#RUNNING_IDS[@]})); then
		docker start "${RUNNING_IDS[@]}" >/dev/null || status=1
	fi
	if [[ "$PG_CREATED" == 1 ]]; then
		live_compose rm -sf postgres >/dev/null 2>&1 || status=1
	elif [[ "$PG_STARTED" == 1 ]]; then
		live_compose stop postgres >/dev/null 2>&1 || status=1
	fi
	if ((status != 0)); then
		echo "error: could not restore every service the upgrade stopped; start them with: docker start ${RUNNING_IDS[*]:-}" >&2
	fi
	return "$status"
}

# Waits until no client session is left. A session that outlives the deadline
# is ended, and the wait then runs for a bounded time more. Any failure to
# count the sessions counts as still open.
wait_for_sessions() {
	local attempt
	for ((attempt = 0; attempt < 30; attempt++)); do
		if [[ "$(client_sessions)" == 0 ]]; then return 0; fi
		sleep 1
	done
	echo "warning: database client sessions are still open; ending them" >&2
	live_psql "SELECT count(*) FROM (SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()) AS ended" >/dev/null || true
	for ((attempt = 0; attempt < 10; attempt++)); do
		if [[ "$(client_sessions)" == 0 ]]; then return 0; fi
		sleep 1
	done
	return 1
}

# Prints the Alembic revision of the database. An empty database (no
# alembic_version table) prints nothing and succeeds. Any other failure, and a
# table with no row, fails: the revision must be read or the upgrade is refused.
database_revision() {
	local has_table revision
	has_table="$(live_psql "SELECT to_regclass('public.alembic_version') IS NOT NULL")" || return 1
	case "$has_table" in
	f) return 0 ;;
	t) ;;
	*) return 1 ;;
	esac
	revision="$(live_psql "SELECT version_num FROM alembic_version")" || return 1
	[[ "$revision" =~ ^[A-Za-z0-9_]+$ ]] || return 1
	printf '%s\n' "$revision"
}

# Asks the release being installed whether it knows a revision. Prints known,
# unknown, or unclear. Unclear covers every failure to ask, and the caller
# refuses it.
target_revision_verdict() {
	local answer
	if answer="$(POTOCOLOM_VERSION="$VERSION" "${stage_compose[@]}" run --rm --no-deps -T api python -c "$REVISION_PROBE" "$1" 2>/dev/null)"; then
		case "$answer" in
		known | unknown)
			printf '%s\n' "$answer"
			return 0
			;;
		esac
	fi
	printf 'unclear\n'
}

# Refuses a database that a newer release migrated. Prints the forward fix, and
# the restore command for the newest backup this release knows, when there is one.
refuse_newer_database() {
	local revision="$1" name rev newest="" verdict
	local pattern='^(v[0-9]+\.[0-9]+\.[0-9]+|source)-([A-Za-z0-9_]+)-[0-9]{8}T[0-9]{6}Z$'
	echo "error: the database is at Alembic revision $revision, which release $VERSION does not know." >&2
	echo "  A newer release migrated it. Running $VERSION on it could damage data, so the upgrade is refused." >&2
	echo "  Nothing in $DIR changed." >&2
	echo "  The forward fix: rerun the install.sh of the newer release." >&2
	while IFS= read -r name; do
		if [[ ! "$name" =~ $pattern ]]; then continue; fi
		rev="${BASH_REMATCH[2]}"
		verdict="$(target_revision_verdict "$rev")"
		if [[ "$verdict" == known ]]; then
			newest="$DIR/backups/$name"
			break
		fi
	done < <(ls -1 "$DIR/backups" 2>/dev/null | sort -t- -k3,3r)
	if [[ -n "$newest" ]]; then
		echo "  Or restore the newest backup that $VERSION knows, into a new project:" >&2
		echo "    docker compose -p $(printf '%q' "$PROJECT") --env-file $(printf '%q' "$ENV_FILE") -f $(printf '%q' "$DIR/deploy/compose/compose.yml") down" >&2
		echo "    COMPOSE_PROFILES=$(printf '%q' "$profile") COMPOSE_FILE=$(printf '%q' "$newest/compose.yml") COMPOSE_PROJECT_NAME=$(printf '%q' "${PROJECT}-restored") $(printf '%q' "$DIR/scripts/restore.sh") $(printf '%q' "$newest")" >&2
		echo "  The restored project starts with an empty model cache." >&2
	fi
	exit 1
}

# Takes the recovery point before the swap. The backup directory name carries
# the saved version and the database revision, so a restore can pick a backup
# that the release it is restored with knows.
take_backup() {
	local revision="$1" db_bytes assets_bytes avail need stamp old dest
	if [[ "${POTOCOLOM_SKIP_BACKUP:-0}" == 1 ]]; then
		echo "warning: POTOCOLOM_SKIP_BACKUP=1, so this upgrade takes no backup and has no recovery point" >&2
		return 0
	fi
	db_bytes="$(live_psql "SELECT pg_database_size('potocolom')")" || db_bytes=""
	assets_bytes="$(docker run --rm --mount "type=volume,src=${PROJECT}_assets,dst=/src,readonly" \
		postgres:16 du -sb /src | awk '{print $1}')" || assets_bytes=""
	avail="$(df -B1 --output=avail "$DIR" | tail -n 1 | tr -dc '0-9')"
	if [[ ! "$db_bytes" =~ ^[0-9]+$ || ! "$assets_bytes" =~ ^[0-9]+$ || -z "$avail" ]]; then
		echo "error: could not measure the database, the assets or the free space for a backup; nothing was changed" >&2
		exit 1
	fi
	need=$(((db_bytes + assets_bytes) * 110 / 100 + 1073741824))
	if ((avail < need)); then
		echo "error: not enough free space for a backup in $DIR/backups." >&2
		echo "  Need $need bytes: database $db_bytes, assets $assets_bytes, plus 10 percent and 1 GiB. Free: $avail bytes." >&2
		echo "  Free some space, or run this again with POTOCOLOM_SKIP_BACKUP=1 to upgrade with no backup." >&2
		exit 1
	fi
	stamp="$(date -u +%Y%m%dT%H%M%SZ)"
	if [[ "$saved_version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then old="$saved_version"; else old=source; fi
	if [[ ! -d "$DIR/backups" ]]; then
		mkdir -p "$DIR/backups"
		BACKUPS_CREATED=1
	fi
	dest="$DIR/backups/$old-$revision-$stamp"
	BACKUP_PARTIAL="$dest"
	echo "backing up $PROJECT to $dest"
	if ! ENV_FILE="$ENV_FILE" COMPOSE_FILE="$DIR/deploy/compose/compose.yml" COMPOSE_PROJECT_NAME="$PROJECT" \
		bash "$STAGE/scripts/backup.sh" "$dest"; then
		echo "error: the backup failed; nothing was changed" >&2
		exit 1
	fi
	# restore.sh takes its compose file from its own release, which the swap
	# replaces, so the backup keeps the compose file it was made with.
	cp "$DIR/deploy/compose/compose.yml" "$dest/compose.yml"
	chmod 600 "$dest/compose.yml"
	BACKUP_PARTIAL=""
	echo "backup kept at $dest ($(du -sh "$dest" | cut -f1))"
}

# Runs on every exit after quiesce_project. A refusal, a failed backup or an
# interrupt removes the partial backup, puts back the services, and removes TMP.
install_exit() {
	local status=$?
	trap - EXIT INT TERM
	if [[ -n "$BACKUP_PARTIAL" ]]; then
		rm -rf -- "$BACKUP_PARTIAL"
	fi
	if [[ "$BACKUPS_CREATED" == 1 ]]; then
		rmdir -- "$DIR/backups" 2>/dev/null || true
	fi
	if [[ "$QUIESCED" == 1 ]]; then
		restore_quiesced || status=1
	fi
	rm -rf -- "$TMP"
	exit "$status"
}

# Replaces the live bundle with the verified one and publishes the staged .env,
# pinned to this release and profile, in one rename. Once the rename is done,
# the stopped services are not put back: the upgrade's own up starts them.
swap_in_bundle() {
	local env_tmp
	mkdir -p "$DIR"
	tar -xzf "$TMP/$BUNDLE" -C "$DIR"
	env_tmp="$(mktemp "$DIR/deploy/compose/.env.XXXXXX")"
	chmod 600 "$env_tmp"
	# Pin release metadata and apply an explicit public URL in one write.
	awk -v v="$VERSION" -v p="$PROJECT" -v prof="$profile" -v set_url="$set_public_url" -v url="$public_url_input" '
		/^POTOCOLOM_VERSION=/ { print "POTOCOLOM_VERSION=" v; found_v = 1; next }
		/^COMPOSE_PROJECT_NAME=/ {
			found_p = 1
			if ($0 ~ /^COMPOSE_PROJECT_NAME=[[:space:]]*$/) print "COMPOSE_PROJECT_NAME=" p
			else print
			next
		}
		/^[[:space:]]*(export[[:space:]]+)?COMPOSE_PROFILES[[:space:]]*[:=]/ {
			if (!found_f) print "COMPOSE_PROFILES=" prof
			found_f = 1
			next
		}
		/^[[:space:]]*(export[[:space:]]+)?PUBLIC_URL[[:space:]]*[:=]/ {
			if (set_url == "true") {
				if (!found_url) print "PUBLIC_URL=" url
				found_url = 1
			} else print
			next
		}
		{ print }
		END {
			if (!found_v) print "POTOCOLOM_VERSION=" v
			if (!found_p) print "COMPOSE_PROJECT_NAME=" p
			if (!found_f) print "COMPOSE_PROFILES=" prof
			if (set_url == "true" && !found_url) print "PUBLIC_URL=" url
		}
	' "$STAGE_ENV" >"$env_tmp"
	mv "$env_tmp" "$ENV_FILE"
	QUIESCED=0
	trap 'rm -rf "$TMP"' EXIT
}

main() {
	# Nothing below may read the piped script as its input.
	exec </dev/null

	# A released copy always installs its own tag; POTOCOLOM_VERSION only
	# serves a copy run from a source checkout, where nothing was written in.
	VERSION="__POTOCOLOM_VERSION__"
	[[ "$VERSION" =~ ^v[0-9] ]] || VERSION="${POTOCOLOM_VERSION:-}"
	if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
		echo "error: set POTOCOLOM_VERSION=vX.Y.Z" >&2
		echo "  this copy of install.sh was given VERSION=$VERSION" >&2
		exit 1
	fi
	if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
		echo "error: release images support Linux x86_64 only (found $(uname -s) $(uname -m))" >&2
		exit 1
	fi
	# PUBLIC_URL is the API's security origin: browsers send it back in
	# Origin byte for byte, so only the canonical form (lowercase, no default
	# port, no leading zero) is accepted, or every sign-in would be refused.
	set_public_url=false
	public_url_input=""
	if [[ "${POTOCOLOM_PUBLIC_URL+x}" == x ]]; then
		set_public_url=true
		public_url_input="$(printf '%s' "$POTOCOLOM_PUBLIC_URL" | tr '[:upper:]' '[:lower:]')"
		url_error=""
		url_scheme=""; url_host=""; url_port=""
		if [[ "$public_url_input" =~ ^(https?)://([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*)(:([1-9][0-9]{0,4}))?$ ]]; then
			url_scheme="${BASH_REMATCH[1]}"; url_host="${BASH_REMATCH[2]}"; url_port="${BASH_REMATCH[7]}"
		fi
		if [[ -z "$url_scheme" ]]; then
			url_error="must be http:// or https:// followed by a hostname or IPv4 address and an optional port, with no path, query, fragment, user info or trailing slash (for IPv6, use a hostname)"
		elif ((${#url_host} > 253)); then
			url_error="has a hostname longer than 253 characters"
		elif [[ -n "$url_port" ]] && ((url_port > 65535)); then
			url_error="has a port above 65535"
		elif [[ "$url_scheme:$url_port" == http:80 || "$url_scheme:$url_port" == https:443 ]]; then
			url_error="must leave out the default port (:$url_port)"
		elif [[ "${url_host##*.}" =~ ^(0x[0-9a-f]*|[0-9]+)$ \
			&& ! "$url_host" =~ ^(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])){3}$ ]]; then
			# A numeric last label makes browsers parse the host as IPv4 and
			# rewrite it (192.168.001.10, 0xc0.0xa8.1.10, 3232235786).
			url_error="must write an IPv4 address as four plain decimal numbers (192.168.1.10)"
		fi
		if [[ -n "$url_error" ]]; then
			echo "error: POTOCOLOM_PUBLIC_URL $url_error" >&2
			exit 1
		fi
	fi
	# Compose prefers a shell variable over .env, so an inherited PUBLIC_URL
	# would silently replace the one this script writes.
	unset PUBLIC_URL
	# The same holds for COMPOSE_PROFILES: an inherited value would pick the
	# worker. The profile this install runs is saved in .env instead.
	unset COMPOSE_PROFILES

	DIR="${POTOCOLOM_DIR:-$HOME/potocolom}"

	for cmd in docker curl tar sha256sum openssl; do
		if ! command -v "$cmd" >/dev/null 2>&1; then
			echo "error: $cmd is required" >&2
			exit 1
		fi
	done
	if ! docker compose version >/dev/null 2>&1; then
		echo "error: docker compose is required" >&2
		exit 1
	fi
	# The volume guard below runs `docker volume inspect`; when Docker does
	# not answer, that means "no volume" and preflight would write a new .env
	# next to a database it cannot see, so ask for the daemon first.
	if ! docker info >/dev/null 2>&1; then
		echo 'error: cannot reach Docker. Start the daemon, or add this user to the docker group' >&2
		echo '  (sudo usermod -aG docker "$USER", then log out and back in), and run this again.' >&2
		exit 1
	fi

	TMP="$(mktemp -d)"
	trap 'rm -rf "$TMP"' EXIT

	BASE_URL="https://github.com/Portocolom-Studio/potocolom/releases/download/$VERSION"
	BUNDLE="potocolom-$VERSION-selfhost.tar.gz"
	if [[ "${POTOCOLOM_RELEASE_URL+x}" == x ]]; then
		release_url="$POTOCOLOM_RELEASE_URL"
		if [[ -z "$release_url" || "$release_url" =~ [[:space:]] || "$release_url" == *\?* || "$release_url" == *\#* ]]; then
			echo 'error: POTOCOLOM_RELEASE_URL must be an https:// URL or absolute file:// URL without query or fragment' >&2
			exit 1
		fi
		case "$release_url" in
			https://*)
				if [[ ! "$release_url" =~ ^https://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?(/[^[:space:]\?#]*)?$ ]]; then
					echo 'error: POTOCOLOM_RELEASE_URL must be a valid https:// location' >&2
					exit 1
				fi
				;;
			file:///*) ;;
			*) echo 'error: POTOCOLOM_RELEASE_URL must use https:// or an absolute file:// URL' >&2; exit 1 ;;
		esac
		# file:// uses a local absolute path without an authority component.
		if [[ "$release_url" == file://* && ! "$release_url" =~ ^file:///[^[:space:]\?#]*$ ]]; then
			echo 'error: POTOCOLOM_RELEASE_URL file:// location must have an absolute path' >&2
			exit 1
		fi
		BASE_URL="${release_url%/}"
	fi
	curl -fsSL -o "$TMP/$BUNDLE" "$BASE_URL/$BUNDLE"
	curl -fsSL -o "$TMP/SHA256SUMS" "$BASE_URL/SHA256SUMS"

	# Only the line ending in this bundle's name counts: a substring match would
	# check some other file, and a missing line means a broken release.
	pattern="  ${BUNDLE//./\\.}\$"
	if ! (cd "$TMP" && grep -E "$pattern" SHA256SUMS | sha256sum -c -); then
		echo "error: SHA256SUMS does not verify $BUNDLE" >&2
		exit 1
	fi

	STAGE="$TMP/stage"
	mkdir -m 700 "$STAGE"
	tar -xzf "$TMP/$BUNDLE" -C "$STAGE"
	STAGE_COMPOSE="$STAGE/deploy/compose/compose.yml"
	# Resolve the Compose project against the saved env file and verified staged
	# compose file, then lock before anything in the live install changes.
	ENV_FILE="$DIR/deploy/compose/.env"
	source "$STAGE/scripts/install-lock.sh"
	PROJECT="potocolom"
	if [[ -f "$ENV_FILE" ]]; then
		if existing="$(install_compose_project "$ENV_FILE" "$STAGE_COMPOSE")"; then
			PROJECT="$existing"
		else
			echo "error: Compose could not read $ENV_FILE; refusing to inspect or change this install" >&2
			exit 1
		fi
	fi
	install_acquire_locks "$DIR" "$ENV_FILE" "$STAGE_COMPOSE" "$PROJECT"
	# A database volume without its .env would be paired with new secrets, so
	# this check runs before anything below can write a .env.
	if [[ ! -e "$ENV_FILE" ]] && database_volume_exists; then
		echo "error: the database volume ${PROJECT}_pgdata already exists, but $ENV_FILE does not." >&2
		echo "  A new .env would get a new database password that this volume does not accept." >&2
		echo "  Restore the .env from the earlier install into that path, or, to start over and" >&2
		echo "  delete that data, run: docker volume rm ${PROJECT}_pgdata" >&2
		exit 1
	fi

	# Until the image pull succeeds, nothing in $DIR changes. Preflight, the
	# profile and the pull run against a staged copy of the live .env.
	STAGE_ENV="$STAGE/deploy/compose/.env"
	saved_profile=""
	saved_version=""
	if [[ -f "$ENV_FILE" ]]; then
		cp "$ENV_FILE" "$STAGE_ENV"
		chmod 600 "$STAGE_ENV"
		if ! saved_profile="$(install_compose_value "$ENV_FILE" "$STAGE_COMPOSE" COMPOSE_PROFILES)" \
			|| ! saved_version="$(install_compose_value "$ENV_FILE" "$STAGE_COMPOSE" POTOCOLOM_VERSION)"; then
			echo "error: Compose could not read $ENV_FILE; refusing to inspect or change this install" >&2
			exit 1
		fi
	fi
	if [[ ! -e "$ENV_FILE" ]]; then
		echo "Installing $VERSION"
	elif [[ -z "$saved_version" ]]; then
		echo "Upgrading a source checkout to $VERSION"
	elif [[ "$saved_version" == "$VERSION" ]]; then
		echo "Reinstalling $VERSION"
	elif [[ "$(printf '%s\n' "$saved_version" "$VERSION" | sort -V | tail -n 1)" == "$saved_version" ]]; then
		echo "Downgrading $saved_version to $VERSION"
	else
		echo "Upgrading $saved_version to $VERSION"
	fi

	# Profile precedence: POTOCOLOM_PROFILE in the shell, then the profile saved
	# in .env, then the worker containers of this project, then detection. Only
	# an explicit POTOCOLOM_PROFILE may change the profile of a running install.
	detected="$(bash "$STAGE/scripts/gpu-profile.sh")" || exit 1
	explicit=false
	if [[ -n "${POTOCOLOM_PROFILE:-}" ]]; then
		explicit=true
		profile="$detected"
	else
		# Source commands keep smoke as the no-GPU default. A release contains
		# no simulated-worker image, so its automatic no-GPU choice is API-only.
		if [[ "$detected" == smoke ]]; then detected=api; fi
		if [[ -n "$saved_profile" ]]; then
			case "$saved_profile" in
			gpu | rocm | api) ;;
			*)
				echo "error: COMPOSE_PROFILES in $ENV_FILE must be gpu, rocm or api, not $saved_profile" >&2
				exit 1
				;;
			esac
			profile="$saved_profile"
		else
			profile="$(project_worker_profile "$PROJECT")" || exit 1
			profile="${profile:-$detected}"
		fi
		if [[ "$profile" != "$detected" ]]; then
			echo "warning: this machine now detects $detected, but this install keeps $profile." >&2
			echo "  To change the profile, run this again with POTOCOLOM_PROFILE=$detected (this removes the other worker)." >&2
		fi
	fi
	if [[ "$profile" == smoke ]]; then
		echo "error: the release installer does not support the simulated worker. Clone the repository and run scripts/compose-smoke.sh for source development." >&2
		exit 1
	fi
	if [[ "$profile" != gpu && "$profile" != rocm && "$profile" != api ]]; then
		echo "error: unsupported release profile: $profile" >&2
		exit 1
	fi
	# The shell value of COMPOSE_PROFILES beats the staged .env, so the staged
	# commands see the resolved profile and nothing the old .env adds.
	stage_compose=(env COMPOSE_PROFILES="$profile" docker compose -p "$PROJECT" --env-file "$STAGE_ENV" -f "$STAGE_COMPOSE" --profile "$profile")

	disk_probe="$DIR"
	while [[ ! -e "$disk_probe" ]]; do disk_probe="$(dirname "$disk_probe")"; done
	if ! ENV_FILE="$STAGE_ENV" POTOCOLOM_PROFILE="$profile" POTOCOLOM_DISK_PATH="$disk_probe" bash "$STAGE/scripts/preflight.sh"; then
		echo "error: preflight failed; fix what it reports, then run this again" >&2
		exit 1
	fi

	# The shell env beats the env file, so the pull runs against this tag
	# before .env names it, and .env is pinned only after the swap.
	images="$(POTOCOLOM_VERSION="$VERSION" "${stage_compose[@]}" config --images)"
	if [[ -z "$images" ]]; then
		echo 'error: Compose did not list any release images' >&2
		exit 1
	fi
	missing_images=false
	while IFS= read -r image; do
		if ! docker image inspect -- "$image" >/dev/null 2>&1; then
			missing_images=true
			break
		fi
	done <<<"$images"
	if [[ "$missing_images" == true ]]; then
		POTOCOLOM_VERSION="$VERSION" "${stage_compose[@]}" pull
	fi

	# The live install changes from here. A database that already exists is
	# stopped, checked and backed up before the verified bundle is swapped in.
	revision=""
	if database_volume_exists; then
		if [[ ! -f "$DIR/deploy/compose/compose.yml" ]]; then
			echo "error: $DIR/deploy/compose/compose.yml is missing, so the running stack cannot be stopped safely; nothing was changed" >&2
			exit 1
		fi
		quiesce_project
		if ! wait_for_sessions; then
			echo "error: database client sessions are still open after the deadline; nothing was changed" >&2
			exit 1
		fi
		if ! revision="$(database_revision)"; then
			echo "error: could not read the database revision; nothing was changed" >&2
			exit 1
		fi
		if [[ -n "$revision" ]]; then
			verdict="$(target_revision_verdict "$revision")"
			case "$verdict" in
			known) ;;
			unknown) refuse_newer_database "$revision" ;;
			*)
				echo "error: could not ask release $VERSION whether it knows revision $revision; nothing was changed" >&2
				exit 1
				;;
			esac
		fi
		if [[ "$saved_version" != "$VERSION" ]]; then
			take_backup "${revision:-none}"
		fi
	fi
	swap_in_bundle

	compose=(docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f "$DIR/deploy/compose/compose.yml" --profile "$profile")
	# Only an explicit POTOCOLOM_PROFILE removes a worker. API-only transitions
	# remove both release workers.
	if [[ "$explicit" == true ]]; then
		case "$profile" in
			gpu) obsolete=('--profile' rocm rm -sf worker-rocm) ;;
			rocm) obsolete=('--profile' gpu rm -sf worker) ;;
			api) obsolete=(--profile gpu rm -sf worker) ;;
		esac
		POTOCOLOM_VERSION="$VERSION" docker compose -p "$PROJECT" --env-file "$ENV_FILE" \
			-f "$DIR/deploy/compose/compose.yml" "${obsolete[@]}"
		if [[ "$profile" == api ]]; then
			POTOCOLOM_VERSION="$VERSION" docker compose -p "$PROJECT" --env-file "$ENV_FILE" \
				-f "$DIR/deploy/compose/compose.yml" --profile rocm rm -sf worker-rocm
		fi
	fi

	POTOCOLOM_VERSION="$VERSION" "${compose[@]}" up -d --no-build

	# Readiness covers the API and its store. It does not wait for model downloads
	# or worker warmup. Each curl has a short timeout and the retry count is fixed.
	ready=false
	for attempt in {1..30}; do
		if curl -fsS --connect-timeout 2 --max-time 2 http://127.0.0.1:8080/api/v1/ready >/dev/null 2>&1; then
			ready=true
			break
		fi
		sleep 2
	done
	if [[ "$ready" != true ]]; then
		echo 'error: API did not become ready within 120 seconds; data is intact.' >&2
		echo "  Diagnose with: docker compose -p $(printf '%q' "$PROJECT") --env-file $(printf '%q' "$ENV_FILE") -f $(printf '%q' "$DIR/deploy/compose/compose.yml") logs api postgres" >&2
		exit 1
	fi

	# Compose syntax: an optional export, and an unquoted value ends at a comment.
	public_url="$(sed -n -E 's/^[[:space:]]*(export[[:space:]]+)?PUBLIC_URL[[:space:]]*[:=][[:space:]]*["'"'"']?([^"'"'"'#[:space:]]*).*/\2/p' "$ENV_FILE" | tail -n 1)"
	public_url="${public_url:-http://localhost:8080}"

	echo
	if [[ "$profile" == api ]]; then
		echo "potocolom $VERSION API and database are ready. Open $public_url"
		echo 'Generation needs an attached GPU worker. Remote worker authentication uses FLEET_SECRET in the env file.'
	else
		echo "potocolom $VERSION is running. Open $public_url"
	fi
	if [[ "$public_url" =~ ^https?://(\[[^]]+\]|[^:/?#]+)(:[0-9]+)?$ ]]; then
		public_host="${BASH_REMATCH[1]}"
		if [[ "$public_host" == localhost || "$public_host" == '[::1]' \
			|| "$public_host" =~ ^127(\.(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])){3}$ ]]; then
			if command -v hostname >/dev/null 2>&1; then
				addresses="$(hostname -I 2>/dev/null | awk '{ for (i = 1; i <= NF; i++) { n = split($i, a, "."); ok = (n == 4); for (j = 1; j <= n; j++) if (a[j] !~ /^[0-9]+$/ || a[j] + 0 > 255) ok = 0; if (ok && a[1] + 0 != 127) { if (out != "") out = out " "; out = out $i } } } END { print out }' || true)"
			else
				addresses=""
			fi
			echo "The studio only works from this machine. To reach it from another machine, rerun:"
			dir_prefix=""
			if [[ "$DIR" != "$HOME/potocolom" ]]; then
				dir_prefix="POTOCOLOM_DIR=$(printf '%q' "$DIR") "
			fi
			echo "  curl -fsSL $BASE_URL/install.sh | ${dir_prefix}POTOCOLOM_PUBLIC_URL=http://ADDRESS:8080 bash"
			if [[ -n "$addresses" ]]; then
				echo "Replace ADDRESS with one of this machine's addresses: $addresses"
			else
				echo "Replace ADDRESS with this machine's address."
			fi
			echo 'HTTPS needs a reverse proxy; see docs/self-hosting.md.'
		fi
	fi
	# The one-use link decides how this install works: whoever spends it chooses
	# an administrator account or no accounts. The API container mints it with
	# the project and files resolved above, so nothing ambient can retarget it.
	# Exit status 3 means the install already chose: a note, not a failure.
	# Anything else is shown, with the command that retries it.
	setup_err="$(mktemp)"
	setup_status=0
	setup_link="$("${compose[@]}" exec -T api python -m app.operator setup-link 2>"$setup_err")" \
		|| setup_status=$?
	if [[ "$setup_status" -eq 0 && "$setup_link" == http*/setup#* ]]; then
		echo
		echo "Finish setup in a browser within one hour. The link works once:"
		echo "  $setup_link"
		open_setup_link "$setup_link"
	elif [[ "$setup_status" -eq 3 ]]; then
		echo "This install already chose how it works, so there is no setup link to show."
	else
		setup_reason="$(head -n 1 "$setup_err" 2>/dev/null || true)"
		echo "warning: no setup link was made: ${setup_reason:-the API did not answer}" >&2
		echo "  Retry with: env -u COMPOSE_PROJECT_NAME docker compose --env-file $(printf '%q' "$ENV_FILE") -f $(printf '%q' "$DIR/deploy/compose/compose.yml") exec -T api python -m app.operator setup-link" >&2
	fi
	rm -f -- "$setup_err"
	echo "Secrets are in $ENV_FILE."
	echo "Lost the link? docs/self-hosting.md explains how to mint another one."
	echo "Running the install.sh of a newer release upgrades this install in place."
	echo "Stop: docker compose -f $(printf '%q' "$DIR/deploy/compose/compose.yml") --profile $(printf '%q' "$profile") down"
}

# Sourced, the functions are available to a test; run, the installer starts.
if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
	main "$@"
fi
