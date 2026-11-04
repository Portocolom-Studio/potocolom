#!/usr/bin/env bash
# The desktop opener for the setup link keeps the token off every command line
# (issue #792). The link goes into a page only this user can read, xdg-open is
# given that page's path, and the page is removed afterwards.
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
failures=0

fail_case() {
	printf 'FAIL %s\n' "$1"
	failures=$((failures + 1))
}

token="setup-token-that-must-not-reach-argv-0123456789abcdefghij"
link="http://localhost:8080/setup#$token"
mkdir -p "$tmp/bin"
cat >"$tmp/bin/xdg-open" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$XDG_OPEN_LOG"
EOF
chmod +x "$tmp/bin/xdg-open"
export XDG_OPEN_LOG="$tmp/xdg-open.args"
export PATH="$tmp/bin:$PATH"

# 1. A desktop session. xdg-open receives a path, never the token.
(
	export DISPLAY=:0 POTOCOLOM_SETUP_PAGE_SECONDS=1
	# shellcheck source=/dev/null
	source "$repo/scripts/install.sh"
	open_setup_link "$link"
)
if [[ ! -s "$XDG_OPEN_LOG" ]]; then
	fail_case desktop-session-opens-a-page "xdg-open was not called"
else
	argv="$(cat "$XDG_OPEN_LOG")"
	page="$(head -n 1 "$XDG_OPEN_LOG")"
	if [[ "$argv" == *"$token"* ]]; then
		fail_case token-absent-from-argv "the token reached xdg-open's command line"
	else
		printf 'ok token-absent-from-argv\n'
	fi
	if [[ "$page" == */setup.html && -f "$page" ]]; then
		printf 'ok page-is-written-to-a-path\n'
	else
		fail_case page-is-written-to-a-path "argv was: $argv"
	fi
	if [[ -f "$page" ]]; then
		mode="$(stat -c %a "$page")"
		dir_mode="$(stat -c %a "$(dirname "$page")")"
		if [[ "$mode" == 600 && "$dir_mode" == 700 ]]; then
			printf 'ok page-and-directory-are-private\n'
		else
			fail_case page-and-directory-are-private "page $mode, directory $dir_mode"
		fi
		if grep -qF "url=$link" "$page"; then
			printf 'ok page-redirects-to-the-setup-link\n'
		else
			fail_case page-redirects-to-the-setup-link "link missing from the page"
		fi
		dir="$(dirname "$page")"
		for _ in $(seq 1 50); do
			[[ -e "$dir" ]] || break
			sleep 0.2
		done
		if [[ -e "$dir" ]]; then
			fail_case page-is-removed-afterwards "$dir still exists"
		else
			printf 'ok page-is-removed-afterwards\n'
		fi
	fi
fi

# 2. No desktop session: nothing is opened and nothing is written.
rm -f "$XDG_OPEN_LOG"
before="$(find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'tmp.*' 2>/dev/null | wc -l)"
(
	unset DISPLAY WAYLAND_DISPLAY
	# shellcheck source=/dev/null
	source "$repo/scripts/install.sh"
	open_setup_link "$link"
)
after="$(find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'tmp.*' 2>/dev/null | wc -l)"
if [[ -e "$XDG_OPEN_LOG" ]]; then
	fail_case no-desktop-session-opens-nothing "xdg-open ran without a display"
elif [[ "$before" != "$after" ]]; then
	fail_case no-desktop-session-writes-nothing "a page directory was left behind"
else
	printf 'ok no-desktop-session-opens-and-writes-nothing\n'
fi

if ((failures > 0)); then
	printf '%d failure(s)\n' "$failures" >&2
	exit 1
fi
printf 'install setup link ok\n'
