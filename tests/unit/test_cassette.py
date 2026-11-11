from pathlib import Path

import httpx
import pytest

from shoplegacy.cards import CardTags
from shoplegacy.providers import QwenShopLLM
from tests.golden.cassette import use_cassette

URL = "https://llm.invalid/v1"
TAGS = CardTags(trade="tree_min", area_type="residential", years=15, closed_year=2024)


def _llm(handler: object) -> QwenShopLLM:
    return QwenShopLLM(URL, "m", "secret-key", 5.0, transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def _draft(llm: QwenShopLLM) -> str:
    return llm.draft_sections({"menu": ["김치찌개가 대표 메뉴였어요."]}, TAGS)["menu"]  # type: ignore[arg-type]


def test_record_then_replay_without_network(tmp_path: Path) -> None:
    reply = '{"menu": ["김치찌개가 대표 메뉴였어요."]}'

    def record(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network used during replay")

    with use_cassette("case", record=True, directory=tmp_path):
        recorded = _draft(_llm(record))
    saved = (tmp_path / "case.yaml").read_text(encoding="utf-8")
    assert "secret-key" not in saved
    assert "authorization" not in saved.lower()
    with use_cassette("case", record=False, directory=tmp_path):
        replayed = _draft(_llm(forbidden))
    assert replayed == recorded


def test_replay_without_cassette_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="cassette missing"):
        use_cassette("absent", record=False, directory=tmp_path)
