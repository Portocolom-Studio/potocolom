# Cleanup and handoff roadmap

Date: 2026-10-05, updated 2026-10-07. Main baseline: `006f408`; protocol 6 and F1 landed since (see the handoff).

This file records the remaining work and the evidence needed to call it done. It is a handoff, not approval to implement every design below. Keep each change inside its existing issue and accepted contract. The detailed finite cases and original contracts remain in the local archive listed at the end.

## Current state

PRs #648 and #649 are merged. Open PRs #118 (optimizer) and #483 (ratings) remain unmerged. A merge into a feature branch does not make work part of Main. The large foundation changes are preserved as signed local archive checkpoints; they are not certified as Main behavior.

Main's API declares worker protocol 5, with protocol 4 as its compatibility floor, and accepts protocol 6 workers when `ROOT_KEYS` is set (#664, #677, #678, #680, #681): a PostgreSQL scheduler lease, an encrypted command journal, durable report receipts and receipt-based restart recovery (#679). Generation jobs and realtime ownership are still in process. The realtime API writes directly to its sockets. After successful database initialization, accounts mode takes a PostgreSQL advisory lock that refuses another accounts startup. Database probe, version check or migration failure starts degraded without that lock. Shared queues, FrameBus, HTTP quota calls, fleet orchestration, and a gateway are designs or unshipped work. Billing and fleet services remain in the private repository behind the QuotaService HTTP boundary.

Use these gate names precisely:

| Gate | What it proves |
|---|---|
| DESIGN | A bounded contract and its cases passed design review. No code is implied. |
| SOURCE | An identified source change passed the stated source review. It does not prove runtime behavior. |
| RUNTIME | The identified code passed its tests or live checks in the stated environment. It does not prove product, hardware, or production acceptance unless those were tested. |
| PRODUCT / external | User-visible behavior, supported hardware, private service, release, or production acceptance passed its own stated check. |

Do not promote a result from one gate to another. Record source identity, exact commands, real exit status, skips, and environment for each claimed pass.

## Ordered next work

The F1 recovery and F2 startup names below identify later C2 defects. They are different from the original foundation F1 pending-frame and F2 calibration tasks. Use the contract name and saved path when assigning work.

| Order | Work | State now | Dependency and completion evidence |
|---|---|---|---|
| 1 | Backend recovery and journal: F1, S1, S2, S3 | F1 is fixed on main for the code main has (#679, failing tests shown first). A durable command journal ships (#677); the S1 v2 design's revoke-transaction rules are not implemented. S2 physical proof after purge and S3 fresh-claim actor are unimplemented. | Implement the remaining S1 rules and pass its journal cases. Next prove physical cleanup after purge (S2), then a fresh claim actor and its recovery cases (S3). Keep this sequence serial. |
| 2 | Backend startup check: F2 | Precise shared-profile admission remains incomplete. | Prove one local owner for both auth modes; for shared profiles prove regional queue/frame/context identity and reciprocal storage authority before admission. Keep accounts locking until those witnesses exist. Rate endpoints, path/bucket equality and readiness responses are not authority. |
| 3 | C2 API protocol work | Landed in slices (#664, #677, #678, #680, #681) with independent review per slice. Drain, shared queues/FrameBus, work-budget enforcement and pause are not ported. | Resolve source-review findings on the exact candidate and complete every cumulative gate, including paired runtime evidence. A design pass or isolated green suite does not close C2. |
| 4 | Worker maintenance repair | Repair v2 author result: 598 tests pass. No independent SOURCE PASS. The 22-case runtime set is 11 PASS and 11 PARTIAL. | Obtain independent source review, then close each partial case. Resolve empty unload-all reconnect descriptors and retained predecessor maintenance-result replay as separate reviewed contract gaps. Library/pinned-GPU calibration and paired API behavior are unproved. Do not claim maintenance complete from the author suite. Coordinate backend work through S1/S2/S3. |
| 5 | P8 lineage and performance | Strict rendering result is 59.885161 FPS, below the 60 FPS bar: FAIL. The 5,000-root bounded case passes. Diagnostic harness v3 is SOURCE REVISE; v4 has not been dispatched. No capture lease exists. | Repair and review the diagnostic source before runtime use. Identify the cause before a new acceptance run; do not lower the threshold or use a lucky retry. Preserve the 5,000-root result. Repack only the changed shelf and keep the selected tree in view. |
| 6 | C3 quota and billing boundary | An idempotent adjustment is approved for previously uncounted late GPU usage, linked to the original reservation. There is no accepted public source norm or shipped caller/fake/outbox. | Record the additive rule in the public contract and a new decision before implementation. Then test duplicate, delayed, expiry, refund and retry handling against the original reservation; charge no usage twice. Keep private ledger and Stripe work behind HTTP and after the public contract. |
| 7 | C4 fleet credentials | Fleet v8 has DESIGN PASS only. | Source work still needs an accepted public verifier contract and paired runtime cases for lease, audience, worker incarnation, expiry, revocation, replay, and rotation. The private producer remains a separate follow-on. |

The worker maintenance source/runtime work and backend sequence have shared dependencies. Keep one owner for shared backend or worker files at a time. Reconcile candidate patches against Main before adoption; archive presence alone is not evidence of adoption.

## Issue decomposition and checks

These rows cover all 36 public open issues in the 2026-10-05 tracker check. A row defines a bundle, not permission to close an issue without its detailed cases. Refresh bodies and discussions before dispatch. The saved original specification contains the full criterion ledger and contracts.

| Issues | Bundle and dependency | Required proof |
|---|---|---|
| #3, #42 | Canvas lifecycle/cadence; original pending-frame foundation repair first | Deferred decode/reordering, highest pending revision, complete reconnect resend, wall-clock idle stop, bounded pipeline counters; built browser tests |
| #10, #49 | Account/settings/security/export/delete and invite UI; reconcile saved invitation work | Real API, recent auth/CSRF, role matrix, failed/retried actions; mint/list/copy/revoke, expired/reused invite; throwaway deletion data |
| #54 | Checkpoints and scaled refine after authorized input upload | Bounded/corrupt/old drawing files, target-resolution replay and pixel equality, lineage parentage, input expiry/late-write cleanup and deterministic recovery |
| #55 | Finish masks against real model contracts | Malformed/boundary polygons; SDXL Turbo mask prompt schema; selected output and outside preservation through actual worker/pixels |
| #130 | Packed lineage shelves and bounded performance after P8 diagnosis | Changed-shelf growth, selected-tree camera, ghosts/search/keyboard/manual offsets; strict 500-node frame rate and 5,000-root fetch/mount/memory checks |
| #60, #196 | Calibration and class-aware capacity | Raw-canvas conversion exactly once; real supported mode/step/mask/batching matrix, fresh cold/warm per-session p95, memory and quality; no batching discount for serial fallback |
| #191, #20, #194, #199 | Shared queues/FrameBus/ownership/fairness and endpoint split; C2 repairs first | Same local/shared conformance, leases/fencing, one socket writer, cancel/requeue/revoke, two owners, store failure/recovery, regional endpoint isolation; preserve accepted fairness policy |
| #21 | Rate budgets, durable metering, quota caller/fake/outbox after C2 | Actual callers; reject before reserve/dispatch; fixed reservation retry IDs, no work on reserve failure, delayed physical completion, expiry/crash/retry, no double charge/refund/adjustment |
| #225 | Public fleet verifier then private producer | Lease/audience/worker/incarnation/key binding, expiry/revoke/replay/rotation, static self-host path; transport and provider boot separately |
| #192, #193 | Go gateway/control bridge and 30-second one-use tickets after shared authority | Local/cross-owner frames, ordered control/drain/revoke, bounded stalls, atomic ticket consume, region/user/session binding, outage and no cookie exposure |
| #48, #197, #198 | Load, regional ready pools and cost ceiling after gateway/fleet | 1,000 sessions at 2 and 4 FPS; per-session tails/drops, churn/owner loss, real topology, ready-only supply/cold starts, stale evidence invalidation and measured spend ceilings; finite queue wait is not guaranteed without preemption |
| #27, #43 | Safety and developer API after consumer/quota contracts | Prompt/update/job/output refusal and failure policy; no refused upload/content retention; key scope/revoke/rotation, body-hashed idempotency, layered limits, no duplicate jobs |
| #40, #50 | Honest pricing/previews and launch after release/private evidence | Both builds, en/es/accessibility; measured pricing terms, verified images/digests and notes; no paid beta before quota/safety/fleet/staging |
| #145 | Windows support as a separate platform slice | Fresh Docker Desktop install, secrets, paths/files, GPU/no-GPU and matching database/assets restore; Linux tests do not close it |
| #115, #122, #134 | Optimizer/core/phase reset/joint targets; review PR118 evidence first | Actual phase state reset, no leaked momentum, seeded view consistency, bounded CPU/GPU resources and preset human quality cohort |
| #116, #117 | Illusion job protocol and designer after accepted core | Progress/cancel/retry/restart, attempts/outputs/checkpoints and N-1; actual configure/run/recover/preview journeys |
| #135, #136, #137, #138 | Illusion research, kept outside generic feature swarm | Fixed cohorts/controls/ablations, held-out human labels, yield/error/cost and stop lines. CLIP triage ROC-AUC 0.706 failed its 0.75 gate; selected examples do not overturn it |
| #265, #266 | Pose and semantic colour research | Same-geometry/seed controls, held-out human quality and model-specific latency; drawing colour is not semantic conditioning |

Foundation candidates also need current-main reconciliation: pending-frame ordering, raw calibration, CI artifact paths/deadlines, actual-master restore, visible logout, bounded slow-peer transport, suspended-account export and authorized image input. Admin keyset pagination is now on main. Tests against archived combined trees do not establish the remaining candidates on main.

Private follow-ons are billing, autoscaler, aggregate telemetry ingest, warehouse, infrastructure/delivery, waitlist, invite operations and cloud edge (private issues #1-#7 and #9). Billing has local reviewed financial/CI source and tests, but no production acceptance. Verify private main before reuse. Public contracts and artifacts precede private consumers; provider stubs precede real staging. Prove separate state/OIDC/migration/rollback, no public bucket, URL authority/expiry, TLS/WSS and deletion propagation. Do not publish private code or local credentials here.

## Parallel work boundaries

| Workstream | Exclusive scope | Start gate |
|---|---|---|
| Backend authority | Shared backend jobs/realtime/auth and their tests | F1 contract/harness admission; serial fixes above |
| Worker maintenance | Worker client/managers/capacity and their tests | Independent repair source gate; paired API tests after backend gates |
| Frontend diagnostics/product | Named canvas/lineage or account files, never two agents on the same module | P8 source gate for diagnostics; backend contract for callers |
| CI/release | Named workflows/scripts/images and artifact fixtures | Current-main path/deadline audit; freeze runner permissions and resource ownership |
| Contract/private integration | Public normative docs/fake clients or separate private repository | Approved public contract, then independent source review |
| QA/portability | Separate conformance/real-caller/platform fixtures | Exact candidate identities and owned environment |

Dispatch no more work than the tool slots allow. Reviewers stay read-only. A coordinator alone reconciles overlapping modules, runs the combined gate and submits a PR. Before code, give each bundle a fixed file list, dependency, negative cases, commands and pass bar; reconcile existing decisions instead of adding speculative seams.

## Remaining programme gates

| Area | Required next evidence |
|---|---|
| Architecture and scale | Implement shared queues and FrameBus only under their existing issues and decisions. Prove adapter parity, ownership/fencing, outage recovery, bounded slow-peer behavior, and cross-owner relay before a load claim. Add gateway and one-use tickets only after the authority and ordered-control contracts pass. A 1,000-session result must use the accepted topology and include per-session latency, drops, failures, and resource use. |
| Quota and fleet | Keep the public API and fake contract in this repository; keep billing, fleet purchase/orchestration, and cloud operations in the private repository. Prove public behavior against fakes before private integration. No shared code or private imports cross this boundary. |
| Tests and CI | Run focused suites for each slice, then `make verify` on the integration tree with its own source path. Start long verification under tmux. Confirm the self-hosted runner is Idle before a push or PR update. A local pass is not a GitHub Actions run. |
| Hardware and portability | CPU/simulation, ROCm, CUDA, Windows, and cloud staging are separate claims. This AMD machine cannot prove CUDA. Do not claim Windows support without a Windows run. Keep exact image, driver, model, and environment identity with hardware results. |
| Self-hosted release | Finish release checks, exact image and dependency checks, restore of matching database plus real assets, and supported hardware evidence before a release claim. Tagging and publication remain maintainer actions. |
| Product and research | Close existing user-flow criteria only under their issues. Keep optimizer and ratings work off Main until their PRs are reviewed and merged. Research claims need fixed cohorts, held-out evidence, resource limits, and a pre-set stop rule. Selected examples do not prove yield or quality. |

## Source of truth and archive

Use `docs/architecture.md`, `docs/blueprint.md`, `docs/connection-handling.md`, `docs/api.md`, `docs/deployment-profiles.md`, `docs/decisions.md`, `docs/local-development.md`, `docs/repository-boundary.md`, and `docs/self-hosted-runner.md` for current public contracts and shipped-state notes. Where a design section differs from a shipped-state note or source, treat it as a design until its issue's runtime and product gates pass. Keep private service implementation in the private repository.

The original long roadmap, its finite test cases, and signed source checkpoints are preserved in the primary checkout at `.local/cleanup-20261005/worktree-state.tar.zst`, `.local/cleanup-20261005/work-checkpoints.bundle`, and `.local/cleanup-20261005/checkpoints.json`. These are local private artifacts, not public source, and must not be treated as merged or independently accepted code. Use them to recover exact contracts and finite cases instead of copying the original large spec into this file.

F1 is done (#679). The next backend steps are the remaining S1 rules, then S2 and S3; F2 may proceed independently.
