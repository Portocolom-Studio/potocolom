#!/usr/bin/env python3
"""Render every Mermaid diagram under docs/."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# \r is tolerated so a CRLF checkout still matches: finding nothing would
# otherwise report success while verifying no diagrams at all.
FENCED_MERMAID = re.compile(
    r"^```mermaid[ \t]*\r?\n(.*?)^```[ \t]*\r?$",
    re.MULTILINE | re.DOTALL,
)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    # Same override order as frontend/scripts/generate-hero-preview.mjs.
    chrome = Path(
        os.environ.get("PUPPETEER_EXECUTABLE_PATH")
        or os.environ.get("CHROME_PATH")
        or "/usr/bin/google-chrome"
    )
    mmdc = shutil.which("mmdc")
    if mmdc is None:
        sys.exit("error: mmdc is required; install mermaid-cli")
    if not chrome.is_file():
        sys.exit(
            f"error: Chrome is required at {chrome}; "
            "set PUPPETEER_EXECUTABLE_PATH or CHROME_PATH to override"
        )

    diagrams = []
    for path in sorted((root / "docs").rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        for block, match in enumerate(FENCED_MERMAID.finditer(text), start=1):
            diagrams.append((path, block, match.group(1)))

    if not diagrams:
        # An extraction bug must fail loudly rather than pass having checked nothing.
        sys.exit("error: no Mermaid diagrams found under docs/; extraction is broken")

    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        config = work / "puppeteer.json"
        config.write_text(
            json.dumps({"executablePath": str(chrome), "args": ["--no-sandbox"]}),
            encoding="utf-8",
        )
        for number, (path, block, diagram) in enumerate(diagrams, start=1):
            source = work / f"{number}.mmd"
            output = work / f"{number}.svg"
            source.write_text(diagram, encoding="utf-8")
            result = subprocess.run(
                [mmdc, "-p", str(config), "-i", str(source), "-o", str(output)],
                capture_output=True,
                text=True,
            )
            if result.returncode:
                relative = path.relative_to(root)
                print(f"error: {relative} Mermaid block {block} failed to render", file=sys.stderr)
                print(result.stdout, end="", file=sys.stderr)
                print(result.stderr, end="", file=sys.stderr)
                return result.returncode

    print(f"mermaid ok: {len(diagrams)} diagrams")
    return 0


if __name__ == "__main__":
    sys.exit(main())
