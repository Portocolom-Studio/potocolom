#!/usr/bin/env bash
# Back up one self-hosted install: database, asset files and the env file as a
# single recovery point. Restored by scripts/restore.sh; the whole flow is
# documented in docs/self-hosting.md#backup-and-restore and proved end to end
# by scripts/test-backup-restore.sh.
set -euo pipefail

usage() {
	cat <<'EOF'
usage: scripts/backup.sh <out-dir>

Writes <out-dir>/database.dump (pg_dump of PostgreSQL), <out-dir>/assets.tar
(the whole assets volume), <out-dir>/env (the env file, which holds ROOT_KEYS,
without which restored encrypted account data cannot be read) and
<out-dir>/SHA256SUMS over those three files. The directory and its files are
readable by the owner only.

The api service is stopped first, because it is the only writer of database
rows and asset files, so the dump and the volume are one recovery point. This
means a short outage: the install is unavailable while the backup runs. The
api is started again when the backup finishes or fails.

Environment:
  COMPOSE_PROJECT_NAME  project to back up (default: the name in the env file,
                        else the compose file's directory)
  ENV_FILE              env file to read settings from and to copy
                        (default deploy/compose/.env)
  COMPOSE_FILE          compose file, or two files separated by ':'
                        (default deploy/compose/compose.yml)
EOF
}

die() {
	echo "error: $*" >&2
	exit 1
}

case "${1:-}" in
-h | --help)
	usage
	exit 0
	;;
esac
[[ $# -eq 1 ]] || {
	usage >&2
	exit 1
}
OUT="$1"

for cmd in docker sha256sum tar; do
	command -v "$cmd" >/dev/null 2>&1 || die "$cmd is required"
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/deploy/compose/.env}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/deploy/compose/compose.yml}"
[[ -f "$ENV_FILE" ]] || die "$ENV_FILE does not exist"
IFS=':' read -r -a FILES <<<"$COMPOSE_FILE"
[[ -f "${FILES[0]}" ]] || die "${FILES[0]} does not exist"

# The project docker compose itself would pick: the shell, then the env file,
# then the directory of the first compose file.
env_project="$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' "$ENV_FILE" | tail -n 1)"
dir_project="$(basename "$(dirname "${FILES[0]}")" \
	| tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]/-/g')"
PROJECT="${COMPOSE_PROJECT_NAME:-${env_project:-$dir_project}}"
COMPOSE=(docker compose -p "$PROJECT" --env-file "$ENV_FILE")
for file in "${FILES[@]}"; do COMPOSE+=(-f "$file"); done

umask 077
mkdir -p "$OUT"
chmod 700 "$OUT"
OUT="$(cd "$OUT" && pwd)"
# A failed run must not leave the previous run's manifest beside this run's
# partial files: restore.sh trusts SHA256SUMS, and a stale one would describe
# files that are no longer there.
# This run writes beside them under .new names and swaps them in only once
# all three exist, so a failed run leaves the previous backup whole.
rm -f "$OUT"/*.new

RESTART_API=0
cleanup() {
	status=$?
	trap - EXIT INT TERM
	rm -f "$OUT"/*.new
	if [[ "$RESTART_API" == 1 ]] && ! "${COMPOSE[@]}" start api; then
		echo "error: the api service could not be started again; run: ${COMPOSE[*]} start api" >&2
		status=1
	fi
	exit "$status"
}
trap cleanup EXIT INT TERM

# Restart only an api that was running: an operator who stopped it on
# purpose gets it back stopped. Set before the stop, so an interrupt during
# the stop still brings it back.
if [[ -n "$("${COMPOSE[@]}" ps --status running --quiet api)" ]]; then
	RESTART_API=1
	echo "stopping the api service; the install is unavailable for this short outage"
	"${COMPOSE[@]}" stop api
fi

echo "dumping the database into $OUT/database.dump"
"${COMPOSE[@]}" exec -T postgres pg_dump -U potocolom -Fc potocolom >"$OUT/database.dump.new"

# Root in the throwaway container so the archive keeps the files' owners, and
# stdout rather than a bind mount so the host file lands under this user's
# umask instead of root's.
echo "archiving the assets volume into $OUT/assets.tar"
docker run --rm --mount "type=volume,src=${PROJECT}_assets,dst=/src,readonly" \
	postgres:16 tar -C /src -cf - . >"$OUT/assets.tar.new"

echo "copying $ENV_FILE into $OUT/env"
cp "$ENV_FILE" "$OUT/env.new"
chmod 600 "$OUT/env.new"

# The manifest is computed before anything is swapped, so a failure here
# (a full disk) still leaves the previous backup whole. What follows is
# renames within one directory, which do not fail for lack of space.
(cd "$OUT" && sha256sum database.dump.new assets.tar.new env.new \
	| sed 's/\.new$//' >SHA256SUMS.new)
chmod 600 "$OUT/SHA256SUMS.new"
rm -f "$OUT/SHA256SUMS"
for name in database.dump assets.tar env; do mv "$OUT/$name.new" "$OUT/$name"; done
mv "$OUT/SHA256SUMS.new" "$OUT/SHA256SUMS"

echo "backup written to $OUT"
