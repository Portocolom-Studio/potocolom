#!/usr/bin/env python3
"""Render every Mermaid diagram under docs/."""

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FENCED_MERMAID = re.compile(
    r"^```mermaid[ \t]*\n(.*?)^```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    chrome = Path("/usr/bin/google-chrome")
    mmdc = shutil.which("mmdc")
    if mmdc is None:
        sys.exit("error: mmdc is required; install mermaid-cli")
    if not chrome.is_file():
        sys.exit(f"error: Chrome is required at {chrome}")

    diagrams = []
    for path in sorted((root / "docs").rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        for block, match in enumerate(FENCED_MERMAID.finditer(text), start=1):
            diagrams.append((path, block, match.group(1)))

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
