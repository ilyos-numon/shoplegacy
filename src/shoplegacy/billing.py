"""Plans, usage limits and LLM call metering."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import psycopg
import structlog
from handoff.contracts import ClaimVerdict

from shoplegacy.providers import FakeShopLLM, ShopLLM
from shoplegacy.question_tree import Axis, FollowUp
from shoplegacy.store import PRINCIPAL, LlmUsageRow, Store

if TYPE_CHECKING:
    from shoplegacy.cards import CardTags

Plan = Literal["free", "pilot", "org"]
LimitResource = Literal["shops", "members", "llm_calls"]
UsageKind = Literal["follow_up", "draft", "verify"]

T = TypeVar("T")

log = structlog.get_logger("shoplegacy")


@dataclass(frozen=True)
class PlanLimits:
    shops: int
    llm_calls_month: int
    members: int


PLANS: dict[Plan, PlanLimits] = {
    "free": PlanLimits(3, 200, 3),
    "pilot": PlanLimits(30, 3000, 10),
    "org": PlanLimits(300, 30000, 50),
}


class PlanLimitError(Exception):
    def __init__(self, resource: LimitResource) -> None:
        super().__init__(f"plan_limit:{resource}")
        self.resource: LimitResource = resource


def plan_of(value: str) -> Plan:
    """The stored plan name as a ``Plan``; anything unknown counts as ``free``."""
    if value == "pilot":
        return "pilot"
    if value == "org":
        return "org"
    return "free"


def limits_for(store: Store, org_id: str) -> PlanLimits:
    return PLANS[plan_of(store.get_org(org_id).plan)]


def current_usage(store: Store, org_id: str, resource: LimitResource, today: date) -> int:
    """How much of a limited resource the org uses now.

    ``shops`` counts the current principal's org, so call it with ``PRINCIPAL``
    set to ``org_id``.
    """
    if resource == "shops":
        return store.count_shops()
    if resource == "members":
        return store.count_members(org_id)
    return store.llm_usage_month(org_id, today).calls


def check_limit(store: Store, org_id: str, resource: str) -> None:
    """Raise PlanLimitError when the org is already at its plan limit for ``resource``.

    Call it before adding one more of the resource (a new shop, a new member).
    """
    if resource not in ("shops", "members", "llm_calls"):
        raise ValueError(f"unknown resource {resource!r}")
    res: LimitResource = (
        "shops" if resource == "shops" else "members" if resource == "members" else "llm_calls"
    )
    limits = limits_for(store, org_id)
    cap = {
        "shops": limits.shops,
        "members": limits.members,
        "llm_calls": limits.llm_calls_month,
    }[res]
    if current_usage(store, org_id, res, datetime.now(UTC).date()) >= cap:
        raise PlanLimitError(res)


@dataclass(frozen=True)
class _NoUsage:
    tokens_in: int = 0
    tokens_out: int = 0


def _default_provider(llm: ShopLLM) -> str:
    return "fake" if isinstance(llm, FakeShopLLM) else "qwen"


class MeteredLLM:
    """ShopLLM wrapper that enforces the org's monthly LLM quota and records usage.

    Over quota the call is not sent: a fake draft is returned instead and
    ``last_provider()`` reports ``"quota"``. Calls made without an org (no
    principal org) pass straight through unmetered.
    """

    def __init__(self, inner: ShopLLM, store: Store, clock: Callable[[], datetime]) -> None:
        self._inner = inner
        self._store = store
        self._clock = clock
        self._fake = FakeShopLLM()
        self._last_provider = _default_provider(inner)
        self._last_usage: Any = _NoUsage()

    def last_provider(self) -> str:
        return self._last_provider

    def last_usage(self) -> Any:
        return self._last_usage

    def _over_quota(self, org_id: str, kind: UsageKind) -> bool:
        try:
            limit = limits_for(self._store, org_id).llm_calls_month
            used = self._store.llm_usage_month(org_id, self._clock().date()).calls
        except (psycopg.Error, LookupError) as exc:  # metering must not break the request
            log.warning("llm_quota_check_failed", org_id=org_id, kind=kind, error=str(exc))
            return False
        if used < limit:
            return False
        log.warning("llm_quota_exceeded", org_id=org_id, kind=kind, used=used, limit=limit)
        return True

    def _record(self, org_id: str, kind: UsageKind, provider: str, usage: Any) -> None:
        try:
            self._store.add_llm_usage(
                LlmUsageRow(
                    org_id=org_id,
                    occurred_at=self._clock(),
                    kind=kind,
                    provider=provider,
                    tokens_in=int(getattr(usage, "tokens_in", 0) or 0),
                    tokens_out=int(getattr(usage, "tokens_out", 0) or 0),
                    fallback=provider == "fallback",
                )
            )
        except (psycopg.Error, LookupError) as exc:  # metering must not break the request
            log.warning("llm_usage_record_failed", org_id=org_id, kind=kind, error=str(exc))

    def _call(self, kind: UsageKind, real: Callable[[], T], fake: Callable[[], T]) -> T:
        org_id = PRINCIPAL.get().org_id
        if org_id and self._over_quota(org_id, kind):
            self._last_provider = "quota"
            self._last_usage = _NoUsage()
            return fake()
        result = real()
        if kind == "follow_up":
            provider = _default_provider(self._inner)
        else:
            last = getattr(self._inner, "last_provider", None)
            provider = str(last()) if callable(last) else _default_provider(self._inner)
        inner_usage = getattr(self._inner, "last_usage", None)
        usage: Any = inner_usage() if callable(inner_usage) else _NoUsage()
        self._last_provider = provider
        self._last_usage = usage
        if org_id:
            self._record(org_id, kind, provider, usage)
        return result

    def follow_up(self, question: str, answer: str, candidates: list[FollowUp]) -> FollowUp | None:
        return self._call(
            "follow_up",
            lambda: self._inner.follow_up(question, answer, candidates),
            lambda: self._fake.follow_up(question, answer, candidates),
        )

    def draft_sections(
        self, answers_by_axis: dict[Axis, list[str]], tags: "CardTags"
    ) -> dict[Axis, str]:
        return self._call(
            "draft",
            lambda: self._inner.draft_sections(answers_by_axis, tags),
            lambda: self._fake.draft_sections(answers_by_axis, tags),
        )

    def verify_claims(
        self, question: str, claims: list[str], excerpts: list[str]
    ) -> list[ClaimVerdict]:
        return self._call(
            "verify",
            lambda: self._inner.verify_claims(question, claims, excerpts),
            lambda: self._fake.verify_claims(question, claims, excerpts),
        )


def metered_llm(inner: ShopLLM, store: Store) -> ShopLLM:
    return MeteredLLM(inner, store, lambda: datetime.now(UTC))
