"""Golden set replayed from recorded Qwen responses: no API key, no network."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from shoplegacy.cards import CardService
from shoplegacy.config import DEFAULT_LLM_BASE_URL
from shoplegacy.providers import AXES, QwenShopLLM
from shoplegacy.store import Principal, Store
from tests.golden.cassette import use_cassette
from tests.golden.test_golden import _ADDRESS, _PHONE, CASE_FILES, _seed

MODEL = "qwen3.7-flash"
_RESULTS: list[dict[str, Any]] = []


@pytest.fixture
def store(app_dsn: str, org_principal: Principal) -> Iterator[Store]:
    instance = Store(app_dsn)
    yield instance
    instance.close()


@pytest.fixture(scope="module", autouse=True)
def _summary() -> Iterator[None]:
    yield
    ratios = [r["supported_ratio"] for r in _RESULTS]
    summary = {
        "cases": len(_RESULTS),
        "mean_supported_ratio": round(sum(ratios) / len(ratios), 3) if ratios else 0.0,
        "min_supported_ratio": min(ratios) if ratios else 0.0,
        "pii_leaks": sum(r["pii_leak"] for r in _RESULTS),
        "empty_axes": sum(r["empty_axes"] for r in _RESULTS),
        "model": MODEL,
    }
    print("QWEN_EVAL_SUMMARY " + json.dumps(summary, ensure_ascii=False))
    if summary["cases"] == len(CASE_FILES):
        assert summary["mean_supported_ratio"] >= 0.7, summary


@pytest.mark.parametrize("path", CASE_FILES, ids=lambda p: p.stem)
def test_recorded_case(store: Store, path: Path, record_property: Any) -> None:
    case = yaml.safe_load(path.read_text(encoding="utf-8"))
    llm = QwenShopLLM(DEFAULT_LLM_BASE_URL, MODEL, "recorded", 60.0)
    with use_cassette(path.stem, record=False):
        card = CardService(store, llm).rebuild_draft(_seed(store, case))
    dumped = card.model_dump_json()
    shop = case["shop"]
    secrets = (shop["name"], shop["owner_name"], *shop["supplier_names"], _ADDRESS)
    metrics = {
        "case": path.stem,
        "supported_ratio": round(card.quality.supported_ratio, 3),
        "warnings": len(card.warnings),
        "pii_leak": int(
            any(secret in dumped for secret in secrets) or _PHONE.search(dumped) is not None
        ),
        "empty_axes": sum(1 for a in AXES if not card.sections[a].strip()),
        "provider": llm.last_provider(),
    }
    print("QWEN_EVAL " + json.dumps(metrics, ensure_ascii=False))
    for key, value in metrics.items():
        record_property(key, value)
    _RESULTS.append(metrics)
    assert metrics["pii_leak"] == 0, metrics
    assert metrics["empty_axes"] == 0, metrics
    assert metrics["supported_ratio"] >= 0.5, (metrics, card.quality.reasons)
    assert metrics["provider"] == "qwen", metrics
