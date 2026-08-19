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
- **`AUTH_MODE=none` is the shipped default** and resolves every request to one local user. Setting `local` or `oauth` now refuses to start rather than silently behaving as `none`; they are seams, not implementations. Tracked in #5 and #9.
- **Thumbnail inspection proves structure, not decodability.** `_png_dimensions` and `_webp_dimensions` in `backend/app/storage.py` walk the container and chunk headers and never decode. A `VP8 ` chunk needs ten header bytes and a `VP8L` five, so a container carrying a header and no compressed data is accepted and a thumbnail row is created for a file no decoder will render. The result is a broken image in the gallery, chosen by the worker, and not active content. Self-hosted API responses carry `X-Content-Type-Options: nosniff`. Cloud presigned S3/MinIO GET URLs do not; they rely on stored `Content-Type` plus a CloudFront response-headers policy that sends nosniff (see [docs/cloud-infrastructure.md](docs/cloud-infrastructure.md)). A decoder does not belong on the request path: decoding here was twice a denial of service, once unbounded and once bounded by a peer-supplied size. Tracked in #281.
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
