#!/usr/bin/env python3
"""Enforce the test quarantine rule (CODING_STANDARDS.md "Tests").

An unconditional skip or expected failure needs a comment on the line directly
above it, naming the issue that tracks the fix:

    # quarantine: #<issue> <why>          (Python)
    // quarantine: #<issue> <why>         (TypeScript and JavaScript)

Unconditional means a decorator without a condition (@pytest.mark.skip,
@pytest.mark.xfail with no condition), a bare pytest.skip() as the first
statement of a test function, it.skip / test.skip / describe.skip and
`skip: true` in a node test option. Conditional skips need no comment:
pytest.mark.skipif, pytest.importorskip, and pytest.skip() inside an if.

`.only(` is never allowed, and neither is a retry (pytest-rerunfailures,
flaky, --reruns, node retry or retries options). A quarantine comment does
not excuse these.

The issue number is checked for shape only (#digits). No network call is made.

Scanned files: the tracked files and the untracked files that are not ignored,
as git ls-files reports them, under backend/tests, worker/tests,
worker/tests_inference and scripts/tests (Python),
frontend/src/**/*.test.ts and frontend/scripts/test-*.mjs (JavaScript), and
every pyproject.toml (retry plugins only).

Usage: python3 scripts/check-test-quarantine.py
Prints "path:line: message" per violation and exits 1; exits 0 when none.
"""
import ast
import os
import re
import subprocess
import sys

PY_DIRS = ("backend/tests/", "worker/tests/", "worker/tests_inference/", "scripts/tests/")
PY_QUARANTINE = re.compile(r"# quarantine: #\d+ \S")
JS_QUARANTINE = re.compile(r"// quarantine: #\d+ \S")
PY_RETRY = re.compile(r"pytest\.mark\.flaky|--reruns|rerunfailures")
TOML_RETRY = re.compile(r"pytest-rerunfailures|\bflaky\b|--reruns")
JS_SKIP = re.compile(r"\b(?:it|test|describe)\.skip\s*\(|\bskip:\s*true\b")
JS_ONLY = re.compile(r"\.only\s*\(")
JS_RETRY = re.compile(r"\bretries?\s*:")

PY_SKIP_HINT = "unconditional skip needs a comment on the line above: # quarantine: #<issue> <why>"
JS_SKIP_HINT = "unconditional skip needs a comment on the line above: // quarantine: #<issue> <why>"
RETRY_HINT = "retries are not allowed: fix the flaky test instead"


def dotted(node):
    """Return 'a.b.c' for a Name or Attribute chain, '' for anything else."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


def check_python(text):
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        return [(error.lineno or 1, f"cannot parse: {error.msg}")]

    def quarantined(lineno):
        return lineno >= 2 and PY_QUARANTINE.match(lines[lineno - 2].strip()) is not None

    found = []
    for lineno, line in enumerate(lines, start=1):
        if PY_RETRY.search(line):
            found.append((lineno, RETRY_HINT))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for deco in node.decorator_list:
                name = dotted(deco)
                if name == "pytest.mark.skip":
                    unconditional = True
                elif name == "pytest.mark.xfail":
                    # The first positional argument, or condition=, makes it conditional.
                    unconditional = not isinstance(deco, ast.Call) or (
                        not deco.args and not any(k.arg == "condition" for k in deco.keywords)
                    )
                else:
                    continue
                if unconditional and not quarantined(deco.lineno):
                    found.append((deco.lineno, PY_SKIP_HINT))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]  # a docstring may come first
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Call) \
                    and dotted(body[0].value) == "pytest.skip":
                call = body[0].value
                if not quarantined(call.lineno):
                    found.append((call.lineno, PY_SKIP_HINT))
    return found


def check_js(text):
    lines = text.splitlines()

    def quarantined(index):
        return index >= 1 and JS_QUARANTINE.match(lines[index - 1].strip()) is not None

    found = []
    for index, line in enumerate(lines):
        lineno = index + 1
        if JS_ONLY.search(line):
            found.append((lineno, ".only( is never allowed: remove it"))
        if JS_RETRY.search(line):
            found.append((lineno, RETRY_HINT))
        if JS_SKIP.search(line) and not quarantined(index):
            found.append((lineno, JS_SKIP_HINT))
    return found


def check_toml(text):
    return [
        (lineno, "retry plugins are not allowed in pyproject.toml: fix the flaky test instead")
        for lineno, line in enumerate(text.splitlines(), start=1)
        if TOML_RETRY.search(line)
    ]


def check_text(path, text):
    """Return (lineno, message) pairs for one file's text."""
    if os.path.basename(path) == "pyproject.toml":
        return check_toml(text)
    if path.endswith(".py"):
        return check_python(text)
    return check_js(text)


def candidate_test_files(root):
    """Yield each test file once: tracked files and untracked, non-ignored files."""
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout
    for path in dict.fromkeys(listing.split("\0")):
        name = os.path.basename(path)
        if name == "pyproject.toml":
            yield path
        elif path.endswith(".py") and path.startswith(PY_DIRS):
            yield path
        elif path.startswith("frontend/src/") and path.endswith(".test.ts"):
            yield path
        elif os.path.dirname(path) == "frontend/scripts" and name.startswith("test-") \
                and name.endswith(".mjs"):
            yield path


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    violations = []
    checked = 0
    for path in candidate_test_files(root):
        checked += 1
        with open(os.path.join(root, path), encoding="utf-8") as fh:
            text = fh.read()
        for lineno, message in check_text(path, text):
            violations.append(f"{path}:{lineno}: {message}")
    for violation in violations:
        print(violation)
    if violations:
        return 1
    print(f"test quarantine: {checked} files checked, no violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
