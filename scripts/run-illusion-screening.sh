#!/usr/bin/env bash
# Screening funnel for out/illusion-experiments-v2 (never mixes with provisional d2e6c52 runs).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="$("$ROOT/scripts/worker-python.sh")"
export PYTHONPATH="${ROOT}/worker"
export TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
OUT_ROOT="${ROOT}/out/illusion-experiments-v2"
mkdir -p "$OUT_ROOT"
FORCE_ARGS=()
if [[ "${1:-}" == "--force" ]]; then
	FORCE_ARGS=(--force)
fi

SD15_SNAP="${HOME}/.cache/huggingface/hub/models--stable-diffusion-v1-5--stable-diffusion-v1-5/snapshots"
LCM_SNAP="${HOME}/.cache/huggingface/hub/models--lykon--dreamshaper-8-lcm/snapshots"
MODEL_FLAGS=()
if [[ -d "$SD15_SNAP" ]]; then
	MODEL_FLAGS+=(--model "$(ls -d "$SD15_SNAP"/*/ | head -1 | sed 's:/*$::')")
fi
if [[ -d "$LCM_SNAP" ]]; then
	MODEL_FLAGS+=(--dream-model "$(ls -d "$LCM_SNAP"/*/ | head -1 | sed 's:/*$::')")
fi

run_locked() {
	"$ROOT/scripts/gpu-lock.sh" "${FORCE_ARGS[@]}" -- \
		env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u ALL_PROXY -u all_proxy \
		"$@"
}

echo "=== variance: dog/sloth seed 2 x3 ==="
for repeat in 0 1 2; do
	out="$OUT_ROOT/variance_dog_sloth_seed2/repeat_${repeat}"
	run_locked "$PY" -m worker.illusion_experiment run \
		--name "variance_r${repeat}" --type flip --pair-id dog_sloth \
		"${MODEL_FLAGS[@]}" \
		--seed 2 --sds-objective legacy --skip-clip --out "$out"
done

echo "=== seed-2 screen (oil corpus) ==="
declare -a PROFILES=(
	"01_legacy|--sds-objective legacy"
	"02_weighted_sds|--sds-objective weighted_sds"
	"03_dream_lr_3e-3|--sds-objective legacy --dream-lr 3e-3"
	"04_dream_lr_1e-2|--sds-objective legacy --dream-lr 1e-2"
	"05_dream_sd15|--sds-objective legacy --dream-model none"
	"06_csd_cfg7.5|--sds-objective csd --sds-guidance 7.5"
	"07_csd_cfg20|--sds-objective csd --sds-guidance 20"
	"08_nfsd_cfg7.5|--sds-objective nfsd --sds-guidance 7.5"
)
declare -a PAIR_IDS=(dog_sloth fox_rabbit walrus_ladybug mountain_valley)

for pair_id in "${PAIR_IDS[@]}"; do
	for profile_entry in "${PROFILES[@]}"; do
		IFS='|' read -r name flags <<<"$profile_entry"
		out="$OUT_ROOT/screen/${name}/${pair_id}"
		echo "RUN $name $pair_id"
		# shellcheck disable=SC2086
		run_locked "$PY" -m worker.illusion_experiment run \
			--name "${name}_${pair_id}" --type flip --pair-id "$pair_id" \
			"${MODEL_FLAGS[@]}" \
			--seed 2 --skip-clip $flags --out "$out"
	done
done

echo "Post-score with: $PY -m worker.illusion_experiment score-tree --root $OUT_ROOT/screen"
echo "Do not mix results with out/illusion-experiments/ (provisional d2e6c52)."
