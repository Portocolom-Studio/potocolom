#!/usr/bin/env bash
# Rejects AI attribution and decorative characters in a commit message or a
# pull request body. The commit-msg hook and commit-hygiene CI both call it,
# so the two gates apply the same rules.
set -euo pipefail

if [ "$#" -ne 1 ]; then
	echo "usage: check-commit-msg.sh <message-file>" >&2
	exit 2
fi

exec python3 - "$1" <<'PY'
import re
import sys

AI_ATTRIBUTION = re.compile(
    r"^\s*co-authored-by:.*(claude|anthropic|cursor|copilot|openai|gpt|gemini|codex)"
    r"|generated with \[?(claude|cursor|copilot|chatgpt|gemini|codex|openai)",
    re.IGNORECASE,
)
# Accented letters are not listed, so names such as Jose still pass.
BANNED = [
    (0x2010, 0x2027),
    (0x00A0, 0x00A0),
    (0x00D7, 0x00D7),
    (0x2190, 0x21FF),
    (0x2264, 0x2265),
    (0x2500, 0x257F),
    (0x2600, 0x27BF),
    (0x1F000, 0x1FAFF),
]

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
except (OSError, UnicodeDecodeError) as error:
    print(f"check-commit-msg: cannot read {path} as UTF-8: {error}", file=sys.stderr)
    sys.exit(2)

problems = []
for number, line in enumerate(lines, 1):
    if line.startswith("#"):
        continue
    if AI_ATTRIBUTION.search(line):
        problems.append((number, "AI attribution", line))
    for char in line:
        if any(low <= ord(char) <= high for low, high in BANNED):
            problems.append((number, f"banned character U+{ord(char):04X}", line))
            break

for number, reason, line in problems:
    print(f"check-commit-msg: line {number}: {reason}: {line}", file=sys.stderr)
sys.exit(1 if problems else 0)
PY
