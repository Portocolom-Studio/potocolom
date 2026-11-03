---
title: Realtime loop
subtitle: How a stroke becomes a frame, and how the frame comes back.
tab: architecture
group: System
order: 2
source: docs/connection-handling.md "Endpoints and transport", "Message catalogue", "Session states", "Reconnection and resume", "Latest input wins", "Delivery semantics"; docs/architecture.md "Real time drawing session", "Frame relay across API replicas", "Idle sessions"
---

A drawing session is a loop. The browser sends the canvas as image frames. A worker turns each frame into a new generated image and sends it back. The target rate is 2 to 4 generated frames per second at 512 px.

Today, one API process holds both sockets and relays frames between them. Frames stay in memory. The steps below describe the shipped path.

## The loop in steps

1. **Open:** the browser connects to `WS /api/v1/realtime` and sends an `open` message with `model_id`, optional `params` and an optional `frame_header`. In `accounts` mode, the socket checks the session cookie on the upgrade.
2. **Assign:** the API finds a free slot on a worker and sends it `open_session` with a `control_generation` number. The worker loads the model and answers `session_ready`. The API then sends `ready` to the browser. `ready` carries the frame limits: `max_frame_bytes`, `formats`, `width` and `height`.
3. **Capture:** the browser encodes the canvas as WebP and sends a binary canvas frame. It sends a new frame every 250 or 500 ms, based on how long the last encode and send took.
4. **Forward:** the API checks the size of the frame. A frame over the 1 MiB payload cap is refused with a non-terminal `error` that has code 4005. The socket stays open. A frame inside the cap goes to the worker that holds the session.
5. **Infer:** the worker keeps one pending frame per session. A newer frame replaces an older frame that has not run yet. The worker runs a few-step image-to-image pass and sends a generated frame. That frame carries the input revision of the canvas it came from.
6. **Return:** the API sends the generated frame to the browser, which draws it. Frames are sent at most once. A dropped frame is not sent again, because the next canvas frame replaces it.
7. **Idle:** after about 60 seconds with no canvas input, the API releases the worker slot and stops metering. The canvas stays in the browser. The next stroke places the session on a worker again, through the admission queue when the pool is full.
8. **Close:** the browser sends `close` or closes its socket. The API sends `close_session` to the worker and frees the slot.

## Frame format

Control messages are JSON text. Frames are binary. A frame never carries JSON, and a control message never carries image bytes. The 21-byte header of a frame is:

```text
byte  0      frame kind: 0x01 canvas (browser to worker), 0x02 generated (worker to browser)
bytes 1-16   session id, UUID big endian
bytes 17-20  input revision, unsigned 32 bit big endian
bytes 21-    image payload (WebP)
```

The input revision belongs to the session. The first canvas frame of a session is revision 1, and each accepted frame raises it. It does not restart when a session moves to another worker or resumes after idle. A generated frame carries the revision of its input, so the browser can match an output to the input it came from.

A browser that does not send `frame_header: 2` in `open` uses a 17-byte header with no revision field. The API adds and removes the revision for that browser. The full message list is in [docs/connection-handling.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/connection-handling.md#message-catalogue).

## How the relay works today

Shipped status (2026-09-29): the 21-byte header above is the current wire. The worker socket speaks protocol 5.

### One process holds both sockets

The API keeps the browser sockets and the worker sessions in process memory. A relay call passes a frame from one socket to the other inside the same process. Nothing is written to Redis or to a shared bus. Restarting the API process ends every live session.

### One writer per browser

Each browser socket has one writer task, `browser_writer` in `backend/app/realtime.py`. Nothing else writes to that socket. Other code posts to the session's mailbox and does not wait for the send.

- Control messages queue in order.
- A generated frame replaces any older frame that has not been sent yet.
- A stalled browser cannot hold up the other browsers that share its worker.

On a development machine, the `slow-consumer` scenario of `scripts/stress.py` used synthetic workers and 1 MB frames. It stalled one browser for 8 seconds. The p95 latency of its neighbour went from 6.7 seconds before the change to 125 ms after it. That is a local measurement, not a capacity result.

### Generation numbers fence old attempts

Every time the API asks a worker to start a session, it sends a `control_generation` number. The number goes up by one for each attempt. Each lifecycle message carries it.

- A `session_ready` for an older attempt does not complete a newer one.
- A `close_session` for an older attempt does not stop a runner that a newer attempt started on the same worker.
- A worker refuses an `open_session` with a number lower than the highest it has seen for that session. It treats an equal number as a repeat of the same request.

Frames are fenced by the runner they reach, not by a number in each frame. A superseded attempt can therefore deliver at most the one frame it had already produced.

The design record is "The realtime session has states, a fencing generation, and one durable accounting owner" in [docs/decisions.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/decisions.md).

### Session states

A session is in one of six states: `queued`, `assigning`, `live`, `idle`, `ending` or `ended`. The API moves a session between them in one place. `ended` is final.

- A worker that cannot load the model sends `session_refused`. That fails one attempt, not the session. The API tries another worker or queues the session.
- A session with no free slot is `queued`. The browser is told its position. It is admitted when a slot frees, a worker registers, or the 30 second session sweep runs.
- Closing the socket leaves the queue.

### Recovery

- **Worker lost:** the API tells the browser `interrupted`, picks another worker, and sends it `open_session` with the next generation number. When that worker answers `session_ready`, the browser gets `resumed` and re-sends its current canvas. The input revision carries on counting.
- **No replacement slot:** the session is queued. The browser gets `queued`, and then `resumed` when it is admitted.
- **Browser lost:** the API sends `close_session` to the worker and frees the slot. Nothing is kept on the server, because the canvas lives in the browser. A browser that comes back opens a new session.

Recovery works only inside the process that owns the sockets. It cannot move a session to another API process, and a session does not survive the loss of its API process.

### Timers

| What                 | Value                           | Where it is set                                                                                                                       |
| -------------------- | ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Worker heartbeat     | every 30 s                      | [connection-handling.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/connection-handling.md#timeouts-and-intervals) |
| Worker declared dead | 90 s without a heartbeat        | `WORKER_DEAD_SECONDS` in `backend/app/realtime.py`                                                                                    |
| First message wait   | 10 s                            | `SESSION_READY_TIMEOUT` in `backend/app/realtime.py`                                                                                  |
| Session sweep        | every 30 s                      | `SESSION_SWEEP_SECONDS` in `backend/app/realtime.py`                                                                                  |
| Idle slot release    | about 60 s without canvas input | `IDLE_RELEASE_SECONDS` in `backend/app/realtime.py`                                                                                   |

The sweep also sends a `keepalive` to each open browser socket. The browser ignores it. It keeps the connection open through proxies.

### Limits per account

An account can hold two realtime sessions at once. A third session is refused with close code 4003. Queued sessions count against this limit. The close codes are listed in [docs/connection-handling.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/connection-handling.md#close-codes).

## Planned relay design

The parts below are designed but not built. The current relay works only inside one API process.

> [!PLANNED]
> A shared frame bus is planned. It would carry frames between API replicas, so the browser socket and the worker socket could be held by different processes. The design is "Realtime frame routing: Redis pub/sub between API replicas" and "Realtime and queue Redis seam: optional, behaviorally equivalent" in [docs/decisions.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/decisions.md). Today the backend has no FrameBus, no Redis package, and no setting named `REDIS_URL`.

> [!PLANNED]
> A socket gateway is planned to hold the browser and worker sockets outside the Python API. The design is "Realtime relay: Go gateway required for the 1000-active-session target" in [docs/decisions.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/decisions.md). The gateway would relay frames in memory when both sockets are local and over the frame bus otherwise. Issue [#48](https://github.com/Portocolom-Studio/potocolom/issues/48) owns the 1,000-session load test. No capacity result for that target is accepted yet.

> [!PLANNED]
> Session checkpoints and a durable accounting outbox are designed but not shipped. They would record usage as the session runs, so that a worker that dies abruptly leaves its usage settled up to the last checkpoint. The design is in the same decision as the fencing generation, "The realtime session has states, a fencing generation, and one durable accounting owner" in [docs/decisions.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/decisions.md).

> [!PLANNED]
> Browser reconnect is design only. A browser that loses its socket opens a new session today. Cross-owner recovery and resume priority are governed by issue [#19](https://github.com/Portocolom-Studio/potocolom/issues/19), "Real-Time Generation Protocol", and issue [#20](https://github.com/Portocolom-Studio/potocolom/issues/20), "Multi-Worker Scheduling".

For the scheduling side of these sessions, see [GPU scheduling](/docs/architecture/gpu-scheduling).
