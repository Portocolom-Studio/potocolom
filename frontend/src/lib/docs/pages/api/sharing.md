---
title: Sharing
subtitle: How an image is shared with another person, and how to stop sharing.
tab: api
group: Rules
order: 6
source: docs/api.md "Sharing"; backend/app/shares.py; backend/app/files.py
---

A share is a link that shows one picture to whoever holds it. The person who receives the link does not need an account.

The link has a token after the `#` in its address. A browser never sends the part after `#` to a server, so the studio takes the token out and posts it in a request body. No route takes a token in a path or a query string. A token in a URL would appear in access logs, proxy traces and `Referer` headers.

## Endpoints

```text
POST   /api/v1/shares          {"asset_id": "...", "days": 1 | 7 | 30}
                               201 {"id": "...", "url": "https://.../shared#<token>"}
DELETE /api/v1/shares/{id}     204, the link stops resolving
POST   /api/v1/shared          {"token": "..."}
                               200 {"asset": {"id", "width", "height", "mime"},
                                    "prompt": "...", "model": "...", "url": "..."}
```

## Create a share

- The caller needs the `user` role. A `viewer` gets `403`.
- The asset must belong to the caller. Otherwise the answer is `404`.
- `days` must be 1, 7 or 30. The share expires after that many days.
- The answer has the share `id`, which you need to revoke it, and the `url` to send to the other person.

## Rules

- **One link per asset:** a share is one active link for one asset. Sharing the same asset again makes a new link and revokes the old one in the same step, so an older link cannot stay live. If two requests race to share one asset, the one that loses answers `409`.
- **Reuse:** the token works on every visit until the share is revoked or its days run out.
- **Storage:** the server keeps only the SHA-256 hash of the token. It cannot show the link again. If the link is lost, create a new share.
- **What the viewer gets:** the picture's ID, width, height and type, plus the prompt and the model. The answer names no account, no email address and no storage key.
- **Picture address:** the `url` in the answer points at `GET /api/v1/shared-picture`, which returns the picture bytes. That address lasts 60 seconds. The server makes a fresh one on every resolve.
- **Refusals:** a token that was never issued, a revoked token and an expired token all get the same `404`. A share also answers `404` when its asset has expired, or when the owner's account is not active.
- **Resolving:** `POST /api/v1/shared` needs no account and no cookie. The token is the only credential.
- **Revoking:** the owner can revoke a share even without the `user` role. An account that was demoted while a link was live can still revoke it.

## Stop sharing

Call `DELETE /api/v1/shares/{id}` with the share's `id`. The answer is `204`, and the link answers `404` from then on. Revoking also stops the picture addresses that were already handed out.

A suspended account's share links are paused, not revoked. They answer `404` while the account is suspended, and they work again when the account is restored.

> [!WARNING]
> Anyone who holds the link can see the picture, and the server cannot take back a copy that someone has already saved. Revoking stops new views. It does not reach copies that people already have.
