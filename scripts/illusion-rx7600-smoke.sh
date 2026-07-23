#!/usr/bin/env bash
# RX 7600 smoke checks for the illusion reliability path.
# Usage: scripts/illusion-rx7600-smoke.sh [--force]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/worker/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
	PY="/home/leon/Nextcloud/ETSIIT/ETSHIT/Github/potocolom/worker/.venv/bin/python"
fi
FORCE_ARGS=()
if [[ "${1:-}" == "--force" ]]; then
	FORCE_ARGS=(--force)
	shift
fi

OUT="${ROOT}/out/illusion-smoke-$$"
mkdir -p "$OUT"

# Prefer cached snapshots when Hub access is blocked by a sandbox proxy.
SD15_SNAP="${HOME}/.cache/huggingface/hub/models--stable-diffusion-v1-5--stable-diffusion-v1-5/snapshots"
LCM_SNAP="${HOME}/.cache/huggingface/hub/models--lykon--dreamshaper-8-lcm/snapshots"
MODEL_ARGS=()
if [[ -d "$SD15_SNAP" ]]; then
	MODEL_ARGS+=(--model "$(ls -d "$SD15_SNAP"/*/ | head -1 | sed 's:/*$::')")
fi
if [[ -d "$LCM_SNAP" ]]; then
	MODEL_ARGS+=(--dream-model "$(ls -d "$LCM_SNAP"/*/ | head -1 | sed 's:/*$::')")
fi

run_smoke() {
	local name="$1"
	shift
	echo "=== smoke: $name ==="
	"$ROOT/scripts/gpu-lock.sh" "${FORCE_ARGS[@]}" -- \
		env TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1 HF_HUB_OFFLINE=1 \
		PYTHONPATH="${ROOT}/worker" \
		"$PY" -m worker.illusions "${MODEL_ARGS[@]}" "$@"
}

run_smoke legacy_flip \
	--type flip \
	--prompt "a dog" --prompt "a sloth" \
	--sds-objective legacy \
	--sds-steps 4 --dream-rounds 1 --dream-steps 4 \
	--seed 2 --out "$OUT/legacy_flip"

run_smoke hidden_microbatch \
	--type hidden \
	--prompt "a" --prompt "b" --prompt "c" --prompt "d" --prompt "e" \
	--sds-steps 2 --dream-rounds 1 --dream-steps 2 \
	--view-batch-size 1 \
	--seed 0 --out "$OUT/hidden"

"$PY" - <<'PY' "$OUT"
import json, sys
from pathlib import Path
out = Path(sys.argv[1])
try:
    import torch
    peak = torch.cuda.max_memory_allocated() / (1024**2) if torch.cuda.is_available() else None
except Exception:
    peak = None
(out / "smoke_summary.json").write_text(json.dumps({"peak_vram_mb_after": peak}, indent=2) + "\n")
print("smoke ok; outputs in", out)
PY
