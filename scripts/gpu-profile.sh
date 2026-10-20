#!/usr/bin/env bash
set -uo pipefail

# An explicit choice wins, so every caller (preflight, install.sh, make,
# dev-stack.sh) honours the same override on a host with several GPUs.
case "${POTOCOLOM_PROFILE:-}" in
	gpu | rocm | smoke)
		printf '%s\n' "$POTOCOLOM_PROFILE"
		exit 0
		;;
esac

# NVIDIA detection must query the device: D3cold and driver files alone do
# not prove CUDA can run. Check it first because an AMD iGPU also exposes
# /dev/kfd.
if nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | grep '[^[:space:]]' >/dev/null; then
	printf 'gpu\n'
elif [[ -e "${POTOCOLOM_KFD:-/dev/kfd}" ]]; then
	# /dev/kfd is the ROCm compute device; display-only amdgpu does not expose it.
	printf 'rocm\n'
else
	printf 'smoke\n'
fi
