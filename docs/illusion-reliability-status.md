# Illusion reliability - status for next agent

Full protocol: [illusion-reliability.md](illusion-reliability.md).

## Branch

- Worktree: `/tmp/potocolom-illusion-reliability`
- Branch: `illusion-reliability-program`
- Do **not** cherry-pick into PR #118 or change optimizer defaults yet.
- Provisional (exclude from acceptance): `d2e6c52` tree `out/illusion-experiments/`
  AND all `out/illusion-experiments-v2/` results (VAE sampling used the SDS
  generator until the post-`1de46b4` corrective commits).
- Durable evidence root: `.local/illusion-experiments-v3` (never `/tmp`).

## Corrective work after `1de46b4` (this round)

- Restore 5f30fdd legacy VAE sampling: `posterior.sample()` on the global RNG;
  `posterior_eps` only for opt-in microbatch comparison.
- Typed `PhaseEvent` observer: `sds_begin/end`, SDS checkpoints 60/125/250/500,
  dream rounds 1/4/8 (targets + views), `dream_begin/end`, `final`.
- Corpus `PromptPair` subjects + exact oil prompts; styles applied once.
- Scoring: `--root`/`positional`, CLIP loaded once per tree, merge-by-phase,
  fixed ROC-AUC (perfect -> 1.0), strict 24-case ratings gate.
- Campaign planner/runner (`worker.illusion_campaign`), GPU lock flock-before-
  preflight + correct `rocm-smi` parse, screening script continues after
  failures with 65m timeouts into `.local/illusion-experiments-v3`.

## Verified so far (CPU)

- Worker suite green after these fixes (95 tests at last run).
- Campaign dry-run: wave1=24, wave2=16, away<=184, unique spec hashes.

## Still required before pilot departure

1. Three-way GPU equivalence at 60 SDS / 0 Dream rounds:
   `5f30fdd` legacy vs corrected default vs corrected instrumented (step-60).
   Digests must match. Until this passes, do not trust any GPU evidence.
2. Hidden microbatch smoke under 16 GB on the corrected SHA.
3. Offline-cache CLIP ViT-L/14 + diffusion snapshots.
4. Dummy fail/timeout/interrupt resume simulation for the campaign runner.
5. Then Wave 1 (24 runs) under `.local/illusion-experiments-v3` with blind
   sheets; Wave 2; only later the 52-hour away campaign.

## Authorship

`leonfullxr` / DCO `-s` / no Co-authored-by.
