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

Progress snapshot (what already passed on the reliability branch):
[illusion-reliability-status.md](illusion-reliability-status.md).

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

## Provisional evidence

The `d2e6c52` runs AND every `out/illusion-experiments-v2/` result are
provisional. They stand only until legacy VAE RNG parity is proven against
the reference PR #118 path; do not cite them as acceptance evidence until
that parity check passes. All new evidence goes under a fresh tree,
`.local/illusion-experiments-v3`, and is never mixed with the provisional
trees above.

Control chronology (journal, events, audits) lives under
`.local/illusion-reliability/`. Both directories are gitignored.

## Prompt corpus

Each pair in `worker.illusion_experiment.FINAL_PAIRS` (a `PromptPair`) carries
both style-free `subject_a` / `subject_b` and the exact legacy oil prompts
`prompt_a` / `prompt_b`; `SCREEN_PAIRS` is the four-pair funnel. Style
`none`/`oil` uses the exact oil prompts verbatim (never re-wrapped);
`coherent_oil` is a distinct template. Other styles wrap each subject with
`apply_style_template` exactly once. Each manifest records `subjects`,
`effective_prompts`, and `style_requested`.

```bash
$PY -m worker.illusion_experiment run --pair-id dog_sloth --seed 2 \
  --out .local/illusion-experiments-v3/run
```

## Objectives and schedules

- `legacy` (default): CFG residual; do not change until acceptance.
- `weighted_sds`, `csd`, `nfsd`: ablation objectives. Canonical `csd` is
  `w(t)*(cond-uncond)` and rejects `--sds-guidance`.
- `--sqrt-timestep-anneal`: square-root SDS timestep schedule only (not full
  HiFA). `--hifa-schedule` remains a deprecated alias.

## Campaign

```bash
$PY -m worker.illusion_campaign plan --phase wave1 \
  --out .local/illusion-reliability/campaigns/wave1/plan.json \
  --evidence-root .local/illusion-experiments-v3
scripts/run-illusion-campaign-tmux.sh \
  .local/illusion-reliability/campaigns/wave1/plan.json
```

Phases: `wave1` | `wave2` | `away`. Resume validates plan SHA, HEAD, and
image hashes. GPU lock waits for idle and returns exit 75 when temporarily
busy. CLIP scoring writes `clip_scores.json` sidecars and does not mutate
raw optimizer manifests by default.

Default `IllusionConfig` is legacy-equivalent: empty `checkpoint_steps`,
`collect_diagnostics=False`, VAE slicing and `channels_last` off.

Instrumented runs (harness `--collect-diagnostics`) record phase-qualified
checkpoints `sds_0060`, `sds_0125`, `sds_0250`, `sds_0500`, and `final`
(never reuse 500 for final). Campaign attempts use `attempt_001/` with
sibling `driver/attempt_001/` status and logs. Resume skips only completed
manifests that match plan/HEAD/spec and both image hashes.

Post-hoc CLIP scoring (ViT-L/14, pinned revision) after `--skip-clip` GPU
runs writes `clip_scores.json` sidecars by default and does not rewrite raw
optimizer manifests. `score-tree` loads CLIP once and caches text embeddings:

```bash
$PY -m worker.illusion_experiment score-run .local/illusion-experiments-v3/run
$PY -m worker.illusion_experiment score-run --run .local/illusion-experiments-v3/run
$PY -m worker.illusion_experiment score-tree .local/illusion-experiments-v3/screen
$PY -m worker.illusion_experiment score-tree --root .local/illusion-experiments-v3/screen
```

## Screening and final matrix

Use the fresh tree `.local/illusion-experiments-v3`. Do not mix with the
provisional `d2e6c52` or `out/illusion-experiments-v2` runs.

```bash
scripts/run-illusion-screening.sh
$PY -m worker.illusion_experiment stage2-plan --profile '...'
$PY -m worker.illusion_experiment final-plan --finalist '...'
```

## Blind ratings and gate

```bash
$PY -m worker.illusion_experiment build-matched-blind \
  --final-root .local/illusion-experiments-v3/final \
  --out .local/illusion-experiments-v3/blind-review
# Rate case sheets; freeze ratings.jsonl; then:
$PY -m worker.illusion_experiment evaluate-ratings \
  --ratings .local/illusion-experiments-v3/blind-review/ratings.jsonl \
  --answer-key .local/illusion-experiments-v3/blind-review/answer_key.json \
  --final-root .local/illusion-experiments-v3/final
```

`evaluate-ratings` is strict: it fails (and lists every failure explicitly)
unless there are exactly 24 unique rated case_ids matching the answer key,
every `keep_a`/`keep_b` is a real boolean, all 48 baseline+finalist run dirs
have `status=completed` manifests with matching `pair_id`/`seed`/`git_sha`/
`spec` (where the answer key provides them), and each run records
`phase_timings.total_s`, ran under 60 minutes, and did not OOM. Missing
ratings are never silently skipped. Accept new defaults only when all hold:

- at least 16/24 finalist keepers
- at least 6/8 pairs with 2+ keepers from three seeds
- elephant/swan and moose/butterfly each at least 2/3
- at least four more baseline-to-finalist conversions than regressions
- every run under 60 minutes without OOM

## Calibration

```bash
$PY -m worker.illusion_experiment calibrate labelled.json
$PY -m worker.illusion_experiment calibrate --corpus labelled.json
```

Reports ROC-AUC (average ranks for ties; perfect high-score-is-keep gives
1.0), a hit-rate at CLIP pair-score threshold 0, and checkpoint-to-final
Spearman. Automated style/seed selection stays off unless Spearman >= 0.6 and
hit-rate >= 75%; CLIP in-loop balancing stays off unless ROC-AUC >= 0.75.
