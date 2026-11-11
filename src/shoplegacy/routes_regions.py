"""Region lookup and card feedback API (mounted under ``/api``)."""

import hashlib
import hmac
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from shoplegacy import observability, regions
from shoplegacy.app_state import State, get_state
from shoplegacy.auth import ANON, PRINCIPAL
from shoplegacy.store import ParentNotFoundError

FEEDBACK_RATE_LIMIT = "10/hour"
_FINGERPRINT_CONTEXT = b"shoplegacy-feedback-fingerprint"

router = APIRouter()
StateDep = Annotated[State, Depends(get_state)]


class RegionOut(BaseModel):
    code: str
    sido: str
    sigungu: str | None


class FeedbackRequest(BaseModel):
    vote: Literal["up", "down"]


class FeedbackResponse(BaseModel):
    up: int
    down: int
    counted: bool


def _out(rows: list[regions.Region]) -> list[RegionOut]:
    return [RegionOut(code=r.code, sido=r.sido, sigungu=r.sigungu) for r in rows]


def _fingerprint(request: Request, secret: str) -> str:
    """One vote per client IP per day, keyed with a server-side secret.

    ``request.client`` is already the real client when the app sits behind a
    trusted proxy. The user agent is left out: rotating it must not buy votes.
    """
    if not secret:
        raise RuntimeError("feedback fingerprints need SHOPLEGACY_FEEDBACK_SECRET")
    host = request.client.host if request.client else ""
    key = hmac.new(secret.encode("utf-8"), _FINGERPRINT_CONTEXT, hashlib.sha256).digest()
    raw = f"{host}|{date.today().isoformat()}"
    return hmac.new(key, raw.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


@router.get("/regions")
def list_regions(sido: str | None = None) -> list[RegionOut]:
    if sido is None:
        return _out(regions.list_sido())
    return _out(regions.list_sigungu(sido))


@router.get("/regions/search")
def search_regions(q: Annotated[str, Query(min_length=1, max_length=40)]) -> list[RegionOut]:
    return _out(regions.search(q))


@router.post("/cards/{card_id}/feedback", status_code=201)
@observability.route_limit(FEEDBACK_RATE_LIMIT)
def card_feedback(
    card_id: str, body: FeedbackRequest, request: Request, state: StateDep
) -> FeedbackResponse:
    try:
        fingerprint = _fingerprint(request, state.settings.feedback_secret)
    except RuntimeError:
        raise HTTPException(status_code=503, detail="feedback_unavailable") from None
    token = PRINCIPAL.set(ANON)
    try:
        try:
            counted = state.store.add_feedback(card_id, body.vote, fingerprint)
        except ParentNotFoundError:
            raise HTTPException(status_code=404, detail="card not found") from None
        counts = state.store.feedback_counts(card_id)
    finally:
        PRINCIPAL.reset(token)
    return FeedbackResponse(up=counts.up, down=counts.down, counted=counted)
