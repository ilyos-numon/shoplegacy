"""Six-axis question tree models and loader (same shape and rules as handoff's)."""

import re
from collections import Counter
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from handoff.question_tree import FollowUp
from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from shoplegacy.store import ShopRow

Axis = Literal["location", "suppliers", "menu", "pricing", "customers", "failures"]
AXIS_LABELS: dict[str, str] = {
    "location": "입지",
    "suppliers": "거래처",
    "menu": "메뉴",
    "pricing": "가격",
    "customers": "손님",
    "failures": "실패",
}
AXIS_PREFIX: dict[str, str] = {
    "location": "loc",
    "suppliers": "sup",
    "menu": "men",
    "pricing": "pri",
    "customers": "cus",
    "failures": "fai",
}
MIN_NODES = 12
MIN_NODES_PER_AXIS = 2
ONLY_IF_KEYS = frozenset({"area_type", "trade"})
NUMBER_PROBE = "대략 숫자로 말씀해 주시면 더 도움이 돼요. 어느 정도였을까요?"

TREES_DIR = Path(__file__).parent / "question_trees"
_TRADE_RE = re.compile(r"^[a-z_]+$")

__all__ = [
    "AXIS_LABELS",
    "AXIS_PREFIX",
    "MIN_NODES",
    "MIN_NODES_PER_AXIS",
    "NUMBER_PROBE",
    "ONLY_IF_KEYS",
    "Axis",
    "FollowUp",
    "QuestionNode",
    "QuestionTree",
    "UnknownTradeError",
    "applicable_nodes",
    "load_tree",
]


class QuestionNode(BaseModel):
    id: str
    axis: Axis
    text: str
    follow_ups: list[FollowUp] = Field(min_length=1)
    only_if: dict[str, list[str]] = {}
    wants_number: bool = False

    @field_validator("only_if")
    @classmethod
    def _check_only_if(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        unknown = set(value) - ONLY_IF_KEYS
        if unknown:
            raise ValueError(
                f"only_if keys must be in {sorted(ONLY_IF_KEYS)}, got {sorted(unknown)}"
            )
        for key, allowed in value.items():
            if not allowed:
                raise ValueError(f"only_if[{key!r}] must list at least one value")
        return value


class QuestionTree(BaseModel):
    trade: str
    title: str
    version: int
    axes: list[Axis]
    nodes: list[QuestionNode]


class UnknownTradeError(LookupError):
    """Raised when no question tree exists for a trade."""


def _check_constraints(tree: QuestionTree) -> None:
    all_axes = set(AXIS_LABELS)
    if set(tree.axes) != all_axes or len(tree.axes) != len(all_axes):
        raise ValueError(f"axes must be exactly {sorted(all_axes)}")
    if len(tree.nodes) < MIN_NODES:
        raise ValueError(f"tree needs at least {MIN_NODES} nodes, got {len(tree.nodes)}")
    counts: Counter[str] = Counter(node.axis for node in tree.nodes)
    for axis in all_axes:
        if counts[axis] < MIN_NODES_PER_AXIS:
            raise ValueError(f"axis {axis} needs at least {MIN_NODES_PER_AXIS} nodes")
    seen: set[str] = set()
    for node in tree.nodes:
        if not re.fullmatch(rf"{AXIS_PREFIX[node.axis]}-\d{{2}}", node.id):
            raise ValueError(f"invalid node id {node.id!r} for axis {node.axis}")
        for follow_up in node.follow_ups:
            if not re.fullmatch(rf"{re.escape(node.id)}-f\d+", follow_up.id):
                raise ValueError(f"invalid follow-up id {follow_up.id!r} under {node.id}")
        for item_id in [node.id] + [f.id for f in node.follow_ups]:
            if item_id in seen:
                raise ValueError(f"duplicate id {item_id!r}")
            seen.add(item_id)
    _check_order(tree.nodes)


def _check_order(nodes: list[QuestionNode]) -> None:
    """No two consecutive nodes share an axis; the first six cover all axes."""
    all_axes = set(AXIS_LABELS)
    for prev, cur in pairwise(nodes):
        if prev.axis == cur.axis:
            raise ValueError(f"consecutive nodes {prev.id} and {cur.id} share axis {cur.axis}")
    first = len(all_axes)
    if {node.axis for node in nodes[:first]} != all_axes:
        raise ValueError(f"first {first} nodes must cover all six axes")


def applicable_nodes(tree: QuestionTree, shop: "ShopRow") -> list[QuestionNode]:
    """Nodes whose ``only_if`` matches the shop; ValueError if the result breaks the order rules."""
    attrs = {"area_type": shop.area_type, "trade": shop.trade}
    nodes = [
        node
        for node in tree.nodes
        if all(attrs[key] in allowed for key, allowed in node.only_if.items())
    ]
    _check_order(nodes)
    return nodes


def _build_tree(data: Any) -> QuestionTree:
    """Validate raw tree data (schema + constraints). Raises ValueError on violation."""
    tree = QuestionTree.model_validate(data)
    _check_constraints(tree)
    return tree


def load_tree(trade: str) -> QuestionTree:
    if not _TRADE_RE.fullmatch(trade):
        raise UnknownTradeError(trade)
    path = TREES_DIR / f"{trade}.yaml"
    if not path.is_file():
        raise UnknownTradeError(trade)
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return _build_tree(data)
