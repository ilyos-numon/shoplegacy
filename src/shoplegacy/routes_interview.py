"""Shop registration and interview session API (mounted under ``/api``)."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from shoplegacy import regions, routes_billing
from shoplegacy.app_state import State, get_state
from shoplegacy.auth import InterviewerDep, Principal
from shoplegacy.cards import CardStatus
from shoplegacy.interview import (
    ContinueReason,
    InterviewSession,
    Question,
    SessionClosedError,
    SessionLimitError,
    SessionNotFoundError,
    SessionTimeOverError,
    ShopNotFoundError,
    ShopProgress,
    TreeExhaustedError,
)
from shoplegacy.question_tree import UnknownTradeError, load_tree
from shoplegacy.store import AreaType, OrgRequiredError, ParentNotFoundError, ShopRow

router = APIRouter()
StateDep = Annotated[State, Depends(get_state)]


class ShopCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    owner_name: str = Field(min_length=1, max_length=50)
    trade: str = Field(min_length=1, max_length=40)
    area_type: AreaType
    years: int = Field(ge=0, le=100)
    closed_year: int = Field(ge=1900, le=2100)
    supplier_names: list[str] = Field(default_factory=list, max_length=20)
    region_code: str | None = Field(default=None, max_length=10)


class ShopResponse(BaseModel):
    """Public shop view: never includes the shop name, owner name or suppliers."""

    shop_id: str
    trade: str
    area_type: AreaType
    years: int
    closed_year: int
    region: str | None  # "시도 시군구"


class ShopSummary(BaseModel):
    """Shop list/progress view: never includes the shop name, owner name or suppliers."""

    shop_id: str
    trade: str
    area_type: AreaType
    years: int
    closed_year: int
    created_at: datetime
    sessions_done: int
    planned: int
    next_number: int
    open_session_id: str | None
    nodes_done: int
    nodes_total: int
    can_continue: bool
    reason: ContinueReason
    card_id: str | None
    card_status: CardStatus | None


class SessionCreate(BaseModel):
    shop_id: str


class SessionStarted(BaseModel):
    session_id: str
    question: Question
    number: int
    planned: int
    remaining_seconds: int


class AnswerRequest(BaseModel):
    text: str = Field(max_length=10000)


class AnswerResponse(BaseModel):
    next_question: Question | None
    remaining_seconds: int
    items_today: int
    items_total: int


class EndResponse(BaseModel):
    session_id: str
    answered: int
    card_id: str


class SessionResponse(BaseModel):
    session_id: str
    shop_id: str
    trade: str
    number: int
    planned: int
    status: Literal["open", "ended"]
    question: Question | None
    remaining_seconds: int
    items_today: int
    items_total: int


def _shop_response(row: ShopRow) -> ShopResponse:
    return ShopResponse(
        shop_id=row.shop_id,
        trade=row.trade,
        area_type=row.area_type,
        years=row.years,
        closed_year=row.closed_year,
        region=_region_label(row),
    )


def _region_label(row: ShopRow) -> str | None:
    if not row.region_sido:
        return None
    return f"{row.region_sido} {row.region_sigungu}" if row.region_sigungu else row.region_sido


def _shop_summary(state: State, row: ShopRow) -> ShopSummary:
    progress: ShopProgress = state.interview.progress(row.shop_id)
    card = state.store.card_for_shop(row.shop_id)
    card_status: CardStatus | None = None
    if card is not None:
        card_status = "published" if card.status == "published" else "draft"
    return ShopSummary(
        shop_id=row.shop_id,
        trade=row.trade,
        area_type=row.area_type,
        years=row.years,
        closed_year=row.closed_year,
        created_at=row.created_at,
        sessions_done=progress.sessions_done,
        planned=progress.planned,
        next_number=progress.next_number,
        open_session_id=progress.open_session_id,
        nodes_done=progress.nodes_done,
        nodes_total=progress.nodes_total,
        can_continue=progress.can_continue,
        reason=progress.reason,
        card_id=card.card_id if card is not None else None,
        card_status=card_status,
    )


def _own_shop(state: State, p: Principal, shop_id: str) -> ShopRow:
    """Shop of the caller's current org; other orgs' shops look missing (404)."""
    row = state.store.get_shop(shop_id)
    if row is None or row.org_id != p.org_id:
        raise HTTPException(status_code=404, detail="shop not found")
    return row


def _own_session(state: State, p: Principal, session_id: str) -> None:
    row = state.store.get_session(session_id)
    if row is None or row.org_id != p.org_id:
        raise HTTPException(status_code=404, detail="session not found")


def _session_response(view: InterviewSession) -> SessionResponse:
    return SessionResponse(
        session_id=view.session_id,
        shop_id=view.shop_id,
        trade=view.trade,
        number=view.number,
        planned=view.planned,
        status=view.status,
        question=view.question,
        remaining_seconds=view.remaining_seconds,
        items_today=view.items_today,
        items_total=view.items_total,
    )


@router.post("/shops", status_code=201)
def create_shop(body: ShopCreate, state: StateDep, p: InterviewerDep) -> ShopResponse:
    try:
        load_tree(body.trade)
    except UnknownTradeError:
        raise HTTPException(status_code=422, detail="unknown trade") from None
    region: regions.Region | None = None
    if body.region_code is not None:
        region = regions.find(body.region_code)
        if region is None:
            raise HTTPException(status_code=422, detail="unknown region")
    if p.org_id:
        routes_billing.enforce_limit(state.store, p.org_id, "shops")
    row = ShopRow(
        shop_id=uuid.uuid4().hex,
        name=body.name.strip(),
        owner_name=body.owner_name.strip(),
        trade=body.trade,
        area_type=body.area_type,
        years=body.years,
        closed_year=body.closed_year,
        supplier_names=[s.strip() for s in body.supplier_names if s.strip()],
        created_at=datetime.now(UTC),
        org_id=p.org_id or "",
        region_code=region.code if region else None,
        region_sido=region.sido if region else None,
        region_sigungu=region.sigungu if region else None,
    )
    try:
        # insert_shop fills org_id and created_by from PRINCIPAL (set by the dependency).
        state.store.insert_shop(row)
    except OrgRequiredError:
        raise HTTPException(status_code=400, detail="org_required") from None
    return _shop_response(row)


@router.get("/shops")
def list_shops(state: StateDep, p: InterviewerDep) -> list[ShopSummary]:
    rows = [row for row in state.store.list_shops() if row.org_id == p.org_id]
    return [_shop_summary(state, row) for row in reversed(rows)]


@router.get("/shops/{shop_id}/progress")
def get_shop_progress(shop_id: str, state: StateDep, p: InterviewerDep) -> ShopSummary:
    return _shop_summary(state, _own_shop(state, p, shop_id))


@router.get("/shops/{shop_id}")
def get_shop(shop_id: str, state: StateDep, p: InterviewerDep) -> ShopResponse:
    return _shop_response(_own_shop(state, p, shop_id))


@router.post("/sessions", status_code=201)
def start_session(body: SessionCreate, state: StateDep, p: InterviewerDep) -> SessionStarted:
    _own_shop(state, p, body.shop_id)
    try:
        session_id, question = state.interview.start(body.shop_id)
    except (ShopNotFoundError, ParentNotFoundError):
        raise HTTPException(status_code=404, detail="shop not found") from None
    except SessionLimitError:
        raise HTTPException(status_code=409, detail="session limit reached") from None
    except TreeExhaustedError:
        raise HTTPException(status_code=409, detail="question tree exhausted") from None
    view = state.interview.view(session_id)
    return SessionStarted(
        session_id=session_id,
        question=question,
        number=view.number,
        planned=view.planned,
        remaining_seconds=view.remaining_seconds,
    )


@router.post("/sessions/{session_id}/answer")
def answer_session(
    session_id: str, body: AnswerRequest, state: StateDep, p: InterviewerDep
) -> AnswerResponse:
    if body.text.strip() == "":
        raise HTTPException(status_code=422, detail="answer text must not be blank")
    _own_session(state, p, session_id)
    try:
        nxt = state.interview.answer(session_id, body.text)
    except (SessionNotFoundError, ParentNotFoundError):
        raise HTTPException(status_code=404, detail="session not found") from None
    except SessionClosedError:
        raise HTTPException(status_code=409, detail="session closed") from None
    except SessionTimeOverError:
        raise HTTPException(status_code=410, detail="session time over") from None
    view = state.interview.view(session_id)
    return AnswerResponse(
        next_question=nxt,
        remaining_seconds=view.remaining_seconds,
        items_today=view.items_today,
        items_total=view.items_total,
    )


@router.post("/sessions/{session_id}/end")
def end_session(session_id: str, state: StateDep, p: InterviewerDep) -> EndResponse:
    _own_session(state, p, session_id)
    try:
        answered = state.interview.end(session_id)
    except (SessionNotFoundError, ParentNotFoundError):
        raise HTTPException(status_code=404, detail="session not found") from None
    except SessionClosedError:
        raise HTTPException(status_code=409, detail="session closed") from None
    card = state.cards.rebuild_draft(state.interview.view(session_id).shop_id)
    return EndResponse(session_id=session_id, answered=answered, card_id=card.card_id)


@router.get("/sessions/{session_id}")
def get_session(session_id: str, state: StateDep, p: InterviewerDep) -> SessionResponse:
    _own_session(state, p, session_id)
    try:
        view = state.interview.view(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="session not found") from None
    return _session_response(view)
