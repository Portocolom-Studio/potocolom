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
heartbeat="${plan%.json}.heartbeat"

if tmux has-session -t "$session" 2>/dev/null; then
	echo "tmux session already exists: $session" >&2
	exit 1
fi

# CI owning the card would fight every cell for the whole window, and the lock
# refuses rather than queues politely. Fail loudly now instead of at 3am.
if systemctl is-active --quiet 'actions.runner.*' 2>/dev/null ||
	pgrep -x 'Runner.Listener' >/dev/null 2>&1; then
	echo "WARNING: the self-hosted Actions runner is live. Any CI job during" >&2
	echo "the window makes gpu-lock refuse cells. Stop it first:" >&2
	echo "  sudo systemctl stop 'actions.runner.*'" >&2
	echo "Continuing in 10s; Ctrl-C to abort." >&2
	sleep 10
fi

tmux new-session -d -s "$session" -n heartbeat \
	"while true; do date -Is > '$heartbeat'; sleep 60; done"

for ((i = 0; i < slots; i++)); do
	log="${plan%.json}.shard${i}.log"
	# -u because the driver's stdout is redirected to a file, where Python
	# block-buffers it: without this the top-level log stays empty for hours
	# while the run is perfectly healthy, which reads exactly like a hang.
	tmux new-window -t "$session" -n "shard$i" \
		"cd '$root' && PYTHONPATH=worker POTOCOLOM_GPU_SLOTS='$slots' \
		worker/.venv/bin/python -u -m worker.illusion_campaign run \
		--plan '$plan' --shard '$i/$slots' --deadline-s '$deadline' \
		>> '$log' 2>&1"
	echo "shard $i -> $log"
done

echo "started tmux session: $session ($slots shard(s))"
echo "heartbeat: $heartbeat"
echo "deadline: ${deadline}s"
echo "attach: tmux attach -t $session"
