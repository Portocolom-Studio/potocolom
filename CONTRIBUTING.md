# Contributing

Contributions are welcome. The workflow is ordinary: an issue describing the change, a short-lived branch, a PR into main that passes `make verify`.

## Developer Certificate of Origin

Every commit must carry a `Signed-off-by` line matching the commit author:

```
Signed-off-by: Your Name <you@example.com>
```

`git commit -s` adds it for you. The sign-off asserts the [Developer Certificate of Origin](https://developercertificate.org/): you wrote the change or otherwise have the right to submit it under the project license.

To sign off automatically in your clone, run `make dco-hook` once. It points `core.hooksPath` at the versioned [`.githooks/`](.githooks/) directory, whose `prepare-commit-msg` appends the trailer from your `git config user.name` and `user.email` when a message does not already have one. Plain `git commit` is then enough. Undo it with `git config --unset core.hooksPath`. The same hooks also reject AI attribution trailers and decorative characters in the message, and CI applies the same rules to every commit and the PR body.

Hooks installed this way stay current when you pull, and they do not replace your author identity: set `user.name` and `user.email` correctly first, since the hook only adds the trailer.

Forgot on an existing branch? `git rebase --signoff main` retrofits every commit on it.

Why this project requires it: potocolom is AGPL-3.0 with commercial exceptions sold by the project ([COMMERCIAL.md](COMMERCIAL.md)). Dual licensing is only possible while the project retains the right to relicense the whole, so the provenance of every contribution has to be explicit. PRs without sign-off cannot be merged.

## Ground rules

- No emojis or decorative characters in code, comments, commits, issues or PRs.
- Match the existing style of the file you are editing.
- Reviewers apply [CODING_STANDARDS.md](CODING_STANDARDS.md) to every diff.
- Run `make verify` locally before opening or updating a PR; it runs the backend, worker and frontend gates. CI also runs `make verify-guards` (needs Python 3.11+ with venv support), `make lint-workflows` (needs curl or wget; downloads pinned actionlint and zizmor), `make verify-worker-inference` (needs the inference extras), `make verify-mermaid` (needs mmdc and Chrome), `make verify-compose` (needs Docker) and the secret scan behind `make verify-secrets`; deploy CI also runs the Docker-based release-check tests.
- Merge only when every required check is green; update stale tests or CI steps in the same PR, and never skip, disable or loosen a check to get green.
- Apply the [security and recovery checks](docs/security-checklist.md) to each affected path and record the evidence in the PR.
- For UI changes, follow the [design system](docs/design-system.md), and update it in the same PR when a token, component contract or rule changes.
- Mermaid diagrams must render; run `make verify-mermaid` before pushing.

## Pull requests

- Start from an issue. For a new feature, open the issue first and wait for a maintainer to agree on the scope, since work ahead of its issue is not merged.
- One problem per PR. Split unrelated fixes into their own PRs, even when they are small.
- Show that it works. A logic, data, state or protocol bug gets a test that fails without the fix, at the lowest layer that shows the bug. A purely visual change gets before/after screenshots, and motion or timing gets a short recording. Upload them to the PR; do not commit them.
- Extend an existing test before you add a new one, and never wait with a fixed sleep ([CODING_STANDARDS.md](CODING_STANDARDS.md)).
- Fill in every section of the PR template, including the checks you ran and what you could not check.
- Coding agents are welcome. Read [AGENTS.md](AGENTS.md) and give it to your agent. You remain the author: review the change, sign it off, and leave out agent attribution lines, which the hooks and CI reject.
