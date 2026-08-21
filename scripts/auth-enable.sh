#!/usr/bin/env bash
# Turn on accounts for this installation and print the one-use setup link.
# Writes ROOT_KEYS and PUBLIC_URL into deploy/compose/.env, records the switch
# in PostgreSQL, mints the link, and only then sets AUTH_MODE=accounts.
#
# That order matters: the API refuses to start in accounts mode until the
# switch is recorded, so writing AUTH_MODE first would leave a container that
# cannot boot. Enabling accounts is one way, and undoing it needs an offline
# destructive reset.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/deploy/compose/.env}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/deploy/compose/compose.yml}"
PY="${PY:-$ROOT/backend/.venv/bin/python}"

command -v openssl >/dev/null 2>&1 || {
	echo "error: openssl is required to generate ROOT_KEYS" >&2; exit 1; }
test -f "$ENV_FILE" || {
	echo "error: $ENV_FILE is missing; run make preflight first" >&2; exit 1; }

# Read one key without sourcing the file: .env is data, and sourcing it would
# run whatever anyone who can write it puts there.
read_key() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }

set_key() {
	local key="$1" value="$2" tmp
	tmp="$(mktemp)"
	if grep -q "^${key}=" "$ENV_FILE"; then
		awk -v k="$key" -v v="$value" -F= '$1 == k { print k "=" v; next } { print }' \
			"$ENV_FILE" >"$tmp"
		mv "$tmp" "$ENV_FILE"
	else
		rm -f "$tmp"
		printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
	fi
}

if [[ -n "$(read_key ROOT_KEYS)" ]]; then
	echo "ROOT_KEYS is already set in $ENV_FILE; keeping it."
else
	# Version 1, base64 of 32 random bytes. Newest first, so a later rotation
	# prepends a version and every older one stays readable.
	set_key ROOT_KEYS "1:$(openssl rand -base64 32)"
	echo "wrote ROOT_KEYS to $ENV_FILE"
fi

current_url="$(read_key PUBLIC_URL)"
printf 'Where do browsers reach this install? [%s] ' "${current_url:-http://localhost:8080}"
read -r answer || answer=""
public_url="${answer:-${current_url:-http://localhost:8080}}"
case "$public_url" in
	http://*|https://*) ;;
	*) echo "error: PUBLIC_URL must start with http:// or https://" >&2; exit 1 ;;
esac
case "$public_url" in
	*[\$\`\"\'\\]*|*' '*)
		echo "error: PUBLIC_URL must not contain quotes, spaces, backslashes or \$" >&2
		exit 1 ;;
esac
set_key PUBLIC_URL "$public_url"

# The container carries the compose database; the host does not reach it.
if docker compose -f "$COMPOSE_FILE" ps --status running api 2>/dev/null | grep -q api; then
	echo "running the enable step inside the api container"
	docker compose -f "$COMPOSE_FILE" run --rm --no-deps \
		-e ROOT_KEYS="$(read_key ROOT_KEYS)" -e PUBLIC_URL="$public_url" \
		api python -m app.enable
else
	test -x "$PY" || { echo "error: $PY is missing; run make setup first" >&2; exit 1; }
	# Passed one by one rather than by sourcing the file. An empty
	# DATABASE_URL would override the application default with nothing.
	vars=(ROOT_KEYS="$(read_key ROOT_KEYS)" PUBLIC_URL="$public_url")
	database_url="$(read_key DATABASE_URL)"
	[[ -n "$database_url" ]] && vars+=(DATABASE_URL="$database_url")
	(cd "$ROOT/backend" && env "${vars[@]}" "$PY" -m app.enable)
fi

set_key AUTH_MODE accounts
echo "set AUTH_MODE=accounts in $ENV_FILE; restart the API to serve it."
