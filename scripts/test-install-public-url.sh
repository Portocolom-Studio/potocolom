#!/usr/bin/env bash
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
REAL_CURL="$(command -v curl)"
failures=0
release_test_awk="$(type -P awk)"

fail_case() {
	printf 'FAIL %s\n' "$1"
	failures=$((failures + 1))
}

stage_bundle() {
	local stage="$1"
	mkdir -p "$stage/deploy/compose" "$stage/scripts"
	cp "$repo/deploy/compose/compose.yml" "$repo/deploy/compose/.env.example" "$stage/deploy/compose/"
	cp "$repo/scripts/preflight.sh" "$repo/scripts/ensure-env.sh" "$repo/scripts/install-lock.sh" "$repo/scripts/gpu-profile.sh" "$stage/scripts/"
	(cd "$stage" && tar -czf "$tmp/bundle.tar.gz" deploy scripts)
	(cd "$tmp" && sha256sum bundle.tar.gz | sed 's#bundle.tar.gz#potocolom-v9.9.9-selfhost.tar.gz#' >SHA256SUMS)
	cp "$tmp/bundle.tar.gz" "$stage/potocolom-v9.9.9-selfhost.tar.gz"
	cp "$tmp/SHA256SUMS" "$stage/SHA256SUMS"
}

make_case() {
	local name="$1"
	READY_AFTER=0 READY_FAIL=0 TEST_PROFILE=gpu TEST_NVIDIA=ok
	case_dir="$tmp/$name"
	mkdir -p "$case_dir/bin" "$case_dir/source"
	cat >"$case_dir/bin/curl" <<'EOF'
#!/usr/bin/env bash
if [[ -n "${CURL_LOG:-}" ]]; then printf '%s\n' "$*" >>"$CURL_LOG.args"; fi
while (($#)); do
	if [[ "$1" == -o ]]; then
		out="$2"
		shift 2
	elif [[ "$1" == http://* || "$1" == https://* || "$1" == file://* ]]; then
		url="$1"
		shift
	else
		shift
	fi
done
if [[ -z "${out:-}" ]]; then
	if [[ "${url:-}" == */api/v1/ready ]]; then
		printf '%s\n' "$url" >>"${CURL_LOG:?}"
		count=0
		[[ -f "${READY_COUNT_FILE:?}" ]] && count="$(cat "$READY_COUNT_FILE")"
		count=$((count + 1))
		printf '%s\n' "$count" >"$READY_COUNT_FILE"
		if [[ "${READY_FAIL:-0}" == 1 || "$count" -le "${READY_AFTER:-0}" ]]; then exit 22; fi
		exit 0
	fi
	printf '{"status":"ok"}\n'
	exit 0
fi
printf '%s\n' "${url:-}" >>"${CURL_LOG:?}"
if [[ "${url:-}" == file://* ]]; then
	exec "$REAL_CURL" -fsSL -o "$out" "$url"
fi
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
	*' config --images')
		if [[ "$*" == *'--profile api'* ]]; then printf 'postgres:16\nghcr.io/portocolom-studio/potocolom-api:%s\n' "$POTOCOLOM_VERSION"
		else printf 'postgres:16\nghcr.io/portocolom-studio/potocolom-api:%s\nghcr.io/portocolom-studio/potocolom-worker:%s-cuda\n' "$POTOCOLOM_VERSION" "$POTOCOLOM_VERSION"; fi ;;
	'image inspect'*) [[ "${DOCKER_IMAGES_CACHED:-}" == 1 ]] ;;
	*' pull --policy'* ) exit 99 ;;
	*' pull') [[ "${DOCKER_FAIL_PULL:-}" != 1 ]] ;;
	*' up -d --no-build')
		if [[ -n "${INSTALL_UP_STARTED:-}" ]]; then
			touch "$INSTALL_UP_STARTED"
			while [[ ! -e "$INSTALL_UP_RELEASE" ]]; do sleep 0.02; done
		fi
		exit 0
		;;
	*' pull'|*' up -d --no-build'|*' rm -sf'|*' compose version'|*) exit 0 ;;
esac
EOF
	cat >"$case_dir/bin/nvidia-smi" <<'EOF'
#!/usr/bin/env bash
[[ "${FAKE_NVIDIA:-ok}" == fail ]] && exit 9
if [[ "$*" == *'--query-gpu=name,memory.total,driver_version'* ]]; then
	printf 'NVIDIA Test GPU, 8192 MiB, 595.91.07\n'
elif [[ "$*" == *'--query-gpu=name'* ]]; then
	printf 'NVIDIA Test GPU\n'
else
	printf 'CUDA Version: 13.2\n'
fi
EOF
	cat >"$case_dir/bin/df" <<'EOF'
#!/usr/bin/env bash
printf 'Avail\n100G\n'
EOF
	cat >"$case_dir/bin/awk" <<'EOF'
#!/usr/bin/env bash
if [[ $# == 2 && "$2" == /proc/meminfo ]]; then
	printf '33554432\n'
else
	exec "${RELEASE_TEST_AWK:?}" "$@"
fi
EOF
	chmod +x "$case_dir/bin/df" "$case_dir/bin/awk"
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
	local profile="${TEST_PROFILE:-gpu}" fake_nvidia="${TEST_NVIDIA:-ok}"
	local profile_env=()
	shift
	[[ "$profile" == auto ]] || profile_env=(POTOCOLOM_PROFILE="$profile")
	env PATH="$case_dir/bin:$PATH" RELEASE_TEST_AWK="$release_test_awk" CURL_LOG="$case_dir/curl.log" DOCKER_LOG="$case_dir/docker.log" BUNDLE_DIR="$tmp" REAL_CURL="$REAL_CURL" READY_COUNT_FILE="$case_dir/ready-count" READY_AFTER="${READY_AFTER:-0}" READY_FAIL="${READY_FAIL:-0}" FAKE_NVIDIA="$fake_nvidia" DOCKER_FAIL_PULL="${DOCKER_FAIL_PULL:-}" DOCKER_IMAGES_CACHED="${DOCKER_IMAGES_CACHED:-}" \
		POTOCOLOM_SKIP_GPU_TEST=1 POTOCOLOM_KFD="$case_dir/no-kfd" POTOCOLOM_VERSION=v9.9.9 POTOCOLOM_DIR="$case_dir/install" \
		"${profile_env[@]}" "$@" bash "$repo/scripts/install.sh" 2>&1
}

check_case() {
	local name="$1" expected="$2" env_mode="$3" output rc env_file
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
			if [[ "$rc" -eq 0 && "$(sed -n 's/^PUBLIC_URL=//p' "$env_file" | tail -n 1)" == "$expected" && "$output" != *'only works from this machine'* \
				&& "$(grep -c ' pull | ' "$case_dir/docker.log")" -ge 1 && "$(grep -c ' up -d --no-build | ' "$case_dir/docker.log")" -eq 1 \
				&& "$(sed -n 's/^POTOCOLOM_VERSION=//p' "$env_file")" == v9.9.9 ]]; then printf 'ok %s\n' "$name"; else fail_case "$name"; fi
			;;
		fresh-default-and-help)
			if [[ "$rc" -eq 0 && "$(sed -n 's/^PUBLIC_URL=//p' "$env_file" | tail -n 1)" == "$expected" && "$output" == *'/releases/download/v9.9.9/install.sh | POTOCOLOM_DIR='*' POTOCOLOM_PUBLIC_URL=http://ADDRESS:8080 bash'* && "$output" == *'192.168.1.10 10.0.0.2'* && "$output" == *'docs/self-hosting.md'* ]]; then printf 'ok %s\n' "$name"; else fail_case "$name"; fi
			;;
		upgrade-keeps-public-url)
			if [[ "$rc" -eq 0 && "$(sed -n 's/^PUBLIC_URL=//p' "$env_file" | tail -n 1)" == "$expected" ]]; then printf 'ok %s\n' "$name"; else fail_case "$name"; fi
			;;
	esac
}

make_case preflight-health-probe
if "$case_dir/bin/curl" -sf --max-time 2 http://localhost:8080/api/v1/health | grep -q '"status"'; then
	printf 'ok preflight-health-probe\n'
else
	fail_case preflight-health-probe
fi

check_case install-writes-public-url http://192.168.1.10:8080 set
check_case fresh-default-and-help http://localhost:8080 unset
check_case upgrade-keeps-public-url https://studio.example.test:9443 upgrade

make_case cached-images-reused
rc=0
output="$(DOCKER_IMAGES_CACHED=1 run_install "$case_dir")" || rc=$?
if [[ "$rc" -eq 0 && "$(grep -c 'image inspect' "$case_dir/docker.log")" -eq 3 \
	&& "$(grep -c ' pull | ' "$case_dir/docker.log")" -eq 0 \
	&& "$(sed -n 's/^POTOCOLOM_VERSION=//p' "$case_dir/install/deploy/compose/.env")" == v9.9.9 ]]; then
	printf 'ok cached-images-reused\n'
else
	fail_case cached-images-reused
fi

make_case automatic-api-only-no-gpu
TEST_PROFILE=auto TEST_NVIDIA=fail output="$(run_install "$case_dir")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$output" == *'API and database are ready'* && "$output" == *'Generation needs an attached GPU worker'* \
	&& "$(grep -c 'config --images' "$case_dir/docker.log")" -eq 1 \
	&& "$(grep 'config --images' "$case_dir/docker.log")" != *'worker' && "$(grep -c 'pull' "$case_dir/docker.log")" -eq 1 \
	&& "$(grep ' up -d --no-build' "$case_dir/docker.log")" == *'--profile api'* \
	&& "$(grep -c 'rm -sf worker ' "$case_dir/docker.log")" -eq 1 \
	&& "$(grep -c 'rm -sf worker-rocm' "$case_dir/docker.log")" -eq 1 ]]; then
	printf 'ok automatic-api-only-no-gpu\n'
else fail_case automatic-api-only-no-gpu; printf '%s\n' "$output"; cat "$case_dir/docker.log"; fi

make_case explicit-api-bypasses-gpu-detection
TEST_PROFILE=api output="$(run_install "$case_dir")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$output" == *'API and database are ready'* && "$(grep 'up -d --no-build' "$case_dir/docker.log")" == *'--profile api'* ]]; then
	printf 'ok explicit-api-bypasses-gpu-detection\n'
else fail_case explicit-api-bypasses-gpu-detection; fi

make_case invalid-install-profile
TEST_PROFILE=invalid output="$(run_install "$case_dir")" && rc=0 || rc=$?
if [[ "$rc" -ne 0 && "$output" == *'POTOCOLOM_PROFILE must be'* && "$(grep -c 'config --images\| pull\| up -d' "$case_dir/docker.log")" -eq 0 ]]; then
	printf 'ok invalid-install-profile\n'
else fail_case invalid-install-profile; printf '%s\n' "$output"; cat "$case_dir/docker.log"; fi

make_case explicit-smoke-release-rejected
TEST_PROFILE=smoke output="$(run_install "$case_dir")" && rc=0 || rc=$?
if [[ "$rc" -ne 0 && "$output" == *'release installer does not support the simulated worker'* && "$(grep -c 'config --images\| pull\| up -d' "$case_dir/docker.log")" -eq 0 ]]; then
	printf 'ok explicit-smoke-release-rejected\n'
else fail_case explicit-smoke-release-rejected; fi

make_case readiness-retries-then-succeeds
READY_AFTER=2 TEST_PROFILE=api output="$(run_install "$case_dir")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && -f "$case_dir/ready-count" && "$(cat "$case_dir/ready-count")" == 3 && "$(grep /api/v1/ready "$case_dir/curl.log.args" | head -n 1)" == *'--connect-timeout 2 --max-time 2'* ]]; then printf 'ok readiness-retries-then-succeeds\n'; else fail_case readiness-retries-then-succeeds; printf 'rc=%s count=%s\n%s\n' "$rc" "$(cat "$case_dir/ready-count" 2>/dev/null || true)" "$output"; cat "$case_dir/curl.log"; cat "$case_dir/docker.log"; fi

make_case readiness-timeout-releases-lock
printf '#!/usr/bin/env bash\nexit 0\n' >"$case_dir/bin/sleep"
chmod +x "$case_dir/bin/sleep"
READY_FAIL=1 TEST_PROFILE=api output="$(run_install "$case_dir")" && rc=0 || rc=$?
if [[ "$rc" -ne 0 && "$output" == *'did not become ready within 120 seconds'* && "$output" == *'logs api postgres'* && "$(cat "$case_dir/ready-count")" == 30 \
	&& -s "$case_dir/install/deploy/compose/.env" ]]; then
	READY_FAIL=0 TEST_PROFILE=api output="$(run_install "$case_dir")" && retry_rc=0 || retry_rc=$?
	if [[ "$retry_rc" -eq 0 ]]; then printf 'ok readiness-timeout-releases-lock\n'; else fail_case readiness-timeout-releases-lock; fi
else fail_case readiness-timeout-releases-lock; fi

make_case rejects-unsupported-release-architecture
cat >"$case_dir/bin/uname" <<'EOF'
#!/usr/bin/env bash
case "$1" in -s) printf 'Linux\n' ;; -m) printf 'aarch64\n' ;; *) exit 1 ;; esac
EOF
chmod +x "$case_dir/bin/uname"
output="$(run_install "$case_dir")" && rc=0 || rc=$?
if [[ "$rc" -ne 0 && "$output" == *'release images support Linux x86_64 only (found Linux aarch64)'* && ! -s "$case_dir/curl.log" ]]; then
	printf 'ok rejects-unsupported-release-architecture\n'
else fail_case rejects-unsupported-release-architecture; fi

make_case release-url-default
output="$(run_install "$case_dir")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(head -n 1 "$case_dir/curl.log")" == https://github.com/Portocolom-Studio/potocolom/releases/download/v9.9.9/potocolom-v9.9.9-selfhost.tar.gz ]]; then
	printf 'ok release-url-default\n'
else
	fail_case release-url-default
fi

make_case release-url-file
output="$(run_install "$case_dir" POTOCOLOM_RELEASE_URL="file://$case_dir/source/")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(head -n 1 "$case_dir/curl.log")" == "file://$case_dir/source/potocolom-v9.9.9-selfhost.tar.gz" ]]; then
	printf 'ok release-url-file\n'
else
	fail_case release-url-file
fi

make_case release-url-https
output="$(run_install "$case_dir" POTOCOLOM_RELEASE_URL=https://releases.example.test/path/)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(head -n 1 "$case_dir/curl.log")" == https://releases.example.test/path/potocolom-v9.9.9-selfhost.tar.gz ]]; then
	printf 'ok release-url-https\n'
else
	fail_case release-url-https
fi

make_case checksum-fails-before-extract
printf '%064d  potocolom-v9.9.9-selfhost.tar.gz\n' 0 >"$tmp/SHA256SUMS"
rc=0
output="$(run_install "$case_dir")" || rc=$?
if [[ "$rc" -ne 0 && "$output" == *'SHA256SUMS does not verify'* && ! -e "$case_dir/install/deploy/compose/.env" ]]; then
	printf 'ok checksum-fails-before-extract\n'
else
	fail_case checksum-fails-before-extract
fi

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
invalid_case invalid-empty-hostname-label http://studio..example.com:8080
invalid_case invalid-hostname-label-hyphen http://studio-.example.com:8080
label_63="$(printf 'a%.0s' {1..63})"
invalid_case invalid-hostname-label-length "http://${label_63}a.example"
invalid_case invalid-hostname-length "http://${label_63}.${label_63}.${label_63}.${label_63}"
make_case longest-valid-hostname
valid_host="${label_63}.${label_63}.${label_63}.${label_63:0:61}"
output="$(run_install "$case_dir" "POTOCOLOM_PUBLIC_URL=http://$valid_host:8080")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$output" == *"Open http://$valid_host:8080"* ]]; then
	printf 'ok longest-valid-hostname\n'
else
	fail_case longest-valid-hostname
fi

invalid_release_case() {
	local name="$1" value="$2" output rc
	make_case "$name"
	rc=0
	output="$(run_install "$case_dir" "POTOCOLOM_RELEASE_URL=$value")" || rc=$?
	if [[ "$rc" -eq 1 && "$output" == *'POTOCOLOM_RELEASE_URL'* && ! -s "$case_dir/curl.log" ]]; then
		printf 'ok %s\n' "$name"
	else
		fail_case "$name"
	fi
}
invalid_release_case release-url-blank ''
invalid_release_case release-url-relative 'bundle'
invalid_release_case release-url-ftp 'ftp://example.test/release'
invalid_release_case release-url-relative-file 'file://relative/path'
invalid_release_case release-url-malformed-https 'https://'

make_case pull-failure-keeps-version
mkdir -p "$case_dir/install/deploy/compose"
sed 's/^POTOCOLOM_VERSION=.*/POTOCOLOM_VERSION=v8.8.8/' "$repo/deploy/compose/.env.example" >"$case_dir/install/deploy/compose/.env"
DOCKER_FAIL_PULL=1
output="$(run_install "$case_dir")" && rc=0 || rc=$?
unset DOCKER_FAIL_PULL
if [[ "$rc" -ne 0 && "$(sed -n 's/^POTOCOLOM_VERSION=//p' "$case_dir/install/deploy/compose/.env")" == v8.8.8 ]]; then
	printf 'ok pull-failure-keeps-version\n'
else
	fail_case pull-failure-keeps-version
fi

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

# Hold the first installer at startup so a second process must hit the same
# directory lock while the database and secret file are still in one session.
make_case concurrent-install-lock
REAL_MKTEMP="$(type -P mktemp)"
export REAL_MKTEMP
unlink "$case_dir/bin/mktemp"
cat >"$case_dir/bin/mktemp" <<'EOF'
#!/usr/bin/env bash
for arg in "$@"; do
	[[ "$arg" == */.env.XXXXXX ]] && printf '%s\n' "$arg" >>"${INSTALL_MKTEMP_LOG:?}"
done
exec "$REAL_MKTEMP" "$@"
EOF
chmod +x "$case_dir/bin/mktemp"
export INSTALL_MKTEMP_LOG="$case_dir/mktemp.log"
export INSTALL_UP_STARTED="$case_dir/up-started" INSTALL_UP_RELEASE="$case_dir/up-release"
run_install "$case_dir" >"$case_dir/first.out" 2>&1 &
first_install_pid=$!
for _ in {1..400}; do [[ -e "$INSTALL_UP_STARTED" ]] && break; sleep 0.02; done
if [[ ! -e "$INSTALL_UP_STARTED" ]]; then
	touch "$INSTALL_UP_RELEASE"
	wait "$first_install_pid" || true
	fail_case concurrent-install-lock 'first installer did not reach startup'
else
	# A contender receives a different verified helper and compose file. It must
	# not extract those files into the live install before it gets the locks.
	mkdir -p "$case_dir/replacement"
	tar -xzf "$tmp/bundle.tar.gz" -C "$case_dir/replacement"
	printf '# staged-contender-marker\n' >>"$case_dir/replacement/scripts/gpu-profile.sh"
	(cd "$case_dir/replacement" && tar -czf "$tmp/bundle.tar.gz" deploy scripts)
	(cd "$tmp" && sha256sum bundle.tar.gz | sed 's#bundle.tar.gz#potocolom-v9.9.9-selfhost.tar.gz#' >SHA256SUMS)
	if run_install "$case_dir" >"$case_dir/second.out" 2>&1; then second_install_rc=0; else second_install_rc=$?; fi
	touch "$INSTALL_UP_RELEASE"
	if wait "$first_install_pid"; then first_install_rc=0; else first_install_rc=$?; fi
	if [[ "$first_install_rc" -eq 0 && "$second_install_rc" -ne 0 \
		&& "$(cat "$case_dir/second.out")" == *'another potocolom install or preflight'* \
		&& "$(grep -c 'staged-contender-marker' "$case_dir/install/scripts/gpu-profile.sh" || true)" -eq 0 \
		&& "$(grep -c "$case_dir/install/deploy/compose/.env.XXXXXX" "$INSTALL_MKTEMP_LOG")" -ge 2 \
		&& "$(stat -c '%a' "$case_dir/install/deploy/compose/.env")" == 600 ]]; then
		printf 'ok concurrent-install-lock-held-through-startup-and-before-live-extract\n'
	else
		fail_case concurrent-install-lock "first=$first_install_rc second=$second_install_rc marker=$(grep -c 'staged-contender-marker' "$case_dir/install/scripts/gpu-profile.sh" || true) $(cat "$case_dir/second.out")"
	fi
fi
unset INSTALL_UP_STARTED INSTALL_UP_RELEASE INSTALL_MKTEMP_LOG REAL_MKTEMP

if ((failures > 0)); then exit 1; fi
