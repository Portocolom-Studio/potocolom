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
    FINAL_PAIRS,
    SCREEN_PAIRS,
    PAIR_BY_ID,
    degenerate_run,
    git_sha,
    is_completed_run,
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
# is the deliverable. That is why the budget is set from where quality
# saturates rather than from the paper's figure, and why the corpus is wide.
WINDOW_SDS_STEPS = 3_000
WINDOW_DREAM_ROUNDS = 8
WINDOW_STYLE = "none"
WINDOW_PRIME_RESOLUTION: int | None = None
WINDOW_SEEDS = (11, 23, 37, 53, 71, 89)
WINDOW_CELL_ESTIMATE_S = 1_200.0
# A budget control: the same pairs and seeds at the paper's full 10,000 steps,
# to show on this evidence that the shorter budget gave nothing away. Runs last,
# because the pre-window ladder already answered it once.
WINDOW_CONTROL_SDS_STEPS = 10_000
WINDOW_CONTROL_PAIRS = ("crown_octopus", "stag_oak")
WINDOW_CONTROL_SEEDS = (11, 23)
WINDOW_CONTROL_ESTIMATE_S = 3_500.0
# Proven legacy keepers, carried in so the sweep has a pair whose outcome is
# already known from human review, and the incompatible negative control.
WINDOW_LEGACY_CONTROL_PAIR_IDS = ("dog_sloth", "mountain_valley")


def _window_pair_ids() -> tuple[str, ...]:
    """Every pair the window sweeps, widest axis first.

    docs/illusions.md calls prompts "the biggest lever by far", and issue #138
    already curated a pairing-rule corpus that no GPU cell has ever run. The
    earlier plan sampled five pairs; this samples the axis.
    """
    from worker.illusion_experiment import PAIRING_RULES_PAIRS

    return (
        REFERENCE_CALIBRATION_PAIR_ID,
        *(pair.pair_id for pair in PAIRING_RULES_PAIRS),
        *REFERENCE_COMPATIBLE_PAIR_IDS,
        *WINDOW_LEGACY_CONTROL_PAIR_IDS,
        REFERENCE_CONTROL_PAIR_ID,
    )


def _window_flags(sds_steps: int) -> list[str]:
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
    if WINDOW_PRIME_RESOLUTION is not None:
        flags += ["--prime-resolution", str(WINDOW_PRIME_RESOLUTION)]
    return flags


def build_window_60h() -> list[CampaignEntry]:
    """Breadth-first yield sweep for the unattended 60-hour window.

    Ordering is breadth-first by seed so the matrix degrades gracefully: every
    pair has one seed before any pair has two. A window that ends early still
    answers "which pairs work at all" rather than "these three pairs work".
    """
    entries: list[CampaignEntry] = []
    priority = 0
    pair_ids = _window_pair_ids()

    # The rig check runs first: the known-good pair at the window's own
    # settings. If the short budget or the chosen wording broke what already
    # worked, it shows here in one cell rather than in fifty.
    entries.append(
        _entry(
            tier="window60h",
            profile="anchor",
            pair_id=REFERENCE_CALIBRATION_PAIR_ID,
            seed=WINDOW_SEEDS[0],
            flags=_window_flags(WINDOW_SDS_STEPS),
            priority=priority,
            style=WINDOW_STYLE,
            estimate_s=WINDOW_CELL_ESTIMATE_S,
        )
    )
    priority += 1

    for seed in WINDOW_SEEDS:
        for pair_id in pair_ids:
            if pair_id == REFERENCE_CALIBRATION_PAIR_ID and seed == WINDOW_SEEDS[0]:
                continue  # already covered by the anchor cell
            entries.append(
                _entry(
                    tier="window60h",
                    profile="sweep",
                    pair_id=pair_id,
                    seed=seed,
                    flags=_window_flags(WINDOW_SDS_STEPS),
                    priority=priority,
                    style=WINDOW_STYLE,
                    estimate_s=WINDOW_CELL_ESTIMATE_S,
                )
            )
            priority += 1

    for seed in WINDOW_CONTROL_SEEDS:
        for pair_id in WINDOW_CONTROL_PAIRS:
            entries.append(
                _entry(
                    tier="window60h",
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
        model_id=model_id,
        dream_model_id=dream_model_id,
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
    if EVENTS_PATH.parent.is_dir():
        with EVENTS_PATH.open("a") as handle:
            handle.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), **event}) + "\n")


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
    elif phase == "window60h":
        entries = build_window_60h()
    elif phase == "early-dream-backup":
        entries = build_early_dream_backup()
    else:
        raise ValueError(f"unknown phase: {phase}")
    plan = CampaignPlan(
        campaign_id=f"illusion-{phase}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        git_sha=git_sha(),
        created_at=datetime.now(timezone.utc).isoformat(),
        evidence_root=str(evidence_root),
        model_id=model_id,
        dream_model_id=dream_model_id,
        optimizer_fingerprint=_optimizer_fingerprint(),
        entries=(
            entries
            if phase in ("reference60h", "window60h", "early-dream-backup")
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
        choices=("wave1", "wave2", "away", "reference60h", "window60h", "early-dream-backup"),
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
        print("dry-run ok")
        return 0

    if args.cmd == "run":
        if git_sha() != plan.git_sha:
            print(
                f"refusing run: HEAD {git_sha()} does not match plan.git_sha {plan.git_sha}",
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
        pending = list(plan.entries)
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
                plan, entry, py=py, force_gpu=args.force_gpu, plan_identity=plan_identity
            )
            print(json.dumps({"entry_id": entry.entry_id, "status": result.get("status")}))
            n += 1
            if result.get("status") == "busy":
                print(f"GPU busy; retrying after {args.cooldown_s:.0f}s")
                time.sleep(args.cooldown_s)
                pending.insert(0, entry)
                continue
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
