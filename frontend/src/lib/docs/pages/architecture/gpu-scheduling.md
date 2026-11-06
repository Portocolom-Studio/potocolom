---
title: GPU scheduling
subtitle: How jobs and live sessions share GPU workers today.
tab: architecture
group: System
order: 3
source: docs/architecture.md "GPU scheduling", "One pool, real time first", "Model routing", "Upscale (post-generation)", "Low VRAM operation: the memory ladder", "When the pool is full", "Idle sessions" and their "Shipped status" notes; docs/connection-handling.md "Model store"; docs/decisions.md "GPU pool: shared between jobs and realtime, realtime first", "Queues: sorted sets with Lua pops, PostgreSQL as truth", "Model placement: hot set plus on-demand loading", "Realtime sessions per account are capped at two", "The realtime queue counts against the account cap", "Bundled manifests ship in the image; the models volume holds operator manifests"
---

GPU time is the scarce and expensive resource. Generation jobs and live drawing sessions both run on the same workers. This page describes what the API and the workers do today, then the parts that are planned.

## What runs today

### The job queue

Generation jobs wait in a heap inside the API process. The heap is `InProcessQueues` in `backend/app/jobs.py`. On start, the API rebuilds the heap from PostgreSQL.

- A worker can hold up to two queued jobs at once, set by `JOB_DISPATCH_DEPTH` in `backend/app/jobs.py`. The second job lets the worker encode and upload one result while it denoises the next.
- A job that does not name a `model_id` is routed by the API. The API picks the cheapest registered model whose `tier` (`draft`, `standard` or `premium`), capabilities and parameter schema match the request. Routing uses `backend/app/jobs.py` and `backend/app/manifests.py`. A worker's `hello` lists only the models it has installed, and a model installed while a connection is open is not routed to that connection until it reconnects.
- A worker serves the manifests bundled in its image plus the operator's own files in its `MODELS_DIR` volume, so an upgrade brings new and changed bundled models. A worker started with `BUNDLED_MANIFESTS=false` serves only its own files: a second worker that holds only the upscaler manifest then takes the upscale jobs, and diffusion jobs are not sent to it.
- A job is retried once, and only after the worker holding it dies.

### Realtime slots

A worker measures how fast it runs one frame for each model. It reports that time and the number of realtime slots it can run in its `hello` message. Admission uses a 500 ms per-frame p95 budget. Compatible frames from several sessions can share one GPU cycle.

Realtime needs the full-residency rung of the memory ladder, described next. Only that rung advertises the `realtime` capability.

### The memory ladder

At model load, the worker measures free VRAM and takes the highest rung that fits:

- **Full residency:** the whole pipeline stays on the GPU. This is the only rung that serves realtime.
- **Model offload:** whole components move to the GPU for their own stage. Jobs run, more slowly. Realtime does not.
- **Group offload:** layer groups stream through the GPU, and groups can spill to disk when system RAM is short. Jobs run, slowly. Realtime does not.

The rung is set per model. A worker can keep a small model fully resident while a larger model uses offload. The `MEMORY_MODE` worker setting pins a rung. It accepts `auto` (the default), `full`, `model_offload` or `group_offload`. A pinned rung never moves down. With `auto`, an out-of-memory error during a job moves the model down one rung, reloads it, and retries that job once.

### The admission queue for sessions

A session request with no free slot waits in the admission queue. The session is in state `queued`.

- The queue order is the time each session first joined. A session that is sent back keeps its place.
- A freed slot, a newly registered worker, or the 30 second session sweep admits sessions. Admission is first fit: a session whose model has no room does not hold up a session whose model has room.
- The browser sees its position in the queue. It does not see an estimated wait, because the API keeps no session-duration data to estimate from.
- There is no queue timeout. Closing the socket removes the session from the queue.

An account can hold two realtime sessions. A third request is refused with close code 4003. Queued sessions count against that limit. See [Realtime loop](/docs/architecture/realtime-loop) for the close codes and the session states.

### Idle release

A live session with no canvas input for about 60 seconds gives up its slot. Usage metering stops when the slot is released. The canvas stays in the browser. The next stroke places the session on a worker again. When no slot is free, the session waits in the queue.

### Failed attempts

A worker that cannot make the model resident answers `session_refused`. That fails one attempt, not the session. The API tries another worker, or queues the session when no worker has room.

> [!NOTE]
> Jobs and realtime sessions share the same workers. Preemption is not built, so a session request does not stop a running job to take its slot.

## Planned scheduling

The parts below are designed but not built. The shipped status notes say so directly: preemption and checkpoints between steps are not implemented, there is no scheduler that places models across machines, and the Redis-backed queue remains designed.

> [!PLANNED]
> Preemption and checkpoints are planned. A session request would take a slot from queued work. The worker would finish or checkpoint the current job between denoising steps, free the slot, and resume the job when the session ends. Active sessions would never be preempted. The design is "GPU pool: shared between jobs and realtime, realtime first" in [docs/decisions.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/decisions.md). Issue [#19](https://github.com/Portocolom-Studio/potocolom/issues/19) and issue [#20](https://github.com/Portocolom-Studio/potocolom/issues/20) govern the work.

> [!PLANNED]
> A hot set of models is planned. A scheduler would keep the realtime model and the most used generation models loaded on fixed workers. Other models would load on demand, and the user would see a loading state once. Today each worker loads models on its own, using the memory ladder. No scheduler places models across machines. The design is "Model placement: hot set plus on-demand loading" in [docs/decisions.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/decisions.md). Issue [#20](https://github.com/Portocolom-Studio/potocolom/issues/20), "Multi-Worker Scheduling", governs the work.

> [!PLANNED]
> A Redis-backed queue is planned. It would use sorted sets with Lua pops, with PostgreSQL as the record of truth, and it would support tiered priority. The design is "Queues: sorted sets with Lua pops, PostgreSQL as truth" in [docs/decisions.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/decisions.md). A leader elected among the API replicas would run the scheduler loop, per "Scheduler: leader elected inside the API replicas". Today the backend has no Redis queue. Estimated waits need session-duration data that the API does not keep yet, and priority tiers are not built.
