#!/usr/bin/env bash
# Lint the GitHub Actions workflows with pinned actionlint and zizmor releases.
#
#   scripts/lint-workflows.sh
#
# Run from the repository root; it lints .github/workflows. ACTIONLINT_BIN and
# ZIZMOR_BIN may point at existing binaries (local runs, tests). Otherwise each
# pinned release tarball is downloaded, checked against the SHA-256 below and
# kept in the cache directory, so a later run only re-checks it.
# Both tools always run, so one run reports every finding. The script fails if
# either tool fails.
set -euo pipefail

ACTIONLINT_VERSION="1.7.12"
ACTIONLINT_URL="https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz"
ACTIONLINT_SHA256="8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"

# zizmor publishes no checksum file. This is the SHA-256 GitHub reports as the
# asset digest for this release.
ZIZMOR_VERSION="1.31.1"
ZIZMOR_URL="https://github.com/zizmorcore/zizmor/releases/download/v${ZIZMOR_VERSION}/zizmor-x86_64-unknown-linux-gnu.tar.gz"
ZIZMOR_SHA256="d185f726bdaf92df78273217dfdd24ebcd6b0ef48cf3ce776ff5762b6e138c99"

WORKFLOW_DIR=".github/workflows"
ACTIONLINT_CONFIG=".github/actionlint.yaml"
ZIZMOR_CONFIG=".github/zizmor.yml"
CACHE_ROOT="${XDG_CACHE_HOME:-$HOME/.cache}/potocolom/lint-workflows"

work_dir="$(mktemp -d)"
cleanup() {
	rm -rf "$work_dir"
	return 0
}
trap cleanup EXIT INT TERM

download() {
	local url="$1" dest="$2"
	if command -v curl >/dev/null 2>&1; then
		curl -fsSL -o "$dest" "$url"
	elif command -v wget >/dev/null 2>&1; then
		wget -qO "$dest" "$url"
	else
		echo "error: curl or wget is required to download the workflow linters" >&2
		exit 1
	fi
}

# install_tool <name> <version> <url> <sha256> <member>: sets TOOL_BIN to the
# binary extracted from the verified tarball.
install_tool() {
	local name="$1" version="$2" url="$3" sha256="$4" member="$5"
	local dir="${CACHE_ROOT}/${name}-${version}"
	local tarball="${dir}/$(basename "$url")"
	if [ ! -f "$tarball" ] || ! echo "${sha256}  ${tarball}" | sha256sum -c --status -; then
		mkdir -p "$dir"
		local tmp
		tmp="$(mktemp "${dir}/download.XXXXXX")"
		download "$url" "$tmp"
		# Refuse a tarball that is not exactly the reviewed release.
		if ! echo "${sha256}  ${tmp}" | sha256sum -c -; then
			rm -f "$tmp"
			echo "error: ${url} does not match SHA-256 ${sha256}; refusing to run" >&2
			exit 1
		fi
		mv -f "$tmp" "$tarball"
	fi
	mkdir -p "${work_dir}/${name}"
	tar -xzf "$tarball" -C "${work_dir}/${name}" "$member"
	TOOL_BIN="${work_dir}/${name}/${member}"
}

if [ -n "${ACTIONLINT_BIN:-}" ]; then
	if [ ! -x "$ACTIONLINT_BIN" ]; then
		echo "error: ACTIONLINT_BIN=${ACTIONLINT_BIN} is not an executable" >&2
		exit 2
	fi
else
	install_tool actionlint "$ACTIONLINT_VERSION" "$ACTIONLINT_URL" "$ACTIONLINT_SHA256" actionlint
	ACTIONLINT_BIN="$TOOL_BIN"
fi

if [ -n "${ZIZMOR_BIN:-}" ]; then
	if [ ! -x "$ZIZMOR_BIN" ]; then
		echo "error: ZIZMOR_BIN=${ZIZMOR_BIN} is not an executable" >&2
		exit 2
	fi
else
	install_tool zizmor "$ZIZMOR_VERSION" "$ZIZMOR_URL" "$ZIZMOR_SHA256" zizmor
	ZIZMOR_BIN="$TOOL_BIN"
fi

actionlint_code=0
# Shellcheck and pyflakes are off so the result does not depend on what is on
# PATH; shell scripts are linted separately. The ignore covers the concurrency
# queue key, a GitHub setting that actionlint 1.7.12 does not know yet.
"$ACTIONLINT_BIN" -config-file "$ACTIONLINT_CONFIG" -shellcheck= -pyflakes= \
	-ignore 'unexpected key "queue" for "concurrency" section' \
	"$WORKFLOW_DIR"/*.yml || actionlint_code=$?

# Offline, so no GitHub API token is needed; the audits that need the network
# are not run.
zizmor_code=0
"$ZIZMOR_BIN" --offline --config "$ZIZMOR_CONFIG" "$WORKFLOW_DIR" || zizmor_code=$?

if [ "$actionlint_code" -ne 0 ] || [ "$zizmor_code" -ne 0 ]; then
	echo "workflow lint: actionlint exit ${actionlint_code}, zizmor exit ${zizmor_code}" >&2
	exit 1
fi
echo "workflow lint: actionlint and zizmor found no problems"
