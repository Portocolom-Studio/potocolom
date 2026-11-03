---
title: Account states
subtitle: What each account state allows and how it changes.
tab: api
group: Rules
order: 5
source: docs/api.md "Account states", "Account, export and deletion"; backend/app/states.py, backend/app/shares.py
---

Every account has one state. The state decides whether the account can sign in, change anything, hold a GPU slot and keep its share links live. The states are `active`, `suspended`, `disabled`, `deletion_pending` and `purging`. `cancelled` is a job state. It is never an account state.

## What each state allows

| State              | Sign in        | Read its own work | Change anything              | Hold a GPU slot | Share links                                |
| ------------------ | -------------- | ----------------- | ---------------------------- | --------------- | ------------------------------------------ |
| `active`           | Yes            | Yes               | Yes, within its role         | Yes             | Resolve                                    |
| `suspended`        | Yes, read only | Yes               | No. Any write answers `403`. | No              | Answer `404` until the account is restored |
| `disabled`         | No             | No                | No                           | No              | Answer `404`                               |
| `deletion_pending` | No             | No                | No                           | No              | Answer `404`                               |
| `purging`          | No             | No                | No                           | No              | Answer `404`                               |

Share links resolve only for an active account, so the last three rows answer `404`. A suspended account's links are paused, not revoked. Restoring the account brings them back. For the link rules, see the [sharing page](/docs/api/sharing).

A `disabled`, `deletion_pending` or `purging` account cannot sign in through any door. That includes a password, a provider and a link that was already in a mailbox. Its existing sessions stop resolving.

## How an account changes state

An administrator changes state with this route:

```text
POST /api/v1/users/{id}/state   {"state": "active" | "suspended" | "disabled" | "deletion_pending"}
                                204; admin only; idempotent
                                409 when that transition does not exist
                                403 for an administrator's own account
```

These are the moves the route allows:

| From               | Moves to                                                 |
| ------------------ | -------------------------------------------------------- |
| `active`           | `suspended`, `disabled`, `deletion_pending`              |
| `suspended`        | `active`, `disabled`, `deletion_pending`                 |
| `disabled`         | `active`, `deletion_pending`                             |
| `deletion_pending` | Back to the state it had, through the restore route only |
| `purging`          | Nothing. Only the deletion sweep moves an account here.  |

Any other move answers `409`.

## Rules for state changes

- The check and the write happen in one transaction. Setting an account to the state it already has answers `204` and changes nothing, so a retry after a timeout is not a second event.
- The request cannot set `purging`. The deletion sweep sets it, and only after 30 days.
- When an account leaves `active`, the server revokes every session it holds, closes its realtime sockets, spends its outstanding reset and recovery links, and cancels its queued and running jobs.
- An administrator cannot change their own state, and the last active administrator cannot be suspended. An install with no administrator can only be recovered offline.
- A suspended account keeps its own work and account settings, and can read them. It cannot change anything, and it cannot hold a GPU slot.

## Deleting an account

A person deletes their own account with `DELETE /api/v1/account`. That call answers `204` and moves the account to `deletion_pending` at once. The server revokes every session, spends every outstanding link, closes the realtime sockets and cancels the queued and running jobs. Nothing is destroyed yet.

The account remembers the state it was in, one level deep. `POST /api/v1/users/{id}/restore` puts it back there and answers `204`. An account that was suspended when it asked for deletion comes back suspended. A restore answers `409` for an account that was never waiting for deletion, and for one that is past its 30-day window.

> [!WARNING]
> After 30 days a sweep purges the account. It removes the stored objects first, then the assets, then the jobs, and then the user row. A purged account cannot be restored. Audit rows keep plain IDs with no foreign key, so what an administrator did survives the account.

The last administrator may delete their own account. An install with no administrator can be recovered only offline.
