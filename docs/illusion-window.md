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

Because it was one comparison on one pair, it was validated before freezing.
A6 then overturned it as a universal setting.

### A6: the rig check earned its place

The exact final config was run attended on two cells before freezing anything:
the calibration pair, whose good outcome is known, and `stag_oak`, standing in
for the sixteen scene-rich issue #138 pairs whose subjects had never met the
pencil wording.

`stag_oak` came out well: a stag whose antlers read as branches one way up, an
ancient oak with spreading roots the other. That de-risked sixteen pairs.

The calibration pair collapsed. Both views became giraffes; the penguin was
gone. At SDS-5000 view 2 still held penguin structure, a dark head with a beak
to the right, and Dream round 1 turned it into a second giraffe.

The mechanism follows from what joint Dream does. It reconciles both views into
ONE consensus image, which amplifies a pair whose subjects can genuinely BE one
image and destroys a pair whose subjects cannot, by collapsing onto whichever
subject holds the stronger score field:

| Pair | Topology | Joint Dream |
|---|---|---|
| crown / octopus | compatible, both radial with arms | rescued a neon failure |
| stag / oak | compatible, antlers read as branches | clean result |
| giraffe head / penguin | dissimilar | collapsed to one subject |

On the pairs it does not suit, joint Dream causes the exact failure it was
built to prevent. Which pairs those are is not knowable in advance, so Dream
mode became an axis of the window rather than a setting, and yield is read as
the better of the two modes per pair. Had the config been frozen on A5 alone,
half the corpus could have collapsed unattended.

## The matrix

Phase `window`, 182 cells: 30 pairs x 3 seeds (11, 23, 37) x 2 Dream modes,
plus two budget controls. 5,000 SDS steps, `reference_sketch` wording, 256px
primes. Estimated 91.9h, which deliberately overshoots the window.

Overshooting is deliberate. The window's length is a launch parameter, not a
property of the matrix, and breadth-first ordering means a short window drops
the tail rather than losing pairs. Planning past the deadline therefore costs
nothing and buys coverage if the absence runs long. Measured degradation, with
the driver's own deadline reserve applied:

| Deadline | Cells | Controls | Complete (seed, mode) blocks |
|---|---|---|---|
| 48h | 93 | 2/2 | 3/6 |
| 60h | 117 | 2/2 | 3/6 |
| 72h | 141 | 2/2 | 4/6 |
| 76h | 149 | 2/2 | 4/6 |
| 80h | 157 | 2/2 | 5/6 |

At 76h that is both Dream modes complete at two seeds across all 30 pairs, plus
a partial third seed. Independent Dream runs first in each seed block, because
it is the mode the calibration smoke was reviewed under, so a window cut short
keeps the validated mode and cell 1 remains a rig check that can fail
informatively.

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

Two budget-control cells run after the first seed block, near the 32-hour mark,
duplicating a sweep cell's pair, seed and Dream mode at the paper's full 10,000
steps. They are deliberately not last: a window that runs short must lose sweep
tail rather than the comparison the sweep is measured against. There is no
separate joint-Dream control any more, because both modes are full arms, which
is a stronger test than two extra cells.


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

## Results

182 cells completed, all six blocks at 30/30, zero degenerate. Then all 546
(run, stage) items were scored 0-5 by the human reviewer, both views shown
together with their target subjects, mode and seed hidden.

### Yield

| Measure | Result |
|---|---|
| cell-stages scoring >= 3 (both subjects readable) | 93/546, 17.0% |
| cell-stages scoring >= 4 | 35/546, 6.4% |
| runs producing something usable at their best stage | 62/180, 34.4% |
| pairs reaching a 5 at least once | 15/30 |
| pairs never producing anything usable | 6/30 |

### The Dream phase's later rounds hurt

| Stage | keep rate | score >= 4 | mean score |
|---|---|---|---|
| sds_end | 35/182, 19.2% | 15 | 0.93 |
| dream_d1 | 33/182, 18.1% | 11 | 0.93 |
| final | 25/182, 13.7% | 9 | 0.68 |

The final image is the WORST of the three stages on every measure. Raw SDS-end
is the most likely to be a keeper, Dream round 1 ties it, and rounds 2-8 lose
about a quarter of the keepers and a third of the mean score. The 8-round
schedule is not paying for itself.

A mid-window claim to this effect was retracted because it rested on a
best-of-three selection effect. The direction now stands on human ratings
rather than on a metric artifact, though the magnitude is smaller than that
retracted claim.

### Dream mode has no clear global winner

| Evidence | independent | joint |
|---|---|---|
| CLIP head-to-head, 90 comparisons | 40 | 50 |
| human keep rate, 5k only | 43/270, 15.9% | 50/270, 18.5% |
| human head-to-head, 5k only, 270 comparisons | 48 | 59 (163 tied) |
| human keepers scoring >= 4 | 19 | 16 |

These are not three independent measurements: two of them reuse the same human
labels, and CLIP failed calibration. The conclusion is no clear global winner,
with useful complementarity: across the 90 pair-seeds, 14 were usable only under
independent, 20 only under joint, 14 under both, and 42 under neither. Neither
mode is a default, and the yield above is only reachable by choosing mode per
pair, which is what running both as an axis bought.

Ratings are canonicalized before any of this is computed: the raw log holds 552
rows for 546 items because six were re-rated, and the server means last score
wins. Use `illusion_review --export-ratings`, which also carries the
`(spec_hash, stage, arm)` key on every row. The head-to-head above reads
48/58/164 when a key omitting `sds_steps` merges a 5k cell with a 10k one.

### CLIP is not usable as a screen

Calibrated against the 546 human ratings, `clip_pair_score` gives ROC-AUC
0.706 against a required 0.75, and hit-rate 0.538 against a required 0.75. It
fails both gates in docs/illusion-reliability.md. Better than chance, not good
enough to screen on, so human review stays the gate and no automated
style/seed selection or in-loop balancing may be enabled.

### What this means for the product

Reliability here comes from SELECTION, not from a single correct recipe. A run
yields something usable 34% of the time; the best output for a pair comes from
varying seed, mode and stage. Every keeper the human named is one specific
(pair, seed, mode, stage) cell rather than a pair that can be trusted. The
engineering target is therefore cheap candidate generation plus good ranking,
and ranking currently means a human, because CLIP failed calibration.

Keepers scoring 4 or 5 are exported to
`.local/illusion-reliability/keepers/window-2026-08/` as copies with both
orientations and the printable prime.

## Window 2: results

Phase `window2`, 98 entries producing 206 final-stage observations, finished in
about 44 hours with zero failed cells. Its plan is
`.local/illusion-reliability/campaigns/window2/PLAN.md`; the decision rules
below were fixed in writing before any image was seen.

Two things changed in the design, and both mattered more than the settings
under test. Dream arms now fork from ONE SDS state, so a mode or negative-prompt
contrast differs in exactly the thing being contrasted. And the review tool
shows both views of one image together, scored 0-5, with stage, mode and seed
hidden.

Overall keep rate is 79/206, 38.3 percent, mean score 1.85. That is not a
corpus-wide yield: 144 of the 206 observations are the six PROVEN pairs, so the
number is inflated by design. The comparable figure is block C, 18 previously
untested pairs at one seed, which keeps 11/36, 30.6 percent.

### The predeclared rules

| Rule | Threshold | Measured | Verdict |
|---|---|---|---|
| Negative prompt | frame better by >= 1 category in >= 50% of A bases | 8.5% (6 better, 5 worse, 60 unchanged of 71) | not adopted |
| Colour | oil keeps >= 80% of `reference_sketch`'s rate | 71.4% (oil 34.7%, sketch 48.6%) | not adopted |
| Dream schedule | one round retained unless beaten on both measures | one round 2/4 and mean 3.00; truncated 1/4 and 1.75; full 8 rounds 0/4 and 0.25 | one round retained |

The negative prompt does raise scores, by +10 keepers and +0.40 mean, but the
paired sign test gives p=0.14 and the predeclared criterion was about frames.
On `reference_sketch` it also trades sideways rather than cleanly: `minor` falls
from 11 to 6 while `disqualifying` rises from 14 to 17.

Colour is delivered in 78/78 oil arms, so the medium works. It costs keepers,
which is what fails the rule.

The Dream schedule comparison is four matched pairs per arm, so it settles
nothing on its own. It does reproduce window 1's finding in the harshest form
seen yet: at eight rounds, `wolf_raven` and `elephant_swan` both go from 5 to 0.

### Joint Dream wins, and window 1 could not have seen it

| Comparison | Pairing | joint up / down / tied | sign test |
|---|---|---|---|
| block A | within base and negative setting, one shared SDS state | 38 / 12 / 22 | p = 0.0003 |
| blocks C and D | within pair and seed | 15 / 3 / 6 | p = 0.0075 |

Campaign-wide, joint keeps 49.5 percent against independent's 28.0 percent, mean
2.40 against 1.34. C and D are an independent replication of A on different
pairs and a different seed, and the direction holds in every block.

Window 1 called this parity (48/59 with 163 ties) and was not wrong on its own
data. Its two modes came from separate runs with separate SDS states, so the
comparison carried the whole run-to-run variance. Forking both arms from one
state is the entire difference. The lesson is about the design, not the setting.

This does not overturn A6. Joint Dream still collapses pairs whose subjects
cannot be one image, and the calibration pair is still the example. It wins on
average across a corpus; it is not safe unattended on an unknown pair.

### Frames are a property of the medium, not of the prompt

| Style | none | minor | disqualifying |
|---|---|---|---|
| `oil`, n=78 | 72 | 6 | 0 |
| `reference_sketch`, n=127 | 36 | 35 | 56 |

Same pairs, same seeds, same arms in block A. A prompt that asks for a pencil
sketch gets the paper it is drawn on: hands, desk, torn edges, sketchbook
borders. Asking for those things NOT to appear moved almost nothing, while
changing the medium removed them entirely.

So the frame complaint and the colour complaint are the same trade seen from two
sides. Oil is clean and reads worse; pencil reads better and draws its own
paper. Neither rule adopted anything, and that trade is now measured rather than
argued.

### Colour is measured, not judged

The review tool used to ask two colour questions per final item. Both were
removed:

- `colour_consistent_between_views` cannot be answered no. `derived_2` is
  `derived_1` rotated 180, and the largest single-channel disagreement anywhere
  in window 2 is 1/255, on at most 0.02 percent of pixels.
- `colour_delivered` is mean chroma. Oil's minimum is 30.7 against
  `reference_sketch`'s median of 9.2.

Both are computed by
`.local/illusion-reliability/campaigns/window2/review/measure-colour.py` into
`colour-measured.jsonl`, keyed `(spec_hash, stage, arm)` so it joins the
ratings. The rating rows keep the columns, filled with `na`.

This halved the keystrokes per item, which matters: window 1 measured the score
drifting from 1.52 in the first quintile to 0.47 in the last.

Chroma also found something nobody asked for. Twenty-three `reference_sketch`
items leaked colour above threshold, and they keep at 26.1 percent against 45.7
percent for the monochrome ones. Colour appearing in a pencil prompt is a
symptom, not a neutral variation.

### Block R: no rescues

The three RESCUE candidates were re-run under oil at both modes. None reached a
keeper: `galleon_whale` and `koi_moon` peak at 2 under joint, `hummingbird_fuchsia`
stays at 0 in both modes. Recorded as "not rescued under this recipe", which is
one recipe and one seed, not a verdict on the pairs.

### The best cell, and why it is not a conclusion

`reference_sketch` + joint + negative prompt on came in at 72.2 percent keep and
mean 3.28, far above the other seven cells in block A. It also carries the most
disqualifying frames of any cell, 10 of 18. It is the best of eight cells at
n=18, so the margin includes selection.

It was written up here as window 3's hypothesis. It is not, for two reasons found
after the write-up and recorded in the next section: on the endpoint that
describes a printable image it is 4 of 18, not 13 of 18, and a rerun of one fixed
configuration agrees with itself too weakly for a single 18-cell margin to mean
much. Best-of-eight at n=18 is a place to look, not a thing to spend 60 hours
confirming.

## After window 2: three findings from re-analysis

No new GPU time. All three come from window 2's own ratings, and two of them
change what the next window should do.

### The run is reproducible; the seed is the lottery

Every number in this section is recomputed by
`.local/illusion-reliability/campaigns/window3/reanalysis.py`, which exists
because these were first computed in throwaway shell heredocs and one of them
reached a plan while being wrong. Window 1 has 552 rating rows but 546 unique
ids: six items were rated twice, and the last rating per id is canonical.

Window 1's independent and joint runs are two separate runs of the same pair and
seed, and their `sds_end` images are produced before the Dream mode can matter.
That makes them a clean run-to-run comparison, n=90:

| Measure | Value |
|---|---|
| score correlation | 0.565 |
| mean absolute difference | 0.778 |
| keeper agreement at >= 3 | 85.6 percent, Cohen kappa 0.539 |
| keeper agreement at >= 4 | 90.0 percent, Cohen kappa 0.348 |

So a run reproduces itself moderately well on the human endpoint. "SDS is not
reproducible" should be narrowed to "not pixel-identical": 0 of 90 images match
bit for bit, which turned out to be too strict a criterion to reason from.

What actually varies is the seed. At the endpoint window 3 uses, one Dream round,
the unit being one seed at the better of the two modes:

| Measure | Value |
|---|---|
| pairs with a round-one keeper at some seed | 17 of 30 |
| seed-cells on those pairs that missed | 23 of 51 = 45.1 percent |
| per-seed success on a workable pair | p = 0.549 |

A miss at one seed is therefore weak evidence about the PAIR, which is why window
3 records it as "not obtained in this run" and never as dead. It also makes the
acquisition arithmetic concrete: at p = 0.549, one seed on 97 pairs expects about
1.3 times as many distinct workable pairs as two seeds on 49. That holds only if
the smaller set would be drawn from the same distribution; two seeds win if the
retained pairs are materially better.

WITHDRAWN, and recorded because it was load-bearing for a day. An earlier version
of this section claimed 72 cells across the two windows were reruns of one
configuration, agreeing at r = 0.10 with keeper status disagreeing 44 percent of
the time, and concluded that one observation is "close to uninformative". Those
72 cells match only at the `final` stage, because window 2 rated no other stage,
and window 1's final is after EIGHT Dream rounds against window 2's one. The
schedule is the largest effect either window measured, and it appears here as a
mean shift of 1.19 against 1.81. Matching window 1's `dream_d1` against window
2's `final` is closer to like-for-like and gives r = 0.196 with 28 of 72
disagreeing, still crossing two implementations. Neither is a rerun; no
configuration was ever run twice in either window. The conclusion was also wrong
in the other direction: run agreement is 85.6 percent, not near-random.

What survives is that ranking cells by a single small-n margin ranks noise, which
is what retired the best-cell hypothesis.

### The colour rule measured the wrong endpoint

Oil failed its predeclared rule on raw readability, and that failure stands. But
raw readability counts images that cannot be shipped. Block A, same pairs, same
seeds, per observation, n=72 per style:

Clean means the frame was rated and rated `none` or `minor`. A missing answer is
unrated, not clean: writing the rule as `!= disqualifying` counted one unrated
`reference_sketch` item scoring 5 as clean and moved the number below from 13 to
14. The review server now requires the frame answer once it asks for it.

| Endpoint, per observation, n=72 | `oil` | `reference_sketch` |
|---|---|---|
| score >= 3 | 25 | 35 |
| clean, score >= 3 | 25 | 13 |
| clean, score >= 4 | 11 | 10 |
| disqualifying frames | 0 | 31 |
| frame unrated | 0 | 1 |

`reference_sketch` loses 22 of its 35 readable images to their own frames; oil
loses none. The same defect is in the window 2 keeper export: it filtered on
score alone, so of 43 images scoring >= 4, 16 are disqualifying-framed and one is
unrated, leaving 26 clean keepers. `export_keepers` now applies the frame rule by
default.

That table is per OBSERVATION, and it overstates the case, because the two arms
of one base share an SDS state and are not independent observations. At the unit a
window 3 base actually produces - one base, better of its two arms, negative off:

| Endpoint, per base, n=18 | `oil` | `reference_sketch` | discordant | McNemar p |
|---|---|---|---|---|
| clean, score >= 3 | 7 | 6 | 5 / 4 | 1.00 |
| clean, score >= 4 | 5 | 6 | 4 / 5 | 1.00 |
| raw score >= 3 | 7 | 11 | 2 / 6 | 0.29 |

So the honest statement is that **oil does not yield more.** It ties at the
decision-relevant unit on a small n, and at >= 4 it is nominally behind. Oil is
chosen because it ties while producing no disqualifying frames at all and
delivering colour in 78 of 78 arms, which were the human reviewer's two actual
complaints about window 2's output. That is a product-risk argument, not a yield
argument, and treating the per-observation table as though it settled yield would
be motivated reasoning.

This does not reinstate the rejected rule either. The rule tested raw
readability, a quantity nobody wants to maximise, so the primary endpoint is now
stated as **score >= 3 AND frame not `disqualifying`**, with raw readability as a
secondary.

### A6 stands, and the explanation offered for it was wrong

An intermediate claim held that A6's collapse came from joint Dream combined with
the eight-round schedule, since joint helps 20 of 27 pairs at one round. That is
refuted by this document: A6's collapse was already present at Dream round 1
(line 179 above), whose strength is 0.95, identical to window 2's single round.
Rounds 2 to 8 cannot cause an outcome that exists after round 1.

The properly paired case is `wolf_raven` seed 11 at spec_hash `fd46cd54684e`,
negative off, where independent scored 5 and joint 0 from one shared SDS state,
and the joint arm also moved from a minor to a disqualifying frame. The
negative-on arms of that same base run the other way, 1 against 4, which is why
the negative setting has to be part of the key: keying on `spec_hash` alone lets
one arm pair overwrite the other and prints the opposite result. That is the same
under-specified-key mistake that contaminated the first mode head-to-head, and it
recurred once while checking this very claim.

So joint remains the default and collapse remains real at the shipped
configuration. What is not supportable is predicting collapse per pair. The
evidence for a stable pair property is one pair at one seed, and the same pair
reverses under a different negative setting; meanwhile a single seed misses a
third of the pairs that demonstrably work. There is no stable label to learn, and
a second arm forked from the same snapshot costs about 22 seconds against a 1,556
second SDS phase, which is cheaper than any predictor could be.

## Window 3: acquisition

Planned, not yet run. `worker.illusion_campaign --phase window3`, 116 bases and
232 observations at 55.9h (52.6h at the anchor's measured rate), inside a 68h
deadline for a 70h window. Two blocks: 98 acquisition bases, then an 18-base
wording screen that runs last. The plan is
`.local/illusion-reliability/campaigns/window3/PLAN.md`, the launch procedure is
`RUNBOOK.md` beside it, and the corpus is `BREADTH_FAMILIES` in
`illusion_experiment.py`.

Nothing is under test. Windows 1 and 2 settled the recipe, and the corpus is now
the binding constraint on yield: 144 of window 2's 206 observations came from six
already-proven pairs, and the 18 genuinely untested pairs kept 30.6 percent
against 38.3 percent overall. This window spends its hours on 97 pairs no window
has run.

| Setting | Value | Source |
|---|---|---|
| SDS steps | 5,000 | window 1, A3 |
| Dream rounds | 1 | window 2: eight rounds scored 0.25 against 3.00 |
| Dream arms | joint primary, independent fallback, forked from one SDS state | joint wins overall, collapses some pairs, and the fallback costs 22s |
| Style | `oil` | the frames and colour, not yield: see above |
| Negative prompt | off | rejected by its rule, and oil needs no frame fix |
| Prime resolution | 256 | window 1, A4 |
| GPU slots | 1 | throughput is flat from 1 to 3 |

One seed per pair rather than two, because at p = 0.549 one seed on 97 pairs
discovers about 1.3 times as many distinct workable pairs as two seeds on 49.
Replication buys ranking reliability, which this window does not need, and that is
exactly why a miss is recorded as not obtained.

Order and seeds are both **stratified by family**: permute within each family,
then round-robin across families. An unstratified hash permutation over the whole
corpus put 20 scene pairs and 8 object pairs in the first 60 executions, and gave
one seed five upright pairs and no object or scene pair at all. Neither breaks
total yield if all 97 finish, but both make a truncated window unrepresentative
and confound any comparison between families. Stratified, every prefix is balanced
to within one pair per family.

### The corpus, and the pass that programmatic checks cannot do

97 pairs in four families: upright/pendant 22, branching/radial 25,
object/natural 27, scene/landform 23. Vocabulary is disjoint from both windows
and from the 2026-07-19 gallery curation, so a failure here is new information.

`test_breadth_corpus_obeys_the_pairing_rules` enforces what can be checked
mechanically: unique ids, no collision with any pair either window ran, no
subject used more than twice, no inverse duplicates, no multi-clause subjects, and
no double-wrapped style. Passing it says nothing about whether two subjects share
a scene at compatible frame mass, which is the part that decides whether a pair
can work at all.

A semantic pass cut 22 of the original 119, recorded with their reasons in
`BREADTH_CUT`:

| Reason cut | Examples |
|---|---|
| identical or near-identical silhouettes | `stalagmite_stalactite`, `archrock_bridgearch`, `spire_tarnreflection` |
| symmetric under 180 rotation, so the flip carries no information | `hourglass_teardrop`, `urchin_chestnutcase`, `dandelionclock_seaurchin`, `astrolabe_sanddollar` |
| depends on colour or material | `geode_pomegranate`, `tidepool_stainedglass` |
| depends on fine texture that survives neither 256px nor the flip | `loom_spiderweb`, `waterfallcurtain_lacepanel`, `saltflat_crackedglaze` |
| frame-mass mismatch: a solid animal against a wispy or tiny form | `lemur_fig`, `tapir_mossbeard`, `capybara_willowfrond`, `dipper_icicle` |

The upright/pendant family took the deepest cut and remains the highest-risk
family, because its logic keeps inviting a large animal to be paired with a small
compact form. Scene/landform is next, because several of its distinctions reduce
to texture or viewpoint rather than to inversion. If a family fails wholesale that
is diagnosable from the yield sheets, which is the reason to keep the split.

### Predeclared, before any data is seen

- Primary endpoint: **clean readable**, score >= 3 with the frame rated `none` or
  `minor`. Reported alongside **clean keeper**, score >= 4 on the same frame rule,
  because the exporter uses >= 4 and the two must not be conflated.
- Raw readability is a secondary and never the headline. In window 2 the two
  disagreed by a factor of two.
- The endpoint is the better of a base's two `final` arms. Not SDS-end, not
  another checkpoint, chosen now rather than after seeing the sheets.
- A pair that misses at its single seed is "not obtained in this run".
- No adoption decision about settings comes out of this window. Pairs that produce
  a clean keeper enter the corpus; nothing else changes.
- Window-level success: at least **17 of the 97** new pairs produce a clean
  readable image, the anchor excluded. That is window 2 block C's 6-of-18 strict
  clean rate carried across, whose Wilson 95 percent lower bound is about 16
  percent. It is a soft expectation with no action attached, not a gate, and it is
  transported across a different corpus and medium.

The anchor cell is `wolf_raven`, which exercises the fallback arm by
construction: at spec_hash `fd46cd54684e` with the negative prompt off, its
independent arm scored 5 against joint's 0 from one shared SDS state. Its score is
not a gate, because one seed on a workable pair misses 45 percent of the time.
What it gates is structural: two arm directories, two distinct images, SDS run
once.

### Block W: screening one wording, after a smoke killed the other

Added when the window grew to 70h. The extra hours went here rather than into more
corpus because the binding resource on the corpus is careful pair-writing, not GPU
time - 22 of the last 119 pairs had to be cut in a semantic pass, and 36 more
written in one sitting would repeat that error.

Neither validated wording wins the trade that caps the product. `reference_sketch`
reads better raw, 35 of 72 against 25, and loses 31 of 72 to its own frames. `oil`
produces no disqualifying frames in 78 and only ties on the clean endpoint. The
product wants both and can currently have either.

Two candidates were planned. **Both were smoked at 1,500 steps before any block
time was committed**, on `moose_butterfly` seed 11 with a plain-`oil` control at
the same step count, and the predictions were written down first. Chroma is the
`measure_colour` statistic, threshold 20:

| Wording, 1,500 steps | chroma indep / joint | frames |
|---|---|---|
| `oil` control | 66.0 / 57.2 | clean, full bleed |
| `monochrome_oil` | 18.6 / 27.0 | **wooden picture frame, both arms** |
| `charcoal` | 9.9 / 7.1 | clean, faint edge only |

`monochrome_oil` was CUT. The word works on colour - it cuts chroma by about 60
percent against the control - but it also summons a framed painting, worse than
anything plain `oil` produced in 78 observations, and frame-cleanliness was the
entire reason to start from `oil`. "Monochrome oil painting" is auction-catalogue
vocabulary, where the images genuinely are photographs of framed paintings. The
smoke cost 24 minutes of GPU and saved 8.2 hours.

`charcoal` survived and is the live candidate. It delivers pencil-grade monochrome,
9.9 and 7.1 against `reference_sketch`'s 9.2 median, with none of pencil's frames.
It was included as the CONTROL, expected to fail.

**The mechanism this screen was built on is refuted.** The claim was that frames
come from naming a paper-bound artifact - "pencil sketch", "isolated on plain warm
paper". But `charcoal` is paper-bound and clean, while `monochrome_oil` is
canvas-bound and framed. Both directions fail, so frame behaviour is a property of
the SPECIFIC PHRASE and is not derivable from the medium, from paper-boundness, or
from any reasoning available before rendering. The plain-`oil` control is what makes
this conclusion safe: at the same 1,500 steps it is clean, so the frames belong to
the wordings and not to the reduced step count.

That has a method consequence worth more than the finding: screen candidate strings
with an 8-minute smoke, and do not reason about them. Ten candidates screen in
about 80 minutes, and block time goes only to survivors.

Block W is therefore 1 wording x the 6 proven pairs x seeds 11, 23 and 37, both
Dream arms, 18 bases at 8.2h. Same pairs and seeds as window 2's negative-off
bases, so `charcoal` meets both incumbents on matched ground: `oil` 7 of 18 and
`reference_sketch` 6 of 18 on the clean base rate.

It runs LAST, so an acquisition overrun truncates the bonus and never the
deliverable. Predeclared: at n=18 this is a **screen, not an adoption test**, since
window 2 showed that size resolves only a large effect. A candidate that beats BOTH
incumbents advances to replication in a later window and changes no default here. A
truncated block W is reported as **inconclusive**, never negative.

### Run the SDXL diagnostic first

It has still never been run, and it is two 20-step generations, a VAE
reconstruction and six UNet probes: minutes, not hours. It goes first because the
window has slack and because an unrun diagnostic keeps being deferred.

Predeclare that it is diagnostic only. Whatever it shows, it does not become an
improvised SDXL pivot inside this window; SD 1.5 remains the backbone for the 98
bases either way.

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
