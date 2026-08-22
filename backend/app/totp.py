"""RFC 6238 time based one time passwords, and the material an enrolment needs."""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

STEP = 30
RECOVERY_CODES = 10

_SECRET_BYTES = 20
_RECOVERY_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
_RECOVERY_GROUP = 5
_RECOVERY_GROUPS = 2


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode()


def code_at(secret: str, moment: int, digits: int = 6) -> str:
    counter = struct.pack(">Q", moment // STEP)
    digest = hmac.new(base64.b32decode(secret), counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(truncated % 10**digits).zfill(digits)


def verify(secret: str, code: str, at: int | None = None) -> bool:
    """Accept one step of drift either side and no more: every extra step widens
    the window an attacker guesses into."""
    moment = int(time.time()) if at is None else at
    # Compared as bytes: compare_digest refuses non-ASCII strings, and this
    # value arrives in a request body, so raising would turn a wrong code into
    # a 500 rather than a refusal.
    candidate = code.strip().encode("utf-8", "replace")
    try:
        expected = [code_at(secret, moment + offset * STEP) for offset in (-1, 0, 1)]
    except ValueError:
        return False
    return any(hmac.compare_digest(candidate, other.encode()) for other in expected)


def enrolment_uri(secret: str, account: str, issuer: str) -> str:
    label = quote(f"{issuer}:{account}")
    query = urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": 6,
            "period": STEP,
        }
    )
    return f"otpauth://totp/{label}?{query}"


def new_recovery_codes() -> list[str]:
    codes: set[str] = set()
    while len(codes) < RECOVERY_CODES:
        groups = (
            "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_GROUP))
            for _ in range(_RECOVERY_GROUPS)
        )
        codes.add("-".join(groups))
    return sorted(codes)
