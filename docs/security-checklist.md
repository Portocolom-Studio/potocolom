# Security and recovery checks

These checks are required for each change that affects input, accounts, data,
workers, external services, configuration, or CI. Apply the checks that the
change can affect. Record the test or operational evidence in the PR. Explain
any check that does not apply.

This is a review contract, not a claim that all controls ship. Remaining gaps
are linked to open issues in the table below. Update the facts there when that
work ships. A design, a stub, or a green test
that skips a required case does not prove the control.

## Required checks

| Control | Required evidence |
|---|---|
| Input validation | Reject invalid types, ranges, text, nesting and body sizes before storage or dispatch. Model parameter schemas must be usable. Cover rejected inputs and valid boundary values. |
| Password hashing | Use the shared salted Argon2id path. Prove stored values contain no password and equal passwords get different salts. |
| Secrets outside source | Load live secrets from protected configuration. Scan the tree and relevant history without printing candidate values. Keep fake test values and any narrow scanner allowances explicit. |
| Server auth | In accounts mode, check the session, role, account state and ownership on REST and WebSocket paths. Test direct calls without frontend checks. Keep cookie mutation CSRF checks. |
| Login and reset limits | Charge attempts before costly or account-specific work. Keep the separate caller buckets and uniform reset answer. Test overlap, exhaustion and recovery. |
| External call deadlines | Bound connection, read, retry and total operation time as needed. Prove stalled calls release their resources within a stated bound. Ending an await alone does not stop a blocking thread. |
| Payment and quota replay | Prove duplicate, delayed, conflicting and retried events cannot grant, reserve, charge or refund twice. Use durable unique source keys and transactions; persist settlement retries. |
| Transactions | Commit related database writes together and test rollback on a failed write. For storage work outside the database, prove ordered cleanup and retry recovery. Keep the recorded separate audit transaction. |
| Query indexes | Check real filter and sort plans with representative data. Add only indexes that improve the measured path, and record the write and storage cost. |
| Versioned migrations | Ship model changes with a versioned migration. Test upgrades from populated supported schemas and the declared downgrade behavior. Keep schema metadata aligned. |
| Safe logs and request IDs | Correlate HTTP handling with a bounded server request ID. Keep credentials, token URLs and private worker text out of logs and exception output. Use canary values and a positive logging control to prove this. |
| Store readiness | Prove /api/v1/ready checks PostgreSQL and the configured asset store and returns 503 when either is unavailable. /api/v1/health stays process liveness. |
| Backups and restore | Restore the database, asset files and required protected key configuration from one consistent recovery point into an isolated environment. Prove account sign-in and saved-image access. |
| Closed CORS | Keep HTTP same-origin unless a named origin is required. Check exact allowed origins and CSRF for cookie writes, and allowed browser origins on WebSockets. CORS does not replace auth. |
| Overload responses | Use 429 for a client budget refusal. Preserve documented 503 plus Retry-After for a busy service. Test refusal before expensive work and recovery after the limit clears. |
| Account isolation | Test two distinct accounts. Foreign record IDs, cursors, source assets and nested results must not expose or change another account's data. Test the explicit admin and share permissions separately. |

## Scope and accepted exceptions

AUTH_MODE=none remains the trusted-network, single-user mode. Accounts mode
uses server auth. Admin access and bearer share links are explicit permissions,
not failures of ownership checks. Worker releases keep protocol N-1 support.

Audit delivery remains a separate, fail-open transaction with its bounded
spool. Preserve the accepted waits when adding deadlines: the 3 second
account-lock timeout, the 30 second cap on a later row lock, and the unbounded
operator-collapse wait. Login identifier exhaustion returns 429; the busy
caller queue returns 503 with Retry-After. A change to an accepted rule needs
a new entry in [decisions.md](decisions.md).

Billing and cloud backup operations stay in the private repository. The public
repository owns the quota contract, caller, fake and settlement retry checks;
private services must pass that contract before a cloud acceptance claim. See
[repository-boundary.md](repository-boundary.md).

## Current evidence and open work

Review date: 2026-10-08, against public main 464f8ae and private main 2936bae.
The seven public gaps it found were tracked in [#686](https://github.com/Portocolom-Studio/potocolom/issues/686) and are
now closed; each PR below records its evidence. The rows that still link an
issue are open. Updated 2026-10-10, public main ada5ee7.
This does not prove production readiness.

| Area | Current fact | Open work |
|---|---|---|
| Validation | Request models and size limits ship. Model parameter schemas fail closed ([#708](https://github.com/Portocolom-Studio/potocolom/pull/708)). | Regression checks above |
| Passwords, accounts, login/reset, CORS and ownership | Core controls ship in their stated profiles. Keep their negative tests for each affected path. | Regression checks above |
| Secrets | Runtime values use ignored files or environment settings. CI scans pull requests and main for committed secrets ([#707](https://github.com/Portocolom-Studio/potocolom/pull/707)). | Regression checks above |
| Deadlines | Key HTTP and SMTP calls have limits. Serving database calls have connect, pool-wait and statement bounds; offline commands and migrations have no statement bound on purpose. S3 calls have connect, read and retry bounds, and the image read has a total bound ([#710](https://github.com/Portocolom-Studio/potocolom/pull/710)). | Regression checks above |
| API rate limits and public quota | Login/reset limits ship. General limits and the public quota caller/fake/outbox remain open. | [#21](https://github.com/Portocolom-Studio/potocolom/issues/21) |
| Payments | Private main contains docs, not a billing implementation or replay proof. | Portocolom-Studio/potocolom-cloud#1 |
| Database integrity and readiness | Core related writes use transactions; Alembic revisions 0001-0033 and store readiness ship. | Regression checks above |
| Indexes | Audit search, the admin asset count and the purge sweep have measured indexes ([#709](https://github.com/Portocolom-Studio/potocolom/pull/709)). | Regression checks above |
| Logs | Every HTTP response carries a server request ID that is also on its log records ([#712](https://github.com/Portocolom-Studio/potocolom/pull/712)). Access lines carry no query string, and worker failure text is not logged ([#713](https://github.com/Portocolom-Studio/potocolom/pull/713)). | Regression checks above |
| Recovery | `scripts/backup.sh` and `scripts/restore.sh` restore the database, assets and root keys into a new project; CI proves sign-in and image access after restore ([#711](https://github.com/Portocolom-Studio/potocolom/pull/711)). | Cloud operations: Portocolom-Studio/potocolom-cloud#5 |

Before closing an item, record the exact source, commands, result, skips and
environment. Keep the [release checks](release-checklist.md) and the routed
internal security notes current. A local fixture pass and a cloud restore
drill prove different things; record each where it applies.
