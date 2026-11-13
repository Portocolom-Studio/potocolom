#!/usr/bin/env bash
# Contract test for the frontend verify graph (Makefile target verify-frontend).
#
# The real Makefile runs with a fake npm first on PATH. The fake only appends
# start and end events to a log under flock, so no real npm, browser, database
# or vite build runs. No check asserts a duration. The parallel run makes each
# browser leaf wait (at most 3 s) for a second running leaf, so a peak of 2 comes
# from the graph and not from scheduling luck.
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
failures=0
rc=0

LANDING_LEAVES=(test:landing test:docs test:footer)
PRODUCT_LEAVES=(test:canvas test:models test:self-hosted test:studio-shell test:setup test:signin test:views test:owner-focus test:tour test:fleet test:gpu-card)

note_fail() {
	printf 'FAIL %s\n' "$1"
	failures=$((failures + 1))
}

mkdir -p "$scratch/bin"
cat >"$scratch/bin/npm" <<'EOF'
#!/usr/bin/env bash
# Fake npm: logs one start and one end event per call and never runs the real script.
set -u
case "${1:-}" in
	run) name="${2:-}" ;;
	test) name=test ;;
	*) name="unexpected:$*" ;;
esac
if [[ "$name" == build ]]; then
	if [[ -n "${PUBLIC_SITE_MODE+x}" ]]; then name="build:mode=$PUBLIC_SITE_MODE"; else name="build:mode=UNSET"; fi
fi
event() { # one line per event, appended under flock
	exec 9>>"$FE_GRAPH_LOG"
	flock 9
	printf '%s %s\n' "$1" "$name" >&9
	flock -u 9
	exec 9>&-
}
event start
if [[ "${FE_GRAPH_RENDEZVOUS:-0}" == 1 && "$name" == test:* ]]; then
	# Wait for a second running leaf. The wait is bounded, so a serial graph still ends.
	deadline=$((SECONDS + 3))
	while ((SECONDS < deadline)); do
		running=$(awk '$2 ~ /^test:/ { if ($1 == "start") n++; else if ($1 == "end") n-- } END { print n + 0 }' "$FE_GRAPH_LOG")
		((running >= 2)) && break
		sleep 0.05
	done
fi
sleep 0.3
event end
[[ -n "${FE_GRAPH_FAIL:-}" && "$name" == "$FE_GRAPH_FAIL" ]] && exit 1
exit 0
EOF
chmod +x "$scratch/bin/npm"

# run_make NAME RENDEZVOUS FAIL_CALL MAKE_ARGS...
# Runs the real Makefile from the repo. Sets rc to its exit status.
# Events go to $scratch/NAME.events and make output to $scratch/NAME.out.
run_make() {
	local name="$1" rendezvous="$2" fail="$3"
	shift 3
	: >"$scratch/$name.events"
	rc=0
	env -u MAKEFLAGS -u MFLAGS -u GNUMAKEFLAGS \
		PATH="$scratch/bin:$PATH" FE_GRAPH_LOG="$scratch/$name.events" \
		FE_GRAPH_RENDEZVOUS="$rendezvous" FE_GRAPH_FAIL="$fail" \
		make -C "$repo" --no-print-directory "$@" >"$scratch/$name.out" 2>&1 || rc=$?
}

show_output() { # NAME: last lines of a run's make output, for a failure report
	echo "--- make output ($1), last 40 lines:"
	tail -n 40 "$scratch/$1.out"
}

# check_events NAME MODE: MODE is serial or parallel. Prints one FAIL line per broken rule.
check_events() {
	python3 - "$scratch/$1.events" "$2" "$repo/frontend/package.json" <<'PY' || { note_fail "$1: call-graph checks failed"; show_output "$1"; }
import json
import sys
from collections import Counter

log_path, mode, package_json = sys.argv[1:4]
events = []
with open(log_path) as fh:
    for line in fh:
        kind, _, name = line.rstrip('\n').partition(' ')
        if kind:
            events.append((kind, name))

LANDING_BUILD = 'build:mode=landing'
PRODUCT_BUILD = 'build:mode='
STATIC = ['lint', 'check', 'test']
LANDING = ['test:landing', 'test:docs', 'test:footer']
PRODUCT = ['test:canvas', 'test:models', 'test:self-hosted', 'test:studio-shell',
           'test:setup', 'test:signin', 'test:views', 'test:owner-focus', 'test:tour',
           'test:fleet', 'test:gpu-card']
EXPECTED = Counter(STATIC + [LANDING_BUILD, PRODUCT_BUILD] + LANDING + PRODUCT)

failures = []

# The contract must name only npm scripts that exist.
scripts = json.load(open(package_json))['scripts']
for name in ['lint', 'check', 'test', 'build'] + LANDING + PRODUCT:
    if name not in scripts:
        failures.append(f'frontend/package.json has no script {name!r}')

# Exactly the expected calls, each one started and ended once.
started = Counter(name for kind, name in events if kind == 'start')
ended = Counter(name for kind, name in events if kind == 'end')
for name in sorted(set(EXPECTED) | set(started)):
    if started[name] != EXPECTED[name]:
        failures.append(f'npm calls for {name!r}: expected {EXPECTED[name]}, saw {started[name]}')
if ended != started:
    failures.append('start and end events do not pair up: a call never ended or ended twice')

# Barriers. Each event is unique, so its position in the log is exact.
def position(name, kind):
    hits = [i for i, (k, n) in enumerate(events) if k == kind and n == name]
    return hits[0] if len(hits) == 1 else None

def ends_before_starts(first, second):
    end, start = position(first, 'end'), position(second, 'start')
    if end is None or start is None:
        failures.append(f'cannot check {first} before {second}: an event is missing or repeated')
    elif end > start:
        failures.append(f'barrier broken: {first} must end before {second} starts')

for a, b in zip(STATIC, STATIC[1:]):
    ends_before_starts(a, b)
for name in STATIC:
    ends_before_starts(name, LANDING_BUILD)
for name in LANDING:
    ends_before_starts(LANDING_BUILD, name)
    ends_before_starts(name, PRODUCT_BUILD)
for name in PRODUCT:
    ends_before_starts(PRODUCT_BUILD, name)

# Concurrency, read from the order of the log.
running = peak = leaf_running = leaf_peak = 0
for kind, name in events:
    delta = 1 if kind == 'start' else -1
    running += delta
    peak = max(peak, running)
    if name.startswith('test:'):
        leaf_running += delta
        leaf_peak = max(leaf_peak, leaf_running)
if running != 0:
    failures.append('some call was still running at the end of the run')
if peak > 3:
    failures.append(f'more than 3 calls ran at once (peak {peak})')
if mode == 'parallel' and leaf_peak < 2:
    failures.append(f'parallelism never reached 2 browser leaves (peak {leaf_peak}): the leaves ran one at a time')
if mode == 'serial' and peak != 1:
    failures.append(f'serial run overlapped calls (peak {peak})')

if failures:
    for message in failures:
        print('FAIL ' + message)
    print('event log:')
    for kind, name in events:
        print(f'  {kind} {name}')
    sys.exit(1)
print(f'ok: {mode} run, {sum(EXPECTED.values())} calls once each, barriers hold, '
      f'peak {peak} calls, peak {leaf_peak} browser leaves')
PY
}

# expect_failed_run NAME FAIL_CALL MAKE_ARGS...: make must exit non-zero, and the failing call must have started.
expect_failed_run() {
	local name="$1" fail="$2"
	shift 2
	run_make "$name" 0 "$fail" "$@"
	if ((rc == 0)); then
		note_fail "$name: make exited 0 although $fail failed"
		show_output "$name"
	fi
	grep -Fxq "start $fail" "$scratch/$name.events" ||
		note_fail "$name: $fail never started, so the injected failure did nothing"
}

# no_product_after NAME: a failed landing check must stop the product build and every product leaf.
no_product_after() {
	local ev="$scratch/$1.events" leaf
	if grep -Fxq 'start build:mode=' "$ev"; then
		note_fail "$1: product build started after a landing check failed"
	fi
	for leaf in "${PRODUCT_LEAVES[@]}"; do
		if grep -Fxq "start $leaf" "$ev"; then
			note_fail "$1: $leaf started after a landing check failed"
		fi
	done
}

echo 'frontend verify graph: make -n verify still lists every frontend leaf'
dry="$scratch/dry.out"
dry_rc=0
make -C "$repo" --no-print-directory -n verify >"$dry" 2>&1 || dry_rc=$?
if ((dry_rc != 0)); then
	note_fail "make -n verify exited $dry_rc"
	tail -n 20 "$dry"
fi
for cmd in 'npm run lint' 'npm run check' 'npm test' \
	'PUBLIC_SITE_MODE=landing npm run build' 'PUBLIC_SITE_MODE= npm run build'; do
	grep -qF "$cmd" "$dry" || note_fail "make -n verify does not list: $cmd"
done
for leaf in "${LANDING_LEAVES[@]}" "${PRODUCT_LEAVES[@]}"; do
	grep -qF "npm run $leaf" "$dry" || note_fail "make -n verify does not list: npm run $leaf"
done

echo 'frontend verify graph: make -j3 verify-frontend (leaves may overlap)'
run_make parallel 1 "" -j3 --output-sync=target verify-frontend
if ((rc != 0)); then
	note_fail "make -j3 verify-frontend exited $rc"
	show_output parallel
fi
check_events parallel parallel

echo 'frontend verify graph: make verify-frontend (serial)'
run_make serial 0 "" verify-frontend
if ((rc != 0)); then
	note_fail "make verify-frontend exited $rc"
	show_output serial
fi
check_events serial serial

echo 'frontend verify graph: failure injection'
expect_failed_run docs-j3 test:docs -j3 verify-frontend
no_product_after docs-j3
expect_failed_run docs-j3-k test:docs -j3 -k verify-frontend
no_product_after docs-j3-k
expect_failed_run footer-j3-k test:footer -j3 -k verify-frontend
no_product_after footer-j3-k
expect_failed_run canvas-j3 test:canvas -j3 verify-frontend

if ((failures > 0)); then
	printf 'frontend verify graph: %d check(s) failed\n' "$failures"
	exit 1
fi
echo 'frontend verify graph ok'
