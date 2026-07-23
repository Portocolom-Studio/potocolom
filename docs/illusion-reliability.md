# Diffusion Illusion Reliability Protocol

How to run the flip-quality reliability program for the worker illusion
optimizer. Blind human judgment is the acceptance oracle. CLIP pair scores,
loss curves, and gradient conflict stats are diagnostic until calibrated.

## Scope

- Target hardware: 16 GB RX 7600 XT (ROCm).
- Flip quality is the release gate. Rotation and hidden receive correctness
  and memory smokes only.
- DeepFloyd / Visual Anagrams remain research-only (gated non-commercial).
- Defaults stay `sds_objective=legacy` until the acceptance gate passes.

## Environment

```bash
# From the repository root (or a dedicated worktree of the reliability branch)
export PYTHONPATH=worker
export TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1
# Optional: export POTOCOLOM_WORKER_PYTHON=/path/to/worker/.venv/bin/python
PY=$(scripts/worker-python.sh)
```

Wrap every GPU experiment:

```bash
scripts/gpu-lock.sh -- $PY -m worker.illusion_experiment run ...
```

The lock aborts if ROCm use is high, KFD compute holders are present, or a
self-hosted `Runner.Worker` is active.

## Prompt corpus

Use the exact oil-painting pairs in `worker.illusion_experiment.FINAL_PAIRS`
(and `SCREEN_PAIRS` for the four-pair funnel). Example:

```bash
$PY -m worker.illusion_experiment run --pair-id dog_sloth --seed 2 --out out/run
```

## Instrumentation

Default `IllusionConfig` is legacy-equivalent: empty `checkpoint_steps`,
`collect_diagnostics=False`, VAE slicing and `channels_last` off.

Instrumented runs (harness `--collect-diagnostics`) record phase-qualified
checkpoints `sds_0060`, `sds_0125`, `sds_0250`, `sds_0500`, and `final`
(never reuse 500 for final). Manifests are atomic with
`status=running|completed|failed`. Resume skips only `status=completed`
runs that already have final derived images; incomplete dirs are preserved
and new attempts use `*_attempt_N`.

Post-hoc CLIP scoring (ViT-L/14) after `--skip-clip` GPU runs:

```bash
$PY -m worker.illusion_experiment score-run --run out/run
$PY -m worker.illusion_experiment score-tree --root out/screen
```

## Screening and final matrix

Use a fresh tree such as `out/illusion-experiments-v2`. Do not mix with
provisional runs from earlier SHAs.

```bash
scripts/run-illusion-screening.sh
$PY -m worker.illusion_experiment stage2-plan --best-flags '...'
$PY -m worker.illusion_experiment final-plan --finalist '...'
```

## Blind ratings and gate

```bash
$PY -m worker.illusion_experiment build-matched-blind \
  --legacy-root out/.../legacy --finalist-root out/.../finalist \
  --out out/blind-review
# Rate case sheets; freeze ratings.jsonl; then:
$PY -m worker.illusion_experiment evaluate-ratings \
  --ratings out/blind-review/ratings.jsonl \
  --answer-key out/blind-review/answer_key.json \
  --final-root out/...
```

Accept new defaults only when all hold:

- at least 16/24 finalist keepers
- at least 6/8 pairs with 2+ keepers from three seeds
- elephant/swan and moose/butterfly each at least 2/3
- at least four more baseline-to-finalist conversions than regressions
- every run under 60 minutes without OOM

## Calibration

```bash
$PY -m worker.illusion_experiment calibrate --corpus labelled.json
```

Reports ROC-AUC and checkpoint-to-final Spearman. Automated style/seed
selection stays off unless Spearman >= 0.6 and hit-rate >= 75%; CLIP
in-loop balancing stays off unless ROC-AUC >= 0.75.
