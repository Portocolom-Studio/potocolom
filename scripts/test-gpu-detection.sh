#!/usr/bin/env bash
set -uo pipefail
# ensure-env.sh honours these; inherited, they would point at a real .env.
unset ENV_FILE ENV_EXAMPLE
unset POTOCOLOM_SKIP_GPU_TEST POTOCOLOM_PROFILE

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
	for utility in bash dirname basename mkdir chmod uname awk df tail tr sort head grep cut xargs echo id openssl mktemp mv sed flock cksum stat readlink; do
		[[ -e "$case_dir/bin/$utility" ]] || ln -s "$(type -P "$utility")" "$case_dir/bin/$utility"
	done
	rm -f "$case_dir/bin/id"
	cat >"$case_dir/bin/id" <<'EOF'
#!/usr/bin/env bash
case "$1" in
	-u) printf '1000\n' ;;
	-nG) printf '%s\n' "${FAKE_GROUPS:-video render}" ;;
	*) exit 1 ;;
esac
EOF
	chmod +x "$case_dir/bin/id"
}

# The repository layout preflight reads, so ensure-env writes only in the case.
copy_repo() {
	mkdir -p "$case_dir/repo/scripts" "$case_dir/repo/deploy/compose" "$case_dir/repo/worker/models"
	cp "$repo/scripts/preflight.sh" "$repo/scripts/gpu-profile.sh" "$repo/scripts/ensure-env.sh" "$repo/scripts/install-lock.sh" "$case_dir/repo/scripts/"
	cp "$repo/deploy/compose/.env.example" "$case_dir/repo/deploy/compose/.env.example"
	cp "$repo/worker/models/"*.json "$case_dir/repo/worker/models/"
}

expect_profile() {
	local name="$1" nvidia="$2" kfd="$3" expected="$4" override="${5:-}" actual rc=0
	new_case "$name"
	if [[ "$kfd" == present ]]; then
		: >"$case_dir/kfd"
	fi
	actual="$(POTOCOLOM_PROFILE="$override" FAKE_NVIDIA="$nvidia" POTOCOLOM_KFD="$case_dir/kfd" PATH="$case_dir/bin:/usr/bin:/bin" bash "$repo/scripts/gpu-profile.sh" 2>&1)" || rc=$?
	if [[ "$expected" == error && "$rc" -ne 0 && "$actual" == *'POTOCOLOM_PROFILE must be'* ]]; then
		printf 'ok %s\n' "$name"
	elif [[ "$actual" == "$expected" && "$rc" -eq 0 ]]; then
		printf 'ok %s\n' "$name"
	else
		fail_case "$name" "expected: $expected\nactual: $actual"
	fi
}

# expect_preflight_gpu NAME EXPECTED [key=value...]
# keys: vendor (PCI vendor, default 0x10de), kfd (present|absent), topology
# (comma-separated gfx_target_version values), id, id_like
# (os-release; id=missing leaves no file, crlf=yes writes CRLF line ends), nvidia (ok|fail), docker (ok|fail
# for run --gpus), toolkit (none|ctk|runtime|cli), rc (exit code, or any),
# absent (text that must not appear), mode (contributor|selfhost; default
# contributor without a working NVIDIA GPU, selfhost with one), profile
# (POTOCOLOM_PROFILE), groups (what id -nG prints), render (readable|unreadable
# for a fake /dev/dri/renderD128).
expect_preflight_gpu() {
	local name="$1" expected="$2" vendor=0x10de kfd=absent topology="" id=ubuntu id_like="" nvidia=fail docker=ok toolkit=none rc=any absent="" mode="" crlf=no profile="" groups="video render" render=readable public_url=""
	local slot="0000:01:00.0" output code kfd_path option tool node=0 value
	shift 2
	for option in "$@"; do
		local "$option"
	done
	new_case "$name"
	copy_repo
	if [[ -n "$public_url" ]]; then
		printf 'PUBLIC_URL=%s\n' "$public_url" >>"$case_dir/repo/deploy/compose/.env"
	fi
	if [[ "$id" != missing ]]; then
		printf 'ID=%s\nID_LIKE="%s"\n' "$id" "$id_like" >"$case_dir/os-release"
		[[ "$crlf" == yes ]] && sed -i 's/$/\r/' "$case_dir/os-release"
	fi
	mkdir -p "$case_dir/pci/$slot"
	printf '0x030000\n' >"$case_dir/pci/$slot/class"
	printf '%s\n' "$vendor" >"$case_dir/pci/$slot/vendor"
	kfd_path="$case_dir/no-kfd"
	if [[ "$kfd" == present ]]; then
		kfd_path="$case_dir/kfd"
		: >"$kfd_path"
	fi
	if [[ -n "$topology" ]]; then
		mkdir -p "$case_dir/topology"
		IFS=',' read -ra values <<<"$topology"
		for value in "${values[@]}"; do
			mkdir -p "$case_dir/topology/$node"
			printf 'gfx_target_version %s\n' "$value" >"$case_dir/topology/$node/properties"
			node=$((node + 1))
		done
	fi
	mkdir -p "$case_dir/dri"
	if [[ "$render" == unreadable && "$EUID" -eq 0 ]]; then
		# Root reads a mode 000 file, so this case cannot be staged.
		printf 'skip %s (running as root)\n' "$name"
		return
	fi
	if [[ "$render" == unreadable ]]; then
		: >"$case_dir/dri/renderD128"
		chmod 000 "$case_dir/dri/renderD128"
	fi
	cat >"$case_dir/bin/docker" <<EOF
#!/usr/bin/env bash
if [[ "\$1" == --version ]]; then printf 'Docker version 28.0.0, build test\\n'; exit 0; fi
if [[ "\$1" == compose ]]; then printf '2.30.0\\n'; exit 0; fi
if [[ "\$1" == volume && "\$2" == inspect ]]; then exit 1; fi
if [[ "\$1" == run && '$docker' == fail ]]; then exit 1; fi
exit 0
EOF
	chmod +x "$case_dir/bin/docker"
	case "$toolkit" in
		ctk) toolkit=nvidia-ctk ;;
		runtime) toolkit=nvidia-container-runtime ;;
		cli) toolkit=nvidia-container-cli ;;
		*) toolkit="" ;;
	esac
	if [[ -n "$toolkit" ]]; then
		printf '#!/usr/bin/env bash\nexit 0\n' >"$case_dir/bin/$toolkit"
		chmod +x "$case_dir/bin/$toolkit"
	fi
	# The contributor toolchain passes, so only GPU findings decide the exit code.
	for tool in make node npm; do
		printf '#!/usr/bin/env bash\nprintf "%s ok\\n"\n' "$tool" >"$case_dir/bin/$tool"
		chmod +x "$case_dir/bin/$tool"
	done
	if [[ -z "$mode" ]]; then
		mode=contributor
		[[ "$nvidia" == ok ]] && mode=selfhost
	fi
	local args=()
	[[ "$mode" == contributor ]] && args=(--contributor)
	# Port 9 (discard) is never served here, so the selfhost port check passes.
	output="$(env FAKE_GROUPS="$groups" POTOCOLOM_PROFILE="$profile" FAKE_NVIDIA="$nvidia" POTOCOLOM_OS_RELEASE="$case_dir/os-release" POTOCOLOM_KFD="$kfd_path" POTOCOLOM_KFD_TOPOLOGY="$case_dir/topology" POTOCOLOM_DRI="$case_dir/dri" \
		POTOCOLOM_PCI_DEVICES="$case_dir/pci" POTOCOLOM_PORT=9 TMPDIR="$case_dir" PATH="$case_dir/bin" \
		bash "$case_dir/repo/scripts/preflight.sh" "${args[@]}" 2>&1)"
	code=$?
	if [[ "$output" == *"$expected"* && ( -z "$absent" || "$output" != *"$absent"* ) && ( "$rc" == any || "$code" -eq "$rc" ) ]]; then
		printf 'ok %s\n' "$name"
	else
		fail_case "$name" "exit=$code\n$output"
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
if [[ "$1" == volume && "$2" == inspect ]]; then exit 1; fi
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
expect_profile profile-no-gpu-default nvidia-fail absent smoke
expect_profile profile-override-rocm ok present rocm rocm
expect_profile profile-override-api ok present api api
expect_profile profile-override-invalid ok present error cuda
expect_preflight_gpu preflight-api-only-command 'API and database only' profile=api mode=selfhost absent='NVIDIA Container Toolkit missing' rc=0
expect_preflight_gpu preflight-unusable-nvidia 'NVIDIA GPU found at 0000:01:00.0' rc=0
expect_preflight_gpu preflight-rocm-unusable-nvidia 'NVIDIA GPU found at 0000:01:00.0' kfd=present
expect_preflight_gpu preflight-rocm-override-without-kfd 'POTOCOLOM_PROFILE=rocm but /dev/kfd is missing' vendor=0x8086 profile=rocm rc=1
expect_preflight_gpu preflight-amd-without-kfd 'AMD GPU found at 0000:01:00.0' vendor=0x1002
expect_preflight_gpu amd-supported-dgpu 'gfx1102 supported by the ROCm worker' vendor=0x1002 kfd=present topology=110002 rc=0
expect_preflight_gpu amd-supported-plus-unsupported-igpu 'gfx1036 is not supported by the ROCm worker' vendor=0x1002 kfd=present topology=110002,100306 rc=0
expect_preflight_gpu amd-unsupported-only 'no AMD GPU supported by the ROCm worker' vendor=0x1002 kfd=present topology=100306 rc=1
expect_preflight_gpu amd-gfx90a-encoding 'gfx90a supported by the ROCm worker' vendor=0x1002 kfd=present topology=90010 rc=0
expect_preflight_gpu amd-video-without-render 'not in the '"'"'render'"'"' group and cannot read' vendor=0x1002 kfd=present topology=110002 groups=video render=unreadable rc=0
expect_preflight_gpu amd-debian-firmware 'sudo apt-get update && sudo apt-get install -y firmware-amd-graphics' vendor=0x1002 id=debian
expect_preflight_gpu amd-fedora-firmware 'sudo dnf install -y amd-gpu-firmware' vendor=0x1002 id=fedora
expect_preflight_gpu preflight-unsupported-pci 'no NVIDIA or AMD compute GPU detected' vendor=0x8086

expect_preflight_gpu distro-ubuntu 'sudo apt-get install -y ubuntu-drivers-common && sudo ubuntu-drivers install' id=ubuntu rc=0
expect_preflight_gpu distro-linuxmint 'sudo ubuntu-drivers install' id=linuxmint id_like='ubuntu debian'
expect_preflight_gpu distro-debian 'sudo apt-get install -y linux-headers-$(dpkg --print-architecture) nvidia-driver firmware-misc-nonfree' id=debian
expect_preflight_gpu distro-crlf-release 'nvidia-driver firmware-misc-nonfree' id=debian crlf=yes
expect_preflight_gpu distro-debian-toolkit-repo 'sudo gpg --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg' id=debian
expect_preflight_gpu distro-apt-prerequisites 'sudo apt-get install -y ca-certificates curl gnupg2' id=debian
expect_preflight_gpu distro-fedora-driver 'sudo dnf install -y akmod-nvidia xorg-x11-drv-nvidia-cuda' id=fedora
expect_preflight_gpu distro-fedora 'sudo dnf install -y nvidia-container-toolkit' id=fedora
expect_preflight_gpu distro-rocky 'https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/' id=rocky id_like='rhel centos fedora'
expect_preflight_gpu distro-rocky-toolkit 'sudo dnf install -y nvidia-container-toolkit' id=rocky id_like='rhel centos fedora'
expect_preflight_gpu distro-opensuse 'sudo zypper --gpg-auto-import-keys install -y nvidia-container-toolkit' id=opensuse-tumbleweed id_like='opensuse suse'
expect_preflight_gpu distro-arch 'https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html' id=arch
expect_preflight_gpu distro-missing-release 'https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html' id=missing
expect_preflight_gpu laptop-toolkit-missing 'sudo apt-get install -y nvidia-container-toolkit' vendor=0x8086 id=debian nvidia=ok docker=fail rc=1 absent='toolkit installed but passthrough is broken'
expect_preflight_gpu toolkit-present-passthrough-fails 'toolkit installed but passthrough is broken' vendor=0x8086 nvidia=ok docker=fail toolkit=ctk rc=1
expect_preflight_gpu toolkit-runtime-passthrough-fails 'toolkit installed but passthrough is broken' vendor=0x8086 nvidia=ok docker=fail toolkit=runtime rc=1
expect_preflight_gpu contributor-toolkit-missing-warns 'warn NVIDIA Container Toolkit missing' vendor=0x8086 nvidia=ok mode=contributor rc=0
expect_preflight_gpu toolkit-cli-passthrough-fails 'toolkit installed but passthrough is broken' vendor=0x8086 nvidia=ok docker=fail toolkit=cli rc=1
expect_preflight_gpu selfhost-loopback-public-url 'the studio only works from this machine' nvidia=ok toolkit=ctk mode=selfhost public_url=http://localhost:8080 rc=0
expect_preflight_gpu contributor-skips-loopback-public-url 'Host ready for make init.' mode=contributor public_url=http://localhost:8080 absent='the studio only works from this machine' rc=0

expect_toolchain toolchain-old-node v20.0.0 true 'Node.js 24+ is required' 1
expect_toolchain toolchain-missing-npm v24.0.0 false 'npm not installed' 1
expect_toolchain toolchain-all-present v24.0.0 true 'ok node v24.0.0' 0

new_case preflight-unknown-argument
mkdir -p "$case_dir/repo/scripts"
cp "$repo/scripts/preflight.sh" "$repo/scripts/install-lock.sh" "$case_dir/repo/scripts/"
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
