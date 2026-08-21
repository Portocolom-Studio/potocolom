#!/usr/bin/env bash
# Turn on accounts for this installation and print the one-use setup link.
# Writes ROOT_KEYS, AUTH_MODE and PUBLIC_URL into deploy/compose/.env, records
# the switch in PostgreSQL, and mints the link. Enabling accounts is one way:
# an installation that has enabled them cannot start in AUTH_MODE=none again.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/deploy/compose/.env}"
PY="${PY:-$ROOT/backend/.venv/bin/python}"

command -v openssl >/dev/null 2>&1 || {
	echo "error: openssl is required to generate ROOT_KEYS" >&2; exit 1; }
test -x "$PY" || { echo "error: $PY is missing; run make setup first" >&2; exit 1; }
test -f "$ENV_FILE" || { echo "error: $ENV_FILE is missing; run make preflight first" >&2; exit 1; }

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
	# prepends and every older version stays readable.
	set_key ROOT_KEYS "1:$(openssl rand -base64 32)"
	echo "wrote ROOT_KEYS to $ENV_FILE"
fi

current_url="$(read_key PUBLIC_URL)"
printf 'Where do browsers reach this install? [%s] ' "${current_url:-http://localhost:8080}"
read -r answer || answer=""
public_url="${answer:-${current_url:-http://localhost:8080}}"
set_key PUBLIC_URL "$public_url"
set_key AUTH_MODE accounts

cd "$ROOT/backend"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
AUTH_MODE=none PUBLIC_URL="$public_url" "$PY" -m app.enable
