---
title: First run
subtitle: Choose how a new install works, with the one-use setup link.
tab: self-hosting
group: Install
order: 2
source: docs/self-hosting.md "Accounts" ("First run"), "Installing a release"; scripts/install.sh; scripts/ensure-env.sh
---

A new install does not start with logins, and it does not start in a fixed mode. It starts unclaimed. The first person who spends the setup link decides whether the install has accounts. This page covers that choice, what the studio does until someone makes it, and what an upgrade decides on its own.

## The setup link

The installer generates every secret, including the root key ring, starts the API, and prints a one-use setup link. The link has this form:

```text
http://localhost:8080/setup#<token>
```

The origin is `PUBLIC_URL`, which is `http://localhost:8080` by default. On a desktop session, the installer also opens the link in your browser. Without one, the printed link is the only copy, so keep it.

The link is good for one hour and works once. Only a hash of it is stored. Keep it private until someone has spent it.

## Choose how the install works

Whoever opens the link in a browser chooses one of two modes:

- **An administrator account.** You give an email and a password. Passwords must be 10 to 128 characters and must not appear in the bundled blocklist. The claimant takes over the implicit local user, so every job and asset made before accounts existed belongs to that administrator. Nothing is stranded and nothing is copied.
- **No accounts.** Every request resolves to one local administrator. This suits a single operator. Anyone who can reach the API can do anything the install can do, so keep it on a trusted network. Give `FLEET_SECRET` only to workers you trust, as described in [Remote GPU workers](/docs/self-hosting/remote-gpu-workers).

The choice is recorded in PostgreSQL. You can turn accounts on later. See [Accounts](/docs/self-hosting/accounts).

## Until someone chooses

Until the link is spent, every API route answers `503` with `{"detail": "setup_required"}`. The exceptions are the health and readiness probes, `GET /api/v1/config`, the setup routes and the fleet socket. The fleet socket lets a worker report its hardware before anyone signs in. The studio shows the setup page.

The setup page does not show the worker's hardware. `GET /api/v1/admin/models` answers `503` until setup is complete.

## The AUTH_MODE override

`AUTH_MODE` in `deploy/compose/.env` is an optional override. Leave it empty. The API then follows the recorded choice.

Setting `AUTH_MODE=none` or `AUTH_MODE=accounts` decides an install that has not chosen yet. The value must agree with an install that has chosen. If it does not agree, the API refuses to start and names the variable. Scripted installs and CI use the override and never see the setup page.

The mode is held per API process. An install without a pinned `AUTH_MODE` must run a single API process, as self-hosted Compose does. A scaled deployment must pin `AUTH_MODE` to the recorded choice.

An existing `.env` keeps its non-empty keys when the installer runs again, including `AUTH_MODE`. A release from before the setup link may still carry `AUTH_MODE=none` from the old template. On a new install, that line decides the mode before anyone sees the setup page. If you want to choose on the setup page, remove the line and restart the API.

## Installs that already exist

An install that already exists is decided at upgrade. It keeps accounts if it had them, and it is `none` if it holds any user or work. An upgrade therefore never shows the setup page.

## A lost setup link

From a source checkout, `make setup-link` prints a fresh setup link for an install that nobody has claimed yet. Start the install first with `make compose-up`.

```bash
make setup-link
```

A release install has no Makefile. From the install directory (`~/potocolom` by default), with `COMPOSE_PROJECT_NAME` unset in your shell, run:

```bash
env -u COMPOSE_PROJECT_NAME docker compose --env-file deploy/compose/.env -f deploy/compose/compose.yml exec -T api python -m app.operator setup-link
```

The installer prints this command, with the absolute paths of your install, when it cannot mint a link.

Either command replaces any setup link that is still outstanding. Both refuse once the install runs without accounts, and once an account holds an identity. If the API is not running, the command refuses. The release command exits with status 3 when the install has already chosen. The installer shows that as a note rather than a failure.

## Next steps

- [Accounts](/docs/self-hosting/accounts): turn accounts on later, recover access, and rotate the root key.
- [Installing a release](/docs/self-hosting/installing-a-release): the install steps and the upgrade notes.
