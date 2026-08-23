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
_RECOVERY_GROUPS = 4


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode()


def code_at(secret: str, moment: int, digits: int = 6) -> str:
    counter = struct.pack(">Q", moment // STEP)
    digest = hmac.new(base64.b32decode(secret), counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(truncated % 10**digits).zfill(digits)


def matched_step(secret: str, code: str, at: int | None = None) -> int | None:
    """The time step this code belongs to, or None.

    The caller needs the step, not just a yes: RFC 6238 requires that a code
    which has been accepted once is never accepted again, and the step is what
    identifies it.
    """
    moment = int(time.time()) if at is None else at
    candidate = code.strip().encode("utf-8", "replace")
    for offset in (-1, 0, 1):
        step_at = moment + offset * STEP
        try:
            expected = code_at(secret, step_at).encode()
        except ValueError:
            return None
        if hmac.compare_digest(candidate, expected):
            return step_at // STEP
    return None


def verify(secret: str, code: str, at: int | None = None) -> bool:
    """Accept one step of drift either side and no more: every extra step widens
    the window an attacker guesses into."""
    return matched_step(secret, code, at) is not None


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
