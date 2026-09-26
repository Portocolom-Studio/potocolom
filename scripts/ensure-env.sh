#!/usr/bin/env bash
# Create deploy/compose/.env from the example when missing, and fill empty
# POSTGRES_PASSWORD / FLEET_SECRET. Never overwrites a non-empty value, except
# the example's own POSTGRES_PASSWORD placeholder before any database exists.
# ENV_FILE / ENV_EXAMPLE / PGDATA_VOLUME_OVERRIDE override defaults (verify-guards).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/deploy/compose/.env}"
ENV_EXAMPLE="${ENV_EXAMPLE:-$ROOT/deploy/compose/.env.example}"
PLACEHOLDER_PASSWORD="change-me"

if [[ ! -f "$ENV_EXAMPLE" ]]; then
	echo "error: $ENV_EXAMPLE is missing; cannot write .env" >&2
	exit 1
fi
if ! command -v openssl >/dev/null 2>&1; then
	echo "error: openssl is required to generate POSTGRES_PASSWORD and FLEET_SECRET" >&2
	exit 1
fi

key_filled() {
	local key="$1"
	grep -q "^${key}=.\+" "$ENV_FILE" 2>/dev/null
}

write_new() {
	local pg fleet tmp
	pg="$(openssl rand -hex 32)"
	fleet="$(openssl rand -hex 32)"
	tmp="$(mktemp)"
	awk -v pg="$pg" -v fleet="$fleet" '
		/^POSTGRES_PASSWORD=/ { print "POSTGRES_PASSWORD=" pg; next }
		/^FLEET_SECRET=/ { print "FLEET_SECRET=" fleet; next }
		{ print }
	' "$ENV_EXAMPLE" >"$tmp"
	mv "$tmp" "$ENV_FILE"
	echo "wrote $ENV_FILE"
	echo "FLEET_SECRET=$fleet"
	echo "A worker on another machine needs a copy of FLEET_SECRET."
}

fill_key() {
	local key="$1" val tmp
	val="$(openssl rand -hex 32)"
	tmp="$(mktemp)"
	if grep -q "^${key}=" "$ENV_FILE"; then
		awk -v k="$key" -v v="$val" -F= '
			$1 == k { print k "=" v; next }
			{ print }
		' "$ENV_FILE" >"$tmp"
		mv "$tmp" "$ENV_FILE"
	else
		printf '\n%s=%s\n' "$key" "$val" >>"$ENV_FILE"
	fi
	echo "filled $key in $ENV_FILE"
	if [[ "$key" == "FLEET_SECRET" ]]; then
		echo "FLEET_SECRET=$val"
		echo "A worker on another machine needs a copy of FLEET_SECRET."
	fi
}

env_value() {
	# The value as Compose reads it: CRLF, surrounding blanks and one pair of
	# quotes do not change it, so they must not hide the placeholder either.
	sed -n "s/^${1}=//p" "$ENV_FILE" 2>/dev/null | tail -n 1 | tr -d '\r' |
		sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\\(.*\\)'$/\\1/"
}

has_placeholder_password() {
	[[ "$(env_value POSTGRES_PASSWORD)" == "$PLACEHOLDER_PASSWORD" ]]
}

# Postgres reads POSTGRES_PASSWORD only when it creates its data volume, so
# rotating the placeholder after that would lock the API out of a running
# database. Rotation therefore needs proof that no volume exists: Docker must
# answer, and none of the names this install can have may exist. Docker
# installed but not answering (daemon down, no docker group) is "unknown",
# never "absent". Prints the state: absent, unknown, or the volume name.
database_state() {
	if ! command -v docker >/dev/null 2>&1; then
		echo absent
		return
	fi
	if ! docker info >/dev/null 2>&1; then
		echo unknown
		return
	fi
	local project candidates=() name
	project="$(env_value COMPOSE_PROJECT_NAME)"
	if [[ -n "${PGDATA_VOLUME_OVERRIDE:-}" ]]; then
		candidates=("$PGDATA_VOLUME_OVERRIDE")
	else
		for name in "${COMPOSE_PROJECT_NAME:-}" "$project" compose potocolom-smoke; do
			[[ -n "$name" ]] && candidates+=("${name}_pgdata")
		done
	fi
	for name in "${candidates[@]}"; do
		if docker volume inspect "$name" >/dev/null 2>&1; then
			echo "$name"
			return
		fi
	done
	echo absent
}

if [[ ! -e "$ENV_FILE" ]]; then
	write_new
else
	if has_placeholder_password; then
		state="$(database_state)"
		if [[ "$state" == absent ]]; then
			fill_key POSTGRES_PASSWORD
		else
			if [[ "$state" == unknown ]]; then
				echo "warning: POSTGRES_PASSWORD is still the example value '${PLACEHOLDER_PASSWORD}', and Docker" >&2
				echo "  did not answer, so it is not safe to tell whether a database already uses it." >&2
				echo "  Start Docker (or run as a user in the docker group) and run this again." >&2
			else
				echo "warning: POSTGRES_PASSWORD is still the example value '${PLACEHOLDER_PASSWORD}' and the" >&2
				echo "  database volume $state already uses it. Change it in the database first, with a" >&2
				echo "  hex value (openssl rand -hex 32; Compose expands \$ inside DATABASE_URL):" >&2
				echo "  docker compose -p ${state%_pgdata} -f deploy/compose/compose.yml exec postgres psql -U potocolom -c \"ALTER ROLE potocolom PASSWORD '<new>'\"" >&2
				echo "  then put the same value in $ENV_FILE." >&2
			fi
		fi
	fi
	key_filled POSTGRES_PASSWORD || fill_key POSTGRES_PASSWORD
	key_filled FLEET_SECRET || fill_key FLEET_SECRET
fi
