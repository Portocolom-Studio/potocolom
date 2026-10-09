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
trap cleanup EXIT INT TERM

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
canary="ghp_$(python3 -c 'import secrets, string; print("".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36)))')"
printf 'GITHUB_TOKEN=%s\n' "$canary" >"$canary_dir/canary.env"
run_tree_scan "$canary_dir"

case_failed=0
if [ "$scan_code" -eq 0 ]; then
	echo "FAIL: canary: the scan exited 0; a planted GitHub token was not detected"
	case_failed=1
fi
if grep -qF "$canary" <<<"$scan_out"; then
	echo "FAIL: canary: the scanner printed the token it was meant to find"
	case_failed=1
fi
if ! grep -qF 'canary.env' <<<"$scan_out"; then
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

# Case 3: a token added only while resolving a merge conflict lives in no
# parent's diff, so the range scan must read merge diffs too.
merge_dir=$(mktemp -d)
temp_dirs+=("$merge_dir")
merge_token="ghp_$(python3 -c 'import secrets, string; print("".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36)))')"
(
	cd "$merge_dir"
	g() { git -c user.name=self-test -c user.email=self-test@example.invalid "$@"; }
	git init -q
	echo base >a.txt
	g add a.txt
	g commit -qm base
	git checkout -qb side
	echo side >a.txt
	g commit -qam side
	git checkout -q -
	echo main >a.txt
	g commit -qam main
	g merge -q side >/dev/null 2>&1 || true
	printf 'resolved\nGITHUB_TOKEN=%s\n' "$merge_token" >a.txt
	g add a.txt
	g commit -qm merge
)
scan_code=0
scan_out=$(cd "$merge_dir" && bash "$scan" range "$(git -C "$merge_dir" rev-list --max-parents=0 HEAD)" HEAD 2>&1) || scan_code=$?
if [ "$scan_code" -ne 0 ] && ! grep -qF "$merge_token" <<<"$scan_out"; then
	echo "PASS: merge: a token added in a merge resolution is detected (scan exit ${scan_code}) and not printed"
else
	echo "FAIL: merge: scan exit ${scan_code}; scanner output follows, with the token masked:"
	printf '%s\n' "$scan_out" | sed "s/${merge_token}/<masked>/g"
	failures=$((failures + 1))
fi

if [ "$failures" -gt 0 ]; then
	echo "secret scan self-test: ${failures} case(s) failed"
	exit 1
fi
echo "secret scan self-test: all cases passed"
