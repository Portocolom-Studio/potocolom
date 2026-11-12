---
title: How to contribute
subtitle: The workflow, the sign-off rule and what a pull request must show.
tab: contributing
group: Contribute
order: 1
source: CONTRIBUTING.md "Contributing", "Developer Certificate of Origin", "Ground rules", "Pull requests"; AGENTS.md
---

Contributions are welcome. The workflow has three steps.

1. Open an issue that describes the change.
2. Work on a short-lived branch.
3. Open a pull request into main that passes `make verify`.

## Developer Certificate of Origin

Every commit must carry a `Signed-off-by` line. The name and email must match the commit author:

```
Signed-off-by: Your Name <you@example.com>
```

Run `git commit -s`. Git adds the line for you.

The sign-off asserts the [Developer Certificate of Origin](https://developercertificate.org/). It means that you wrote the change, or that you have the right to submit it under the project license.

### Add the sign-off with a hook

Run `make dco-hook` once in your clone. It sets `core.hooksPath` to the versioned [`.githooks/`](https://github.com/Portocolom-Studio/potocolom/blob/main/.githooks/) directory.

The `prepare-commit-msg` hook adds the trailer from your `git config user.name` and `user.email`. It adds the line only when the message does not have one yet. After that, plain `git commit` is enough.

Undo the hook with `git config --unset core.hooksPath`.

The same hooks reject AI attribution trailers and decorative characters in the commit message. CI applies the same rules to every commit and to the PR body.

The hooks stay current when you pull. The hook adds only the trailer. It does not set your author identity, so set `user.name` and `user.email` first.

Forgot the sign-off on an existing branch? Run `git rebase --signoff main`. It signs off every commit on the branch.

### Why the project needs it

potocolom is AGPL-3.0. The project sells commercial exceptions ([COMMERCIAL.md](https://github.com/Portocolom-Studio/potocolom/blob/main/COMMERCIAL.md)). Dual licensing works only while the project keeps the right to relicense the whole. So the origin of every contribution must be explicit. A pull request without a sign-off cannot be merged.

## Ground rules

- No emojis or decorative characters in code, comments, commits, issues or PRs.
- Match the existing style of the file you edit.
- Reviewers apply [CODING_STANDARDS.md](https://github.com/Portocolom-Studio/potocolom/blob/main/CODING_STANDARDS.md) to every diff.
- Run `make verify` before you open or update a PR. It runs the backend, worker and frontend gates. For the test levels, read [the testing ladder](/docs/contributing/testing-ladder).
- CI also runs these checks. Each one has its own requirement:
  - `make verify-guards` needs Python 3.11 or newer with venv support.
  - `make lint-workflows` needs curl or wget. It downloads pinned actionlint and zizmor.
  - `make verify-worker-inference` needs the inference extras.
  - `make verify-mermaid` needs mmdc and Chrome.
  - `make verify-compose` needs Docker.
  - The secret scan behind `make verify-secrets`.
  - Deploy CI also runs the Docker-based release-check tests.
- Merge only when every required check is green. Update a stale test or CI step in the same PR. Never skip, disable or loosen a check to get green.
- Apply the [security and recovery checks](/docs/contributing/security-checklist) to each affected path. Record the evidence in the PR.
- For UI changes, follow the [design system](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/design-system.md). Update it in the same PR when a token, component contract or rule changes.
- Mermaid diagrams must render. Run `make verify-mermaid` before you push.

## Pull requests

- Start from an issue. For a new feature, open the issue first. Wait for a maintainer to agree on the scope. Work that is ahead of its issue is not merged.
- One problem per PR. Split unrelated fixes into their own PRs, even when they are small.
- Show that it works. A logic, data, state or protocol bug needs a test that fails without the fix. Write the test at the lowest layer that shows the bug.
- A purely visual change needs before and after screenshots. Motion or timing needs a short recording. Upload them to the PR. Do not commit them.
- Extend an existing test before you add a new one. Never wait with a fixed sleep. See [CODING_STANDARDS.md](https://github.com/Portocolom-Studio/potocolom/blob/main/CODING_STANDARDS.md).
- Fill in every section of the PR template. List the checks you ran, and the checks you could not run.

## Working with a coding agent

Coding agents are welcome. The guide for agents is [AGENTS.md](https://github.com/Portocolom-Studio/potocolom/blob/main/AGENTS.md), at the repository root. Give it to your agent. You stay the author. Review the change and sign it off. Leave out agent attribution lines. The hooks and CI reject them.

The key points from AGENTS.md are:

- Read the four rules under "What we never compromise on". They cover self-hosting, realtime, private data by default, and the line between this repository and the private cloud code.
- Check GPU memory headroom before a GPU job. Never kill a process that you found by matching its name or path.
- Test the right checkout. In a git worktree, a bare `pytest` tests the main checkout's code, not your worktree.
- Walk the list of surfaces before you call a change done. The list covers profiles, site modes, hardware, the wire protocol, languages, reverse states, data and security.
- Docs upkeep is part of done. Before you commit, fix stale text in the repository docs and on the /docs pages.
- An agent opens a pull request only when the person it works for asks for one.
