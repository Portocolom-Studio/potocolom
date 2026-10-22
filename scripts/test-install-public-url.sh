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
# Only downloads (-o) are served; anything else, such as preflight's health
# probe of a studio that happens to be on port 8080, gets a refused connection.
[[ -n "${out:-}" ]] || exit 7
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
invalid_case invalid-ipv4-leading-zero http://192.168.001.10:8080
invalid_case invalid-ipv4-hex http://0xc0.0xa8.1.10:8080
invalid_case invalid-ipv4-integer http://3232235786:8080
invalid_case invalid-ipv4-octet http://192.168.1.256:8080

# A hostname that only starts with 127. is not loopback.
make_case hostname-starting-127-is-not-loopback
output="$(run_install "$case_dir" POTOCOLOM_PUBLIC_URL=http://127.studio.example:8080)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$output" != *'only works from this machine'* ]]; then
	printf 'ok hostname-starting-127-is-not-loopback\n'
else
	fail_case hostname-starting-127-is-not-loopback
fi

# The final message reads the value the way Compose does.
make_case reads-export-and-comment
mkdir -p "$case_dir/install/deploy/compose"
sed 's|^PUBLIC_URL=.*|export PUBLIC_URL=http://192.168.1.10:8080 # set by hand|' "$repo/deploy/compose/.env.example" >"$case_dir/install/deploy/compose/.env"
output="$(run_install "$case_dir")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$output" == *'Open http://192.168.1.10:8080'$'\n'* && "$output" != *'only works from this machine'* ]]; then
	printf 'ok reads-export-and-comment\n'
else
	fail_case reads-export-and-comment
fi

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
	printf 'export PUBLIC_URL = "http://old2:8080" # old export\nPUBLIC_URL= http://old3:8080 # value spacing\n  PUBLIC_URL = http://old4:8080 # lhs spacing\nPUBLIC_URL: http://old5:8080\n'
} >"$case_dir/install/deploy/compose/.env"
output="$(run_install "$case_dir" POTOCOLOM_PUBLIC_URL=http://192.168.1.10:8080)" && rc=0 || rc=$?
forms="$(grep -E '^[[:space:]]*(export[[:space:]]+)?PUBLIC_URL[[:space:]]*[:=]' "$case_dir/install/deploy/compose/.env")"
if [[ "$rc" -eq 0 && "$forms" == 'PUBLIC_URL=http://192.168.1.10:8080' ]]; then
	printf 'ok replaces-every-line-form\n'
else
	fail_case replaces-every-line-form
fi

# The final message follows Compose syntax with spaces, either separator,
# optional export, comments, and quoted values.
make_case reads-spaced-colon-remote-url
mkdir -p "$case_dir/install/deploy/compose"
sed '/^PUBLIC_URL=/d' "$repo/deploy/compose/.env.example" >"$case_dir/install/deploy/compose/.env"
printf 'export PUBLIC_URL :  "https://studio.example.com" # operator value\r\n' >>"$case_dir/install/deploy/compose/.env"
output="$(run_install "$case_dir")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$output" == *'Open https://studio.example.com'$'\n'* ]]; then
	printf 'ok reads-spaced-colon-remote-url\n'
else
	fail_case reads-spaced-colon-remote-url
fi

# Preflight reads the last assignment from either separator form.
for format in colon spaced-equals; do
	name="preflight-loopback-$format"
	make_case "$name"
	mkdir -p "$case_dir/install/deploy/compose"
	sed '/^PUBLIC_URL=/d' "$repo/deploy/compose/.env.example" >"$case_dir/install/deploy/compose/.env"
	if [[ "$format" == colon ]]; then
		printf 'PUBLIC_URL: "http://localhost:8080" # local only\r\n' >>"$case_dir/install/deploy/compose/.env"
	else
		printf 'PUBLIC_URL = '\''http://127.0.0.1:8080'\'' # local only\r\n' >>"$case_dir/install/deploy/compose/.env"
	fi
	output="$(run_install "$case_dir")" && rc=0 || rc=$?
	if [[ "$rc" -eq 0 && "$output" == *'PUBLIC_URL is http://'*'the studio only works from this machine'* ]]; then
		printf 'ok %s\n' "$name"
	else
		fail_case "$name"
	fi
done

# Compose prefers a shell variable over .env, so the installer drops it.
make_case inherited-public-url-dropped
output="$(run_install "$case_dir" PUBLIC_URL=http://localhost:8080 POTOCOLOM_PUBLIC_URL=http://192.168.1.10:8080)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && -s "$case_dir/docker.log" ]] && ! grep -q 'PUBLIC_URL=http://' "$case_dir/docker.log"; then
	printf 'ok inherited-public-url-dropped\n'
else
	fail_case inherited-public-url-dropped
fi

make_case failed-optional-hostname-lookup
printf '#!/usr/bin/env bash\nexit 1\n' >"$case_dir/bin/hostname"
output="$(run_install "$case_dir")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$output" == *"Replace ADDRESS with this machine's address."* ]]; then
	printf 'ok failed-optional-hostname-lookup\n'
else
	fail_case failed-optional-hostname-lookup
fi

if ((failures > 0)); then exit 1; fi
