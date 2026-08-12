#!/usr/bin/env bash
# Print this checkout's dev-loop ports and database suffix, one per line. The
# main worktree keeps the canonical values; a linked worktree derives an offset
# from its path so two checkouts can run the dev loop at once. Host ports 1025,
# 5432, 6379, 8025, 9000, 9001, 9100, 9101, 1125 and 8125 are in use on this
# machine or by the dev containers, and the bands 8100-8499 and 5700-6099
# avoid all of them.
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
id="$("$script_dir/checkout-id.sh")"

if [[ -z "$id" ]]; then
  printf 'API_PORT=8000\nWEB_PORT=5173\nDB_SUFFIX=\n'
  exit 0
fi

h=$((16#${id:0:4}))
printf 'API_PORT=%d\nWEB_PORT=%d\nDB_SUFFIX=_%s\n' \
  "$((8100 + h % 400))" "$((5700 + h % 400))" "$id"
