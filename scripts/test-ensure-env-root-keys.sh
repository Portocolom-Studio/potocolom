#!/usr/bin/env bash
# ensure-env.sh writes ROOT_KEYS when it is empty, once, and never replaces a
# value that is already there (issue #792). Docker is stubbed as unusable so
# the dotenv fallback in install-lock.sh reads the file, as a direct guard run does.
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
failures=0

fail_case() {
	printf 'FAIL %s\n' "$1"
	failures=$((failures + 1))
}

mkdir -p "$tmp/bin" "$tmp/root/scripts" "$tmp/root/deploy/compose"
cp "$repo/scripts/ensure-env.sh" "$repo/scripts/install-lock.sh" "$tmp/root/scripts/"
cp "$repo/deploy/compose/compose.yml" "$repo/deploy/compose/.env.example" "$tmp/root/deploy/compose/"
cat >"$tmp/bin/docker" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$tmp/bin/docker"

ENV_FILE="$tmp/root/deploy/compose/.env"
run_ensure() {
	PATH="$tmp/bin:$PATH" ENV_FILE="$ENV_FILE" ENV_EXAMPLE="$tmp/root/deploy/compose/.env.example" \
		bash "$tmp/root/scripts/ensure-env.sh" >"$tmp/out" 2>&1
}
root_line() { grep -E '^[[:space:]]*(export[[:space:]]+)?ROOT_KEYS[[:space:]]*[:=]' "$ENV_FILE"; }
root_count() { grep -cE '^[[:space:]]*(export[[:space:]]+)?ROOT_KEYS[[:space:]]*[:=]' "$ENV_FILE"; }

# 1. A fresh install writes one ring, in the version:base64 shape auth-enable uses.
if run_ensure; then
	if [[ "$(root_line)" =~ ^ROOT_KEYS=1:[A-Za-z0-9+/]{43}=$ ]]; then
		printf 'ok fresh-install-writes-root-keys\n'
	else
		fail_case fresh-install-writes-root-keys "unexpected line: $(root_line)"
	fi
else
	fail_case fresh-install-writes-root-keys "ensure-env exited non-zero: $(cat "$tmp/out")"
fi

# 2. A second run keeps the value and does not add a line.
first="$(root_line)"
if run_ensure && [[ "$(root_line)" == "$first" && "$(root_count)" == 1 ]]; then
	printf 'ok second-run-keeps-root-keys\n'
else
	fail_case second-run-keeps-root-keys "value or line count changed"
fi

# 3. An empty ROOT_KEYS in an existing file is filled, and the rest of the file survives.
sed -i 's/^ROOT_KEYS=.*/ROOT_KEYS=/' "$ENV_FILE"
printf 'CUSTOM_MARKER=keep-me\n' >>"$ENV_FILE"
if run_ensure && [[ "$(root_line)" =~ ^ROOT_KEYS=1:[A-Za-z0-9+/]{43}=$ \
	&& "$(root_count)" == 1 ]] && grep -qx 'CUSTOM_MARKER=keep-me' "$ENV_FILE"; then
	printf 'ok empty-root-keys-is-filled-in-place\n'
else
	fail_case empty-root-keys-is-filled-in-place "$(root_line)"
fi

# 4. A non-empty value, including an export form with spaces, is never replaced.
existing='export ROOT_KEYS = "7:operator-kept-this-ring" # operator comment'
sed -i '/ROOT_KEYS/d' "$ENV_FILE"
printf '%s\n' "$existing" >>"$ENV_FILE"
if run_ensure && [[ "$(root_line)" == "$existing" && "$(root_count)" == 1 ]] \
	&& ! grep -q 'wrote ROOT_KEYS' "$tmp/out"; then
	printf 'ok existing-root-keys-is-never-replaced\n'
else
	fail_case existing-root-keys-is-never-replaced "$(root_line)"
fi

# 5. The value is never printed; only the fact that it was written is.
rm -f "$ENV_FILE"
run_ensure
written="$(sed -n 's/^ROOT_KEYS=//p' "$ENV_FILE")"
if [[ -n "$written" ]] && ! grep -qF "$written" "$tmp/out"; then
	printf 'ok root-keys-value-is-not-printed\n'
else
	fail_case root-keys-value-is-not-printed "the installer output contains the ring"
fi

# A template default is retired only with its original comment and no overrides.
legacy_block() {
    cat <<'EOF'
# Accounts. AUTH_MODE=none is the default and resolves every request to one
# implicit local administrator, which is what a single-operator install wants.
# `make auth-enable` switches this to accounts, writes ROOT_KEYS below, and
# prints a one-use link that claims the administrator account. Enabling
# accounts is one way: the install refuses to start in none mode afterwards.
AUTH_MODE=none
EOF
}
base="$tmp/base.env"
cp "$ENV_FILE" "$base"
sed -i "/^AUTH_MODE=/d" "$base"
cp "$base" "$ENV_FILE"
legacy_block >>"$ENV_FILE"
if run_ensure && ! grep -qx 'AUTH_MODE=none' "$ENV_FILE" && grep -q 'database.*mode' "$ENV_FILE"; then
    cp "$ENV_FILE" "$tmp/once"
    if run_ensure && cmp -s "$tmp/once" "$ENV_FILE"; then
        printf 'ok legacy-auth-mode-is-retired-idempotently\n'
    else
        fail_case legacy-auth-mode-idempotent
    fi
else
    fail_case legacy-auth-mode-is-retired
fi

cp "$base" "$ENV_FILE"
printf '%s\n' 'AUTH_MODE=accounts' >>"$ENV_FILE"
legacy_block >>"$ENV_FILE"
cp "$ENV_FILE" "$tmp/before"
if run_ensure && cmp -s "$tmp/before" "$ENV_FILE"; then
    printf 'ok duplicate-override-is-preserved\n'
else
    fail_case duplicate-override-is-preserved
fi

cp "$base" "$ENV_FILE"
printf '%s\n' 'AUTH_MODE=none' >>"$ENV_FILE"
cp "$ENV_FILE" "$tmp/before"
if run_ensure && cmp -s "$tmp/before" "$ENV_FILE"; then
    printf 'ok ambiguous-pin-is-preserved\n'
else
    fail_case ambiguous-pin-is-preserved
fi

cp "$base" "$ENV_FILE"
printf "%s\n" "CUSTOM='first" >>"$ENV_FILE"
legacy_block >>"$ENV_FILE"
printf "%s\n" "last'" >>"$ENV_FILE"
cp "$ENV_FILE" "$tmp/before"
if run_ensure && cmp -s "$tmp/before" "$ENV_FILE"; then
    printf 'ok multiline-content-is-preserved\n'
else
    fail_case multiline-content-is-preserved
fi

# The operator guidance must describe the recovery and socket transition.
if grep -q 'then recreate the API container to read them' "$repo/docs/api.md" && \
    [[ "$(grep -c 'then recreate the API container to read them' "$repo/docs/api.md")" == 2 ]]; then
    printf 'ok api-docs-describe-key-recovery\n'
else
    fail_case api-docs-describe-key-recovery
fi
if grep -q 'closes canvas sockets opened without a session; a socket withholding its first open is refused after the switch' "$repo/docs/security-checklist.md"; then
    printf 'ok checklist-describes-socket-transition\n'
else
    fail_case checklist-describes-socket-transition
fi

if ((failures > 0)); then
	printf '%d failure(s)\n' "$failures" >&2
	exit 1
fi
printf 'root keys ok\n'
