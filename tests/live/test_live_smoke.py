"""Real-model smoke tests; each skips with its reason when the model is unavailable."""

import os
import re
from pathlib import Path

import pytest

from shoplegacy.cards import CardTags
from shoplegacy.config import DEFAULT_LLM_BASE_URL
from shoplegacy.providers import AXES, QwenShopLLM

SAMPLE = Path(__file__).parent / "sample_ko.wav"
HANGUL = re.compile(r"[가-힣]")


def test_qwen_draft_and_verify() -> None:
    api_key = os.environ.get("SHOPLEGACY_LLM_API_KEY", "").strip()
    if not api_key:
        pytest.skip("SHOPLEGACY_LLM_API_KEY not set")
    llm = QwenShopLLM(
        os.environ.get("SHOPLEGACY_LLM_BASE_URL", DEFAULT_LLM_BASE_URL),
        os.environ.get("SHOPLEGACY_LLM_MODEL", "qwen3.7-flash"),
        api_key,
        60.0,
    )
    answers = {
        "location": ["우리 가게는 지하철 출구 앞 골목에 있었어요.", "점심 유동 인구가 많았어요."],
        "suppliers": ["거래처 A에서 매주 재료를 받았어요.", "주문은 전화로 했어요."],
        "menu": ["대표 메뉴는 김치찌개였어요.", "메뉴는 단순하게 유지했어요."],
        "pricing": ["김치찌개는 9000원에 팔았어요.", "원가율은 삼십 퍼센트 정도였어요."],
        "customers": ["단골은 점심에 오는 직장인이 많았어요.", "신규 손님은 소개로 왔어요."],
        "failures": ["임대료가 두 번 올랐어요.", "점심 외 시간대가 비었어요."],
    }
    tags = CardTags(trade="restaurant", area_type="station", years=8, closed_year=2024)
    sections = llm.draft_sections(answers, tags)  # type: ignore[arg-type]
    assert set(sections) == set(AXES)
    assert len(sections) == 6
    assert all(isinstance(text, str) and text.strip() for text in sections.values())

    claims = ["대표 메뉴는 김치찌개였다.", "가게는 해외에 열 곳이 있었다."]
    excerpts = ["대표 메뉴는 김치찌개였어요.", "메뉴는 단순하게 유지했어요."]
    verdicts = llm.verify_claims("가게의 메뉴에 대해 사장님은 무엇을 말했나요?", claims, excerpts)
    assert len(verdicts) == len(claims)


def test_whisper_transcribes_sample() -> None:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        pytest.skip("faster-whisper not installed")
    from shoplegacy.stt import WhisperSTT

    result = WhisperSTT(os.environ.get("SHOPLEGACY_WHISPER_MODEL", "small")).transcribe(
        SAMPLE.read_bytes(), SAMPLE.name, "audio/wav"
    )
    assert HANGUL.search(result.text), result.text
