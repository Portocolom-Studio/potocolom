#!/usr/bin/env bash
# Cooperative GPU lock for illusion experiments on the reference RX 7600 XT.
# Usage: scripts/gpu-lock.sh [--force] -- <command> [args...]
# Acquire the flock FIRST, then preflight, so two campaigns cannot both pass
# a preflight race. Abort (exit 2) if another GPU workload or the self-hosted
# CI runner is busy, unless --force is passed after the operator confirms.

set -euo pipefail

# Parse rocm-smi --showuse lines like "GPU use (%): 7" (not "7 %").
# Safe to source for fixture tests.
parse_rocm_gpu_use_pct() {
	local raw="${1:-}"
	local max_use=0
	local n
	while IFS= read -r line; do
		if [[ "$line" =~ GPU\ use\ \(%\):[[:space:]]*([0-9]+) ]]; then
			n="${BASH_REMATCH[1]}"
			if (( n > max_use )); then
				max_use=$n
			fi
		fi
	done <<<"$raw"
	echo "$max_use"
}

# When sourced (unit tests), stop after helpers.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
	return 0
fi

LOCK_FILE="${POTOCOLOM_GPU_LOCK:-/tmp/potocolom-gpu.lock}"
FORCE=0

while [[ $# -gt 0 ]]; do
	case "$1" in
	--force)
		FORCE=1
		shift
		;;
	--)
		shift
		break
		;;
	*)
		break
		;;
	esac
done

if [[ $# -lt 1 ]]; then
	echo "usage: $0 [--force] -- <command> [args...]" >&2
	exit 64
fi

preflight() {
	local busy=0
	local reason=""

	if command -v rocm-smi >/dev/null 2>&1; then
		local use
		use="$(parse_rocm_gpu_use_pct "$(rocm-smi --showuse 2>/dev/null || true)")"
		if [[ "${use:-0}" -gt 15 ]]; then
			busy=1
			reason="rocm-smi GPU use ${use}%"
		fi
	fi

	if ls /dev/kfd >/dev/null 2>&1; then
		local kfd
		kfd="$(lsof /dev/kfd 2>/dev/null | awk 'NR>1 && $1 !~ /rocm-smi|amdgpu/ {print $1}' | sort -u | tr '\n' ' ' || true)"
		if [[ -n "${kfd// /}" ]]; then
			if echo "$kfd" | rg -qi 'python|pt_main|hip'; then
				busy=1
				reason="${reason:+$reason; }KFD holders: $kfd"
			fi
		fi
	fi

	# Exact process name only: pgrep -af falsely matches shells whose argv
	# text merely mentions Runner.Worker (Cursor sandboxes, prior agents).
	if pgrep -x 'Runner.Worker' >/dev/null 2>&1; then
		busy=1
		reason="${reason:+$reason; }self-hosted Runner.Worker active"
	fi

	if [[ -n "${POTOCOLOM_CAMPAIGN_PIDFILE:-}" && -f "${POTOCOLOM_CAMPAIGN_PIDFILE}" ]]; then
		local other
		other="$(cat "${POTOCOLOM_CAMPAIGN_PIDFILE}" 2>/dev/null || true)"
		if [[ -n "$other" && "$other" != "$$" ]] && kill -0 "$other" 2>/dev/null; then
			busy=1
			reason="${reason:+$reason; }campaign pid $other active"
		fi
	fi

	if [[ "$busy" -eq 1 && "$FORCE" -eq 0 ]]; then
		echo "gpu-lock: abort - GPU not free ($reason)" >&2
		echo "gpu-lock: re-run with --force only after confirming no overlap" >&2
		return 2
	fi
	if [[ "$busy" -eq 1 && "$FORCE" -eq 1 ]]; then
		echo "gpu-lock: WARNING forcing acquire despite: $reason" >&2
	fi
	return 0
}

# Hold the lock for the whole critical section (preflight + command).
exec 9>"$LOCK_FILE"
if ! flock 9; then
	echo "gpu-lock: failed to acquire $LOCK_FILE" >&2
	exit 2
fi
preflight
exec "$@"
