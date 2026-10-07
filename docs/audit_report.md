# Project audit

Audit date: 2026-10-05. Source baseline: main `006f408ab1ed8148639cfc804553c13420ae8c0c`, after PRs [648](https://github.com/Portocolom-Studio/potocolom/pull/648) and [649](https://github.com/Portocolom-Studio/potocolom/pull/649).

The user changed the current task to cleanup and handoff. The full implementation programme is incomplete. This report separates main from saved local drafts. Start a new session with [session-handoff.md](session-handoff.md); the remaining work is in [implementation_spec.md](implementation_spec.md).

> Update 2026-10-07: this audit is a snapshot of main at `006f408`. Since then, protocol 6 and the F1 recovery fix have landed (#657, #663, #664, #677 to #681). The Protocol, Dispatch and Realtime rows below describe the snapshot; [session-handoff.md](session-handoff.md) lists what changed.

## Evidence and saved work

The audit read the authoritative architecture, blueprint, wire protocol, API, profiles, decisions, local development, repository boundary and runner documents. It used routed local internals, source, tests, issue bodies and discussions, Git ancestry, and independent reviews. Source and tests establish shipped behavior; accepted decisions establish intended behavior.

The authenticated tracker check on 2026-10-05 found 36 public open issues and two open PRs: optimizer [118](https://github.com/Portocolom-Studio/potocolom/pull/118) and ratings [483](https://github.com/Portocolom-Studio/potocolom/pull/483). Both remain unmerged. The prior programme also reviewed eight private issues. Refresh the private tracker before assigning cloud work; private source and credentials must stay in the private repository.

The cleanup inventory found 112 old worktrees, with 56 dirty working trees. All 56 sets of eligible source edits were saved as signed local checkpoint commits. Their original branch refs were preserved. This includes the other session's benchmark, gallery, package and worker edits, older invitation/cookie work, the optimizer's local edits, and all foundation candidates. The saved bytes were checked before and after each checkpoint. No broad foundation candidate was merged.

After the archive and selected filesystem restore passed, fresh source checks found all 109 removal candidates clean with their heads in the bundle. All 109 were removed. The primary checkout was restored to main. The two open PR worktrees and this cleanup PR's temporary worktree remain clean apart from the reviewed cleanup change itself. The process check covered readable current-user working directories; four unreadable login/key services were recorded as limits. It is not a universal process audit.

The local archive has one entry folder, `.local/cleanup-20261005/`. Its Git bundle contains complete history and checkpoint refs; its state archive contains ignored working memory, proof packets and local dependency state. The archive receipt records integrity, restore checks and omissions. These files are local and private. They are evidence of saved work, not proof that a feature works. The handoff gives the restore procedure.

## Current architecture

| Area | Main behavior | Evidence |
|---|---|---|
| API and state | FastAPI; PostgreSQL accounts, jobs, assets and usage; after successful database initialization, an accounts-mode advisory lock refuses another accounts startup | `backend/app/main.py:97`, `backend/app/db.py:216` |
| Dispatch | In-process job heap; startup rebuilds queued jobs and retries interrupted jobs | `backend/app/jobs.py:75`, `backend/app/jobs.py:2140` |
| Realtime | Process-local workers, sessions and admission queue; API owns browser/worker sockets and directly awaits fleet sends | `backend/app/realtime.py:42`, `backend/app/realtime.py:741` |
| Protocol | Worker protocol 5; API compatibility floor 4; browser frame versions are translated by the API | `backend/app/realtime.py:42`, `worker/worker/client.py:36` |
| Storage | Local disk and S3-compatible adapters; database assets bind authorized output reads | `backend/app/storage.py`, `backend/app/files.py` |
| Worker | Python/diffusers; CUDA, ROCm and CPU; model manifests, memory ladder and same-shape batching | `worker/worker/engine.py`, `worker/worker/memory_ladder.py`, `worker/worker/frame_batch.py` |
| Product | Drawing document, replay files, canvas tools, lineage, account/auth/admin and usage surfaces | `frontend/src/lib/drawing-document.ts`, `frontend/src/lib/components`, `backend/app/accounts.py` |
| Site | One SvelteKit source; landing/product build gate controls presentation, not authorization | `frontend/src/routes`, `frontend/.env` |

Shared Redis queues, FrameBus, cross-process socket ownership, HTTP quota callers, durable late-usage settlement, a managed fleet, gateway and tickets are targets. Main does not implement those services. Adding Redis configuration alone does not enable scale-out. The accounts startup lock must remain until real shared authority and storage admission are proved. A database probe, version check or migration failure starts the API degraded without the lock: health stays up and readiness returns 503. DB-backed account requests and accounts-mode browser sessions are refused; unauthenticated-mode browser and fleet-token handshakes remain possible. Later auth-mode, key-ring or local-user initialization errors fail startup instead.

The core design remains sound: PostgreSQL is durable authority; Redis is advisory and reconstructible; the API owns sockets; the worker reports physical execution completion; private billing and fleet systems use HTTP contracts. Scale tests must prove these rules through actual callers, not adapter tests alone.

## Changes merged during cleanup

| Change | Result |
|---|---|
| PR648, admin pagination | Typed Python tuples keep the indexed SQL row comparison compatible with current SQLAlchemy/mypy. Tied timestamp and UUID page tests passed. |
| PR649, simulation CI | Each Actions job gets a PostgreSQL 16 service, a dynamic loopback port and a 512 MiB data tmpfs. CI refuses the developer database. Job cleanup removes its service. |
| This cleanup PR | Freezes the reset test's rate-limit clock and tests queue drain after elapsed time; updates architecture/profile facts and adds the audit, roadmap and handoff. Final gate results are recorded in the handoff and PR. |

The exact combined PR648/649 tree passed local full verification: 1,182 backend tests, 373 worker tests, 296 frontend unit tests and 63 canvas checks, all with zero skips. Both frontend builds, CSP and browser checks passed. All 33 tracked Mermaid diagrams rendered. These counts certify that narrow tree, not the archived foundation programme.

The push to main passed simulation, frontend, docs and deployment checks. Its backend job failed one reset test: the fake sleep recorder still used a real clock, so request time reduced the expected delay. The failure is a test timing defect; production rate limiting correctly drains elapsed time. The cleanup fix has an independent source review and an actual HTTP regression check. The local full gate passed with 1,183 backend, 373 worker, 296 frontend unit and 63 canvas checks, all with zero skips, plus both builds and browser/CSP checks. The focused recovery file passed 18 tests. All 33 Mermaid diagrams rendered. Backend/worker combined coverage was 85%/80%; XML reports and environment limits are in the local archive. PR CI must also pass before merge.

The new merge commit `006f408` has literal newline escapes in its message, so its DCO text is not a valid separate sign-off line. A code/tree/parent-identical corrected commit, `d7129bc35b369c06ca4251cdd03ca486bf4f73d7`, is saved locally. The user approved a protected update, but GitHub rejected it because main forbids force pushes and requires PRs. The remote history is unchanged. A later signed commit does not repair that earlier message.

## Unfinished work and review limits

| Work | Latest useful evidence | Remaining gate |
|---|---|---|
| Broad foundations and product slices | Local checkpoint source and earlier local tests | Rebase selected slices onto current main; independent source review; actual local/CI gates; PR merge |
| C2 API shared ownership | API v6 source review requested revision | Recovery, startup admission, journal, purge, claim and maintenance repairs; combined acceptance |
| F1 recovery | Contract v4 and harness v3 source passed review | Run actual RED; repair production source; GREEN, mutations and cleanup proof |
| S1 command journal | Identity/floor v2 final design passed | Implement actual producers, durable replay and revoke transaction rules; all finite runtime cases |
| Worker maintenance | Repair v2 author reports 598 tests, zero skips | Independent source gate; 11 of 22 cases remain partial; paired API/worker and library/hardware gates |
| P8 lineage performance | 5,000-root case passed; measured 59.885161 FPS failed strict 60 FPS gate | Repair/review diagnostic harness before any capture; identify cause; retain strict acceptance |
| Quota and fleet | Reviewed adjustment/fleet design packets; private local billing tests | Public normative contracts, actual callers/outbox, real transport/IAM/boot/Stripe and cloud acceptance |
| Gateway and capacity | Design/backlog | Go bridge, tickets, fairness and 1,000-session load/cost gates |
| Product completion | Partial drawings, lineage and account surfaces | Refine/upload/checkpoints, masks, invitation/admin/export, launch/pricing, safety and developer keys |
| Portability and research | CPU/local evidence and off-main optimizer/ratings | CUDA, native ARM/Windows, actual GPU/library evidence, human study gates and release artifacts |

The full architecture review and final gate identified seven confirmed shared-runtime gaps and one unmeasured database risk. An estimated 10,000-20,000 SQL reads per second at 1,000 sessions and 2-4 FPS is a capacity concern, not a measurement. The roadmap retains the repair order and the original finite test contracts in the archive.

Four self-hosted Actions runners were online and idle at the latest pre-PR check. Runner configuration and fork approval controls were preserved. Unit, browser, CPU inference, Compose, restore, exact image, GPU, HTTPS and provider gates are distinct. A Linux CPU pass cannot certify CUDA, Windows, durable power-loss recovery, cloud permissions or production capacity.

No release or production rollout is part of this cleanup. The next session should finish one verified vertical slice at a time, through PRs, using the saved source and review packets as inputs.
