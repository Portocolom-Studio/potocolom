#!/usr/bin/env bash
# Every agent session loads AGENTS.md, so its size is a cost in each one.
# Raising the limit is a visible change in the same pull request (issue #926).
set -euo pipefail
limit=12288
size=$(wc -c < "${1:-AGENTS.md}")
if [ "$size" -gt "$limit" ]; then
	echo "AGENTS.md is $size bytes, over the limit of $limit. Cut text, link to a doc, or raise the limit in $0." >&2
	exit 1
fi
echo "AGENTS.md size: $size of $limit bytes"
