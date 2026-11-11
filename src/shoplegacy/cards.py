"""Shop cards: masked draft generation, review edits and publishing."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol, get_args

from pydantic import BaseModel, TypeAdapter

from shoplegacy.evidence import (
    Evidence,
    EvidenceBuilder,
    Quality,
    build_evidence,
    merge_reviewer_sentences,
    reviewer_sentence_count,
    split_reviewer_sentences,
    supported_ratio,
)
from shoplegacy.interview import ShopNotFoundError
from shoplegacy.masking import MaskMap, mask
from shoplegacy.question_tree import Axis
from shoplegacy.store import (
    PRINCIPAL,
    AreaType,
    CardRow,
    CardVersionRow,
    FeedbackCounts,
    OwnerReviewRow,
    Principal,
    ShopRow,
)

if TYPE_CHECKING:
    from shoplegacy.providers import ShopLLM
    from shoplegacy.store import Store

CardStatus = Literal["draft", "on_hold", "rejected", "published", "unpublished"]
CARD_STATUSES: tuple[CardStatus, ...] = get_args(CardStatus)
CARD_AXES: tuple[Axis, ...] = ("location", "suppliers", "menu", "pricing", "customers", "failures")
# Allowed card status transitions; anything else is a 409.
TRANSITIONS: dict[CardStatus, frozenset[CardStatus]] = {
    "draft": frozenset({"on_hold", "rejected", "published"}),
    "on_hold": frozenset({"draft", "rejected", "published"}),
    "rejected": frozenset({"draft"}),
    "published": frozenset({"unpublished"}),
    "unpublished": frozenset({"draft", "published"}),
}
_EVIDENCE = TypeAdapter(dict[Axis, list[Evidence]])


class GateSettings(Protocol):
    """The quality-gate knobs of ``config.Settings``."""

    @property
    def min_supported_ratio(self) -> float: ...
    @property
    def require_all_axes(self) -> bool: ...
    @property
    def require_consent(self) -> bool: ...
    @property
    def require_owner_approval(self) -> bool: ...


@dataclass(frozen=True)
class DefaultGate:
    """Same defaults as ``config.Settings``, used when ``CardService`` gets no settings."""

    min_supported_ratio: float = 0.8
    require_all_axes: bool = False
    require_consent: bool = True
    require_owner_approval: bool = False


class CardTags(BaseModel):
    trade: str
    area_type: AreaType
    years: int
    closed_year: int
    region: str | None = None  # "<si/do> <si/gun/gu>"


class OwnerDecision(BaseModel):
    decision: str
    comment: str | None
    created_at: datetime


class Card(BaseModel):
    card_id: str
    shop_id: str
    status: CardStatus
    tags: CardTags
    sections: dict[Axis, str]
    warnings: list[str]
    sessions: int
    updated_at: datetime
    evidence: dict[Axis, list[Evidence]]
    quality: Quality
    version: int
    note: str | None
    llm_provider: str
    owner_decision: OwnerDecision | None


class PublicCard(BaseModel):
    """A published card as anonymous readers and other orgs see it: no quality gate,
    warnings, review note or LLM provider."""

    card_id: str
    shop_id: str
    status: CardStatus
    sections: dict[Axis, str]
    tags: CardTags
    evidence: dict[Axis, list[Evidence]]
    version: int
    updated_at: datetime
    sessions: int
    feedback: FeedbackCounts


class CardVersion(BaseModel):
    version: int
    status: CardStatus
    created_at: datetime
    sections: dict[Axis, str]


class SourceAnswer(BaseModel):
    answer_id: int | None
    session_number: int
    axis: Axis
    question: str
    answer_masked: str


class CardSources(BaseModel):
    answers: list[SourceAnswer]


class CardNotFoundError(LookupError):
    """No card with the given id."""


class CardNotCleanError(RuntimeError):
    """The card still has masking warnings and cannot be published."""

    def __init__(self, warnings: list[str]) -> None:
        super().__init__("card has unresolved warnings")
        self.warnings = warnings


class GateFailedError(CardNotCleanError):
    """The quality / consent gate refused a transition to ``published``."""

    def __init__(self, reasons: list[str], warnings: list[str]) -> None:
        super().__init__(warnings)
        self.args = ("quality gate failed",)
        self.reasons = reasons


class InvalidTransitionError(ValueError):
    """The status change is not in the transition table."""

    def __init__(self, from_status: CardStatus, to_status: CardStatus) -> None:
        super().__init__(f"invalid transition {from_status} -> {to_status}")
        self.from_status = from_status
        self.to_status = to_status


class CardPublishedError(RuntimeError):
    """The card is published; its sections cannot be regenerated."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _mask_map(shop: ShopRow) -> MaskMap:
    return MaskMap(
        shop_name=shop.name, owner_name=shop.owner_name, supplier_names=shop.supplier_names
    )


def _region(sido: str | None, sigungu: str | None) -> str | None:
    return " ".join(part for part in (sido, sigungu) if part) or None


def _tags(shop: ShopRow) -> CardTags:
    return CardTags(
        trade=shop.trade,
        area_type=shop.area_type,
        years=shop.years,
        closed_year=shop.closed_year,
        region=_region(shop.region_sido, shop.region_sigungu),
    )


def _status(value: str) -> CardStatus:
    for status in CARD_STATUSES:
        if status == value:
            return status
    return "draft"


def _sections(sections_json: str) -> dict[Axis, str]:
    raw: dict[str, str] = json.loads(sections_json)
    return {axis: raw.get(axis, "") for axis in CARD_AXES}


def _evidence(evidence_json: str) -> dict[Axis, list[Evidence]]:
    parsed = _EVIDENCE.validate_json(evidence_json or "{}")
    return {axis: parsed.get(axis, []) for axis in CARD_AXES}


def _draft_sections(row: CardRow) -> dict[Axis, str] | None:
    """The stored LLM draft; None for cards drafted before it was kept."""
    raw: dict[str, str] = json.loads(row.draft_sections_json or "{}")
    if not raw:
        return None
    return {axis: raw.get(axis, "") for axis in CARD_AXES}


def _quality(
    row: CardRow,
    consent_kinds: set[str],
    settings: GateSettings,
    owner_review: OwnerReviewRow | None = None,
) -> Quality:
    """Pre-publication quality gate: reasons empty means the card may be published."""
    sections = _sections(row.sections_json)
    evidence = _evidence(row.evidence_json)
    missing: list[Axis] = [axis for axis in CARD_AXES if not sections[axis].strip()]
    present: list[Axis] = [axis for axis in CARD_AXES if axis not in missing]
    reasons: list[str] = []
    if json.loads(row.warnings_json):
        reasons.append("warnings")
    if settings.require_all_axes:
        reasons.extend(f"missing_axis:{axis}" for axis in missing)
    for axis in present:
        ratio = supported_ratio(evidence, [axis])
        if ratio < settings.min_supported_ratio:
            reasons.append(f"low_support:{axis}:{ratio:.2f}")
    if settings.require_consent and "publication" not in consent_kinds:
        reasons.append("consent_missing:publication")
    decision = owner_review.decision if owner_review is not None else None
    if settings.require_owner_approval and decision != "approve":
        reasons.append("owner_approval_missing")
    flags: list[str] = []
    written = reviewer_sentence_count(evidence)
    if written:
        flags.append(f"reviewer_sentences:{written}")
    if row.llm_provider == "fallback":
        flags.append("llm_fallback")
    elif row.llm_provider == "quota":
        flags.append("llm_quota_exceeded")
    if decision == "request_changes":
        flags.append("owner_changes_requested")
    return Quality(
        supported_ratio=supported_ratio(evidence, present),
        missing_axes=missing,
        gate_ok=not reasons,
        reasons=reasons,
        flags=flags,
    )


class CardService:
    def __init__(
        self,
        store: Store,
        llm: ShopLLM,
        clock: Callable[[], datetime] = _utc_now,
        evidence_builder: EvidenceBuilder = build_evidence,
        settings: GateSettings | None = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._clock = clock
        self._evidence_builder = evidence_builder
        self._settings: GateSettings = settings if settings is not None else DefaultGate()

    def _shop(self, shop_id: str) -> ShopRow:
        shop = self._store.get_shop(shop_id)
        if shop is None:
            raise ShopNotFoundError(shop_id)
        return shop

    def _card_tags(self, shop_id: str, card_id: str) -> CardTags:
        # Same-org callers read the shop; anon and other orgs only see the tags of
        # a shop with a published card (public.public_card_tags).
        if not PRINCIPAL.get().is_anon:
            shop = self._store.get_shop(shop_id)
            if shop is not None:
                return _tags(shop)
        public = self._store.public_card_tags(shop_id)
        if public is None:
            raise ShopNotFoundError(shop_id)
        trade, area_type, years, closed_year, sigungu = public
        return CardTags(
            trade=trade,
            area_type=area_type,
            years=years,
            closed_year=closed_year,
            region=self._public_region(card_id, sigungu),
        )

    def _public_region(self, card_id: str, sigungu: str | None) -> str | None:
        # The tags give the si/gun/gu; the si/do comes from the published region.
        found = self._store.public_card_region(card_id)
        if found is None:
            return None
        return _region(found[1], sigungu)

    def _owner_review(self, row: CardRow) -> OwnerReviewRow | None:
        # Owner reviews are org-private (RLS), like consents.
        p = PRINCIPAL.get()
        if p.is_anon or p.org_id != row.org_id:
            return None
        return self._store.latest_owner_review(row.card_id)

    def _consent_kinds(self, row: CardRow) -> set[str]:
        # Consents are org-private (RLS): anon and other orgs cannot read them.
        p = PRINCIPAL.get()
        if p.is_anon or p.org_id != row.org_id or not self._settings.require_consent:
            return set()
        return self._store.active_consent_kinds(row.shop_id)

    def _card(self, row: CardRow) -> Card:
        review = self._owner_review(row)
        return Card(
            card_id=row.card_id,
            shop_id=row.shop_id,
            status=_status(row.status),
            tags=self._card_tags(row.shop_id, row.card_id),
            sections=_sections(row.sections_json),
            warnings=list(json.loads(row.warnings_json)),
            sessions=row.sessions,
            updated_at=row.updated_at,
            evidence=_evidence(row.evidence_json),
            quality=_quality(row, self._consent_kinds(row), self._settings, review),
            version=row.version,
            note=row.note,
            llm_provider=row.llm_provider,
            owner_decision=(
                OwnerDecision(
                    decision=review.decision, comment=review.comment, created_at=review.created_at
                )
                if review is not None
                else None
            ),
        )

    def public_card(self, card: Card) -> PublicCard:
        """The anonymous view of ``card`` with its feedback counts."""
        return PublicCard(
            card_id=card.card_id,
            shop_id=card.shop_id,
            status=card.status,
            sections=card.sections,
            tags=card.tags,
            evidence=card.evidence,
            version=card.version,
            updated_at=card.updated_at,
            sessions=card.sessions,
            feedback=self._store.feedback_counts(card.card_id),
        )

    def _evidence_json(
        self,
        shop_id: str,
        sections: dict[Axis, str],
        mask_map: MaskMap,
        draft_sections: dict[Axis, str] | None = None,
    ) -> str:
        """Evidence JSON; sentences missing from ``draft_sections`` are reviewer-written."""
        answers = self._store.shop_answers(shop_id, ended_only=True)
        if draft_sections is None:
            built = self._evidence_builder(self._llm, sections, answers, mask_map)
        else:
            drafted, written = split_reviewer_sentences(sections, draft_sections)
            checked = self._evidence_builder(self._llm, drafted, answers, mask_map)
            built = merge_reviewer_sentences(checked, sections, written)
        return _EVIDENCE.dump_json({axis: built.get(axis, []) for axis in CARD_AXES}).decode()

    def _row(self, card_id: str) -> CardRow:
        row = self._store.get_card(card_id)
        if row is None:
            raise CardNotFoundError(card_id)
        return row

    def _masked_sections(
        self, sections: dict[Axis, str], mask_map: MaskMap
    ) -> tuple[dict[Axis, str], list[str]]:
        out: dict[Axis, str] = {}
        warnings: list[str] = []
        for axis in CARD_AXES:
            result = mask(sections.get(axis, ""), mask_map)
            out[axis] = result.text
            warnings.extend(w for w in result.warnings if w not in warnings)
        return out, warnings

    def rebuild_draft(self, shop_id: str) -> Card:
        shop = self._shop(shop_id)
        mask_map = _mask_map(shop)
        sessions = sum(1 for s in self._store.list_sessions(shop_id) if s.ended_at is not None)
        existing = self._store.card_for_shop(shop_id)
        now = self._clock()
        if existing is not None and existing.status in ("published", "unpublished"):
            existing.sessions = sessions
            existing.updated_at = now
            self._store.upsert_card(existing)
            return self._card(existing)
        answers_by_axis: dict[Axis, list[str]] = {axis: [] for axis in CARD_AXES}
        for entry in self._store.shop_answers(shop_id, ended_only=True):
            answers_by_axis[entry.axis].append(mask(entry.answer, mask_map).text)
        drafted = self._llm.draft_sections(answers_by_axis, _tags(shop))
        provider = self._llm.last_provider()
        sections, warnings = self._masked_sections(drafted, mask_map)
        row = CardRow(
            card_id=existing.card_id if existing is not None else uuid.uuid4().hex,
            shop_id=shop_id,
            status="draft",
            sections_json=json.dumps(sections, ensure_ascii=False),
            warnings_json=json.dumps(warnings, ensure_ascii=False),
            sessions=sessions,
            updated_at=now,
            evidence_json=self._evidence_json(shop_id, sections, mask_map),
            version=existing.version if existing is not None else 0,
            note=existing.note if existing is not None else None,
            llm_provider=provider,
            draft_sections_json=json.dumps(sections, ensure_ascii=False),
        )
        self._store.upsert_card(row)
        return self._card(row)

    def regenerate(self, card_id: str) -> Card:
        """Redraft sections and evidence from the answers; status back to draft, version kept."""
        row = self._row(card_id)
        if row.status == "published":
            raise CardPublishedError(card_id)
        if row.status == "unpublished":
            # rebuild_draft leaves unpublished cards alone; regenerating is explicit.
            row.status = "draft"
            self._store.upsert_card(row)
        return self.rebuild_draft(row.shop_id)

    def get(self, card_id: str) -> Card:
        return self._card(self._row(card_id))

    def update_sections(self, card_id: str, sections: dict[Axis, str]) -> Card:
        missing = [axis for axis in CARD_AXES if axis not in sections]
        if missing:
            raise ValueError(f"missing sections: {', '.join(missing)}")
        row = self._row(card_id)
        mask_map = _mask_map(self._shop(row.shop_id))
        masked, warnings = self._masked_sections(sections, mask_map)
        row.sections_json = json.dumps(masked, ensure_ascii=False)
        row.warnings_json = json.dumps(warnings, ensure_ascii=False)
        row.evidence_json = self._evidence_json(
            row.shop_id, masked, mask_map, _draft_sections(row)
        )
        row.updated_at = self._clock()
        self._store.upsert_card(row)
        return self._card(row)

    def transition(
        self, card_id: str, status: CardStatus, note: str | None, principal: Principal
    ) -> Card:
        row = self._row(card_id)
        current = _status(row.status)
        if status not in TRANSITIONS[current]:
            raise InvalidTransitionError(current, status)
        now = self._clock()
        if status == "published":
            quality = _quality(
                row, self._consent_kinds(row), self._settings, self._owner_review(row)
            )
            if not quality.gate_ok:
                raise GateFailedError(quality.reasons, list(json.loads(row.warnings_json)))
            row.version += 1
        row.status = status
        row.note = note
        row.updated_at = now
        self._store.upsert_card(row)
        if status == "published":
            self._store.add_card_version(
                CardVersionRow(
                    card_id=row.card_id,
                    version=row.version,
                    org_id=row.org_id,
                    status=status,
                    sections_json=row.sections_json,
                    evidence_json=row.evidence_json,
                    created_by=principal.user_id or None,
                    created_at=now,
                )
            )
            self._store.add_audit(
                "card.publish",
                "cards",
                row.card_id,
                {"version": row.version, "from": current, "note": note},
            )
        return self._card(row)

    def publish(self, card_id: str) -> Card:
        """Alias of ``transition(card_id, "published", None, <current principal>)``."""
        return self.transition(card_id, "published", None, PRINCIPAL.get())

    def versions(self, card_id: str) -> list[CardVersion]:
        self._row(card_id)
        return [
            CardVersion(
                version=v.version,
                status=_status(v.status),
                created_at=v.created_at,
                sections=_sections(v.sections_json),
            )
            for v in self._store.list_card_versions(card_id)
        ]

    def sources(self, card_id: str) -> CardSources:
        """The ended-session answers a card is drafted from, masked like the card."""
        row = self._row(card_id)
        mask_map = _mask_map(self._shop(row.shop_id))
        return CardSources(
            answers=[
                SourceAnswer(
                    answer_id=entry.answer_id,
                    session_number=entry.session_number,
                    axis=entry.axis,
                    question=mask(entry.question, mask_map).text,
                    answer_masked=mask(entry.answer, mask_map).text,
                )
                for entry in self._store.shop_answers(row.shop_id, ended_only=True)
            ]
        )

    def list_published(
        self,
        trade: str | None = None,
        area_type: AreaType | None = None,
        region_code: str | None = None,
        q: str | None = None,
    ) -> list[Card]:
        rows = self._store.list_cards("published", region_code=region_code, q=q)
        cards = [self._card(row) for row in rows]
        return [
            card
            for card in cards
            if (trade is None or card.tags.trade == trade)
            and (area_type is None or card.tags.area_type == area_type)
        ]
