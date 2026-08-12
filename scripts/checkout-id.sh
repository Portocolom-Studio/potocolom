#!/usr/bin/env bash
# Print the id that ties this checkout to its own dev ports and database, or
# nothing for the main worktree. A linked worktree's --git-dir resolves to
# .git/worktrees/, which is how this detects one. The main worktree is
# special-cased because the documented ports in docs/local-development.md must
# keep working for the ordinary single-checkout case; if git is unavailable or
# any step fails, print nothing and let callers fall back to the canonical
# values, which is the safe default.
set -euo pipefail

git_dir="$(git rev-parse --git-dir 2>/dev/null || true)"
if [[ "$git_dir" != *"/worktrees/"* ]]; then
  exit 0
fi

root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$root" ]] || exit 0

printf '%s' "$root" | sha256sum | cut -c1-8
