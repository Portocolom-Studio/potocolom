#!/usr/bin/env bash
# Keep the public docs at /docs in step with the repository docs.
#
# Usage: scripts/check-docs-site.sh <base> <head>
#
# Every page under frontend/src/lib/docs/pages names the repository docs it
# mirrors in its `source:` front matter line. When the range base...head changes
# one of those docs, at least one page that names it must change in the same
# range. A change with no reader-facing effect (a typo, a link, an internal note)
# passes when a commit in the range carries a `Docs-site: <reason>` trailer.
#
# Every page at head also needs an entry in PAGE_ICONS (frontend/src/lib/docs/
# icons.ts), or the prerender fails late in `make verify`. No trailer skips this.
set -euo pipefail

[ $# -eq 2 ] || { echo "usage: $0 <base> <head>" >&2; exit 2; }
base=$1
head=$2
pages=frontend/src/lib/docs/pages

icons=$(git show "$head:frontend/src/lib/docs/icons.ts")
fail=0
while IFS= read -r page; do
	[ -n "$page" ] || continue
	key=${page#"$pages/"}
	key=${key%.md}
	if ! grep -qF "'$key':" <<<"$icons"; then
		echo "docs site check: $page has no icon. Add '$key' to PAGE_ICONS in frontend/src/lib/docs/icons.ts." >&2
		fail=1
	fi
done < <(git ls-tree -r --name-only "$head" -- "$pages" | grep -E "^$pages/[^/]+/[^/]+\.md$" || true)
[ $fail -eq 0 ] || exit 1

if git log --format=%B "$base..$head" | grep -qE '^Docs-site: .+'; then
	echo "docs site check: skipped by a Docs-site trailer"
	exit 0
fi

changed=$(git diff --name-only "$base...$head")
while IFS= read -r doc; do
	[ -n "$doc" ] || continue
	# Pages whose source line names this exact path (README.md must not match
	# docs/README.md, so the path may not continue a longer one on either side).
	mirrors=$(git grep -lP "^source:.*(?<![\\w/.-])\\Q$doc\\E(?![\\w/.-])" "$head" -- "$pages/*/*.md" |
		sed "s|^$head:||" || true)
	[ -n "$mirrors" ] || continue
	if ! grep -qxF -f <(printf '%s\n' "$mirrors") <(printf '%s\n' "$changed"); then
		echo "docs site check: $doc changed, but no page that mirrors it did. Update one of:" >&2
		printf '  %s\n' $mirrors >&2
		fail=1
	fi
done < <(printf '%s\n' "$changed" | grep -E '\.md$' | grep -v "^$pages/" || true)

[ $fail -eq 0 ] || {
	echo "If the change has no reader-facing effect, add a 'Docs-site: <reason>' trailer to a commit." >&2
	exit 1
}
echo "docs site check: ok"
