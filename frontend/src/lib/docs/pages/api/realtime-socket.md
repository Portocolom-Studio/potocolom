---
title: Realtime socket
subtitle: The WebSocket that carries canvas frames and generated frames.
tab: api
group: Endpoints
order: 4
source: docs/api.md "WS /api/v1/realtime", "Authentication (shipped, kept here for the exact shapes)"; backend/app/auth_mode.py; docs/connection-handling.md "Endpoints and transport", "Message catalogue", "Timeouts and intervals", "Latest input wins", "Close codes"
---

The studio opens this socket while a person draws. The browser sends canvas frames up. The server sends generated frames back. Control messages are JSON text. Images are binary frames. The full wire rules are in [connection-handling.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/connection-handling.md).

## Endpoint

`WS /api/v1/realtime`

- **Sign-in:** with `AUTH_MODE=accounts`, the session cookie authenticates the upgrade. With no cookie, the handshake fails with HTTP 403. A cookie that matches no session closes the socket with `4401`. An account that is not `active`, or that has the `viewer` role, closes with `4403`. With `AUTH_MODE=none`, the socket belongs to the implicit local user. While the installation is unclaimed, the upgrade is refused and no socket opens.
- **Revocation:** logout, a role change, a suspension, a disable or a deletion closes the live socket with `4401`. Turning accounts on with `POST /api/v1/auth/enable` closes every socket that was opened without a session, with `4401` as well.
- **Origin:** if the handshake carries an `Origin` header that is not the install's public address or one of its `ALLOWED_ORIGINS`, the handshake fails with HTTP 403. A handshake with no `Origin` is accepted.
- **Limit:** one account may hold two realtime sockets at once. A third is refused with `4003`.

> [!NOTE]
> The Origin check protects the install from other web pages that a person visits. It is not authentication. The cookie check is what identifies the user.

## Messages

Text messages are JSON objects. Each has a `type` field. Binary messages are image frames. A frame starts with a header, and the header has two forms. The browser chooses the form in its `open` message.

```text
byte 0       frame kind: 0x01 canvas (browser to worker), 0x02 generated (worker to browser)
bytes 1-16   session ID, UUID, big endian
bytes 17-20  input revision, unsigned 32-bit, big endian (only with frame_header 2)
bytes 21-    image bytes (WebP or PNG)
```

With `frame_header` 1, which is the default, the header has no revision field. It is 17 bytes long. With `frame_header` 2, the header is 21 bytes long. Use 2 when you want each generated frame matched to the canvas that made it.

## Message flow

1. Open the socket. The server checks the cookie and the Origin before it accepts the socket.
2. Send `open` first. It names the model, and it can carry `params` and `frame_header`:

   ```json
   {
   	"type": "open",
   	"model_id": "sd-sim",
   	"params": { "prompt": "a red house" },
   	"frame_header": 2
   }
   ```

   A socket that sends nothing for 10 seconds closes with `4000`. Any `frame_header` value other than 1 or 2 gets an `error` with code `4000`.

3. Wait for `ready`. It carries the `session_id` and a `limits` object. If no worker has room, the server sends `queued` with a 1-based `position` instead. It sends `ready` once the session gets a worker. A session that was already running, and then lost its worker, gets `resumed` instead of `ready`.
4. Send canvas frames as binary. The first canvas frame of a session has revision 1, and each accepted frame raises the revision. A frame whose revision the server has already accepted is dropped with no error.
5. Read generated frames as binary frames with kind `0x02`. With `frame_header` 2, each one carries the revision of the canvas it was made from.
6. Expect fewer frames under load. The worker keeps one pending canvas frame per session. A newer frame replaces an older one that has not been processed yet. The user sees fresher frames, not a growing delay.
7. Change settings with `update_params`, which takes only the keys that change:

   ```json
   { "type": "update_params", "params": { "prompt": "a blue house" } }
   ```

   The server answers `params_updated` with the merged parameters. The browser should send the current canvas again when that message arrives, because a settings change may not change the pixels. A change to `seed` is refused with an `error`, and the session keeps running.

8. If a worker is lost, the server sends `interrupted`, moves the session to another worker, and then sends `resumed`. Send the current canvas again when `resumed` arrives. The revision counter does not restart.
9. After about 60 seconds with no canvas input, the server releases the session's GPU slot. The browser is not told. The next canvas frame gets a worker again, and the canvas stays where it is.
10. The server sends `keepalive` about every 30 seconds. It does nothing. The browser should ignore it.
11. End the session with `{"type": "close"}`, or close the socket with code 1000. A dropped connection also ends the session.

## Messages from the server

| Type             | Fields                 | Meaning                                                                                                          |
| ---------------- | ---------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `ready`          | `session_id`, `limits` | Frames may flow. `limits` gives the 1 MiB frame cap, the formats (`webp`, `png`) and the 512 by 512 canvas size. |
| `queued`         | `position`             | The session is waiting for a worker. Sent when the position changes and about every 30 seconds.                  |
| `resumed`        | none                   | A worker is ready again. Send the current canvas.                                                                |
| `interrupted`    | none                   | The worker was lost. A new worker is on its way.                                                                 |
| `params_updated` | `params`               | An update was accepted. The merged parameters are included.                                                      |
| `keepalive`      | none                   | No-op.                                                                                                           |
| `error`          | `code`, `message`      | A failure. Terminal errors close the socket after this message.                                                  |

Two errors leave the socket open. Code `4005` means the canvas frame was over the 1 MiB cap. It is sent at most once per second per session. A refused `update_params`, such as a `seed` change, is the other.

## Close codes

| Code | Meaning                                                                                                        | Sent to     |
| ---- | -------------------------------------------------------------------------------------------------------------- | ----------- |
| 1000 | Normal close.                                                                                                  | Either side |
| 4000 | Protocol violation: the first message was not `open`, the JSON was malformed, or the `frame_header` was wrong. | Either side |
| 4002 | Unsupported worker protocol version.                                                                           | Worker only |
| 4003 | The account already holds its two realtime sockets.                                                            | Browser     |
| 4004 | Unknown model.                                                                                                 | Browser     |
| 4005 | Canvas frame over the 1 MiB cap. Sent as an `error` message, and the socket stays open.                        | Browser     |
| 4401 | Authentication required, or the session is no longer valid.                                                    | Browser     |
| 4403 | Signed in, but not allowed to open a realtime session.                                                         | Browser     |

## Protocol version

The browser socket has no version field of its own. Its frame layout is set by `frame_header`, as described above. The worker socket has a separate protocol version, which the connection-handling document describes.

> [!PLANNED]
> A browser cannot reconnect to a session yet. A session lives inside the API process that owns it, so a restart or a move to another replica ends it. Drain messages and more close codes are planned under issue #19.
