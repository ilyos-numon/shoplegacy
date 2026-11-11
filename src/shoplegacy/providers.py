"""LLM providers for interviews and card drafts (follow-ups delegate to handoff)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx
import structlog
from handoff.contracts import Citation, ClaimVerdict
from handoff.llm_tasks.verify import _parse_verdicts, fake_verify_claims
from handoff.providers import FakeLLM, ProviderError
from handoff.providers_qwen import QwenLLM, _Client

from shoplegacy.question_tree import AXIS_LABELS, Axis, FollowUp

if TYPE_CHECKING:
    from shoplegacy.cards import CardTags
    from shoplegacy.config import Settings

log = structlog.get_logger("shoplegacy")

SECTION_MAX_CHARS = 600
AXES: tuple[Axis, ...] = ("location", "suppliers", "menu", "pricing", "customers", "failures")

DRAFT_SYSTEM = (
    "당신은 폐업한 가게 사장님의 인터뷰 답변을 후배 창업자에게 전하는 카드 문장으로 옮깁니다. "
    "축마다 답변 한 개당 문장 한 개로 옮깁니다. 두 답변을 한 문장으로 합치지 않습니다. "
    "사장님이 쓴 단어·숫자·표현을 그대로 옮기고 바꿔 말하지 않습니다. "
    "문장 끝 존댓말 어미를 맞추는 것만 허용합니다. "
    "답변에 없는 사실은 쓰지 않습니다. 맨 위 머리말의 업종·상권·운영 연수·폐업 연도도 "
    "답변에 없으면 문장에 쓰지 않습니다. 교훈이나 평가를 지어내지 않습니다. "
    "상호, 사람 이름, 거래처 이름, 전화번호, 주소는 쓰지 않습니다. "
    "JSON 객체 하나만 출력합니다. 키는 축 이름, 값은 문장 문자열의 배열입니다. "
    '예: {"menu": ["대표 메뉴는 김치찌개였어요.", "메뉴는 열 가지 안쪽으로 단순하게 유지했어요."]}'
)

VERIFY_CARD_SYSTEM = (
    "당신은 폐업한 가게 사장님의 인터뷰 답변으로 만든 카드 문장(주장)을 검증하는 검사기입니다. "
    "질문(축 이름), 번호가 붙은 근거 발췌(사장님 답변), 번호가 붙은 주장을 읽고 주장마다 판정합니다.\n"
    "1. supported: 주장의 내용이 어떤 근거 발췌에 적혀 있으면 true. "
    "표현·어순·어미가 달라도 같은 뜻이면 true 입니다. "
    "주장이 두 발췌의 내용을 함께 담으면 둘 다 근거에 있을 때 true 이고, "
    "citation 은 더 많은 내용을 담은 발췌 번호입니다. "
    "근거 어디에도 없는 숫자·이름·장소·이유·시점이 하나라도 있으면 false 입니다.\n"
    "2. citation: 주장을 뒷받침하는 근거 번호(1부터). "
    "supported 가 true 이면 citation 은 반드시 근거 번호이고 null 이면 안 됩니다. "
    "supported 가 false 이면 null 입니다.\n"
    "3. answers_question: 주장이 그 축(질문)에 관한 내용이면 true.\n"
    "4. situation_covered: citation 이 있으면 true, 없으면 false.\n"
    "situation 에는 축 이름을 한 줄로 적습니다. "
    '반드시 {"situation": "<축 이름 한 줄>", "verdicts": [{"index": <주장 번호 1부터>, '
    '"supported": true|false, "answers_question": true|false, '
    '"situation_covered": true|false, "citation": <근거 번호 또는 null>}]} '
    "형식의 JSON 만 출력하고, 주장마다 정확히 한 항목을 주장 번호 순서로 씁니다."
)
VERIFY_CARD_MAX_TOKENS = 600

_SENTENCE_GAP = re.compile(r"(?<=[.!?])(?=\S)")


def _join_sentences(value: object) -> str:
    """Sentence list → space-joined text; a string gets a space after each sentence end."""
    if isinstance(value, list):
        return " ".join(s.strip() for s in value if isinstance(s, str) and s.strip())
    if isinstance(value, str):
        return _SENTENCE_GAP.sub(" ", value.strip())
    return ""


@dataclass(frozen=True)
class LLMUsage:
    tokens_in: int = 0
    tokens_out: int = 0


class ShopLLM(Protocol):
    def follow_up(
        self, question: str, answer: str, candidates: list[FollowUp]
    ) -> FollowUp | None: ...

    def draft_sections(
        self, answers_by_axis: dict[Axis, list[str]], tags: CardTags
    ) -> dict[Axis, str]: ...

    def verify_claims(
        self, question: str, claims: list[str], excerpts: list[str]
    ) -> list[ClaimVerdict]: ...

    def last_provider(self) -> str:
        """What the previous draft_sections / verify_claims call actually used."""
        ...

    def last_usage(self) -> LLMUsage: ...


def _citations(excerpts: list[str]) -> list[Citation]:
    # handoff's Citation.axis is its own 4-value Literal; its verify helpers only read
    # .excerpt (fake) or len() (parser), so build unvalidated stand-ins.
    return [Citation.model_construct(excerpt=e) for e in excerpts]


def _all_unsupported(claims: list[str]) -> list[ClaimVerdict]:
    return [ClaimVerdict(claim=c, supported=False, answers_question=False) for c in claims]


def fake_sections(answers_by_axis: dict[Axis, list[str]]) -> dict[Axis, str]:
    """Join each axis's answers and cut to the section limit; empty axes stay ''."""
    out: dict[Axis, str] = {}
    for axis in AXES:
        parts = [a.strip() for a in answers_by_axis.get(axis, []) if a.strip()]
        out[axis] = " ".join(parts)[:SECTION_MAX_CHARS]
    return out


class FakeShopLLM:
    """Deterministic stand-in; follow-ups delegate to handoff's FakeLLM."""

    def __init__(self) -> None:
        self._inner = FakeLLM()

    def follow_up(
        self, question: str, answer: str, candidates: list[FollowUp]
    ) -> FollowUp | None:
        return self._inner.follow_up(question, answer, candidates)

    def draft_sections(
        self, answers_by_axis: dict[Axis, list[str]], tags: CardTags
    ) -> dict[Axis, str]:
        return fake_sections(answers_by_axis)

    def verify_claims(
        self, question: str, claims: list[str], excerpts: list[str]
    ) -> list[ClaimVerdict]:
        """Supported when containment(claim, excerpt) >= handoff SUPPORT_MIN for some excerpt."""
        return fake_verify_claims(question, claims, _citations(excerpts))

    def last_provider(self) -> str:
        return "fake"

    def last_usage(self) -> LLMUsage:
        return LLMUsage()


def _usage(data: Any) -> LLMUsage:
    """Token counts from an OpenAI-compatible response; missing or odd values are 0."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return LLMUsage()

    def count(key: str) -> int:
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return LLMUsage(tokens_in=count("prompt_tokens"), tokens_out=count("completion_tokens"))


class QwenShopLLM:
    """Qwen (OpenAI-compatible) provider; falls back to fake drafts on provider errors."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout_s: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._model = model
        self._inner = QwenLLM(base_url, model, api_key, timeout_s, transport=transport)
        self._client = _Client("llm", base_url, api_key, timeout_s, transport)
        self._last_provider = "qwen"
        self._last_usage = LLMUsage()

    def last_provider(self) -> str:
        return self._last_provider

    def last_usage(self) -> LLMUsage:
        return self._last_usage

    def _fallback(self, error: str) -> None:
        self._last_provider = "fallback"
        log.warning("llm_fallback", error=error)

    def follow_up(
        self, question: str, answer: str, candidates: list[FollowUp]
    ) -> FollowUp | None:
        return self._inner.follow_up(question, answer, candidates)

    def _chat(self, system: str, user: str, max_tokens: int) -> Any:
        """JSON-mode chat; records token usage. Returns the message content (any type)."""
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "enable_thinking": False,
            "response_format": {"type": "json_object"},
        }
        data = self._client.post("/chat/completions", body)
        self._last_usage = _usage(data)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("llm request failed: status=malformed") from None

    def _chat_json(self, user: str) -> Any:
        content = self._chat(DRAFT_SYSTEM, user, 1500)
        if not isinstance(content, str):
            raise ProviderError("llm request failed: status=malformed")
        try:
            return json.loads(content)
        except ValueError:
            raise ProviderError("llm request failed: status=invalid_json") from None

    def draft_sections(
        self, answers_by_axis: dict[Axis, list[str]], tags: CardTags
    ) -> dict[Axis, str]:
        fallback = fake_sections(answers_by_axis)
        filled = [axis for axis in AXES if fallback[axis]]
        self._last_provider = "qwen"
        self._last_usage = LLMUsage()
        if not filled:
            return fallback
        blocks = []
        for axis in filled:
            joined = "\n".join(f"- {a}" for a in answers_by_axis.get(axis, []) if a.strip())
            blocks.append(f"[{axis} / {AXIS_LABELS[axis]}]\n{joined}")
        user = (
            f"업종: {tags.trade}, 상권: {tags.area_type}, 운영 {tags.years}년, "
            f"폐업 {tags.closed_year}년\n\n" + "\n\n".join(blocks)
        )
        try:
            parsed = self._chat_json(user)
        except ProviderError as exc:
            self._fallback(str(exc))
            return fallback
        if not isinstance(parsed, dict):
            self._fallback("llm request failed: status=not_object")
            return fallback
        out: dict[Axis, str] = {}
        for axis in AXES:
            text = _join_sentences(parsed.get(axis))
            if axis in filled and text:
                out[axis] = text[:SECTION_MAX_CHARS]
            else:
                out[axis] = fallback[axis]
        return out

    def verify_claims(
        self, question: str, claims: list[str], excerpts: list[str]
    ) -> list[ClaimVerdict]:
        """Card verify prompt + handoff's parser; an unparsable reply is retried once."""
        self._last_provider = "qwen"
        self._last_usage = LLMUsage()
        if not claims:
            return []
        evidence = "\n".join(f"[근거 {i}] {e}" for i, e in enumerate(excerpts, start=1))
        numbered = "\n".join(f"[주장 {i}] {c}" for i, c in enumerate(claims, start=1))
        user = f"질문: {question}\n근거:\n{evidence}\n주장:\n{numbered}"
        for _attempt in range(2):
            try:
                content = self._chat(VERIFY_CARD_SYSTEM, user, VERIFY_CARD_MAX_TOKENS)
            except ProviderError as exc:
                self._fallback(str(exc))
                return _all_unsupported(claims)
            text = content if isinstance(content, str) else ""
            parsed = _parse_verdicts(text, claims, _citations(excerpts))
            if parsed is not None:
                return parsed
        log.warning("llm_verify_parse_failed", claims=len(claims), attempts=2)
        self._fallback("llm request failed: status=invalid_verdicts")
        return _all_unsupported(claims)


def build_llm(settings: Settings) -> ShopLLM:
    if settings.provider == "qwen":
        return QwenShopLLM(
            settings.llm_base_url,
            settings.llm_model,
            settings.llm_api_key,
            settings.llm_timeout_s,
        )
    return FakeShopLLM()
