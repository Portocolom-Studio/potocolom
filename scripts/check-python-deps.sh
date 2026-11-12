#!/usr/bin/env bash
# Fail when an installed Python dependency has a known vulnerability (issue #872).
#
# Usage: scripts/check-python-deps.sh [venv ...]
#
# With no arguments, audits backend/.venv, worker/.venv and worker/.venv-inference.
# Each CI job builds only its own venv, so a venv that is not there is skipped.
# The check fails when no venv is found at all. pip-audit runs from .ci-audit,
# its own venv at the repository root, so the project venvs never get it.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
audit_version=2.10.1
audit_venv=$root/.ci-audit
pip_audit=$audit_venv/bin/pip-audit

# Reinstall when the pinned version changes, so a kept .ci-audit never audits
# with an older pip-audit than the one recorded here.
if ! "$pip_audit" --version 2>/dev/null | grep -qF " $audit_version"; then
	rm -rf "$audit_venv"
	python3 -m venv "$audit_venv"
	"$audit_venv/bin/pip" install --quiet "pip-audit==$audit_version"
fi

if [ $# -eq 0 ]; then
	set -- "$root/backend/.venv" "$root/worker/.venv" "$root/worker/.venv-inference"
fi

# Torch advisories accepted 2026-11-12 while the worker images stay on torch 2.9
# (Dockerfile.worker-cuda and Dockerfile.worker-rocm). They also match the local
# ROCm torch 2.9.1 build. Remove each entry when the torch pin moves past its fix.
ignored=(
	--ignore-vuln PYSEC-2025-193 # torch, fixed in 2.9.1
	--ignore-vuln PYSEC-2025-194 # torch, fixed in 2.13.0
	--ignore-vuln PYSEC-2025-195 # torch, fixed in 2.10.0
	--ignore-vuln PYSEC-2026-139 # torch, no fix published
	--ignore-vuln PYSEC-2026-1856 # torch, fixed in 2.10.0
	--ignore-vuln PYSEC-2026-2286 # torch, fixed in 2.10.0
)

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

audited=0
status=0
for venv in "$@"; do
	name=$(basename "$(dirname "$venv")")/$(basename "$venv")
	if [ ! -x "$venv/bin/python" ]; then
		echo "skip: $name is not built in this job"
		continue
	fi
	audited=$((audited + 1))

	"$venv/bin/python" -m pip freeze --exclude-editable > "$tmp/installed.txt"
	# pip-audit cannot resolve a direct URL requirement to a version, so it
	# cannot audit this one. Drop it here and say so, instead of a silent skip.
	if grep -q '^diffusers @ ' "$tmp/installed.txt"; then
		echo "notice: $name installs diffusers from a URL, so diffusers was not audited"
	fi
	# A local label such as torch==2.9.0+cpu is not on PyPI, so pip-audit would skip
	# the package. The label names another build of the same release, so audit that.
	sed -E -e '/^diffusers @ /d' \
		-e 's/^([^=[:space:]]+==[^+[:space:]]+)\+[^[:space:]]*$/\1/' \
		"$tmp/installed.txt" > "$tmp/audit.txt"

	echo "audit: $name ($(wc -l < "$tmp/audit.txt") packages)"
	if ! "$pip_audit" -r "$tmp/audit.txt" --no-deps --disable-pip --progress-spinner off "${ignored[@]}"; then
		echo "error: $name has known vulnerabilities (listed above)" >&2
		status=1
	fi
done

if [ "$audited" -eq 0 ]; then
	echo "error: no project venv to audit; build one first (make setup), or pass venv paths" >&2
	exit 1
fi
exit "$status"
