# Review instructions

Read [AGENTS.md](../AGENTS.md) and [CODING_STANDARDS.md](../CODING_STANDARDS.md) and apply them to every pull request.

- Do not ask to reverse a choice recorded in [docs/decisions.md](../docs/decisions.md). Say that a new decision entry is needed instead.
- Check the "Hit every surface" list in AGENTS.md: profiles, both site modes, hardware, the wire protocol with N-1 compatibility, both languages, reverse states, data, security, docs.
- A bug fix needs a test that fails without the fix. Flag fixed sleeps and retries in tests.
- Backend endpoints must match [docs/api.md](../docs/api.md); the wire protocol must match [docs/connection-handling.md](../docs/connection-handling.md).
- A changed repository doc needs its /docs mirror page updated (`scripts/check-docs-site.sh`).
- Flag non-ASCII punctuation, emojis and AI attribution lines.
