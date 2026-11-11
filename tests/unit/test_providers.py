import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from shoplegacy.cards import CardTags
from shoplegacy.config import load_settings
from shoplegacy.providers import (
    DRAFT_SYSTEM,
    SECTION_MAX_CHARS,
    VERIFY_CARD_MAX_TOKENS,
    VERIFY_CARD_SYSTEM,
    FakeShopLLM,
    LLMUsage,
    QwenShopLLM,
    build_llm,
)
from shoplegacy.question_tree import Axis, FollowUp

TAGS = CardTags(trade="tree_min", area_type="residential", years=15, closed_year=2024)
DB_ENV = {
    "SHOPLEGACY_DATABASE_URL": "postgresql://unused@localhost/unused",
    "SHOPLEGACY_JWT_SECRET": "test-secret",
}
CANDIDATES = [FollowUp(id="loc-01-f1", text="첫 꼬리"), FollowUp(id="loc-01-f2", text="둘째")]


def _qwen(handler: Any) -> QwenShopLLM:
    return QwenShopLLM(
        "https://llm.invalid/v1", "m", "k", 5.0, transport=httpx.MockTransport(handler)
    )


def _reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_fake_follow_up_picks_first() -> None:
    llm = FakeShopLLM()
    assert llm.follow_up("q", "a", CANDIDATES) == CANDIDATES[0]
    assert llm.follow_up("q", "a", []) is None


def test_fake_draft_sections_joins_and_truncates() -> None:
    answers: dict[Axis, list[str]] = {"menu": ["국밥", "수육"], "failures": ["가" * 700]}
    out = FakeShopLLM().draft_sections(answers, TAGS)
    assert set(out) == {"location", "suppliers", "menu", "pricing", "customers", "failures"}
    assert out["menu"] == "국밥 수육"
    assert len(out["failures"]) == SECTION_MAX_CHARS
    assert out["location"] == ""


def test_qwen_draft_sections_uses_response() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _reply(json.dumps({"menu": "정리된 메뉴 문단", "location": "지어낸 문단"}))

    out = _qwen(handler).draft_sections({"menu": ["국밥"]}, TAGS)
    assert out["menu"] == "정리된 메뉴 문단"
    assert out["location"] == ""  # no answers on that axis → stays empty
    assert seen[0]["response_format"] == {"type": "json_object"}


def test_qwen_draft_sections_falls_back_on_bad_json() -> None:
    out = _qwen(lambda _r: _reply("not json")).draft_sections({"menu": ["국밥"]}, TAGS)
    assert out["menu"] == "국밥"


def test_qwen_follow_up_delegates() -> None:
    llm = _qwen(lambda _r: _reply(json.dumps({"pick": 1})))
    assert llm.follow_up("q", "a", CANDIDATES) == CANDIDATES[1]


def test_build_llm(tmp_path: Path) -> None:
    fake = build_llm(load_settings({**DB_ENV, "SHOPLEGACY_DATA_DIR": str(tmp_path)}))
    assert isinstance(fake, FakeShopLLM)
    qwen = build_llm(
        load_settings(
            {**DB_ENV, "SHOPLEGACY_PROVIDER": "qwen", "SHOPLEGACY_LLM_API_KEY": "test-key"}
        )
    )
    assert isinstance(qwen, QwenShopLLM)


def test_qwen_requires_key() -> None:
    with pytest.raises(ValueError):
        load_settings({**DB_ENV, "SHOPLEGACY_PROVIDER": "qwen"})


def test_fake_verify_claims_uses_containment() -> None:
    verdicts = FakeShopLLM().verify_claims(
        "메뉴", ["국밥이 주력이었어요.", "파스타를 팔았다."], ["수육도 있었다.", "국밥이 주력이었어요."]
    )
    assert [v.supported for v in verdicts] == [True, False]
    assert verdicts[0].citation_index == 2
    assert verdicts[1].citation_index is None
    assert FakeShopLLM().verify_claims("메뉴", [], ["x"]) == []


def test_qwen_verify_claims_provider_error_all_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from handoff import providers_qwen

    monkeypatch.setattr(providers_qwen, "_sleep", lambda _s: None)
    calls: list[int] = []

    def handler(_r: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    verdicts = _qwen(handler).verify_claims("메뉴", ["국밥이 주력이었어요."], ["국밥이 주력이었어요."])
    assert calls  # the provider was actually called
    assert [(v.supported, v.citation_index) for v in verdicts] == [(False, None)]


# --- provider and usage of the last call ------------------------------------------


def _reply_usage(content: str, usage: Any) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"content": content}}], "usage": usage}
    )


def test_fake_last_provider_and_usage() -> None:
    llm = FakeShopLLM()
    llm.draft_sections({"menu": ["국밥"]}, TAGS)
    assert llm.last_provider() == "fake"
    assert llm.last_usage() == LLMUsage(0, 0)


def test_qwen_draft_records_provider_and_tokens() -> None:
    usage = {"prompt_tokens": 321, "completion_tokens": 45, "total_tokens": 366}
    llm = _qwen(lambda _r: _reply_usage(json.dumps({"menu": "정리"}), usage))
    llm.draft_sections({"menu": ["국밥"]}, TAGS)
    assert llm.last_provider() == "qwen"
    assert llm.last_usage() == LLMUsage(tokens_in=321, tokens_out=45)


def test_qwen_usage_missing_or_odd_is_zero() -> None:
    llm = _qwen(lambda _r: _reply_usage(json.dumps({"menu": "정리"}), {"prompt_tokens": "x"}))
    llm.draft_sections({"menu": ["국밥"]}, TAGS)
    assert llm.last_usage() == LLMUsage(0, 0)
    verdicts = json.dumps(
        {"verdicts": [{"index": 1, "supported": True, "answers_question": True, "citation": 1}]}
    )
    llm = _qwen(lambda _r: _reply_usage(verdicts, {"prompt_tokens": 7, "completion_tokens": 2}))
    llm.verify_claims("메뉴", ["국밥이다."], ["국밥이다."])
    assert (llm.last_provider(), llm.last_usage()) == ("qwen", LLMUsage(7, 2))
    llm = _qwen(lambda _r: _reply(json.dumps({"menu": "정리"})))
    llm.draft_sections({"menu": ["국밥"]}, TAGS)
    assert llm.last_usage() == LLMUsage(0, 0)


def test_qwen_draft_fallback_is_reported_and_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    from handoff import providers_qwen

    monkeypatch.setattr(providers_qwen, "_sleep", lambda _s: None)
    llm = _qwen(lambda _r: httpx.Response(500))
    with capture_logs() as logs:
        out = llm.draft_sections({"menu": ["국밥"]}, TAGS)
    assert out["menu"] == "국밥"
    assert llm.last_provider() == "fallback"
    events = [e for e in logs if e["event"] == "llm_fallback"]
    assert len(events) == 1 and events[0]["log_level"] == "warning"
    assert "error" in events[0]
    # the next successful call resets the provider
    ok = _qwen(lambda _r: _reply(json.dumps({"menu": "정리"})))
    ok.draft_sections({"menu": ["국밥"]}, TAGS)
    assert ok.last_provider() == "qwen"


def test_qwen_verify_fallback_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    from handoff import providers_qwen

    monkeypatch.setattr(providers_qwen, "_sleep", lambda _s: None)
    llm = _qwen(lambda _r: httpx.Response(503))
    llm.verify_claims("메뉴", ["국밥이다."], ["국밥이다."])
    assert llm.last_provider() == "fallback"


# --- sentence-per-answer drafts and card verify prompt ------------------------------


def test_qwen_draft_joins_sentence_array() -> None:
    reply = {"menu": [" 대표 메뉴는 국밥이었어요. ", "", "수육도 팔았어요."], "pricing": [1, 2]}
    out = _qwen(lambda _r: _reply(json.dumps(reply))).draft_sections(
        {"menu": ["국밥"], "pricing": ["팔천원"]}, TAGS
    )
    assert out["menu"] == "대표 메뉴는 국밥이었어요. 수육도 팔았어요."
    assert out["pricing"] == "팔천원"  # no usable sentence → fake fallback for that axis


def test_qwen_draft_string_gets_space_after_sentence_end() -> None:
    reply = {"menu": "대표 메뉴는 국밥이었어요.수육도 팔았어요!손님이 많았나요?네."}
    out = _qwen(lambda _r: _reply(json.dumps(reply))).draft_sections({"menu": ["국밥"]}, TAGS)
    assert out["menu"] == "대표 메뉴는 국밥이었어요. 수육도 팔았어요! 손님이 많았나요? 네."


def test_qwen_draft_keeps_header_but_forbids_header_facts() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _reply(json.dumps({"menu": ["국밥이었어요."]}))

    _qwen(handler).draft_sections({"menu": ["국밥"]}, TAGS)
    system, user = (m["content"] for m in seen[0]["messages"])
    assert system == DRAFT_SYSTEM
    assert "운영 15년" in user and "폐업 2024년" in user  # S1: header facts were not the cause
    for rule in ("답변 한 개당 문장 한 개", "합치지 않습니다", "그대로", "머리말", "배열"):
        assert rule in system


def _verdicts_json(n: int) -> str:
    items = [
        {"index": i, "supported": True, "answers_question": True, "citation": 1}
        for i in range(1, n + 1)
    ]
    return json.dumps({"situation": "메뉴", "verdicts": items})


def test_qwen_verify_retries_once_on_unparsable_reply() -> None:
    seen: list[dict[str, Any]] = []
    replies = ["not json", _verdicts_json(1)]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _reply(replies[len(seen) - 1])

    llm = _qwen(handler)
    verdicts = llm.verify_claims("메뉴", ["국밥이다."], ["국밥이다."])
    assert len(seen) == 2
    assert seen[0] == seen[1]  # the same request is sent again
    assert seen[0]["messages"][0]["content"] == VERIFY_CARD_SYSTEM
    assert seen[0]["max_tokens"] == VERIFY_CARD_MAX_TOKENS
    assert "null" in VERIFY_CARD_SYSTEM and "citation" in VERIFY_CARD_SYSTEM
    assert [(v.supported, v.citation_index) for v in verdicts] == [(True, 1)]
    assert llm.last_provider() == "qwen"


def test_qwen_verify_two_unparsable_replies_fall_back_and_log() -> None:
    calls: list[int] = []

    def handler(_r: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _reply(json.dumps({"verdicts": []}))  # wrong length → parser returns None

    llm = _qwen(handler)
    with capture_logs() as logs:
        verdicts = llm.verify_claims("메뉴", ["국밥이다.", "수육이다."], ["국밥이다."])
    assert len(calls) == 2
    assert [(v.supported, v.citation_index) for v in verdicts] == [(False, None), (False, None)]
    assert llm.last_provider() == "fallback"
    failed = [e for e in logs if e["event"] == "llm_verify_parse_failed"]
    assert len(failed) == 1 and failed[0]["log_level"] == "warning"
    assert (failed[0]["claims"], failed[0]["attempts"]) == (2, 2)
    fallback = [e for e in logs if e["event"] == "llm_fallback"]
    assert fallback and fallback[0]["error"] == "llm request failed: status=invalid_verdicts"
