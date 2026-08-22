import base64

import pytest

from app import totp

# RFC 6238 appendix B, the SHA-1 rows. The published vectors are the point:
# they check this against the standard rather than against itself.
RFC_SECRET = b"12345678901234567890"
RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


@pytest.mark.parametrize("moment, expected", RFC_VECTORS)
def test_the_published_vectors_hold(moment, expected):
    secret = base64.b32encode(RFC_SECRET).decode()
    assert totp.code_at(secret, moment, digits=8) == expected


def test_a_code_is_six_digits_by_default():
    secret = totp.new_secret()
    code = totp.code_at(secret, 1234567890)
    assert len(code) == 6 and code.isdigit()


def test_a_secret_is_base32_and_not_guessable():
    first, second = totp.new_secret(), totp.new_secret()
    assert first != second
    assert base64.b32decode(first)
    # 160 bits, the RFC's recommendation for SHA-1.
    assert len(base64.b32decode(first)) == 20


def test_the_current_code_verifies():
    secret = totp.new_secret()
    assert totp.verify(secret, totp.code_at(secret, 1000), at=1000) is True


def test_a_wrong_code_does_not():
    secret = totp.new_secret()
    assert totp.verify(secret, "000000", at=1000) is False
    assert totp.verify(secret, "", at=1000) is False
    assert totp.verify(secret, "not-a-code", at=1000) is False


def test_a_code_nobody_could_have_typed_is_refused_rather_than_raised():
    """compare_digest refuses non-ASCII strings, and this value arrives in a
    request body. Raising here turns a wrong code into a 500."""
    secret = totp.new_secret()
    assert totp.verify(secret, "caf\u00e9", at=1000) is False
    assert totp.verify(secret, "\U0001f600" * 6, at=1000) is False


def test_a_broken_secret_is_refused_rather_than_raised():
    assert totp.verify("not-base32!", "000000", at=1000) is False


def test_one_step_of_drift_is_tolerated_in_both_directions():
    """A phone clock is never exactly right, and a code typed at the end of
    its window arrives in the next one."""
    secret = totp.new_secret()
    now = 1_000_000
    assert totp.verify(secret, totp.code_at(secret, now - totp.STEP), at=now) is True
    assert totp.verify(secret, totp.code_at(secret, now + totp.STEP), at=now) is True


def test_two_steps_of_drift_is_not():
    """Every extra step widens the window an attacker guesses into."""
    secret = totp.new_secret()
    now = 1_000_000
    assert totp.verify(secret, totp.code_at(secret, now - 2 * totp.STEP), at=now) is False
    assert totp.verify(secret, totp.code_at(secret, now + 2 * totp.STEP), at=now) is False


def test_the_enrolment_uri_carries_what_an_authenticator_needs():
    secret = totp.new_secret()
    uri = totp.enrolment_uri(secret, account="ana@example.com", issuer="potocolom")
    assert uri.startswith("otpauth://totp/")
    assert f"secret={secret}" in uri
    assert "issuer=potocolom" in uri
    assert "algorithm=SHA1" in uri
    assert "digits=6" in uri
    assert f"period={totp.STEP}" in uri
    # The label is percent encoded, so an address with an @ does not break it.
    assert "ana%40example.com" in uri


def test_recovery_codes_are_distinct_and_readable():
    codes = totp.new_recovery_codes()
    assert len(codes) == totp.RECOVERY_CODES == 10
    assert len(set(codes)) == len(codes)
    for code in codes:
        # Grouped for someone copying them off a screen by hand.
        assert "-" in code
        assert code == code.lower()
