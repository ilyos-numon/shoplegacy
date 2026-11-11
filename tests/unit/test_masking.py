from typing import Literal

import handoff.privacy

from shoplegacy.masking import (
    LOCATION_MASK,
    NAME_MASK,
    ORG_MASK,
    MaskMap,
    MaskResult,
    configure,
    mask,
)
from shoplegacy.ner import NameSpan, NoopNameFinder

MAP = MaskMap(shop_name="가나다식당", owner_name="홍길동", supplier_names=["정다운상회"])


def test_four_replacements_in_one_sentence() -> None:
    result = mask("가나다식당 홍길동 사장은 010-0000-0000 로 정다운상회 에 주문했다", MAP)
    assert isinstance(result, MaskResult)
    assert result.replaced == 4
    assert result.text == "우리 가게 사장님 사장은 [전화번호] 로 거래처 A 에 주문했다"
    assert result.warnings == []


def test_uses_handoff_privacy_scrub() -> None:
    result = mask("메일 owner@example.com", MaskMap())
    assert handoff.privacy.EMAIL_MASK in result.text
    assert result.replaced == 1


def test_longer_supplier_name_first() -> None:
    mapping = MaskMap(supplier_names=["대한", "대한정육"])
    result = mask("대한정육 그리고 대한", mapping)
    assert result.text == "거래처 B 그리고 거래처 A"
    assert result.replaced == 2


def test_empty_keys_are_skipped() -> None:
    result = mask("그냥 평범한 문장", MaskMap(shop_name="", owner_name="", supplier_names=[""]))
    assert result.text == "그냥 평범한 문장"
    assert result.replaced == 0


def test_address_is_replaced() -> None:
    result = mask("가게는 행복동 12-3 에 있었다", MaskMap())
    assert result.text == "가게는 [주소] 에 있었다"
    assert result.replaced == 1
    assert result.warnings == []


def test_landmark_only_warns() -> None:
    text = "행복초등학교 앞이라 아이들이 많았다"
    result = mask(text, MaskMap())
    assert result.text == text
    assert result.replaced == 0
    assert result.warnings == ["행복초등학교"]


def test_remaining_handle_is_warned() -> None:
    result = mask("단골 @regular-kim 이 자주 왔다", MaskMap())
    assert result.warnings == ["@regular-kim"]


def test_masking_is_idempotent() -> None:
    first = mask("가나다식당 홍길동 010-0000-0000 정다운상회 행복동 12-3", MAP)
    second = mask(first.text, MAP)
    assert second.text == first.text
    assert second.replaced == 0
    assert second.warnings == []


class FakeNameFinder:
    """Returns spans for each fixed word found in the text."""

    def __init__(self, words: dict[str, Literal["PERSON", "ORG", "LOCATION"]]) -> None:
        self.words = words
        self.seen: list[str] = []

    def find(self, text: str) -> list[NameSpan]:
        self.seen.append(text)
        spans: list[NameSpan] = []
        for word, label in self.words.items():
            start = text.find(word)
            while start != -1:
                spans.append(NameSpan(start, start + len(word), label))
                start = text.find(word, start + 1)
        return sorted(spans, key=lambda s: s.start)


def test_ner_person_is_masked() -> None:
    result = mask("김영희 사장이 말하길", MaskMap(), FakeNameFinder({"김영희": "PERSON"}))
    assert result.text == f"{NAME_MASK} 사장이 말하길"
    assert result.ner_replaced == 1
    assert result.replaced == 1


def test_ner_org_is_masked() -> None:
    result = mask("옆집 한빛분식 이 문을 닫았다", MaskMap(), FakeNameFinder({"한빛분식": "ORG"}))
    assert result.text == f"옆집 {ORG_MASK} 이 문을 닫았다"


def test_ner_location_is_masked() -> None:
    result = mask("처음엔 마포 에서 시작했다", MaskMap(), FakeNameFinder({"마포": "LOCATION"}))
    assert result.text == f"처음엔 {LOCATION_MASK} 에서 시작했다"


def test_ner_span_overlapping_dictionary_mask_is_ignored() -> None:
    finder = FakeNameFinder({"사장님": "PERSON", "우리 가게": "ORG", "거래처 A": "ORG"})
    result = mask("가나다식당 홍길동 정다운상회", MAP, finder)
    assert result.text == "우리 가게 사장님 거래처 A"
    assert result.ner_replaced == 0
    assert result.replaced == 3


def test_ner_replaces_back_to_front_keeping_offsets() -> None:
    finder = FakeNameFinder({"김영희": "PERSON", "한빛분식": "ORG", "마포": "LOCATION"})
    result = mask("김영희 씨가 마포 한빛분식 에서 일했다", MaskMap(), finder)
    assert result.text == f"{NAME_MASK} 씨가 {LOCATION_MASK} {ORG_MASK} 에서 일했다"
    assert result.ner_replaced == 3


def test_ner_replaced_is_counted_with_dictionary_hits() -> None:
    finder = FakeNameFinder({"김영희": "PERSON", "이철수": "PERSON"})
    result = mask("홍길동 은 김영희 와 이철수 를 뽑았다", MAP, finder)
    assert result.text == f"사장님 은 {NAME_MASK} 와 {NAME_MASK} 를 뽑았다"
    assert result.ner_replaced == 2
    assert result.replaced == 3


def test_default_finder_comes_from_configure() -> None:
    finder = FakeNameFinder({"김영희": "PERSON"})
    try:
        configure(finder)
        result = mask("김영희 사장이 말하길", MaskMap())
    finally:
        configure(NoopNameFinder())
    assert result.text == f"{NAME_MASK} 사장이 말하길"
    assert finder.seen == ["김영희 사장이 말하길"]


def test_default_finder_finds_nothing() -> None:
    result = mask("김영희 사장이 말하길", MaskMap())
    assert result.text == "김영희 사장이 말하길"
    assert result.ner_replaced == 0
