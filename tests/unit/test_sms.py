import sys

import pytest
import structlog

from shoplegacy.config import load_settings
from shoplegacy.sms import (
    LogSmsSender,
    SolapiSmsSender,
    build_sms_sender,
    normalize_phone,
)

REQUIRED = {"SHOPLEGACY_DATABASE_URL": "postgresql://x/y", "SHOPLEGACY_JWT_SECRET": "s"}


def test_log_sender_logs_only_last_four_digits() -> None:
    with structlog.testing.capture_logs() as logs:
        result = LogSmsSender().send("01012345678", "링크 본문")
    assert result.provider == "log" and result.accepted and result.message_id is None
    entry = next(e for e in logs if e["event"] == "sms_log")
    assert entry["to"] == "5678"
    assert entry["body_length"] == len("링크 본문")
    assert "01012345678" not in repr(logs)
    assert "링크 본문" not in repr(logs)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("010-1234-5678", "01012345678"), (" 010 1234 5678 ", "01012345678")],
)
def test_normalize_phone_accepts_mobile(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "02-123-4567", "011-1234-5678", "010-1234-567", "010123456789", "전화"]
)
def test_normalize_phone_rejects(raw: str) -> None:
    with pytest.raises(ValueError, match="invalid phone"):
        normalize_phone(raw)


def test_solapi_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "solapi", None)
    sender = SolapiSmsSender("key", "secret", "01000000000")
    with pytest.raises(RuntimeError, match="solapi not installed"):
        sender.send("01012345678", "본문")


def test_build_sms_sender_by_provider() -> None:
    assert isinstance(build_sms_sender(load_settings(REQUIRED)), LogSmsSender)
    solapi = load_settings(
        {
            **REQUIRED,
            "SHOPLEGACY_SMS_PROVIDER": "solapi",
            "SHOPLEGACY_SMS_API_KEY": "k",
            "SHOPLEGACY_SMS_API_SECRET": "s",
            "SHOPLEGACY_SMS_FROM": "01000000000",
        }
    )
    assert isinstance(build_sms_sender(solapi), SolapiSmsSender)
