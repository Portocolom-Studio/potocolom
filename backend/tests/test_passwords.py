import pytest

from app.passwords import (
    MAX_LENGTH,
    MIN_LENGTH,
    PasswordRejected,
    blocklist,
    hash_password,
    verify_password,
)

GOOD = "correct horse battery staple"


def test_the_designed_length_window_is_fifteen_to_one_hundred_and_twenty_eight():
    assert (MIN_LENGTH, MAX_LENGTH) == (15, 128)


@pytest.mark.parametrize("password", ["", "short", "a" * (MIN_LENGTH - 1)])
def test_a_short_password_is_refused(password):
    with pytest.raises(PasswordRejected):
        hash_password(password)


def test_a_password_at_the_lower_bound_is_accepted():
    assert hash_password("a" * MIN_LENGTH)


def test_a_password_past_the_upper_bound_is_refused():
    """Argon2 hashes whatever it is given, so an unbounded password is an
    unbounded amount of work for anyone who can post one."""
    assert hash_password("b" * MAX_LENGTH)
    with pytest.raises(PasswordRejected):
        hash_password("b" * (MAX_LENGTH + 1))
    # Padding must not walk past the cap either: a check that measured only
    # the stripped value would hand Argon2 an unbounded string.
    with pytest.raises(PasswordRejected):
        hash_password(" " + "b" * MAX_LENGTH)
    with pytest.raises(PasswordRejected):
        hash_password(" " * 10000 + "b" * MIN_LENGTH + " " * 10000)


def test_the_bundled_blocklist_is_offline_and_not_empty():
    assert len(blocklist()) >= 50
    assert all(len(entry) >= MIN_LENGTH for entry in blocklist())
    assert all(entry == entry.strip().lower() for entry in blocklist())


def test_a_blocklisted_password_is_refused_whatever_its_case():
    entry = sorted(blocklist())[0]
    with pytest.raises(PasswordRejected):
        hash_password(entry)
    with pytest.raises(PasswordRejected):
        hash_password(entry.upper())
    with pytest.raises(PasswordRejected):
        hash_password(f"  {entry}  ")


def test_hashing_is_the_only_door_so_no_caller_can_skip_the_policy():
    """The policy lives behind the hash, not beside it: a caller that reaches
    for the hash cannot forget to check first."""
    with pytest.raises(PasswordRejected):
        hash_password("password")


def test_a_hash_verifies_its_own_password_and_nothing_else():
    stored = hash_password(GOOD)
    assert verify_password(stored, GOOD) is True
    assert verify_password(stored, GOOD + "!") is False
    assert verify_password(stored, "") is False


def test_the_hash_is_argon2id_with_the_designed_parameters():
    stored = hash_password(GOOD)
    assert stored.startswith("$argon2id$")
    assert "m=19456" in stored
    assert "t=2" in stored
    assert "p=1" in stored


def test_the_same_password_never_hashes_to_the_same_string():
    assert hash_password(GOOD) != hash_password(GOOD)


def test_the_stored_hash_never_contains_the_password():
    assert GOOD not in hash_password(GOOD)


def test_a_corrupt_stored_hash_verifies_nothing_and_does_not_raise():
    """A row an operator edited by hand must fail the login, not the process."""
    assert verify_password("not-a-hash", GOOD) is False
    assert verify_password("", GOOD) is False
