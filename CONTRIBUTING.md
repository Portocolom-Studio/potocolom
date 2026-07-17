# Contributing

Contributions are welcome. The workflow is ordinary: an issue describing the change, a short-lived branch, a PR into main that passes `make verify`.

## Developer Certificate of Origin

Every commit must carry a `Signed-off-by` line matching the commit author:

```
Signed-off-by: Your Name <you@example.com>
```

`git commit -s` adds it for you. The sign-off asserts the [Developer Certificate of Origin](https://developercertificate.org/): you wrote the change or otherwise have the right to submit it under the project license.

To sign off automatically in your clone, add a `prepare-commit-msg` hook:

```sh
cat > .git/hooks/prepare-commit-msg <<'HOOK'
#!/bin/sh
NAME=$(git config user.name)
EMAIL=$(git config user.email)
grep -qs "^Signed-off-by: " "$1" || printf "\nSigned-off-by: %s <%s>\n" "$NAME" "$EMAIL" >> "$1"
HOOK
chmod +x .git/hooks/prepare-commit-msg
```

Forgot on an existing branch? `git rebase --signoff main` retrofits every commit on it.

Why this project requires it: potocolom is AGPL-3.0 with commercial exceptions sold by the project ([COMMERCIAL.md](COMMERCIAL.md)). Dual licensing is only possible while the project retains the right to relicense the whole, so the provenance of every contribution has to be explicit. PRs without sign-off cannot be merged.

## Ground rules

- No emojis or decorative characters in code, comments, commits, issues or PRs.
- Match the existing style of the file you are editing.
- Run `make verify` locally before opening or updating a PR; it runs exactly what CI runs.
- Mermaid diagrams must render; validate them before pushing.
