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
| Closed CORS | Keep HTTP same-origin unless a named origin is required. Check exact allowed origins on every unsafe HTTP request (cookie writes in accounts mode, every write in `none`, #854), CSRF for cookie writes, and allowed browser origins on WebSockets. CORS does not replace auth. |
| Overload responses | Use 429 for a client budget refusal. Preserve documented 503 plus Retry-After for a busy service. Test refusal before expensive work and recovery after the limit clears. |
| Account isolation | Test two distinct accounts. Foreign record IDs, cursors, source assets and nested results must not expose or change another account's data. Test the explicit admin and share permissions separately. |

## Scope and accepted exceptions

AUTH_MODE=none, chosen at setup or set by the override, remains the
trusted-network, single-user mode. An install that has not chosen yet answers
only the setup surface. Accounts mode uses server auth. Admin access and bearer share links are explicit permissions,
not failures of ownership checks. Worker releases keep protocol N-1 support.

Audit delivery remains a separate, fail-open transaction with its bounded
process-memory spool. A process crash during a database outage loses records
still in that spool. Preserve the accepted waits when adding deadlines: the 3 second
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
| First-run decision (#792) | A new install is unclaimed. Until the one-use setup link is spent, every API route except config, health, ready, the setup route and the fleet socket answers 503 `setup_required` before it reads data. The link lasts one hour and is stored only as a hash; every bad link gets the same 403. Both choices, and every refusal, run in one transaction with the link spend. Concurrent claims serialize on SETUP_LOCK, and a claim refuses a database that already says none. The runtime enable needs an allowed Origin, refuses an explicit `AUTH_MODE=none` override and a mode that was already switched elsewhere, and closes canvas sockets opened without a session; a socket withholding its first open is refused after the switch. The installer writes ROOT_KEYS once. `make setup-link` and the installer mint the link inside the running compose API, and refuse rather than mint in another database when that API is stopped or Docker cannot answer, `make setup-link` is the compose install only, and `make dev-setup-link` is the native dev stack only; neither guesses ([`test-setup-link.sh`](../scripts/test-setup-link.sh)). Evidence: [`test_installation_decision.py`](../backend/tests/test_installation_decision.py) (startup matrix, gate, both choices, two-link races, runtime switch with socket close, offline link), [`test_migration_0035.py`](../backend/tests/test_migration_0035.py), [`test-ensure-env-root-keys.sh`](../scripts/test-ensure-env-root-keys.sh), and the mutation check below. | Not proved: the installer printing the link against a live docker stack (only the stubbed installer tests run), and the browser setup page (#795). The anonymous setup route and the runtime enable have no rate limit; the one-hour, one-use link is the only limit. A request authorized as the implicit administrator before the switch and still running when it completes is accepted, because every caller of a none install is that administrator (see decisions.md). Tracked in [#686](https://github.com/Portocolom-Studio/potocolom/issues/686). |
| Passwords, accounts, login/reset, CORS and ownership | Core controls ship in their stated profiles. Keep their negative tests for each affected path. | Regression checks above |
| Secrets | Runtime values use ignored files or environment settings. Candidate [#767](https://github.com/Portocolom-Studio/potocolom/pull/767) serializes account setup with upgrades, preserves effective Compose root keys, and replaces the env file through private destination-local files; `scripts/test-auth-enable-locking.sh` covers these guards. CI scans pull requests and main for committed secrets ([#707](https://github.com/Portocolom-Studio/potocolom/pull/707)). | Regression checks above |
| Deadlines | Key HTTP and SMTP calls have limits. Serving database calls have connect, pool-wait and statement bounds; offline commands and migrations have no statement bound on purpose. S3 calls have connect, read and retry bounds, and the image read has a total bound ([#710](https://github.com/Portocolom-Studio/potocolom/pull/710)). | Regression checks above |
| API rate limits and public quota | Login/reset limits ship. General limits and the public quota caller/fake/outbox remain open. | [#21](https://github.com/Portocolom-Studio/potocolom/issues/21) |
| Payments | Private main contains docs, not a billing implementation or replay proof. | Portocolom-Studio/potocolom-cloud#1 |
| Database integrity and readiness | Core related writes use transactions; Alembic revisions 0001-0035 and store readiness ship. Revision 0035 records the installation's auth decision, and [`test_migration_0035.py`](../backend/tests/test_migration_0035.py) covers its three starting states and its downgrade. Revision 0034 preserves proved command identity and treats unknown original owners as unknown; [`test_command_identity_migration.py`](../backend/tests/test_command_identity_migration.py) covers populated upgrades and malformed retained identities. [`test_s1_transaction_races.py`](../backend/tests/test_s1_transaction_races.py) covers atomic revocation and erasure; [`test_s1_collapse_order.py`](../backend/tests/test_s1_collapse_order.py) checks complete account journal locks before a regional lease. [`test_auth_r2.py`](../backend/tests/test_auth_r2.py) covers `test_health_and_readiness_when_connect_fails`, `test_readiness_when_required_stores_are_available` and `test_readiness_reports_unavailable_asset_store`; [`test_deadlines.py`](../backend/tests/test_deadlines.py) proves the probe ends within its bound. | Regression checks above |
| Indexes | Audit search, the admin asset count and the purge sweep have measured indexes ([#709](https://github.com/Portocolom-Studio/potocolom/pull/709)). | Regression checks above |
| Logs | Every HTTP response carries a server request ID included on API application log records ([#712](https://github.com/Portocolom-Studio/potocolom/pull/712)); this does not cover Uvicorn access lines, worker logs or a shared API-to-worker trace. Candidate [#766](https://github.com/Portocolom-Studio/potocolom/pull/766) removes userinfo, query and fragment from worker HTTP URLs, including literal apostrophes, in plain/JSON messages and chained exceptions; [`worker/tests/test_logs.py`](../worker/tests/test_logs.py) uses real HTTP responses and canaries. This covers signed URLs, not arbitrary private text in worker exceptions. Access lines carry no query string, and worker failure text sent to the API is not logged ([#713](https://github.com/Portocolom-Studio/potocolom/pull/713)). See [`test_request_id.py`](../backend/tests/test_request_id.py) and [`test_log_secrets.py`](../backend/tests/test_log_secrets.py). CloudWatch export, operational metrics/alerts and Sentry are designed, not shipped. | Regression checks above |
| Recovery | `scripts/backup.sh` and `scripts/restore.sh` restore the database, assets and root keys into a new project; CI proves this local self-hosted integration path with sign-in and image access after restore ([#711](https://github.com/Portocolom-Studio/potocolom/pull/711)). The postgres healthcheck connects over TCP, so a restore on a new volume waits for the real server and not the first-start one; `scripts/test-postgres-ready.sh` proves it in CI ([#899](https://github.com/Portocolom-Studio/potocolom/pull/899)). This does not prove cloud PITR or production RPO/RTO. | Cloud operations: Portocolom-Studio/potocolom-cloud#5 |
| CI and dependencies | Every `actions/checkout` sets `persist-credentials: false`, and `make lint-workflows` runs pinned, SHA-256 checked actionlint and zizmor on every workflow ([#871](https://github.com/Portocolom-Studio/potocolom/issues/871)). `make check-python-deps` runs pip-audit on the backend and worker venvs in CI and fails on any finding not recorded in the script; six torch 2.9 advisories are recorded until the worker images move off torch 2.9, and diffusers (a URL install) is not audited ([#872](https://github.com/Portocolom-Studio/potocolom/issues/872)). Dependabot proposes weekly grouped updates for actions, npm, pip and Docker with a seven-day cooldown and no auto-merge ([#873](https://github.com/Portocolom-Studio/potocolom/issues/873)). `make shellcheck` checks every tracked shell script at error severity ([#866](https://github.com/Portocolom-Studio/potocolom/issues/866)). | Lock files for the Python projects; raise the shellcheck severity; drop the torch exceptions when the pin moves |

Before closing an item, record the exact source, commands, result, skips and
environment. Keep the [release checks](release-checklist.md) and the routed
internal security notes current. A local fixture pass and a cloud restore
drill prove different things; record each where it applies.

### Mutation check for #792

Each guard was broken on purpose in the working tree, the named test was run
from `backend/` with `PYTHONPATH` set to that tree, and the file was restored
byte for byte before the next guard. A guard counts as covered when its test
fails with the guard broken.

| Guard broken on purpose | Test that must fail | Result |
|---|---|---|
| Setup gate lets every request through | `test_an_unclaimed_install_answers_setup_required_outside_the_setup_surface` | killed |
| Claim does not spend the link | `test_first_admin_setup.py::test_a_claimed_link_cannot_be_replayed` | killed |
| No SETUP_LOCK in the no-accounts choice | `test_a_live_link_cannot_switch_a_claimed_install_to_none_beside_a_claim` | killed |
| No SETUP_LOCK in the accounts claim | `test_two_live_links_cannot_both_claim_the_installation` | killed |
| Startup ignores the recorded decision | `test_startup_starts_or_refuses_as_the_matrix_says` | killed |
| Migration decides every install unclaimed | `test_migration_0035.py::test_an_install_with_people_is_decided_none_so_an_upgrade_never_shows_setup` | killed |
| Runtime switch leaves none-mode canvases open | `test_a_canvas_open_when_accounts_turn_on_is_closed` | killed |
| ensure-env overwrites a non-empty ROOT_KEYS | `scripts/test-ensure-env-root-keys.sh` | killed |
| Offline switch is not refused: the mode is not re-read under SETUP_LOCK | `test_a_tokenless_switch_is_refused_once_accounts_were_turned_on_offline` | killed |
| Sessionless socket stays live after the switch | `test_a_socket_that_withholds_open_across_the_switch_is_refused` | killed |
| Setup link passed to xdg-open on the command line | `scripts/test-install-setup-link.sh` | killed |
| Compose setup-link mints without a running API | `test-setup-link.sh`, stopped-api case | killed |
| Dev setup-link ignores an exported PUBLIC_URL | `test-setup-link.sh`, exported-URL case | killed |
| Installer treats already-decided as a failure | `test-install-public-url.sh`, decided case | killed |
| Installer hides a failed mint | `test-install-public-url.sh`, failed-mint case | killed |
| Explicit AUTH_MODE=none override does not refuse the switch | `test_a_switch_refuses_an_explicit_none_override_and_names_the_variable` | killed |

The first pass left the two SETUP_LOCK mutations alive. The existing race tests
reuse one link, which its own row already serializes. The new race tests plant a
second live link and slow the password hash so the two claims overlap, and the
losing side must return 409. Without the lock the unique password-identity index
still stops a double adoption, but the loser then fails with a database error
rather than the claimed answer, and only the 409 assertion tells the two apart.
The same pass added a claim-side refusal when the database says none, which the
no-accounts choice already had.

Open for #686: the mutation check covers the tests, not a live docker stack or
the desktop opener on a real session.
