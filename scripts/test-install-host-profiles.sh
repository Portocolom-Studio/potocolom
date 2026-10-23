#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
failures=0

fail_case() { printf 'FAIL %s\n' "$1"; failures=$((failures + 1)); }

profile() {
	local name="$1" expected="$2" override="${3:-}" actual rc=0
	actual="$(env POTOCOLOM_PROFILE="$override" POTOCOLOM_KFD="$tmp/no-kfd" PATH="$tmp/bin:$PATH" bash "$repo/scripts/gpu-profile.sh" 2>&1)" || rc=$?
	if [[ "$expected" == error ]]; then
		if ((rc != 0)) && [[ "$actual" == *'POTOCOLOM_PROFILE must be'* ]]; then printf 'ok %s\n' "$name"; else fail_case "$name"; fi
	elif [[ "$actual" == "$expected" && "$rc" -eq 0 ]]; then printf 'ok %s\n' "$name"; else fail_case "$name"; fi
}

mkdir -p "$tmp/bin"
cat >"$tmp/bin/nvidia-smi" <<'EOF'
#!/usr/bin/env bash
[[ "${FAKE_NVIDIA:-0}" == 1 ]] && printf 'GPU\n' && exit 0
exit 1
EOF
chmod +x "$tmp/bin/nvidia-smi"

profile explicit-api api api
profile invalid-profile error potato
actual="$(FAKE_NVIDIA=0 env -u POTOCOLOM_PROFILE POTOCOLOM_KFD="$tmp/no-kfd" PATH="$tmp/bin:$PATH" bash "$repo/scripts/gpu-profile.sh")"
if [[ "$actual" == smoke ]]; then printf 'ok source-auto-no-gpu-smoke\n'; else fail_case source-auto-no-gpu-smoke; fi
actual="$(FAKE_NVIDIA=1 env POTOCOLOM_PROFILE=api POTOCOLOM_KFD="$tmp/no-kfd" PATH="$tmp/bin:$PATH" bash "$repo/scripts/gpu-profile.sh")"
if [[ "$actual" == api ]]; then printf 'ok api-on-gpu-host\n'; else fail_case api-on-gpu-host; fi

env_file="$tmp/.env"
cp "$repo/deploy/compose/.env.example" "$env_file"
set +e
unset CUDA_VISIBLE_DEVICES HIP_VISIBLE_DEVICES INTERNAL_URL
config="$(docker compose --env-file "$env_file" -f "$repo/deploy/compose/compose.yml" --profile gpu config 2>&1)"
rc=$?
set -e
if [[ "$rc" -eq 0 && "$config" != *'CUDA_VISIBLE_DEVICES: ""'* && "$config" != *'HIP_VISIBLE_DEVICES: ""'* ]]; then printf 'ok absent-device-masks\n'; else fail_case absent-device-masks; fi

printf 'HIP_VISIBLE_DEVICES=3\nINTERNAL_URL=https://dotenv-api.example.test\n' >>"$env_file"
CUDA_VISIBLE_DEVICES=2 INTERNAL_URL=https://api.example.test docker compose --env-file "$env_file" -f "$repo/deploy/compose/compose.yml" --profile gpu config >"$tmp/shell-config" 2>&1
docker compose --env-file "$env_file" -f "$repo/deploy/compose/compose.yml" --profile rocm config >"$tmp/dotenv-rocm-config" 2>&1
if rg -q 'CUDA_VISIBLE_DEVICES: "?2"?' "$tmp/shell-config" && rg -q 'HIP_VISIBLE_DEVICES: "?3"?' "$tmp/dotenv-rocm-config"; then printf 'ok shell-and-dotenv-device-masks\n'; else fail_case shell-and-dotenv-device-masks; fi
if rg -q 'INTERNAL_URL: https://api.example.test' "$tmp/shell-config"; then printf 'ok shell-internal-url-reaches-api-settings\n'; else fail_case shell-internal-url-reaches-api-settings; fi
docker compose --env-file "$env_file" -f "$repo/deploy/compose/compose.yml" --profile gpu config >"$tmp/dotenv-config" 2>&1
if rg -q 'INTERNAL_URL: https://dotenv-api.example.test' "$tmp/dotenv-config"; then printf 'ok dotenv-internal-url-reaches-api-settings\n'; else fail_case dotenv-internal-url-reaches-api-settings; fi

if ((failures)); then printf '%d host-profile check(s) failed\n' "$failures"; exit 1; fi
