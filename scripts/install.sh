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
	# Resolve the Compose project against the saved env file and verified staged
	# compose file, then lock before replacing any live bundle file.
	ENV_FILE="$DIR/deploy/compose/.env"
	source "$STAGE/scripts/install-lock.sh"
	PROJECT="potocolom"
	if [[ -f "$ENV_FILE" ]]; then
		if existing="$(install_compose_project "$ENV_FILE" "$STAGE/deploy/compose/compose.yml")"; then
			PROJECT="$existing"
		else
			echo "error: Compose could not read $ENV_FILE; refusing to inspect or change this install" >&2
			exit 1
		fi
	fi
	install_acquire_locks "$DIR" "$ENV_FILE" "$STAGE/deploy/compose/compose.yml" "$PROJECT"
	mkdir -p "$DIR"
	tar -xzf "$TMP/$BUNDLE" -C "$DIR"
	# The bundle carries no .env, so an existing install keeps its secrets.
	if [[ ! -e "$ENV_FILE" ]] && docker volume inspect "${PROJECT}_pgdata" >/dev/null 2>&1; then
		echo "error: the database volume ${PROJECT}_pgdata already exists, but $ENV_FILE does not." >&2
		echo "  A new .env would get a new database password that this volume does not accept." >&2
		echo "  Restore the .env from the earlier install into that path, or, to start over and" >&2
		echo "  delete that data, run: docker volume rm ${PROJECT}_pgdata" >&2
		exit 1
	fi

	if ! bash "$DIR/scripts/preflight.sh"; then
		echo "error: preflight failed; fix what it reports, then run this again" >&2
		exit 1
	fi

	if [[ -n "${POTOCOLOM_PROFILE:-}" ]]; then
		profile="$POTOCOLOM_PROFILE"
	elif [[ ! -f "$DIR/scripts/gpu-profile.sh" ]]; then
		echo "error: the $VERSION bundle predates scripts/gpu-profile.sh; set POTOCOLOM_PROFILE=gpu (NVIDIA) or rocm (AMD)" >&2
		exit 1
	else
		profile="$(bash "$DIR/scripts/gpu-profile.sh")"
		if [[ "$profile" == smoke ]]; then
			echo "error: no NVIDIA or AMD GPU found. The released images need one. To try the stack with the simulated worker, clone the repository and run scripts/compose-smoke.sh. See the GPU section of the preflight output above." >&2
			exit 1
		fi
	fi
	if [[ "$profile" != gpu && "$profile" != rocm ]]; then
		echo "error: POTOCOLOM_PROFILE must be gpu or rocm, not $profile" >&2
		exit 1
	fi

	compose=(docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f "$DIR/deploy/compose/compose.yml" --profile "$profile")
	# The shell env beats the env file, so the pull runs against this tag
	# before .env names it, and .env is pinned only afterwards: a failed
	# pull must not leave it naming a tag that was never pulled.
	images="$(POTOCOLOM_VERSION="$VERSION" "${compose[@]}" config --images)"
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
		POTOCOLOM_VERSION="$VERSION" "${compose[@]}" pull
	fi

	# Pin release metadata and apply an explicit public URL in one write.
	env_tmp="$(mktemp "$DIR/deploy/compose/.env.XXXXXX")"
	chmod 600 "$env_tmp"
	awk -v v="$VERSION" -v p="$PROJECT" -v set_url="$set_public_url" -v url="$public_url_input" '
		/^POTOCOLOM_VERSION=/ { print "POTOCOLOM_VERSION=" v; found_v = 1; next }
		/^COMPOSE_PROJECT_NAME=/ {
			found_p = 1
			if ($0 ~ /^COMPOSE_PROJECT_NAME=[[:space:]]*$/) print "COMPOSE_PROJECT_NAME=" p
			else print
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
			if (set_url == "true" && !found_url) print "PUBLIC_URL=" url
		}
	' "$ENV_FILE" >"$env_tmp"
	mv "$env_tmp" "$ENV_FILE"

	# A host that changed vendor since the last install (or one that older
	# releases detected as AMD before NVIDIA) still runs the other worker.
	if [[ "$profile" == gpu ]]; then other=(rocm worker-rocm); else other=(gpu worker); fi
	POTOCOLOM_VERSION="$VERSION" docker compose -p "$PROJECT" --env-file "$ENV_FILE" \
		-f "$DIR/deploy/compose/compose.yml" --profile "${other[0]}" rm -sf "${other[1]}"

	POTOCOLOM_VERSION="$VERSION" "${compose[@]}" up -d --no-build

	# Compose syntax: an optional export, and an unquoted value ends at a comment.
	public_url="$(sed -n -E 's/^[[:space:]]*(export[[:space:]]+)?PUBLIC_URL[[:space:]]*[:=][[:space:]]*["'"'"']?([^"'"'"'#[:space:]]*).*/\2/p' "$ENV_FILE" | tail -n 1)"
	public_url="${public_url:-http://localhost:8080}"

	echo
	echo "potocolom $VERSION is running. Open $public_url"
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
	echo "Secrets are in $ENV_FILE."
	echo "bash \"$DIR/scripts/auth-enable.sh\" turns on accounts."
	echo "Running the install.sh of a newer release upgrades this install in place."
	echo "Stop: docker compose -f \"$DIR/deploy/compose/compose.yml\" --profile $profile down"
}

main "$@"
