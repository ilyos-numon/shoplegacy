"""SMS delivery for owner review links."""

import re
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from shoplegacy.config import Settings

log = structlog.get_logger("shoplegacy")

_MOBILE = re.compile(r"010\d{8}")


@dataclass(frozen=True)
class SmsResult:
    provider: str
    accepted: bool
    message_id: str | None


class SmsError(RuntimeError):
    """The SMS provider rejected or failed to send a message."""


class SmsSender(Protocol):
    def send(self, to: str, body: str) -> SmsResult: ...


class LogSmsSender:
    """Logs instead of sending; only the last 4 digits of the number are logged."""

    def send(self, to: str, body: str) -> SmsResult:
        log.info("sms_log", to=to[-4:], body_length=len(body))
        return SmsResult(provider="log", accepted=True, message_id=None)


class SolapiSmsSender:
    """Sends through the SOLAPI SDK (optional extra ``sms``), imported on first send."""

    def __init__(self, api_key: str, api_secret: str, sender: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._sender = sender
        self._service: Any = None

    def _client(self) -> Any:
        if self._service is None:
            try:
                from solapi import SolapiMessageService
            except ImportError:
                raise RuntimeError("solapi not installed") from None
            self._service = SolapiMessageService(api_key=self._api_key, api_secret=self._api_secret)
        return self._service

    def send(self, to: str, body: str) -> SmsResult:
        service = self._client()
        from solapi.model import RequestMessage

        message = RequestMessage(from_=self._sender, to=to, text=body)
        try:
            response = service.send(message)
        except Exception as exc:  # the SDK raises several unrelated types
            log.warning("sms_failed", provider="solapi", to=to[-4:], error=type(exc).__name__)
            raise SmsError("sms_failed") from exc
        group_id = getattr(getattr(response, "group_info", None), "group_id", None)
        log.info("sms_sent", provider="solapi", to=to[-4:], body_length=len(body))
        return SmsResult(
            provider="solapi",
            accepted=True,
            message_id=group_id if isinstance(group_id, str) else None,
        )


def build_sms_sender(settings: Settings) -> SmsSender:
    if settings.sms_provider == "solapi":
        return SolapiSmsSender(settings.sms_api_key, settings.sms_api_secret, settings.sms_from)
    return LogSmsSender()


def normalize_phone(raw: str) -> str:
    """Digits only, Korean mobile ``010XXXXXXXX``; anything else is ``ValueError``."""
    digits = "".join(ch for ch in raw if ch in "0123456789")
    if not _MOBILE.fullmatch(digits):
        raise ValueError("invalid phone")
    return digits
