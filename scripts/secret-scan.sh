#!/usr/bin/env bash
# Scan for committed secrets with a pinned gitleaks release.
#
#   scripts/secret-scan.sh tree                  the checked-out working tree
#   scripts/secret-scan.sh range <base> <head>   only the commits in base..head
#   scripts/secret-scan.sh history               full history of the current branch
#
# GITLEAKS_BIN may point at an existing gitleaks binary (local runs, tests);
# otherwise the pinned release tarball is downloaded and SHA-256 verified.
# Findings are summarised by rule, file, line, commit and fingerprint only:
# the matched value is never printed, and .gitleaksignore allows one exact
# fingerprint at a time.
set -euo pipefail

GITLEAKS_VERSION="8.30.1"
GITLEAKS_URL="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"
GITLEAKS_SHA256="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"

usage() {
	cat >&2 <<'EOF'
usage: scripts/secret-scan.sh <mode>

  tree                 scan the checked-out working tree
  range <base> <head>  scan only the commits in base..head
  history              scan the full history of the current branch

Set GITLEAKS_BIN to use an existing gitleaks binary instead of downloading
the pinned release.
EOF
	exit 2
}

work_dir=""
report=""
cleanup() {
	if [ -n "$work_dir" ]; then
		rm -rf "$work_dir"
	fi
	if [ -n "$report" ]; then
		rm -f "$report"
	fi
	return 0
}
trap cleanup EXIT

install_gitleaks() {
	work_dir=$(mktemp -d)
	local tarball="$work_dir/gitleaks.tar.gz"
	if command -v curl >/dev/null 2>&1; then
		curl -fsSL -o "$tarball" "$GITLEAKS_URL"
	elif command -v wget >/dev/null 2>&1; then
		wget -qO "$tarball" "$GITLEAKS_URL"
	else
		echo "error: curl or wget is required to download gitleaks ${GITLEAKS_VERSION}" >&2
		exit 1
	fi
	# Refuse a tarball that is not exactly the reviewed release.
	if ! echo "${GITLEAKS_SHA256}  ${tarball}" | sha256sum -c -; then
		echo "error: ${GITLEAKS_URL} does not match SHA-256 ${GITLEAKS_SHA256}; refusing to run" >&2
		exit 1
	fi
	tar -xzf "$tarball" -C "$work_dir" gitleaks
	GITLEAKS_BIN="$work_dir/gitleaks"
}

mode="${1:-}"
case "$mode" in
	tree)
		scan_args=(dir)
		;;
	range)
		[ "$#" -eq 3 ] || usage
		scan_args=(git --log-opts "$2..$3")
		;;
	history)
		scan_args=(git --log-opts HEAD)
		;;
	*)
		usage
		;;
esac

if [ -n "${GITLEAKS_BIN:-}" ]; then
	if [ ! -x "$GITLEAKS_BIN" ]; then
		echo "error: GITLEAKS_BIN=${GITLEAKS_BIN} is not an executable" >&2
		exit 2
	fi
else
	install_gitleaks
fi

report="$(mktemp)"
scan_code=0
# --redact keeps the secret out of the report file as well as stdout and stderr.
"${GITLEAKS_BIN}" "${scan_args[@]}" --redact --no-banner -f json -r "$report" . || scan_code=$?

if [ "$scan_code" -eq 0 ]; then
	echo "secret scan (${mode}): no findings"
	exit 0
fi

if [ ! -s "$report" ]; then
	echo "secret scan (${mode}): gitleaks failed with exit code ${scan_code} and wrote no report" >&2
	exit "$scan_code"
fi

# Summarise the JSON report: rule, file, line, commit and fingerprint per
# finding, and nothing that could hold the matched value.
if ! command -v python3 >/dev/null 2>&1; then
	echo "error: python3 is required to summarise the gitleaks report" >&2
	exit 2
fi
if ! python3 - "$report" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    findings = json.load(handle)
for finding in findings:
    print(
        "finding: rule={RuleID} file={File} line={StartLine} commit={Commit} "
        "fingerprint={Fingerprint}".format(
            RuleID=finding.get("RuleID") or "-",
            File=finding.get("File") or "-",
            StartLine=finding.get("StartLine") or "-",
            Commit=finding.get("Commit") or "-",
            Fingerprint=finding.get("Fingerprint") or "-",
        )
    )
PY
then
	echo "error: could not read the gitleaks report written to the temp directory" >&2
	exit 2
fi

echo "secret scan (${mode}): findings are redacted above; a reviewed false positive is allowed by its exact fingerprint in .gitleaksignore"
exit "$scan_code"
