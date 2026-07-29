# The illusion yield window

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
its smoke ran. A freshly built plan took the Hub-id defaults instead, so
every window cell would have died at launch with nobody watching, and the
window would have been spent before anyone looked. Model ids are now resolved
to cached snapshot directories where plans are built and where the experiment
CLI builds its config.

### A2: prompt wording

Same pair, seed, budget, guidance and objective; only the wording differs.

| Wording | Medium delivered | Crown view | std / detail |
|---|---|---|---|
| `a centered intricate HB pencil illustration of X, full object, strong silhouette, isolated on plain warm paper` | no, neon pink on green | crown lost to tentacles | 0.108 / 0.0209 |
| `a centered intricate oil painting of X, ...` | yes, oil on canvas | flat octopus silhouette | 0.307 / 0.0393 |
| `an intricate detailed hb pencil sketch of X` | yes, graphite on paper | ornate crown, most detail | 0.317 / 0.0587 |

The window uses the third. It is the only wording with a human-approved cell
behind it, it delivered its own medium everywhere it was tried, and monochrome
removes the colour agreement the two flip views would otherwise negotiate.

A3 qualifies this: the scaffolded wording did NOT go neon on
`lighthouse_goblet`. Wording interacts with the pair rather than being
categorically broken. What stands is that the short wording was never worse.

### A3: the budget cut was wrong

On `lighthouse_goblet`, a pair no cell had run, quality improved monotonically:

| SDS steps | Result |
|---|---|
| 1500 | readable, mottled, low contrast |
| 2000 | marginally better than 1500 |
| 3000 | noticeably cleaner structure |
| 5000 | cleanest tone and detail |

There is no plateau at 2,000. The calibration smoke saturating there was the
paper's own easiest pair, which is exactly the caveat attached to the original
proposal, now measured and now falsified for the general case. Cutting to 3,000
would have under-baked every cell and understated yield by blaming pairs for
the schedule.

5,000 steps at four seeds and 3,000 at six cost the same ~50 GPU-hours, so the
budget goes up and the seed count comes down. The per-cell ladder means the
yield-versus-budget curve still comes free from the same sweep.

### A4: 512px primes rejected

The prime is the printable artifact, so a 512px network was worth asking about.
It cost 1491s of SDS against 638s and 2665s total against 810s, a 3.3x with
Dream alone 6.8x slower, for no visible quality gain at equal steps. VRAM rose
from 4331 to 5680 MB. The paper's 256px network stands.

### A5: joint Dream Targets, the largest single win

The recipe pinned `dream_joint` off. Turning it on, with the same pair, seed,
budget and wording as the neon cell above, and therefore from an identical neon
SDS state:

| Dream targets | Result | Total | std |
|---|---|---|---|
| independent per view | purple mush, crown gone | 810s | 0.108 |
| joint, reconciled | legible monochrome crown and octopus | 807s | 0.296 |

It rescued a failing cell for nothing. This is issue #134's mechanism measured
rather than argued: independent per-view targets fight over the shared pixels
and drive the collapse that has been the dominant failure mode all along.

Because it is one comparison on one pair, the window carries an
`independent_dream_control` arm that duplicates two sweep cells with joint
targets off, so the result is refreshed at window scale instead of trusted.

## The matrix

Phase `window`, 154 cells over 30 pairs and five seeds (11, 23, 37, 53, 71),
at 5,000 SDS steps with joint Dream, `reference_sketch` wording and 256px
primes. Estimated 77.9h.

The window's length is a launch parameter, not a property of the matrix, so the
seed count is sized generously: breadth-first ordering means a short window
drops the tail rather than losing pairs. Measured degradation, with the driver's
own deadline reserve applied:

| Deadline | Cells | Controls | Full seed blocks |
|---|---|---|---|
| 48h | 93 | 3/4 | 3/5 |
| 60h | 117 | 4/4 | 3/5 |
| 72h | 141 | 4/4 | 4/5 |
| 76h | 149 | 4/4 | 4/5 |
| 80h | 154 | 4/4 | 5/5 |

The 30 pairs:

- the 16 curated pairing-rule pairs from issue #138, never GPU-tested;
- the five topology-compatible reference pairs the earlier plan would have run;
- seven pairs from the legacy oil corpus whose human verdicts already exist,
  including the acceptance gate's own two control pairs and the two the
  2026-07-19 curation called strongest. Running these under the new recipe is
  the only way to say whether it beats what exists rather than merely producing
  something. `walrus_ladybug` stays excluded by standing decision;
- `locomotive_eye_control`, the deliberately incompatible negative control;
- `giraffe_penguin_calibration`, the known-good anchor.

Ordering is breadth-first by seed. Every pair gets one seed before any pair
gets two, so a window that ends early answers "which pairs work at all"
rather than "these three pairs work". Cell 1 is the rig check: the anchor
pair at the window's own settings. If a shorter budget or a different wording
broke what already worked, it shows in one cell rather than fifty.

Four control cells run after the third seed block, near the 48-hour mark, each
duplicating a sweep cell's pair and seed so the comparison is direct rather than
across the corpus. They are deliberately not last: a window that runs short must
lose sweep tail rather than the comparisons the sweep is measured against.

- `independent_dream_control`, two cells with joint Dream off, refreshing A5;
- `budget_control_10k`, two cells at the paper's 10,000 steps, testing on this
  window's own evidence whether 5,000 left anything on the table.

Every cell writes its own checkpoint ladder as fractions of its budget
(250, 1000, 2500, 5000) plus Dream rounds 1, 4 and 8 and the final image. So
each cell records where its own subjects formed rather than inheriting the
calibration pair's saturation point, and the yield-versus-budget question is
answered by the sweep itself.

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
`.local/illusion-reliability/campaigns/window/RUNBOOK.md`. Two operational
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

Do not use the blind sheets as the yield readout. At this scale they are a
tall shuffled strip per stage, and the shuffle that makes them fair for an A/B
is exactly what destroys per-pair grouping.

CLIP pair scores are written as sidecars for post-hoc calibration against
the human ratings (`calibrate`), and remain diagnostic until that calibration
clears its thresholds. No default changes and no PR #118 cherry-pick without
a later human acceptance gate.
