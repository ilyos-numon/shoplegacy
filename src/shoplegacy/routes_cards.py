"""Card review and publishing API (mounted under ``/api``)."""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal, get_args

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from shoplegacy.app_state import State, get_state
from shoplegacy.auth import (
    ANON,
    PRINCIPAL,
    Principal,
    ReviewerDep,
    current_principal,
    has_role,
)
from shoplegacy.cards import (
    Card,
    CardNotFoundError,
    CardPublishedError,
    CardSources,
    CardStatus,
    CardTags,
    CardVersion,
    GateFailedError,
    InvalidTransitionError,
    PublicCard,
)
from shoplegacy.interview import ShopNotFoundError
from shoplegacy.owner_review import OwnerReviewService
from shoplegacy.question_tree import Axis
from shoplegacy.routes_owner import get_clock
from shoplegacy.store import AreaType, CardRow, OrgRequiredError, ParentNotFoundError

router = APIRouter()
StateDep = Annotated[State, Depends(get_state)]
PrincipalDep = Annotated[Principal, Depends(current_principal)]
ClockDep = Annotated[Callable[[], datetime], Depends(get_clock)]



def _card_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="card not found")


class InboxItem(BaseModel):
    card_id: str
    shop_id: str
    status: CardStatus
    tags: CardTags
    version: int
    updated_at: datetime
    owner_decision: Literal["approve", "request_changes"] | None
    owner_link_active: bool
    support_ratio: float


class Inbox(BaseModel):
    items: list[InboxItem]


class SectionsUpdate(BaseModel):
    sections: dict[Axis, Annotated[str, Field(max_length=5000)]]


class StatusUpdate(BaseModel):
    status: CardStatus
    note: Annotated[str, Field(max_length=1000)] | None = None


def _own_card(state: State, p: Principal, card_id: str) -> CardRow:
    """Card of the caller's current org; other orgs' cards look missing (404)."""
    row = state.store.get_card(card_id)
    if row is None or row.org_id != p.org_id:
        raise _card_not_found()
    return row


def _public_card(state: State, card_id: str) -> PublicCard:
    """Read ``card_id`` as an anonymous visitor: only published cards exist."""
    token = PRINCIPAL.set(ANON)
    try:
        card = state.cards.get(card_id)
        if card.status != "published":
            raise _card_not_found()
        return state.cards.public_card(card)
    except CardNotFoundError:
        raise _card_not_found() from None
    finally:
        PRINCIPAL.reset(token)


@router.get("/cards")
def list_cards(
    state: StateDep,
    status: Literal["published"] = "published",
    trade: str | None = None,
    area_type: AreaType | None = None,
    region: Annotated[str | None, Query(pattern=r"^[0-9]{2,10}$")] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=50)] = None,
) -> list[PublicCard]:
    token = PRINCIPAL.set(ANON)
    try:
        cards = state.cards.list_published(
            trade=trade, area_type=area_type, region_code=region, q=q
        )
        return [state.cards.public_card(card) for card in cards]
    finally:
        PRINCIPAL.reset(token)


@router.get("/review/inbox")
def review_inbox(state: StateDep, clock: ClockDep, p: ReviewerDep) -> Inbox:
    """Every card of the caller's org, newest first, with the fields the inbox shows."""
    owner = OwnerReviewService(state.store, state.settings, state.sms, clock)
    items: list[InboxItem] = []
    for status in get_args(CardStatus):
        for row in state.store.list_cards(status):
            if row.org_id != p.org_id:
                continue
            try:
                card = state.cards.get(row.card_id)
            except (CardNotFoundError, ShopNotFoundError):
                continue
            decision: Literal["approve", "request_changes"] | None = None
            if card.owner_decision is not None:
                found = card.owner_decision.decision
                decision = "approve" if found == "approve" else "request_changes"
            items.append(
                InboxItem(
                    card_id=card.card_id,
                    shop_id=card.shop_id,
                    status=card.status,
                    tags=card.tags,
                    version=card.version,
                    updated_at=card.updated_at,
                    owner_decision=decision,
                    owner_link_active=any(
                        link.state == "active" for link in owner.links(card.card_id)
                    ),
                    support_ratio=card.quality.supported_ratio,
                )
            )
    items.sort(key=lambda item: item.updated_at, reverse=True)
    return Inbox(items=items)


@router.get("/cards/{card_id}")
def get_card(card_id: str, state: StateDep, p: PrincipalDep) -> Card | PublicCard:
    if not p.is_anon and p.org_id is not None and has_role(p.role, "reviewer"):
        row = state.store.get_card(card_id)
        if row is not None and row.org_id == p.org_id:
            try:
                return state.cards.get(card_id)
            except CardNotFoundError:
                raise _card_not_found() from None
    return _public_card(state, card_id)


@router.put("/cards/{card_id}")
def update_card(card_id: str, body: SectionsUpdate, state: StateDep, p: ReviewerDep) -> Card:
    row = _own_card(state, p, card_id)
    if row.status == "published":
        raise HTTPException(status_code=409, detail="card is published")
    try:
        return state.cards.update_sections(card_id, body.sections)
    except (CardNotFoundError, ParentNotFoundError):
        raise _card_not_found() from None
    except OrgRequiredError:
        raise HTTPException(status_code=400, detail="org_required") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _transition(
    state: State, p: Principal, card_id: str, status: CardStatus, note: str | None
) -> Card:
    _own_card(state, p, card_id)
    try:
        return state.cards.transition(card_id, status, note, p)
    except (CardNotFoundError, ParentNotFoundError):
        raise _card_not_found() from None
    except OrgRequiredError:
        raise HTTPException(status_code=400, detail="org_required") from None
    except InvalidTransitionError as exc:
        detail = {"code": "invalid_transition", "from": exc.from_status, "to": exc.to_status}
        raise HTTPException(status_code=409, detail=detail) from None
    except GateFailedError as exc:
        gate = {"code": "gate_failed", "reasons": exc.reasons}
        raise HTTPException(status_code=409, detail=gate) from None


@router.post("/cards/{card_id}/status")
def set_card_status(card_id: str, body: StatusUpdate, state: StateDep, p: ReviewerDep) -> Card:
    return _transition(state, p, card_id, body.status, body.note)


@router.post("/cards/{card_id}/publish")
def publish_card(card_id: str, state: StateDep, p: ReviewerDep) -> Card:
    return _transition(state, p, card_id, "published", None)


@router.post("/cards/{card_id}/regenerate")
def regenerate_card(card_id: str, state: StateDep, p: ReviewerDep) -> Card:
    _own_card(state, p, card_id)
    try:
        return state.cards.regenerate(card_id)
    except (CardNotFoundError, ParentNotFoundError, ShopNotFoundError):
        raise _card_not_found() from None
    except OrgRequiredError:
        raise HTTPException(status_code=400, detail="org_required") from None
    except CardPublishedError:
        raise HTTPException(status_code=409, detail="card is published") from None


@router.get("/cards/{card_id}/versions")
def card_versions(card_id: str, state: StateDep, p: ReviewerDep) -> list[CardVersion]:
    _own_card(state, p, card_id)
    try:
        return state.cards.versions(card_id)
    except CardNotFoundError:
        raise _card_not_found() from None


@router.get("/cards/{card_id}/sources")
def card_sources(card_id: str, state: StateDep, p: ReviewerDep) -> CardSources:
    _own_card(state, p, card_id)
    try:
        return state.cards.sources(card_id)
    except (CardNotFoundError, ShopNotFoundError):
        raise _card_not_found() from None
