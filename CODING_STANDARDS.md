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

### Quarantine skipped and flaky tests

A flaky test is fixed, not retried. A skip or an expected failure that always
applies is tracked by an issue.

- Retry is not allowed: no `pytest-rerunfailures` or `flaky` plugin, no
  `@pytest.mark.flaky`, no `--reruns`, and no node `retry` or `retries` option.
- An unconditional skip or expected failure has a comment on the line above:
  `# quarantine: #<issue> <why>` in Python, or `// quarantine: #<issue> <why>`
  in TypeScript and JavaScript. Unconditional means `@pytest.mark.skip`, an
  `xfail` with no condition, a bare `pytest.skip()` as the first statement of a
  test, `it.skip`, `test.skip`, `describe.skip` and `skip: true`.
- A conditional skip needs no comment: `pytest.mark.skipif`,
  `pytest.importorskip`, or a `pytest.skip()` inside an `if` that tests the
  environment.
- `.only` is never allowed, even with a quarantine comment.
- `scripts/check-test-quarantine.py` enforces the rule. `make lint` and the
  commit-hygiene workflow run it.

Why: a retry turns a race into a green check, and the race stays in the code.
On 2026-11-12 `frontend/scripts/test-docs-browser.mjs` failed twice under
runner load; a retry would have hidden the cause. A quarantine comment keeps
every known gap visible in the issue tracker.

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

### Cleanup survives cancellation

A job or script that creates state outside its own process (containers,
networks, volumes, temp directories, processes) names that state so a later
step can find it, and removes it in a step that also runs when the job is
cancelled or times out (`if: always()` in a workflow). An `EXIT` trap alone is
not enough: a cancelled run can kill the script before its trap finishes.

Why: PR #928 made a new push cancel the older pull request run. The deploy
job's compose stacks were removed only by the scripts' `EXIT` traps, and each
run used unique project names, so a cancelled run would have left them on the
shared runner for good. Review caught it; the job now tags everything with
`CI_RUN_TAG` and removes it in a last `if: always()` step.

## Checks

### An exemption is as narrow as its reason

When a check must let something through, exempt the exact pattern, line or
rule that needs it, not the whole file, directory or check. Name the reason
next to the exemption.

Why: the quarantine check flagged sample violations in its own self-test, and
the first fix skipped that whole file (PR #935). That also hid any real
unannotated skip in it. The fix that held split the sample strings so only the
samples stop matching, and the file stays under every rule.
