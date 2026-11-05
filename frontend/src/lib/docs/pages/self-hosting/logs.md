---
title: Logs
subtitle: Read the container logs and find the useful lines.
tab: self-hosting
group: Run it
order: 12
source: docs/self-hosting.md "Logs"
---

Each service writes its output to Docker's log store. Run these commands from the install directory. For a release install, that is `~/potocolom`.

```bash
docker compose -f deploy/compose/compose.yml logs
docker compose -f deploy/compose/compose.yml logs -f api worker
```

The first command prints the logs of every service. The second follows the `api` and `worker` logs live. On an AMD host, the worker service is `worker-rocm`, so use `logs -f api worker-rocm`. With `make`, `make compose-logs` runs the same commands and detects the profile.

The installer also prints a logs command when the API does not become ready within its 120 second limit.

## Log storage

Every service in `deploy/compose/compose.yml` uses Docker's `json-file` logging driver. Each container keeps five files of 10 MB each, so about 50 MB per container. The files survive container restarts. Docker removes them when the container is removed.

To change the limit, edit `max-size` or `max-file` in the `x-logging` block of `deploy/compose/compose.yml`, then recreate the services.

## What each service logs

- `api`: access log lines, which carry no query strings, and process detail. If a file that a job left behind cannot be deleted, a line names the key and says `still cannot delete`.
- `worker` (or `worker-rocm` on AMD): process detail, such as startup, protocol, driver and traceback messages. On the NVIDIA worker, `logs -f worker` shows the download progress for a model's weights on its first job.
- `postgres`: the source does not describe the lines this service writes.
- `worker-sim`: the simulated worker used by the smoke tests in a source checkout. The source does not describe its lines.

The source says that container logs keep the process detail, such as startup, protocol, driver and traceback messages.

## A job that seems stuck

A job waits in the queue until its model's weights have finished downloading. While the download runs, the worker log shows its progress. A gated model whose download Hugging Face refuses fails its job instead. See [Gated models](/docs/self-hosting/gated-models).

## Job failures are not in the logs

The reason that a job failed is stored in PostgreSQL, in `jobs.failure_reason`. Phase timings, GPU sample history and usage events are also stored there. To find out why a job failed, check the job record in the database before you read the container logs. See [What persists where](/docs/self-hosting/what-persists-where).
