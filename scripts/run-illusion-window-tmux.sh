#!/usr/bin/env bash
# Launch the unattended illusion window: K driver shards over ONE immutable
# plan, each holding one GPU slot.
#
# Usage: run-illusion-window-tmux.sh PLAN.json [SLOTS]
#
# SLOTS defaults to 1, which is exactly the single-driver behaviour of
# run-illusion-campaign-tmux.sh. Raise it only from the measured throughput in
# .local/illusion-reliability/campaigns/prewindow/: the card may or may not have
# headroom, and that is a measurement rather than an expectation.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
	echo "usage: $0 PLAN.json [SLOTS]" >&2
	exit 64
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
plan="$(realpath "$1")"
slots="${2:-1}"
session="illusion-window"
deadline="${POTOCOLOM_CAMPAIGN_DEADLINE_S:-208800}"
# gpu-lock's utilisation check reads rocm-smi "GPU use", which on this box
# includes GRAPHICS: Xorg, a browser and GNOME idle around 16-23% and have been
# measured at 41-55% with Firefox busy. Against the 15% default every cell is
# refused with exit 75 and the campaign silently stalls in a retry loop, which
# is exactly what happened once. The real compute guards are the KFD-holder and
# Runner.Worker checks, which stay active at any threshold, so this is set high
# enough that the desktop cannot trip it.
export POTOCOLOM_GPU_IDLE_PCT="${POTOCOLOM_GPU_IDLE_PCT:-90}"

if tmux has-session -t "$session" 2>/dev/null; then
	echo "tmux session already exists: $session" >&2
	exit 1
fi

# CI owning the card would fight every cell for the whole window, and the lock
# refuses rather than queues politely. Refuse to launch rather than warn: a
# window that starts against a live runner stalls in a retry loop all night.
if systemctl is-active --quiet 'actions.runner.*' 2>/dev/null ||
	pgrep -x 'Runner.Listener' >/dev/null 2>&1; then
	echo "refusing to launch: the self-hosted Actions runner is live. Any CI" >&2
	echo "job during the window makes gpu-lock refuse cells. Stop it first:" >&2
	echo "  sudo systemctl stop 'actions.runner.*'" >&2
	exit 1
fi

# Any OTHER compute process on the card is just as fatal, and less obvious. A
# dev worker left running from another worktree holds /dev/kfd while sitting at
# 0% GPU, and gpu-lock refuses on the holder regardless of the idle threshold.
# The campaign then loops "GPU busy" all night with a fresh heartbeat and no
# error, which has already cost this program two hours once. Name the process
# rather than making the operator find it.
holders="$(lsof /dev/kfd 2>/dev/null | awk 'NR>1 && $1 !~ /rocm-smi|amdgpu/ {print $2" "$1}' | sort -u)"
if [[ -n "$holders" ]]; then
	echo "refusing to launch: another process already holds the GPU." >&2
	echo "$holders" | while read -r pid name; do
		echo "  pid $pid  $name  $(ps -p "$pid" -o args= 2>/dev/null | cut -c1-90)" >&2
	done
	echo "Stop it, or gpu-lock will refuse every cell for the whole window." >&2
	exit 1
fi

for ((i = 0; i < slots; i++)); do
	log="${plan%.json}.shard${i}.log"
	heartbeat="${plan%.json}.shard${i}.heartbeat"
	# The heartbeat is owned by the driver it reports on and stops within a
	# minute of it: the old free-running `date` loop stayed fresh after every
	# shard had died, which is worse than no heartbeat at all.
	#
	# -u because the driver's stdout is redirected to a file, where Python
	# block-buffers it: without this the top-level log stays empty for hours
	# while the run is perfectly healthy, which reads exactly like a hang.
	cmd="cd '$root' && PYTHONPATH=worker POTOCOLOM_GPU_SLOTS='$slots' \
		worker/.venv/bin/python -u -m worker.illusion_campaign run \
		--plan '$plan' --shard '$i/$slots' --deadline-s '$deadline' \
		>> '$log' 2>&1 & \
		driver=\$!; \
		while kill -0 \$driver 2>/dev/null; do date -Is > '$heartbeat'; sleep 60; done; \
		wait \$driver"
	if ((i == 0)); then
		tmux new-session -d -s "$session" -n "shard$i" "$cmd"
	else
		tmux new-window -t "$session" -n "shard$i" "$cmd"
	fi
	echo "shard $i -> $log (heartbeat $heartbeat)"
done

echo "started tmux session: $session ($slots shard(s))"
echo "deadline: ${deadline}s"
echo "attach: tmux attach -t $session"
