---
title: Accounts and the root key
subtitle: Turn on accounts, keep access when locked out, and rotate the root key.
tab: self-hosting
group: Run it
order: 7
source: docs/self-hosting.md "Accounts", "When nobody can get in", "Changing the root key"; docs/api.md "Authentication"
---

Accounts give each person their own sign-in. This page covers how an install gets accounts, the commands that get you back in when nobody can sign in, and how to rotate the root key. The root key is a secret that the install uses to encrypt stored secrets, such as second factors. For the first choice that a new install makes, see [First run](/docs/self-hosting/first-run).

## Single-operator mode and accounts mode

A new install is unclaimed until someone chooses accounts or no accounts. The choice is recorded in PostgreSQL. The `AUTH_MODE` setting in `deploy/compose/.env` is an optional override, and it normally stays empty.

In `none` mode, which is the single-operator choice, every request resolves to one local user, and that user holds the `admin` role. Anyone who can reach the API can do anything the install can do. Keep this mode on a trusted network.

`AUTH_MODE=accounts` gives each person their own account. There are three ways to get there:

- Choose accounts on the setup link, on a new install. See [First run](/docs/self-hosting/first-run).
- Run `make auth-enable` on the machine. This is the offline switch, described below.
- As an administrator of an install that runs without accounts, call `POST /api/v1/auth/enable` with an email and a password. Accounts turn on with no restart. In the studio, the Account view of an install without accounts is a Turn on accounts form that calls this route.

The runtime route works like this. The account that you name takes over the implicit local user, as the setup claim does. Every browser canvas that was open without a session is closed, and the next request needs a session. The route refuses while `AUTH_MODE=none` is set in `deploy/compose/.env`. To clear that, remove every top-level `AUTH_MODE` line from `deploy/compose/.env`, including duplicate assignments with any value, restart the API, and then turn accounts on. A request already running as the administrator when the switch happens finishes under that authority, which is no more than the administrator already had.

`make auth-enable` is the offline switch. It works on an install that is unclaimed or runs without accounts. The command:

- generates the root key ring (`ROOT_KEYS`) in `deploy/compose/.env` if it is missing, and keeps any root keys that already exist,
- asks where browsers reach this install,
- records the switch in PostgreSQL,
- prints a one-use setup call that claims the administrator account.

If the API is still running, it keeps serving `none` until it restarts, and the runtime route refuses to switch from it. Restart the API, which is what the command tells you to do.

The claimant takes over the implicit local user. Every job and asset made before accounts existed belongs to that administrator. Nothing is copied and nothing is stranded.

The setup link lasts one hour and works once. Running `make auth-enable` or `make setup-link` again replaces it. Only a hash of the link is stored, so a lost link means you mint another one.

If `make auth-enable` and an upgrade run at the same time, the second command stops before it changes the configuration. Let the first command finish, then run the second one again.

> [!WARNING]
> `ROOT_KEYS` is the only way to read encrypted account data, such as second factors. Keep a copy of the value with your backups. Without it, a restored install cannot read that data.

### Signing in and inviting people

After you claim the administrator account, open `/login` in the studio. Sign-in uses an address and a password. Passwords must be 10 to 128 characters and must not appear in the bundled blocklist. An optional second factor adds a code step, and Google or GitHub sign-in works when you configure them. A session is a cookie that the browser cannot read.

Adding people does not need a mail service. An administrator creates an invitation for an address and a role. The API returns the invitation link once. Copy it and hand it over however you like. The link:

- is bound to that address,
- works once,
- expires after 72 hours.

People accept an invitation at `/join#{token}`. Asking to see the link again makes a new link and retires the old one. A link that never reached its recipient cannot be used by whoever saw it.

Promoting someone to administrator asks for evidence:

- The administrator who promotes must have signed in within the last 30 minutes.
- The promoted account must hold a verified address. If it does not, the promotion needs an explicit attestation, and the record names the administrator who vouched for it.
- Nobody can change their own role.
- The last administrator cannot be demoted.

### Accounts mode is one way

An install that has turned on accounts refuses to start in `none` mode. Falling back would answer every request as an administrator.

To undo accounts, use the offline reset:

```bash
make auth-collapse CONFIRM="destroy the accounts on this installation"
```

> [!WARNING]
> This destroys every account, session, second factor and invitation on the install. Generations and assets move to the single local user, so the images are kept.

After the reset, remove `AUTH_MODE=accounts` from `deploy/compose/.env` (or set `AUTH_MODE=none`), and restart the API. An accounts override refuses to start an install that runs without accounts.

## Export and delete an account

An account can take everything the install holds about it with `GET /api/v1/account/export`. The export is one JSON file with a link to each image. The image files themselves are not in it.

An account can also ask to be deleted with `DELETE /api/v1/account`. Deleting stops the account at once and destroys nothing for 30 days. An administrator can restore it inside that window with `POST /api/v1/users/{id}/restore`. After that, a sweep purges it.

## When nobody can get in

Every way back is a command run at the machine. There is no web route for recovery, because a route that can do this is a route that someone could steal a session for.

```bash
make auth-recover EMAIL=admin@example.com   # a one-use ten-minute link for one administrator
make auth-reclaim EMAIL=admin@example.com   # make that account an active administrator again
make auth-reclaim CLAIM=1                   # a fresh setup link; whoever spends it is an admin
make auth-configure                         # what mail and OAuth would do if the API started now
make auth-clear-factor EMAIL=someone@example.com   # take a second factor off an account
```

Use each command for one case:

- `make auth-recover EMAIL=<address>` is for an administrator who forgot a password. The link works once and lasts ten minutes.
- `make auth-reclaim EMAIL=<address>` is for an install where every administrator was suspended, deleted or demoted at once. It makes that account an active administrator again.
- `make auth-reclaim CLAIM=1` mints a new setup link and retires any setup link that is still open. The person who held the old link is not necessarily the person at the machine, so the old link stops working.
- `make auth-configure` prints what mail and OAuth would do if the API started now. It changes nothing.
- `make auth-clear-factor EMAIL=<address>` is for an account whose authenticator and every recovery code are gone. It removes the second factor and its codes, ends every session that the account had open, and records an audit row. The account keeps its password and role, and can enrol a new factor. An open realtime canvas on one of those sessions closes within half a minute, when the API next checks which of its sockets are still signed in. If the person still has the authenticator or a recovery code, they can remove the factor themselves with `DELETE /api/v1/account/totp`.

`make auth-reclaim CLAIM=1` and `make auth-enable` both refuse once the install has been claimed. A setup link can only adopt the implicit local user while nobody holds an identity.

## Changing the root key

`ROOT_KEYS` holds every key version, newest first. A running install reads all of them, so an older key can still open what it sealed. Nothing moves to the new key on its own.

Each entry has the form `version:key`, and entries are separated by commas. The key is base64 of 32 random bytes. The installer writes the first entry as version `1` when it generates the root key ring, and `make auth-enable` does the same when the ring is missing. A rotation adds a new entry with the next version number at the front:

```text
ROOT_KEYS=2:<new-base64-key>,1:<old-base64-key>
```

The source gives no separate command to create the new key. Generate it the same way that `scripts/auth-enable.sh` does, with `openssl rand -base64 32`.

Then rotate in this order:

```bash
# 1. Put a new key at the front of ROOT_KEYS in deploy/compose/.env, keep the old.
make auth-rotate-keys           # re-encrypt everything under the newest key
make auth-rotate-keys CHECK=1   # nothing left under an older one?
# 2. Only then remove the old key from ROOT_KEYS and restart.
```

The rotation refuses outright if any stored secret is sealed under a version that is no longer in `ROOT_KEYS`. Rewriting a value that the install cannot read would destroy the secret behind it. Remove an old key only after the check shows that nothing is left under it.
