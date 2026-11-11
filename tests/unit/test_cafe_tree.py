from collections import Counter
from datetime import UTC, datetime

import pytest

from shoplegacy.question_tree import AXIS_LABELS, QuestionTree, applicable_nodes, load_tree
from shoplegacy.store import AreaType, ShopRow

MIN_CAFE_NODES = 15
MAX_TOTAL_QUESTIONS = 40
MIN_TEXT_LEN = 20
MAX_TEXT_LEN = 120
YO_ENDINGS = ("요?", "요.")
AREA_TYPES: list[AreaType] = ["residential", "station", "office", "other"]
CAFE_WORDS = ("원두", "테이크아웃", "좌석", "음료")


@pytest.fixture(scope="module")
def tree() -> QuestionTree:
    return load_tree("cafe")


def _texts(tree: QuestionTree) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for node in tree.nodes:
        items.append((node.id, node.text))
        items += [(f.id, f.text) for f in node.follow_ups]
    return items


def test_cafe_tree_loads(tree: QuestionTree) -> None:
    assert tree.trade == "cafe"
    assert tree.title
    assert tree.version == 1


def test_cafe_tree_size_and_axes(tree: QuestionTree) -> None:
    assert len(tree.nodes) >= MIN_CAFE_NODES
    assert set(tree.axes) == set(AXIS_LABELS)
    counts = Counter(n.axis for n in tree.nodes)
    for axis in AXIS_LABELS:
        assert counts[axis] >= 2, axis
    assert {n.axis for n in tree.nodes[:6]} == set(AXIS_LABELS)
    assert sum(1 + len(n.follow_ups) for n in tree.nodes) <= MAX_TOTAL_QUESTIONS


def test_cafe_tree_polite_yo_endings(tree: QuestionTree) -> None:
    for item_id, text in _texts(tree):
        assert MIN_TEXT_LEN <= len(text) <= MAX_TEXT_LEN, (item_id, len(text))
        assert text.endswith(YO_ENDINGS), (item_id, text)


def test_cafe_tree_has_cafe_content(tree: QuestionTree) -> None:
    joined = " ".join(text for _, text in _texts(tree))
    for word in CAFE_WORDS:
        assert word in joined, word
    assert sum(1 for n in tree.nodes if n.wants_number) >= 1


def test_cafe_tree_applicable_for_every_area(tree: QuestionTree) -> None:
    for area in AREA_TYPES:
        shop = ShopRow("s1", "가상 카페", "가상 사장", "cafe", area, 5, 2025, [], datetime.now(UTC))
        assert applicable_nodes(tree, shop)
