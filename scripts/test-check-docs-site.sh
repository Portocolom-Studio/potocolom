#!/usr/bin/env bash
# Self-test for scripts/check-docs-site.sh in a throwaway repository.
set -euo pipefail

check="$(cd "$(dirname "$0")" && pwd)/check-docs-site.sh"
repo=$(mktemp -d)
trap 'rm -rf "$repo"' EXIT
cd "$repo"
git init -q
git config user.email test@example.invalid
git config user.name test
mkdir -p docs frontend/src/lib/docs/pages/guide
printf 'a\n' >docs/self-hosting.md
printf 'a\n' >README.md
printf 'a\n' >docs/README.md
printf 'a\n' >docs/other.md
printf -- '---\nsource: docs/self-hosting.md "First run"\n---\na\n' >frontend/src/lib/docs/pages/guide/start.md
printf -- '---\nsource: README.md\n---\na\n' >frontend/src/lib/docs/pages/guide/intro.md
printf -- '---\nsource: docs/README.md\n---\na\n' >frontend/src/lib/docs/pages/guide/notes.md
printf "const PAGE_ICONS = {\n\t'guide/start': 'book',\n\t'guide/intro': 'book',\n\t'guide/notes': 'book'\n};\n" >frontend/src/lib/docs/icons.ts
git add -A && git commit -qm base
base=$(git rev-parse HEAD)

expect() {
	local want=$1 name=$2
	if bash "$check" "$base" HEAD >/dev/null 2>&1; then got=pass; else got=fail; fi
	[ "$got" = "$want" ] || { echo "FAIL: $name (expected $want, got $got)" >&2; exit 1; }
	echo "ok: $name"
	git reset -q --hard "$base"
}

printf 'b\n' >>docs/self-hosting.md && git commit -qam doc
expect fail "a mirrored doc changes alone"

printf 'b\n' >>docs/self-hosting.md
printf 'b\n' >>frontend/src/lib/docs/pages/guide/start.md && git commit -qam both
expect pass "a mirrored doc changes with its page"

printf 'b\n' >>docs/self-hosting.md && git commit -qam doc -m 'Docs-site: typo only'
expect pass "a Docs-site trailer skips the check"

printf 'b\n' >>docs/other.md && git commit -qam other
expect pass "a doc no page mirrors is free to change"

printf 'b\n' >>README.md && git commit -qam readme
expect fail "README.md is matched as its own path"

printf 'b\n' >>README.md
printf 'b\n' >>frontend/src/lib/docs/pages/guide/notes.md && git commit -qam wrong-page
expect fail "a page that mirrors docs/README.md does not cover README.md"

printf 'a\n' >frontend/src/lib/docs/pages/guide/new.md && git add -A && git commit -qm new-page
expect fail "a new page with no icon"

printf 'a\n' >frontend/src/lib/docs/pages/guide/new.md && git add -A && git commit -qm new-page -m 'Docs-site: no icon'
expect fail "a Docs-site trailer does not skip the icon check"

printf 'a\n' >frontend/src/lib/docs/pages/guide/new.md
sed -i "s|'guide/notes': 'book'|'guide/notes': 'book',\\n\\t'guide/new': 'book'|" frontend/src/lib/docs/icons.ts
git add -A && git commit -qm new-page-icon
expect pass "a new page with its icon"

echo "check-docs-site: all cases pass"
