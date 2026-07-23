"""Immutable illusion campaign plans and an unattended runner.

Writes evidence under ``.local/illusion-experiments-v3`` by default.
Not imported by the online worker path.
"""

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
    git_sha,
    repo_root,
    write_manifest_atomic,
)

DEFAULT_EVIDENCE_ROOT = Path(".local/illusion-experiments-v3")
GENERATION_DEADLINE_S = 52 * 3600
SCORE_RESERVE_S = 2 * 3600
RUN_TIMEOUT_S = 65 * 60
TELEMETRY_INTERVAL_S = 10

WAVE1_PROFILES: list[tuple[str, list[str]]] = [
    ("legacy", ["--sds-objective", "legacy"]),
    ("weighted_sds", ["--sds-objective", "weighted_sds"]),
    ("csd_7_5", ["--sds-objective", "csd", "--sds-guidance", "7.5"]),
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

    def spec_hash(self) -> str:
        payload = {
            "profile": self.profile,
            "pair_id": self.pair_id,
            "seed": self.seed,
            "flags": list(self.flags),
            "style": self.style,
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
    entries: list[CampaignEntry] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "git_sha": self.git_sha,
            "created_at": self.created_at,
            "evidence_root": self.evidence_root,
            "model_id": self.model_id,
            "dream_model_id": self.dream_model_id,
            "entries": [
                {**asdict(entry), "spec_hash": entry.spec_hash()} for entry in self.entries
            ],
        }


def _entry(
    *,
    tier: str,
    profile: str,
    pair_id: str,
    seed: int,
    flags: list[str],
    priority: int,
    style: str = "none",
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
    )


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


def build_pilot_wave2(
    base_flags: list[str], *, base_is_legacy_joint: bool = False
) -> list[CampaignEntry]:
    """Build exactly four Wave-2 profiles from B's stripped flags."""
    candidates: list[tuple[str, list[str]]] = [
        ("B_hifa", base_flags + ["--hifa-schedule"]),
        ("B_round_robin", base_flags + ["--round-robin"]),
        ("B_dream_joint", base_flags + ["--dream-joint"]),
        ("B_hifa_joint", base_flags + ["--hifa-schedule", "--dream-joint"]),
        ("B_rr_joint", base_flags + ["--round-robin", "--dream-joint"]),
    ]
    # If Wave 1 already measured legacy+dream_joint, skip the pure joint candidate.
    if base_is_legacy_joint or base_flags == ["--sds-objective", "legacy"]:
        candidates = [c for c in candidates if c[0] != "B_dream_joint"]
    selected_profiles = candidates[:4]
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
    # Tier 3: CSD20, dream_lr 1e-2, classic SD15 dream
    for profile, flags in [
        ("csd_20", ["--sds-objective", "csd", "--sds-guidance", "20"]),
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
    # Tier 4: styles on subjects x 4 screen pairs x seeds 0,1 using B (first finalist)
    b_flags = finalist_profiles[0][1] if finalist_profiles else ["--sds-objective", "legacy"]
    for style in ("oil", "pencil", "editorial"):
        # oil style is oil-equivalent (exact prompts); still listed for coherent-oil control
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
        ("csd_20", ["--sds-objective", "csd", "--sds-guidance", "20"]),
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
        ("finalist_c", ["--sds-objective", "csd", "--sds-guidance", "7.5"]),
    ]
    entries = build_pilot_wave1()
    entries.extend(build_pilot_wave2(["--sds-objective", "legacy"]))
    if include_away:
        entries.extend(build_away_tiers(finalists))
    # Deduplicate by spec_hash keeping first (higher priority already ordered)
    seen_hash: set[str] = set()
    unique: list[CampaignEntry] = []
    for entry in entries:
        digest = entry.spec_hash()
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


def is_completed_matching(out_dir: Path, expected_sha: str, expected_spec: str) -> bool:
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
    return (out_dir / "derived_1.png").is_file() and (out_dir / "derived_2.png").is_file()


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
) -> dict[str, Any]:
    root = Path(plan.evidence_root)
    out = root / entry.out_rel
    status_path = out / "driver_status.json"
    log_path = out / "run.log"
    out.mkdir(parents=True, exist_ok=True)

    if is_completed_matching(out, plan.git_sha, entry.spec_hash()):
        return {"entry_id": entry.entry_id, "status": "skipped_completed"}

    # Incomplete prior attempt -> new attempt directory
    if (out / "manifest.json").is_file():
        attempt = 1
        while (root / f"{entry.out_rel}_attempt_{attempt}").exists():
            attempt += 1
        out = root / f"{entry.out_rel}_attempt_{attempt}"
        out.mkdir(parents=True, exist_ok=True)
        status_path = out / "driver_status.json"
        log_path = out / "run.log"

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
        *list(entry.flags),
    ]
    status: dict[str, Any] = {
        "entry_id": entry.entry_id,
        "status": "running",
        "pid": None,
        "spec_hash": entry.spec_hash(),
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
            deadline = time.monotonic() + RUN_TIMEOUT_S
            telemetry: list[dict[str, Any]] = []
            next_tel = time.monotonic()
            while True:
                rc = proc.poll()
                now = time.monotonic()
                if now >= next_tel:
                    telemetry.append(_sample_telemetry())
                    write_manifest_atomic(out / "telemetry.json", {"samples": telemetry[-360:]})
                    next_tel = now + TELEMETRY_INTERVAL_S
                if rc is not None:
                    break
                if now >= deadline:
                    os.killpg(proc.pid, signal.SIGKILL)
                    status["status"] = "timeout"
                    status["error"] = f"exceeded {RUN_TIMEOUT_S}s"
                    write_manifest_atomic(status_path, status)
                    return status
                time.sleep(1.0)
            # Stamp spec hash into completed manifest if present
            manifest_path = out / "manifest.json"
            if manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text())
                    manifest["spec_hash"] = entry.spec_hash()
                    write_manifest_atomic(manifest_path, manifest)
                except json.JSONDecodeError:
                    pass
            if rc == 0:
                status["status"] = "completed"
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
    # Retry infrastructure/lock failures once; never auto-retry OOM.
    if result.get("status") == "failed" and result.get("error") != "oom":
        exit_code = result.get("exit_code")
        if exit_code in (2, 64) or exit_code is None:
            result = _attempt()
            result["retried"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    plan_cmd = sub.add_parser("plan", help="write an immutable campaign plan JSON")
    plan_cmd.add_argument("--out", type=Path, required=True)
    plan_cmd.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    plan_cmd.add_argument("--model", default="stable-diffusion-v1-5/stable-diffusion-v1-5")
    plan_cmd.add_argument("--dream-model", default="lykon/dreamshaper-8-lcm")
    plan_cmd.add_argument("--pilot-only", action="store_true")

    dry = sub.add_parser("dry-run", help="assert wave/away counts and unique spec hashes")
    dry.add_argument("--plan", type=Path, required=True)

    run = sub.add_parser("run", help="execute a plan until deadline")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--force-gpu", action="store_true")
    run.add_argument("--max-entries", type=int, default=None)
    run.add_argument("--deadline-s", type=float, default=GENERATION_DEADLINE_S)

    args = parser.parse_args(argv)
    if args.cmd == "plan":
        plan = build_full_plan(
            evidence_root=args.evidence_root,
            model_id=args.model,
            dream_model_id=args.dream_model,
            include_away=not args.pilot_only,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(plan.to_json(), indent=2) + "\n")
        print(json.dumps(plan_counts(plan), indent=2))
        print(f"wrote {args.out}")
        return 0

    plan_data = json.loads(Path(args.plan).read_text())
    entries = []
    for raw in plan_data["entries"]:
        entries.append(
            CampaignEntry(
                entry_id=raw["entry_id"],
                tier=raw["tier"],
                profile=raw["profile"],
                pair_id=raw["pair_id"],
                seed=raw["seed"],
                flags=tuple(raw["flags"]),
                out_rel=raw["out_rel"],
                priority=raw["priority"],
                style=raw.get("style", "none"),
            )
        )
    plan = CampaignPlan(
        campaign_id=plan_data["campaign_id"],
        git_sha=plan_data["git_sha"],
        created_at=plan_data["created_at"],
        evidence_root=plan_data["evidence_root"],
        model_id=plan_data["model_id"],
        dream_model_id=plan_data["dream_model_id"],
        entries=entries,
    )

    if args.cmd == "dry-run":
        counts = plan_counts(plan)
        hashes = [e.spec_hash() for e in plan.entries]
        assert len(hashes) == len(set(hashes)), "duplicate spec hashes"
        wave1 = counts.get("wave1", 0)
        wave2 = counts.get("wave2", 0)
        away = sum(v for k, v in counts.items() if k.startswith("tier"))
        print(
            json.dumps({"counts": counts, "wave1": wave1, "wave2": wave2, "away": away}, indent=2)
        )
        if wave1 != 24:
            print(f"FAIL: wave1 expected 24 got {wave1}", file=sys.stderr)
            return 1
        if wave2 != 16:
            print(f"FAIL: wave2 expected 16 got {wave2}", file=sys.stderr)
            return 1
        if away > 184:
            print(f"FAIL: away expected <=184 got {away}", file=sys.stderr)
            return 1
        print("dry-run ok")
        return 0

    if args.cmd == "run":
        py = os.environ.get("POTOCOLOM_WORKER_PYTHON") or str(
            repo_root() / "worker" / ".venv" / "bin" / "python"
        )
        started = time.monotonic()
        n = 0
        for entry in sorted(plan.entries, key=lambda e: (e.priority, e.entry_id)):
            if args.max_entries is not None and n >= args.max_entries:
                break
            if time.monotonic() - started > args.deadline_s:
                print("generation deadline reached; stopping")
                break
            print(f"RUN {entry.entry_id}")
            result = run_entry(plan, entry, py=py, force_gpu=args.force_gpu)
            print(json.dumps({"entry_id": entry.entry_id, "status": result.get("status")}))
            n += 1
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
