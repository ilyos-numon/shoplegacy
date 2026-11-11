"""Owner review link API (mounted under ``/api``).

The reviewer issues a signed link; the shop owner opens it without logging in
and approves the card or asks for changes.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from shoplegacy.app_state import State, get_state
from shoplegacy.auth import ReviewerDep
from shoplegacy.owner_review import (
    Decision,
    LinkExpiredError,
    LinkInvalidError,
    LinkRevokedError,
    OwnerCardNotFoundError,
    OwnerCardView,
    OwnerLinkInfo,
    OwnerLinkResponse,
    OwnerReview,
    OwnerReviewService,
)
from shoplegacy.sms import SmsError
from shoplegacy.store import (
    AlreadyDecidedError,
    AlreadyRevokedError,
    OrgRequiredError,
    ParentNotFoundError,
)

router = APIRouter()
StateDep = Annotated[State, Depends(get_state)]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def get_clock() -> Callable[[], datetime]:
    """Clock for issuing links and stamping decisions (overridable in tests)."""
    return _utc_now


ClockDep = Annotated[Callable[[], datetime], Depends(get_clock)]


class OwnerLinkRequest(BaseModel):
    phone: Annotated[str, Field(max_length=40)] | None = None


class DecisionRequest(BaseModel):
    decision: Decision
    comment: Annotated[str, Field(max_length=500)] | None = None


def _service(state: State, clock: Callable[[], datetime]) -> OwnerReviewService:
    return OwnerReviewService(state.store, state.settings, state.sms, clock)


def _card_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="card not found")


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="owner_links_unavailable")


@router.post("/cards/{card_id}/owner-link", status_code=201)
def create_owner_link(
    card_id: str,
    body: OwnerLinkRequest,
    state: StateDep,
    clock: ClockDep,
    p: ReviewerDep,
) -> OwnerLinkResponse:
    row = state.store.get_card(card_id)
    if row is None or row.org_id != p.org_id:
        raise _card_not_found()
    try:
        return _service(state, clock).create_link(card_id, body.phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except (OwnerCardNotFoundError, ParentNotFoundError):
        raise _card_not_found() from None
    except OrgRequiredError:
        raise HTTPException(status_code=400, detail="org_required") from None
    except SmsError:
        raise HTTPException(status_code=502, detail="sms_failed") from None
    except RuntimeError:
        raise _unavailable() from None


@router.get("/cards/{card_id}/owner-links")
def list_owner_links(
    card_id: str, state: StateDep, clock: ClockDep, p: ReviewerDep
) -> list[OwnerLinkInfo]:
    row = state.store.get_card(card_id)
    if row is None or row.org_id != p.org_id:
        raise _card_not_found()
    return _service(state, clock).links(card_id)


@router.delete("/cards/{card_id}/owner-links/{token_id}", status_code=204)
def revoke_owner_link(
    card_id: str, token_id: str, state: StateDep, clock: ClockDep, p: ReviewerDep
) -> Response:
    row = state.store.get_card(card_id)
    if row is None or row.org_id != p.org_id:
        raise _card_not_found()
    try:
        _service(state, clock).revoke(card_id, token_id)
    except AlreadyRevokedError:
        raise HTTPException(status_code=409, detail="already_revoked") from None
    except LookupError:
        raise HTTPException(status_code=404, detail="owner link not found") from None
    return Response(status_code=204)


def _link_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, LinkRevokedError):
        return HTTPException(status_code=410, detail="link_revoked")
    if isinstance(exc, LinkExpiredError):
        return HTTPException(status_code=410, detail="link_expired")
    if isinstance(exc, LinkInvalidError):
        return HTTPException(status_code=401, detail="link_invalid")
    if isinstance(exc, (OwnerCardNotFoundError, ParentNotFoundError)):
        return _card_not_found()
    if isinstance(exc, AlreadyDecidedError):
        return HTTPException(status_code=409, detail="already_decided")
    return _unavailable()


@router.get("/owner/{token}")
def owner_view(token: str, state: StateDep, clock: ClockDep) -> OwnerCardView:
    try:
        return _service(state, clock).view(token)
    except (
        LinkExpiredError,
        LinkInvalidError,
        LinkRevokedError,
        OwnerCardNotFoundError,
        ParentNotFoundError,
        RuntimeError,
    ) as exc:
        raise _link_errors(exc) from None


@router.post("/owner/{token}/decision", status_code=201)
def owner_decision(
    token: str, body: DecisionRequest, state: StateDep, clock: ClockDep
) -> OwnerReview:
    try:
        return _service(state, clock).decide(token, body.decision, body.comment)
    except (
        LinkExpiredError,
        LinkInvalidError,
        LinkRevokedError,
        OwnerCardNotFoundError,
        ParentNotFoundError,
        RuntimeError,
    ) as exc:
        raise _link_errors(exc) from None
