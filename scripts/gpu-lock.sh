#!/usr/bin/env bash
# Cooperative GPU lock for illusion experiments on the reference RX 7600 XT.
# Usage: scripts/gpu-lock.sh [--force] -- <command> [args...]
# Acquire the flock FIRST, then preflight, so two campaigns cannot both pass
# a preflight race. Wait briefly for a temporary workload to finish, then
# return exit 75 when the GPU remains busy so a campaign can retry safely.

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
WAIT_S="${POTOCOLOM_GPU_WAIT_S:-300}"
# Desktop residual on the reference RX 7600 often sits ~16-23% with empty KFD.
# Override with POTOCOLOM_GPU_IDLE_PCT (predeparture uses 25).
IDLE_PCT="${POTOCOLOM_GPU_IDLE_PCT:-15}"
# How many cells may share the card. 1 keeps the original exclusive behaviour
# byte for byte. Above 1 the utilisation and KFD checks are dropped, because
# with intentional overlap a busy GPU and sibling KFD holders are the expected
# state rather than a conflict. Raise this only from a measured throughput win:
# the recipe's small batches leave the card underused, but that is a
# measurement, not an assumption.
SLOTS="${POTOCOLOM_GPU_SLOTS:-1}"

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

	# Sharing the card on purpose: a busy GPU and sibling KFD holders are the
	# intended state, so only the checks that still mean "someone else owns
	# this machine" apply.
	if [[ "$SLOTS" -le 1 ]]; then
		if command -v rocm-smi >/dev/null 2>&1; then
			local use
			use="$(parse_rocm_gpu_use_pct "$(rocm-smi --showuse 2>/dev/null || true)")"
			if [[ "${use:-0}" -gt "${IDLE_PCT}" ]]; then
				busy=1
				reason="rocm-smi GPU use ${use}% (idle threshold ${IDLE_PCT}%)"
			fi
		fi

		if ls /dev/kfd >/dev/null 2>&1; then
			local kfd
			kfd="$(lsof /dev/kfd 2>/dev/null | awk 'NR>1 && $1 !~ /rocm-smi|amdgpu/ {print $1}' | sort -u | tr '\n' ' ' || true)"
			if [[ -n "${kfd// /}" ]]; then
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
		return 75
	fi
	if [[ "$busy" -eq 1 && "$FORCE" -eq 1 ]]; then
		echo "gpu-lock: WARNING forcing acquire despite: $reason" >&2
	fi
	return 0
}

# Hold a slot for the whole critical section (preflight + command). Slot 1 keeps
# the original lock path, so a single-slot run is unchanged and still mutually
# exclusive with any other single-slot caller.
slot_path() {
	if [[ "$1" -eq 1 ]]; then echo "$LOCK_FILE"; else echo "${LOCK_FILE}.slot$1"; fi
}

acquire_slot() {
	local i
	for ((i = 1; i <= SLOTS; i++)); do
		exec 9>"$(slot_path "$i")"
		if flock -n 9; then
			SLOT="$i"
			return 0
		fi
	done
	return 1
}

waited=0
until acquire_slot; do
	if ((waited >= WAIT_S)); then
		echo "gpu-lock: all $SLOTS slot(s) busy after ${waited}s" >&2
		exit 75
	fi
	sleep 5
	waited=$((waited + 5))
done
if [[ "$SLOTS" -gt 1 ]]; then
	echo "gpu-lock: holding slot $SLOT of $SLOTS" >&2
fi
waited=0
until preflight; do
	if (( FORCE == 1 || waited >= WAIT_S )); then
		exit 75
	fi
	sleep 5
	waited=$((waited + 5))
done
exec "$@"
