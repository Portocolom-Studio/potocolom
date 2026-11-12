#!/usr/bin/env bash
# Run shellcheck at error severity on every tracked shell script, using a pinned
# release (issue #866).
#
#   scripts/shellcheck.sh
#
# SHELLCHECK_BIN may point at an existing shellcheck binary (local runs, tests);
# otherwise the pinned release tarball is downloaded and SHA-256 verified.
# Any error-severity message fails the run. Warnings and info messages are not
# checked yet; the severity is raised in later changes.
set -euo pipefail

SHELLCHECK_VERSION="0.11.0"
SHELLCHECK_URL="https://github.com/koalaman/shellcheck/releases/download/v${SHELLCHECK_VERSION}/shellcheck-v${SHELLCHECK_VERSION}.linux.x86_64.tar.xz"
# The release publishes no checksum file. This value is the digest GitHub reports
# for the asset (releases API, assets[].digest for v0.11.0), and it matches a
# fresh download of the asset.
SHELLCHECK_SHA256="8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198"

if [ "$#" -ne 0 ]; then
	echo "usage: scripts/shellcheck.sh" >&2
	exit 2
fi

work_dir=""
cleanup() {
	if [ -n "$work_dir" ]; then
		rm -rf "$work_dir"
	fi
	return 0
}
trap cleanup EXIT INT TERM

install_shellcheck() {
	work_dir=$(mktemp -d)
	local tarball="$work_dir/shellcheck.tar.xz"
	if command -v curl >/dev/null 2>&1; then
		curl -fsSL -o "$tarball" "$SHELLCHECK_URL"
	elif command -v wget >/dev/null 2>&1; then
		wget -qO "$tarball" "$SHELLCHECK_URL"
	else
		echo "error: curl or wget is required to download shellcheck ${SHELLCHECK_VERSION}" >&2
		exit 1
	fi
	# Refuse a tarball that is not exactly the reviewed release.
	if ! echo "${SHELLCHECK_SHA256}  ${tarball}" | sha256sum -c -; then
		echo "error: ${SHELLCHECK_URL} does not match SHA-256 ${SHELLCHECK_SHA256}; refusing to run" >&2
		exit 1
	fi
	tar -xJf "$tarball" -C "$work_dir" --strip-components=1 "shellcheck-v${SHELLCHECK_VERSION}/shellcheck"
	SHELLCHECK_BIN="$work_dir/shellcheck"
}

# The file list comes from git, so the run covers exactly the tracked scripts.
cd "$(dirname "$0")/.."
files=()
while IFS= read -r -d '' file; do
	files+=("$file")
done < <(git ls-files -z '*.sh')
if [ "${#files[@]}" -eq 0 ]; then
	echo "error: git ls-files lists no tracked .sh files; run this from a git checkout" >&2
	exit 2
fi

if [ -n "${SHELLCHECK_BIN:-}" ]; then
	if [ ! -x "$SHELLCHECK_BIN" ]; then
		echo "error: SHELLCHECK_BIN=${SHELLCHECK_BIN} is not an executable" >&2
		exit 2
	fi
else
	install_shellcheck
fi

"$SHELLCHECK_BIN" --severity=error "${files[@]}"
echo "shellcheck: ${#files[@]} tracked shell scripts, no error-severity messages"
