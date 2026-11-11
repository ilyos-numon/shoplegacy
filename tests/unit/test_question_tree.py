import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from shoplegacy import question_tree
from shoplegacy.question_tree import (
    AXIS_LABELS,
    QuestionTree,
    UnknownTradeError,
    _build_tree,
    applicable_nodes,
    load_tree,
)
from shoplegacy.store import AreaType, ShopRow

FIXTURES = Path(__file__).parent / "fixtures"


def _raw() -> dict[str, Any]:
    with (FIXTURES / "tree_min.yaml").open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


def test_load_fixture_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(question_tree, "TREES_DIR", FIXTURES)
    tree = load_tree("tree_min")
    assert tree.trade == "tree_min"
    assert len(tree.nodes) == 12
    assert {n.axis for n in tree.nodes} == set(AXIS_LABELS)


def test_unknown_trade_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(question_tree, "TREES_DIR", FIXTURES)
    with pytest.raises(UnknownTradeError):
        load_tree("no_such_trade")
    with pytest.raises(UnknownTradeError):
        load_tree("../fixtures/tree_min")


def test_too_few_nodes_rejected() -> None:
    data = _raw()
    data["nodes"] = data["nodes"][:11]
    with pytest.raises(ValueError, match="at least 12"):
        _build_tree(data)


def test_missing_axis_rejected() -> None:
    data = _raw()
    data["axes"] = data["axes"][:5]
    with pytest.raises(ValueError, match="axes"):
        _build_tree(data)


def test_consecutive_axis_rejected() -> None:
    data = _raw()
    nodes = data["nodes"]
    nodes[1], nodes[6] = nodes[6], nodes[1]  # loc-01, loc-02 adjacent
    with pytest.raises(ValueError):
        _build_tree(data)


def test_bad_ids_rejected() -> None:
    data = _raw()
    bad = copy.deepcopy(data)
    bad["nodes"][0]["id"] = "sup-09"
    with pytest.raises(ValueError, match="invalid node id"):
        _build_tree(bad)
    bad = copy.deepcopy(data)
    bad["nodes"][0]["follow_ups"][0]["id"] = "loc-01-x1"
    with pytest.raises(ValueError, match="invalid follow-up id"):
        _build_tree(bad)
    bad = copy.deepcopy(data)
    bad["nodes"][6]["id"] = "loc-01"
    bad["nodes"][6]["follow_ups"][0]["id"] = "loc-01-f2"
    with pytest.raises(ValueError, match="duplicate"):
        _build_tree(bad)


def _shop(area_type: AreaType, trade: str = "tree_min") -> ShopRow:
    return ShopRow(
        "s1", "가상 가게", "가상 사장", trade, area_type, 10, 2025, [], datetime.now(UTC)
    )


def test_only_if_unknown_key_rejected() -> None:
    data = _raw()
    data["nodes"][6]["only_if"] = {"years": ["10"]}
    with pytest.raises(ValueError, match="only_if"):
        _build_tree(data)


def test_only_if_empty_values_rejected() -> None:
    data = _raw()
    data["nodes"][6]["only_if"] = {"area_type": []}
    with pytest.raises(ValueError, match="at least one value"):
        _build_tree(data)


def test_only_if_defaults_and_wants_number_parsed() -> None:
    data = _raw()
    data["nodes"][6]["only_if"] = {"area_type": ["station"], "trade": ["tree_min"]}
    data["nodes"][3]["wants_number"] = True
    tree = _build_tree(data)
    assert tree.nodes[0].only_if == {} and tree.nodes[0].wants_number is False
    assert tree.nodes[3].wants_number is True
    assert tree.nodes[6].only_if == {"area_type": ["station"], "trade": ["tree_min"]}


def test_applicable_nodes_filters_by_area_and_trade() -> None:
    data = _raw()
    data["nodes"][-1]["only_if"] = {"area_type": ["station", "office"]}
    data["nodes"][-2]["only_if"] = {"trade": ["cafe"]}
    tree = _build_tree(data)
    last, second_last = tree.nodes[-1].id, tree.nodes[-2].id
    station = [n.id for n in applicable_nodes(tree, _shop("station"))]
    assert last in station and second_last not in station
    assert len(station) == len(tree.nodes) - 1
    home = [n.id for n in applicable_nodes(tree, _shop("residential"))]
    assert last not in home and second_last not in home
    cafe = [n.id for n in applicable_nodes(tree, _shop("residential", trade="cafe"))]
    assert second_last in cafe and last not in cafe


def test_applicable_nodes_consecutive_axis_after_filter_raises() -> None:
    raw = _raw()
    # loc-02, sup-02 (station only), loc-03: hiding sup-02 puts two location nodes together.
    raw["nodes"][7]["only_if"] = {"area_type": ["station"]}
    raw["nodes"][8] = {
        "id": "loc-03",
        "axis": "location",
        "text": "입지 질문 3번입니다. 자세히 말씀해 주세요.",
        "follow_ups": [{"id": "loc-03-f1", "text": "입지 꼬리 질문 3번입니다."}],
    }
    tree = QuestionTree.model_validate(raw)
    assert len(applicable_nodes(tree, _shop("station"))) == len(tree.nodes)
    with pytest.raises(ValueError, match="consecutive"):
        applicable_nodes(tree, _shop("residential"))


def test_applicable_nodes_first_six_axes_after_filter_raises() -> None:
    data = _raw()
    data["nodes"][2]["only_if"] = {"area_type": ["office"]}
    tree = _build_tree(data)
    assert len(applicable_nodes(tree, _shop("office"))) == len(tree.nodes)
    with pytest.raises(ValueError, match="first 6"):
        applicable_nodes(tree, _shop("station"))
