#!/usr/bin/env bash
# Cooperative GPU lock for illusion experiments on the reference RX 7600 XT.
# Usage: scripts/gpu-lock.sh [--force] -- <command> [args...]
# Abort (exit 2) if another GPU workload or the self-hosted CI runner is busy,
# unless --force is passed after the operator has confirmed the card is free.

set -euo pipefail

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
		use="$(rocm-smi --showuse 2>/dev/null | rg -o '[0-9]+ %' | head -1 | tr -d ' %' || echo 0)"
		if [[ "${use:-0}" -gt 15 ]]; then
			busy=1
			reason="rocm-smi GPU use ${use}%"
		fi
	fi

	# KFD / ROCm compute processes holding the card
	if ls /dev/kfd >/dev/null 2>&1; then
		local kfd
		kfd="$(lsof /dev/kfd 2>/dev/null | awk 'NR>1 && $1 !~ /rocm-smi|amdgpu/ {print $1}' | sort -u | tr '\n' ' ' || true)"
		if [[ -n "${kfd// /}" ]]; then
			# Ignore our own shell and common idle holders; flag python/torch workers
			if echo "$kfd" | rg -qi 'python|pt_main|hip'; then
				busy=1
				reason="${reason:+$reason; }KFD holders: $kfd"
			fi
		fi
	fi

	# Self-hosted CI runner actively processing a job
	if command -v systemctl >/dev/null 2>&1; then
		if systemctl is-active --quiet actions.runner.*.service 2>/dev/null || \
			systemctl is-active --quiet 'actions.runner.*' 2>/dev/null; then
			:
		fi
		# Prefer make target when available from repo root
		if [[ -f Makefile ]] && make -n ci-runner-status >/dev/null 2>&1; then
			local runner_out
			runner_out="$(make ci-runner-status 2>/dev/null || true)"
			if echo "$runner_out" | rg -qi 'Active: active \(running\)'; then
				# Runner service up is OK when Idle; check for Runner.Worker
				if pgrep -af 'Runner.Worker' >/dev/null 2>&1; then
					busy=1
					reason="${reason:+$reason; }self-hosted Runner.Worker active"
				fi
			fi
		elif pgrep -af 'Runner.Worker' >/dev/null 2>&1; then
			busy=1
			reason="${reason:+$reason; }self-hosted Runner.Worker active"
		fi
	elif pgrep -af 'Runner.Worker' >/dev/null 2>&1; then
		busy=1
		reason="${reason:+$reason; }self-hosted Runner.Worker active"
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

preflight

exec flock "$LOCK_FILE" "$@"
