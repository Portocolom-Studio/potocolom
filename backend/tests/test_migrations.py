"""conftest decides whether the shared test database is stamped ahead of this
checkout by reading revision ids off migration filenames. Keep that true."""

import re

from conftest import _VERSIONS, _local_revisions


def test_local_revisions_match_declared_revisions():
    declared = set()
    for path in _VERSIONS.glob("[0-9]*.py"):
        match = re.search(r'^revision = "([^"]+)"', path.read_text(), re.MULTILINE)
        assert match, f"{path.name} declares no revision"
        declared.add(match.group(1))
    assert declared, "no migrations found"
    assert _local_revisions() == declared
