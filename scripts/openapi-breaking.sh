#!/usr/bin/env bash
# Fail a change that breaks the API the base branch serves (issue #984).
#
#   OPENAPI_BASE_REF=origin/main scripts/openapi-breaking.sh
#
# Runs oasdiff breaking from the base ref's backend/openapi.json to the working
# tree's copy, and fails on any error-level change that backend/openapi-breaking-allow.txt
# does not list. A base ref with no snapshot yet is skipped. OASDIFF_BIN may point at an
# existing oasdiff binary (local runs, tests); otherwise the pinned release tarball is
# downloaded and SHA-256 verified.
set -euo pipefail

OASDIFF_VERSION="1.34.1"
OASDIFF_URL="https://github.com/oasdiff/oasdiff/releases/download/v${OASDIFF_VERSION}/oasdiff_${OASDIFF_VERSION}_linux_amd64.tar.gz"
# The SHA-256 GitHub reports as the asset digest for this release, and it matches a
# fresh download of the asset.
OASDIFF_SHA256="75775f65b0adebd26a8e013af51a799342dfdc6262d89efa9e3b7bdf89657d90"

SNAPSHOT="backend/openapi.json"
ALLOW="backend/openapi-breaking-allow.txt"
BASE_REF="${OPENAPI_BASE_REF:-origin/main}"

if [ "$#" -ne 0 ]; then
	echo "usage: scripts/openapi-breaking.sh" >&2
	exit 2
fi

work_dir=$(mktemp -d)
cleanup() {
	rm -rf "$work_dir"
	return 0
}
trap cleanup EXIT INT TERM

install_oasdiff() {
	local tarball="$work_dir/oasdiff.tar.gz"
	# The pinned asset is the Linux x86_64 build, which CI and the dev host run.
	if [ "$(uname -s)" != Linux ] || [ "$(uname -m)" != x86_64 ]; then
		echo "error: the pinned oasdiff build is Linux x86_64; on $(uname -s) $(uname -m) set OASDIFF_BIN to an oasdiff ${OASDIFF_VERSION} binary" >&2
		exit 2
	fi
	if command -v curl >/dev/null 2>&1; then
		curl -fsSL -o "$tarball" "$OASDIFF_URL"
	elif command -v wget >/dev/null 2>&1; then
		wget -qO "$tarball" "$OASDIFF_URL"
	else
		echo "error: curl or wget is required to download oasdiff ${OASDIFF_VERSION}" >&2
		exit 1
	fi
	# Refuse a tarball that is not exactly the reviewed release.
	if ! echo "${OASDIFF_SHA256}  ${tarball}" | sha256sum -c -; then
		echo "error: ${OASDIFF_URL} does not match SHA-256 ${OASDIFF_SHA256}; refusing to run" >&2
		exit 1
	fi
	tar -xzf "$tarball" -C "$work_dir" oasdiff
	OASDIFF_BIN="$work_dir/oasdiff"
}

# The paths below are relative to the repository root, so run from there.
cd "$(dirname "$0")/.."

if ! git rev-parse --verify --quiet "${BASE_REF}^{commit}" >/dev/null; then
	echo "error: base ref ${BASE_REF} is not a commit in this checkout; fetch it or set OPENAPI_BASE_REF" >&2
	exit 2
fi
# Compare against the commit the branch forked from, so routes that main added
# later do not read as removals. CI fetches the base at depth 1 and checks out
# the merge commit, where the base tip is already the right comparison.
BASE=$(git merge-base "$BASE_REF" HEAD 2>/dev/null || echo "$BASE_REF")

if ! git cat-file -e "${BASE}:${SNAPSHOT}" 2>/dev/null; then
	echo "openapi-breaking: skipped, ${BASE_REF} has no ${SNAPSHOT} yet, so there is no base API to compare"
	exit 0
fi
if [ ! -f "$SNAPSHOT" ]; then
	echo "error: ${SNAPSHOT} is missing; generate it with scripts/openapi-snapshot.py" >&2
	exit 2
fi
if [ ! -f "$ALLOW" ]; then
	echo "error: ${ALLOW} is missing; it lists the intended breaks and may be empty" >&2
	exit 2
fi
git show "${BASE}:${SNAPSHOT}" >"$work_dir/base.json"

if [ -n "${OASDIFF_BIN:-}" ]; then
	if [ ! -x "$OASDIFF_BIN" ]; then
		echo "error: OASDIFF_BIN=${OASDIFF_BIN} is not an executable" >&2
		exit 2
	fi
else
	install_oasdiff
fi

status=0
# oasdiff reads every line, so a commented-out exception would still apply.
grep -v '^[[:space:]]*#' "$ALLOW" >"$work_dir/allow.txt" || true
"$OASDIFF_BIN" breaking --fail-on ERR --err-ignore "$work_dir/allow.txt" "$work_dir/base.json" "$SNAPSHOT" || status=$?
if [ "$status" -ne 0 ]; then
	echo "error: oasdiff exited ${status}. Exit 1 means an error-level change breaks ${BASE_REF}: keep the response compatible, or list the intended break in ${ALLOW} with a decision reference (docs/local-development.md)." >&2
	exit "$status"
fi
echo "openapi-breaking: no error-level change against ${BASE_REF}"
