"""Golden evaluation: synthetic interviews must yield masked, evidence-backed cards."""

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from shoplegacy.cards import CardService
from shoplegacy.interview import AnswerEntry
from shoplegacy.providers import FakeShopLLM
from shoplegacy.question_tree import Axis
from shoplegacy.store import AreaType, Principal, SessionRow, ShopRow, Store

CASES_DIR = Path(__file__).parent / "cases"
CASE_FILES = sorted(CASES_DIR.glob("g*.yaml"))
START = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
_PHONE = re.compile(r"0\d{1,2}-\d{3,4}-\d{4}")
_ADDRESS = "행복동 12-3"


@pytest.fixture
def store(app_dsn: str, org_principal: Principal) -> Iterator[Store]:
    instance = Store(app_dsn)
    yield instance
    instance.close()


def _seed(store: Store, case: dict[str, Any]) -> str:
    shop = case["shop"]
    shop_id = uuid.uuid4().hex
    area: AreaType = shop["area_type"]
    store.insert_shop(
        ShopRow(
            shop_id,
            shop["name"],
            shop["owner_name"],
            shop["trade"],
            area,
            shop["years"],
            shop["closed_year"],
            list(shop["supplier_names"]),
            START,
        )
    )
    session_id = uuid.uuid4().hex
    store.insert_session(
        SessionRow(
            session_id,
            shop_id,
            shop["trade"],
            1,
            START,
            START + timedelta(hours=1),
            START + timedelta(minutes=30),
            0,
            None,
        )
    )
    n = 0
    for axis, answers in case["answers"].items():
        for text in answers:
            n += 1
            store.add_answer(
                AnswerEntry(
                    session_id=session_id,
                    question_id=f"{axis}-{n}",
                    axis=axis,
                    question=f"{axis} 질문 {n}",
                    answer=text,
                    created_at=START + timedelta(seconds=n),
                    session_number=1,
                )
            )
    return shop_id


@pytest.mark.parametrize("path", CASE_FILES, ids=[p.stem for p in CASE_FILES])
def test_golden_case(store: Store, path: Path) -> None:
    case = yaml.safe_load(path.read_text(encoding="utf-8"))
    expect = case["expect"]
    axes: list[Axis] = list(case["answers"])
    assert len(axes) == 6
    assert all(len(case["answers"][axis]) >= 2 for axis in axes)

    card = CardService(store, FakeShopLLM()).rebuild_draft(_seed(store, case))
    dumped = card.model_dump_json()

    if expect["pii_free"]:
        assert card.warnings == []
        shop = case["shop"]
        for secret in (shop["name"], shop["owner_name"], *shop["supplier_names"], _ADDRESS):
            assert secret not in dumped
        assert _PHONE.search(dumped) is None
    assert card.quality.supported_ratio >= expect["min_supported_ratio"], (
        card.quality.supported_ratio,
        card.quality.reasons,
    )
    assert "거래처 A" in card.sections["suppliers"]
    print(f"GOLDEN_RATIO {path.stem} {card.quality.supported_ratio:.3f}")
    for axis in expect["nonempty_axes"]:
        assert card.sections[axis].strip(), axis
        assert card.evidence[axis], axis
