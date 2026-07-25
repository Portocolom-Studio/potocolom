#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 PLAN.json" >&2
  exit 64
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
session="illusion-campaign"
plan="$(realpath "$1")"
log="${plan%.json}.run.log"
heartbeat="${plan%.json}.heartbeat"
deadline="${POTOCOLOM_CAMPAIGN_DEADLINE_S:-187200}"

if tmux has-session -t "$session" 2>/dev/null; then
  echo "tmux session already exists: $session" >&2
  exit 1
fi

tmux new-session -d -s "$session" "cd '$root' && (while true; do date -Is > '$heartbeat'; sleep 60; done) & heartbeat_pid=\$!; trap 'kill \$heartbeat_pid' EXIT; PYTHONPATH=worker worker/.venv/bin/python -m worker.illusion_campaign run --plan '$plan' --deadline-s '$deadline' >> '$log' 2>&1"
echo "started tmux session: $session"
echo "log: $log"
echo "heartbeat: $heartbeat"
echo "deadline: ${deadline}s"
