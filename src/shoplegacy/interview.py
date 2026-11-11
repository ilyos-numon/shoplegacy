"""Interview session service (six-axis version of handoff's SessionService)."""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from shoplegacy.question_tree import (
    NUMBER_PROBE,
    Axis,
    QuestionNode,
    applicable_nodes,
    load_tree,
)

if TYPE_CHECKING:
    from shoplegacy.providers import ShopLLM
    from shoplegacy.store import SessionRow, ShopRow, Store

QuestionKind = Literal["main", "follow_up"]
ContinueReason = Literal["ok", "limit", "exhausted"]
GRACE_SECONDS = 30
NUMBER_PROBE_SUFFIX = "-num"
_DIGIT_RE = re.compile(r"\d")


class Question(BaseModel):
    id: str
    axis: Axis
    text: str
    kind: QuestionKind


class AnswerEntry(BaseModel):
    session_id: str
    question_id: str
    axis: Axis
    question: str
    answer: str
    created_at: datetime
    session_number: int
    answer_id: int | None = None


@dataclass(frozen=True)
class InterviewSession:
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


@dataclass(frozen=True)
class ShopProgress:
    shop_id: str
    trade: str
    sessions_done: int
    planned: int
    next_number: int
    open_session_id: str | None
    nodes_done: int
    nodes_total: int
    can_continue: bool
    reason: ContinueReason


class ShopNotFoundError(LookupError):
    """Raised when a shop id is unknown."""


class SessionNotFoundError(LookupError):
    """Raised when a session id is unknown."""


class SessionClosedError(RuntimeError):
    """Raised when a closed session receives an answer or end request."""


class SessionLimitError(RuntimeError):
    """Raised when the shop already has the planned number of sessions."""


class TreeExhaustedError(RuntimeError):
    """Raised when every node of the tree has already been covered."""


class SessionTimeOverError(RuntimeError):
    """Raised when an answer arrives after deadline + grace."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _main_question(nodes: list[QuestionNode], index: int) -> Question:
    node = nodes[index]
    return Question(id=node.id, axis=node.axis, text=node.text, kind="main")


def _current(row: SessionRow) -> Question | None:
    if row.current_json is None:
        return None
    return Question.model_validate_json(row.current_json)


class InterviewService:
    def __init__(
        self,
        store: Store,
        llm: ShopLLM,
        clock: Callable[[], datetime] = _utc_now,
        session_seconds: int = 1200,
        planned: int = 5,
    ) -> None:
        self._store = store
        self._llm = llm
        self._clock = clock
        self._session_seconds = session_seconds
        self._planned = planned

    def _start_index(self, nodes: list[QuestionNode], last: SessionRow) -> int:
        current = _current(last)
        if current is None:
            return len(nodes)
        if current.kind == "main" and not any(
            a.question_id == current.id for a in self._store.answers(last.session_id)
        ):
            return last.node_index
        return last.node_index + 1

    def start(self, shop_id: str) -> tuple[str, Question]:
        from shoplegacy.store import SessionRow

        shop = self._store.get_shop(shop_id)
        if shop is None:
            raise ShopNotFoundError(shop_id)
        nodes = applicable_nodes(load_tree(shop.trade), shop)
        prev = self._store.list_sessions(shop_id)
        if len(prev) >= self._planned:
            raise SessionLimitError(f"shop already has {len(prev)} sessions")
        for row in prev:
            if row.ended_at is None:
                self.end(row.session_id)
        if prev:
            refreshed = self._store.get_session(prev[-1].session_id)
            index = self._start_index(nodes, refreshed if refreshed is not None else prev[-1])
        else:
            index = 0
        if index >= len(nodes):
            raise TreeExhaustedError(f"question tree {shop.trade} is exhausted")
        first = _main_question(nodes, index)
        session_id = uuid.uuid4().hex
        now = self._clock()
        self._store.insert_session(
            SessionRow(
                session_id=session_id,
                shop_id=shop_id,
                trade=shop.trade,
                number=len(prev) + 1,
                started_at=now,
                deadline=now + timedelta(seconds=self._session_seconds),
                ended_at=None,
                node_index=index,
                current_json=first.model_dump_json(),
            )
        )
        return session_id, first

    def progress(self, shop_id: str) -> ShopProgress:
        """Read-only view of where the shop's interview stands (never ends sessions)."""
        shop = self._store.get_shop(shop_id)
        if shop is None:
            raise ShopNotFoundError(shop_id)
        nodes = applicable_nodes(load_tree(shop.trade), shop)
        prev = self._store.list_sessions(shop_id)
        last = prev[-1] if prev else None
        nodes_done = self._start_index(nodes, last) if last is not None else 0
        reason: ContinueReason
        if len(prev) >= self._planned:
            reason = "limit"
        elif nodes_done >= len(nodes):
            reason = "exhausted"
        else:
            reason = "ok"
        return ShopProgress(
            shop_id=shop_id,
            trade=shop.trade,
            sessions_done=len(prev),
            planned=self._planned,
            next_number=len(prev) + 1,
            open_session_id=last.session_id if last is not None and last.ended_at is None else None,
            nodes_done=nodes_done,
            nodes_total=len(nodes),
            can_continue=reason == "ok",
            reason=reason,
        )

    def _session_nodes(self, row: SessionRow) -> list[QuestionNode]:
        shop: ShopRow | None = self._store.get_shop(row.shop_id)
        if shop is None:
            raise ShopNotFoundError(row.shop_id)
        return applicable_nodes(load_tree(row.trade), shop)

    def _get(self, session_id: str) -> SessionRow:
        row = self._store.get_session(session_id)
        if row is None:
            raise SessionNotFoundError(session_id)
        return row

    def _get_open(self, session_id: str) -> SessionRow:
        row = self._get(session_id)
        if row.ended_at is not None:
            raise SessionClosedError(session_id)
        return row

    def answer(self, session_id: str, text: str) -> Question | None:
        if text.strip() == "":
            raise ValueError("answer text must not be blank")
        row = self._get_open(session_id)
        now = self._clock()
        if now > row.deadline + timedelta(seconds=GRACE_SECONDS):
            raise SessionTimeOverError(session_id)
        nodes = self._session_nodes(row)
        current = _current(row)
        if current is None or row.node_index >= len(nodes):
            return None
        self._store.add_answer(
            AnswerEntry(
                session_id=session_id,
                question_id=current.id,
                axis=current.axis,
                question=current.text,
                answer=text,
                created_at=now,
                session_number=row.number,
            )
        )
        nxt: Question | None = None
        node = nodes[row.node_index]
        probe_id = node.id + NUMBER_PROBE_SUFFIX
        if current.kind == "main" and node.wants_number and not _DIGIT_RE.search(text):
            nxt = Question(id=probe_id, axis=node.axis, text=NUMBER_PROBE, kind="follow_up")
        elif current.kind == "main" or current.id == probe_id:
            follow = self._llm.follow_up(node.text, text, node.follow_ups)
            if follow is not None:
                nxt = Question(id=follow.id, axis=node.axis, text=follow.text, kind="follow_up")
        if nxt is None:
            row.node_index += 1
            if row.node_index < len(nodes):
                nxt = _main_question(nodes, row.node_index)
        row.current_json = nxt.model_dump_json() if nxt is not None else None
        self._store.update_session(row)
        if now >= row.deadline:
            return None
        return nxt

    def end(self, session_id: str) -> int:
        row = self._get_open(session_id)
        answered = len(self._store.answers(session_id))
        row.ended_at = self._clock()
        self._store.update_session(row)
        return answered

    def view(self, session_id: str) -> InterviewSession:
        row = self._get(session_id)
        ended = row.ended_at is not None
        if ended:
            remaining = 0
        else:
            remaining = max(0, math.ceil((row.deadline - self._clock()).total_seconds()))
        today = len(self._store.answers(session_id))
        total = len(self._store.shop_answers(row.shop_id, ended_only=True))
        if not ended:
            total += today
        return InterviewSession(
            session_id=row.session_id,
            shop_id=row.shop_id,
            trade=row.trade,
            number=row.number,
            planned=self._planned,
            status="ended" if ended else "open",
            question=_current(row),
            remaining_seconds=remaining,
            items_today=today,
            items_total=total,
        )
