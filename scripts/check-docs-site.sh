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
# Every repository Markdown file a page names in its source line must be in both
# path filters of .github/workflows/docs.yml, so a change to it runs this job. No
# trailer skips this either.
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
# GitHub globs: * stays in one directory and ** crosses them. fnmatch lets * cross
# "/", so the matcher is a regex translation in Python.
python3 - "$head" "$pages" <<'PYEOF' || fail=1
import re
import subprocess
import sys

head, pages = sys.argv[1], sys.argv[2]


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def glob_regex(pattern):
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile("^" + out + "$")


# The push list sits under the 4-space "paths:" key; the changes job's list
# follows its "paths: |" line.
push, changes, mode = [], [], None
for line in git("show", f"{head}:.github/workflows/docs.yml").splitlines():
    push_item = re.match(r'^      - "(.+)"$', line)
    changes_item = re.match(r"^        (\S.*)$", line)
    if line == "    paths:":
        mode = push
    elif line == "      paths: |":
        mode = changes
    elif mode is push and push_item:
        push.append(push_item.group(1))
    elif mode is changes and changes_item:
        changes.append(changes_item.group(1))
    else:
        mode = None
if not push or not changes:
    sys.exit("docs site check: no path filters found in .github/workflows/docs.yml")
# GitHub applies "!" patterns in order; this matcher treats every entry as an
# include, so refuse negations rather than pass a path GitHub would skip.
if any(p.startswith("!") for p in push + changes):
    sys.exit("docs site check: .github/workflows/docs.yml has a '!' path pattern; teach this check its order")

tracked = set(git("ls-tree", "-r", "--name-only", head).splitlines())
sources = {}
for page in git("ls-tree", "-r", "--name-only", head, "--", pages).splitlines():
    if not re.fullmatch(re.escape(pages) + r"/[^/]+/[^/]+\.md", page):
        continue
    for line in git("show", f"{head}:{page}").splitlines():
        if line.startswith("source:"):
            # Only .md sources: the mirror check below acts only on changed .md files.
            for token in re.findall(r"[A-Za-z0-9_./-]+\.md", line):
                if token in tracked:
                    sources.setdefault(token, page)

failed = False
for path in sorted(sources):
    for name, patterns in (("push", push), ("changes", changes)):
        if not any(glob_regex(p).match(path) for p in patterns):
            print(f"docs site check: {path} (source of {sources[path]}) is not in the {name} "
                  "paths of .github/workflows/docs.yml, so a change to it would skip this job.",
                  file=sys.stderr)
            failed = True
sys.exit(1 if failed else 0)
PYEOF
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
