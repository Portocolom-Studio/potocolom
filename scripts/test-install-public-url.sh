#!/usr/bin/env bash
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
failures=0

fail_case() {
	printf 'FAIL %s\n' "$1"
	failures=$((failures + 1))
}

stage_bundle() {
	local stage="$1"
	mkdir -p "$stage/deploy/compose" "$stage/scripts"
	cp "$repo/deploy/compose/compose.yml" "$repo/deploy/compose/.env.example" "$stage/deploy/compose/"
	cp "$repo/scripts/preflight.sh" "$repo/scripts/ensure-env.sh" "$repo/scripts/gpu-profile.sh" "$stage/scripts/"
	(cd "$stage" && tar -czf "$tmp/bundle.tar.gz" deploy scripts)
	(cd "$tmp" && sha256sum bundle.tar.gz | sed 's#bundle.tar.gz#potocolom-v9.9.9-selfhost.tar.gz#' >SHA256SUMS)
}

make_case() {
	local name="$1"
	case_dir="$tmp/$name"
	mkdir -p "$case_dir/bin" "$case_dir/source"
	cat >"$case_dir/bin/curl" <<'EOF'
#!/usr/bin/env bash
while (($#)); do
	if [[ "$1" == -o ]]; then
		out="$2"
		shift 2
	else
		shift
	fi
done
printf 'called\n' >>"${CURL_LOG:?}"
case "$out" in
	*SHA256SUMS) cp "$BUNDLE_DIR/SHA256SUMS" "$out" ;;
	*) cp "$BUNDLE_DIR/bundle.tar.gz" "$out" ;;
esac
EOF
	cat >"$case_dir/bin/docker" <<'EOF'
#!/usr/bin/env bash
printf '%s | PUBLIC_URL=%s\n' "$*" "${PUBLIC_URL-unset}" >>"${DOCKER_LOG:?}"
case "$*" in
	'--version') printf 'Docker version 28.0.0, build test\n' ;;
	'compose version') printf '2.30.0\n' ;;
	'info') exit 0 ;;
	'volume inspect'*) exit 1 ;;
	*' pull'|*' up -d --no-build'|*' rm -sf'|*' compose version'|*) exit 0 ;;
esac
EOF
	cat >"$case_dir/bin/nvidia-smi" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == *'--query-gpu=name,memory.total,driver_version'* ]]; then
	printf 'NVIDIA Test GPU, 8192 MiB, 595.91.07\n'
elif [[ "$*" == *'--query-gpu=name'* ]]; then
	printf 'NVIDIA Test GPU\n'
else
	printf 'CUDA Version: 13.2\n'
fi
EOF
	for utility in bash dirname sed tail tr awk grep sha256sum tar mktemp mkdir cp; do
		[[ -e "$case_dir/bin/$utility" ]] || ln -s "$(type -P "$utility")" "$case_dir/bin/$utility"
	done
	cat >"$case_dir/bin/nvidia-ctk" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
	cat >"$case_dir/bin/hostname" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == -I ]]; then printf '192.168.1.10 10.0.0.2\n'; fi
EOF
	chmod +x "$case_dir/bin/curl" "$case_dir/bin/docker" "$case_dir/bin/nvidia-smi" "$case_dir/bin/nvidia-ctk" "$case_dir/bin/hostname"
	stage_bundle "$case_dir/source"
	: >"$case_dir/curl.log"
	: >"$case_dir/docker.log"
}

run_install() {
	local case_dir="$1"
	shift
	env PATH="$case_dir/bin:$PATH" CURL_LOG="$case_dir/curl.log" DOCKER_LOG="$case_dir/docker.log" BUNDLE_DIR="$tmp" \
		POTOCOLOM_SKIP_GPU_TEST=1 POTOCOLOM_VERSION=v9.9.9 POTOCOLOM_DIR="$case_dir/install" \
		POTOCOLOM_PROFILE=gpu "$@" bash "$repo/scripts/install.sh" 2>&1
}

check_case() {
	local name="$1" expected="$2" env_mode="$3" output rc env_file curl_calls
	make_case "$name"
	if [[ "$env_mode" == upgrade ]]; then
		mkdir -p "$case_dir/install/deploy/compose"
		sed 's#^PUBLIC_URL=.*#PUBLIC_URL=https://studio.example.test:9443#' "$repo/deploy/compose/.env.example" >"$case_dir/install/deploy/compose/.env"
	fi
	rc=0
	if [[ "$env_mode" == set ]]; then
		output="$(run_install "$case_dir" POTOCOLOM_PUBLIC_URL=http://192.168.1.10:8080)" || rc=$?
	else
		output="$(run_install "$case_dir")" || rc=$?
	fi
	env_file="$case_dir/install/deploy/compose/.env"
	case "$name" in
		install-writes-public-url)
			if [[ "$rc" -eq 0 && "$(sed -n 's/^PUBLIC_URL=//p' "$env_file" | tail -n 1)" == http://192.168.1.10:8080 && "$output" != *'only works from this machine'* \
				&& "$(grep -c ' pull | ' "$case_dir/docker.log")" -ge 1 && "$(grep -c ' up -d --no-build | ' "$case_dir/docker.log")" -eq 1 \
				&& "$(sed -n 's/^POTOCOLOM_VERSION=//p' "$env_file")" == v9.9.9 ]]; then printf 'ok %s\n' "$name"; else fail_case "$name"; fi
			;;
		fresh-default-and-help)
			if [[ "$rc" -eq 0 && "$(sed -n 's/^PUBLIC_URL=//p' "$env_file" | tail -n 1)" == http://localhost:8080 && "$output" == *'/releases/download/v9.9.9/install.sh | POTOCOLOM_DIR='*' POTOCOLOM_PUBLIC_URL=http://ADDRESS:8080 bash'* && "$output" == *'192.168.1.10 10.0.0.2'* && "$output" == *'docs/self-hosting.md'* ]]; then printf 'ok %s\n' "$name"; else fail_case "$name"; fi
			;;
		upgrade-keeps-public-url)
			if [[ "$rc" -eq 0 && "$(sed -n 's/^PUBLIC_URL=//p' "$env_file" | tail -n 1)" == https://studio.example.test:9443 ]]; then printf 'ok %s\n' "$name"; else fail_case "$name"; fi
			;;
	esac
}

check_case install-writes-public-url http://192.168.1.10:8080 set
check_case fresh-default-and-help http://localhost:8080 unset
check_case upgrade-keeps-public-url https://studio.example.test:9443 upgrade

invalid_case() {
	local name="$1" value="$2" output rc
	make_case "$name"
	rc=0
	output="$(run_install "$case_dir" "POTOCOLOM_PUBLIC_URL=$value")" || rc=$?
	if [[ "$rc" -eq 1 && "$output" == *'POTOCOLOM_PUBLIC_URL'* && ! -s "$case_dir/curl.log" ]]; then
		printf 'ok %s\n' "$name"
	else
		fail_case "$name"
	fi
}

invalid_case invalid-path http://example.test/path
invalid_case invalid-trailing-slash http://example.test/
invalid_case invalid-scheme ftp://example.test
invalid_case invalid-empty-host http://:8080
invalid_case invalid-default-https-port https://studio.example:443
invalid_case invalid-default-http-port http://studio.example:80
invalid_case invalid-port-range http://studio.example:65536
invalid_case invalid-leading-zero-port http://studio.example:08080
invalid_case invalid-ipv6-literal 'http://[::1]:8080'

# The value is stored in the canonical form browsers send as Origin.
make_case normalizes-case
output="$(run_install "$case_dir" POTOCOLOM_PUBLIC_URL=HTTP://Studio.Example:8080)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(sed -n 's/^PUBLIC_URL=//p' "$case_dir/install/deploy/compose/.env" | tail -n 1)" == http://studio.example:8080 ]]; then
	printf 'ok normalizes-case\n'
else
	fail_case normalizes-case
fi

# Every line form Compose would read is replaced, not only the plain one.
make_case replaces-every-line-form
mkdir -p "$case_dir/install/deploy/compose"
{
	cat "$repo/deploy/compose/.env.example"
	printf 'export PUBLIC_URL=http://old2:8080\n  PUBLIC_URL = http://old3:8080\n'
} >"$case_dir/install/deploy/compose/.env"
output="$(run_install "$case_dir" POTOCOLOM_PUBLIC_URL=http://192.168.1.10:8080)" && rc=0 || rc=$?
forms="$(grep -E '^[[:space:]]*(export[[:space:]]+)?PUBLIC_URL[[:space:]]*=' "$case_dir/install/deploy/compose/.env")"
if [[ "$rc" -eq 0 && "$forms" == 'PUBLIC_URL=http://192.168.1.10:8080' ]]; then
	printf 'ok replaces-every-line-form\n'
else
	fail_case replaces-every-line-form
fi

# Compose prefers a shell variable over .env, so the installer drops it.
make_case inherited-public-url-dropped
output="$(run_install "$case_dir" PUBLIC_URL=http://localhost:8080 POTOCOLOM_PUBLIC_URL=http://192.168.1.10:8080)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && -s "$case_dir/docker.log" ]] && ! grep -q 'PUBLIC_URL=http://' "$case_dir/docker.log"; then
	printf 'ok inherited-public-url-dropped\n'
else
	fail_case inherited-public-url-dropped
fi

if ((failures > 0)); then exit 1; fi
