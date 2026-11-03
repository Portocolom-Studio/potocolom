---
title: Endpoints
subtitle: Every endpoint that ships, grouped by purpose.
tab: api
group: Endpoints
order: 3
source: docs/api.md "Endpoint catalogue", "Implemented endpoints", "Administration"
---

> [!PLANNED]
> Other endpoints are designed, and their request and response shapes are fixed in [docs/blueprint.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/blueprint.md). This page lists only the routes that ship.

Every REST path below starts with `/api/v1`. Unless a row says otherwise, the caller must be signed in. In `AUTH_MODE=none` the account routes answer `404`. The `user` role can write its own work, `viewer` is read only, and `admin` covers install settings and other accounts. See the [authentication page](/docs/api/authentication) for the rules.

## Health and configuration

| Method    | Path             | Purpose                                                                            |
| --------- | ---------------- | ---------------------------------------------------------------------------------- |
| GET, HEAD | `/api/v1/health` | Process liveness for the load balancer. HEAD answers with no body.                 |
| GET       | `/api/v1/ready`  | PostgreSQL and asset storage readiness. `200` when both are up, `503` otherwise.   |
| GET       | `/api/v1/config` | Runtime settings for the studio: sign-in methods, languages and the status banner. |

## Realtime and fleet sockets

| Method | Path               | Purpose                                                                                             |
| ------ | ------------------ | --------------------------------------------------------------------------------------------------- |
| WS     | `/api/v1/realtime` | Realtime drawing session for the studio. See the [realtime socket page](/docs/api/realtime-socket). |
| WS     | `/api/v1/fleet`    | Worker connection, not for browsers. Needs the `X-Fleet-Token` header.                              |

## Models and generations

| Method | Path                               | Purpose                                                                               |
| ------ | ---------------------------------- | ------------------------------------------------------------------------------------- |
| GET    | `/api/v1/models`                   | Registered models, with parameter schemas and GPU-time estimates.                     |
| POST   | `/api/v1/generations`              | Queue a job: text to image, image to image or upscale. Answers `202` with the job ID. |
| GET    | `/api/v1/generations`              | The caller's generation history, with cursor paging and filters.                      |
| GET    | `/api/v1/generations/models`       | Model IDs used by the caller's saved images.                                          |
| GET    | `/api/v1/generations/{id}`         | One job's state, timings and result asset.                                            |
| GET    | `/api/v1/generations/{id}/events`  | Server-sent events with job progress, until a final state.                            |
| POST   | `/api/v1/generations/{id}/cancel`  | Stop a queued or running job. The owner or an admin may call it.                      |
| POST   | `/api/v1/generations/{id}/star`    | Star one of the caller's generations. Idempotent.                                     |
| DELETE | `/api/v1/generations/{id}/star`    | Remove the star. Idempotent.                                                          |
| GET    | `/api/v1/generations/{id}/lineage` | Ancestors, direct children and subtree size of one generation.                        |
| GET    | `/api/v1/generations/{id}/subtree` | Bounded descendants and render data for one canvas tree.                              |

## Assets and files

| Method | Path                   | Purpose                                                                                 |
| ------ | ---------------------- | --------------------------------------------------------------------------------------- |
| GET    | `/api/v1/assets/{id}`  | Bytes of an asset the caller owns. An admin reading another account's asset is audited. |
| PUT    | `/api/v1/files/{key}`  | Local storage upload target for workers. Each key is write once.                        |
| GET    | `/api/v1/worker-input` | Source image for a worker, behind a 15-minute capability.                               |

## Sharing

| Method | Path                     | Purpose                                                                 |
| ------ | ------------------------ | ----------------------------------------------------------------------- |
| POST   | `/api/v1/shares`         | Create a link to one asset, for 1, 7 or 30 days. Needs the `user` role. |
| DELETE | `/api/v1/shares/{id}`    | Revoke a link. The link stops resolving.                                |
| POST   | `/api/v1/shared`         | Resolve a link token. Needs no account.                                 |
| GET    | `/api/v1/shared-picture` | Picture bytes for a resolved share. Needs no account.                   |

## Studio, metrics and usage

| Method | Path                          | Purpose                                                               |
| ------ | ----------------------------- | --------------------------------------------------------------------- |
| GET    | `/api/v1/studio/gpu`          | Live GPU snapshot and the models loaded on it. Admin only.            |
| GET    | `/api/v1/metrics/gpu/history` | GPU samples over a time range. Admin only.                            |
| GET    | `/api/v1/usage/me`            | The caller's own usage totals, by model and category.                 |
| GET    | `/api/v1/telemetry/preview`   | The anonymous telemetry payload for the previous UTC day. Admin only. |

## Benchmark

These routes are for the benchmark script. Every route needs an admin. The session reads need nothing else, and the other routes also need `BENCHMARK_API=true`.

| Method | Path                              | Purpose                                                                          |
| ------ | --------------------------------- | -------------------------------------------------------------------------------- |
| GET    | `/api/v1/benchmark/sessions`      | Session summaries, newest first. Pass the last ID as `cursor` for the next page. |
| GET    | `/api/v1/benchmark/sessions/{id}` | The full report for one session.                                                 |
| POST   | `/api/v1/benchmark/sessions`      | Store a completed benchmark report. Answers `201`.                               |
| GET    | `/api/v1/benchmark/models`        | Models that can be benchmarked.                                                  |
| GET    | `/api/v1/benchmark/gpu`           | Live GPU status from a connected worker.                                         |
| POST   | `/api/v1/benchmark/gpu/load`      | Load a model for a benchmark run.                                                |
| POST   | `/api/v1/benchmark/gpu/unload`    | Unload that model.                                                               |

## Authentication

| Method | Path                               | Purpose                                                         |
| ------ | ---------------------------------- | --------------------------------------------------------------- |
| POST   | `/api/v1/auth/setup`               | Claim the installation with the one-use setup link.             |
| POST   | `/api/v1/auth/register`            | Accept an invitation and set a password.                        |
| POST   | `/api/v1/auth/login`               | Password sign-in. Sets the session cookies.                     |
| POST   | `/api/v1/auth/totp`                | Answer a sign-in challenge with a TOTP code or a recovery code. |
| POST   | `/api/v1/auth/logout`              | End the current session and clear the cookies.                  |
| GET    | `/api/v1/auth/redirect/{provider}` | Start a Google or GitHub sign-in.                               |
| GET    | `/api/v1/auth/callback/{provider}` | Finish a provider sign-in or link.                              |
| POST   | `/api/v1/auth/reset`               | Ask for a password reset link. Always answers `202`.            |
| POST   | `/api/v1/auth/reset/complete`      | Spend a reset or recovery link and set a password.              |

## Account

| Method | Path                                    | Purpose                                                                                  |
| ------ | --------------------------------------- | ---------------------------------------------------------------------------------------- |
| GET    | `/api/v1/account`                       | The account, its live sessions, whether a second factor is on, and its linked providers. |
| POST   | `/api/v1/account/password`              | Change or add a password. Needs recent authentication.                                   |
| POST   | `/api/v1/account/email`                 | Change the primary email address.                                                        |
| DELETE | `/api/v1/account/sessions/{id}`         | Sign out one of the account's own sessions.                                              |
| POST   | `/api/v1/account/totp`                  | Start enrolling a second factor. Needs recent authentication.                            |
| POST   | `/api/v1/account/totp/confirm`          | Confirm an enrolment with a code from the authenticator.                                 |
| DELETE | `/api/v1/account/totp`                  | Remove the second factor. Needs recent authentication and a code.                        |
| POST   | `/api/v1/account/identities/{provider}` | Start linking a provider. Needs recent authentication.                                   |
| DELETE | `/api/v1/account/identities/{provider}` | Unlink a provider. Refused when it is the last way in.                                   |
| GET    | `/api/v1/account/export`                | Everything the install holds about the account, as streamed JSON. No secrets.            |
| DELETE | `/api/v1/account`                       | Stop the account now. Its data is purged after 30 days.                                  |

## Invitations and users

These routes are for admins.

| Method | Path                              | Purpose                                                                     |
| ------ | --------------------------------- | --------------------------------------------------------------------------- |
| POST   | `/api/v1/invitations`             | Invite an address to a role. The link is returned once.                     |
| GET    | `/api/v1/invitations`             | Open invitations, without their links.                                      |
| DELETE | `/api/v1/invitations/{id}`        | Revoke an open invitation.                                                  |
| POST   | `/api/v1/invitations/{id}/reveal` | Make a new link and retire the previous one.                                |
| GET    | `/api/v1/users`                   | Accounts with their role and state. Cursor paging and an email search.      |
| GET    | `/api/v1/users/{id}`              | One account. The read is audited.                                           |
| GET    | `/api/v1/users/{id}/generations`  | One account's generations, read only.                                       |
| POST   | `/api/v1/users/{id}/role`         | Change an account's role.                                                   |
| POST   | `/api/v1/users/{id}/state`        | Suspend, disable, reactivate or mark an account for deletion.               |
| POST   | `/api/v1/users/{id}/restore`      | Put back an account that is waiting for deletion, inside its 30-day window. |

## Audit

These routes are for admins.

| Method | Path                      | Purpose                                                              |
| ------ | ------------------------- | -------------------------------------------------------------------- |
| GET    | `/api/v1/audit`           | Search the audit log.                                                |
| GET    | `/api/v1/audit/summary`   | Seven days of privileged actions, and the gaps in them.              |
| GET    | `/api/v1/audit/anomalies` | Administrators who opened unusually many accounts.                   |
| GET    | `/api/v1/audit/export`    | The filtered audit as a JSON download. The export is itself audited. |

## Mail and status banner

| Method | Path                    | Purpose                                                                                                     |
| ------ | ----------------------- | ----------------------------------------------------------------------------------------------------------- |
| GET    | `/api/v1/mail/status`   | What the mail outbox is doing. Admin only.                                                                  |
| POST   | `/api/v1/mail/feedback` | SES bounce and complaint notices from SNS. Answers `404` unless `EMAIL_BACKEND=ses`. No account credential. |
| PUT    | `/api/v1/admin/banner`  | Set the status banner. Answers `204`. Admin only.                                                           |
| DELETE | `/api/v1/admin/banner`  | Clear the status banner. Answers `204`. Admin only.                                                         |
