import pytest

from app.keyring import KeyRing, KeyRingError, parse_root_keys

V1 = bytes(range(32))
V2 = bytes(range(32, 64))
FLEET_SECRET = "a-fleet-shared-secret"


def _ring(*entries: tuple[int, bytes]) -> KeyRing:
    return KeyRing(list(entries))


def test_parse_root_keys_reads_an_active_write_key_first():
    import base64

    raw = f"2:{base64.b64encode(V2).decode()},1:{base64.b64encode(V1).decode()}"
    assert parse_root_keys(raw) == [(2, V2), (1, V1)]


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "2",
    "two:AAAA",
    "0:" + "A" * 44,
    "-1:" + "A" * 44,
    "1:not-base64!!",
    "1:" + "A" * 43,
    "1:QUJD",
    "1:{k},1:{k}",
    "2:{k},1:{k}",
    "32768:{k}",
    "65535:{k}",
])
def test_parse_root_keys_fails_closed_on_anything_it_cannot_trust(raw):
    """A misread key ring must refuse, never silently produce a weaker ring."""
    import base64

    raw = raw.format(k=base64.b64encode(V1).decode())
    with pytest.raises(KeyRingError):
        parse_root_keys(raw)


def test_a_repeated_key_under_a_new_version_is_refused():
    """Rotating onto the same bytes leaves every row on the key being retired,
    and nothing downstream can tell: the ring shows two versions, the startup
    check passes, and the re-encrypt sweep reports it finished."""
    import base64

    key = base64.b64encode(V1).decode()
    with pytest.raises(KeyRingError):
        parse_root_keys(f"2:{key},1:{key}")


def test_a_version_wider_than_its_column_is_refused():
    """key_version is a signed smallint, so a wider version fails on INSERT
    after the secret it protects has already been generated."""
    import base64

    with pytest.raises(KeyRingError):
        parse_root_keys(f"32768:{base64.b64encode(V1).decode()}")


def test_purpose_keys_are_domain_separated():
    ring = _ring((1, V1))
    sessions = ring.derive("sessions")
    factors = ring.derive("totp-factors")
    assert sessions != factors
    assert sessions != V1 and factors != V1
    assert len(sessions) == 32
    assert sessions != FLEET_SECRET.encode()


def test_purpose_keys_differ_per_root_version():
    assert _ring((1, V1)).derive("sessions") != _ring((2, V2)).derive("sessions")


def test_encryption_writes_with_the_active_version():
    ring = _ring((2, V2), (1, V1))
    assert ring.active_version == 2
    assert ring.version_of(ring.encrypt("sessions", b"secret", b"")) == 2


def test_every_ring_version_stays_readable():
    """Multi-read is what makes rotation possible without downtime."""
    old = _ring((1, V1))
    blob = old.encrypt("sessions", b"secret", b"")
    rotated = _ring((2, V2), (1, V1))
    assert rotated.decrypt("sessions", blob, b"") == b"secret"
    assert rotated.decrypt("sessions", rotated.encrypt("sessions", b"secret", b""), b"") == b"secret"


def test_a_removed_version_fails_closed():
    """Removing a key must lose the plaintext, not degrade to reading it."""
    blob = _ring((1, V1)).encrypt("sessions", b"secret", b"")
    with pytest.raises(KeyRingError):
        _ring((2, V2)).decrypt("sessions", blob, b"")


def test_ciphertext_is_bound_to_its_purpose():
    ring = _ring((1, V1))
    blob = ring.encrypt("sessions", b"secret", b"")
    with pytest.raises(KeyRingError):
        ring.decrypt("totp-factors", blob, b"")


def test_ciphertext_is_bound_to_its_associated_data():
    ring = _ring((1, V1))
    blob = ring.encrypt("totp-factors", b"secret", aad=b"user-a")
    assert ring.decrypt("totp-factors", blob, aad=b"user-a") == b"secret"
    with pytest.raises(KeyRingError):
        ring.decrypt("totp-factors", blob, aad=b"user-b")
    with pytest.raises(KeyRingError):
        ring.decrypt("totp-factors", blob, b"")


def test_a_tampered_ciphertext_is_refused():
    ring = _ring((1, V1))
    blob = bytearray(ring.encrypt("sessions", b"secret", b""))
    blob[-1] ^= 0x01
    with pytest.raises(KeyRingError):
        ring.decrypt("sessions", bytes(blob), b"")


def test_truncated_ciphertext_is_refused():
    ring = _ring((1, V1))
    with pytest.raises(KeyRingError):
        ring.decrypt("sessions", ring.encrypt("sessions", b"secret", b"")[:6], b"")


def test_every_call_states_what_the_ciphertext_is_bound_to():
    """An omitted binding is a blob that verifies in any row of its purpose,
    so there is no default: a caller with nothing to bind says so."""
    ring = _ring((1, V1))
    with pytest.raises(TypeError):
        ring.encrypt("sessions", b"secret")
    with pytest.raises(TypeError):
        ring.decrypt("sessions", b"blob")


def test_the_same_plaintext_never_encrypts_to_the_same_bytes():
    """A repeated nonce under one key destroys AES-GCM confidentiality."""
    ring = _ring((1, V1))
    assert ring.encrypt("sessions", b"secret", b"") != ring.encrypt("sessions", b"secret", b"")


def test_reencrypt_moves_a_blob_to_the_active_version():
    """The re-encrypt step of active-write, multi-read, re-encrypt, remove."""
    rotated = _ring((2, V2), (1, V1))
    old = _ring((1, V1)).encrypt("sessions", b"secret", b"")
    fresh = rotated.reencrypt("sessions", old, b"")
    assert rotated.version_of(fresh) == 2
    assert _ring((2, V2)).decrypt("sessions", fresh, b"") == b"secret"


def test_reencrypt_preserves_associated_data():
    rotated = _ring((2, V2), (1, V1))
    old = _ring((1, V1)).encrypt("totp-factors", b"secret", aad=b"user-a")
    fresh = rotated.reencrypt("totp-factors", old, aad=b"user-a")
    assert rotated.decrypt("totp-factors", fresh, aad=b"user-a") == b"secret"


def test_an_empty_ring_is_refused():
    with pytest.raises(KeyRingError):
        KeyRing([])
