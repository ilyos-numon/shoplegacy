"""Real Presidio + spaCy Korean NER; skips when the optional ner extra is missing."""

import importlib.util

import pytest

from shoplegacy.masking import MaskMap, mask
from shoplegacy.ner import PresidioNameFinder


def test_presidio_masks_person_name() -> None:
    if importlib.util.find_spec("presidio_analyzer") is None:
        pytest.skip("presidio not installed")
    result = mask("김영희 사장이 말하길", MaskMap(), PresidioNameFinder("ko_core_news_md"))
    assert "김영희" not in result.text
    assert result.ner_replaced >= 1
