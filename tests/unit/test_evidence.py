import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from handoff import providers_qwen
from handoff.contracts import ClaimVerdict

from shoplegacy.evidence import (
    Evidence,
    build_evidence,
    lexical_citation,
    split_sentences,
    supported_ratio,
)
from shoplegacy.interview import AnswerEntry
from shoplegacy.masking import MaskMap
from shoplegacy.providers import FakeShopLLM, QwenShopLLM
from shoplegacy.question_tree import Axis

MASK = MaskMap(shop_name="행복식당", owner_name="김사장")
AXES = {"location", "suppliers", "menu", "pricing", "customers", "failures"}


def _answer(axis: Axis, text: str, answer_id: int) -> AnswerEntry:
    return AnswerEntry(
        session_id="s1",
        question_id=f"{axis}-01",
        axis=axis,
        question="q",
        answer=text,
        created_at=datetime(2026, 11, 2, tzinfo=UTC),
        session_number=1,
        answer_id=answer_id,
    )


def _qwen(handler: Any) -> QwenShopLLM:
    return QwenShopLLM(
        "https://llm.invalid/v1", "m", "k", 5.0, transport=httpx.MockTransport(handler)
    )


def _reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_split_sentences_korean_endings() -> None:
    assert split_sentences("손님이 많았다. 점심이 바빴어요. 저녁은 한산했다.") == [
        "손님이 많았다.",
        "점심이 바빴어요.",
        "저녁은 한산했다.",
    ]


def test_split_sentences_question_and_exclamation() -> None:
    assert split_sentences("왜 그랬을까? 정말 힘들었다!  다시는 안 해") == [
        "왜 그랬을까?",
        "정말 힘들었다!",
        "다시는 안 해",
    ]


def test_split_sentences_no_split_without_space() -> None:
    assert split_sentences("단가는 3.5배였다.\n\n") == ["단가는 3.5배였다."]


def test_fake_supported_and_unsupported() -> None:
    answers = [_answer("location", "역 앞이라 출근길 손님이 많았어요. 월세는 비쌌어요.", 11)]
    sections: dict[Axis, str] = {"location": "역 앞이라 출근길 손님이 많았어요. 주차장이 넓었다."}
    result = build_evidence(FakeShopLLM(), sections, answers, MASK)
    ev = result["location"]
    assert [e.claim for e in ev] == ["역 앞이라 출근길 손님이 많았어요.", "주차장이 넓었다."]
    assert ev[0].supported is True
    assert ev[0].answer_id == 11
    assert "역 앞" in ev[0].quote
    assert ev[1].supported is False
    assert ev[1].answer_id is None
    assert ev[1].quote == ""


def test_quote_is_masked_and_short() -> None:
    text = "행복식당은 김사장이 혼자 했어요. " + "가" * 300
    answers = [_answer("menu", text, 5)]
    sections: dict[Axis, str] = {"menu": "우리 가게은 사장님이 혼자 했어요."}
    ev = build_evidence(FakeShopLLM(), sections, answers, MASK)["menu"]
    assert ev[0].supported is True
    assert "행복식당" not in ev[0].quote and "김사장" not in ev[0].quote
    assert len(ev[0].quote) <= 160


def test_empty_axis_and_no_answers() -> None:
    answers = [_answer("menu", "국밥이 주력이었어요.", 1)]
    sections: dict[Axis, str] = {"menu": "국밥이 주력이었어요.", "pricing": "가격은 낮았다.", "location": "  "}
    result = build_evidence(FakeShopLLM(), sections, answers, MASK)
    assert set(result) == AXES
    assert result["location"] == []
    assert result["suppliers"] == []
    assert [e.supported for e in result["menu"]] == [True]
    # sentences with no answers on the axis are unsupported, not dropped
    assert [e.supported for e in result["pricing"]] == [False]


def test_ratio_from_built_evidence() -> None:
    answers = [_answer("menu", "국밥이 주력이었어요.", 1), _answer("pricing", "가격은 칠천원이었다.", 2)]
    sections: dict[Axis, str] = {
        "menu": "국밥이 주력이었어요. 디저트도 팔았다.",
        "pricing": "가격은 칠천원이었다.",
    }
    result = build_evidence(FakeShopLLM(), sections, answers, MASK)
    assert supported_ratio(result, ["menu", "pricing"]) == pytest.approx(2 / 3)
    assert supported_ratio(result, ["location"]) == 0.0


def test_qwen_success_maps_citation_to_answer() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        verdicts = [
            {"index": 1, "supported": True, "answers_question": True, "citation": 2},
            {"index": 2, "supported": False, "answers_question": False, "citation": None},
        ]
        return _reply(json.dumps({"situation": "거래처", "verdicts": verdicts}))

    answers = [_answer("suppliers", "고기는 시장에서 받았다.", 7), _answer("suppliers", "채소는 새벽에 샀다.", 8)]
    sections: dict[Axis, str] = {"suppliers": "채소는 새벽에 샀다. 생선은 수입했다."}
    ev = build_evidence(_qwen(handler), sections, answers, MASK)["suppliers"]
    assert [e.supported for e in ev] == [True, False]
    assert ev[0].answer_id == 8
    assert ev[0].quote == "채소는 새벽에 샀다."
    assert ev[1].answer_id is None
    user = seen[0]["messages"][1]["content"]
    assert "거래처" in user and "[근거 2]" in user and "[주장 2]" in user
    assert seen[0]["response_format"] == {"type": "json_object"}


def test_qwen_malformed_output_marks_all_unsupported() -> None:
    answers = [_answer("menu", "국밥이 주력이었어요.", 1)]
    sections: dict[Axis, str] = {"menu": "국밥이 주력이었어요. 수육도 팔았어요."}
    bad = json.dumps({"verdicts": [{"index": 1, "supported": True}]})
    ev = build_evidence(_qwen(lambda _r: _reply(bad)), sections, answers, MASK)["menu"]
    assert len(ev) == 2
    assert all(e.supported is False for e in ev)


def test_qwen_provider_error_marks_all_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers_qwen, "_sleep", lambda _s: None)
    answers = [_answer("menu", "국밥이 주력이었어요.", 1)]
    sections: dict[Axis, str] = {"menu": "국밥이 주력이었어요."}
    ev = build_evidence(_qwen(lambda _r: httpx.Response(500)), sections, answers, MASK)["menu"]
    assert ev == [Evidence(claim="국밥이 주력이었어요.", answer_id=None, quote="", supported=False)]


# --- reviewer-written sentences ---------------------------------------------------


class _SpyLLM(FakeShopLLM):
    def __init__(self) -> None:
        super().__init__()
        self.claims: list[list[str]] = []

    def verify_claims(self, question: str, claims: list[str], excerpts: list[str]) -> Any:
        self.claims.append(list(claims))
        return super().verify_claims(question, claims, excerpts)


def test_sentence_not_in_draft_is_reviewer_written() -> None:
    answers = [_answer("menu", "국밥이 주력이었어요.", 1)]
    draft: dict[Axis, str] = {"menu": "국밥이 주력이었어요."}
    sections: dict[Axis, str] = {"menu": "점심 손님이 많았다. 국밥이 주력이었어요."}
    ev = build_evidence(FakeShopLLM(), sections, answers, MASK, draft_sections=draft)["menu"]
    assert [(e.claim, e.source, e.supported) for e in ev] == [
        ("점심 손님이 많았다.", "reviewer", False),
        ("국밥이 주력이었어요.", "answer", True),
    ]
    assert (ev[0].answer_id, ev[0].quote) == (None, "")
    assert ev[1].answer_id == 1


def test_reviewer_sentences_are_not_sent_for_checking() -> None:
    spy = _SpyLLM()
    answers = [_answer("menu", "국밥이 주력이었어요.", 1)]
    draft: dict[Axis, str] = {"menu": "국밥이 주력이었어요.", "pricing": "가격은 낮았다."}
    sections: dict[Axis, str] = {
        "menu": "국밥이 주력이었어요. 새로 쓴 문장이다.",
        "pricing": "전부 새로 썼다.",
    }
    result = build_evidence(spy, sections, answers, MASK, draft_sections=draft)
    assert spy.claims == [["국밥이 주력이었어요."]]
    assert [e.source for e in result["pricing"]] == ["reviewer"]


def test_ratio_leaves_out_reviewer_sentences() -> None:
    def ev(supported: bool, source: str = "answer") -> Evidence:
        return Evidence.model_validate(
            {"claim": "c", "answer_id": None, "quote": "", "supported": supported, "source": source}
        )

    evidence: dict[Axis, list[Evidence]] = {
        "menu": [ev(True), ev(False), ev(False, "reviewer"), ev(False, "reviewer")],
        "pricing": [ev(False, "reviewer")],
    }
    assert supported_ratio(evidence, ["menu"]) == pytest.approx(0.5)
    assert supported_ratio(evidence, ["pricing"]) == 1.0  # only reviewer sentences
    assert supported_ratio(evidence, ["location"]) == 0.0  # no sentences at all
    assert Evidence(claim="c", answer_id=None, quote="", supported=True).source == "answer"


def test_without_draft_every_sentence_is_checked() -> None:
    answers = [_answer("menu", "국밥이 주력이었어요.", 1)]
    sections: dict[Axis, str] = {"menu": "국밥이 주력이었어요. 새 문장."}
    ev = build_evidence(FakeShopLLM(), sections, answers, MASK)["menu"]
    assert [e.source for e in ev] == ["answer", "answer"]


# --- containment rescue -------------------------------------------------------------


class _VerdictLLM(FakeShopLLM):
    """Test-only verifier: fixed (supported, citation) per claim and a fixed provider."""

    def __init__(self, provider: str, verdicts: list[tuple[bool, int | None]]) -> None:
        super().__init__()
        self._provider = provider
        self._verdicts = verdicts

    def verify_claims(
        self, question: str, claims: list[str], excerpts: list[str]
    ) -> list[ClaimVerdict]:
        return [
            ClaimVerdict(claim=c, supported=s, answers_question=s, citation_index=i)
            for c, (s, i) in zip(claims, self._verdicts, strict=True)
        ]

    def last_provider(self) -> str:
        return self._provider


SUPPLIER_ANSWERS = [
    _answer("suppliers", "고기는 시장에서 받았다.", 7),
    _answer("suppliers", "채소는 새벽에 샀다.", 8),
]


def test_rescue_near_verbatim_sentence_when_verifier_says_no() -> None:
    llm = _VerdictLLM("qwen", [(False, None), (False, None)])
    sections: dict[Axis, str] = {"suppliers": "채소는 새벽에 샀어요. 생선은 수입했다."}
    ev = build_evidence(llm, sections, SUPPLIER_ANSWERS, MASK)["suppliers"]
    assert [e.supported for e in ev] == [True, False]
    assert ev[0].answer_id == 8
    assert (ev[1].answer_id, ev[1].quote) == (None, "")


def test_no_rescue_when_verifier_fell_back() -> None:
    llm = _VerdictLLM("fallback", [(False, None)])
    sections: dict[Axis, str] = {"suppliers": "채소는 새벽에 샀다."}
    ev = build_evidence(llm, sections, SUPPLIER_ANSWERS, MASK)["suppliers"]
    assert ev == [Evidence(claim="채소는 새벽에 샀다.", answer_id=None, quote="", supported=False)]


def test_rescued_citation_sets_the_quote() -> None:
    llm = _VerdictLLM("qwen", [(False, 2)])
    sections: dict[Axis, str] = {"suppliers": "고기는 시장에서 받았어요."}
    ev = build_evidence(llm, sections, SUPPLIER_ANSWERS, MASK)["suppliers"]
    assert (ev[0].supported, ev[0].answer_id, ev[0].quote) == (True, 7, "고기는 시장에서 받았다.")
    assert lexical_citation("고기는 시장에서 받았어요.", ["채소는 새벽에 샀다.", "고기는 시장에서 받았다."]) == 2
    assert lexical_citation("생선은 수입했다.", ["채소는 새벽에 샀다."]) is None


def test_supported_without_citation_gets_one_from_containment() -> None:
    llm = _VerdictLLM("qwen", [(True, None), (True, None), (True, 9)])
    sections: dict[Axis, str] = {
        "suppliers": "채소는 새벽에 샀어요. 주요 재료는 매주 공급받았습니다. 고기는 시장에서 받았다."
    }
    ev = build_evidence(llm, sections, SUPPLIER_ANSWERS, MASK)["suppliers"]
    assert [(e.supported, e.answer_id) for e in ev] == [(True, 8), (False, None), (True, 7)]
    assert ev[0].quote == "채소는 새벽에 샀다."
