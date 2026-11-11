"""Field-level encryption for personal data stored in the database."""

from collections.abc import Sequence
from typing import Protocol

import structlog
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from shoplegacy.config import Settings

ENC_PREFIX = "enc:v1:"

ENCRYPTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "shops": ("name", "owner_name", "supplier_names_json"),
    "answers": ("text",),
    "sessions": ("current_json",),
}

log = structlog.get_logger("shoplegacy")


class Cipher(Protocol):
    def encrypt(self, plaintext: str) -> str: ...

    def decrypt(self, stored: str) -> str: ...

    def encrypt_bytes(self, data: bytes) -> bytes: ...

    def decrypt_bytes(self, data: bytes) -> bytes: ...


class NullCipher:
    """Identity cipher used when no keys are configured."""

    def encrypt(self, plaintext: str) -> str:
        return plaintext

    def decrypt(self, stored: str) -> str:
        return stored

    def encrypt_bytes(self, data: bytes) -> bytes:
        return data

    def decrypt_bytes(self, data: bytes) -> bytes:
        return data


class FernetCipher:
    """Encrypts with the first key and decrypts with any key (rotation)."""

    def __init__(self, keys: Sequence[str]) -> None:
        if not keys:
            raise ValueError("invalid SHOPLEGACY_PII_KEYS")
        try:
            fernets = [Fernet(key.encode()) for key in keys]
        except (ValueError, TypeError):
            raise ValueError("invalid SHOPLEGACY_PII_KEYS") from None
        self._fernet = MultiFernet(fernets)

    def encrypt(self, plaintext: str) -> str:
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        return ENC_PREFIX + token.decode("ascii")

    def decrypt(self, stored: str) -> str:
        if not stored.startswith(ENC_PREFIX):
            return stored
        token = stored[len(ENC_PREFIX) :].encode("ascii")
        try:
            return self._fernet.decrypt(token).decode("utf-8")
        except InvalidToken:
            raise ValueError("cannot decrypt value with SHOPLEGACY_PII_KEYS") from None

    def encrypt_bytes(self, data: bytes) -> bytes:
        return self._fernet.encrypt(data)

    def decrypt_bytes(self, data: bytes) -> bytes:
        try:
            return self._fernet.decrypt(data)
        except InvalidToken:
            raise ValueError("cannot decrypt data with SHOPLEGACY_PII_KEYS") from None


def build_cipher(settings: Settings) -> Cipher:
    """Return a FernetCipher for configured keys, else NullCipher with a warning."""
    if not settings.pii_keys:
        log.warning("pii_encryption_disabled")
        return NullCipher()
    return FernetCipher(settings.pii_keys)


def generate_key() -> str:
    return Fernet.generate_key().decode()
