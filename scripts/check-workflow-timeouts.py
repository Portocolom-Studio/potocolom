#!/usr/bin/env python3
"""Fail when a GitHub Actions job runs steps but sets no job-level timeout-minutes.

Without the key a job gets GitHub's 360-minute default, so one hung step holds
a self-hosted runner for six hours. A job that only calls a reusable workflow
(uses:) cannot take timeout-minutes at the call site, so it is not checked.

Usage: check-workflow-timeouts.py [workflow-dir]   (default .github/workflows)
Prints "file: job" for each offender and exits 1; exits 0 when none.
"""
import glob
import os
import re
import sys

DEFAULT_DIR = ".github/workflows"
JOB_RE = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
KEY_RE = re.compile(r"^    ([A-Za-z-]+):")


def jobs_with_yaml(text):
    import yaml

    doc = yaml.safe_load(text) or {}
    result = {}
    for name, job in (doc.get("jobs") or {}).items():
        if isinstance(job, dict):
            result[name] = ("steps" in job, "timeout-minutes" in job)
    return result


def jobs_with_lines(text):
    # Job keys sit at indent 2 under a top-level "jobs:"; job-level keys at indent 4.
    result = {}
    in_jobs = False
    job = None
    for line in text.splitlines():
        if line.startswith("jobs:"):
            in_jobs = True
            continue
        if line and not line.startswith(" ") and not line.startswith("#"):
            in_jobs = False
            job = None
            continue
        if not in_jobs:
            continue
        m = JOB_RE.match(line)
        if m:
            job = m.group(1)
            result[job] = (False, False)
            continue
        m = KEY_RE.match(line)
        if job and m:
            steps, timeout = result[job]
            key = m.group(1)
            result[job] = (steps or key == "steps", timeout or key == "timeout-minutes")
    return result


def parse(text):
    try:
        import yaml  # noqa: F401
    except ImportError:
        return jobs_with_lines(text)
    return jobs_with_yaml(text)


def offenders(directory):
    files = sorted(glob.glob(os.path.join(directory, "*.yml")))
    if not files:
        raise SystemExit(f"error: no *.yml workflow files in {directory}")
    found = []
    for path in files:
        with open(path, encoding="utf-8") as fh:
            jobs = parse(fh.read())
        for name, (has_steps, has_timeout) in jobs.items():
            if has_steps and not has_timeout:
                found.append(f"{path}: {name}")
    return found


def main(argv):
    directory = argv[1] if len(argv) > 1 else DEFAULT_DIR
    bad = offenders(directory)
    for line in bad:
        print(line)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
