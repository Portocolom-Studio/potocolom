#!/usr/bin/env bash
# Resolve the worker Python interpreter without hard-coded user paths.
# Prefer POTOCOLOM_WORKER_PYTHON, then the worktree worker/.venv.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -n "${POTOCOLOM_WORKER_PYTHON:-}" ]]; then
	if [[ ! -x "${POTOCOLOM_WORKER_PYTHON}" ]]; then
		echo "POTOCOLOM_WORKER_PYTHON is set but not executable: ${POTOCOLOM_WORKER_PYTHON}" >&2
		exit 1
	fi
	echo "${POTOCOLOM_WORKER_PYTHON}"
	exit 0
fi
CANDIDATE="${ROOT}/worker/.venv/bin/python"
if [[ -x "$CANDIDATE" ]]; then
	echo "$CANDIDATE"
	exit 0
fi
echo "No worker Python found. Create worker/.venv or set POTOCOLOM_WORKER_PYTHON." >&2
echo "Example: python3 -m venv worker/.venv && worker/.venv/bin/pip install -e 'worker[inference]'" >&2
exit 1
