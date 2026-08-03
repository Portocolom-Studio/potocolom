# Security

## Reporting a vulnerability

Report privately through GitHub: open the [Security tab](https://github.com/Portocolom-Studio/potocolom/security/advisories/new) and use **Report a vulnerability**. That opens a private advisory visible only to you and the maintainers. Do not open a public issue for a vulnerability.

A useful report says what an attacker can do, which files or endpoints are involved, and how you confirmed it. A proof of concept helps but is not required.

potocolom is maintained by one person. Expect acknowledgement within a few days rather than a few hours. There is no response-time commitment and no bug bounty.

## Supported versions

No release is tagged yet. `main` is the only supported ref, and fixes land there.

## Scope

The project documents its security posture in [docs/internals/08-security-production.md](docs/internals/08-security-production.md), which separates what is shipped from what is designed and deferred. Read the deferred list before reporting: several open surfaces are recorded decisions rather than oversights, and reports about them cost you time without producing a fix.

Known and deliberate, so not vulnerabilities:

- **The fleet WebSocket (`/api/v1/fleet`) is unauthenticated.** Documented in [README.md](README.md) and mitigated by deployment posture: treat the host as a trusted LAN. Tracked in #206.
- **`AUTH_MODE=none` is the shipped default** and resolves every request to one local user. Only that mode exists; `local` and `oauth` are seams, not implementations. Tracked in #5 and #9.
- **There is no rate limiting.** Deferred by recorded decision.
- **Pull requests execute contributor code on a self-hosted runner.** Accepted for a solo org and documented in [docs/self-hosted-runner.md](docs/self-hosted-runner.md).

In scope, and worth reporting:

- Anything that crosses a boundary the documentation claims holds. If a document says a check is enforced and it is not, that is a finding regardless of how small it looks.
- Anything reachable by a client that is not on the trusted LAN, including a web page the operator merely visits.
- Anything that lets one user reach another user's jobs, assets or sessions.
- Anything that lets a worker corrupt or forge state the API treats as authoritative.
- Anything in the shipped container images, compose stack or release artifacts.

Out of scope: findings in `design-sketches/` or other untracked local scratch, issues that require an already-compromised host, and results from automated scanners submitted without a working path to impact.

## Disclosure

Coordinated. Report privately, and we agree on a disclosure date once a fix exists or the issue is confirmed as won't-fix. Reporters are credited in the advisory unless they ask not to be.

## Self-hosting

Deployment posture is part of the security model for the self-hosted profile. [docs/self-hosting.md](docs/self-hosting.md) covers what to expose and what to keep on a trusted network. A misconfigured deployment is an ordinary issue, not an advisory.
