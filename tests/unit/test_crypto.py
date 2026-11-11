import pytest
import structlog
from cryptography.fernet import Fernet

from shoplegacy.config import load_settings
from shoplegacy.crypto import (
    ENC_PREFIX,
    ENCRYPTED_COLUMNS,
    FernetCipher,
    NullCipher,
    build_cipher,
    generate_key,
)

DB = "postgresql://shoplegacy_app:shoplegacy_app@localhost:56322/postgres"
REQUIRED = {"SHOPLEGACY_DATABASE_URL": DB, "SHOPLEGACY_JWT_SECRET": "test-secret"}


def test_round_trip() -> None:
    cipher = FernetCipher([generate_key()])
    stored = cipher.encrypt("김밥 가게 사장님")
    assert stored != "김밥 가게 사장님"
    assert cipher.decrypt(stored) == "김밥 가게 사장님"


def test_encrypt_adds_prefix() -> None:
    key = generate_key()
    stored = FernetCipher([key]).encrypt("x")
    assert ENC_PREFIX == "enc:v1:"
    assert stored.startswith(ENC_PREFIX)
    assert Fernet(key.encode()).decrypt(stored[len(ENC_PREFIX) :].encode()) == b"x"


def test_plaintext_passthrough() -> None:
    cipher = FernetCipher([generate_key()])
    assert cipher.decrypt("legacy plain value") == "legacy plain value"
    assert cipher.decrypt("") == ""


def test_key_rotation() -> None:
    old_key = generate_key()
    new_key = generate_key()
    stored = FernetCipher([old_key]).encrypt("거래처 목록")
    rotated = FernetCipher([new_key, old_key])
    assert rotated.decrypt(stored) == "거래처 목록"
    fresh = rotated.encrypt("새 값")
    assert FernetCipher([new_key]).decrypt(fresh) == "새 값"


def test_wrong_key_cannot_decrypt() -> None:
    stored = FernetCipher([generate_key()]).encrypt("x")
    with pytest.raises(ValueError):
        FernetCipher([generate_key()]).decrypt(stored)


@pytest.mark.parametrize("keys", [["not-a-key"], [], [generate_key(), "short"]])
def test_invalid_keys(keys: list[str]) -> None:
    with pytest.raises(ValueError, match="invalid SHOPLEGACY_PII_KEYS"):
        FernetCipher(keys)


def test_null_cipher_identity() -> None:
    cipher = NullCipher()
    assert cipher.encrypt("평문") == "평문"
    assert cipher.decrypt("평문") == "평문"
    assert cipher.decrypt("enc:v1:abc") == "enc:v1:abc"


def test_build_cipher_without_keys_warns() -> None:
    with structlog.testing.capture_logs() as logs:
        cipher = build_cipher(load_settings(REQUIRED))
    assert isinstance(cipher, NullCipher)
    assert any(
        e["event"] == "pii_encryption_disabled" and e["log_level"] == "warning" for e in logs
    )


def test_build_cipher_with_keys() -> None:
    key = generate_key()
    with structlog.testing.capture_logs() as logs:
        cipher = build_cipher(load_settings({**REQUIRED, "SHOPLEGACY_PII_KEYS": key}))
    assert isinstance(cipher, FernetCipher)
    assert not any(e["event"] == "pii_encryption_disabled" for e in logs)
    assert FernetCipher([key]).decrypt(cipher.encrypt("v")) == "v"


def test_encrypted_columns() -> None:
    assert ENCRYPTED_COLUMNS == {
        "shops": ("name", "owner_name", "supplier_names_json"),
        "answers": ("text",),
        "sessions": ("current_json",),
    }


def test_bytes_round_trip() -> None:
    cipher = FernetCipher([generate_key()])
    data = bytes(range(256)) * 4
    stored = cipher.encrypt_bytes(data)
    assert stored != data
    assert cipher.decrypt_bytes(stored) == data
    with pytest.raises(ValueError):
        FernetCipher([generate_key()]).decrypt_bytes(stored)
    with pytest.raises(ValueError):
        cipher.decrypt_bytes(b"not a token")


def test_null_cipher_bytes_identity() -> None:
    cipher = NullCipher()
    assert cipher.encrypt_bytes(b"\x00\x01wav") == b"\x00\x01wav"
    assert cipher.decrypt_bytes(b"\x00\x01wav") == b"\x00\x01wav"


def test_bytes_key_rotation() -> None:
    old_key = generate_key()
    new_key = generate_key()
    stored = FernetCipher([old_key]).encrypt_bytes(b"audio")
    rotated = FernetCipher([new_key, old_key])
    assert rotated.decrypt_bytes(stored) == b"audio"
    assert FernetCipher([new_key]).decrypt_bytes(rotated.encrypt_bytes(b"new")) == b"new"
