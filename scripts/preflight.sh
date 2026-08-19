#!/usr/bin/env bash
# Check this machine against the self-hosting requirements and name the
# compose profile it can actually run. Writes deploy/compose/.env from the
# example when the file is missing (hex secrets); never overwrites one that
# already exists. Starts nothing and installs nothing.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${POTOCOLOM_PORT:-8080}"

# Worker images are built on CUDA 12.8; a 12.x driver runs them under CUDA
# minor version compatibility, which needs 525.60.13 or newer.
CUDA_IMAGE_VERSION="12.8"
CUDA_MIN_DRIVER="525.60.13"

if [[ -t 1 ]]; then
  R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[1m'; N=$'\e[0m'
else
  R=""; G=""; Y=""; B=""; N=""
fi

fails=0
warns=0

pass() { printf '  %sok%s %s\n' "$G" "$N" "$1"; }
warn() { printf '  %swarn%s %s\n' "$Y" "$N" "$1"; warns=$((warns + 1)); }
fail() { printf '  %sfail%s %s\n' "$R" "$N" "$1"; fails=$((fails + 1)); }
note() { printf '      %s\n' "$1"; }
head_() { printf '\n%s%s%s\n' "$B" "$1" "$N"; }

# Compare dotted versions: version_ge 535.261.03 525.60.13
version_ge() {
  [[ "$1" == "$2" ]] && return 0
  local first
  first="$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)"
  [[ "$first" == "$2" ]]
}

# ---------------------------------------------------------------- host

head_ "Host"

if [[ "$(uname -s)" == "Linux" ]]; then
  pass "Linux ($(uname -r))"
else
  fail "$(uname -s) is not a supported host; potocolom is developed and released on Linux"
fi

ram_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
ram_gb=$((ram_kb / 1024 / 1024))
if ((ram_gb >= 16)); then
  pass "RAM ${ram_gb} GB"
elif ((ram_gb >= 8)); then
  warn "RAM ${ram_gb} GB (8 GB minimum met, 16 GB comfortable)"
else
  fail "RAM ${ram_gb} GB is below the 8 GB minimum"
fi

disk_gb="$(df -BG --output=avail "$ROOT" 2>/dev/null | tail -n1 | tr -dc '0-9')"
disk_gb="${disk_gb:-0}"
if ((disk_gb >= 50)); then
  pass "Disk ${disk_gb} GB free"
elif ((disk_gb >= 20)); then
  warn "Disk ${disk_gb} GB free (20 GB minimum met; weights are 2-7 GB per model)"
else
  fail "Disk ${disk_gb} GB free is below the 20 GB minimum"
fi

if ! (echo >"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; then
  pass "port ${PORT} free"
elif curl -sf --max-time 2 "http://localhost:${PORT}/api/v1/health" 2>/dev/null | grep -q '"status"'; then
  # Re-running preflight against a stack that is already up is not a problem.
  pass "port ${PORT} serving potocolom already (stack is up)"
else
  fail "port ${PORT} is in use by something else; free it or publish the studio elsewhere"
fi

# -------------------------------------------------------------- docker

head_ "Docker"

docker_ok=false
if ! command -v docker >/dev/null 2>&1; then
  fail "docker not installed - https://docs.docker.com/engine/install/"
else
  pass "docker $(docker --version | awk '{print $3}' | tr -d ,)"

  if docker compose version >/dev/null 2>&1; then
    pass "compose $(docker compose version --short 2>/dev/null)"
  else
    fail "docker compose v2 not available (the v1 'docker-compose' binary will not work)"
  fi

  # Distinguish the three ways the daemon is unreachable: they have
  # completely different fixes and the raw error does not say which.
  docker_err="$(docker info --format '{{.ServerVersion}}' 2>&1 >/dev/null)"
  if [[ -z "$docker_err" ]]; then
    pass "docker daemon reachable"
    docker_ok=true
  elif [[ "$docker_err" == *"permission denied"* ]]; then
    fail "docker daemon refuses this user: not in the 'docker' group"
    note "sudo usermod -aG docker $USER   # then log out and back in"
  elif [[ "$docker_err" == *"Cannot connect"* || "$docker_err" == *"Is the docker daemon running"* ]]; then
    fail "docker daemon not running"
    note "sudo systemctl enable --now docker"
  else
    fail "docker daemon unreachable: ${docker_err}"
  fi
fi

# ----------------------------------------------------------------- gpu

head_ "GPU"

profile=""
vram_mb=0

# AMD first: /dev/kfd is the compute device the ROCm worker needs, and it is
# absent on machines with only a display-capable amdgpu.
if [[ -e /dev/kfd ]]; then
  pass "AMD compute device /dev/kfd present"
  if [[ -d /dev/dri ]]; then
    pass "/dev/dri present"
  else
    fail "/dev/dri missing; the ROCm worker needs both devices passed through"
  fi
  groups_have=""
  for g in video render; do
    if id -nG | tr ' ' '\n' | grep -qx "$g"; then groups_have="${groups_have} ${g}"; fi
  done
  if [[ "$groups_have" == *video* ]]; then
    pass "user in group(s):${groups_have}"
  else
    fail "user not in the 'video' group"
    note "sudo usermod -aG video,render $USER   # then log out and back in"
  fi
  if command -v rocm-smi >/dev/null 2>&1; then
    vram_mb="$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, 'NR==2 {print int($2/1048576)}')"
    vram_mb="${vram_mb:-0}"
    ((vram_mb > 0)) && pass "VRAM ${vram_mb} MiB"
  else
    warn "rocm-smi not on the host (fine: the container ships ROCm userspace), VRAM unknown"
  fi
  profile="rocm"

# NVIDIA. Deliberately NOT detected with lsmod: on Optimus laptops the dGPU
# sits in D3cold with its modules unloaded until something touches it, so
# lsmod reports nothing on a perfectly working install. /proc/driver/nvidia
# exists regardless, and nvidia-smi wakes the device. Driver files alone do
# not prove CUDA can run, so nvidia-smi must actually enumerate a GPU before
# this branch selects the gpu profile; otherwise the host falls through to
# the simulated worker.
elif [[ -r /proc/driver/nvidia/version ]] || command -v nvidia-smi >/dev/null 2>&1; then
  if command -v nvidia-smi >/dev/null 2>&1 && smi="$(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>&1)" && [[ -n "$smi" ]]; then
    gpu_name="$(echo "$smi" | head -n1 | cut -d, -f1 | xargs)"
    vram_mb="$(echo "$smi" | head -n1 | cut -d, -f2 | tr -dc '0-9')"
    driver_version="$(echo "$smi" | head -n1 | cut -d, -f3 | xargs)"
    pass "${gpu_name}, ${vram_mb} MiB VRAM, driver ${driver_version}"

    if version_ge "$driver_version" "$CUDA_MIN_DRIVER"; then
      driver_cuda="$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version: *\([0-9.]*\).*/\1/p' | head -n1)"
      if [[ -n "$driver_cuda" ]] && ! version_ge "$driver_cuda" "$CUDA_IMAGE_VERSION"; then
        warn "driver supports CUDA ${driver_cuda}; the worker image is built on CUDA ${CUDA_IMAGE_VERSION}"
        note "expected to work under CUDA minor version compatibility (driver >= ${CUDA_MIN_DRIVER})."
        note "if the worker fails on a CUDA error, upgrade the driver before anything else."
      fi
    else
      fail "driver ${driver_version} is older than ${CUDA_MIN_DRIVER}; the CUDA ${CUDA_IMAGE_VERSION} worker image will not load"
    fi
  else
    warn "NVIDIA driver files present but nvidia-smi did not report a GPU"
    note "the gpu profile needs a usable GPU; this host will fall through to the simulated worker"
  fi

  if command -v nvidia-ctk >/dev/null 2>&1; then
    pass "NVIDIA Container Toolkit installed"
  else
    fail "NVIDIA Container Toolkit missing; the gpu profile cannot pass the GPU through"
    note "https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
  fi

  # The only check that proves the whole chain works, so it is worth the
  # container pull. Skip with POTOCOLOM_SKIP_GPU_TEST=1.
  if [[ "$docker_ok" == true && "${POTOCOLOM_SKIP_GPU_TEST:-0}" != "1" ]]; then
    if docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L >/dev/null 2>&1; then
      pass "docker can reach the GPU (--gpus all works)"
    else
      fail "docker cannot reach the GPU; toolkit installed but passthrough is broken"
      note "sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
    fi
  fi
  # Only claim the gpu profile when nvidia-smi actually enumerated a GPU
  # above. Driver files alone do not prove CUDA can run, so a host with the
  # driver installed but no device falls through to the simulated worker.
  if [[ -n "${vram_mb:-}" && "$vram_mb" -gt 0 ]]; then
    profile="gpu"
  else
    profile="smoke"
  fi

else
  warn "no NVIDIA or AMD compute GPU detected"
  note "the full stack still runs against the simulated worker: scripts/compose-smoke.sh"
  profile="smoke"
fi

# ------------------------------------------------------------ capacity

if ((vram_mb > 0)); then
  head_ "Selectable models against ${vram_mb} MiB"

  realtime=(); degraded=()
  for manifest in "$ROOT"/worker/models/*.json; do
    [[ -e "$manifest" ]] || continue
    # benchmark_only manifests never reach the studio's model picker
    # (registry.public()), so they are not capacity a self-hoster can spend.
    grep -q '"benchmark_only"[[:space:]]*:[[:space:]]*true' "$manifest" && continue
    id="$(grep -m1 '"id"' "$manifest" | cut -d'"' -f4)"
    floor="$(grep -m1 '"min_vram_gb"' "$manifest" | tr -dc '0-9')"
    [[ -n "$id" && -n "$floor" ]] || continue
    if ((vram_mb >= floor * 1024)); then
      realtime+=("${id} (${floor} GB)")
    else
      degraded+=("${id} (${floor} GB)")
    fi
  done

  if ((${#realtime[@]} > 0)); then
    pass "full residency, realtime capable: ${realtime[*]}"
  else
    warn "no model reaches full residency on this card"
  fi
  if ((${#degraded[@]} > 0)); then
    warn "below their floor, will load offloaded without the realtime capability:"
    note "${degraded[*]}"
    note "generation still works; the live draw-and-render loop is what you lose."
  fi
fi

# -------------------------------------------------------------- secrets

head_ "Compose secrets"

ENV_FILE="$ROOT/deploy/compose/.env"
ENV_EXAMPLE="$ROOT/deploy/compose/.env.example"

if [[ -e "$ENV_FILE" ]]; then
  pass "deploy/compose/.env already exists (left alone)"
else
  if [[ ! -f "$ENV_EXAMPLE" ]]; then
    fail "deploy/compose/.env.example is missing; cannot write .env"
  elif ! command -v openssl >/dev/null 2>&1; then
    fail "openssl is required to generate POSTGRES_PASSWORD and FLEET_SECRET"
  else
    pg="$(openssl rand -hex 32)"
    fleet="$(openssl rand -hex 32)"
    tmp="$(mktemp)"
    awk -v pg="$pg" -v fleet="$fleet" '
      /^POSTGRES_PASSWORD=/ { print "POSTGRES_PASSWORD=" pg; next }
      /^FLEET_SECRET=/ { print "FLEET_SECRET=" fleet; next }
      { print }
    ' "$ENV_EXAMPLE" > "$tmp"
    mv "$tmp" "$ENV_FILE"
    pass "wrote $ENV_FILE"
    note "FLEET_SECRET=$fleet"
    note "A worker on another machine needs a copy of FLEET_SECRET."
  fi
fi

# ------------------------------------------------------------- verdict

head_ "Verdict"

if ((fails > 0)); then
  printf '  %s%d blocking problem(s)%s above. Fix them, then run this again.\n' "$R" "$fails" "$N"
  exit 1
fi

((warns > 0)) && printf '  %d warning(s); the stack will start.\n' "$warns"

case "$profile" in
  gpu)   printf '  Ready. NVIDIA worker:\n\n    docker compose -f deploy/compose/compose.yml --profile gpu up -d --build\n\n  Then open http://localhost:%s\n' "$PORT" ;;
  rocm)  printf '  Ready. AMD worker:\n\n    docker compose -f deploy/compose/compose.yml --profile rocm up -d --build\n\n  Then open http://localhost:%s\n' "$PORT" ;;
  smoke) printf '  Ready for the simulated worker (no GPU inference):\n\n    scripts/compose-smoke.sh\n' ;;
esac
