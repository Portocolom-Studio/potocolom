"""Immutable illusion campaign plans and an unattended runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from worker.illusion_experiment import (
    BREADTH_FAMILIES,
    BREADTH_PAIRS,
    FINAL_PAIRS,
    SCREEN_PAIRS,
    PAIR_BY_ID,
    degenerate_run,
    git_sha,
    is_completed_run,
    local_snapshot,
    repo_root,
    resolve_pair_prompts,
    write_manifest_atomic,
)

PREFERRED_EVIDENCE_ROOT = Path(
    "/home/leon/Nextcloud/ETSIIT/ETSHIT/Github/potocolom/.local/illusion-experiments-v3"
)
DEFAULT_EVIDENCE_ROOT = (
    PREFERRED_EVIDENCE_ROOT
    if PREFERRED_EVIDENCE_ROOT.exists()
    else Path(".local/illusion-experiments-v3")
)
GENERATION_DEADLINE_S = 52 * 3600
RUN_TIMEOUT_S = 65 * 60
TELEMETRY_INTERVAL_S = 10
START_RESERVE_S = 5 * 60
BUSY_EXIT_CODE = 75
EVENTS_PATH = Path(
    "/home/leon/Nextcloud/ETSIIT/ETSHIT/Github/potocolom/.local/illusion-reliability/events.jsonl"
)

WAVE1_PROFILES: list[tuple[str, list[str]]] = [
    ("legacy", ["--sds-objective", "legacy"]),
    ("weighted_sds", ["--sds-objective", "weighted_sds"]),
    ("csd", ["--sds-objective", "csd"]),
    ("nfsd_7_5", ["--sds-objective", "nfsd", "--sds-guidance", "7.5"]),
    ("dream_lr_3e3", ["--sds-objective", "legacy", "--dream-lr", "3e-3"]),
    ("dream_joint", ["--sds-objective", "legacy", "--dream-joint"]),
]


@dataclass(frozen=True)
class CampaignEntry:
    entry_id: str
    tier: str
    profile: str
    pair_id: str
    seed: int
    flags: tuple[str, ...]
    out_rel: str
    priority: int
    estimate_s: float = 950.0
    style: str = "none"

    def spec_hash(
        self,
        *,
        model_id: str = "",
        dream_model_id: str = "",
        optimizer_fingerprint: str = "",
    ) -> str:
        _subjects, effective_prompts = resolve_pair_prompts(PAIR_BY_ID[self.pair_id], self.style)
        payload = {
            "effective_prompts": effective_prompts,
            "seed": self.seed,
            "optimizer_config": {"type": "flip", "flags": list(self.flags), "style": self.style},
            "model_snapshot_paths_or_revisions": {
                "model": model_id,
                "dream_model": dream_model_id,
            },
            "optimizer_fingerprint": optimizer_fingerprint,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class CampaignPlan:
    campaign_id: str
    git_sha: str
    created_at: str
    evidence_root: str
    model_id: str
    dream_model_id: str
    optimizer_fingerprint: str = ""
    entries: list[CampaignEntry] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        data = {
            "campaign_id": self.campaign_id,
            "git_sha": self.git_sha,
            "created_at": self.created_at,
            "evidence_root": self.evidence_root,
            "model_id": self.model_id,
            "dream_model_id": self.dream_model_id,
            "optimizer_fingerprint": self.optimizer_fingerprint,
            "entries": [
                {
                    **asdict(entry),
                    "spec_hash": entry.spec_hash(
                        model_id=self.model_id,
                        dream_model_id=self.dream_model_id,
                        optimizer_fingerprint=self.optimizer_fingerprint,
                    ),
                }
                for entry in self.entries
            ],
        }
        data["plan_sha"] = plan_sha(data)
        return data


def plan_sha(data: dict[str, Any]) -> str:
    payload = {key: value for key, value in data.items() if key != "plan_sha"}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _entry(
    *,
    tier: str,
    profile: str,
    pair_id: str,
    seed: int,
    flags: list[str],
    priority: int,
    style: str = "none",
    estimate_s: float = 950.0,
) -> CampaignEntry:
    out_rel = f"{tier}/{profile}/{pair_id}/seed_{seed}"
    entry_id = f"{tier}__{profile}__{pair_id}__s{seed}"
    return CampaignEntry(
        entry_id=entry_id,
        tier=tier,
        profile=profile,
        pair_id=pair_id,
        seed=seed,
        flags=tuple(flags),
        out_rel=out_rel,
        priority=priority,
        style=style,
        estimate_s=estimate_s,
    )


REFERENCE_COMPATIBLE_PAIR_IDS = (
    "pine_chandelier",
    "crown_octopus",
    "volcano_bouquet",
    "lighthouse_goblet",
    "gown_jellyfish",
)
REFERENCE_CALIBRATION_PAIR_ID = "giraffe_penguin_calibration"
REFERENCE_CONTROL_PAIR_ID = "locomotive_eye_control"
REFERENCE_SEEDS = (11, 23, 37, 53, 71, 89)


# --- The 60-hour window matrix -------------------------------------------
#
# Every value below is a decision taken from the pre-window measurements under
# .local/illusion-reliability/campaigns/prewindow/. Changing one changes the
# plan SHA, which is the point: the plan file IS the record of the decision.
#
# The window asks how often the recipe produces a usable illusion, so coverage
# is the deliverable. The corpus is therefore wide, and the per-cell budget is
# set from where quality stopped improving on a pair nobody had run.
#
# A3 measured that it does not stop at 2,000. On lighthouse_goblet, quality
# improved monotonically through 5,000 steps. The calibration smoke saturating
# by 2,000 was the paper's own easiest pair, not the general case, so an earlier
# plan to cut to 3,000 would have under-baked every cell and understated yield
# by blaming pairs for the schedule. 5,000 steps at 4 seeds and 5,000 at 6 seeds
# cost the same ~50 GPU-hours; well-baked cells win because a thin cell answers
# a different question.
WINDOW_SDS_STEPS = 5_000
WINDOW_DREAM_ROUNDS = 8
# A2: the only wording with a human-approved cell behind it, and the only one
# that delivered its own medium on every pair tried. Monochrome also removes the
# colour agreement the two flip views would otherwise have to negotiate.
WINDOW_STYLE = "reference_sketch"
# A4: 512px native primes cost 3.3x total (2665s against 810s, Dream alone
# 6.8x) for no visible gain. The paper's 256px network stands.
WINDOW_PRIME_RESOLUTION: int | None = None
# A5 and A6: joint Dream Targets are an AXIS, not a setting.
#
# A5 showed joint targets rescuing crown_octopus from a neon collapse at no cost,
# which is issue #134's mechanism working as designed. A6 then ran the same
# config on the calibration pair and lost the penguin entirely: at SDS-5000 view
# 2 still held penguin structure, and Dream round 1 turned it into a second
# giraffe. stag_oak, meanwhile, came out clean.
#
# Joint Dream reconciles both views into ONE consensus image. That amplifies a
# pair whose two subjects can genuinely BE one image (crown/octopus are both
# radial-with-arms, stag antlers read as oak branches) and destroys a pair whose
# subjects cannot (a giraffe head and a penguin), by collapsing onto whichever
# subject holds the stronger score field. It causes the very failure it was
# built to prevent, on the pairs it does not suit.
#
# Which pairs those are is not knowable in advance, so the window measures both
# modes on every pair and yield is read as the better of the two.
#
# Independent comes FIRST. It is the mode the calibration smoke was reviewed
# under, so the first cell is a rig check that can actually fail informatively;
# leading with joint would make cell 1 reproduce A6's known collapse and tell
# nobody anything. It also means a window cut short keeps the validated mode.
WINDOW_DREAM_MODES = (False, True)
# Three seeds across two Dream modes. Adding the mode axis costs seed
# resolution, and that is the right trade: a tight rate estimate for a mode that
# is wrong for the pair answers nothing, while the better of two modes is the
# answer a product would actually ship.
WINDOW_SEEDS = (11, 23, 37)
# A3 measured 1750s for a 5,000-step cell with eight Dream rounds; A5 showed
# joint Dream adds nothing. Rounded up so the deadline reserve stays honest.
WINDOW_CELL_ESTIMATE_S = 1_800.0
# Controls are emitted after this many seed blocks rather than at the very end,
# so a window that runs short loses sweep tail rather than the comparison the
# sweep is measured against. One block covers both Dream modes at seed 11, so
# the controls land near 30h and survive almost any window.
WINDOW_CONTROLS_AFTER_SEED_BLOCKS = 1
# A budget control at the paper's full 10,000 steps, to show on this window's own
# evidence whether 5,000 left anything on the table. Runs last: the pre-window
# ladder already answered it once, on one pair.
WINDOW_CONTROL_SDS_STEPS = 10_000
WINDOW_CONTROL_PAIRS = ("crown_octopus", "stag_oak")
WINDOW_CONTROL_SEEDS = (11,)
WINDOW_CONTROL_ESTIMATE_S = 3_450.0
# Pairs carried over from the legacy oil corpus because human review has already
# ruled on them. Two of these are the acceptance gate's own control pairs and two
# more were called out as the strongest of the 2026-07-19 curation, so running
# them under the new recipe is the only way to say whether it beats what exists
# rather than merely producing something. walrus_ladybug stays excluded by
# standing decision.
WINDOW_KNOWN_VERDICT_PAIR_IDS = (
    "dog_sloth",
    "mountain_valley",
    "elephant_swan",
    "moose_butterfly",
    "fox_rabbit",
    "squirrel_pelican",
    "gorilla_starfish",
)


def _window_pair_ids() -> tuple[str, ...]:
    """Every pair the window sweeps, widest axis first.

    docs/illusions.md calls prompts "the biggest lever by far", and issue #138
    already curated a pairing-rule corpus that no GPU cell has ever run. The
    earlier plan sampled five pairs; this samples the axis, and carries enough
    already-judged pairs to anchor the new recipe against the old one.
    """
    from worker.illusion_experiment import PAIRING_RULES_PAIRS

    return (
        REFERENCE_CALIBRATION_PAIR_ID,
        *(pair.pair_id for pair in PAIRING_RULES_PAIRS),
        *REFERENCE_COMPATIBLE_PAIR_IDS,
        *WINDOW_KNOWN_VERDICT_PAIR_IDS,
        REFERENCE_CONTROL_PAIR_ID,
    )


def _window_flags(sds_steps: int, *, dream_joint: bool = True) -> list[str]:
    flags = [
        "--experimental-recipe",
        "author_reference",
        "--collect-diagnostics",
        "--skip-clip",
        "--sds-steps",
        str(sds_steps),
        "--dream-rounds",
        str(WINDOW_DREAM_ROUNDS),
    ]
    if dream_joint:
        flags.append("--dream-joint")
    if WINDOW_PRIME_RESOLUTION is not None:
        flags += ["--prime-resolution", str(WINDOW_PRIME_RESOLUTION)]
    return flags


def _window_controls(priority: int) -> tuple[list[CampaignEntry], int]:
    """The budget comparison. Duplicates a sweep cell's pair, seed and Dream mode
    at the paper's full 10,000 steps, so the comparison is direct.

    There is no separate joint-Dream control any more: both Dream modes are full
    arms of the sweep, which is a stronger test than two extra cells."""
    entries: list[CampaignEntry] = []
    for seed in WINDOW_CONTROL_SEEDS:
        for pair_id in WINDOW_CONTROL_PAIRS:
            entries.append(
                _entry(
                    tier="window",
                    profile="budget_control_10k",
                    pair_id=pair_id,
                    seed=seed,
                    flags=_window_flags(WINDOW_CONTROL_SDS_STEPS),
                    priority=priority,
                    style=WINDOW_STYLE,
                    estimate_s=WINDOW_CONTROL_ESTIMATE_S,
                )
            )
            priority += 1
    return entries, priority


def build_window() -> list[CampaignEntry]:
    """Breadth-first yield sweep for the unattended window.

    Ordering is breadth-first by seed so the matrix degrades gracefully: every
    pair has one seed before any pair has two. A window that ends early still
    answers "which pairs work at all" rather than "these three pairs work".

    The window's length is a launch parameter, not a property of this matrix.
    Sizing the seed count generously is therefore close to free: the tail is
    what a short window drops. Controls are emitted mid-sweep so that tail is
    sweep cells rather than the comparisons the sweep is measured against.
    """
    entries: list[CampaignEntry] = []
    priority = 0
    pair_ids = _window_pair_ids()

    controls_emitted = False
    for block, seed in enumerate(WINDOW_SEEDS):
        if block == WINDOW_CONTROLS_AFTER_SEED_BLOCKS:
            control_entries, priority = _window_controls(priority)
            entries.extend(control_entries)
            controls_emitted = True
        for joint in WINDOW_DREAM_MODES:
            mode = "joint" if joint else "independent"
            for pair_id in pair_ids:
                # The very first cell is the rig check: the known-good pair, at
                # the window's own settings. A6 is why this exists. It caught
                # joint Dream destroying this exact pair, in one cell.
                anchor = (
                    pair_id == REFERENCE_CALIBRATION_PAIR_ID
                    and seed == WINDOW_SEEDS[0]
                    and joint == WINDOW_DREAM_MODES[0]
                )
                entries.append(
                    _entry(
                        tier="window",
                        profile="anchor" if anchor else f"sweep_{mode}",
                        pair_id=pair_id,
                        seed=seed,
                        flags=_window_flags(WINDOW_SDS_STEPS, dream_joint=joint),
                        priority=priority,
                        style=WINDOW_STYLE,
                        estimate_s=WINDOW_CELL_ESTIMATE_S,
                    )
                )
                priority += 1
    if not controls_emitted:
        control_entries, priority = _window_controls(priority)
        entries.extend(control_entries)

    return entries


# --- Window 2: fork the Dream arms from one SDS state --------------------
#
# Window 1 measured that SDS is not reproducible on this rig: across its 90
# (pair, seed) groups the independent and joint arms share their whole SDS
# configuration and ZERO of 90 SDS-end images are pixel-identical (mean absolute
# difference 0.3012). A neg-off cell against a neg-on cell therefore compared two
# SDS states as well as two Dream settings. Block A instead runs SDS once per
# (pair, style, seed) and forks its four Dream arms from that identical state, so
# each contrast differs in exactly the thing under test - and costs one SDS phase
# instead of four.
WINDOW2_SDS_STEPS = 5_000
# One Dream round. Window 1's ratings condemned the late rounds, and
# strength_schedule() would render "2 rounds" as [0.95, 0.05], whose round 2
# carries old round 8's strength: the very thing that was condemned.
WINDOW2_DREAM_ROUNDS = 1
# reference_sketch, spelled out: the code has three pencil templates and only
# this one is window 1's validated wording (illusions.py STYLE_TEMPLATES).
WINDOW2_STYLES = ("reference_sketch", "oil")
WINDOW2_SEEDS = (11, 23, 37)
WINDOW2_DEPTH_SEED = 53
# Desired-medium terms are deliberately absent: the positive prompt is an "hb
# pencil sketch", and Perp-Neg reports ordinary negative prompting failing when
# the positive and negative concepts overlap.
WINDOW2_NEGATIVE_PROMPT = (
    "hand, fingers, desk, table, paper edge, torn paper, frame, border, "
    "sketchbook, watermark, signature, text, photograph of a drawing"
)
# The four Dream arms of one base: {negative off, on} x {independent, joint}.
# Independent and neg-off comes first, so arm 1 of cell 1 is window 1's
# configuration with only the Dream-round change.
WINDOW2_ARMS: tuple[tuple[str, str, str], ...] = (
    ("neg_off_indep", "indep", "off"),
    ("neg_on_indep", "indep", "on"),
    ("neg_off_joint", "joint", "off"),
    ("neg_on_joint", "joint", "on"),
)
# From campaigns/window/review/corpus-decisions.json.
WINDOW2_PROVEN = (
    "giraffe_penguin_calibration",
    "moose_butterfly",
    "eagle_phoenix",
    "elephant_swan",
    "wolf_raven",
    "deer_turtle",
)
WINDOW2_SENTINEL_PAIRS = (
    "giraffe_penguin_calibration",
    "moose_butterfly",
    "wolf_raven",
    "elephant_swan",
)
WINDOW2_RESCUE = ("koi_moon", "galleon_whale", "hummingbird_fuchsia")
WINDOW2_REST = (
    "ballerina_leaf",
    "bear_salmon",
    "crown_octopus",
    "dog_sloth",
    "fox_rabbit",
    "gown_jellyfish",
    "heron_swan",
    "horse_wave",
    "lighthouse_goblet",
    "locomotive_eye_control",
    "mountain_valley",
    "octopus_camel",
    "penguin_bat",
    "pine_chandelier",
    "rabbit_fox",
    "squirrel_pelican",
    "stag_oak",
    "volcano_bouquet",
)
# Genuine truncation of window 1's eight-round schedule, as explicit strengths:
# its first two values, not two rounds spread across the same endpoints.
WINDOW2_TRUNCATED_STRENGTHS = ("0.95", "0.821")
WINDOW2_FULL_DREAM_ROUNDS = 8
# 1,608s SDS + 4 x 40s arm + 60s overhead for a forked base; single-arm cells
# carry the same SDS phase with one Dream round, and B's cells are costed for
# their own schedules rather than as one-round cells.
WINDOW2_BASE_ESTIMATE_S = 1_828.0
WINDOW2_CELL_ESTIMATE_S = 1_690.0
WINDOW2_TRUNCATED_ESTIMATE_S = 1_713.0
WINDOW2_FULL_DREAM_ESTIMATE_S = 1_841.0


# --- Window 3: acquisition, not settings ---------------------------------
#
# Windows 1 and 2 settled the recipe, so nothing here is under test. The corpus
# is the binding constraint on yield: 144 of window 2's 206 observations came
# from six already-proven pairs. This window spends its hours on pairs no window
# has run, at the settled configuration.
#
# Two arms forked from one SDS state, joint first. Joint is the settled default
# (38 up, 12 down, p=0.0003) but collapses some pairs, and which pairs is not
# predictable: window 1 varied ONLY the seed, and a single seed missed 24 of the
# 72 seed-cells belonging to a pair that demonstrably produced a keeper at some
# other seed - one seed finds about 0.67 of the pairs that can work. So a second
# arm off the same snapshot is cheaper than any predictor could be: about 22s
# against the 1,556s SDS phase it reuses.
WINDOW3_SDS_STEPS = WINDOW2_SDS_STEPS
WINDOW3_DREAM_ROUNDS = 1
# oil, not window 1's reference_sketch, and NOT because it yields more. At the
# unit this window actually produces - one base, best of its two arms, negative
# off - the two media tie: 7 clean keepers against 6 of 18, McNemar p = 1.00. Oil
# is chosen because it ties while producing 0 disqualifying frames against
# reference_sketch's 31 in 72 observations, and delivering colour in 78/78 arms.
# Those two were the human reviewer's actual complaints. The per-observation
# table (oil 25, reference_sketch 14) overstates this: two arms of one base share
# an SDS state, so they are not independent observations.
WINDOW3_STYLE = "oil"
# The anchor exercises the fallback arm by construction: at spec_hash
# fd46cd54684e, negative off, its independent arm beat joint 5 to 0 from one
# shared SDS state, with joint also moving from a minor to a disqualifying frame.
# Its score is not a gate - one seed on a working pair misses a third of the time,
# so gating on one score would gate on noise. What it gates is structural.
WINDOW3_ANCHOR_PAIR = "wolf_raven"
WINDOW3_ANCHOR_SEED = 11
# Seeds are drawn round-robin along the family-interleaved order, so each seed
# lands in several families rather than concentrating in one. Window 2 leaned on
# seed 11, and a corpus-wide constant seed confounds pair effects with that draw.
WINDOW3_SEED_POOL = (
    11,
    23,
    37,
    53,
    71,
    89,
    101,
    113,
    131,
    149,
    167,
    181,
    199,
    211,
    233,
    251,
    269,
)
# Execution order is a fixed hash permutation rather than corpus order, so a
# window cut short leaves an unbiased sample of all four families instead of
# whichever family happens to sort first.
WINDOW3_ORDER_SALT = "window3-2026-08"
WINDOW3_ARMS: tuple[tuple[str, str, str], ...] = (
    ("joint", "joint", "off"),
    ("indep", "indep", "off"),
)
# 1,556s SDS measured in window 2 + 2 x 22s arm + ~60s load and lock. The anchor
# smoke came in at 1,632s, so this carries about 100s of margin per base.
WINDOW3_BASE_ESTIMATE_S = 1_736.0

# Block W: the wording screen, added when the window grew to 70h. It runs LAST,
# so an acquisition overrun truncates the bonus and never the deliverable.
#
# Neither validated wording wins the trade that caps the product: reference_sketch
# reads better raw (35 of 72 against 25) and loses 31 of 72 to its own frames,
# while oil produces 0 frames in 78 and only ties on the clean endpoint (7 against
# 6 per base, McNemar p=1.00). See STYLE_TEMPLATES for the mechanism each
# candidate tests.
#
# Same pairs and same seeds as window 2's negative-off block A bases, so each
# candidate is compared against both incumbents on identical ground rather than
# against a remembered number.
WINDOW3_WORDINGS = ("monochrome_oil", "charcoal")
WINDOW3_WORDING_PAIRS = WINDOW2_PROVEN
WINDOW3_WORDING_SEEDS = WINDOW2_SEEDS


def _window2_flags(
    *,
    dream_joint: bool = False,
    dream_rounds: int = WINDOW2_DREAM_ROUNDS,
    strengths: tuple[str, ...] = (),
    arms: tuple[tuple[str, str, str], ...] = (),
) -> list[str]:
    flags = [
        "--experimental-recipe",
        "author_reference",
        "--collect-diagnostics",
        "--skip-clip",
        "--sds-steps",
        str(WINDOW2_SDS_STEPS),
        "--dream-rounds",
        str(dream_rounds),
    ]
    for strength in strengths:
        flags += ["--dream-strength", strength]
    if dream_joint:
        flags.append("--dream-joint")
    if arms:
        # Only forked bases carry a negative prompt, so every unforked cell is
        # window 1's recipe with one Dream round and nothing else changed.
        flags += ["--negative-prompt", WINDOW2_NEGATIVE_PROMPT]
        for name, mode, negative in arms:
            flags += ["--dream-arm", f"{name}:{mode}:{negative}"]
    return flags


def build_window2() -> list[CampaignEntry]:
    """98 entries producing 206 observations, in execution order A, B, R, C, D.

    Order is deliberate: the factorial that answers both review complaints
    completes first, the schedule sentinel and the oil rescue next, and the
    truncatable tail is the breadth block then the depth block, which test
    nothing new.
    """
    entries: list[CampaignEntry] = []
    priority = 0

    def add(
        *, profile: str, pair_id: str, seed: int, flags: list[str], style: str, estimate_s: float
    ) -> None:
        nonlocal priority
        entries.append(
            _entry(
                tier="window2",
                profile=profile,
                pair_id=pair_id,
                seed=seed,
                flags=flags,
                priority=priority,
                style=style,
                estimate_s=estimate_s,
            )
        )
        priority += 1

    # A: 6 proven pairs x 2 styles x 3 seeds, four forked Dream arms each.
    for seed in WINDOW2_SEEDS:
        for style in WINDOW2_STYLES:
            for pair_id in WINDOW2_PROVEN:
                anchor = (
                    pair_id == WINDOW2_PROVEN[0]
                    and style == WINDOW2_STYLES[0]
                    and seed == WINDOW2_SEEDS[0]
                )
                add(
                    profile="a_anchor" if anchor else f"a_forked_{style}",
                    pair_id=pair_id,
                    seed=seed,
                    flags=_window2_flags(arms=WINDOW2_ARMS),
                    style=style,
                    estimate_s=WINDOW2_BASE_ESTIMATE_S,
                )

    # B: Dream schedule sentinel, fully pinned: reference_sketch, neg off,
    # independent, seed 11. Truncation first, then the full eight rounds.
    sentinel: tuple[tuple[str, int, tuple[str, ...], float], ...] = (
        ("b_truncated_2", 2, WINDOW2_TRUNCATED_STRENGTHS, WINDOW2_TRUNCATED_ESTIMATE_S),
        ("b_full_8", WINDOW2_FULL_DREAM_ROUNDS, (), WINDOW2_FULL_DREAM_ESTIMATE_S),
    )
    for profile, rounds, strengths, estimate in sentinel:
        for pair_id in WINDOW2_SENTINEL_PAIRS:
            add(
                profile=profile,
                pair_id=pair_id,
                seed=WINDOW2_SEEDS[0],
                flags=_window2_flags(dream_rounds=rounds, strengths=strengths),
                style=WINDOW2_STYLES[0],
                estimate_s=estimate,
            )

    # R, C, D: single-arm cells, independent mode first so a window cut short
    # keeps the validated mode.
    tail: tuple[tuple[str, tuple[str, ...], str, int], ...] = (
        ("r_oil", WINDOW2_RESCUE, "oil", WINDOW2_SEEDS[0]),
        ("c", WINDOW2_REST, WINDOW2_STYLES[0], WINDOW2_SEEDS[0]),
        ("d", WINDOW2_PROVEN, WINDOW2_STYLES[0], WINDOW2_DEPTH_SEED),
    )
    for block, pairs, style, seed in tail:
        for joint in (False, True):
            for pair_id in pairs:
                add(
                    profile=f"{block}_{'joint' if joint else 'indep'}",
                    pair_id=pair_id,
                    seed=seed,
                    flags=_window2_flags(dream_joint=joint),
                    style=style,
                    estimate_s=WINDOW2_CELL_ESTIMATE_S,
                )

    return entries


def _window3_flags() -> list[str]:
    # No --negative-prompt: it was rejected by its rule (frame improved in 8.5%
    # of paired bases against 50% required), and oil needs no frame fix. No
    # --prime-resolution either: 256 is already the author_reference default.
    flags = [
        "--experimental-recipe",
        "author_reference",
        "--collect-diagnostics",
        "--skip-clip",
        "--sds-steps",
        str(WINDOW3_SDS_STEPS),
        "--dream-rounds",
        str(WINDOW3_DREAM_ROUNDS),
    ]
    for name, mode, negative in WINDOW3_ARMS:
        flags += ["--dream-arm", f"{name}:{mode}:{negative}"]
    return flags


def _hash_order(pair_ids: list[str]) -> list[str]:
    """Deterministic permutation. md5 is stable across Python versions, where
    random.shuffle's algorithm is only stable in practice."""
    return sorted(
        pair_ids,
        key=lambda pair_id: hashlib.md5(
            f"{WINDOW3_ORDER_SALT}:{pair_id}".encode(), usedforsecurity=False
        ).hexdigest(),
    )


def window3_order() -> list[tuple[str, int]]:
    """The frozen (pair_id, seed) sequence of the breadth block, stratified.

    Both the order and the seeds are stratified BY FAMILY. An unstratified hash
    permutation over the whole corpus put 20 scene pairs and 8 object pairs in
    the first 60 executions, and gave seed 181 five upright pairs and no object
    or scene pair at all. Neither breaks total yield if all 97 finish, but both
    make a truncated window unrepresentative and confound any family comparison.

    Stratifying: permute within each family, then round-robin across families, so
    any prefix of the result is close to balanced. Seeds are assigned along that
    interleaved order, which spreads each seed across families too.
    """
    within = {
        family: _hash_order([pair.pair_id for pair in pairs])
        for family, pairs in BREADTH_FAMILIES.items()
    }
    families = _hash_order(list(within))
    interleaved: list[str] = []
    for index in range(max(len(v) for v in within.values())):
        for family in families:
            if index < len(within[family]):
                interleaved.append(within[family][index])
    return [
        (pair_id, WINDOW3_SEED_POOL[index % len(WINDOW3_SEED_POOL)])
        for index, pair_id in enumerate(interleaved)
    ]


def build_window3() -> list[CampaignEntry]:
    """134 bases producing 268 observations: 98 acquisition then 36 wording.

    Acquisition, so one seed per pair rather than two. Expected distinct
    successful pairs is 2Np for one seed on 2N pairs against N(2p - p^2) for two
    seeds on N, and 2Np > 2Np - Np^2 always. Replication buys ranking
    reliability, which this window does not need; that is exactly why a pair
    that misses at its single seed is recorded as not obtained, never as dead.
    """
    entries = [
        _entry(
            tier="window3",
            profile="anchor",
            pair_id=WINDOW3_ANCHOR_PAIR,
            seed=WINDOW3_ANCHOR_SEED,
            flags=_window3_flags(),
            priority=0,
            style=WINDOW3_STYLE,
            estimate_s=WINDOW3_BASE_ESTIMATE_S,
        )
    ]
    for priority, (pair_id, seed) in enumerate(window3_order(), start=1):
        entries.append(
            _entry(
                tier="window3",
                profile="breadth",
                pair_id=pair_id,
                seed=seed,
                flags=_window3_flags(),
                priority=priority,
                style=WINDOW3_STYLE,
                estimate_s=WINDOW3_BASE_ESTIMATE_S,
            )
        )

    # Block W last: 36 bases, 2 wordings x 6 proven pairs x 3 seeds. At n=18 per
    # wording this is a SCREEN, not an adoption test - window 2 showed n=18 can
    # only resolve a large effect (7 against 6 gave McNemar p=1.00). A candidate
    # that beats both incumbents advances to replication in a later window and
    # changes no default here.
    priority = len(entries)
    for wording in WINDOW3_WORDINGS:
        for seed in WINDOW3_WORDING_SEEDS:
            for pair_id in WINDOW3_WORDING_PAIRS:
                entries.append(
                    _entry(
                        tier="window3",
                        profile=f"w_{wording}",
                        pair_id=pair_id,
                        seed=seed,
                        flags=_window3_flags(),
                        priority=priority,
                        style=wording,
                        estimate_s=WINDOW3_BASE_ESTIMATE_S,
                    )
                )
                priority += 1
    return entries


def build_reference_author_60h() -> list[CampaignEntry]:
    """36-cell breadth-first author-recipe matrix for the unattended window."""
    pair_seeds: dict[str, tuple[int, ...]] = {
        REFERENCE_CALIBRATION_PAIR_ID: REFERENCE_SEEDS[:4],
        **{pair_id: REFERENCE_SEEDS for pair_id in REFERENCE_COMPATIBLE_PAIR_IDS},
        REFERENCE_CONTROL_PAIR_ID: REFERENCE_SEEDS[:2],
    }
    pair_order = (
        REFERENCE_CALIBRATION_PAIR_ID,
        *REFERENCE_COMPATIBLE_PAIR_IDS,
        REFERENCE_CONTROL_PAIR_ID,
    )
    entries: list[CampaignEntry] = []
    priority = 0
    for seed in REFERENCE_SEEDS:
        for pair_id in pair_order:
            if seed not in pair_seeds[pair_id]:
                continue
            entries.append(
                _entry(
                    tier="reference60h",
                    profile="author_reference",
                    pair_id=pair_id,
                    seed=seed,
                    flags=[
                        "--experimental-recipe",
                        "author_reference",
                        "--collect-diagnostics",
                        "--skip-clip",
                    ],
                    priority=priority,
                    estimate_s=5280,
                )
            )
            priority += 1
    return entries


def build_early_dream_backup() -> list[CampaignEntry]:
    """Cheap fallback emphasizing the retained early-Dream observation."""
    seeds = (*REFERENCE_SEEDS, 107, 131)
    pair_ids = (REFERENCE_CALIBRATION_PAIR_ID, *REFERENCE_COMPATIBLE_PAIR_IDS)
    flags = [
        "--sds-objective",
        "legacy",
        "--round-robin",
        "--sds-steps",
        "500",
        "--dream-lr",
        "3e-3",
        "--dream-rounds",
        "2",
        "--dream-strength",
        "0.95",
        "--dream-strength",
        "0.50",
        "--collect-diagnostics",
        "--skip-clip",
    ]
    entries: list[CampaignEntry] = []
    priority = 0
    for seed in seeds:
        for pair_id in pair_ids:
            entries.append(
                _entry(
                    tier="early_dream_backup",
                    profile="legacy_rr_d1",
                    pair_id=pair_id,
                    seed=seed,
                    flags=flags,
                    priority=priority,
                    estimate_s=600,
                )
            )
            priority += 1
    return entries


def build_pilot_wave1() -> list[CampaignEntry]:
    entries: list[CampaignEntry] = []
    for profile, flags in WAVE1_PROFILES:
        for pair in SCREEN_PAIRS:
            entries.append(
                _entry(
                    tier="wave1",
                    profile=profile,
                    pair_id=pair.pair_id,
                    seed=2,
                    flags=list(flags) + ["--collect-diagnostics", "--skip-clip"],
                    priority=10,
                )
            )
    return entries


def build_pilot_wave2(base_flags: list[str]) -> list[CampaignEntry]:
    """Build four selected Wave-2 profiles from the declared base selection."""
    candidates: list[tuple[str, list[str]]] = [
        ("B_sqrt_anneal", base_flags + ["--sqrt-timestep-anneal"]),
        ("B_round_robin", base_flags + ["--round-robin"]),
        ("B_dream_joint", base_flags + ["--dream-joint"]),
        ("B_sqrt_anneal_joint", base_flags + ["--sqrt-timestep-anneal", "--dream-joint"]),
        ("B_rr_joint", base_flags + ["--round-robin", "--dream-joint"]),
    ]
    # Legacy plus dream_joint was already measured in Wave 1, so retain four
    # novel ablations for the default campaign.
    selected_profiles = [candidate for candidate in candidates if candidate[0] != "B_dream_joint"][
        :4
    ]
    out: list[CampaignEntry] = []
    for name, flags in selected_profiles:
        for pair in SCREEN_PAIRS:
            out.append(
                _entry(
                    tier="wave2",
                    profile=name,
                    pair_id=pair.pair_id,
                    seed=2,
                    flags=list(flags) + ["--collect-diagnostics", "--skip-clip"],
                    priority=20,
                )
            )
    return out


def build_away_tiers(finalist_profiles: list[tuple[str, list[str]]]) -> list[CampaignEntry]:
    """Build ordered away tiers 1-6. finalist_profiles are three non-legacy winners."""
    entries: list[CampaignEntry] = []
    all_pairs = FINAL_PAIRS
    non_screen = [p for p in FINAL_PAIRS if p.pair_id not in {s.pair_id for s in SCREEN_PAIRS}]
    # Tier 1: legacy + 3 finalists x 8 pairs x seeds 0,1
    tier1_profiles = [("legacy", ["--sds-objective", "legacy"])] + finalist_profiles[:3]
    for profile, flags in tier1_profiles:
        for pair in all_pairs:
            for seed in (0, 1):
                entries.append(
                    _entry(
                        tier="tier1",
                        profile=profile,
                        pair_id=pair.pair_id,
                        seed=seed,
                        flags=list(flags) + ["--collect-diagnostics", "--skip-clip"],
                        priority=100,
                    )
                )
    # Tier 2: all ten pilot profiles on four non-screen pairs at seed 2
    pilot = WAVE1_PROFILES + [(n, f) for n, f in finalist_profiles[:4]]
    seen: set[str] = set()
    for profile, flags in pilot:
        if profile in seen:
            continue
        seen.add(profile)
        for pair in non_screen:
            entries.append(
                _entry(
                    tier="tier2",
                    profile=profile,
                    pair_id=pair.pair_id,
                    seed=2,
                    flags=list(flags) + ["--collect-diagnostics", "--skip-clip"],
                    priority=200,
                )
            )
    # Tier 3: legacy CFG 50, dream learning rate 1e-2, classic SD15 dream.
    for profile, flags in [
        ("legacy_cfg_50", ["--sds-objective", "legacy", "--sds-guidance", "50"]),
        ("dream_lr_1e2", ["--sds-objective", "legacy", "--dream-lr", "1e-2"]),
        ("dream_sd15", ["--sds-objective", "legacy", "--dream-model", "none"]),
    ]:
        for pair in all_pairs:
            entries.append(
                _entry(
                    tier="tier3",
                    profile=profile,
                    pair_id=pair.pair_id,
                    seed=2,
                    flags=list(flags) + ["--collect-diagnostics", "--skip-clip"],
                    priority=300,
                )
            )
    # Tier 4: styles on subjects x 4 screen pairs x seeds 0,1 using B.
    b_flags = finalist_profiles[0][1] if finalist_profiles else ["--sds-objective", "legacy"]
    for style in ("coherent_oil", "pencil", "editorial"):
        profile = f"style_{style}"
        for pair in SCREEN_PAIRS:
            for seed in (0, 1):
                entries.append(
                    _entry(
                        tier="tier4",
                        profile=profile,
                        pair_id=pair.pair_id,
                        seed=seed,
                        flags=list(b_flags)
                        + ["--style", style, "--collect-diagnostics", "--skip-clip"],
                        priority=400,
                        style=style,
                    )
                )
    # Tier 5: layout ablations on dog_sloth + mountain_valley seed 0
    for profile, extra in [
        ("layout_default", []),
        ("layout_vae_slicing", ["--enable-vae-slicing"]),
        ("layout_channels_last", ["--channels-last"]),
        ("layout_both", ["--enable-vae-slicing", "--channels-last"]),
    ]:
        for pair_id in ("dog_sloth", "mountain_valley"):
            entries.append(
                _entry(
                    tier="tier5",
                    profile=profile,
                    pair_id=pair_id,
                    seed=0,
                    flags=list(b_flags) + extra + ["--collect-diagnostics", "--skip-clip"],
                    priority=500,
                )
            )
    # Tier 6 optional: tier3 profiles x 4 screen x seeds 0,1
    for profile, flags in [
        ("legacy_cfg_50", ["--sds-objective", "legacy", "--sds-guidance", "50"]),
        ("dream_lr_1e2", ["--sds-objective", "legacy", "--dream-lr", "1e-2"]),
        ("dream_sd15", ["--sds-objective", "legacy", "--dream-model", "none"]),
    ]:
        for pair in SCREEN_PAIRS:
            for seed in (0, 1):
                entries.append(
                    _entry(
                        tier="tier6",
                        profile=profile,
                        pair_id=pair.pair_id,
                        seed=seed,
                        flags=list(flags) + ["--collect-diagnostics", "--skip-clip"],
                        priority=600,
                    )
                )
    return entries


def build_full_plan(
    *,
    evidence_root: Path,
    model_id: str,
    dream_model_id: str,
    include_away: bool = True,
    finalist_profiles: list[tuple[str, list[str]]] | None = None,
) -> CampaignPlan:
    finalists = finalist_profiles or [
        ("finalist_a", ["--sds-objective", "legacy"]),
        ("finalist_b", ["--sds-objective", "weighted_sds"]),
        ("finalist_c", ["--sds-objective", "csd"]),
    ]
    entries = build_pilot_wave1()
    entries.extend(build_pilot_wave2(["--sds-objective", "legacy"]))
    if include_away:
        entries.extend(build_away_tiers(finalists))
    # Deduplicate by spec_hash keeping first (higher priority already ordered)
    seen_hash: set[str] = set()
    unique: list[CampaignEntry] = []
    for entry in entries:
        digest = entry.spec_hash(
            model_id=model_id, dream_model_id=dream_model_id, optimizer_fingerprint=""
        )
        if digest in seen_hash:
            continue
        seen_hash.add(digest)
        unique.append(entry)
    return CampaignPlan(
        campaign_id=f"illusion-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        git_sha=git_sha(),
        created_at=datetime.now(timezone.utc).isoformat(),
        evidence_root=str(evidence_root),
        # Offline cells need the cached snapshot directory, not the Hub id: this
        # machine's snapshot is incomplete and HF_HUB_OFFLINE refuses it outright.
        model_id=local_snapshot(model_id),
        dream_model_id=local_snapshot(dream_model_id),
        entries=unique,
    )


def plan_counts(plan: CampaignPlan) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in plan.entries:
        counts[entry.tier] = counts.get(entry.tier, 0) + 1
    counts["total"] = len(plan.entries)
    return counts


def is_completed_matching(
    out_dir: Path, expected_sha: str, expected_spec: str, expected_plan_sha: str | None = None
) -> bool:
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError:
        return False
    if manifest.get("status") != "completed":
        return False
    if manifest.get("git_sha") != expected_sha:
        return False
    if manifest.get("spec_hash") != expected_spec:
        return False
    if expected_plan_sha is not None and manifest.get("plan_sha") != expected_plan_sha:
        return False
    return is_completed_run(out_dir)


def _entry_root(plan: CampaignPlan, entry: CampaignEntry) -> Path:
    return Path(plan.evidence_root) / entry.out_rel


def _attempts(entry_root: Path) -> list[Path]:
    return sorted(
        (path for path in entry_root.glob("attempt_*") if path.is_dir()),
        key=lambda path: path.name,
    )


def _next_attempt(entry_root: Path) -> tuple[int, Path]:
    number = 1
    while (entry_root / f"attempt_{number:03d}").exists():
        number += 1
    return number, entry_root / f"attempt_{number:03d}"


def _append_event(event: dict[str, Any]) -> None:
    # Journalling is optional telemetry, so it must never take the campaign down
    # with it. An unwritable events.jsonl used to raise straight through the
    # driver, killing a 47-hour run immediately AFTER a cell had completed fine.
    if not EVENTS_PATH.parent.is_dir():
        return
    try:
        with EVENTS_PATH.open("a") as handle:
            handle.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), **event}) + "\n")
    except OSError as exc:
        print(f"warning: could not append campaign event: {exc}", file=sys.stderr)


def _sample_telemetry() -> dict[str, Any]:
    sample: dict[str, Any] = {"ts": datetime.now(timezone.utc).isoformat()}
    try:
        out = subprocess.check_output(
            ["rocm-smi", "--showuse", "--showmeminfo", "vram", "--showtemp", "--showpower"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        sample["rocm_smi"] = out[-2000:]
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        sample["rocm_smi"] = None
    return sample


def run_entry(
    plan: CampaignPlan,
    entry: CampaignEntry,
    *,
    py: str,
    force_gpu: bool = False,
    dry_run: bool = False,
    plan_identity: str | None = None,
    timeout_cap_s: float | None = None,
) -> dict[str, Any]:
    entry_root = _entry_root(plan, entry)
    identity = plan_identity or plan.to_json()["plan_sha"]
    spec = entry.spec_hash(
        model_id=plan.model_id,
        dream_model_id=plan.dream_model_id,
        optimizer_fingerprint=plan.optimizer_fingerprint,
    )
    for previous in _attempts(entry_root):
        if is_completed_matching(previous, plan.git_sha, spec, identity):
            return {"entry_id": entry.entry_id, "status": "skipped_completed"}

    attempt, out = _next_attempt(entry_root)
    driver = entry_root / "driver" / f"attempt_{attempt:03d}"
    status_path = driver / "status.json"
    log_path = driver / "run.log"

    cmd = [
        str(repo_root() / "scripts" / "gpu-lock.sh"),
        *(["--force"] if force_gpu else []),
        "--",
        py,
        "-m",
        "worker.illusion_experiment",
        "run",
        "--name",
        entry.entry_id,
        "--type",
        "flip",
        "--pair-id",
        entry.pair_id,
        "--seed",
        str(entry.seed),
        "--model",
        plan.model_id,
        "--dream-model",
        plan.dream_model_id,
        "--style",
        entry.style,
        "--out",
        str(out),
        "--campaign-id",
        plan.campaign_id,
        "--spec-hash",
        spec,
        "--plan-sha",
        identity,
        "--optimizer-fingerprint",
        plan.optimizer_fingerprint,
        *list(entry.flags),
    ]
    status: dict[str, Any] = {
        "entry_id": entry.entry_id,
        "status": "running",
        "pid": None,
        "attempt": attempt,
        "out": str(out),
        "spec_hash": spec,
        "plan_sha": identity,
        "git_sha": plan.git_sha,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "cmd": cmd,
    }
    write_manifest_atomic(status_path, status)
    if dry_run:
        status["status"] = "dry_run"
        write_manifest_atomic(status_path, status)
        return status

    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root() / "worker")
    env["TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL"] = "1"
    env.setdefault("HF_HUB_OFFLINE", "1")

    def _attempt() -> dict[str, Any]:
        with log_path.open("w") as log_file:
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(repo_root()),
                start_new_session=True,
            )
            status["pid"] = proc.pid
            write_manifest_atomic(status_path, status)
            timeout_s = max(RUN_TIMEOUT_S, entry.estimate_s * 1.5)
            # --deadline-s was only a START gate, so a cell could begin with
            # ~2,210s left and run to its own 3,900s timeout, overrunning the
            # window by ~28 minutes. The caller caps it by the time left.
            if timeout_cap_s is not None:
                timeout_s = min(timeout_s, max(60.0, timeout_cap_s))
            deadline = time.monotonic() + timeout_s
            telemetry: list[dict[str, Any]] = []
            next_tel = time.monotonic()
            while True:
                rc = proc.poll()
                now = time.monotonic()
                if now >= next_tel:
                    telemetry.append(_sample_telemetry())
                    write_manifest_atomic(driver / "telemetry.json", {"samples": telemetry[-360:]})
                    next_tel = now + TELEMETRY_INTERVAL_S
                if rc is not None:
                    break
                if now >= deadline:
                    os.killpg(proc.pid, signal.SIGKILL)
                    status["status"] = "timeout"
                    status["error"] = f"exceeded {timeout_s:.0f}s"
                    write_manifest_atomic(status_path, status)
                    return status
                time.sleep(1.0)
            if rc == 0:
                status["status"] = "completed"
                # Exit 0 is the cell's own opinion. The evidence is the manifest
                # and both derived images (every arm's, for a forked base), so
                # validate them here rather than discovering it at review time.
                if not is_completed_matching(out, plan.git_sha, spec, identity):
                    status["status"] = "failed"
                    status["error"] = "invalid_output"
            elif rc == BUSY_EXIT_CODE:
                status["status"] = "busy"
                status["exit_code"] = rc
            else:
                status["status"] = "failed"
                status["exit_code"] = rc
                # Detect OOM in log
                log_text = log_path.read_text(errors="replace")[-4000:]
                if "out of memory" in log_text.lower() or "oom" in log_text.lower():
                    status["error"] = "oom"
            write_manifest_atomic(status_path, status)
            return status

    result = _attempt()
    _append_event(
        {"campaign_id": plan.campaign_id, "entry_id": entry.entry_id, "status": result["status"]}
    )
    return result


def _optimizer_fingerprint() -> str:
    optimizer = repo_root() / "worker" / "worker" / "illusions.py"
    return hashlib.sha256(optimizer.read_bytes()).hexdigest()[:16]


def _profile_map() -> dict[str, list[str]]:
    return {name: flags for name, flags in WAVE1_PROFILES}


def _select_wave2(base_flags: list[str], selected: str | None) -> list[CampaignEntry]:
    entries = build_pilot_wave2(base_flags)
    if selected is None:
        return entries
    wanted = selected.split(",")
    if len(wanted) != 4 or len(set(wanted)) != 4:
        raise ValueError("--wave2-profiles must name exactly four distinct profiles")
    available = {entry.profile for entry in entries}
    if not set(wanted) <= available:
        raise ValueError(f"unknown Wave-2 profile: expected one of {sorted(available)}")
    return [entry for entry in entries if entry.profile in wanted]


def _finalists(value: str | None) -> list[tuple[str, list[str]]]:
    profiles = _profile_map()
    names = (value or "weighted_sds,nfsd_7_5,dream_joint").split(",")
    if len(names) < 1:
        raise ValueError("--finalists cannot be empty")
    unknown = [name for name in names if name not in profiles]
    if unknown:
        raise ValueError(f"unknown finalist profiles: {unknown}")
    return [(name, profiles[name]) for name in names]


def _blocked_rotated(entries: list[CampaignEntry]) -> list[CampaignEntry]:
    """Keep phase blocks while rotating profiles between consecutive pairs."""
    by_profile: dict[str, list[CampaignEntry]] = {}
    for entry in entries:
        by_profile.setdefault(entry.profile, []).append(entry)
    profiles = list(by_profile)
    ordered: list[CampaignEntry] = []
    round_index = 0
    while any(by_profile.values()):
        for offset in range(len(profiles)):
            profile = profiles[(round_index + offset) % len(profiles)]
            if by_profile[profile]:
                ordered.append(by_profile[profile].pop(0))
        round_index += 1
    return ordered


def _shard(text: str) -> tuple[int, int]:
    """Parse ``I/K`` into a zero-based shard index and shard count."""
    index_text, _, count_text = text.partition("/")
    if not count_text:
        raise argparse.ArgumentTypeError("expected I/K, e.g. 0/2")
    index, count = int(index_text), int(count_text)
    if count < 1 or not 0 <= index < count:
        raise argparse.ArgumentTypeError(f"shard {text} out of range")
    return index, count


def build_phase_plan(
    *,
    phase: str,
    evidence_root: Path,
    model_id: str,
    dream_model_id: str,
    base_selection: str = "legacy",
    wave2_profiles: str | None = None,
    finalists: str | None = None,
) -> CampaignPlan:
    profiles = _profile_map()
    if phase == "wave1":
        entries = build_pilot_wave1()
    elif phase == "wave2":
        if base_selection not in profiles:
            raise ValueError(f"unknown --base-selection: {base_selection}")
        entries = _select_wave2(profiles[base_selection], wave2_profiles)
    elif phase == "away":
        entries = build_away_tiers(_finalists(finalists))
    elif phase == "reference60h":
        entries = build_reference_author_60h()
    elif phase == "window":
        entries = build_window()
    elif phase == "window2":
        entries = build_window2()
    elif phase == "window3":
        entries = build_window3()
    elif phase == "early-dream-backup":
        entries = build_early_dream_backup()
    else:
        raise ValueError(f"unknown phase: {phase}")
    plan = CampaignPlan(
        campaign_id=f"illusion-{phase}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        git_sha=git_sha(),
        created_at=datetime.now(timezone.utc).isoformat(),
        evidence_root=str(evidence_root),
        # Offline cells need the cached snapshot directory, not the Hub id: this
        # machine's snapshot is incomplete and HF_HUB_OFFLINE refuses it outright.
        model_id=local_snapshot(model_id),
        dream_model_id=local_snapshot(dream_model_id),
        optimizer_fingerprint=_optimizer_fingerprint(),
        entries=(
            entries
            if phase in ("reference60h", "window", "window2", "window3", "early-dream-backup")
            else _blocked_rotated(entries)
        ),
    )
    return plan


def load_plan(path: Path) -> tuple[CampaignPlan, str]:
    data = json.loads(path.read_text())
    actual_sha = plan_sha(data)
    if data.get("plan_sha") != actual_sha:
        raise ValueError("plan_sha does not match plan contents")
    entries = [
        CampaignEntry(
            entry_id=raw["entry_id"],
            tier=raw["tier"],
            profile=raw["profile"],
            pair_id=raw["pair_id"],
            seed=raw["seed"],
            flags=tuple(raw["flags"]),
            out_rel=raw["out_rel"],
            priority=raw["priority"],
            estimate_s=raw.get("estimate_s", 950.0),
            style=raw.get("style", "none"),
        )
        for raw in data["entries"]
    ]
    return (
        CampaignPlan(
            campaign_id=data["campaign_id"],
            git_sha=data["git_sha"],
            created_at=data["created_at"],
            evidence_root=data["evidence_root"],
            model_id=data["model_id"],
            dream_model_id=data["dream_model_id"],
            optimizer_fingerprint=data.get("optimizer_fingerprint", ""),
            entries=entries,
        ),
        actual_sha,
    )


def _command_status(plan: CampaignPlan, plan_identity: str) -> dict[str, int]:
    counts = {"completed": 0, "incomplete": 0, "missing": 0}
    for entry in plan.entries:
        spec = entry.spec_hash(
            model_id=plan.model_id,
            dream_model_id=plan.dream_model_id,
            optimizer_fingerprint=plan.optimizer_fingerprint,
        )
        attempts = _attempts(_entry_root(plan, entry))
        if any(is_completed_matching(path, plan.git_sha, spec, plan_identity) for path in attempts):
            counts["completed"] += 1
        elif attempts:
            counts["incomplete"] += 1
        else:
            counts["missing"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    plan_cmd = sub.add_parser("plan", help="write an immutable campaign plan JSON")
    plan_cmd.add_argument("--out", type=Path, required=True)
    plan_cmd.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    plan_cmd.add_argument("--model", default="stable-diffusion-v1-5/stable-diffusion-v1-5")
    plan_cmd.add_argument("--dream-model", default="lykon/dreamshaper-8-lcm")
    plan_cmd.add_argument(
        "--phase",
        choices=(
            "wave1",
            "wave2",
            "away",
            "reference60h",
            "window",
            "window2",
            "window3",
            "early-dream-backup",
        ),
        required=True,
    )
    plan_cmd.add_argument("--base-selection", default="legacy")
    plan_cmd.add_argument("--wave2-profiles")
    plan_cmd.add_argument("--finalists")

    dry = sub.add_parser("dry-run", help="assert wave/away counts and unique spec hashes")
    dry.add_argument("--plan", type=Path, required=True)

    run = sub.add_parser("run", help="execute a plan until deadline")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--force-gpu", action="store_true")
    run.add_argument("--max-entries", type=int, default=None)
    run.add_argument("--deadline-s", type=float, default=GENERATION_DEADLINE_S)
    run.add_argument("--cooldown-s", type=float, default=300.0)
    run.add_argument(
        "--shard",
        type=_shard,
        default=None,
        metavar="I/K",
        help="run only entries at positions congruent to I modulo K, so K "
        "drivers can share one immutable plan. Pair with POTOCOLOM_GPU_SLOTS=K.",
    )
    run.add_argument(
        "--abort-after-failed",
        type=int,
        default=3,
        help="stop and exit nonzero after this many consecutive failed or "
        "timed-out cells (0 disables). An unattended window must not spend "
        "hours reproducing the same failure.",
    )
    run.add_argument(
        "--allow-dirty",
        action="store_true",
        help="run even though tracked files are modified. Off by default: a dirty "
        "optimizer under a matching HEAD produces evidence that names a commit it "
        "did not come from.",
    )
    run.add_argument(
        "--abort-after-busy",
        type=int,
        default=20,
        help="stop and exit nonzero after this many consecutive cells refused as "
        "busy (0 disables). 20 at the default cooldown is roughly an hour of "
        "refusals. Without this the driver retried forever and then exited 0 at "
        "the deadline having produced nothing, which is what a lost "
        "POTOCOLOM_GPU_IDLE_PCT looks like from outside.",
    )
    run.add_argument(
        "--abort-after-dead",
        type=int,
        default=4,
        help="stop after this many consecutive cells emit near-constant images "
        "(0 disables). A catastrophe brake for unattended windows, not a "
        "quality gate: no image statistic separates good illusions from bad.",
    )

    for command in ("status", "audit", "report"):
        command_parser = sub.add_parser(command, help=f"show campaign {command}")
        command_parser.add_argument("--plan", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.cmd == "plan":
        if args.evidence_root.is_absolute() and str(args.evidence_root).startswith("/tmp"):
            parser.error("refusing /tmp evidence root for unattended campaign")
        plan = build_phase_plan(
            phase=args.phase,
            evidence_root=args.evidence_root,
            model_id=args.model,
            dream_model_id=args.dream_model,
            base_selection=args.base_selection,
            wave2_profiles=args.wave2_profiles,
            finalists=args.finalists,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(plan.to_json(), indent=2) + "\n")
        print(json.dumps(plan_counts(plan), indent=2))
        print(f"wrote {args.out}")
        return 0

    plan, plan_identity = load_plan(args.plan)
    if args.cmd in ("status", "audit", "report"):
        print(
            json.dumps(
                {"plan_sha": plan_identity, **_command_status(plan, plan_identity)}, indent=2
            )
        )
        return 0

    if args.cmd == "dry-run":
        counts = plan_counts(plan)
        hashes = [
            e.spec_hash(
                model_id=plan.model_id,
                dream_model_id=plan.dream_model_id,
                optimizer_fingerprint=plan.optimizer_fingerprint,
            )
            for e in plan.entries
        ]
        assert len(hashes) == len(set(hashes)), "duplicate spec hashes"
        wave1 = counts.get("wave1", 0)
        wave2 = counts.get("wave2", 0)
        away = sum(v for k, v in counts.items() if k.startswith("tier"))
        print(
            json.dumps({"counts": counts, "wave1": wave1, "wave2": wave2, "away": away}, indent=2)
        )
        if wave1 and wave1 != 24:
            print(f"FAIL: wave1 expected 24 got {wave1}", file=sys.stderr)
            return 1
        if wave2 and wave2 != 16:
            print(f"FAIL: wave2 expected 16 got {wave2}", file=sys.stderr)
            return 1
        if away > 184:
            print(f"FAIL: away expected <=184 got {away}", file=sys.stderr)
            return 1
        if counts.get("reference60h", 0) not in (0, 36):
            print("FAIL: reference60h expected 36", file=sys.stderr)
            return 1
        if counts.get("early_dream_backup", 0) not in (0, 48):
            print("FAIL: early_dream_backup expected 48", file=sys.stderr)
            return 1
        if counts.get("window2", 0) not in (0, 98):
            print(f"FAIL: window2 expected 98 got {counts.get('window2')}", file=sys.stderr)
            return 1
        window3_expected = (
            1
            + len(BREADTH_PAIRS)
            + len(WINDOW3_WORDINGS) * len(WINDOW3_WORDING_PAIRS) * len(WINDOW3_WORDING_SEEDS)
        )
        if counts.get("window3", 0) not in (0, window3_expected):
            print(
                f"FAIL: window3 expected {window3_expected} got {counts.get('window3')}",
                file=sys.stderr,
            )
            return 1
        print("dry-run ok")
        return 0

    if args.cmd == "run":
        if git_sha() != plan.git_sha:
            print(
                f"refusing run: HEAD {git_sha()} does not match plan.git_sha {plan.git_sha}",
                file=sys.stderr,
            )
            return 1
        # HEAD alone is not the provenance. A dirty optimizer runs happily under a
        # matching SHA while every manifest records the plan's frozen fingerprint,
        # so the evidence would name a commit that did not produce it.
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            cwd=repo_root(),
        ).stdout.strip()
        if dirty and not args.allow_dirty:
            print(
                "refusing run: tracked files are modified, so the evidence would "
                f"name {plan.git_sha[:9]} without matching it:\n{dirty}\n"
                "Commit or stash, then regenerate the plan. --allow-dirty overrides.",
                file=sys.stderr,
            )
            return 1
        if Path(plan.evidence_root).is_absolute() and str(plan.evidence_root).startswith("/tmp"):
            print("refusing unattended run with /tmp evidence root", file=sys.stderr)
            return 1
        py = os.environ.get("POTOCOLOM_WORKER_PYTHON") or str(
            repo_root() / "worker" / ".venv" / "bin" / "python"
        )
        started = time.monotonic()
        n = 0
        dead_streak = 0
        fail_streak = 0
        busy_streak = 0
        pending = list(plan.entries)
        if args.shard is not None:
            index, count = args.shard
            # Disjoint by position, so K drivers share one immutable plan
            # instead of K forked plans. Each shard still walks the plan's
            # breadth-first order.
            pending = [e for i, e in enumerate(pending) if i % count == index]
            print(f"shard {index}/{count}: {len(pending)} of {len(plan.entries)} entries")
        while pending:
            entry = pending.pop(0)
            if args.max_entries is not None and n >= args.max_entries:
                break
            remaining = args.deadline_s - (time.monotonic() - started)
            if remaining < entry.estimate_s * 1.1 + START_RESERVE_S:
                print(f"deadline reserve skips {entry.entry_id}")
                break
            print(f"RUN {entry.entry_id}")
            result = run_entry(
                plan,
                entry,
                py=py,
                force_gpu=args.force_gpu,
                plan_identity=plan_identity,
                timeout_cap_s=remaining,
            )
            print(json.dumps({"entry_id": entry.entry_id, "status": result.get("status")}))
            n += 1
            if result.get("status") == "busy":
                # A busy streak must be able to end the run. This loop used to
                # retry forever without counting anything, so a lost
                # POTOCOLOM_GPU_IDLE_PCT made every cell refuse, and the driver
                # then exited 0 at the deadline having produced nothing. Silent
                # total failure that reports success is the worst outcome
                # available, so it is now a nonzero exit.
                busy_streak += 1
                print(
                    f"GPU busy {busy_streak}/{args.abort_after_busy}; "
                    f"retrying after {args.cooldown_s:.0f}s"
                )
                if args.abort_after_busy and busy_streak >= args.abort_after_busy:
                    print(
                        f"ABORT: {busy_streak} consecutive cells refused as busy. "
                        "The GPU is held by something else, or "
                        "POTOCOLOM_GPU_IDLE_PCT is below the desktop's own idle "
                        "utilisation. Nothing was produced.",
                        file=sys.stderr,
                    )
                    _append_event(
                        {"event": "abort_busy", "entry_id": entry.entry_id, "streak": busy_streak}
                    )
                    return 1
                time.sleep(args.cooldown_s)
                pending.insert(0, entry)
                continue
            busy_streak = 0
            if result.get("status") in ("failed", "timeout"):
                fail_streak += 1
                print(f"cell {result.get('status')} {fail_streak}/{args.abort_after_failed}")
                if args.abort_after_failed and fail_streak >= args.abort_after_failed:
                    print(
                        f"ABORT: {fail_streak} consecutive cells failed or timed out. "
                        f"Stopping so an unattended window does not spend hours "
                        f"reproducing the same failure.",
                        file=sys.stderr,
                    )
                    return 4
            else:
                fail_streak = 0
            if args.abort_after_dead and result.get("status") == "completed":
                if degenerate_run(Path(str(result.get("out")))):
                    dead_streak += 1
                    print(f"degenerate output {dead_streak}/{args.abort_after_dead}")
                    if dead_streak >= args.abort_after_dead:
                        print(
                            f"ABORT: {dead_streak} consecutive cells emitted near-constant "
                            f"images. Stopping so the rest of the window is not spent on a "
                            f"dead axis.",
                            file=sys.stderr,
                        )
                        return 3
                else:
                    dead_streak = 0
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
