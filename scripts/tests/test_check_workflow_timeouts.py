from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

DRIVER = Path(__file__).resolve().parents[1] / "check-workflow-timeouts.py"
SPEC = importlib.util.spec_from_file_location("check_workflow_timeouts", DRIVER)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check
SPEC.loader.exec_module(check)

PASSING = """\
name: ok
on: push
jobs:
  build:
    runs-on: [self-hosted]
    timeout-minutes: 15
    steps:
      - run: make verify
"""

FAILING = """\
name: bad
on: push
jobs:
  build:
    runs-on: [self-hosted]
    steps:
      - run: make verify
        timeout-minutes: 5
"""

REUSABLE_CALLER = """\
name: caller
on: push
jobs:
  changes:
    uses: ./.github/workflows/changes.yml
"""

MIXED = """\
name: mixed
on: push
jobs:
  changes:
    uses: ./.github/workflows/changes.yml
  test:
    needs: changes
    timeout-minutes: 20
    runs-on: [self-hosted]
    steps:
      - run: make test
  lint:
    runs-on: [self-hosted]
    steps:
      - run: make lint
"""


def write_dir(files: dict[str, str]) -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory()
    for name, text in files.items():
        Path(tmp.name, name).write_text(text, encoding="utf-8")
    return tmp


class CheckWorkflowTimeoutsTest(unittest.TestCase):
    def run_main(self, directory: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = check.main(["check", directory])
        return code, out.getvalue()

    def test_job_with_timeout_passes(self) -> None:
        tmp = write_dir({"ok.yml": PASSING})
        self.addCleanup(tmp.cleanup)
        code, out = self.run_main(tmp.name)
        self.assertEqual(code, 0, out)
        self.assertEqual(out, "")

    def test_job_without_timeout_fails_and_names_job(self) -> None:
        tmp = write_dir({"bad.yml": FAILING})
        self.addCleanup(tmp.cleanup)
        code, out = self.run_main(tmp.name)
        self.assertEqual(code, 1)
        self.assertIn("bad.yml: build", out)

    def test_reusable_caller_is_not_flagged(self) -> None:
        tmp = write_dir({"caller.yml": REUSABLE_CALLER})
        self.addCleanup(tmp.cleanup)
        code, out = self.run_main(tmp.name)
        self.assertEqual(code, 0, out)

    def test_only_the_job_without_job_level_timeout_is_flagged(self) -> None:
        tmp = write_dir({"mixed.yml": MIXED})
        self.addCleanup(tmp.cleanup)
        code, out = self.run_main(tmp.name)
        self.assertEqual(code, 1)
        self.assertEqual(out.strip().splitlines(), [f"{Path(tmp.name, 'mixed.yml')}: lint"])

    def test_line_parser_agrees_with_yaml(self) -> None:
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        for text in (PASSING, FAILING, REUSABLE_CALLER, MIXED):
            self.assertEqual(check.jobs_with_lines(text), check.jobs_with_yaml(text))

    def test_empty_directory_is_an_error(self) -> None:
        tmp = write_dir({})
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(SystemExit):
            check.main(["check", tmp.name])


if __name__ == "__main__":
    unittest.main()
