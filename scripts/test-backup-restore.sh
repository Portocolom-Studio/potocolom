#!/usr/bin/env bash
# Prove that a self-hosted backup restores the database and the stored image
# files together, from one recovery point, into an isolated environment: sign
# in to the restored account with a password and a fresh TOTP code, open the
# saved master and its thumbnail, and fail when the backup or the restored
# volume is missing anything. Documented in
# docs/self-hosting.md#backup-and-restore; run it with `make verify-backup`.
set -euo pipefail

umask 077
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SMOKE_FILE="$ROOT/deploy/compose/compose.smoke.yml"

mkdir -p /tmp/opencode
WORK="$(mktemp -d /tmp/opencode/p693-backup-restore-XXXXXX)"
ENVF="$WORK/source.env"
JAR="$WORK/cookies.txt"
BACKUP="$WORK/backup"
SRC="p693-src-$$"
DST="p693-dst-$$"
NEG="p693-neg-$$"
NOREF="p693-noref-$$"
EMAIL="admin@example.com"
PASSWORD="backup-restore-proof-password"
COMPOSE_FILE="$SMOKE_FILE"
BASE=""
CHECKS=0
FAILURES=0

pass() {
	CHECKS=$((CHECKS + 1))
	echo "PASS: $1"
}

fail() {
	CHECKS=$((CHECKS + 1))
	FAILURES=$((FAILURES + 1))
	echo "FAIL: $1"
}

abort() {
	echo "FAIL: $1" >&2
	if [[ -n "${2:-}" ]]; then printf '%s\n' "$2" >&2; fi
	exit 1
}

# The release workflow pins the published images the same way
# scripts/compose-smoke.sh does, as an extra compose file on top of the smoke
# stack. The env vars travel into backup.sh and restore.sh through the shell.
if [[ -n "${COMPOSE_SMOKE_API_IMAGE:-}${COMPOSE_SMOKE_WORKER_IMAGE:-}" ]]; then
	: "${COMPOSE_SMOKE_API_IMAGE:?both smoke images must be set}"
	: "${COMPOSE_SMOKE_WORKER_IMAGE:?both smoke images must be set}"
	cat >"$WORK/images.yml" <<'YAML'
services:
  api:
    image: ${COMPOSE_SMOKE_API_IMAGE}
  worker-sim:
    image: ${COMPOSE_SMOKE_WORKER_IMAGE}
    environment:
      DEVICE: cpu
      MODELS_DIR: ""
YAML
	COMPOSE_FILE="$SMOKE_FILE:$WORK/images.yml"
	BUILD=(--no-build --pull never)
else
	BUILD=(--build)
fi
export COMPOSE_FILE

file_flags=()
IFS=':' read -r -a compose_files <<<"$COMPOSE_FILE"
for file in "${compose_files[@]}"; do file_flags+=(-f "$file"); done

src() {
	docker compose -p "$SRC" --env-file "$ENVF" "${file_flags[@]}" "$@"
}

dst() {
	docker compose -p "$DST" --env-file "$BACKUP/env" "${file_flags[@]}" "$@"
}

neg() {
	docker compose -p "$NEG" --env-file "$BACKUP/env" "${file_flags[@]}" "$@"
}

# Nothing of this project ever runs: restore.sh refuses it before creating a
# container. The trap still tears it down, because a regression that did
# create one must not leave it behind.
noref() {
	docker compose -p "$NOREF" --env-file "$BACKUP/env" "${file_flags[@]}" "$@"
}

cleanup() {
	status=$?
	trap - EXIT
	for project in src dst neg noref; do
		"$project" down -v --remove-orphans >/dev/null 2>&1 || true
	done
	# The images are tagged per project and built only by a run that did not
	# pin published ones; keeping them grows the image list on every run.
	if [[ -z "${COMPOSE_SMOKE_API_IMAGE:-}" ]]; then
		for project in "$SRC" "$DST" "$NEG" "$NOREF"; do
			docker image rm -f "${project}-api" "${project}-worker-sim" \
				>/dev/null 2>&1 || true
		done
	fi
	rm -rf "$WORK"
	exit "$status"
}
trap cleanup EXIT

free_port() {
	python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

wait_healthy() {
	local url="$1"
	for _ in $(seq 1 90); do
		if curl -sf --max-time 5 "$url/api/v1/health" >/dev/null 2>&1; then return 0; fi
		sleep 2
	done
	return 1
}

STATUS=000
BODY=""
call() { # METHOD PATH [JSON-BODY]; sets STATUS and BODY
	local method="$1" path="$2" data="${3:-}" csrf out
	local args=(-sS -X "$method" -b "$JAR" -c "$JAR" -H "Origin: $BASE"
		-o - -w $'\n%{http_code}' --max-time 60)
	csrf="$(awk '$6 == "potocolom_csrf" { print $7 }' "$JAR" 2>/dev/null | tail -n 1)"
	if [[ -n "$csrf" ]]; then args+=(-H "X-CSRF-Token: $csrf"); fi
	if [[ -n "$data" ]]; then args+=(-H "Content-Type: application/json" --data "$data"); fi
	out="$(curl "${args[@]}" "$BASE$path" 2>>"$WORK/curl.log" || true)"
	STATUS="${out##*$'\n'}"
	BODY="${out%$'\n'*}"
	if ! [[ "$STATUS" =~ ^[0-9]{3}$ ]]; then STATUS=000; fi
}

json_field() { # NAME; the JSON document on stdin
	python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

fetch_asset() { # ID FILE; prints the sha256 of the served bytes
	local code
	code="$(curl -sS -b "$JAR" -o "$2" -w '%{http_code}' --max-time 60 \
		"$BASE/api/v1/assets/$1" || true)"
	if [[ "$code" != 200 ]]; then
		echo "GET /api/v1/assets/$1 answered HTTP ${code:-000}" >&2
		return 1
	fi
	sha256sum "$2" | awk '{print $1}'
}

# A six-digit code for the authenticator's current step, with the RFC 6238
# parameters backend/app/totp.py uses, so no test dependency is needed. Waits
# until a step newer than $2 has begun: one step's code is accepted once.
totp_code() { # SECRET LAST_STEP
	python3 - "$1" "$2" <<'PY'
import base64
import hashlib
import hmac
import struct
import sys
import time

secret = sys.argv[1]
last = int(sys.argv[2])
while int(time.time()) // 30 <= last:
    time.sleep(1)
counter = struct.pack(">Q", int(time.time()) // 30)
digest = hmac.new(base64.b32decode(secret), counter, hashlib.sha1).digest()
offset = digest[-1] & 0x0F
value = int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF
print(str(value % 1000000).zfill(6))
PY
}

last_step() { # COMPOSE-FUNCTION; the newest TOTP step the account already spent
	local step
	step="$($1 exec -T postgres psql -v ON_ERROR_STOP=1 -At -U potocolom -d potocolom \
		-c 'SELECT COALESCE(MAX(last_step), -1) FROM auth_factors' | tr -d '[:space:]')"
	echo "${step:--1}"
}

storage_keys_ok() { # COMPOSE-FUNCTION PROJECT
	local fn="$1" project="$2" keys
	keys="$($fn exec -T postgres psql -v ON_ERROR_STOP=1 -At -U potocolom -d potocolom \
		-c 'SELECT storage_key FROM assets ORDER BY storage_key')" || return 1
	if [[ -z "$keys" ]]; then
		echo "the database holds no asset rows" >&2
		return 1
	fi
	printf '%s\n' "$keys" | docker run --rm -i \
		--mount "type=volume,src=${project}_assets,dst=/data,readonly" \
		postgres:16 sh -c '
			missing=0
			while IFS= read -r key; do
				[ -n "$key" ] || continue
				if [ ! -f "/data/$key" ]; then
					echo "no file in the volume for storage key: $key" >&2
					missing=1
				fi
			done
			exit $missing'
}

sign_in() { # COMPOSE-FUNCTION; password plus a fresh TOTP code, ending at /account
	local code
	code="$(totp_code "$TOTP_SECRET" "$(last_step "$1")")"
	call POST /api/v1/auth/login "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}"
	[[ "$STATUS" == 200 && "$BODY" == *totp_required* ]] || return 1
	call POST /api/v1/auth/totp "{\"code\":\"$code\"}"
	[[ "$STATUS" == 204 ]] || return 1
	call GET /api/v1/account
	[[ "$STATUS" == 200 && "$BODY" == *"$EMAIL"* ]]
}

set_env() { # KEY VALUE: one env-file line, the way scripts/auth-enable.sh writes it
	local key="$1" value="$2" tmp
	tmp="$(mktemp "$WORK/env.XXXXXX")"
	awk -v k="$key" -v v="$value" -F= '$1 == k { print k "=" v; next } { print }' "$ENVF" >"$tmp"
	if ! grep -q "^${key}=" "$ENVF"; then printf '%s=%s\n' "$key" "$value" >>"$tmp"; fi
	mv "$tmp" "$ENVF"
}

# --- the source install -----------------------------------------------------

export COMPOSE_SMOKE_PORT="$(free_port)"
BASE="http://localhost:$COMPOSE_SMOKE_PORT"
touch "$JAR"
read -r DB_PASSWORD FLEET_HEX ROOT_KEY < <(python3 -c '
import base64, secrets
print(secrets.token_hex(32), secrets.token_hex(32),
      "1:" + base64.b64encode(secrets.token_bytes(32)).decode())')
cat >"$ENVF" <<EOF
COMPOSE_PROJECT_NAME=$SRC
SMOKE_AUTH_MODE=none
POSTGRES_PASSWORD=$DB_PASSWORD
FLEET_SECRET=$FLEET_HEX
SMOKE_ROOT_KEYS=$ROOT_KEY
EOF
chmod 600 "$ENVF"

echo "starting the source stack on $BASE"
for attempt in 1 2 3; do
	if src up -d "${BUILD[@]}" --remove-orphans >"$WORK/up.log" 2>&1; then break; fi
	if [[ "$attempt" == 3 ]]; then
		cat "$WORK/up.log" >&2
		abort "the source stack did not start"
	fi
	src down -v --remove-orphans >/dev/null 2>&1 || true
	# Another job on this machine can take the port between the probe and the
	# bind, so take another port and try again rather than report a clash.
	export COMPOSE_SMOKE_PORT="$(free_port)"
	BASE="http://localhost:$COMPOSE_SMOKE_PORT"
	echo "the start failed; retrying on $BASE"
done
wait_healthy "$BASE" || abort "the source API never became healthy"

# Turn accounts on before the API first starts in that mode, the order
# scripts/auth-enable.sh documents: the switch is recorded in PostgreSQL, the
# one-use setup link is minted, and only then does AUTH_MODE change.
if ! enable_out="$(src run --rm --no-deps -T api python -m app.enable 2>"$WORK/enable.log")"; then
	cat "$WORK/enable.log" >&2
	abort "accounts could not be enabled on the source project"
fi
token="$(printf '%s\n' "$enable_out" | sed -n 's/.*"token": "\([^"]*\)".*/\1/p' | head -n 1)"
[[ -n "$token" ]] || abort "no one-use setup link was minted"
set_env SMOKE_AUTH_MODE accounts
src up -d --remove-orphans
wait_healthy "$BASE" || abort "the source API never came up in accounts mode"

call POST /api/v1/auth/setup "{\"token\":\"$token\",\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}"
[[ "$STATUS" == 204 ]] || abort "claiming the installation answered HTTP $STATUS" "$BODY"

# Enrolling needs recent authentication, which claiming deliberately does not
# grant, so sign in first; the sign-in itself still opens a plain session
# because the account has no second factor yet.
call POST /api/v1/auth/login "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}"
[[ "$STATUS" == 204 ]] || abort "signing in to the new administrator answered HTTP $STATUS" "$BODY"

call POST /api/v1/account/totp
[[ "$STATUS" == 200 ]] || abort "starting a second factor answered HTTP $STATUS" "$BODY"
TOTP_SECRET="$(json_field secret <<<"$BODY")"
enrolment="$(json_field enrolment <<<"$BODY")"
call POST /api/v1/account/totp/confirm \
	"{\"enrolment\":\"$enrolment\",\"code\":\"$(totp_code "$TOTP_SECRET" -1)\"}"
[[ "$STATUS" == 204 ]] || abort "confirming the second factor answered HTTP $STATUS" "$BODY"

registered=0
for _ in $(seq 1 60); do
	call GET /api/v1/models
	if [[ "$STATUS" == 200 && "$BODY" == *sd-sim* ]]; then
		registered=1
		break
	fi
	sleep 1
done
[[ "$registered" == 1 ]] || abort "the simulated worker never offered sd-sim"

call POST /api/v1/generations '{"model_id":"sd-sim","params":{"prompt":"backup and restore proof"}}'
[[ "$STATUS" == 202 ]] || abort "queueing a generation answered HTTP $STATUS" "$BODY"
job_id="$(json_field job_id <<<"$BODY")"
state=""
for _ in $(seq 1 60); do
	call GET "/api/v1/generations/$job_id"
	state="$(json_field state <<<"$BODY")"
	if [[ "$state" == succeeded || "$state" == failed ]]; then break; fi
	sleep 1
done
[[ "$state" == succeeded ]] || abort "the generation ended as $state" "$BODY"

read -r master_id thumb_id < <(python3 -c '
import json, sys
assets = json.load(sys.stdin).get("assets") or []
if not assets or not assets[0].get("thumbnail_url"):
    sys.exit("the job saved no master with a thumbnail")
print(assets[0]["id"], assets[0]["thumbnail_url"].rsplit("/", 1)[-1])' <<<"$BODY") \
	|| abort "the generation saved no master and thumbnail" "$BODY"
master_key="$(src exec -T postgres psql -v ON_ERROR_STOP=1 -At -U potocolom -d potocolom \
	-c "SELECT storage_key FROM assets WHERE id = '$master_id'" | tr -d '[:space:]')"
[[ -n "$master_key" ]] || abort "the master asset has no storage key"
master_sha="$(fetch_asset "$master_id" "$WORK/master.png")" \
	|| abort "the master image did not open on the source project"
thumb_sha="$(fetch_asset "$thumb_id" "$WORK/thumb.webp")" \
	|| abort "the thumbnail did not open on the source project"

# --- one recovery point -----------------------------------------------------

echo "backing up the source project"
if COMPOSE_PROJECT_NAME="$SRC" ENV_FILE="$ENVF" COMPOSE_FILE="$COMPOSE_FILE" \
	"$ROOT/scripts/backup.sh" "$BACKUP" >"$WORK/backup.log" 2>&1; then
	cat "$WORK/backup.log"
else
	cat "$WORK/backup.log" >&2
	abort "backup.sh could not write a backup"
fi

if [[ -f "$BACKUP/database.dump" && -f "$BACKUP/assets.tar" && -f "$BACKUP/env" \
	&& -f "$BACKUP/SHA256SUMS" ]] \
	&& (cd "$BACKUP" && sha256sum --check SHA256SUMS >/dev/null 2>&1); then
	pass "the backup holds database.dump, assets.tar and env with matching checksums"
else
	fail "the backup holds database.dump, assets.tar and env with matching checksums"
	abort "an incomplete backup cannot be restored"
fi

src down -v --remove-orphans
if docker volume inspect "${SRC}_pgdata" >/dev/null 2>&1 \
	|| docker volume inspect "${SRC}_assets" >/dev/null 2>&1; then
	fail "down -v removed the source project's volumes"
else
	pass "down -v removed the source project's volumes"
fi

# --- the restored install ---------------------------------------------------

export COMPOSE_SMOKE_PORT="$(free_port)"
BASE="http://localhost:$COMPOSE_SMOKE_PORT"
echo "restoring into a new project on $BASE"
if COMPOSE_PROJECT_NAME="$DST" COMPOSE_SMOKE_PORT="$COMPOSE_SMOKE_PORT" \
	"$ROOT/scripts/restore.sh" "$BACKUP" >"$WORK/restore.log" 2>&1; then
	cat "$WORK/restore.log"
	pass "restore.sh restored the backup into a new, empty project"
else
	cat "$WORK/restore.log" >&2
	abort "restore.sh could not restore the backup"
fi
wait_healthy "$BASE" || abort "the restored API never became healthy"

rm -f "$JAR"
touch "$JAR"
if ! sign_in dst; then
	fail "signing in to the restored account with password and a fresh TOTP code"
	abort "without a restored session the image checks cannot run"
fi
pass "signing in to the restored account with password and a fresh TOTP code"

if sha="$(fetch_asset "$master_id" "$WORK/restored.png")" && [[ "$sha" == "$master_sha" ]]; then
	pass "the restored master image opens with its original sha256"
else
	fail "the restored master image opens with its original sha256"
fi
if sha="$(fetch_asset "$thumb_id" "$WORK/restored-thumb.webp")" && [[ "$sha" == "$thumb_sha" ]]; then
	pass "the restored thumbnail opens with its original sha256"
else
	fail "the restored thumbnail opens with its original sha256"
fi
if storage_keys_ok dst "$DST"; then
	pass "every assets.storage_key row resolves to a file in the restored volume"
else
	fail "every assets.storage_key row resolves to a file in the restored volume"
fi
dst down -v --remove-orphans

# --- the backups that must be refused or caught -----------------------------

echo "checking a backup with database.dump removed"
cp -a "$BACKUP" "$WORK/no-database"
rm "$WORK/no-database/database.dump"
if COMPOSE_PROJECT_NAME="$NOREF" COMPOSE_FILE="$COMPOSE_FILE" \
	"$ROOT/scripts/restore.sh" "$WORK/no-database" >"$WORK/refuse.log" 2>&1; then
	fail "restore.sh refused a backup with database.dump removed"
elif docker volume inspect "${NOREF}_pgdata" >/dev/null 2>&1 \
	|| docker volume inspect "${NOREF}_assets" >/dev/null 2>&1; then
	fail "restore.sh refused the backup but created a volume of the target project anyway"
elif ! grep -q "missing database.dump" "$WORK/refuse.log"; then
	# A refusal for any other reason would pass the line above while proving
	# nothing about the gate this case exists to test.
	fail "restore.sh refused the backup for another reason than the missing database.dump"
	cat "$WORK/refuse.log" >&2
else
	pass "restore.sh refused a backup with database.dump removed"
fi

echo "checking a backup whose assets.tar lacks one image"
cp -a "$BACKUP" "$WORK/short-assets"
mkdir "$WORK/unpacked"
tar -xf "$WORK/short-assets/assets.tar" -C "$WORK/unpacked"
[[ -f "$WORK/unpacked/$master_key" ]] || abort "the master file is not inside assets.tar"
rm "$WORK/unpacked/$master_key"
tar -cf "$WORK/short-assets/assets.tar" -C "$WORK/unpacked" .
(cd "$WORK/short-assets" && sha256sum database.dump assets.tar env >SHA256SUMS)

export COMPOSE_SMOKE_PORT="$(free_port)"
BASE="http://localhost:$COMPOSE_SMOKE_PORT"
if COMPOSE_PROJECT_NAME="$NEG" COMPOSE_SMOKE_PORT="$COMPOSE_SMOKE_PORT" \
	"$ROOT/scripts/restore.sh" "$WORK/short-assets" >"$WORK/restore-short.log" 2>&1; then
	cat "$WORK/restore-short.log"
else
	cat "$WORK/restore-short.log" >&2
	abort "restore.sh could not restore the backup whose assets.tar lacks one image"
fi
wait_healthy "$BASE" || abort "the API of the incomplete restore never became healthy"

if keys_out="$(storage_keys_ok neg "$NEG" 2>&1)"; then
	fail "the storage-key check caught the image missing from assets.tar"
elif ! grep -qF "$master_key" <<<"$keys_out"; then
	# The check has to fail on the file this case took out of assets.tar, not
	# on some other fault of the restored stack.
	fail "the storage-key check failed for another reason than the missing master file"
	echo "$keys_out" >&2
else
	echo "$keys_out" >&2
	pass "the storage-key check caught the image missing from assets.tar"
fi

rm -f "$JAR"
touch "$JAR"
if ! sign_in neg; then
	abort "the incomplete restore cannot be signed in to, so the image check proves nothing"
fi
if fetch_asset "$master_id" "$WORK/missing.png" >/dev/null 2>&1; then
	fail "the image missing from assets.tar fails to open through the API"
else
	pass "the image missing from assets.tar fails to open through the API"
fi
neg down -v --remove-orphans

echo
if ((FAILURES > 0)); then
	echo "backup/restore test FAILED: $FAILURES of $CHECKS checks did not pass"
	exit 1
fi
echo "backup/restore test passed: $CHECKS checks"
