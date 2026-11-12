from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from textwrap import dedent

DRIVER = Path(__file__).resolve().parents[1] / "check-test-quarantine.py"
SPEC = importlib.util.spec_from_file_location("check_test_quarantine", DRIVER)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check
SPEC.loader.exec_module(check)

# Split so this file does not trip the retry check when the script scans it.
RERUN_PLUGIN = "pytest-" "rerunfailures"
RERUN_FLAG = "--" "reruns"
FLAKY_MARKER = "pytest.mark" + ".flaky"


def lines_of(path: str, text: str) -> list[int]:
    return [lineno for lineno, _ in check.check_text(path, dedent(text))]


class PythonQuarantine(unittest.TestCase):
    def test_unconditional_skip_without_comment_fails(self):
        text = """\
            import pytest

            @pytest.mark.skip(reason="flaky on CI")
            def test_a():
                assert True
            """
        self.assertEqual(lines_of("backend/tests/test_a.py", text), [3])

    def test_skip_with_quarantine_comment_passes(self):
        text = """\
            import pytest

            # quarantine: #924 the fixture leaks a socket on CI
            @pytest.mark.skip(reason="flaky on CI")
            def test_a():
                assert True
            """
        self.assertEqual(lines_of("backend/tests/test_a.py", text), [])

    def test_quarantine_comment_needs_issue_number_and_reason(self):
        text = """\
            import pytest

            # quarantine: needs a fix
            @pytest.mark.skip(reason="flaky on CI")
            def test_a():
                assert True
            """
        self.assertEqual(lines_of("backend/tests/test_a.py", text), [4])

    def test_skipif_passes_without_comment(self):
        text = """\
            import sys
            import pytest

            @pytest.mark.skipif(sys.platform == "win32", reason="no fork")
            def test_a():
                assert True
            """
        self.assertEqual(lines_of("backend/tests/test_a.py", text), [])

    def test_xfail_without_condition_fails_and_with_condition_passes(self):
        bare = """\
            import pytest

            @pytest.mark.xfail(reason="known bug")
            def test_a():
                assert False
            """
        conditional = """\
            import sys
            import pytest

            @pytest.mark.xfail(sys.platform == "win32", reason="known bug")
            def test_a():
                assert False
            """
        self.assertEqual(lines_of("worker/tests/test_a.py", bare), [3])
        self.assertEqual(lines_of("worker/tests/test_a.py", conditional), [])

    def test_bare_pytest_skip_first_statement_fails(self):
        text = """\
            import pytest

            def test_a():
                pytest.skip("database unavailable")
                assert True
            """
        self.assertEqual(lines_of("worker/tests/test_a.py", text), [4])

    def test_pytest_skip_after_a_docstring_fails_and_inside_if_passes(self):
        first = """\
            import pytest

            def test_a():
                \"\"\"Docstring first.\"\"\"
                pytest.skip("not cached")
            """
        inside_if = """\
            import pytest

            def test_a():
                value = 1
                if value == 2:
                    pytest.skip("not cached")
            """
        self.assertEqual(lines_of("worker/tests/test_a.py", first), [5])
        self.assertEqual(lines_of("worker/tests/test_a.py", inside_if), [])

    def test_rerun_plugin_marker_and_flag_fail(self):
        marker = f"import pytest\n\n@{FLAKY_MARKER}(reruns=2)\ndef test_a():\n    pass\n"
        flag = f"import pytest\n\nargs = ['{RERUN_FLAG}', '2']  # {RERUN_PLUGIN}\n"
        self.assertEqual(lines_of("backend/tests/test_a.py", marker), [3])
        self.assertEqual(lines_of("backend/tests/test_a.py", flag), [3])


class JavaScriptQuarantine(unittest.TestCase):
    def test_it_skip_needs_comment(self):
        bare = "it.skip('renders', () => {});\n"
        quarantined = "// quarantine: #924 the picker races the mock\nit.skip('renders', () => {});\n"
        self.assertEqual(lines_of("frontend/scripts/test-a.mjs", bare), [1])
        self.assertEqual(lines_of("frontend/scripts/test-a.mjs", quarantined), [])

    def test_skip_true_option_needs_comment_and_skip_expression_passes(self):
        literal = "test('renders', { skip: true }, () => {});\n"
        expression = "test('renders', { skip: process.env.CI === 'x' }, () => {});\n"
        self.assertEqual(lines_of("frontend/src/lib/a.test.ts", literal), [1])
        self.assertEqual(lines_of("frontend/src/lib/a.test.ts", expression), [])

    def test_only_fails_even_with_quarantine_comment(self):
        text = "// quarantine: #924 debugging a hang\ntest.only('renders', () => {});\n"
        self.assertEqual(lines_of("frontend/src/lib/a.test.ts", text), [2])

    def test_retries_option_fails(self):
        text = "test('renders', { retries: 2 }, () => {});\n"
        self.assertEqual(lines_of("frontend/src/lib/a.test.ts", text), [1])


class Pyproject(unittest.TestCase):
    def test_rerunfailures_in_pyproject_fails(self):
        text = f'[project.optional-dependencies]\ntest = [\n  "{RERUN_PLUGIN}>=14",\n]\n'
        self.assertEqual(lines_of("backend/pyproject.toml", text), [3])

    def test_pyproject_without_retry_plugin_passes(self):
        text = '[project.optional-dependencies]\ntest = [\n  "pytest>=8",\n]\n'
        self.assertEqual(lines_of("backend/pyproject.toml", text), [])


if __name__ == "__main__":
    unittest.main()
