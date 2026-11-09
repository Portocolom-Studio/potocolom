# Coding standards

Rules a reviewer applies to a diff. They are judgement calls that no linter can
make; anything a tool can check belongs in `make lint`, `make verify` or CI
instead. Each rule names the failure it prevents.

## Tests

### Wait for state across channels

A test that changes state through one channel reads it through another only
after it has waited for that state to arrive. Channels here are a WebSocket, an
HTTP request, a timer, a page navigation and the DOM.

- Backend: after `ws.send_json(...)`, poll the HTTP view until it shows the
  change (with a deadline) before the next request depends on it.
- Browser: after `page.goto(...)`, `waitForSelector` the element, visible, before
  `click` or `$eval` on it.
- A fixed `sleep` or `setTimeout` is not a wait: it passes on a fast machine and
  fails on a loaded CI runner.

Why: such a race passes locally most of the time and fails CI on a busy runner.
Two shipped in the first-run onboarding PRs and survived several reviews:
`backend/tests/test_worker_models.py` (`_wait_until_manageable`) and
`frontend/scripts/test-model-management.mjs` (the wait before the admin entry
click).

## Scripts

### Own a path before you delete it

A script that writes into an operator's directory creates its temporary files
and directories with `mktemp` beside the target, records or marks what it
created, and removes only those paths. A name pattern never proves ownership:
the operator may have a file, a directory or a symlink with that name.

Why: the upgrade swap in PR #832 first used fixed temp names
(`.<name>.potocolom-new`) and cleanup by name. Each of five review rounds found
one more operator file the cleanup could delete: a symlinked parent, a folder
of the same name, a planted symlink, an exact-name file. The cure was the rule
above, in `scripts/install.sh` (`swap_in_bundle`, `take_backup`).
