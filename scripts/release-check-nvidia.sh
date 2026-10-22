#!/usr/bin/env bash
set -euo pipefail

usage() {
	printf 'usage: scripts/release-check-nvidia.sh HOST=user@host VERSION=vX.Y.Z [INSTALL_DIR=/absolute/path] [RELEASE_DIR=/local/artifacts]\n' >&2
}

host="${HOST:-${1:-}}"
version="${VERSION:-${2:-}}"
install_dir="${INSTALL_DIR:-${3:-}}"
release_dir="${RELEASE_DIR:-}"
if [[ ! "$host" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]*@[a-zA-Z0-9][a-zA-Z0-9.-]*$ ||
	! "$version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
	usage
	exit 2
fi
if [[ -n "$install_dir" && "$install_dir" != /* ]] ||
	[[ "$install_dir" == *$'\n'* || "$install_dir" == *$'\r'* ]]; then
	echo 'INSTALL_DIR must be an absolute path without line breaks' >&2
	exit 2
fi
if [[ -n "$release_dir" && ! -d "$release_dir" ]]; then
	echo 'RELEASE_DIR must name a local artifact directory' >&2
	exit 2
fi

root="$(dirname "$(dirname "${BASH_SOURCE[0]}")")"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/release"
if [[ -n "$release_dir" ]]; then
	cp "$release_dir/install.sh" "$release_dir/potocolom-$version-selfhost.tar.gz" \
		"$release_dir/SHA256SUMS" "$tmp/release/"
else
	gh release download "$version" --dir "$tmp/release" \
		--pattern install.sh --pattern "potocolom-$version-selfhost.tar.gz" --pattern SHA256SUMS
fi
python3 - "$root/scripts/release-check.py" "$tmp/release" "$version" <<'PY'
import importlib.util
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("release_check", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.verify_release(pathlib.Path(sys.argv[2]), sys.argv[3])
PY
cp "$root/scripts/release-check.py" "$tmp/release/release-check.py"
remote_dir="/tmp/potocolom-release-check-$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
quoted_remote_dir="$(printf '%q' "$remote_dir")"
if [[ -n "$install_dir" ]]; then
	remote_install_dir="$(printf '%q' "$install_dir")"
	install_argument="--install-dir $remote_install_dir"
else
	install_argument='--install-dir "$HOME/potocolom"'
fi
remote_cmd="mkdir -m 700 -- $quoted_remote_dir && { tar -C $quoted_remote_dir -xf -; rc=\$?; if [ \$rc -eq 0 ]; then python3 $quoted_remote_dir/release-check.py --version $(printf '%q' "$version") --release-dir $quoted_remote_dir $install_argument --profile gpu --keep; rc=\$?; fi; rm -rf -- $quoted_remote_dir; exit \$rc; }"
remote_cmd="bash -c $(python3 -c 'import shlex, sys; print(shlex.quote(sys.argv[1]))' "$remote_cmd")"
if tar -C "$tmp/release" -cf - install.sh "potocolom-$version-selfhost.tar.gz" SHA256SUMS release-check.py \
	| env -u GH_TOKEN -u GITHUB_TOKEN -u GH_ENTERPRISE_TOKEN \
		ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 \
		-o ServerAliveCountMax=2 -- "$host" "$remote_cmd"; then
	printf 'PASS NVIDIA release check %s on %s\n' "$version" "$host"
else
	printf 'FAIL NVIDIA release check %s on %s\n' "$version" "$host" >&2
	exit 1
fi
