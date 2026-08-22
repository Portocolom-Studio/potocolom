# Security

## Reporting a vulnerability

Report privately through GitHub: open the [Security tab](https://github.com/Portocolom-Studio/potocolom/security/advisories/new) and use **Report a vulnerability**. That opens a private advisory visible only to you and the maintainers. Do not open a public issue for a vulnerability.

A useful report says what an attacker can do, which files or endpoints are involved, and how you confirmed it. A proof of concept helps but is not required.

potocolom is maintained by one person. Expect acknowledgement within a few days rather than a few hours. There is no response-time commitment and no bug bounty.

## Supported versions

No release is tagged yet. `main` is the only supported ref, and fixes land there.

## Scope

Read the list below before reporting. Several open surfaces here are recorded decisions rather than oversights, and reports about them cost you time without producing a fix. [docs/decisions.md](docs/decisions.md) carries the reasoning and the rejected alternatives for each.

Known and deliberate, so not vulnerabilities:

- **The fleet WebSocket (`/api/v1/fleet`) authenticates workers with a shared secret in `FLEET_TOKEN_KEY`, compared in constant time.** An unset key refuses the handshake (HTTP 403 before accept) and refuses to start the API. `scripts/preflight.sh` writes `deploy/compose/.env` on first run so a fresh install has a secret. Origin check stays. Signed cloud tokens remain #225. Both WebSocket endpoints reject a browser `Origin` that is not allowlisted, so a page you merely visit cannot reach them; a process on the same network still can, which is what worker authentication is for.
- **`AUTH_MODE=none` is the shipped default** and resolves every request to one implicit local admin. `AUTH_MODE=accounts` refuses to start until the accounts release lands.
- **Model discovery requires an authenticated principal.** Install operations, including studio GPU status, GPU history, telemetry preview, benchmark operations, and benchmark session reads and writes, require the admin role.
- **Asset reads use opaque asset IDs.** The API checks the asset owner or admin role, returns 404 for missing or unauthorized assets, and returns 400 for an unsafe download name. The retired storage-key GET route always returns 404. For local storage, worker input uses an opaque 32-byte capability that expires after 15 minutes and does not reveal the storage key.
- **Authenticated API responses are not cacheable.** Responses under `/api/v1/` include `Cache-Control: no-store`.
- **Thumbnail inspection proves structure, not decodability.** `_png_dimensions` and `_webp_dimensions` in `backend/app/storage.py` walk the container and chunk headers and never decode. A `VP8 ` chunk needs ten header bytes and a `VP8L` five, so a container carrying a header and no compressed data is accepted and a thumbnail row is created for a file no decoder will render. The result is a broken image in the gallery, chosen by the worker, and not active content. Self-hosted API responses carry `X-Content-Type-Options: nosniff`. Cloud presigned S3/MinIO GET URLs do not; they rely on stored `Content-Type` plus a CloudFront response-headers policy that sends nosniff (see [docs/cloud-infrastructure.md](docs/cloud-infrastructure.md)). A decoder does not belong on the request path: decoding here was twice a denial of service, once unbounded and once bounded by a peer-supplied size. Tracked in #281.
- **Audit fails open, and covers admin work that changes something.** An admin request with an unsafe method is recorded before it runs, in the admin role check itself rather than per route, so a route added later cannot forget. Admin reads are not recorded: the studio polls two of them every two seconds, and recording those would bury real administrator work under millions of rows any caller can drive for free. A read that reaches another user's data is a different thing, carries a target the role check cannot know, and will record itself when those routes exist. Today every admin route with an unsafe method sits behind `BENCHMARK_API`, which is off by default, so a default install writes no audit rows yet. When the record cannot reach PostgreSQL the action still proceeds: refusing privileged work every time the audit table is unreachable turns one incident into an outage. The loss is never silent. Each lost record is written as a structured JSON line on the `potocolom.audit` logger, a bounded 1000-event spool keeps it for the next successful insert, and the flush carries distinct high-severity `audit.fallback` and `audit.overflow` events so the seven-day summary shows the gap. A sustained outage past 1000 events drops the oldest and counts them into `audit.overflow`. Delivery is serialized and bounded to five seconds, so a slow audit cannot queue admin requests behind it. Records are kept 90 days. The actor and target are plain ids with no foreign key, so deleting an account erases neither what it did nor who did it.
- **Registration is invitation-only, and an invitation link is a bearer capability.** Anyone holding the link can claim that one address at that one role, once, within 72 hours. That is the point: a self-hosted install is not required to run mail, so the administrator copies the link and hands it over by whatever means they trust. Only the SHA-256 is stored, so the link is displayed once; asking to see it again mints a new one and retires the old, because a link nobody could read may have leaked on the way to them. Treat the link like a password.
- **Promotion to administrator needs evidence, and demotion needs none.** The caller must have authenticated in the last 30 minutes, and the target must either hold a verified address or be promoted under an explicit attestation, which is recorded against the account it promoted. On a no-mail install the attestation is the only evidence available, and it is deliberately a statement by a named administrator rather than a checkbox that leaves no trace. An administrator cannot change their own role at all, and the last administrator cannot be demoted, because an install with no administrator can only be recovered offline.
- **There is no rate limiting.** Deferred by recorded decision, see [docs/decisions.md](docs/decisions.md).
- **Pull requests execute contributor code on a self-hosted runner.** Accepted for a solo org and documented in [docs/self-hosted-runner.md](docs/self-hosted-runner.md).

In scope, and worth reporting:

- Anything that crosses a boundary the documentation claims holds. If a document says a check is enforced and it is not, that is a finding regardless of how small it looks.
- Anything reachable by a client that is not on the operator's private network. A web page the operator merely visits counts: report any way one reaches an endpoint despite the `Origin` check.
- Anything that lets one user reach another user's jobs, assets or sessions.
- Anything that lets a worker corrupt or forge state the API treats as authoritative.
- Anything in the shipped container images, compose stack or release artifacts.

Out of scope: findings in `design-sketches/` or other untracked local scratch, issues that require an already-compromised host, and results from automated scanners submitted without a working path to impact.

## Disclosure

Coordinated. Report privately, and we agree on a disclosure date once a fix exists or the issue is confirmed as won't-fix. Reporters are credited in the advisory unless they ask not to be.

## Self-hosting

Deployment posture is part of the security model for the self-hosted profile. [docs/self-hosting.md](docs/self-hosting.md) covers what to expose and what to keep on a trusted network. A misconfigured deployment is an ordinary issue, not an advisory.
