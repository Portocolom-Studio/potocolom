#!/usr/bin/env bash
# Proves scripts/test-install-serial.sh: two suites that source it never run at
# the same time, and a background child that a suite leaves behind does not keep
# the next suite waiting (#831).
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)" || { echo 'error: mktemp failed' >&2; exit 1; }
trap 'rm -rf "$tmp"' EXIT
export POTOCOLOM_SUITE_LOCK_NAME="serial-lock-test-$$"
unset POTOCOLOM_SUITE_LOCK_HELD
failures=0

pass_case() { printf 'ok %s\n' "$1"; }
fail_case() { printf 'FAIL %s\n  %s\n' "$1" "$2"; failures=$((failures + 1)); }

# A suite that records when it runs, then optionally leaves a sleeping child.
cat >"$tmp/suite.sh" <<SUITE
#!/usr/bin/env bash
set -uo pipefail
source "$repo/scripts/test-install-serial.sh"
printf 'start %s %s\n' "\$1" "\$(date +%s%N)" >>"$tmp/log"
sleep 1
printf 'end %s %s\n' "\$1" "\$(date +%s%N)" >>"$tmp/log"
if [[ "\${2:-}" == leave-child ]]; then sleep 30 >/dev/null 2>&1 & echo "\$!" >"$tmp/child"; fi
SUITE

# 1. Two suites started together run one after the other.
: >"$tmp/log"
bash "$tmp/suite.sh" a & bash "$tmp/suite.sh" b & wait
read -r -a times < <(awk '{ print $3 }' "$tmp/log" | tr '\n' ' ')
order="$(awk '{ printf "%s%s ", $1, $2 }' "$tmp/log")"
if [[ "${#times[@]}" -eq 4 && ( "$order" == 'starta enda startb endb ' || "$order" == 'startb endb starta enda ' ) \
	&& "${times[2]}" -ge "${times[1]}" ]]; then
	pass_case suites-run-one-at-a-time
else
	fail_case suites-run-one-at-a-time "order: $order"
fi

# 2. A child left behind by one suite does not hold the lock for the next.
: >"$tmp/log"
bash "$tmp/suite.sh" first leave-child
started="$(date +%s)"
timeout 10 bash "$tmp/suite.sh" second && rc=0 || rc=$?
waited=$(($(date +%s) - started))
if [[ -s "$tmp/child" ]]; then kill "$(cat "$tmp/child")" 2>/dev/null || true; fi
if [[ "$rc" -eq 0 && "$waited" -lt 5 ]]; then
	pass_case leftover-child-does-not-hold-the-lock
else
	fail_case leftover-child-does-not-hold-the-lock "rc=$rc waited=${waited}s"
fi

if ((failures > 0)); then
	printf '%d suite lock check(s) failed\n' "$failures"
	exit 1
fi
printf 'suite lock checks ok\n'
