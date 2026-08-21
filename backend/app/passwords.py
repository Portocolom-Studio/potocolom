"""Password policy and storage for account credentials.

The length window and the bundled blocklist are enforced inside hash_password
so that no caller can store a hash for a password the policy refuses. Both
checks read the stripped, lowercased form, so surrounding whitespace or a
change of case cannot carry a blocked password past them.
"""

from functools import cache
from pathlib import Path

from argon2 import PasswordHasher

MIN_LENGTH = 15
MAX_LENGTH = 128

_BLOCKLIST_FILE = Path(__file__).with_name("password_blocklist.txt")
_HASHER = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1)


class PasswordRejected(Exception):
    pass


@cache
def blocklist() -> frozenset[str]:
    text = _BLOCKLIST_FILE.read_text(encoding="utf-8")
    return frozenset(line.strip().lower() for line in text.splitlines() if line.strip())


def hash_password(password: str) -> str:
    candidate = password.strip()
    if not MIN_LENGTH <= len(candidate) <= MAX_LENGTH:
        raise PasswordRejected(f"password must be {MIN_LENGTH} to {MAX_LENGTH} characters")
    if candidate.lower() in blocklist():
        raise PasswordRejected("password is on the bundled blocklist")
    return _HASHER.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _HASHER.verify(stored_hash, password)
    except Exception:
        return False
