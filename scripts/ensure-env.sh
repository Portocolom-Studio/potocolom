#!/usr/bin/env bash
# Create deploy/compose/.env from the example when missing, and fill empty
# POSTGRES_PASSWORD / FLEET_SECRET. Never overwrites a non-empty Compose value.
# ENV_FILE / ENV_EXAMPLE override paths (verify-guards).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/deploy/compose/.env}"
ENV_EXAMPLE="${ENV_EXAMPLE:-$ROOT/deploy/compose/.env.example}"
COMPOSE_FILE="$ROOT/deploy/compose/compose.yml"
PLACEHOLDER_PASSWORD="change-me"
source "$ROOT/scripts/install-lock.sh"

if [[ ! -f "$ENV_EXAMPLE" ]]; then
	echo "error: $ENV_EXAMPLE is missing; cannot write .env" >&2
	exit 1
fi
if ! command -v openssl >/dev/null 2>&1; then
	echo "error: openssl is required to generate POSTGRES_PASSWORD and FLEET_SECRET" >&2
	exit 1
fi

project="potocolom"
if [[ -f "$ENV_FILE" ]]; then
	if value="$(install_compose_project "$ENV_FILE" "$COMPOSE_FILE")"; then
		project="$value"
	else
		echo "error: Compose could not read $ENV_FILE; refusing to change its credentials" >&2
		exit 1
	fi
fi
install_acquire_locks "$ROOT" "$ENV_FILE" "$COMPOSE_FILE" "$project"

compose_value() {
	local key="$1" value
	if value="$(install_compose_value "$ENV_FILE" "$COMPOSE_FILE" "$key")"; then
		printf '%s' "$value"
	else
		echo "error: Compose could not read $ENV_FILE; refusing to change its credentials" >&2
		return 1
	fi
}

replace_secrets() {
	local source_file="$1" pg="$2" fleet="$3" write_pg="$4" write_fleet="$5" tmp dir
	dir="$(dirname "$ENV_FILE")"
	mkdir -p "$dir"
	tmp="$(mktemp "$dir/.env.XXXXXX")"
	chmod 600 "$tmp"
	awk -v pg="$pg" -v fleet="$fleet" -v write_pg="$write_pg" -v write_fleet="$write_fleet" '
		function is_key(line, key) {
			return line ~ "^[[:space:]]*(export[[:space:]]+)?" key "[[:space:]]*[:=]"
		}
		{
			line = $0
			sub(/\r$/, "", line)
			if (is_key(line, "POSTGRES_PASSWORD")) {
				if (write_pg == "true") {
					if (!seen_pg++) print "POSTGRES_PASSWORD=" pg
				} else print
				next
			}
			if (is_key(line, "FLEET_SECRET")) {
				if (write_fleet == "true") {
					if (!seen_fleet++) print "FLEET_SECRET=" fleet
				} else print
				next
			}
			print
		}
		END {
			if (write_pg == "true" && !seen_pg) print "POSTGRES_PASSWORD=" pg
			if (write_fleet == "true" && !seen_fleet) print "FLEET_SECRET=" fleet
		}
	' "$source_file" >"$tmp"
	mv -f -- "$tmp" "$ENV_FILE"
}

new_secret() { openssl rand -hex 32; }

database_state() {
	if ! command -v docker >/dev/null 2>&1; then
		echo absent
		return
	fi
	if ! docker info >/dev/null 2>&1; then
		echo unknown
		return
	fi
	local project_name candidate
	project_name="$(compose_value COMPOSE_PROJECT_NAME)"
	for candidate in "${COMPOSE_PROJECT_NAME:-}" "$project_name" compose potocolom potocolom-smoke; do
		[[ -n "$candidate" ]] || continue
		if docker volume inspect "${candidate}_pgdata" >/dev/null 2>&1; then
			echo "${candidate}_pgdata"
			return
		fi
	done
	echo absent
}

if [[ ! -e "$ENV_FILE" ]]; then
	pg="$(new_secret)"
	fleet="$(new_secret)"
	replace_secrets "$ENV_EXAMPLE" "$pg" "$fleet" true true
	echo "wrote $ENV_FILE"
	echo "FLEET_SECRET=$fleet"
	echo "A worker on another machine needs a copy of FLEET_SECRET."
else
	pg="$(compose_value POSTGRES_PASSWORD)"
	fleet="$(compose_value FLEET_SECRET)"
	fleet_changed=false
	if [[ -z "$pg" ]]; then
		state="$(database_state)"
		if [[ "$state" != absent ]]; then
			echo "error: POSTGRES_PASSWORD is missing, and database state is $state." >&2
			echo "  Restore the saved password in $ENV_FILE before running this again." >&2
			exit 1
		fi
		pg="$(new_secret)"
		if [[ -z "$fleet" ]]; then fleet="$(new_secret)"; fleet_changed=true; fi
		replace_secrets "$ENV_FILE" "$pg" "$fleet" true "$fleet_changed"
	elif [[ "$pg" == "$PLACEHOLDER_PASSWORD" ]]; then
		state="$(database_state)"
		if [[ "$state" == absent ]]; then
			pg="$(new_secret)"
			if [[ -z "$fleet" ]]; then fleet="$(new_secret)"; fleet_changed=true; fi
			replace_secrets "$ENV_FILE" "$pg" "$fleet" true "$fleet_changed"
		elif [[ "$state" == unknown ]]; then
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
	else
		if [[ -z "$fleet" ]]; then
			fleet="$(new_secret)"
			fleet_changed=true
			replace_secrets "$ENV_FILE" "$pg" "$fleet" false true
		fi
	fi
	if [[ -z "$fleet" ]]; then
		fleet="$(new_secret)"
		fleet_changed=true
		replace_secrets "$ENV_FILE" "$pg" "$fleet" false true
	fi
	chmod 600 "$ENV_FILE"
	if [[ "$fleet_changed" == true ]]; then
		echo "filled FLEET_SECRET in $ENV_FILE"
		echo "FLEET_SECRET=$fleet"
		echo "A worker on another machine needs a copy of FLEET_SECRET."
	fi
fi
