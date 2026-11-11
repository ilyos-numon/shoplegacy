"""Signed, expiring links that let a shop owner review their card."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from pydantic import BaseModel

from shoplegacy.config import Settings
from shoplegacy.sms import SmsSender, normalize_phone
from shoplegacy.store import OwnerLinkRow, OwnerReviewRow, Store

OWNER_SCOPE = "owner_review"
_ALGORITHM = "HS256"
_CLAIMS = ("scope", "card_id", "shop_id", "org_id", "jti", "iat", "exp")

Decision = Literal["approve", "request_changes"]
LinkState = Literal["active", "expired", "revoked"]


@dataclass(frozen=True)
class OwnerLink:
    url: str
    token: str
    token_id: str
    expires_at: datetime


class LinkInvalidError(Exception):
    """Bad signature or wrong scope."""


class LinkExpiredError(Exception):
    """The link is past its expiry time."""


class LinkRevokedError(Exception):
    """The reviewer revoked the link."""


class OwnerCardNotFoundError(LookupError):
    """The card behind a valid link no longer exists."""


class OwnerLinkResponse(BaseModel):
    url: str
    token_id: str
    expires_at: datetime
    delivery: Literal["log", "sms"]


class OwnerLinkInfo(BaseModel):
    token_id: str
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    state: LinkState


class OwnerDecision(BaseModel):
    decision: Decision
    comment: str | None
    created_at: datetime


class OwnerCardView(BaseModel):
    card_id: str
    trade: str
    status: str
    version: int
    sections: dict[str, str]
    decision: OwnerDecision | None
    expires_at: datetime


class OwnerReview(BaseModel):
    review_id: str
    card_id: str
    card_version: int
    decision: Decision
    comment: str | None
    created_at: datetime


def _secret(settings: Settings) -> str:
    if not settings.jwt_secret:
        raise RuntimeError("owner links need SHOPLEGACY_JWT_SECRET")
    return settings.jwt_secret


def issue_link(
    card_id: str, shop_id: str, org_id: str, settings: Settings, now: datetime
) -> OwnerLink:
    secret = _secret(settings)
    issued = now.astimezone(UTC).replace(microsecond=0)
    expires_at = issued + timedelta(hours=settings.owner_link_ttl_hours)
    jti = uuid.uuid4().hex
    claims = {
        "scope": OWNER_SCOPE,
        "card_id": card_id,
        "shop_id": shop_id,
        "org_id": org_id,
        "jti": jti,
        "iat": int(issued.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(claims, secret, algorithm=_ALGORITHM)
    return OwnerLink(
        url=f"{settings.public_base_url}/owner/{token}",
        token=token,
        token_id=jti,
        expires_at=expires_at,
    )


def verify_link(token: str, settings: Settings) -> dict[str, str]:
    """Claims of a valid link, every value as a string (``iat``/``exp`` in epoch seconds)."""
    secret = _secret(settings)
    try:
        claims: dict[str, Any] = jwt.decode(
            token, secret, algorithms=[_ALGORITHM], options={"require": list(_CLAIMS)}
        )
    except jwt.ExpiredSignatureError:
        raise LinkExpiredError("link_expired") from None
    except jwt.PyJWTError:
        raise LinkInvalidError("link_invalid") from None
    if claims.get("scope") != OWNER_SCOPE:
        raise LinkInvalidError("link_invalid")
    for name in ("card_id", "shop_id", "org_id", "jti"):
        if not isinstance(claims.get(name), str) or not claims[name]:
            raise LinkInvalidError("link_invalid")
    return {name: str(claims[name]) for name in _CLAIMS}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _link_state(row: OwnerLinkRow, now: datetime) -> LinkState:
    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at < now:
        return "expired"
    return "active"


class OwnerReviewService:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        sms: SmsSender,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._settings = settings
        self._sms = sms
        self._clock = clock

    def create_link(self, card_id: str, phone: str | None) -> OwnerLinkResponse:
        """Issue a link for a card of the caller's org (checked by the caller).

        ``phone`` is normalised first (``ValueError("invalid phone")``) and, when
        given, receives the link by SMS.
        """
        to = normalize_phone(phone) if phone is not None else None
        card = self._store.get_card(card_id)
        if card is None:
            raise OwnerCardNotFoundError(card_id)
        link = issue_link(card.card_id, card.shop_id, card.org_id, self._settings, self._clock())
        self._store.insert_owner_link(
            OwnerLinkRow(
                token_id=link.token_id,
                org_id=card.org_id,
                card_id=card.card_id,
                issued_at=link.expires_at - timedelta(hours=self._settings.owner_link_ttl_hours),
                expires_at=link.expires_at,
                revoked_at=None,
                revoked_by=None,
            )
        )
        delivery: Literal["log", "sms"] = "log"
        if to is not None:
            result = self._sms.send(to, f"가게 이야기 카드를 확인해 주세요: {link.url}")
            delivery = "log" if result.provider == "log" else "sms"
        self._store.add_audit(
            "card.owner_link",
            "cards",
            card.card_id,
            {
                "token_id": link.token_id,
                "expires_at": link.expires_at.isoformat(),
                "delivery": delivery,
            },
        )
        return OwnerLinkResponse(
            url=link.url, token_id=link.token_id, expires_at=link.expires_at, delivery=delivery
        )

    def links(self, card_id: str) -> list[OwnerLinkInfo]:
        """Links issued for a card of the caller's org, newest first."""
        now = self._clock()
        return [
            OwnerLinkInfo(
                token_id=row.token_id,
                issued_at=row.issued_at,
                expires_at=row.expires_at,
                revoked_at=row.revoked_at,
                state=_link_state(row, now),
            )
            for row in self._store.list_owner_links(card_id)
        ]

    def revoke(self, card_id: str, token_id: str) -> None:
        """Revoke a link of the card: ``LookupError`` if there is no such link,
        ``AlreadyRevokedError`` (from the store) if it was revoked before."""
        self._store.revoke_owner_link(card_id, token_id)
        self._store.add_audit("card.owner_link.revoke", "cards", card_id, {"token_id": token_id})

    def _claims_and_view(self, token: str) -> tuple[dict[str, str], dict[str, Any]]:
        claims = verify_link(token, self._settings)
        # Links issued before links were recorded have no row ("unknown") and stay valid.
        state = self._store.owner_link_state(claims["jti"])
        if state == "revoked":
            raise LinkRevokedError("link_revoked")
        if state == "expired":
            raise LinkExpiredError("link_expired")
        view = self._store.owner_card_view(claims["card_id"])
        if view is None:
            raise OwnerCardNotFoundError(claims["card_id"])
        if str(view["shop_id"]) != claims["shop_id"] or str(view["org_id"]) != claims["org_id"]:
            raise LinkInvalidError("link_invalid")
        return claims, view

    def view(self, token: str) -> OwnerCardView:
        claims, view = self._claims_and_view(token)
        decision = None
        if view.get("decision") is not None:
            decision = OwnerDecision(
                decision=view["decision"],
                comment=view.get("comment"),
                created_at=view["decided_at"],
            )
        return OwnerCardView(
            card_id=view["card_id"],
            trade=view["trade"],
            status=view["status"],
            version=int(view["version"]),
            sections={str(k): str(v) for k, v in (view.get("sections") or {}).items()},
            decision=decision,
            expires_at=datetime.fromtimestamp(int(claims["exp"]), UTC),
        )

    def decide(self, token: str, decision: Decision, comment: str | None) -> OwnerReview:
        """Record the owner's decision on the card's current version.

        A second decision with the same link on the same version raises
        ``AlreadyDecidedError`` (from the store).
        """
        claims, view = self._claims_and_view(token)

        review_id = str(uuid.uuid4())
        version = int(view["version"])
        created_at = self._clock()
        self._store.add_owner_review(
            OwnerReviewRow(
                review_id=review_id,
                org_id=claims["org_id"],
                shop_id=claims["shop_id"],
                card_id=claims["card_id"],
                card_version=version,
                token_id=claims["jti"],
                decision=decision,
                comment=comment,
                created_at=created_at,
            )
        )
        return OwnerReview(
            review_id=review_id,
            card_id=claims["card_id"],
            card_version=version,
            decision=decision,
            comment=comment,
            created_at=created_at,
        )
