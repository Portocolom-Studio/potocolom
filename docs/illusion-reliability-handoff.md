# Illusion reliability program - handoff (branch illusion-reliability-program)

Worktree: `/tmp/potocolom-illusion-reliability`
Base: `origin/issue-115-illusion-optimizer` @ `5f30fdd`
Branch: `illusion-reliability-program` (do not push the shared PR branch from this worktree)

## What landed

- `scripts/gpu-lock.sh` - cooperative flock + rocm-smi / KFD / Runner.Worker preflight
- `worker/worker/illusions.py` - `sds_objective` (legacy|weighted_sds|csd|nfsd), split `sds_lr`/`dream_lr`
  with `--learning-rate` alias, HiFA schedule flag, round-robin, style templates, CLIP-margin
  warning, VAE slicing, channels_last, view microbatching, FFN/SSIM caches, checkpoints hook
- `worker/worker/illusion_experiment.py` - manifests, SDS checkpoints, CLIP margins, blind sheets,
  variance/screen/final command plans
- `scripts/run-illusion-screening.sh`, `scripts/illusion-rx7600-smoke.sh`
- Unit tests: `worker/tests/test_illusions_objectives.py` (+ FakeScheduler alphas in existing tests)
- Docs: reliability protocol section in `docs/illusions.md`

## Defaults

Still `legacy` SDS + LCM Dream Targets. **Do not flip defaults** until the blind acceptance
gate passes (16/24 keepers, 6/8 pairs with 2/3, elephant/swan and moose/butterfly 2/3, net
conversion, <60 min / no OOM).

## Experiments

Variance dog/sloth seed-2 x3 and the seed-2 screen funnel are started via:

```bash
cd /tmp/potocolom-illusion-reliability
./scripts/run-illusion-screening.sh
```

Logs and manifests under `out/illusion-experiments/` (gitignored). After screening, build a
blind sheet with `python -m worker.illusion_experiment blind-sheet`, freeze ratings, then run
`final-plan` for the 24-case matrix.

## Integration into PR #118

1. Review commits on `illusion-reliability-program`
2. Cherry-pick or merge into `issue-115-illusion-optimizer` yourself (sign-off already present)
3. Push the PR branch and request Copilot review
4. Keep DeepFloyd / Visual Anagrams research-only
