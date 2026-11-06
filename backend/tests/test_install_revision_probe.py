"""The installer's downgrade guard asks the target API image whether it knows the
database's Alembic revision (`REVISION_PROBE` in scripts/install.sh). This runs that
exact script against the real migrations, as the image would."""

import re
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
INSTALL_SH = BACKEND.parent / "scripts" / "install.sh"


def _probe() -> str:
    match = re.search(r'^REVISION_PROBE="(.*?)"$', INSTALL_SH.read_text(), re.M | re.S)
    assert match, "REVISION_PROBE not found in scripts/install.sh"
    # The image runs it from /app, where Dockerfile.api puts alembic.ini.
    return match.group(1).replace("/app/alembic.ini", str(BACKEND / "alembic.ini"))


def _answer(revision: str) -> str:
    result = subprocess.run([sys.executable, "-c", _probe(), revision], cwd=BACKEND,
                            capture_output=True, text=True, timeout=60, check=True)
    return result.stdout.strip()


def _revisions() -> list[str]:
    found = []
    for path in sorted((BACKEND / "migrations" / "versions").glob("*.py")):
        match = re.search(r'^revision(?:: str)? = ["\']([^"\']+)["\']', path.read_text(), re.M)
        if match:
            found.append(match.group(1))
    return found


def test_every_shipped_revision_is_known():
    revisions = _revisions()
    assert len(revisions) > 10
    assert {_answer(revision) for revision in (revisions[0], revisions[-1])} == {"known"}


def test_a_revision_the_release_lacks_is_unknown():
    # A database migrated by a newer release holds a revision this one does not ship.
    assert _answer("ffffffffffff") == "unknown"
