---
title: Security checklist
subtitle: The checks a change that touches security must pass.
tab: contributing
group: Maintain
order: 4
source: docs/security-checklist.md "Required checks" (rows Input validation through Account isolation), "Scope and accepted exceptions", and the closing paragraph of "Current evidence and open work" (recording evidence)
---

A change that affects input, accounts, stored data, workers, external services, configuration or CI must pass each check below that the change can affect. Record the test or operational evidence in the pull request. If a check does not apply, write one line in the pull request that says why.

Passing these checks does not prove that the product is ready for production. The checks are a review standard. They do not claim that every control is built.

> [!WARNING]
> A design, a stub, or a passing test that skips a required case does not prove the control.

## Required checks

1. **Input validation:** Reject invalid types, ranges, text, nesting and body sizes before the data is stored or sent to a worker. Model parameter schemas must be usable. Test rejected inputs and valid boundary values.
2. **Password hashing:** Use the shared salted Argon2id path. Prove that stored values do not contain the password, and that two equal passwords get different salts.
3. **Secrets outside source:** Load live secrets from protected configuration. Scan the working tree and the relevant history without printing any candidate value. Keep fake test values and any scanner allowance explicit. Locally, run `make verify-secrets`, and run `scripts/secret-scan.sh history` for the full history.
4. **Server authentication:** In accounts mode, check the session, the role, the account state and the ownership on every REST and WebSocket path. Test direct calls that skip the frontend. Keep the CSRF checks on requests that change cookies.
5. **Login and reset limits:** Charge each attempt before any costly or account-specific work. Keep the separate limits for each caller, and keep the reset answer the same for every request. Test overlapping requests, exhausted limits and recovery.
6. **External call deadlines:** Set limits on connection, read, retry and total time where they apply. Prove that a stalled call releases its resources within a stated limit. Ending an await alone does not stop a blocking thread.
7. **Quota and payment replay:** Prove that duplicate, delayed, conflicting and retried events cannot grant, reserve, charge or refund anything twice. Use durable unique keys for each source event and database transactions.
8. **Transactions:** Commit related database writes together, and test the rollback after a failed write. For storage work outside the database, prove that cleanup runs in order and that a retry recovers.
9. **Query indexes:** Check the real filter and sort plans with representative data. Add only the indexes that improve a measured path, and record their cost for writes and storage.
10. **Versioned migrations:** Ship each model change with a versioned migration. Test upgrades from populated, supported schemas, and test the downgrade behavior that the migration declares. Keep the schema metadata in line with the models.
11. **Safe logs and request IDs:** Link each HTTP request to a bounded server request ID. Keep credentials, token URLs and private worker text out of logs and exception output. Prove this with canary values and a check that shows the logging works.
12. **Store readiness:** Prove that `/api/v1/ready` checks PostgreSQL and the asset store, and returns 503 when either one is down. `/api/v1/health` only shows that the process is running.
13. **Backups and restore:** Restore the database, the asset files and the required protected key configuration from one consistent recovery point, into an isolated environment. Prove that an account can sign in and open its saved images. A local restore test proves only the local path.
14. **Closed CORS:** Keep HTTP requests on the same origin unless a named origin is needed. Check the exact allowed origins on every state-changing HTTP request (cookie writes in accounts mode, and every write in `none` mode), the CSRF protection for cookie-changing requests, and the allowed browser origins for WebSockets. CORS does not replace authentication.
15. **Overload responses:** Return 429 when a client goes over its budget. Keep the documented 503 with a `Retry-After` header for a busy service. Test that the refusal happens before any costly work, and that the service recovers after the limit clears.
16. **Account isolation:** Test with two separate accounts. Record IDs, cursors, source assets and nested results that belong to one account must not show or change another account's data. Test the admin and share permissions on their own.

## Scope and accepted exceptions

`AUTH_MODE=none`, chosen at setup or set by the override, is the trusted-network, single-user mode. An install that has not chosen yet answers only the setup surface. In this mode, the server checks for accounts do not apply. Accounts mode uses the server checks in check 4. Admin access and bearer share links are explicit permissions. They are not failures of the ownership checks.

Worker releases must still work with the previous protocol version (N-1).

> [!WARNING]
> Audit records are written in a separate transaction, and the audit write does not stop a request when it fails. Records wait in a bounded spool in the process memory. If the process crashes during a database outage, the records still in that spool are lost.

When you add a deadline, keep the waits that the project accepts on purpose: the 3 second account lock timeout, the 30 second cap on a later row lock, and the unbounded wait when an operator turns accounts off. A change to an accepted rule needs a new entry in [the decisions log](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/decisions.md).

## Recording evidence

Write the evidence in the pull request, in this order:

1. The check name.
2. The test file or the command you ran.
3. The result, and any test that was skipped.
4. The environment, when the result depends on it, such as the database version or the browser.
5. For each check that does not apply, one line that explains why.
