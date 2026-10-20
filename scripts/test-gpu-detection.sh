#!/usr/bin/env bash
set -uo pipefail
# ensure-env.sh honours these; inherited, they would point at a real .env.
unset ENV_FILE ENV_EXAMPLE

repo="$(dirname "${BASH_SOURCE[0]}")/.."
tmp="$(mktemp -d "$repo/.gpu-detection.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT
failures=0

fail_case() {
	local name="$1" output="$2"
	printf 'FAIL %s\n%s\n' "$name" "$output"
	failures=$((failures + 1))
}

new_case() {
	case_dir="$tmp/$1"
	mkdir -p "$case_dir/bin"
	cat >"$case_dir/bin/nvidia-smi" <<'EOF'
#!/usr/bin/env bash
if [[ "${FAKE_NVIDIA:-fail}" == ok ]]; then
	printf 'Test GPU\n'
	exit 0
fi
if [[ "${FAKE_NVIDIA:-fail}" == two ]]; then
	printf 'Test GPU 0\n'
	sleep 0.2
	printf 'Test GPU 1\n'
	exit 0
fi
exit 9
EOF
	chmod +x "$case_dir/bin/nvidia-smi"
}

expect_profile() {
	local name="$1" nvidia="$2" kfd="$3" expected="$4" actual
	new_case "$name"
	if [[ "$kfd" == present ]]; then
		: >"$case_dir/kfd"
	fi
	actual="$(FAKE_NVIDIA="$nvidia" POTOCOLOM_KFD="$case_dir/kfd" PATH="$case_dir/bin:/usr/bin:/bin" bash "$repo/scripts/gpu-profile.sh" 2>&1)"
	if [[ "$actual" == "$expected" ]]; then
		printf 'ok %s\n' "$name"
	else
		fail_case "$name" "expected: $expected\nactual: $actual"
	fi
}

expect_preflight_gpu() {
	local name="$1" vendor="$2" expected="$3" kfd="${4:-absent}" slot="0000:01:00.0" output kfd_path
	new_case "$name"
	mkdir -p "$case_dir/repo/scripts" "$case_dir/repo/deploy/compose" "$case_dir/repo/worker/models" "$case_dir/pci/$slot"
	cp "$repo/scripts/preflight.sh" "$repo/scripts/gpu-profile.sh" "$repo/scripts/ensure-env.sh" "$case_dir/repo/scripts/"
	cp "$repo/deploy/compose/.env.example" "$case_dir/repo/deploy/compose/.env.example"
	cp "$repo/worker/models/"*.json "$case_dir/repo/worker/models/"
	printf '0x030000\n' >"$case_dir/pci/$slot/class"
	printf '%s\n' "$vendor" >"$case_dir/pci/$slot/vendor"
	if [[ "$kfd" == present ]]; then
		: >"$case_dir/kfd"
		kfd_path="$case_dir/kfd"
	else
		kfd_path="$case_dir/no-kfd"
	fi
	output="$(FAKE_NVIDIA=fail POTOCOLOM_KFD="$kfd_path" POTOCOLOM_PCI_DEVICES="$case_dir/pci" POTOCOLOM_SKIP_GPU_TEST=1 TMPDIR="$case_dir" PATH="$case_dir/bin:/usr/bin:/bin" bash "$case_dir/repo/scripts/preflight.sh" --contributor 2>&1)" || true
	if [[ "$output" == *"$expected"* && "$output" == *"$slot"* ]]; then
		printf 'ok %s\n' "$name"
	else
		fail_case "$name" "$output"
	fi
}

expect_toolchain() {
	local name="$1" node_version="$2" npm_present="$3" expected="$4" output rc
	new_case "$name"
	mkdir -p "$case_dir/repo/scripts" "$case_dir/repo/deploy/compose" "$case_dir/repo/worker/models"
	for utility in bash dirname uname awk df tail tr sort grep cut xargs id openssl mktemp mv sed head; do
		ln -s "$(command -v "$utility")" "$case_dir/bin/$utility"
	done
	cp "$repo/scripts/preflight.sh" "$repo/scripts/gpu-profile.sh" "$repo/scripts/ensure-env.sh" "$case_dir/repo/scripts/"
	cp "$repo/deploy/compose/.env.example" "$case_dir/repo/deploy/compose/.env.example"
	cp "$repo/worker/models/"*.json "$case_dir/repo/worker/models/"
	cat >"$case_dir/bin/make" <<'EOF'
#!/usr/bin/env bash
target="${!#}"
case "$target" in
	check-python) printf 'venvs use /usr/bin/python3 (3.12.0)\n' ;;
	check-node)
		if node -e 'process.exit(Number(process.versions.node.split(".")[0]) < 24 ? 1 : 0)'; then
			exit 0
		fi
		printf 'error: Node.js 24 or newer is required (found %s).\n' "$(node --version)" >&2
		exit 1
		;;
	*) exit 2 ;;
esac
EOF
	cat >"$case_dir/bin/node" <<EOF
#!/usr/bin/env bash
if [[ "\$1" == --version ]]; then
	printf '%s\\n' '$node_version'
	exit 0
fi
if [[ "\$1" == -e ]]; then
	if [[ '$node_version' == v24.* ]]; then exit 0; fi
	exit 1
fi
exit 2
EOF
	cat >"$case_dir/bin/docker" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == --version ]]; then printf 'Docker version 28.0.0, build test\n'; exit 0; fi
if [[ "$1" == compose ]]; then
	if [[ "$2" == version ]]; then printf '2.30.0\n'; exit 0; fi
fi
exit 0
EOF
	if [[ "$npm_present" == true ]]; then
		cat >"$case_dir/bin/npm" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
	fi
	chmod +x "$case_dir/bin/make" "$case_dir/bin/node" "$case_dir/bin/docker"
	[[ "$npm_present" != true ]] || chmod +x "$case_dir/bin/npm"
	output="$(FAKE_NVIDIA=fail POTOCOLOM_KFD="$case_dir/no-kfd" POTOCOLOM_PCI_DEVICES="$case_dir/no-pci" POTOCOLOM_SKIP_GPU_TEST=1 TMPDIR="$case_dir" PATH="$case_dir/bin" bash "$case_dir/repo/scripts/preflight.sh" --contributor 2>&1)"
	rc=$?
	if [[ "$output" == *"$expected"* ]]; then
		if [[ "$name" == toolchain-all-present && "$output" == *"fail"* ]] ||
			[[ "$name" != toolchain-all-present && "$rc" -ne 1 ]]; then
			fail_case "$name" "$output"
		else
			printf 'ok %s\n' "$name"
		fi
	else
		fail_case "$name" "$output"
	fi
}

expect_profile profile-nvidia-kfd-wins ok present gpu
expect_profile profile-nvidia-without-kfd ok absent gpu
expect_profile profile-two-nvidia-gpus two present gpu
expect_profile profile-rocm nvidia-fail present rocm
expect_profile profile-smoke nvidia-fail absent smoke
expect_preflight_gpu preflight-unusable-nvidia 0x10de 'NVIDIA GPU found at'
expect_preflight_gpu preflight-rocm-unusable-nvidia 0x10de 'NVIDIA GPU found at' present
expect_preflight_gpu preflight-amd-without-kfd 0x1002 'AMD GPU found at'

expect_toolchain toolchain-old-node v20.0.0 true 'Node.js 24+ is required'
expect_toolchain toolchain-missing-npm v24.0.0 false 'npm not installed'
expect_toolchain toolchain-all-present v24.0.0 true 'Toolchain'

new_case preflight-unsupported-pci
mkdir -p "$case_dir/repo/scripts" "$case_dir/repo/deploy/compose" "$case_dir/repo/worker/models" "$case_dir/pci/0000:02:00.0"
cp "$repo/scripts/preflight.sh" "$repo/scripts/gpu-profile.sh" "$repo/scripts/ensure-env.sh" "$case_dir/repo/scripts/"
cp "$repo/deploy/compose/.env.example" "$case_dir/repo/deploy/compose/.env.example"
cp "$repo/worker/models/"*.json "$case_dir/repo/worker/models/"
printf '0x030000\n' >"$case_dir/pci/0000:02:00.0/class"
printf '0x8086\n' >"$case_dir/pci/0000:02:00.0/vendor"
output="$(FAKE_NVIDIA=fail POTOCOLOM_KFD="$case_dir/no-kfd" POTOCOLOM_PCI_DEVICES="$case_dir/pci" POTOCOLOM_SKIP_GPU_TEST=1 TMPDIR="$case_dir" PATH="$case_dir/bin:/usr/bin:/bin" bash "$case_dir/repo/scripts/preflight.sh" --contributor 2>&1)" || true
if [[ "$output" == *"no NVIDIA or AMD compute GPU detected"* ]]; then
	printf 'ok preflight-unsupported-pci\n'
else
	fail_case preflight-unsupported-pci "$output"
fi

new_case preflight-unknown-argument
mkdir -p "$case_dir/repo/scripts"
cp "$repo/scripts/preflight.sh" "$case_dir/repo/scripts/"
code=0
output="$(bash "$case_dir/repo/scripts/preflight.sh" --bogus 2>&1)" || code=$?
if [[ "$code" -eq 2 && "$output" == *"usage: preflight.sh [--contributor]"* ]]; then
	printf 'ok preflight-unknown-argument\n'
else
	fail_case preflight-unknown-argument "exit=$code\n$output"
fi

if ((failures > 0)); then
	exit 1
fi
