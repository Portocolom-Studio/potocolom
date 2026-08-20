#!/usr/bin/env bash
# Create deploy/compose/.env from the example when missing, and fill empty
# POSTGRES_PASSWORD / FLEET_SECRET. Never overwrites a non-empty value.
# ENV_FILE / ENV_EXAMPLE override paths (verify-guards).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/deploy/compose/.env}"
ENV_EXAMPLE="${ENV_EXAMPLE:-$ROOT/deploy/compose/.env.example}"

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

if [[ ! -e "$ENV_FILE" ]]; then
	write_new
else
	key_filled POSTGRES_PASSWORD || fill_key POSTGRES_PASSWORD
	key_filled FLEET_SECRET || fill_key FLEET_SECRET
fi
