#!/usr/bin/env bash
# Canary self-test for scripts/secret-scan.sh. A GitHub token built at run
# time is planted in a temp directory: the scan must fail on it and must never
# print it. A second directory of plain placeholders must pass untouched.
# Needs no network when GITLEAKS_BIN is set.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
scan="$root/scripts/secret-scan.sh"

failures=0
temp_dirs=()
cleanup() {
	local dir
	for dir in ${temp_dirs[@]+"${temp_dirs[@]}"}; do
		rm -rf "$dir"
	done
	return 0
}
trap cleanup EXIT

scan_out=""
scan_code=0

# Runs `secret-scan.sh tree` in the given directory, capturing stdout and
# stderr together so the canary check sees everything the scanner prints.
run_tree_scan() {
	local dir="$1"
	scan_code=0
	scan_out=$(cd "$dir" && bash "$scan" tree 2>&1) || scan_code=$?
}

if [ -n "${GITLEAKS_BIN:-}" ]; then
	echo "self-test: using GITLEAKS_BIN=${GITLEAKS_BIN} (no network)"
else
	echo "self-test: GITLEAKS_BIN is unset; each scan downloads the pinned gitleaks release"
fi

# Case 1: a run-time GitHub personal access token must be caught, and its
# value must never appear in any output. The token is generated here so the
# repository itself never contains a detectable credential.
canary_dir=$(mktemp -d)
temp_dirs+=("$canary_dir")
canary="ghp_$(head -c 1000 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9' | head -c 36)"
printf 'GITHUB_TOKEN=%s\n' "$canary" >"$canary_dir/canary.env"
run_tree_scan "$canary_dir"

case_failed=0
if [ "$scan_code" -eq 0 ]; then
	echo "FAIL: canary: the scan exited 0; a planted GitHub token was not detected"
	case_failed=1
fi
if printf '%s' "$scan_out" | grep -qF "$canary"; then
	echo "FAIL: canary: the scanner printed the token it was meant to find"
	case_failed=1
fi
if ! printf '%s' "$scan_out" | grep -qF 'canary.env'; then
	echo "FAIL: canary: the output never names canary.env, so the finding was not reported"
	case_failed=1
fi
if [ "$case_failed" -eq 0 ]; then
	echo "PASS: canary: planted GitHub token detected (scan exit ${scan_code}); its value is absent from all output"
else
	echo "FAIL: canary: scanner output follows, with the token masked:"
	printf '%s\n' "$scan_out" | sed "s/${canary}/<masked>/g"
	failures=$((failures + 1))
fi

# Case 2: placeholder values people legitimately commit must not be flagged.
benign_dir=$(mktemp -d)
temp_dirs+=("$benign_dir")
printf '%s\n' 'API_KEY=changeme' 'token: <your-token-here>' >"$benign_dir/placeholders.env"
run_tree_scan "$benign_dir"

if [ "$scan_code" -eq 0 ]; then
	echo "PASS: benign: placeholder values accepted (scan exit 0)"
else
	echo "FAIL: benign: placeholders were flagged (scan exit ${scan_code}); scanner output follows:"
	printf '%s\n' "$scan_out"
	failures=$((failures + 1))
fi

if [ "$failures" -gt 0 ]; then
	echo "secret scan self-test: ${failures} case(s) failed"
	exit 1
fi
echo "secret scan self-test: all cases passed"
