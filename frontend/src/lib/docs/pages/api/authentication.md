---
title: Authentication
subtitle: How a session cookie or a bearer token identifies a user.
tab: api
group: Start here
order: 2
source: docs/api.md "Conventions", "Authentication (shipped, kept here for the exact shapes)"; docs/blueprint.md "The mode seam"; docs/self-hosting.md "First run"; backend/app/accounts.py, backend/app/auth.py, backend/app/sessions.py, backend/app/recovery.py, backend/app/factors.py, backend/app/enable.py, backend/app/passwords.py, backend/app/auth_mode.py
---

Every request resolves to a principal, which is the account and role the API acts for. How the API finds that account depends on `AUTH_MODE`.

## Modes

- **Unclaimed:** a new install starts here. Until someone spends the setup link, every API route answers `503` with `{"detail": "setup_required"}`, except the few routes that must work before setup. The setup link chooses `none` or `accounts`.
- **`none`:** the install runs without accounts. Every request is the one implicit local admin, and nobody signs in. The account routes answer `404`, and `GET /api/v1/config` returns an empty `auth_methods` list.
- **`accounts`:** every request must present a session cookie or a bearer token. Sign-in, invitations and the account routes are active.

The `AUTH_MODE` environment variable is an optional override. When it is unset, the database decides. When it is set, it must agree with the recorded choice, or the API refuses to start and names the variable.

This page covers `accounts` mode.

## Roles

Each account has one role. From lowest to highest they are `viewer`, `user` and `admin`.

- A `viewer` reads, and cannot change anything.
- A `user` creates and changes its own work.
- An `admin` can do everything, including install settings and the routes that read other accounts.

Write endpoints need the `user` or `admin` role. A `viewer` gets `403` from them.

Promoting an account to `admin` needs recent authentication from the caller, and either a verified address on the target account or an explicit `attested` flag. An administrator cannot change their own role, and the last administrator cannot be demoted. A role change ends every session the account holds, because an old session carries the old role.

## Sessions and cookies

A session token is 32 random bytes. The server stores only its SHA-256 hash, so a copy of the database does not hand out live sessions.

The browser holds two cookies. The names depend on the scheme:

| Cookie  | Over HTTPS                 | Over plain HTTP     | Readable by the page |
| ------- | -------------------------- | ------------------- | -------------------- |
| Session | `__Host-potocolom_session` | `potocolom_session` | No (HttpOnly)        |
| CSRF    | `__Host-potocolom_csrf`    | `potocolom_csrf`    | Yes                  |

The session cookie is `SameSite=Lax`, host-only and has `Path=/`. The CSRF cookie must be readable, because the browser has to send its value back in a header. The `__Host-` prefix requires `Secure`, and browsers drop `Secure` cookies on plain HTTP. LAN self-hosting uses plain HTTP, so it gets the names without the prefix.

Session lengths:

- A normal session lasts 12 hours.
- Remember-me lasts 30 days, with a 7-day idle window.
- An administrator's session lasts 12 hours, with a 30-minute idle window. An administrator cannot be remembered.

Recent authentication is a separate grant. It lasts 30 minutes and signing in grants it. Routes that change how an account is proved need it.

## Requests from a browser

A request that changes data (`POST`, `PUT` or `DELETE`) and uses the session cookie must send both of these:

- An `Origin` header that is the install's public address or one of its `ALLOWED_ORIGINS`, matched exactly.
- An `X-CSRF-Token` header whose value equals the CSRF cookie.

If either is missing or wrong, the API answers `403`. An unsafe request with no `Origin` is refused.

In `none` mode a `POST`, `PUT`, `PATCH` or `DELETE` needs only the `Origin` header, which must be the public address or one of `ALLOWED_ORIGINS`. Otherwise it answers `403`. Scripts that call the API in `none` mode send the same header.

A request that sends `Authorization: Bearer <token>` needs neither header. A bearer is presented deliberately rather than sent along by the browser. A bearer wins over the cookie. An invalid bearer gets `401`, and the API never falls back to the cookie.

## Passwords

A password must be 10 to 128 characters and must not be on the bundled blocklist. Spaces at the start and end of a password do not count toward the 10, but the 128 limit applies to the whole string, spaces included. The blocklist ignores case and those surrounding spaces, so changing them does not get a blocked password through. The password is stored exactly as typed.

## Set up a new install

A new install is unclaimed until someone spends its one-use setup link. The installer prints the link. It works once and lasts one hour. The person who opens it chooses how the install works, and `POST /api/v1/auth/setup` records that choice. With `mode` set to `accounts`, which is the default, the link claims the administrator account:

```json
{ "token": "...", "mode": "accounts", "email": "ana@example.com", "password": "..." }
```

The answer is `204` with the session cookies. The claimant takes over the implicit local user, so the jobs and assets made before accounts existed belong to them. The claim grants no recent authentication.

To run without accounts, send `mode` set to `none` with the token only:

```json
{ "token": "...", "mode": "none" }
```

The answer is `204` with no cookies. The install then runs in `none` mode.

`POST /api/v1/auth/setup/status` takes the same token and answers `200` with `{"claimed": true}` or `{"claimed": false}`. It does not spend the link. An unknown, spent or expired link answers `403`.

A refusal never spends the link. A bad email or password answers `400`. An unknown, spent or expired link answers `403`, and so does every link on an install that has accounts. An install that runs without accounts answers `404`. A live link on an install that is already decided answers `409`. If `ROOT_KEYS` is empty, the answer is `503` with `root_keys_missing`. Run `scripts/ensure-env.sh` or the installer, recreate the API container, and then use the link again.

## Turn accounts on later

A `none` install can turn accounts on without a restart. An administrator sends an email and a password to `POST /api/v1/auth/enable`:

```json
{ "email": "ana@example.com", "password": "..." }
```

The answer is `204` with the session cookies, and the install runs in `accounts` mode from then on. The administrator is the implicit local user, and the account they name takes that row over, as the setup claim does. Every socket opened without a session closes with `4401`. The switch is one way.

The route answers `404` when the install already runs with accounts, and `403` to a caller without the admin role. A request with no `Origin`, or one that is not allowed, also answers `403`. A `409` means one of three things: `AUTH_MODE=none` is set in the environment, the recorded mode is already `accounts` (for example after `make auth-enable` ran elsewhere), or an account already holds an identity. For the first case, remove every top-level `AUTH_MODE` line from `deploy/compose/.env` and restart the API. A bad email or password answers `400`, and the install stays in `none`. An unclaimed install answers `503` with `setup_required`, and a database that is down answers `503` too.

## Join by invitation

Registration is invitation-only. An administrator creates an invitation with `POST /api/v1/invitations`, for one address and one role. The invitation works once and lasts 72 hours. The new user accepts it with:

```json
{ "token": "...", "password": "..." }
```

`POST /api/v1/auth/register` answers `204` with the session cookies. An address cannot register without an invitation, and a sign-in with a provider never creates an account.

> [!WARNING]
> The invitation link is shown once, when it is created, and the server keeps only its hash. Anyone who has the link can register the address it names. Send it only to that person. Revealing the invitation again with `POST /api/v1/invitations/{id}/reveal` makes a new link and retires the old one.

## Sign in with a password

```json
{ "email": "ana@example.com", "password": "...", "remember_me": true }
```

Send this body to `POST /api/v1/auth/login`. The answers are:

- `204`: the session and CSRF cookies are set.
- `200` with `{"totp_required": true}`: the password was right, and the account has a second factor. The server sets a short-lived challenge cookie instead of a session. Answer it at `POST /api/v1/auth/totp`.
- `401`: bad credentials, or an address that no account holds. Both get the same answer. An account that is disabled, waiting for deletion or being purged also gets `401`.
- `429`: more than ten attempts for one email address in ten minutes.

Login is slowed down as well. From the sixth attempt, the answer waits 0.5 seconds, then doubles up to 8 seconds. This happens whether or not the password was right. Attempts from one network address are delayed but never refused, because many people can share one address.

## Sign in with a provider

Google and GitHub are the supported providers.

- `GET /api/v1/auth/redirect/{provider}` starts the sign-in. It answers `404` unless the provider is listed in the install's settings and has credentials. It accepts an optional `next` parameter, a same-origin `/app` address to return to. Any other value is ignored.
- `GET /api/v1/auth/callback/{provider}` finishes the sign-in and sets the same session cookies as a password sign-in.

A provider sign-in works only for a provider identity that was already linked to an account. It never finds or creates an account from an email address.

To link a provider to a signed-in account, call `POST /api/v1/account/identities/{provider}`. This needs recent authentication. The provider's own access token is discarded as soon as the server reads the identity.

> [!PLANNED]
> Apple sign-in is deferred. The API has no Apple route.

## Sign out

`POST /api/v1/auth/logout` ends the session that the request used, and clears both cookies. It answers `204` even when the session had already ended, so a browser can always sign out.

## Second factor (TOTP)

A second factor is optional for every role. It gates sign-in only. It does not gate setup, invitation acceptance, promotion or recovery. It does not change what an account may do, and it does not change how long a session lasts.

To turn one on, a signed-in account does two calls:

1. `POST /api/v1/account/totp` needs recent authentication. It returns the secret, an `otpauth://` URI, the recovery codes and an `enrolment` value. Nothing is saved yet.
2. `POST /api/v1/account/totp/confirm` takes the `enrolment` value and a code from the authenticator app. Only this call saves the factor and its recovery codes.

An enrolment that is never confirmed expires after 30 minutes, and it does not change a factor that already works. If the account already has a factor, the confirm call also needs `current_code`, a code from the factor being replaced or one of its recovery codes. Ten `current_code` attempts end replacement of that factor, whether or not each attempt was right.

At sign-in, `POST /api/v1/auth/totp` takes a `code` from the authenticator app or one unused recovery code. A correct answer returns the session. The challenge lasts ten minutes and allows ten attempts. A challenge carries no authority of its own.

## Password reset and administrator recovery

`POST /api/v1/auth/reset` takes `{"email": "..."}` and always answers `202` with the same body. The answer does not show whether an account exists.

- A non-administrator is emailed a one-use link valid for 30 minutes.
- An administrator is never emailed. An administrator recovers at the machine, with `make auth-recover EMAIL=admin@example.com`. The command prints a one-use link valid for 10 minutes.

Open the link and send its token with a new password to `POST /api/v1/auth/reset/complete`:

```json
{ "token": "...", "password": "..." }
```

The answer is `204`. Completing either kind of link sets the password, revokes every session the account held, and returns the user to the login screen. It does not sign anyone in.

## Changing credentials

Changing how an account is proved needs recent authentication. Changing a password that already exists also needs the current password. Each such change does two things:

- It ends the account's other sessions.
- It keeps the browser that made the change signed in. The response sets new session and CSRF cookies, and the old session token stops working.

If two of these changes race with the same session token, the one that loses answers `409`, changes nothing and sets no cookie.

A client that loses the response to one of these changes is signed out and has to sign in again.

Changing the primary email address resets the verified status of that address. Saving the address the account already holds, in any letter case, changes nothing and answers `204`.
