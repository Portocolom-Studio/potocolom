#!/usr/bin/env bash
# Print the id that ties this checkout to its own dev ports and database, or
# nothing for the main worktree. A linked worktree's --git-dir resolves inside
# .git/worktrees/ while --git-common-dir stays on the shared .git, so the two
# differ exactly in a linked worktree; git may return either form, hence the
# absolute-path comparison. The main worktree is special-cased because the
# documented ports in docs/local-development.md must keep working for the
# ordinary single-checkout case; if git is unavailable or any step fails, print
# nothing and let callers fall back to the canonical values, which is the safe
# default.
set -euo pipefail

git_dir="$(git rev-parse --git-dir 2>/dev/null || true)"
common_dir="$(git rev-parse --git-common-dir 2>/dev/null || true)"
if [[ -z "$git_dir" || -z "$common_dir" ]]; then
  exit 0
fi
if [[ "$(cd "$git_dir" && pwd)" == "$(cd "$common_dir" && pwd)" ]]; then
  exit 0
fi

root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$root" ]] || exit 0

# shasum is the macOS spelling; without the fallback every make invocation in
# this checkout would die on the guard in the Makefile.
if command -v sha256sum >/dev/null 2>&1; then
  printf '%s' "$root" | sha256sum | cut -c1-8
else
  printf '%s' "$root" | shasum -a 256 | cut -c1-8
fi
