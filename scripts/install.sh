#!/usr/bin/env bash
# Install a released potocolom: download the self-host bundle from the GitHub
# release, verify it against SHA256SUMS, run preflight, then pull the published
# images and start the stack. The release workflow writes its tag into the
# VERSION below before uploading this file.
set -euo pipefail

# Piped from curl, bash runs lines as they arrive: wrapping everything in main
# means a cut-off download defines a function and runs nothing.
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

	ENV_FILE="$DIR/deploy/compose/.env"
	# The project a release install owns is potocolom; a name the operator
	# already set in .env wins, so reruns and upgrades stay on the
	# containers and volumes that name belongs to.
	PROJECT="potocolom"
	if [[ -f "$ENV_FILE" ]]; then
		existing="$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' "$ENV_FILE" | tail -n 1)"
		PROJECT="${existing:-potocolom}"
	fi

	# Preflight is what creates .env: a fresh .env brings a fresh database
	# password, which a pre-existing volume does not accept.
	if [[ ! -e "$ENV_FILE" ]] && docker volume inspect "${PROJECT}_pgdata" >/dev/null 2>&1; then
		echo "error: the database volume ${PROJECT}_pgdata already exists, but $ENV_FILE does not." >&2
		echo "  A new .env would get a new database password that this volume does not accept." >&2
		echo "  Restore the .env from the earlier install into that path, or, to start over and" >&2
		echo "  delete that data, run: docker volume rm ${PROJECT}_pgdata" >&2
		exit 1
	fi

	TMP="$(mktemp -d)"
	trap 'rm -rf "$TMP"' EXIT

	BASE_URL="https://github.com/Portocolom-Studio/potocolom/releases/download/$VERSION"
	BUNDLE="potocolom-$VERSION-selfhost.tar.gz"
	curl -fsSL -o "$TMP/$BUNDLE" "$BASE_URL/$BUNDLE"
	curl -fsSL -o "$TMP/SHA256SUMS" "$BASE_URL/SHA256SUMS"

	# Only the line ending in this bundle's name counts: a substring match would
	# check some other file, and a missing line means a broken release.
	pattern="  ${BUNDLE//./\\.}\$"
	if ! (cd "$TMP" && grep -E "$pattern" SHA256SUMS | sha256sum -c -); then
		echo "error: SHA256SUMS does not verify $BUNDLE" >&2
		exit 1
	fi

	mkdir -p "$DIR"
	tar -xzf "$TMP/$BUNDLE" -C "$DIR"
	# The bundle carries no .env, so an existing install keeps its secrets.

	if ! bash "$DIR/scripts/preflight.sh"; then
		echo "error: preflight failed; fix what it reports, then run this again" >&2
		exit 1
	fi

	if [[ -n "${POTOCOLOM_PROFILE:-}" ]]; then
		profile="$POTOCOLOM_PROFILE"
	elif [[ -e /dev/kfd ]]; then
		profile="rocm"
	elif nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1; then
		profile="gpu"
	else
		echo "error: no NVIDIA or AMD GPU found. The released images need one. To try the stack with the simulated worker, clone the repository and run scripts/compose-smoke.sh." >&2
		exit 1
	fi
	if [[ "$profile" != gpu && "$profile" != rocm ]]; then
		echo "error: POTOCOLOM_PROFILE must be gpu or rocm, not $profile" >&2
		exit 1
	fi

	compose=(docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f "$DIR/deploy/compose/compose.yml" --profile "$profile")
	# The shell env beats the env file, so the pull runs against this tag
	# before .env names it, and .env is pinned only afterwards: a failed
	# pull must not leave it naming a tag that was never pulled.
	POTOCOLOM_VERSION="$VERSION" "${compose[@]}" pull

	# One pass for both keys: the version is always the installed tag, the
	# project name only when its line is absent or empty, so a name the
	# operator set is left alone.
	env_tmp="$(mktemp)"
	awk -v v="$VERSION" -v p="$PROJECT" '
		/^POTOCOLOM_VERSION=/ { print "POTOCOLOM_VERSION=" v; found_v = 1; next }
		/^COMPOSE_PROJECT_NAME=/ {
			found_p = 1
			if ($0 ~ /^COMPOSE_PROJECT_NAME=[[:space:]]*$/) print "COMPOSE_PROJECT_NAME=" p
			else print
			next
		}
		{ print }
		END {
			if (!found_v) print "POTOCOLOM_VERSION=" v
			if (!found_p) print "COMPOSE_PROJECT_NAME=" p
		}
	' "$ENV_FILE" >"$env_tmp"
	mv "$env_tmp" "$ENV_FILE"

	"${compose[@]}" up -d --no-build

	public_url="$(sed -n 's/^PUBLIC_URL=//p' "$ENV_FILE" | tail -n 1 | tr -d '"' | tr -d '\r')"
	public_url="${public_url:-http://localhost:8080}"

	echo
	echo "potocolom $VERSION is running. Open $public_url"
	echo "Secrets are in $ENV_FILE."
	echo "bash \"$DIR/scripts/auth-enable.sh\" turns on accounts."
	echo "Running the install.sh of a newer release upgrades this install in place."
	echo "Stop: docker compose -f \"$DIR/deploy/compose/compose.yml\" --profile $profile down"
}

main "$@"
