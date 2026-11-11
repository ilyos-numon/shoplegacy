from shoplegacy.evidence import Evidence, build_evidence, split_sentences, supported_ratio
from shoplegacy.masking import MaskMap


def _ev(supported: bool) -> Evidence:
    return Evidence(claim="c", answer_id=1, quote="q", supported=supported)


def test_split_sentences_korean_and_punctuation() -> None:
    text = "역 앞이라 손님이 많다.  점심엔 줄이 길어요. 왜일까? 좋아!\n끝"
    assert split_sentences(text) == ["역 앞이라 손님이 많다.", "점심엔 줄이 길어요.", "왜일까?", "좋아!", "끝"]


def test_split_sentences_drops_empty() -> None:
    assert split_sentences("   ") == []
    assert split_sentences("") == []


def test_supported_ratio_over_given_axes() -> None:
    evidence = {"location": [_ev(True), _ev(False)], "menu": [_ev(True)], "pricing": [_ev(False)]}
    assert supported_ratio(evidence, ["location", "menu"]) == 2 / 3  # type: ignore[arg-type]


def test_supported_ratio_zero_when_empty() -> None:
    assert supported_ratio({}, ["location"]) == 0.0


def test_build_evidence_stub_returns_empty_axes() -> None:
    result = build_evidence(None, {}, [], MaskMap(shop_name="x", owner_name="y"))  # type: ignore[arg-type]
    assert set(result) == {"location", "suppliers", "menu", "pricing", "customers", "failures"}
    assert all(v == [] for v in result.values())
