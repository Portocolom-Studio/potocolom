---
title: API overview
subtitle: Conventions, versions and what the API covers.
tab: api
group: Start here
order: 1
source: docs/api.md "Conventions"; docs/blueprint.md "The mode seam"
---

The potocolom API is one HTTP surface and two WebSocket endpoints. The studio in the browser calls it. So do the benchmark script and the worker connection. This page lists the rules that every route follows. The [authentication page](/docs/api/authentication) explains how a request names its user, and the [endpoints page](/docs/api/endpoints) lists every route that ships.

## Base path and versioning

- **Base path:** every REST route starts with `/api/v1`. Request and response bodies are JSON.
- **Versioning:** the version is the path prefix. Within a release, the API makes only additive changes to responses, so the studio from the previous release keeps working for the same window.
- **Worker protocol:** the worker connection has its own protocol version. The API accepts the current version and the one before it.

## Responses and errors

- **REST errors:** the body is `{"detail": "..."}` with a normal HTTP status code.
- **WebSocket errors:** the server sends `{"type": "error", "code": <int>, "message": "..."}` and then closes the socket with the same code. The close codes are listed on the [realtime socket page](/docs/api/realtime-socket).
- **Request IDs:** every HTTP response has an `X-Request-ID` header with 32 hex characters. The server makes the value, and a value sent by the client is ignored. Give this ID to the operator when you report a problem, because it matches the server log lines. WebSocket connections have no request ID.
- **Caching:** responses under `/api/v1/` send `Cache-Control: no-store`.
- **Body size:** JSON and form bodies are capped at 1 MiB, file uploads at 64 MiB, and SES feedback at 512 KiB. A larger body gets `413` with `{"detail": "request body too large"}` before the route runs.

## Authentication in one line

- **Modes:** a new install is unclaimed until someone spends its setup link, and that link chooses the mode. In `none` mode every request is the one implicit local admin, and an unsafe request still needs an allowed `Origin`. In `accounts` mode a request must present a session cookie or an `Authorization: Bearer` token. See the [authentication page](/docs/api/authentication) for the details.
- **Unclaimed installs:** until the setup link is spent, every route answers `503` with `{"detail": "setup_required"}`. The exceptions are the config, health and readiness routes, the two setup routes and the worker socket. A browser WebSocket is closed before it is accepted, so its handshake fails instead.

## Roles in one line

- **Roles:** `viewer` only reads, `user` also changes its own work, and `admin` also manages the install and other accounts. A write endpoint answers `403` to a `viewer`.

## Example responses

The health check answers with process state only:

```json
{ "status": "ok" }
```

A body that is too large gets this answer before the route runs:

```json
{ "detail": "request body too large" }
```

Until the install is claimed, most routes answer this instead:

```json
{ "detail": "setup_required" }
```

## Where to go next

- [Authentication](/docs/api/authentication): how a request names its user.
- [Endpoints](/docs/api/endpoints): every route that ships, by purpose.
- [Realtime socket](/docs/api/realtime-socket): the drawing WebSocket and its close codes.
