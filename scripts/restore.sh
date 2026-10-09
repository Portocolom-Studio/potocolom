#!/usr/bin/env bash
# Restore a backup made by scripts/backup.sh into a new, empty compose project.
# The flow is documented in docs/self-hosting.md#backup-and-restore and proved
# end to end by scripts/test-backup-restore.sh.
set -euo pipefail

usage() {
	cat <<'EOF'
usage: scripts/restore.sh <backup-dir>

Restores a backup made by scripts/backup.sh into a new, empty compose project.
It verifies SHA256SUMS and refuses if any of database.dump, assets.tar or env
is missing or does not match, refuses a project that already has a pgdata or
assets volume, then starts PostgreSQL alone, restores the dump, unpacks
assets.tar into the new assets volume, and starts the stack with the backup's
env file (which carries AUTH_MODE, ROOT_KEYS and every other setting).

The target project is COMPOSE_PROJECT_NAME when it is set, else the project
name recorded in the backup's env file.

Environment:
  COMPOSE_PROJECT_NAME  project to restore into
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
BACKUP="$1"
[[ -d "$BACKUP" ]] || die "$BACKUP is not a directory"
BACKUP="$(cd "$BACKUP" && pwd)"

for cmd in docker sha256sum tar; do
	command -v "$cmd" >/dev/null 2>&1 || die "$cmd is required"
done

# The gate comes first: a backup missing one of the three files restores half
# an installation, which is worse than no restore at all.
[[ -f "$BACKUP/SHA256SUMS" ]] || die "$BACKUP has no SHA256SUMS"
for name in database.dump assets.tar env; do
	[[ -f "$BACKUP/$name" ]] || die "the backup is missing $name"
	awk -v f="$name" '$2 == f { found = 1 } END { exit !found }' "$BACKUP/SHA256SUMS" \
		|| die "SHA256SUMS does not list $name"
done
(cd "$BACKUP" && sha256sum --check --strict SHA256SUMS) || die "SHA256SUMS does not match the backup"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$BACKUP/env"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/deploy/compose/compose.yml}"
IFS=':' read -r -a FILES <<<"$COMPOSE_FILE"
[[ -f "${FILES[0]}" ]] || die "${FILES[0]} does not exist"

# The same resolution backup.sh and docker compose use: the shell, then the
# env file, then the directory of the first compose file.
env_project="$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' "$ENV_FILE" | tail -n 1)"
dir_project="$(basename "$(dirname "${FILES[0]}")" \
	| tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]/-/g')"
PROJECT="${COMPOSE_PROJECT_NAME:-${env_project:-$dir_project}}"
COMPOSE=(docker compose -p "$PROJECT" --env-file "$ENV_FILE")
for file in "${FILES[@]}"; do COMPOSE+=(-f "$file"); done

for volume in pgdata assets; do
	if docker volume inspect "${PROJECT}_${volume}" >/dev/null 2>&1; then
		die "$PROJECT already has a ${volume} volume; restore into a new, empty project (set COMPOSE_PROJECT_NAME)"
	fi
done

CREATED=0
DATA_DONE=0
cleanup() {
	status=$?
	trap - EXIT
	if [[ $status -ne 0 && "$CREATED" == 1 && "$DATA_DONE" == 0 ]]; then
		# Nothing is worth keeping yet: a half-restored project would only
		# refuse the next attempt with the volume guard above.
		echo "the restore did not finish; removing the partial project $PROJECT" >&2
		"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
	elif [[ $status -ne 0 && "$DATA_DONE" == 1 ]]; then
		echo "the data was restored; the stack could not be started. Start it with:" >&2
		echo "  ${COMPOSE[*]} up -d" >&2
	fi
	exit "$status"
}
trap cleanup EXIT
CREATED=1

# Create the containers and the volumes without starting anything, so the
# assets volume exists to be unpacked into before the API can serve from it.
echo "creating the containers and volumes of $PROJECT"
"${COMPOSE[@]}" up --no-start

echo "starting postgres"
"${COMPOSE[@]}" up -d --wait --wait-timeout 120 postgres

echo "restoring the database"
"${COMPOSE[@]}" exec -T postgres pg_restore -U potocolom --exit-on-error -d potocolom \
	<"$BACKUP/database.dump"

echo "unpacking the asset files"
docker run --rm --mount "type=volume,src=${PROJECT}_assets,dst=/data" \
	--mount "type=bind,src=$BACKUP,dst=/in,readonly" \
	postgres:16 tar -C /data -xf /in/assets.tar
DATA_DONE=1

echo "starting the stack"
"${COMPOSE[@]}" up -d

echo "restored $PROJECT from $BACKUP"
