#!/usr/bin/env bash
set -uo pipefail
# ensure-env.sh honours these; inherited, they would point at a real .env.
unset ENV_FILE ENV_EXAMPLE
unset POTOCOLOM_SKIP_GPU_TEST

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
	if [[ "$*" == *'--query-gpu=name,memory.total,driver_version'* ]]; then
		printf 'NVIDIA GeForce RTX 3050 Laptop GPU, 4096 MiB, 595.91.07\n'
	elif [[ "$*" == *'--query-gpu=name'* ]]; then
		printf 'NVIDIA GeForce RTX 3050 Laptop GPU\n'
	else
		printf 'CUDA Version: 13.2\n'
	fi
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
	for utility in bash dirname uname awk df tail tr sort head grep cut xargs id openssl mktemp mv sed; do
		[[ -e "$case_dir/bin/$utility" ]] || ln -s "$(command -v "$utility")" "$case_dir/bin/$utility"
	done
}

# The repository layout preflight reads, so ensure-env writes only in the case.
copy_repo() {
	mkdir -p "$case_dir/repo/scripts" "$case_dir/repo/deploy/compose" "$case_dir/repo/worker/models"
	cp "$repo/scripts/preflight.sh" "$repo/scripts/gpu-profile.sh" "$repo/scripts/ensure-env.sh" "$case_dir/repo/scripts/"
	cp "$repo/deploy/compose/.env.example" "$case_dir/repo/deploy/compose/.env.example"
	cp "$repo/worker/models/"*.json "$case_dir/repo/worker/models/"
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
	local name="$1" vendor="$2" expected="$3" kfd="${4:-absent}" id="${5:-ubuntu}" id_like="${6:-}" nvidia="${7:-fail}" docker_mode="${8:-ok}" toolkit="${9:-false}" expected_rc="${10:-any}" unexpected="${11:-}" slot="0000:01:00.0" output kfd_path contributor="--contributor" env_args=()
	new_case "$name"
	copy_repo
	if [[ "$id" != missing ]]; then
		printf 'ID=%s\nID_LIKE="%s"\n' "$id" "$id_like" >"$case_dir/os-release"
	fi
	mkdir -p "$case_dir/pci/$slot"
	printf '0x030000\n' >"$case_dir/pci/$slot/class"
	printf '%s\n' "$vendor" >"$case_dir/pci/$slot/vendor"
	if [[ "$kfd" == present ]]; then
		: >"$case_dir/kfd"
		kfd_path="$case_dir/kfd"
	else
		kfd_path="$case_dir/no-kfd"
	fi
	cat >"$case_dir/bin/docker" <<EOF
#!/usr/bin/env bash
if [[ "\$1" == --version ]]; then printf 'Docker version 28.0.0, build test\\n'; exit 0; fi
if [[ "\$1" == compose ]]; then printf '2.30.0\\n'; exit 0; fi
if [[ "\$1" == info ]]; then exit 0; fi
if [[ "\$1" == run && '$docker_mode' == fail ]]; then exit 1; fi
exit 0
EOF
	chmod +x "$case_dir/bin/docker"
	if [[ "$toolkit" == true || "$toolkit" == ctk ]]; then
		toolkit_binary=nvidia-ctk
	elif [[ "$toolkit" == runtime ]]; then
		toolkit_binary=nvidia-container-runtime
	elif [[ "$toolkit" == cli ]]; then
		toolkit_binary=nvidia-container-cli
	else
		toolkit_binary=""
	fi
	if [[ -n "$toolkit_binary" ]]; then
		printf '#!/usr/bin/env bash\nexit 0\n' >"$case_dir/bin/$toolkit_binary"
		chmod +x "$case_dir/bin/$toolkit_binary"
	fi
	[[ "$nvidia" == ok ]] && contributor=""
	env_args=("FAKE_NVIDIA=$nvidia" "POTOCOLOM_OS_RELEASE=$case_dir/os-release" "POTOCOLOM_KFD=$kfd_path" "POTOCOLOM_PCI_DEVICES=$case_dir/pci" "TMPDIR=$case_dir" "PATH=$case_dir/bin")
	[[ "$docker_mode" == fail ]] || env_args+=(POTOCOLOM_SKIP_GPU_TEST=1)
	output="$(env "${env_args[@]}" bash "$case_dir/repo/scripts/preflight.sh" $contributor 2>&1)"
	local rc=$?
	if [[ "$output" == *"$expected"* && ( -z "$unexpected" || "$output" != *"$unexpected"* ) && ( "$expected_rc" == any || "$rc" -eq "$expected_rc" ) ]]; then
		printf 'ok %s\n' "$name"
	else
		fail_case "$name" "exit=$rc\n$output"
	fi
}

expect_toolchain() {
	local name="$1" node_version="$2" npm_present="$3" expected="$4" expected_rc="$5" output rc
	new_case "$name"
	copy_repo
	cat >"$case_dir/bin/make" <<'EOF'
#!/usr/bin/env bash
target="${!#}"
case "$target" in
	check-python) printf 'venvs use /usr/bin/python3 (3.12.0)\n' ;;
	check-node)
		if node -e 'process.exit(Number(process.versions.node.split(".")[0]) < 24 ? 1 : 0)'; then
			printf 'node %s\n' "$(node --version)"
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
	if [[ "$output" == *"$expected"* && "$rc" -eq "$expected_rc" ]]; then
		printf 'ok %s\n' "$name"
	else
		fail_case "$name" "exit=$rc\n$output"
	fi
}

expect_profile profile-nvidia-kfd-wins ok present gpu
expect_profile profile-nvidia-without-kfd ok absent gpu
expect_profile profile-two-nvidia-gpus two present gpu
expect_profile profile-rocm nvidia-fail present rocm
expect_profile profile-smoke nvidia-fail absent smoke
expect_preflight_gpu preflight-unusable-nvidia 0x10de 'NVIDIA GPU found at 0000:01:00.0'
expect_preflight_gpu preflight-rocm-unusable-nvidia 0x10de 'NVIDIA GPU found at 0000:01:00.0' present
expect_preflight_gpu preflight-amd-without-kfd 0x1002 'AMD GPU found at 0000:01:00.0'
expect_preflight_gpu preflight-unsupported-pci 0x8086 'no NVIDIA or AMD compute GPU detected'

expect_preflight_gpu distro-ubuntu 0x10de 'sudo ubuntu-drivers install' absent ubuntu ''
expect_preflight_gpu distro-linuxmint 0x10de 'sudo ubuntu-drivers install' absent linuxmint 'ubuntu debian'
expect_preflight_gpu distro-debian 0x10de 'sudo apt-get update && sudo apt-get install -y nvidia-driver firmware-misc-nonfree' absent debian
expect_preflight_gpu distro-fedora 0x10de 'sudo dnf install -y nvidia-container-toolkit' absent fedora
expect_preflight_gpu distro-rocky 0x10de 'https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/' absent rocky 'rhel centos fedora'
expect_preflight_gpu distro-opensuse 0x10de 'sudo zypper --gpg-auto-import-keys install -y nvidia-container-toolkit' absent opensuse-tumbleweed 'opensuse suse'
expect_preflight_gpu distro-arch 0x10de 'https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html' absent arch
expect_preflight_gpu distro-missing-release 0x10de 'https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html' absent missing
expect_preflight_gpu laptop-toolkit-missing 0x8086 'sudo apt-get install -y nvidia-container-toolkit' absent debian '' ok fail false 1 'toolkit installed but passthrough is broken'
expect_preflight_gpu toolkit-present-passthrough-fails 0x8086 'toolkit installed but passthrough is broken' absent ubuntu '' ok fail true 1
expect_preflight_gpu toolkit-runtime-passthrough-fails 0x8086 'toolkit installed but passthrough is broken' absent ubuntu '' ok fail runtime 1

expect_toolchain toolchain-old-node v20.0.0 true 'Node.js 24+ is required' 1
expect_toolchain toolchain-missing-npm v24.0.0 false 'npm not installed' 1
expect_toolchain toolchain-all-present v24.0.0 true 'ok node v24.0.0' 0

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
