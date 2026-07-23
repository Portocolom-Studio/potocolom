# Illusion reliability - status for next agent

Full protocol: [illusion-reliability.md](illusion-reliability.md).

## Branch

- Worktree: `/tmp/potocolom-illusion-reliability`
- Branch: `illusion-reliability-program`
- HEAD: `2ac34f4` (GPU-lock false-positive fix; prior corrective `ee08f8a`)
- Do **not** cherry-pick into PR #118 or change optimizer defaults yet.
- Provisional (exclude from acceptance): `out/illusion-experiments/` (d2e6c52)
  and `out/illusion-experiments-v2/` (pre-VAE-fix).
- Durable evidence: `.local/illusion-experiments-v3/`

## Verification complete (ready for Wave 1)

| Check | Result |
|-------|--------|
| 3-way GPU digests (60 SDS, 0 Dream, seed 2) | **match=true** vs `5f30fdd` for corrected default and instrumented |
| Hidden microbatch smoke | peak ~9417 MB / 16368 MB, no OOM |
| CLIP ViT-L/14 offline | revision `32bd64288804d66eefd0ccbe215aa642df71cc41` |
| Campaign dry-run | wave1=24, wave2=16, away=180 |
| Resume/skip tests | `test_illusion_campaign_resume.py` passed |
| Worker suite | 95+ tests green at last full run |

Equiv summary: `.local/illusion-experiments-v3/equiv3/equiv3_summary.json`
Hidden report: `.local/illusion-experiments-v3/hidden_microbatch_smoke/vram_report.json`
CLIP pin: `.local/illusion-experiments-v3/clip_cache.json`

## Next: Wave 1 pilot (do not skip blind ratings)

24 runs: 6 profiles x 4 screen pairs, seed 2, diagnostics on, `--skip-clip`,
into `.local/illusion-experiments-v3/`. Then post-score with cached CLIP,
build blinded sheets, human rate, choose base B.

```bash
cd /tmp/potocolom-illusion-reliability
PY=$(scripts/worker-python.sh)
export PYTHONPATH=worker TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1 HF_HUB_OFFLINE=1
$PY -m worker.illusion_campaign plan --pilot-only \
  --out .local/illusion-experiments-v3/pilot-plan.json \
  --evidence-root .local/illusion-experiments-v3
# Prefer campaign runner for wave1 only, or scripts/run-illusion-screening.sh
```

## Authorship

`leonfullxr` / DCO `-s` / no Co-authored-by.
