import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shoplegacy.question_tree import AXIS_LABELS, QuestionTree, applicable_nodes, load_tree
from shoplegacy.store import AreaType, ShopRow

MIN_TREE_NODES = 12
MAX_TREE_NODES = 18
MAX_TOTAL_QUESTIONS = 40
MIN_TEXT_LEN = 20
MAX_TEXT_LEN = 120
ALLOWED_ENDINGS = ("요?", "요.", "까?", "세요.")

# Generic identifying patterns that must never appear in question texts.
BUILTIN_FORBIDDEN = [
    r"(\d{1,3}\.){3}\d{1,3}",  # IPv4
    r"https?://",  # URLs
    r"[\w.+-]+@[\w-]+\.[\w.]+",  # e-mail
    r"\d{2,3}-\d{3,4}-\d{4}",  # phone number
    r"주식회사|\(주\)|㈜",  # company markers
    r"[A-Z][A-Z0-9]+-\d+",  # issue keys
]


def _rules_file() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "doc" / "portfolio" / "sanitize-rules.md"
        if candidate.is_file():
            return candidate
    return None


def _forbidden_patterns() -> list[str]:
    patterns = list(BUILTIN_FORBIDDEN)
    rules = _rules_file()
    if rules is not None:
        text = rules.read_text(encoding="utf-8")
        block = re.search(r"```sanitize-patterns\n(.*?)```", text, re.DOTALL)
        if block:
            patterns += [line.strip() for line in block.group(1).splitlines() if line.strip()]
    return patterns


@pytest.fixture(scope="module")
def tree() -> QuestionTree:
    return load_tree("restaurant")


def _texts(tree: QuestionTree) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for node in tree.nodes:
        items.append((node.id, node.text))
        items += [(f.id, f.text) for f in node.follow_ups]
    return items


def test_tree_loads(tree: QuestionTree) -> None:
    assert tree.trade == "restaurant"
    assert tree.title
    assert tree.version >= 1
    assert set(tree.axes) == set(AXIS_LABELS)


def test_node_count_in_range(tree: QuestionTree) -> None:
    assert MIN_TREE_NODES <= len(tree.nodes) <= MAX_TREE_NODES


def test_follow_ups_per_node(tree: QuestionTree) -> None:
    for node in tree.nodes:
        assert 1 <= len(node.follow_ups) <= 2, node.id


def test_total_questions_within_e2e_limit(tree: QuestionTree) -> None:
    total = sum(1 + len(n.follow_ups) for n in tree.nodes)
    assert total <= MAX_TOTAL_QUESTIONS


def test_every_axis_has_two_nodes(tree: QuestionTree) -> None:
    counts = Counter(n.axis for n in tree.nodes)
    for axis in AXIS_LABELS:
        assert counts[axis] >= 2, axis


def test_first_six_nodes_cover_all_axes(tree: QuestionTree) -> None:
    assert {n.axis for n in tree.nodes[:6]} == set(AXIS_LABELS)


def test_text_length_and_polite_endings(tree: QuestionTree) -> None:
    for item_id, text in _texts(tree):
        assert MIN_TEXT_LEN <= len(text) <= MAX_TEXT_LEN, (item_id, len(text))
        assert text.endswith(ALLOWED_ENDINGS), (item_id, text)


def test_no_forbidden_names(tree: QuestionTree) -> None:
    patterns = _forbidden_patterns()
    assert len(patterns) >= len(BUILTIN_FORBIDDEN)
    for item_id, text in _texts(tree):
        for pattern in patterns:
            assert not re.search(pattern, text, re.IGNORECASE), (item_id, pattern)
    for pattern in patterns:
        assert not re.search(pattern, tree.title, re.IGNORECASE), pattern


AREA_TYPES: list[AreaType] = ["residential", "station", "office", "other"]


def _shop(area_type: AreaType) -> ShopRow:
    return ShopRow(
        "s1", "가상 식당", "가상 사장", "restaurant", area_type, 10, 2025, [], datetime.now(UTC)
    )


def test_version_two_branches_and_number_nodes(tree: QuestionTree) -> None:
    assert tree.version == 2
    assert sum(1 for n in tree.nodes if n.only_if) >= 2
    assert sum(1 for n in tree.nodes if n.wants_number) >= 3


def test_first_nodes_unchanged_for_existing_flows(tree: QuestionTree) -> None:
    assert [n.id for n in tree.nodes[:6]] == [
        "loc-01",
        "sup-01",
        "men-01",
        "pri-01",
        "cus-01",
        "fai-01",
    ]
    assert all(not n.only_if for n in tree.nodes[:6])


def test_applicable_for_every_area_type(tree: QuestionTree) -> None:
    sizes = {area: len(applicable_nodes(tree, _shop(area))) for area in AREA_TYPES}
    assert sizes["station"] == len(tree.nodes)
    assert sizes["residential"] < sizes["station"]
