# Session handoff

Start here. The user requested cleanup, safe merges, current docs and a clear next-work document. The full feature programme remains incomplete. Resume it only as a new task, through small PRs.

## Read and check first

1. Read the local `AGENTS.md` instructions, then the routing table in `docs/internals/AGENTS.md`. Local internals are ignored and must stay local.
2. Read [audit_report.md](audit_report.md) for shipped state and [implementation_spec.md](implementation_spec.md) for the work order and gates. Read the nine authoritative documents named in the local instructions before changing a contract.
3. Check `git status --short`, `git worktree list`, remote main and the live issue/PR tracker. The source baseline for this audit is `006f408ab1ed8148639cfc804553c13420ae8c0c`; this cleanup PR adds the handoff and reset-test repair.
4. Keep optimizer PR118 and ratings PR483 unmerged. Their original branch refs and local work were saved. Do not delete remote branches: deletion can close their PRs.

PRs [648](https://github.com/Portocolom-Studio/potocolom/pull/648) and [649](https://github.com/Portocolom-Studio/potocolom/pull/649) are merged. They contain only admin pagination and isolated simulation CI fixes. The broad foundation candidates remain local drafts.

All 109 reviewed old worktrees were removed after archive and restore checks. The primary checkout is back on main and clean. The two open PR worktrees and the temporary cleanup PR worktree remain. Their source is committed; no untracked source remains in those retained trees. The cleanup worktree can be removed after its PR merge and the primary fast-forward.

## Saved work: one local archive

In the primary checkout, the local archive is:

`.local/cleanup-20261005/`

| File | Purpose |
|---|---|
| `checkpoints.json` | All 56 dirty source checkpoints: old path/branch/base, new signed ref/commit, initial staged/working patches and byte-preservation checks |
| `work-checkpoints.bundle` | Complete Git history and refs, including clean old worktree heads and the corrected merge-message commit |
| `worktree-state.tar.zst` | Old worktrees, ignored memory, reviews, test packets and local dependencies |
| `archive-verification.json` | Hashes, restored tree checks, archive integrity, critical state bytes/modes and explicit omissions |
| `archive-validation.json` | State validation before the separately retained cache move |
| `archive-critical-files.json` | Exact tar member names, hashes and modes for key contracts, reviews and saved memory |
| `restore-drill.json`, `removal-manifest.json`, `worktree-removal.json` | Selected restore, fresh clean/bundled source checks and completed worktree cleanup |
| `retained-trivy-cache/fanal/` | The unreadable root-owned cache, retained with the same inode, owner and permissions |
| `index-history/` | Initial staged and unstaged binary patches |
| `verification/`, `verification-v2/` | Initial and final local test logs, source origins, coverage and owned SQL resource closure |
| `fixture-cleanup/` | Private database/assets backups and owned fixture shutdown receipts |
| `final-state.json` | Final main/PR/worktree state and cleanup receipts |
| `reviews/` | Cleanup source, archive safety and final document reviews |

Keep the archive private. It includes private/local evidence and may include credentials. Its receipts certify saved bytes and selected restores; they do not certify runtime behavior. Old sockets are omitted. The cache was retained separately after tar could not read it; the tar creation failure remains recorded.

The old source trees are not a second live checkout fleet. Restore only the candidate needed for the next slice. The checkpoint manifest maps every source tree to its saved branch. The main integration candidate was `foundation-20261003`; its original large specification and review contracts remain saved with it.

Run these commands from the primary checkout that contains the local archive. Verify the bundle and clone it into a new empty folder:

```sh
cd "$(git rev-parse --show-toplevel)"
test -f .local/cleanup-20261005/work-checkpoints.bundle
git bundle verify .local/cleanup-20261005/work-checkpoints.bundle
git clone --no-checkout .local/cleanup-20261005/work-checkpoints.bundle /tmp/potocolom-restored
git -C /tmp/potocolom-restored switch --detach <checkpoint-commit-from-checkpoints.json>
```

Use `git show <checkpoint>:docs/implementation_spec.md` to read the original complete programme contract. Use `git diff <original-base>..<checkpoint>` to isolate a candidate. A checkpoint is a preservation commit, not an approved feature commit. Do not merge a whole checkpoint branch.

Recover ignored evidence as individual regular tar members into a new scratch directory. Stream with `zstd -dc` and Python `tarfile` in `r|` mode; use `extractfile()` for exact named members. Match the recorded hash and mode. Do not extract broadly over the live checkout or restore archived `.git` worktree pointers. Archived symlinks often point to old paths; restore their target data explicitly instead of following them.

The central archive receipt and removal/restore receipts are the current authority for cleanup. Older frozen source guards describe their original capture. Their HEAD/status fields are historical after checkpoint commits and worktree removal.

## Decisions already made

- Billing: add an idempotent settlement adjustment tied to the original reservation. Charge only previously uncounted late GPU usage. Record a new public decision and contract; commit/refund alone remain terminal under the old blueprint. Do not ask the user to decide this again.
- Lineage: keep packed shelves. Repack only the shelf whose tree changes. Keep the selected tree in view. Fixed positions must not allow tree overlap.
- One public codebase, self-hosted and cloud; private billing/fleet behind HTTP. PostgreSQL is durable authority. Redis is advisory. Worker completion requires physical work to return.
- Use PR workflows. The user authorized suitable cleanup merges in this session. Future feature PRs follow the normal user-merge rule unless the user authorizes otherwise.
- Use up to six Luna HIGH implementation/test agents in disjoint scopes; Sol6.1 HIGH adversarial reviews; Sol6.1 XHIGH or Astra XHIGH final gates. The current tool limit is three active children, so run the six workstreams in batches.

## Next work

Start with current main and one isolated branch. Restore the F1 recovery contract v4 and harness v3; verify their exact saved source identities, then run the approved actual-caller RED on new owned resources. The harness source passed review, but no recovery runtime or production repair ran. Do not skip RED or infer it from a source pass.

The shared backend sequence is **F1 recovery, S1 journal, S2 purge, S3 claim, then maintenance**. One agent owns the overlapping backend files. F2 startup admission is a separate blocking gate: queue/frame regional identity and reciprocal storage proof must be exact. Matching rate-limit endpoints, path strings, bucket names or readiness responses are not sufficient.

The worker maintenance repair needs independent source review before paired runtime tests. The P8 diagnostic repair needs source review before any capture. Keep the original strict 60 FPS failure. The roadmap has the remaining product, quota, fleet, gateway, capacity, portability, release and research slices and their dependencies.

Every restored packet must bind the source, review verdict, finite cases and new resource ownership. Archive manifests do not grant runtime leases. Never point destructive helpers at the developer database or an unknown container. No cloud expense, production rollout or GPU benchmark is needed for the first source/test slice.

## Verification and delivery

The final cleanup full `make verify` passed locally: 1,183 backend tests, 373 worker tests, 296 frontend unit tests, 63 canvas checks, one landing browser check and one real API sign-in check. All reported zero skips. Both builds and CSP checks passed; all 33 Mermaid diagrams rendered. Backend and worker combined line/branch coverage was 85% and 80%; XML line/branch rates were 88.29%/75.05% and 81.91%/73.99%. Frontend type checking had zero errors and six existing warnings. Borrowed dependencies were used with verified source paths; this was not a clean install or GPU test. The focused recovery file passed 18 tests. Source was unchanged during the gate, and the owned test databases, role and isolated fixture container were removed. Final logs and XML reports are in `verification-v2/`; the earlier pass remains in `verification/`. PR CI is a separate check.

Run a focused suite first, then full `make verify` under `/usr/bin/tmux`. From a worktree force `PYTHONPATH=<worktree>/backend:<worktree>/worker`; borrowed editable installs otherwise test another checkout. Use a new owned PostgreSQL role/database and set both `DATABASE_URL` and `POSTGRES_ADMIN_URL`. CPU inference, Compose, restore, diagrams, GPU and provider checks are separate gates. Do not claim clean installation from borrowed dependencies.

Before a push, start the runner with `make ci-runner-start` and check it is online and idle. Use REST for labels, milestone, assignee and PR updates; request Copilot after each push. Run Mermaid CLI with Chrome and `--no-sandbox` for changed diagrams. Every new commit needs `git commit -s`. Record state changes in local `.local/state.md`; update routed internals in place when behavior changes.

## Unresolved merge-message error

The main merge `006f408` has literal newline escapes, so its DCO sign-off is not on a separate line. Corrected commit `d7129bc35b369c06ca4251cdd03ca486bf4f73d7` has identical code, tree and parents and is saved as `archive/cleanup-20261005/merge-message-correction`. The user approved a protected replacement, but GitHub rejected it: main forbids force pushes and requires PRs. The remote commit remains unchanged. Do not bypass branch rules or report this history error as fixed.

## Suggested skills

Use `handoff` to keep this entry short, `merge-ready` for independent review and local gates, and `improve-codebase-architecture` for a scoped architecture change. Read each skill first. Local user instructions override generic skill defaults. Keep the existing issue/decision scope and avoid speculative interfaces.
