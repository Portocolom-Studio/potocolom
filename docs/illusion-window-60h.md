# The 60-hour illusion yield window

The question this window answers: how often does the optimizer produce a
usable flip illusion, and what makes the difference between a pair that
works and one that does not. Yield is the deliverable, so coverage is what
the GPU budget buys.

This supersedes the 36-cell plan in
[illusion-reliability-60h.md](illusion-reliability-60h.md) as the primary
axis. That plan's calibration smoke is retained as evidence and as this
window's rig check. Product defaults stay `legacy` and PR #118 is
untouched, exactly as before.

## Why the earlier plan was re-cut

The prepared plan spent 51.3 GPU-hours on 36 cells: five topology-compatible
pairs, one calibration pair, one incompatible control. Three things about it
did not survive review.

**The budget was set from the paper, not from measurement.** Reviewing the
calibration smoke's own checkpoint ladder:

| Checkpoint | Both views readable |
|---|---|
| SDS-500 | no, nothing formed |
| SDS-2000 | yes, giraffe and penguin both clear |
| SDS-5000 | yes, marginally refined |
| SDS-10000 | yes, no readable gain over 5000 |
| final, post-Dream | cleanest of the five |

SDS was 3240.9s of the 3416.6s cell, which is 94.9 percent. Dream was
173.5s and produced the artifact the human picked. Spending the budget where
quality had already saturated cost roughly three times the coverage.

**Nothing would have been attributable.** The recipe differs from the runs
that failed in six ways at once: SGD rather than Adam, 10,000 rather than 500
steps, guidance 60 rather than 100, weighted SDS at 0.1 rather than legacy,
a 256px rather than 512px prime network, and pencil rather than oil prompts,
over a different pair corpus. A 40 percent yield would not have said which
of those earned it.

**The dominant axis was the one being under-sampled.** docs/illusions.md
calls prompts "the biggest lever by far", and issue #138 already holds a
curated corpus encoding the pairing rules that the 2026-07-19 curation
produced. No GPU cell had ever run it. Five pairs does not sample that axis.

A confound was also found and named while re-cutting: the calibration pair
carries the wording `an intricate detailed hb pencil sketch of {}`, which is
the only wording any reviewed cell has used, while the five reference pairs
carry heavier scaffolding (`a centered intricate HB pencil illustration of
{}, full object, strong silhouette, isolated on plain warm paper`) that no
GPU cell had validated. Both are now requestable styles, so the difference
is a measurable arm rather than a hidden variable.

## Pre-window measurements

Five measurements were run before the window, under
`.local/illusion-reliability/campaigns/prewindow/`. They set the budget,
the wording, the prime resolution and the concurrency the window uses. Their
outcomes are recorded in that directory and summarised in the runbook.

| Arm | Question |
|---|---|
| A1 concurrency | Does the card have throughput headroom for K cells at once? |
| A2 style | Which wording earned the smoke: the reviewed short one, the untested scaffolded one, or oil at identical scaffolding? |
| A3 saturation | Does "readable by SDS-2000" hold on a pair nobody has run? |
| A4 prime resolution | The prime is what gets printed. Is 512px native better than 256, or does the signal hide in high frequencies as the paper warns in Sec. 4.3? |
| A5 joint Dream | The recipe pins `dream_joint` off, yet joint targets moved phase-2 loss 98 percent per round against 44-60, and independent per-view targets are the recorded mechanism behind subject collapse. Dream is about 15 percent of a cell, so asking is nearly free. |

### A1: concurrency rejected

| Slots | Aggregate | Per cell | Gain |
|------:|----------:|---------:|-----:|
| 1 | 3.11 steps/s | 3.11 | baseline |
| 2 | 3.25 steps/s | 1.63 / 1.62 | +4.5% |
| 3 | 3.12 steps/s | 1.04 / 1.04 / 1.46 | +0.3% |

Aggregate throughput is flat, so the ceiling is compute rather than memory:
peak VRAM stayed 4331 MB per cell at every slot count. The window therefore
runs at one slot. K=3 also finished unevenly (342.5s against 480.6s), which
would make cell duration unpredictable for the driver's deadline arithmetic.

`POTOCOLOM_GPU_SLOTS` and `--shard` stay in the tree at their default of 1,
tested and inert. The measurement is what makes 1 correct here, and a
different card would deserve its own measurement rather than inheriting this
conclusion.

At one slot, K=1 measured 0.322 s/step against the calibration smoke's
0.324, so the rig reproduces its own baseline.

### The blocker A1 found first

Every cell of the first attempt died in 11 seconds with
`IncompleteSnapshotError`: `HF_HUB_OFFLINE` refuses this machine's SD 1.5
snapshot, which is missing 23 files including the safety checker the code
already works around. The 36-cell plan hardcoded absolute snapshot paths, so
its smoke ran. A freshly built plan took the Hub-id defaults instead, so all
154 window cells would have died at launch with nobody watching, and the
window would have been spent before anyone looked. Model ids are now resolved
to cached snapshot directories where plans are built and where the experiment
CLI builds its config.

## The matrix

Phase `window60h`, 154 cells over 25 pairs and six seeds
(11, 23, 37, 53, 71, 89):

- the 16 curated pairing-rule pairs from issue #138, never GPU-tested;
- the five topology-compatible reference pairs the earlier plan would have run;
- `dog_sloth` and `mountain_valley`, whose human verdicts are already known,
  as positive controls;
- `locomotive_eye_control`, the deliberately incompatible negative control;
- `giraffe_penguin_calibration`, the known-good anchor.

Ordering is breadth-first by seed. Every pair gets one seed before any pair
gets two, so a window that ends early answers "which pairs work at all"
rather than "these three pairs work". Cell 1 is the rig check: the anchor
pair at the window's own settings. If a shorter budget or a different wording
broke what already worked, it shows in one cell rather than fifty.

Four full-budget control cells run last, at the paper's 10,000 steps on two
pairs and two seeds, so the shorter budget is defensible on this window's own
evidence rather than only on the pre-window ladder.

Every cell writes its own checkpoint ladder as fractions of its budget, so
each records where its subjects actually formed instead of relying on the
calibration pair's saturation point generalising.

## What the harness will and will not do unattended

The window runs with nobody watching, so it is worth being exact about the
safety net.

It stops on catastrophe: four consecutive cells emitting near-constant
images aborts the run (`--abort-after-dead`, default 4). The floors are
calibrated on this repository's evidence, where the worst non-catastrophic
run sits at luma std 0.101 and detail 0.0054, the approved smoke at 0.351
and 0.0122, and the catastrophic SDXL cell at 0.006 and 0.0017.

It will not stop on bad quality, and cannot. Measured across every completed
run under `.local/`, no simple image statistic separates the human-rejected
SDXL pilot (median detail 0.0088) from the human-tolerated Wave 1 corpus
(median 0.0097). An automated quality gate is therefore not available for
this workload and is deliberately not attempted. Clearing the floors means
the optimizer emitted an image, never that the image is good.

The real protection against spending the window on a dead axis is the plan
shape, not a threshold: breadth-first ordering, a rig check first, a wide
corpus so no single unvalidated choice consumes 50 hours, and controls whose
answers are already known so a broken rig is visible in review.

## Running it

Preflight, launch and review are in the campaign runbook at
`.local/illusion-reliability/campaigns/window60h/RUNBOOK.md`. Two operational
notes that have cost time before:

- Stop the self-hosted Actions runner. Any CI job makes `gpu-lock` refuse
  cells for as long as the job runs, and the launcher warns about this.
- Never wrap the campaign in an outer `gpu-lock`. Each cell takes the lock
  itself; an outer lock deadlocks against the inner one and has already
  cost hours once.

## Review

Read yield first, with `build-yield`: one 1024x768 sheet per pair, its seeds
side by side, plus an index and a machine-readable `yield.json`. This is the
readout for the question the window asks. The index counts cells that emitted
an image, which is not the same as a readable illusion, so the sheets are
still the evidence.

`build-stage-blind` remains the acceptance path, and it is stage-separated:
independent blinded sheets for the last SDS checkpoint, Dream round 1, and
the final image. Rate the three stages separately and freeze `ratings.jsonl`
before opening the answer key. The smoke is the precedent for why stages are
split: SDS-end had the strongest raw dual-subject structure while final had
the cleaner presentation, and a finals-only sheet would have hidden that.

Do not use the blind sheets as the yield readout. At 154 runs they are a
1024x19712 strip per stage, and the shuffle that makes them fair for an A/B
is exactly what destroys per-pair grouping.

CLIP pair scores are written as sidecars for post-hoc calibration against
the human ratings (`calibrate`), and remain diagnostic until that calibration
clears its thresholds. No default changes and no PR #118 cherry-pick without
a later human acceptance gate.
