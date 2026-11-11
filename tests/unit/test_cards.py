import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from shoplegacy import question_tree
from shoplegacy.cards import (
    CardNotCleanError,
    CardNotFoundError,
    CardPublishedError,
    CardService,
    CardStatus,
    CardTags,
    DefaultGate,
    GateFailedError,
    InvalidTransitionError,
)
from shoplegacy.evidence import Evidence
from shoplegacy.interview import AnswerEntry, InterviewService, ShopNotFoundError
from shoplegacy.main import create_app
from shoplegacy.masking import MaskMap
from shoplegacy.providers import FakeShopLLM, ShopLLM
from shoplegacy.question_tree import Axis
from shoplegacy.store import (
    ANON,
    PRINCIPAL,
    CardRow,
    ConsentRow,
    OwnerReviewRow,
    Principal,
    ShopRow,
    Store,
)

FIXTURES = Path(__file__).parent / "fixtures"
START = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
AXES: list[Axis] = ["location", "suppliers", "menu", "pricing", "customers", "failures"]
SHOP_NAME = "가나다식당"
OWNER = "홍길동"
SUPPLIER = "정다운상회"
PHONE = "010-0000-0000"
SECRET_ANSWER = f"{SHOP_NAME} {OWNER} {PHONE} {SUPPLIER} 이야기"


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def store(
    app_dsn: str, org_principal: Principal, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Store]:
    monkeypatch.setattr(question_tree, "TREES_DIR", FIXTURES)
    instance = Store(app_dsn)
    instance.insert_shop(
        ShopRow("shop1", SHOP_NAME, OWNER, "tree_min", "office", 12, 2025, [SUPPLIER], START)
    )
    yield instance
    instance.close()


def _interview(store: Store, answers: int) -> None:
    service = InterviewService(store, FakeShopLLM(), clock=Clock())
    session_id, _ = service.start("shop1")
    for _ in range(answers):
        service.answer(session_id, SECRET_ANSWER)
    service.end(session_id)


def _assert_clean(text: str) -> None:
    for secret in (SHOP_NAME, OWNER, PHONE, SUPPLIER):
        assert secret not in text


def test_rebuild_draft_masks_answers_by_axis(store: Store) -> None:
    _interview(store, 12)
    cards = CardService(store, FakeShopLLM(), clock=Clock())
    card = cards.rebuild_draft("shop1")
    assert card.status == "draft"
    assert card.sessions == 1
    assert set(card.sections) == set(AXES)
    assert all(card.sections[axis] for axis in AXES)
    assert "우리 가게" in card.sections["location"]
    assert "거래처 A" in card.sections["suppliers"]
    assert card.warnings == []
    assert card.tags.trade == "tree_min" and card.tags.years == 12
    _assert_clean(card.model_dump_json())


class SpyLLM(FakeShopLLM):
    def __init__(self) -> None:
        super().__init__()
        self.received: list[dict[Axis, list[str]]] = []

    def draft_sections(
        self, answers_by_axis: dict[Axis, list[str]], tags: CardTags
    ) -> dict[Axis, str]:
        self.received.append({axis: list(texts) for axis, texts in answers_by_axis.items()})
        return super().draft_sections(answers_by_axis, tags)


def test_llm_receives_only_masked_answers(store: Store) -> None:
    service = InterviewService(store, FakeShopLLM(), clock=Clock())
    session_id, _ = service.start("shop1")
    for _ in range(4):
        service.answer(session_id, f"{SECRET_ANSWER} 행복동 12-3")
    service.end(session_id)
    spy = SpyLLM()
    CardService(store, spy, clock=Clock()).rebuild_draft("shop1")
    assert len(spy.received) == 1
    texts = [t for answers in spy.received[0].values() for t in answers]
    assert len(texts) == 4
    for text in texts:
        for secret in (SHOP_NAME, OWNER, PHONE, SUPPLIER, "행복동 12-3"):
            assert secret not in text
        for token in ("우리 가게", "사장님", "거래처 A", "[전화번호]", "[주소]"):
            assert token in text


def test_rebuild_ignores_open_session_answers(store: Store) -> None:
    service = InterviewService(store, FakeShopLLM(), clock=Clock())
    session_id, _ = service.start("shop1")
    service.answer(session_id, "아직 진행 중")
    card = CardService(store, FakeShopLLM()).rebuild_draft("shop1")
    assert card.sessions == 0
    assert all(text == "" for text in card.sections.values())


def test_rebuild_unknown_shop(store: Store) -> None:
    with pytest.raises(ShopNotFoundError):
        CardService(store, FakeShopLLM()).rebuild_draft("missing")


def test_second_rebuild_reuses_card_id(store: Store) -> None:
    _interview(store, 2)
    cards = CardService(store, FakeShopLLM(), clock=Clock())
    first = cards.rebuild_draft("shop1")
    _interview(store, 2)
    second = cards.rebuild_draft("shop1")
    assert second.card_id == first.card_id
    assert second.sessions == 2


def test_rebuild_after_publish_keeps_sections(store: Store) -> None:
    _interview(store, 2)
    cards = CardService(store, FakeShopLLM(), clock=Clock())
    card = cards.rebuild_draft("shop1")
    _grant(store)
    published = cards.publish(card.card_id)
    _interview(store, 4)
    again = cards.rebuild_draft("shop1")
    assert again.card_id == card.card_id
    assert again.status == "published"
    assert again.sections == published.sections
    assert again.sessions == 2


def test_update_sections_masks_and_warns(store: Store) -> None:
    _interview(store, 2)
    cards = CardService(store, FakeShopLLM(), clock=Clock(), evidence_builder=FakeBuilder())
    card = cards.rebuild_draft("shop1")
    _grant(store)
    sections = {axis: f"{axis} 내용" for axis in AXES}
    sections["location"] = f"{SHOP_NAME} 는 행복동 12-3, 행복초등학교 앞"
    updated = cards.update_sections(card.card_id, sections)
    assert updated.sections["location"] == "우리 가게 는 [주소], 행복초등학교 앞"
    assert updated.warnings == ["행복초등학교"]
    with pytest.raises(CardNotCleanError) as info:
        cards.publish(card.card_id)
    assert info.value.warnings == ["행복초등학교"]
    sections["location"] = "학교 앞"
    clean = cards.update_sections(card.card_id, sections)
    assert clean.warnings == []
    assert cards.publish(card.card_id).status == "published"
    assert [c.card_id for c in cards.list_published()] == [card.card_id]


def test_update_sections_requires_all_axes(store: Store) -> None:
    _interview(store, 2)
    cards = CardService(store, FakeShopLLM())
    card = cards.rebuild_draft("shop1")
    with pytest.raises(ValueError):
        cards.update_sections(card.card_id, {"location": "x"})


def test_missing_card(store: Store) -> None:
    cards = CardService(store, FakeShopLLM())
    with pytest.raises(CardNotFoundError):
        cards.get("missing")
    with pytest.raises(CardNotFoundError):
        cards.publish("missing")
    assert cards.list_published() == []


def test_card_api_flow(
    tmp_path: Path,
    app_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    reviewer_headers: dict[str, str],
) -> None:
    monkeypatch.setenv("SHOPLEGACY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", app_dsn)
    monkeypatch.setenv("SHOPLEGACY_JWT_SECRET", "test-secret")
    monkeypatch.setenv("SHOPLEGACY_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_STT_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_REQUIRE_CONSENT", "false")
    app = create_app()
    # Reviewer-written sections are not drawn from the answers: all-supported fake builder.
    state = app.state.shop
    state.cards = CardService(
        state.store, state.llm, evidence_builder=FakeBuilder(), settings=state.settings
    )
    client = TestClient(app, headers=reviewer_headers)
    shop = {
        "name": SHOP_NAME,
        "owner_name": OWNER,
        "trade": "restaurant",
        "area_type": "residential",
        "years": 20,
        "closed_year": 2025,
        "supplier_names": [SUPPLIER],
    }
    shop_id = client.post("/api/shops", json=shop).json()["shop_id"]
    sid = client.post("/api/sessions", json={"shop_id": shop_id}).json()["session_id"]
    for _ in range(40):
        resp = client.post(f"/api/sessions/{sid}/answer", json={"text": SECRET_ANSWER})
        assert resp.status_code == 200
        if resp.json()["next_question"] is None:
            break
    end = client.post(f"/api/sessions/{sid}/end")
    assert end.status_code == 200
    card_id = end.json()["card_id"]
    assert card_id

    got = client.get(f"/api/cards/{card_id}")
    assert got.status_code == 200
    body = got.json()
    assert body["status"] == "draft" and body["warnings"] == []
    assert all(body["sections"][axis] for axis in AXES)
    _assert_clean(got.text)
    assert client.get("/api/cards/missing").status_code == 404
    assert client.get("/api/cards?status=published").json() == []
    assert client.get("/api/cards?status=draft").status_code == 422

    sections = dict(body["sections"])
    sections["location"] = "행복아파트 단지 안"
    put = client.put(f"/api/cards/{card_id}", json={"sections": sections})
    assert put.status_code == 200 and put.json()["warnings"] == ["행복아파트"]
    blocked = client.post(f"/api/cards/{card_id}/publish")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "gate_failed"
    assert "warnings" in blocked.json()["detail"]["reasons"]
    assert client.get(f"/api/cards/{card_id}").json()["warnings"] == ["행복아파트"]
    partial = client.put(f"/api/cards/{card_id}", json={"sections": {"location": "x"}})
    assert partial.status_code == 422
    assert client.put("/api/cards/missing", json={"sections": sections}).status_code == 404
    assert client.post("/api/cards/missing/publish").status_code == 404

    sections["location"] = f"{OWNER} 사장이 고른 주택가 골목"
    put = client.put(f"/api/cards/{card_id}", json={"sections": sections})
    assert put.status_code == 200 and put.json()["warnings"] == []
    _assert_clean(put.text)
    published = client.post(f"/api/cards/{card_id}/publish")
    assert published.status_code == 200 and published.json()["status"] == "published"
    listed = client.get("/api/cards?status=published")
    assert [c["card_id"] for c in listed.json()] == [card_id]
    _assert_clean(listed.text)


def _publish_for(store: Store, cards: CardService, shop_id: str) -> str:
    service = InterviewService(store, FakeShopLLM(), clock=Clock())
    session_id, _ = service.start(shop_id)
    for _ in range(2):
        service.answer(session_id, SECRET_ANSWER)
    service.end(session_id)
    card = cards.rebuild_draft(shop_id)
    return cards.publish(card.card_id).card_id


def test_list_published_filters_by_trade_and_area(store: Store) -> None:
    for shop_id, area in (("shop_res", "residential"), ("shop_sta", "station")):
        store.insert_shop(
            ShopRow(shop_id, SHOP_NAME, OWNER, "tree_min", area, 10, 2025, [SUPPLIER], START)
        )
    cards = CardService(store, FakeShopLLM(), clock=Clock(), settings=OPEN_GATE)
    residential = _publish_for(store, cards, "shop_res")
    station = _publish_for(store, cards, "shop_sta")
    only_station = cards.list_published(area_type="station")
    assert [c.card_id for c in only_station] == [station]
    assert cards.list_published(trade="cafe") == []
    assert [c.card_id for c in cards.list_published(trade="tree_min")] == [station, residential]
    assert [c.card_id for c in cards.list_published()] == [station, residential]


def test_list_published_ignores_drafts(store: Store) -> None:
    _interview(store, 2)
    cards = CardService(store, FakeShopLLM(), clock=Clock())
    cards.rebuild_draft("shop1")
    assert cards.list_published() == []
    assert cards.list_published(trade="tree_min", area_type="office") == []


def test_card_api_filters(
    tmp_path: Path,
    app_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    reviewer_headers: dict[str, str],
) -> None:
    monkeypatch.setenv("SHOPLEGACY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", app_dsn)
    monkeypatch.setenv("SHOPLEGACY_JWT_SECRET", "test-secret")
    monkeypatch.setenv("SHOPLEGACY_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_STT_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_REQUIRE_CONSENT", "false")
    client = TestClient(create_app(), headers=reviewer_headers)
    published: list[str] = []
    for area in ("residential", "station"):
        shop = {
            "name": SHOP_NAME,
            "owner_name": OWNER,
            "trade": "restaurant",
            "area_type": area,
            "years": 20,
            "closed_year": 2025,
            "supplier_names": [SUPPLIER],
        }
        shop_id = client.post("/api/shops", json=shop).json()["shop_id"]
        sid = client.post("/api/sessions", json={"shop_id": shop_id}).json()["session_id"]
        assert client.post(f"/api/sessions/{sid}/answer", json={"text": "골목 장사"}).status_code == 200
        card_id = client.post(f"/api/sessions/{sid}/end").json()["card_id"]
        resp = client.post(f"/api/cards/{card_id}/publish")
        assert resp.status_code == 200
        published.append(card_id)

    assert client.get("/api/cards?status=published&area_type=mall").status_code == 422
    station = client.get("/api/cards?status=published&area_type=station")
    assert station.status_code == 200
    items = station.json()
    assert [c["card_id"] for c in items] == [published[1]]
    assert all(c["tags"]["area_type"] == "station" for c in items)
    assert client.get("/api/cards?trade=cafe").json() == []
    both = client.get("/api/cards?status=published&trade=restaurant").json()
    assert [c["card_id"] for c in both] == [published[1], published[0]]


# --- status transitions, quality gate, versions, sources -----------------------

ALL_STATUSES: list[CardStatus] = ["draft", "on_hold", "rejected", "published", "unpublished"]
ALLOWED: set[tuple[str, str]] = {
    ("draft", "on_hold"),
    ("draft", "rejected"),
    ("draft", "published"),
    ("on_hold", "draft"),
    ("on_hold", "rejected"),
    ("on_hold", "published"),
    ("rejected", "draft"),
    ("published", "unpublished"),
    ("unpublished", "draft"),
    ("unpublished", "published"),
}
OPEN_GATE = DefaultGate(require_consent=False)


class FakeBuilder:
    """Evidence builder stand-in: one claim per non-empty axis, support set per axis."""

    def __init__(self, unsupported: dict[Axis, int] | None = None, claims: int = 2) -> None:
        self.unsupported = unsupported or {}
        self.claims = claims
        self.calls = 0

    def __call__(
        self,
        llm: ShopLLM,
        sections: dict[Axis, str],
        answers: list[AnswerEntry],
        mask_map: MaskMap,
    ) -> dict[Axis, list[Evidence]]:
        self.calls += 1
        out: dict[Axis, list[Evidence]] = {}
        for axis in AXES:
            if not sections.get(axis):
                out[axis] = []
                continue
            bad = self.unsupported.get(axis, 0)
            out[axis] = [
                Evidence(claim=f"{axis}-{i}", answer_id=None, quote="q", supported=i >= bad)
                for i in range(self.claims)
            ]
        return out


def _service(
    store: Store, builder: FakeBuilder | None = None, settings: DefaultGate = OPEN_GATE
) -> CardService:
    return CardService(
        store, FakeShopLLM(), clock=Clock(), evidence_builder=builder or FakeBuilder(),
        settings=settings,
    )


def _force_status(store: Store, card_id: str, status: str) -> None:
    row = store.get_card(card_id)
    assert row is not None
    row.status = status
    store.upsert_card(row)


def _grant(store: Store, kind: str = "publication") -> ConsentRow:
    row = ConsentRow(
        consent_id=uuid.uuid4().hex,
        org_id="",
        shop_id="shop1",
        kind=kind,
        method="verbal",
        note=None,
        granted_by=PRINCIPAL.get().user_id,
        granted_at=START,
        revoked_at=None,
        revoke_reason=None,
    )
    store.insert_consent(row)
    return row


def _audit_publishes(pg_dsn: str, card_id: str) -> list[int]:
    with psycopg.connect(pg_dsn) as conn:
        rows = conn.execute(
            "SELECT new_json->>'version' FROM audit_log WHERE action = 'card.publish' "
            "AND row_id = %s ORDER BY id",
            (card_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


@pytest.mark.parametrize("src", ALL_STATUSES)
@pytest.mark.parametrize("dst", ALL_STATUSES)
def test_transition_table(store: Store, src: CardStatus, dst: CardStatus) -> None:
    _interview(store, 12)
    cards = _service(store)
    card = cards.rebuild_draft("shop1")
    _force_status(store, card.card_id, src)
    if (src, dst) in ALLOWED:
        moved = cards.transition(card.card_id, dst, "memo", PRINCIPAL.get())
        assert moved.status == dst and moved.note == "memo"
        assert cards.get(card.card_id).status == dst
    else:
        with pytest.raises(InvalidTransitionError) as info:
            cards.transition(card.card_id, dst, None, PRINCIPAL.get())
        assert (info.value.from_status, info.value.to_status) == (src, dst)
        assert cards.get(card.card_id).status == src


def test_gate_reason_warnings(store: Store) -> None:
    _interview(store, 12)
    cards = _service(store)
    card = cards.rebuild_draft("shop1")
    sections = dict(card.sections)
    sections["location"] = "행복초등학교 앞"
    edited = cards.update_sections(card.card_id, sections)
    assert edited.quality.reasons == ["warnings"] and not edited.quality.gate_ok
    with pytest.raises(GateFailedError) as info:
        cards.transition(card.card_id, "published", None, PRINCIPAL.get())
    assert info.value.reasons == ["warnings"]
    assert info.value.warnings == ["행복초등학교"]


def test_gate_reason_missing_axis_only_when_required(store: Store) -> None:
    _interview(store, 12)
    card = _service(store).rebuild_draft("shop1")
    sections = dict(card.sections)
    sections["pricing"] = ""
    sections["failures"] = "  "
    relaxed = _service(store).update_sections(card.card_id, sections)
    assert relaxed.quality.missing_axes == ["pricing", "failures"]
    assert relaxed.quality.gate_ok and relaxed.quality.reasons == []
    strict = _service(store, settings=DefaultGate(require_all_axes=True, require_consent=False))
    quality = strict.get(card.card_id).quality
    assert quality.reasons == ["missing_axis:pricing", "missing_axis:failures"]
    with pytest.raises(GateFailedError):
        strict.transition(card.card_id, "published", None, PRINCIPAL.get())


def test_gate_reason_low_support_per_axis(store: Store) -> None:
    _interview(store, 12)
    cards = _service(store, FakeBuilder(unsupported={"menu": 1, "customers": 2}))
    card = cards.rebuild_draft("shop1")
    assert card.quality.reasons == ["low_support:menu:0.50", "low_support:customers:0.00"]
    assert card.quality.supported_ratio == pytest.approx(9 / 12)
    assert card.evidence["menu"][0].supported is False
    lenient = _service(store, settings=DefaultGate(min_supported_ratio=0.5, require_consent=False))
    assert lenient.get(card.card_id).quality.reasons == ["low_support:customers:0.00"]


def _no_evidence(
    llm: ShopLLM, sections: dict[Axis, str], answers: list[AnswerEntry], mask_map: MaskMap
) -> dict[Axis, list[Evidence]]:
    return {axis: [] for axis in AXES}


def test_gate_low_support_when_evidence_missing(store: Store) -> None:
    _interview(store, 12)
    cards = CardService(
        store, FakeShopLLM(), clock=Clock(), evidence_builder=_no_evidence, settings=OPEN_GATE
    )
    card = cards.rebuild_draft("shop1")  # builder finds no evidence at all
    assert card.quality.supported_ratio == 0.0
    assert card.quality.reasons == [f"low_support:{axis}:0.00" for axis in AXES]


def test_gate_reason_consent_missing(store: Store) -> None:
    _interview(store, 12)
    cards = _service(store, settings=DefaultGate())
    card = cards.rebuild_draft("shop1")
    assert card.quality.reasons == ["consent_missing:publication"]
    with pytest.raises(GateFailedError) as info:
        cards.transition(card.card_id, "published", None, PRINCIPAL.get())
    assert info.value.reasons == ["consent_missing:publication"]
    _grant(store, "recording")
    assert cards.get(card.card_id).quality.reasons == ["consent_missing:publication"]
    consent = _grant(store)
    assert cards.get(card.card_id).quality.gate_ok
    published = cards.transition(card.card_id, "published", None, PRINCIPAL.get())
    assert published.status == "published"
    cards.transition(card.card_id, "unpublished", None, PRINCIPAL.get())
    consent.revoked_at = START
    consent.revoke_reason = "철회"
    store.update_consent(consent)
    with pytest.raises(GateFailedError):
        cards.transition(card.card_id, "published", None, PRINCIPAL.get())


def test_publish_snapshots_versions_and_audits(store: Store, pg_dsn: str) -> None:
    _interview(store, 12)
    cards = _service(store)
    card = cards.rebuild_draft("shop1")
    assert card.version == 0 and cards.versions(card.card_id) == []
    first = cards.transition(card.card_id, "published", None, PRINCIPAL.get())
    assert first.version == 1
    cards.transition(card.card_id, "unpublished", "고칠 곳 있음", PRINCIPAL.get())
    cards.transition(card.card_id, "draft", None, PRINCIPAL.get())
    sections = dict(first.sections)
    sections["menu"] = "새 메뉴 이야기"
    cards.update_sections(card.card_id, sections)
    second = cards.publish(card.card_id)
    assert second.version == 2 and second.status == "published"
    versions = cards.versions(card.card_id)
    assert [v.version for v in versions] == [1, 2]
    assert all(v.status == "published" for v in versions)
    assert versions[0].sections == first.sections
    assert versions[1].sections["menu"] == "새 메뉴 이야기"
    rows = store.list_card_versions(card.card_id)
    assert rows[0].created_by == PRINCIPAL.get().user_id
    assert _audit_publishes(pg_dsn, card.card_id) == [1, 2]


def test_failed_gate_keeps_version_and_status(store: Store, pg_dsn: str) -> None:
    _interview(store, 12)
    cards = _service(store, FakeBuilder(unsupported={"location": 2}))
    card = cards.rebuild_draft("shop1")
    with pytest.raises(GateFailedError):
        cards.publish(card.card_id)
    after = cards.get(card.card_id)
    assert (after.status, after.version) == ("draft", 0)
    assert cards.versions(card.card_id) == []
    assert _audit_publishes(pg_dsn, card.card_id) == []


def test_non_publish_transitions_do_not_snapshot(store: Store) -> None:
    _interview(store, 12)
    cards = _service(store)
    card = cards.rebuild_draft("shop1")
    cards.transition(card.card_id, "on_hold", "확인 필요", PRINCIPAL.get())
    held = cards.transition(card.card_id, "rejected", "근거 부족", PRINCIPAL.get())
    assert held.note == "근거 부족" and held.version == 0
    assert cards.versions(card.card_id) == []


def test_regenerate_redrafts_and_keeps_version(store: Store) -> None:
    _interview(store, 12)
    builder = FakeBuilder()
    cards = _service(store, builder)
    card = cards.rebuild_draft("shop1")
    cards.publish(card.card_id)
    with pytest.raises(CardPublishedError):
        cards.regenerate(card.card_id)
    cards.transition(card.card_id, "unpublished", "다시", PRINCIPAL.get())
    sections = dict(card.sections)
    sections["menu"] = "손으로 고친 문장"
    _force_status(store, card.card_id, "draft")
    cards.update_sections(card.card_id, sections)
    _force_status(store, card.card_id, "on_hold")
    calls = builder.calls
    again = cards.regenerate(card.card_id)
    assert again.status == "draft" and again.version == 1 and again.note == "다시"
    assert again.sections == card.sections
    assert builder.calls == calls + 1
    with pytest.raises(CardNotFoundError):
        cards.regenerate("missing")


def test_rebuild_leaves_unpublished_untouched(store: Store) -> None:
    _interview(store, 2)
    cards = _service(store)
    card = cards.rebuild_draft("shop1")
    cards.publish(card.card_id)
    cards.transition(card.card_id, "unpublished", None, PRINCIPAL.get())
    _interview(store, 6)
    again = cards.rebuild_draft("shop1")
    assert again.status == "unpublished"
    assert again.sections == card.sections
    assert again.sessions == 2 and again.version == 1


def test_sources_are_masked(store: Store) -> None:
    _interview(store, 3)
    cards = _service(store)
    card = cards.rebuild_draft("shop1")
    sources = cards.sources(card.card_id)
    assert len(sources.answers) == 3
    assert [a.session_number for a in sources.answers] == [1, 1, 1]
    assert all(a.answer_id is not None for a in sources.answers)
    for item in sources.answers:
        assert "우리 가게" in item.answer_masked and "[전화번호]" in item.answer_masked
    _assert_clean(sources.model_dump_json())
    with pytest.raises(CardNotFoundError):
        cards.sources("missing")
    with pytest.raises(CardNotFoundError):
        cards.versions("missing")


def test_card_status_api(
    tmp_path: Path,
    app_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    reviewer_headers: dict[str, str],
    org_fixture: tuple[str, str, str, str],
) -> None:
    monkeypatch.setenv("SHOPLEGACY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", app_dsn)
    monkeypatch.setenv("SHOPLEGACY_JWT_SECRET", "test-secret")
    monkeypatch.setenv("SHOPLEGACY_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_STT_PROVIDER", "fake")
    app = create_app()
    state = app.state.shop
    client = TestClient(app, headers=reviewer_headers)
    shop = {
        "name": SHOP_NAME,
        "owner_name": OWNER,
        "trade": "restaurant",
        "area_type": "residential",
        "years": 20,
        "closed_year": 2025,
        "supplier_names": [SUPPLIER],
    }
    shop_id = client.post("/api/shops", json=shop).json()["shop_id"]
    sid = client.post("/api/sessions", json={"shop_id": shop_id}).json()["session_id"]
    for _ in range(3):
        assert client.post(f"/api/sessions/{sid}/answer", json={"text": SECRET_ANSWER}).status_code == 200
    card_id = client.post(f"/api/sessions/{sid}/end").json()["card_id"]

    # Default app: fake-provider evidence and REQUIRE_CONSENT=true -> the gate refuses.
    blocked = client.post(f"/api/cards/{card_id}/publish")
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert detail["code"] == "gate_failed"
    assert detail["reasons"][-1] == "consent_missing:publication"
    assert all(r.startswith("low_support:") for r in detail["reasons"][:-1])

    bad = client.post(f"/api/cards/{card_id}/status", json={"status": "unpublished"})
    assert (bad.status_code, bad.json()) == (
        409,
        {"detail": {"code": "invalid_transition", "from": "draft", "to": "unpublished"}},
    )
    assert client.post(f"/api/cards/{card_id}/status", json={"status": "gone"}).status_code == 422
    hold = client.post(f"/api/cards/{card_id}/status", json={"status": "on_hold", "note": "보류"})
    assert hold.status_code == 200
    assert (hold.json()["status"], hold.json()["note"]) == ("on_hold", "보류")

    state.cards = CardService(
        state.store, state.llm, evidence_builder=FakeBuilder(), settings=OPEN_GATE
    )
    regen = client.post(f"/api/cards/{card_id}/regenerate")
    assert regen.status_code == 200 and regen.json()["status"] == "draft"
    assert regen.json()["quality"]["gate_ok"] is True
    published = client.post(f"/api/cards/{card_id}/status", json={"status": "published"})
    assert published.status_code == 200
    assert (published.json()["status"], published.json()["version"]) == ("published", 1)
    regen = client.post(f"/api/cards/{card_id}/regenerate")
    assert (regen.status_code, regen.json()) == (409, {"detail": "card is published"})

    versions = client.get(f"/api/cards/{card_id}/versions")
    assert versions.status_code == 200
    assert [(v["version"], v["status"]) for v in versions.json()] == [(1, "published")]
    assert set(versions.json()[0]["sections"]) == set(AXES)

    sources = client.get(f"/api/cards/{card_id}/sources")
    assert sources.status_code == 200
    answers = sources.json()["answers"]
    assert len(answers) == 3
    assert set(answers[0]) == {"answer_id", "session_number", "axis", "question", "answer_masked"}
    _assert_clean(sources.text)

    for path in ("versions", "sources"):
        assert client.get(f"/api/cards/missing/{path}").status_code == 404
    assert client.post("/api/cards/missing/regenerate").status_code == 404
    assert client.post("/api/cards/missing/status", json={"status": "draft"}).status_code == 404
    anon = TestClient(app)
    assert anon.get(f"/api/cards/{card_id}/sources").status_code == 401
    org_id, _, _, interviewer_token = org_fixture
    interviewer = {"Authorization": f"Bearer {interviewer_token}", "X-Org-Id": org_id}
    assert anon.get(f"/api/cards/{card_id}/versions", headers=interviewer).status_code == 403
    assert (
        anon.post(f"/api/cards/{card_id}/status", json={"status": "unpublished"}, headers=interviewer)
    ).status_code == 403


# --- reviewer sentences, LLM provider, owner decision, public card ----------------


class ProviderLLM(FakeShopLLM):
    """Fake drafts that report a given provider (as a fallback or quota wrapper would)."""

    def __init__(self, provider: str) -> None:
        super().__init__()
        self.provider = provider

    def last_provider(self) -> str:
        return self.provider


def _owner_review(store: Store, card_id: str, decision: str) -> None:
    store.add_owner_review(
        OwnerReviewRow(
            review_id=uuid.uuid4().hex,
            org_id="",
            shop_id="shop1",
            card_id=card_id,
            card_version=0,
            token_id=uuid.uuid4().hex,
            decision=decision,
            comment="상호는 빼 주세요" if decision == "request_changes" else None,
            created_at=START,
        )
    )


def test_reviewer_sentence_marked_and_left_out_of_ratio(store: Store) -> None:
    _interview(store, 12)
    cards = CardService(store, FakeShopLLM(), clock=Clock(), settings=OPEN_GATE)
    card = cards.rebuild_draft("shop1")
    before = card.quality.supported_ratio
    assert card.quality.flags == []
    assert all(e.source == "answer" for items in card.evidence.values() for e in items)
    sections = dict(card.sections)
    sections["menu"] = "검수자가 덧붙인 설명이다. " + sections["menu"]
    edited = cards.update_sections(card.card_id, sections)
    menu = edited.evidence["menu"]
    assert menu[0].claim == "검수자가 덧붙인 설명이다."
    assert (menu[0].source, menu[0].supported, menu[0].answer_id) == ("reviewer", False, None)
    assert [e.source for e in menu[1:]] == ["answer"] * (len(menu) - 1)
    assert edited.quality.flags == ["reviewer_sentences:1"]
    assert edited.quality.supported_ratio == pytest.approx(before)


def test_axis_written_by_reviewer_only_passes_ratio(store: Store) -> None:
    _interview(store, 12)
    cards = CardService(store, FakeShopLLM(), clock=Clock(), settings=OPEN_GATE)
    card = cards.rebuild_draft("shop1")
    sections = dict(card.sections)
    sections["location"] = "검수자가 새로 쓴 문단이다. 두 번째 문장이다."
    edited = cards.update_sections(card.card_id, sections)
    assert [e.source for e in edited.evidence["location"]] == ["reviewer", "reviewer"]
    assert not any(r.startswith("low_support:location") for r in edited.quality.reasons)
    assert "reviewer_sentences:2" in edited.quality.flags


def test_llm_provider_and_draft_are_stored(store: Store) -> None:
    _interview(store, 12)
    card = CardService(store, FakeShopLLM(), clock=Clock()).rebuild_draft("shop1")
    assert card.llm_provider == "fake"
    row = store.get_card(card.card_id)
    assert row is not None
    assert row.llm_provider == "fake"
    assert json.loads(row.draft_sections_json) == card.sections


def test_llm_fallback_flag(store: Store) -> None:
    _interview(store, 12)
    card = CardService(store, ProviderLLM("fallback"), clock=Clock()).rebuild_draft("shop1")
    assert card.llm_provider == "fallback"
    assert "llm_fallback" in card.quality.flags
    assert not any("llm_fallback" in r for r in card.quality.reasons)


def test_llm_quota_flag(store: Store) -> None:
    _interview(store, 12)
    card = CardService(store, ProviderLLM("quota"), clock=Clock()).rebuild_draft("shop1")
    assert card.llm_provider == "quota"
    assert "llm_quota_exceeded" in card.quality.flags
    assert "llm_fallback" not in card.quality.flags


def test_owner_approval_gate_only_when_required(store: Store) -> None:
    _interview(store, 12)
    card = _service(store).rebuild_draft("shop1")
    assert "owner_approval_missing" not in card.quality.reasons
    strict = _service(
        store, settings=DefaultGate(require_consent=False, require_owner_approval=True)
    )
    gated = strict.get(card.card_id)
    assert gated.quality.reasons == ["owner_approval_missing"]
    with pytest.raises(GateFailedError) as err:
        strict.publish(card.card_id)
    assert err.value.reasons == ["owner_approval_missing"]
    _owner_review(store, card.card_id, "approve")
    assert strict.get(card.card_id).quality.reasons == []
    assert strict.publish(card.card_id).status == "published"


def test_owner_decision_exposed_and_changes_flagged(store: Store) -> None:
    _interview(store, 12)
    cards = _service(store)
    card = cards.rebuild_draft("shop1")
    assert card.owner_decision is None
    _owner_review(store, card.card_id, "request_changes")
    got = cards.get(card.card_id)
    assert got.owner_decision is not None
    assert (got.owner_decision.decision, got.owner_decision.comment) == (
        "request_changes",
        "상호는 빼 주세요",
    )
    assert "owner_changes_requested" in got.quality.flags
    assert got.quality.gate_ok is True  # display only by default


def test_list_published_passes_region_and_q_to_store(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, dict[str, object]]] = []
    original = store.list_cards

    def spy(status: str, **kwargs: Any) -> list[CardRow]:
        seen.append((status, kwargs))
        return original(status, **kwargs)

    monkeypatch.setattr(store, "list_cards", spy)
    cards = CardService(store, FakeShopLLM(), clock=Clock())
    assert cards.list_published(region_code="11", q="종로") == []
    assert seen == [("published", {"region_code": "11", "q": "종로"})]


def test_public_card_api_hides_quality_and_shows_feedback(
    tmp_path: Path,
    app_dsn: str,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    reviewer_headers: dict[str, str],
) -> None:
    monkeypatch.setenv("SHOPLEGACY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", app_dsn)
    monkeypatch.setenv("SHOPLEGACY_JWT_SECRET", "test-secret")
    monkeypatch.setenv("SHOPLEGACY_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_STT_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_REQUIRE_CONSENT", "false")
    app = create_app()
    client = TestClient(app, headers=reviewer_headers)
    shop = {
        "name": SHOP_NAME,
        "owner_name": OWNER,
        "trade": "restaurant",
        "area_type": "residential",
        "years": 20,
        "closed_year": 2025,
        "supplier_names": [SUPPLIER],
    }
    shop_id = client.post("/api/shops", json=shop).json()["shop_id"]
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE shops SET region_code = '1111000000', region_sido = '서울특별시', "
            "region_sigungu = '종로구' WHERE shop_id = %s",
            (shop_id,),
        )
    sid = client.post("/api/sessions", json={"shop_id": shop_id}).json()["session_id"]
    assert client.post(f"/api/sessions/{sid}/answer", json={"text": "골목 장사"}).status_code == 200
    card_id = client.post(f"/api/sessions/{sid}/end").json()["card_id"]
    assert client.post(f"/api/cards/{card_id}/publish").status_code == 200
    assert app.state.shop.store.add_feedback(card_id, "up", "fp-1") is True

    own = client.get(f"/api/cards/{card_id}").json()
    assert own["llm_provider"] == "fake"
    assert own["quality"]["flags"] == []
    assert own["owner_decision"] is None
    assert own["tags"]["region"] == "서울특별시 종로구"

    anon = TestClient(app)
    public = anon.get(f"/api/cards/{card_id}")
    assert public.status_code == 200
    body = public.json()
    for hidden in ("quality", "note", "warnings", "llm_provider", "owner_decision"):
        assert hidden not in body
    assert "consent_missing" not in public.text
    assert body["feedback"] == {"up": 1, "down": 0}
    assert body["tags"]["region"] == "서울특별시 종로구"
    assert set(body) == {
        "card_id", "shop_id", "status", "sections", "tags", "evidence", "version",
        "updated_at", "sessions", "feedback",
    }

    listed = anon.get("/api/cards?status=published").json()
    assert [c["card_id"] for c in listed] == [card_id]
    assert "quality" not in listed[0] and listed[0]["feedback"] == {"up": 1, "down": 0}
    assert [c["card_id"] for c in anon.get("/api/cards?region=11").json()] == [card_id]
    assert anon.get("/api/cards?region=26").json() == []
    assert [c["card_id"] for c in anon.get("/api/cards?q=종로").json()] == [card_id]
    assert anon.get("/api/cards?q=부산").json() == []
    assert anon.get("/api/cards?region=abc").status_code == 422


def test_public_tags_take_sigungu_from_v2_tuple(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    sigungu: list[str | None] = ["종로구"]
    monkeypatch.setattr(
        store,
        "public_card_tags",
        lambda shop_id: ("restaurant", "residential", 20, 2025, sigungu[0]),
    )
    monkeypatch.setattr(
        store, "public_card_region", lambda card_id: ("1111000000", "서울특별시", None)
    )
    cards = CardService(store, FakeShopLLM(), clock=Clock())
    token = PRINCIPAL.set(ANON)
    try:
        tags = cards._card_tags("shop1", "card1")
        assert (tags.trade, tags.area_type, tags.years, tags.closed_year) == (
            "restaurant",
            "residential",
            20,
            2025,
        )
        assert tags.region == "서울특별시 종로구"
        sigungu[0] = None
        assert cards._card_tags("shop1", "card1").region == "서울특별시"
    finally:
        PRINCIPAL.reset(token)
