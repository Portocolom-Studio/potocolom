#!/usr/bin/env bash
# Check this machine against the self-hosting requirements and name the
# compose profile it can actually run. Writes deploy/compose/.env from the
# example when the file is missing (hex secrets), and fills empty
# POSTGRES_PASSWORD / FLEET_SECRET. Never overwrites a non-empty secret, except
# the example's change-me database password before any database uses it.
# Starts nothing and installs nothing.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${POTOCOLOM_PORT:-8080}"
contributor=false
if (($# > 0)); then
  if (($# == 1)) && [[ "$1" == --contributor ]]; then
    contributor=true
  else
    printf 'usage: preflight.sh [--contributor]\n' >&2
    exit 2
  fi
fi

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

if [[ "$contributor" != true ]]; then
  if ! (echo >"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; then
    pass "port ${PORT} free"
  elif curl -sf --max-time 2 "http://localhost:${PORT}/api/v1/health" 2>/dev/null | grep -q '"status"'; then
    # Re-running preflight against a stack that is already up is not a problem.
    pass "port ${PORT} serving potocolom already (stack is up)"
  else
    fail "port ${PORT} is in use by something else; free it or publish the studio elsewhere"
  fi
fi

# ---------------------------------------------------------- toolchain

if [[ "$contributor" == true ]]; then
  head_ "Toolchain"

  make_available=false
  if command -v make >/dev/null 2>&1; then
    pass "make installed"
    make_available=true
  else
    fail "make not installed"
    note "sudo apt install make   # or your package manager"
  fi

  if [[ "$make_available" == true ]]; then
    if out="$(make -s -C "$ROOT" check-python 2>&1)"; then
      python_result="$(printf '%s\n' "$out" | tail -n1)"
      pass "$python_result"
    else
      fail "Python 3.11+ with venv support is required"
      while IFS= read -r line; do
        [[ -n "$line" ]] && note "$line"
      done <<<"$out"
    fi

    if out="$(make -s -C "$ROOT" check-node 2>&1)"; then
      pass "node $(node --version)"
    else
      fail "Node.js 24+ is required"
      while IFS= read -r line; do
        [[ -n "$line" ]] && note "$line"
      done <<<"$out"
    fi
  fi

  if command -v npm >/dev/null 2>&1; then
    pass "npm installed"
  else
    fail "npm not installed (it ships with Node.js)"
  fi
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

profile="$(bash "$ROOT/scripts/gpu-profile.sh")"
vram_mb=0

scan_unusable_gpus() {
  local vendors="$1" pci_devices device class vendor slot found=false
  pci_devices="${POTOCOLOM_PCI_DEVICES:-/sys/bus/pci/devices}"
  for device in "$pci_devices"/*; do
    [[ -d "$device" && -r "$device/class" && -r "$device/vendor" ]] || continue
    IFS= read -r class <"$device/class" || continue
    [[ "$class" == 0x03* ]] || continue
    IFS= read -r vendor <"$device/vendor" || continue
    [[ " $vendors " == *" $vendor "* ]] || continue
    slot="${device##*/}"
    if [[ "$vendor" == 0x10de ]]; then
      warn "NVIDIA GPU found at ${slot} but no working NVIDIA driver"
      note "install the NVIDIA driver, reboot, then check that nvidia-smi lists the GPU:"
      note "https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/"
      note "then the NVIDIA Container Toolkit for Docker: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
    elif [[ "$vendor" == 0x1002 ]]; then
      warn "AMD GPU found at ${slot} but /dev/kfd is missing (amdgpu compute not available)"
      note "load the amdgpu kernel driver; see docs/self-hosting.md"
    fi
    found=true
  done
  [[ "$found" == true ]]
}

if [[ "$profile" == rocm ]]; then
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
  if [[ "$(id -u)" -eq 0 ]]; then
    pass "running as root"
  elif [[ "$groups_have" == *video* ]]; then
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
  scan_unusable_gpus "0x10de" || true
  if [[ -r /proc/driver/nvidia/version ]] || command -v nvidia-smi >/dev/null 2>&1; then
    warn "NVIDIA driver files present but nvidia-smi did not report a GPU"
    note "this host uses the AMD worker; the NVIDIA card stays unused until nvidia-smi lists it"
  fi

elif [[ "$profile" == gpu ]]; then
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

  # The native worker of make init/make dev runs without the toolkit; only
  # the Docker gpu profile needs it, so a contributor gets a warning.
  container_gpu=fail
  [[ "$contributor" == true ]] && container_gpu=warn
  if command -v nvidia-ctk >/dev/null 2>&1; then
    pass "NVIDIA Container Toolkit installed"
  else
    "$container_gpu" "NVIDIA Container Toolkit missing; the gpu profile cannot pass the GPU through"
    note "https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
  fi

  # The only check that proves the whole chain works, so it is worth the
  # container pull. Skip with POTOCOLOM_SKIP_GPU_TEST=1.
  if [[ "$docker_ok" == true && "${POTOCOLOM_SKIP_GPU_TEST:-0}" != "1" ]]; then
    if docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L >/dev/null 2>&1; then
      pass "docker can reach the GPU (--gpus all works)"
    else
      "$container_gpu" "docker cannot reach the GPU; toolkit installed but passthrough is broken"
      note "sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
    fi
  fi
else
  if [[ -r /proc/driver/nvidia/version ]] || command -v nvidia-smi >/dev/null 2>&1; then
    warn "NVIDIA driver files present but nvidia-smi did not report a GPU"
    note "the gpu profile needs a usable GPU; this host will fall through to the simulated worker"
  fi
  if ! scan_unusable_gpus "0x10de 0x1002"; then
    warn "no NVIDIA or AMD compute GPU detected"
    note "the full stack still runs against the simulated worker: scripts/compose-smoke.sh"
  fi
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

# Create .env when missing, and fill empty POSTGRES_PASSWORD / FLEET_SECRET.
# A leftover .env with no FLEET_SECRET (or an empty one) used to be left
# alone, then the API refused to start.
if env_out="$(bash "$ROOT/scripts/ensure-env.sh" 2>&1)"; then
  pass "deploy/compose/.env ready"
  while IFS= read -r line; do
    [[ -n "$line" ]] && note "$line"
  done <<<"$env_out"
else
  fail "$env_out"
fi

# ------------------------------------------------------------- verdict

head_ "Verdict"

if ((fails > 0)); then
  printf '  %s%d blocking problem(s)%s above. Fix them, then run this again.\n' "$R" "$fails" "$N"
  exit 1
fi

if [[ "$contributor" == true ]]; then
  ((warns > 0)) && printf '  %d warning(s).\n' "$warns"
else
  ((warns > 0)) && printf '  %d warning(s); the stack will start.\n' "$warns"
fi

if [[ "$contributor" == true ]]; then
  printf '  Host ready for make init.\n'
  exit 0
fi

# A source checkout has the Dockerfiles and builds the images; a release
# install only has the compose file, so it pulls what the image: tags name
# and never builds.
up_flags="--no-build"
if [[ -d "$ROOT/deploy/docker" ]]; then
  up_flags="--build"
fi

case "$profile" in
  gpu)   printf '  Ready. NVIDIA worker:\n\n    docker compose -f deploy/compose/compose.yml --profile gpu up -d %s\n\n  Then open http://localhost:%s\n' "$up_flags" "$PORT" ;;
  rocm)  printf '  Ready. AMD worker:\n\n    docker compose -f deploy/compose/compose.yml --profile rocm up -d %s\n\n  Then open http://localhost:%s\n' "$up_flags" "$PORT" ;;
  smoke) printf '  Ready for the simulated worker (no GPU inference):\n\n    scripts/compose-smoke.sh\n' ;;
esac
