#!/usr/bin/env bash
# Register a self-hosted Actions runner for this repo (docs/self-hosted-runner.md).
set -euo pipefail

REPO="${RUNNER_REPO:-Portocolom-Studio/potocolom}"
INSTALL_DIR="${RUNNER_INSTALL_DIR:-$HOME/.local/share/potocolom-actions-runner}"
LABELS="${RUNNER_LABELS:-self-hosted,Linux,X64,potocolom}"
RUNNER_USER="${SUDO_USER:-$USER}"
RUNNER_URL="${RUNNER_URL:-https://github.com/$REPO}"

if ! command -v curl >/dev/null; then
  echo "curl is required" >&2
  exit 1
fi

if ! command -v docker >/dev/null; then
  echo "warning: docker not found; backend CI needs it for the postgres service" >&2
fi

fetch_token_via_gh() {
  if ! command -v gh >/dev/null; then
    return 1
  fi
  gh api "repos/$REPO/actions/runners/registration-token" -X POST --jq .token 2>/dev/null
}

TOKEN="${RUNNER_TOKEN:-}"
if [ -z "$TOKEN" ]; then
  echo "Fetching registration token via gh for $REPO ..."
  TOKEN="$(fetch_token_via_gh || true)"
fi
if [ -z "$TOKEN" ]; then
  cat >&2 <<EOF
Could not get a registration token automatically.

Get one manually (expires in 1 hour):
  https://github.com/$REPO/settings/actions/runners/new

Use the token from step 2 (./config.sh --token ...), NOT a personal access token.
Org-level runner tokens require RUNNER_URL=https://github.com/ORG and org admin rights.

Then rerun:
  RUNNER_TOKEN='<token>' $0
EOF
  exit 1
fi

VERSION="$(
  curl -fsSL https://api.github.com/repos/actions/runner/releases/latest \
    | sed -n 's/.*"tag_name": "v\([^"]*\)".*/\1/p' \
    | head -1
)"
if [ -z "$VERSION" ]; then
  echo "could not resolve actions/runner release version" >&2
  exit 1
fi

ARCH="x64"
TARBALL="actions-runner-linux-${ARCH}-${VERSION}.tar.gz"
URL="https://github.com/actions/runner/releases/download/v${VERSION}/${TARBALL}"

mkdir -p "$(dirname "$INSTALL_DIR")"
if [ -f "$INSTALL_DIR/.runner" ]; then
  echo "runner already configured at $INSTALL_DIR" >&2
  echo "remove it first: cd $INSTALL_DIR && ./config.sh remove" >&2
  exit 1
fi

# A failed config.sh leaves a half-installed tree; start clean.
if [ -d "$INSTALL_DIR" ] && [ ! -f "$INSTALL_DIR/.runner" ]; then
  echo "removing incomplete install at $INSTALL_DIR"
  rm -rf "$INSTALL_DIR"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "$URL" -o "$TMP/$TARBALL"
mkdir -p "$INSTALL_DIR"
tar -xzf "$TMP/$TARBALL" -C "$INSTALL_DIR"

cd "$INSTALL_DIR"
if ! ./config.sh \
  --url "$RUNNER_URL" \
  --token "$TOKEN" \
  --name "$(hostname -s)-potocolom" \
  --labels "$LABELS" \
  --unattended \
  --replace; then
  cat >&2 <<EOF

Registration failed.

Common causes of 404 Not Found:
  - Registration token expired (valid ~1 hour) or already used once
  - Personal access token pasted instead of the runner registration token
  - RUNNER_URL does not match where the token was issued
    repo token: RUNNER_URL=https://github.com/$REPO (default)
    org token:  RUNNER_URL=https://github.com/Portocolom-Studio

Retry with a fresh token:
  RUNNER_TOKEN="\$(gh api repos/$REPO/actions/runners/registration-token -X POST --jq .token)" $0
EOF
  rm -rf "$INSTALL_DIR"
  exit 1
fi

chown -R "$RUNNER_USER:$RUNNER_USER" "$INSTALL_DIR" 2>/dev/null || true

echo ""
echo "Runner installed at $INSTALL_DIR"
echo "  test:  cd $INSTALL_DIR && ./run.sh"
echo "  service: cd $INSTALL_DIR && sudo ./svc.sh install && sudo ./svc.sh start"
