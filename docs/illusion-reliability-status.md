# Illusion reliability - status for next agent

Full protocol: [illusion-reliability.md](illusion-reliability.md).

## Branch

- Branch: `illusion-reliability-program` (worktree may be used for GPU work)
- Do **not** cherry-pick into PR #118 or change optimizer defaults yet.
- Authorship: `leonfullxr` / DCO `-s` / no Co-authored-by.

## Durable local layout (gitignored)

All machine evidence lives under the primary checkout:

```text
.local/illusion-experiments-v3/     # Wave 1 evidence + retries (canonical)
.local/illusion-reliability/        # control record
  JOURNAL.md
  events.jsonl
  current.json
  audits/
    wave1-f03-audit.json
    evidence-sha256.json
  campaigns/<id>/
```

Never store campaign evidence under `/tmp`. Temporary worktree copies may exist
for recovery but the primary `.local/` tree is canonical after hash verify.

## Wave 1 record (frozen at f03bbf5 evidence)

| Class | Count | Notes |
|-------|------:|-------|
| Completed raw | 21 | Retain all files |
| Admit clean pilot (non-CSD) | 18 | Under `wave1/*/seed_2_attempt_1` |
| f03 retries | 2 | `wave1_retries/legacy/dog_sloth`, `wave1_retries/dream_lr_3e3/fox_rabbit` |
| Quarantine CSD | 3 | `csd_scaled_7_5_noncanonical` - rerun canonical CSD |
| Failed preflight | 3 | Residual GPU use; no optimizer entry. CSD walrus not retried |

Clean 24-run Wave 1 index target: 18 + 2 retries + 4 canonical CSD (after semantics).

## Corrective commits after f03bbf5

Inspect `git log f03bbf5..HEAD` on this branch for:

1. Canonical CSD / sqrt anneal / coherent_oil / microbatch telemetry
2. Provenance-safe campaign resume (attempt/driver layout)
3. Detached tmux campaign launcher
4. GPU idle wait + exit 75 temporary-busy; CLIP sidecars; answer-key identity

## Verification already done

| Check | Result |
|-------|--------|
| 3-way GPU digests (60 SDS, 0 Dream) | match vs `5f30fdd` (see `.local/.../equiv3/`) |
| Hidden microbatch smoke | ~9417 MB / 16368 MB |
| CLIP ViT-L/14 offline | rev `32bd64288804d66eefd0ccbe215aa642df71cc41` |
| Worker suite | 116 tests green at `7f8d342` |

Full 500-SDS + Dream parity later passed, but Wave 1/2 human review did not
freeze a base B. The author-reference campaign below supersedes that axis.

## Current direction (2026-07-29)

The primary phase is now `window` (154 cells, 30 pairs x 5 seeds),
documented in
[illusion-window.md](illusion-window.md). Its fallback remains
`early-dream-backup` (48 short cells). Product defaults and PR #118 remain
unchanged.

`reference60h` (36 cells) is superseded. It varied six things at once against
the failed runs, so no outcome would have been attributable, and it sampled
five pairs on the axis docs/illusions.md calls the biggest lever while the
curated pairing-rule corpus in issue #138 had never been run at all. Five
measured pre-window arms then changed three of its settings and found a bug
that would have killed every cell at launch. See the window document.

Note the budget claim in the earlier version of this section was wrong: the
calibration smoke saturated by 2,000 steps because it is the paper's own
easiest pair. A3 measured monotone improvement to 5,000 on a pair nobody had
run, so the window raises the budget rather than cutting it.

Its calibration smoke is retained as evidence, and the window's first cell is
a rig check on the same pair at the window's own settings.

### Superseded direction (2026-07-25)

Wave 1/2 and the SDXL pilot did not freeze a keeper profile. The steps below
are retained as historical provenance, superseded first by the
author-reference campaign in
[illusion-reliability-60h.md](illusion-reliability-60h.md) and then by the
window above.

### Author-reference smoke result

The giraffe/penguin seed-11 smoke completed at `7f8d342` without error:

- 10,000 SDS steps plus eight Dream rounds completed in 3416.60 seconds
  (56.9 minutes);
- SDS took 3240.95 seconds and Dream took 173.45 seconds;
- peak allocated VRAM was 4331.11 MB;
- SDS 500/2000/5000/10000, Dream d1/d4/d8, and final checkpoints are present;
- the human selected the SDS-10000 prime and final prime as the two best
  artifacts.

The smoke is a positive stage-specific result: SDS-10000 has the strongest raw
dual-subject structure, while final has the cleaner presentation. This does
not promote a product default. The remaining 35 cells are paused and require
an explicit launch instruction. At the measured smoke rate they would take
about 33.2 GPU-hours; retain the original 51.3-hour remaining estimate as the
conservative budget.

The immutable primary and backup plans remain pinned to implementation commit
`7f8d342`. Run them from a worktree at that exact commit. Later
documentation-only commits must not be substituted for the plan's recorded
Git SHA.

## Historical next (superseded)

1. Generate four canonical CSD Wave 1 cases; build clean 24-run index.
2. Full 500+Dream parity vs `5f30fdd`.
3. Score (CLIP sidecars), blind-rate Wave 1, freeze base B in journal.
4. Wave 2 then freeze finalists + away plan before departure.
5. Launch 60-hour campaign via `scripts/run-illusion-campaign-tmux.sh`.

```bash
# From reliability branch checkout
export PYTHONPATH=worker TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1 HF_HUB_OFFLINE=1
PY=$(scripts/worker-python.sh)
$PY -m worker.illusion_campaign plan --phase wave1 \
  --out .local/illusion-reliability/campaigns/wave1/plan.json \
  --evidence-root .local/illusion-experiments-v3
scripts/run-illusion-campaign-tmux.sh .local/illusion-reliability/campaigns/wave1/plan.json
```

## Exclusions

- `out/illusion-experiments/` @ `d2e6c52`
- `out/illusion-experiments-v2/`
- Quarantined scaled-CSD Wave 1 runs (files retained, not admitted)
