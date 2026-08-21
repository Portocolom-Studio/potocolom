"""Versioned root key ring: HKDF purpose keys, AES-GCM values, rotation without downtime."""

import base64
import os
import struct
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_APPLICATION = b"potocolom root key ring v1"
_KEY_BYTES = 32
_VERSION_BYTES = 2
_NONCE_BYTES = 12
_TAG_BYTES = 16
_MAX_VERSION = 2 ** (_VERSION_BYTES * 8) - 1


class KeyRingError(Exception):
    pass


def _associated_data(purpose: str, aad: bytes) -> bytes:
    name = purpose.encode()
    return struct.pack(">I", len(name)) + name + aad


def parse_root_keys(raw: str) -> list[tuple[int, bytes]]:
    """Refuses every entry it cannot fully trust, so a misread ring never becomes a weaker one."""
    entries: list[tuple[int, bytes]] = []
    seen: set[int] = set()
    for item in raw.split(","):
        version_text, separator, key_text = item.strip().partition(":")
        if not separator:
            raise KeyRingError("root key entry must be version:base64key")
        try:
            version = int(version_text)
        except ValueError:
            raise KeyRingError("root key version must be an integer") from None
        if not 1 <= version <= _MAX_VERSION:
            raise KeyRingError(f"root key version must be between 1 and {_MAX_VERSION}")
        if version in seen:
            raise KeyRingError("root key versions must be unique")
        try:
            key = base64.b64decode(key_text, validate=True)
        except ValueError:
            raise KeyRingError("root key must be base64") from None
        if len(key) != _KEY_BYTES:
            raise KeyRingError("root key must decode to 32 bytes")
        seen.add(version)
        entries.append((version, key))
    return entries


class KeyRing:
    def __init__(self, entries: list[tuple[int, bytes]]) -> None:
        if not entries:
            raise KeyRingError("a key ring needs at least one root key")
        for version, key in entries:
            if not 1 <= version <= _MAX_VERSION:
                raise KeyRingError(f"root key version must be between 1 and {_MAX_VERSION}")
            if len(key) != _KEY_BYTES:
                raise KeyRingError("root key must be 32 bytes")
        if len({version for version, _ in entries}) != len(entries):
            raise KeyRingError("root key versions must be unique")
        self._roots = dict(entries)
        self._active = entries[0][0]

    @property
    def active_version(self) -> int:
        return self._active

    @property
    def versions(self) -> frozenset[int]:
        return frozenset(self._roots)

    def _root(self, version: int | None) -> bytes:
        root = self._roots.get(self._active if version is None else version)
        if root is None:
            raise KeyRingError("root key version is not in the ring")
        return root

    def derive(self, purpose: str, version: int | None = None) -> bytes:
        kdf = HKDF(
            algorithm=hashes.SHA256(),
            length=_KEY_BYTES,
            salt=None,
            info=_APPLICATION + b"|" + purpose.encode(),
        )
        return kdf.derive(self._root(version))

    def encrypt(self, purpose: str, plaintext: bytes, aad: bytes = b"") -> bytes:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(self.derive(purpose)).encrypt(
            nonce, plaintext, _associated_data(purpose, aad)
        )
        return struct.pack(">H", self._active) + nonce + ciphertext

    def version_of(self, blob: bytes) -> int:
        if len(blob) < _VERSION_BYTES + _NONCE_BYTES + _TAG_BYTES:
            raise KeyRingError("blob is too short to hold a version, a nonce and a tag")
        return int.from_bytes(blob[:_VERSION_BYTES], "big")

    def decrypt(self, purpose: str, blob: bytes, aad: bytes = b"") -> bytes:
        key = self.derive(purpose, self.version_of(blob))
        nonce = blob[_VERSION_BYTES:_VERSION_BYTES + _NONCE_BYTES]
        try:
            return AESGCM(key).decrypt(
                nonce, blob[_VERSION_BYTES + _NONCE_BYTES:], _associated_data(purpose, aad)
            )
        except InvalidTag:
            raise KeyRingError("blob does not verify under this purpose, aad and version") from None

    def reencrypt(self, purpose: str, blob: bytes, aad: bytes = b"") -> bytes:
        return self.encrypt(purpose, self.decrypt(purpose, blob, aad), aad)


@lru_cache
def get_key_ring() -> KeyRing:
    from app.settings import get_settings

    return KeyRing(parse_root_keys(get_settings().root_keys))
