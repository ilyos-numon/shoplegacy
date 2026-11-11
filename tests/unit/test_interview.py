from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from shoplegacy import question_tree
from shoplegacy.interview import (
    InterviewService,
    SessionClosedError,
    SessionLimitError,
    SessionNotFoundError,
    SessionTimeOverError,
    ShopNotFoundError,
    TreeExhaustedError,
)
from shoplegacy.main import create_app
from shoplegacy.providers import FakeShopLLM
from shoplegacy.question_tree import NUMBER_PROBE, TREES_DIR, FollowUp
from shoplegacy.store import Principal, ShopRow, Store

FIXTURES = Path(__file__).parent / "fixtures"
TRADE = "tree_min"
START = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class NoFollowUpLLM(FakeShopLLM):
    def follow_up(
        self, question: str, answer: str, candidates: list[FollowUp]
    ) -> FollowUp | None:
        return None


@pytest.fixture(autouse=True)
def _fixture_trees(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(question_tree, "TREES_DIR", FIXTURES)


@pytest.fixture
def store(app_dsn: str, org_principal: Principal) -> Iterator[Store]:
    instance = Store(app_dsn)
    instance.insert_shop(
        ShopRow("shop1", "테스트 식당", "김사장", TRADE, "office", 12, 2025, [], START)
    )
    yield instance
    instance.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(store: Store, clock: Clock) -> InterviewService:
    return InterviewService(store, FakeShopLLM(), clock=clock, session_seconds=600, planned=3)


def test_start_main_then_follow_up_then_next_main(service: InterviewService) -> None:
    session_id, first = service.start("shop1")
    assert (first.id, first.kind, first.axis) == ("loc-01", "main", "location")
    follow = service.answer(session_id, "역 앞이라 점심 손님이 많았어요")
    assert follow is not None and (follow.id, follow.kind) == ("loc-01-f1", "follow_up")
    nxt = service.answer(session_id, "저녁엔 한산했죠")
    assert nxt is not None and (nxt.id, nxt.kind) == ("sup-01", "main")
    view = service.view(session_id)
    assert view.items_today == 2 and view.items_total == 2
    assert view.status == "open" and view.remaining_seconds == 600


def test_unknown_shop_and_session(service: InterviewService) -> None:
    with pytest.raises(ShopNotFoundError):
        service.start("nope")
    with pytest.raises(SessionNotFoundError):
        service.answer("nope", "x")
    with pytest.raises(SessionNotFoundError):
        service.view("nope")


def test_blank_answer_rejected(service: InterviewService) -> None:
    session_id, _ = service.start("shop1")
    with pytest.raises(ValueError):
        service.answer(session_id, "   ")


def test_end_counts_and_closes(service: InterviewService) -> None:
    session_id, _ = service.start("shop1")
    service.answer(session_id, "하나")
    service.answer(session_id, "둘")
    assert service.end(session_id) == 2
    assert service.view(session_id).status == "ended"
    assert service.view(session_id).remaining_seconds == 0
    with pytest.raises(SessionClosedError):
        service.answer(session_id, "셋")
    with pytest.raises(SessionClosedError):
        service.end(session_id)


def test_time_over_after_grace(service: InterviewService, clock: Clock) -> None:
    session_id, _ = service.start("shop1")
    clock.now = START + timedelta(seconds=600 + 31)
    with pytest.raises(SessionTimeOverError):
        service.answer(session_id, "늦은 답")


def test_answer_after_deadline_within_grace_saved_returns_none(
    service: InterviewService, clock: Clock
) -> None:
    session_id, _ = service.start("shop1")
    clock.now = START + timedelta(seconds=610)
    assert service.answer(session_id, "마감 직후 답") is None
    assert service.view(session_id).items_today == 1


def test_resume_continues_and_closes_open_session(service: InterviewService) -> None:
    first_id, _ = service.start("shop1")
    service.answer(first_id, "입지 답")  # follow-up now pending
    second_id, question = service.start("shop1")  # closes the first session
    assert service.view(first_id).status == "ended"
    assert question.id == "sup-01"
    assert service.view(second_id).number == 2
    assert service.view(second_id).items_total == 1


def test_resume_repeats_unanswered_main(service: InterviewService) -> None:
    first_id, _ = service.start("shop1")
    service.end(first_id)
    _, question = service.start("shop1")
    assert question.id == "loc-01"


def test_session_limit(service: InterviewService) -> None:
    for _ in range(3):
        sid, _ = service.start("shop1")
        service.end(sid)
    with pytest.raises(SessionLimitError):
        service.start("shop1")


def test_tree_exhausted(store: Store, clock: Clock) -> None:
    service = InterviewService(store, NoFollowUpLLM(), clock=clock, planned=5)
    session_id, _ = service.start("shop1")
    last = None
    for i in range(12):
        last = service.answer(session_id, f"답 {i}")
    assert last is None
    assert service.view(session_id).question is None
    with pytest.raises(TreeExhaustedError):
        service.start("shop1")


def test_api_flow(
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
    client = TestClient(create_app(), headers=reviewer_headers)
    shop = {
        "name": "가상 국밥집",
        "owner_name": "홍사장",
        "trade": TRADE,
        "area_type": "station",
        "years": 20,
        "closed_year": 2025,
        "supplier_names": ["가상 정육상회"],
    }
    resp = client.post("/api/shops", json=shop)
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == {"shop_id", "trade", "area_type", "years", "closed_year", "region"}
    assert body["region"] is None
    assert "가상 국밥집" not in resp.text and "홍사장" not in resp.text
    assert "가상 정육상회" not in resp.text
    shop_id = body["shop_id"]
    assert client.get(f"/api/shops/{shop_id}").status_code == 200
    assert client.get("/api/shops/missing").status_code == 404
    bad = client.post("/api/shops", json={**shop, "trade": "no_such"})
    assert bad.status_code == 422

    resp = client.post("/api/sessions", json={"shop_id": shop_id})
    assert resp.status_code == 201
    started = resp.json()
    sid = started["session_id"]
    assert started["question"]["id"] == "loc-01" and started["number"] == 1
    assert client.post("/api/sessions", json={"shop_id": "missing"}).status_code == 404

    first = client.post(f"/api/sessions/{sid}/answer", json={"text": "역 앞이었어요"})
    assert first.status_code == 200
    assert first.json()["next_question"]["kind"] == "follow_up"
    second = client.post(f"/api/sessions/{sid}/answer", json={"text": "점심 장사였죠"})
    assert second.status_code == 200 and second.json()["items_today"] == 2
    blank = client.post(f"/api/sessions/{sid}/answer", json={"text": "  "})
    assert blank.status_code == 422

    got = client.get(f"/api/sessions/{sid}")
    assert got.status_code == 200 and got.json()["status"] == "open"
    end = client.post(f"/api/sessions/{sid}/end")
    assert end.status_code == 200
    body = end.json()
    assert body["session_id"] == sid and body["answered"] == 2 and body["card_id"]
    assert client.get(f"/api/cards/{body['card_id']}").status_code == 200
    assert client.post(f"/api/sessions/{sid}/end").status_code == 409
    assert client.post(f"/api/sessions/{sid}/answer", json={"text": "x"}).status_code == 409
    assert client.get("/api/sessions/missing").status_code == 404


def test_progress_without_sessions(service: InterviewService) -> None:
    progress = service.progress("shop1")
    assert (progress.shop_id, progress.trade) == ("shop1", TRADE)
    assert progress.sessions_done == 0 and progress.next_number == 1
    assert progress.nodes_done == 0 and progress.nodes_total > 0
    assert progress.planned == 3 and progress.open_session_id is None
    assert progress.reason == "ok" and progress.can_continue is True
    with pytest.raises(ShopNotFoundError):
        service.progress("nope")


def test_progress_after_ended_round(service: InterviewService) -> None:
    session_id, _ = service.start("shop1")
    open_progress = service.progress("shop1")
    assert open_progress.open_session_id == session_id
    service.answer(session_id, "역 앞이었어요")
    service.answer(session_id, "점심 장사였죠")
    service.end(session_id)
    progress = service.progress("shop1")
    assert progress.sessions_done == 1 and progress.next_number == 2
    assert progress.nodes_done == 1 and progress.open_session_id is None
    assert progress.reason == "ok" and progress.can_continue is True
    _, question = service.start("shop1")
    assert question.id == "sup-01"


def test_progress_does_not_close_open_session(service: InterviewService) -> None:
    session_id, _ = service.start("shop1")
    service.progress("shop1")
    assert service.view(session_id).status == "open"


def test_progress_limit(store: Store, clock: Clock) -> None:
    service = InterviewService(store, FakeShopLLM(), clock=clock, planned=5)
    for _ in range(5):
        service.start("shop1")
    progress = service.progress("shop1")
    assert progress.sessions_done == 5 and progress.planned == 5
    assert progress.reason == "limit" and progress.can_continue is False
    with pytest.raises(SessionLimitError):
        service.start("shop1")


def test_progress_exhausted(store: Store, clock: Clock) -> None:
    service = InterviewService(store, NoFollowUpLLM(), clock=clock, planned=5)
    session_id, _ = service.start("shop1")
    for i in range(12):
        service.answer(session_id, f"답 {i}")
    progress = service.progress("shop1")
    assert progress.nodes_done == progress.nodes_total
    assert progress.reason == "exhausted" and progress.can_continue is False
    with pytest.raises(TreeExhaustedError):
        service.start("shop1")


def test_api_shop_list_and_progress(
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
    client = TestClient(create_app(), headers=reviewer_headers)
    first = {
        "name": "가나다식당",
        "owner_name": "홍길동",
        "trade": TRADE,
        "area_type": "residential",
        "years": 18,
        "closed_year": 2025,
        "supplier_names": ["라마바정육"],
    }
    second = {**first, "name": "가상 분식집", "owner_name": "김가상", "supplier_names": []}
    first_id = client.post("/api/shops", json=first).json()["shop_id"]
    second_id = client.post("/api/shops", json=second).json()["shop_id"]

    resp = client.get("/api/shops")
    assert resp.status_code == 200
    shops = resp.json()
    assert len(shops) == 2
    assert [s["shop_id"] for s in shops] == [second_id, first_id]
    assert shops[0]["card_id"] is None and shops[0]["card_status"] is None
    assert shops[0]["sessions_done"] == 0 and shops[0]["next_number"] == 1
    assert shops[0]["can_continue"] is True and shops[0]["reason"] == "ok"
    for secret in ("가나다식당", "홍길동", "라마바정육", "가상 분식집", "김가상"):
        assert secret not in resp.text
    assert "name" not in shops[0] and "owner_name" not in shops[0]

    assert client.get("/api/shops/missing/progress").status_code == 404
    assert client.get("/api/shops/missing/progress").json()["detail"] == "shop not found"

    sid = client.post("/api/sessions", json={"shop_id": first_id}).json()["session_id"]
    client.post(f"/api/sessions/{sid}/answer", json={"text": "역 앞이었어요"})
    client.post(f"/api/sessions/{sid}/answer", json={"text": "점심 장사였죠"})
    card_id = client.post(f"/api/sessions/{sid}/end").json()["card_id"]
    prog = client.get(f"/api/shops/{first_id}/progress")
    assert prog.status_code == 200
    body = prog.json()
    assert body["sessions_done"] == 1 and body["next_number"] == 2
    assert body["nodes_done"] == 1 and body["open_session_id"] is None
    assert body["card_id"] == card_id and body["card_status"] == "draft"
    for secret in ("가나다식당", "홍길동", "라마바정육"):
        assert secret not in prog.text
    assert client.get(f"/api/shops/{first_id}").status_code == 200


def _restaurant_service(
    store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch, llm: FakeShopLLM | None = None
) -> InterviewService:
    monkeypatch.setattr(question_tree, "TREES_DIR", TREES_DIR)
    for shop_id, area in (("near-station", "station"), ("near-home", "residential")):
        store.insert_shop(
            ShopRow(shop_id, "가상 식당", "가상 사장", "restaurant", area, 10, 2025, [], START)
        )
    return InterviewService(
        store, llm or NoFollowUpLLM(), clock=clock, session_seconds=3600, planned=5
    )


def _walk(service: InterviewService, shop_id: str) -> list[str]:
    session_id, question = service.start(shop_id)
    seen = [question.id]
    nxt = service.answer(session_id, "답 1")
    while nxt is not None:
        seen.append(nxt.id)
        nxt = service.answer(session_id, "답 2")
    return seen


def test_station_shop_gets_branch_nodes_residential_does_not(
    store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _restaurant_service(store, clock, monkeypatch)
    station = _walk(service, "near-station")
    home = _walk(service, "near-home")
    branch = {"loc-03", "cus-03"}
    assert branch <= set(station)
    assert not branch & set(home)
    assert service.progress("near-station").nodes_total == len(
        [q for q in station if not q.endswith("-num")]
    )
    assert service.progress("near-home").nodes_total < service.progress("near-station").nodes_total


def _to_pricing(service: InterviewService, shop_id: str) -> str:
    session_id, question = service.start(shop_id)
    assert question.id == "loc-01"
    for _ in range(3):  # loc-01, sup-01, men-01 (no follow-ups)
        question = service.answer(session_id, "답")  # type: ignore[assignment]
    assert question.id == "pri-01"
    return session_id


def test_answer_without_number_gets_one_number_probe(
    store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _restaurant_service(store, clock, monkeypatch)
    session_id = _to_pricing(service, "near-home")
    probe = service.answer(session_id, "몇 해 지나서 조금 올렸어요")
    assert probe is not None
    assert (probe.id, probe.kind, probe.axis) == ("pri-01-num", "follow_up", "pricing")
    assert probe.text == NUMBER_PROBE
    nxt = service.answer(session_id, "글쎄요, 잘 기억이 안 나요")
    assert nxt is not None and nxt.id == "cus-01"


def test_answer_with_number_gets_no_number_probe(
    store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _restaurant_service(store, clock, monkeypatch)
    session_id = _to_pricing(service, "near-home")
    nxt = service.answer(session_id, "2015년에 500원 올렸어요")
    assert nxt is not None and nxt.id == "cus-01"


def test_number_probe_then_llm_follow_up(
    store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _restaurant_service(store, clock, monkeypatch, llm=FakeShopLLM())
    session_id, _ = service.start("near-home")
    for _ in range(6):  # three mains with one follow-up each
        service.answer(session_id, "답")
    assert service.view(session_id).question.id == "pri-01"  # type: ignore[union-attr]
    probe = service.answer(session_id, "조금 올렸어요")
    assert probe is not None and probe.id == "pri-01-num"
    follow = service.answer(session_id, "천 원쯤이요")
    assert follow is not None and follow.id == "pri-01-f1"
    nxt = service.answer(session_id, "다들 이해해 주셨어요")
    assert nxt is not None and nxt.id == "cus-01"


def _region_client(
    tmp_path: Path, app_dsn: str, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> TestClient:
    monkeypatch.setenv("SHOPLEGACY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", app_dsn)
    monkeypatch.setenv("SHOPLEGACY_JWT_SECRET", "test-secret")
    monkeypatch.setenv("SHOPLEGACY_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_STT_PROVIDER", "fake")
    return TestClient(create_app(), headers=headers)


REGION_SHOP = {
    "name": "가상 국밥집",
    "owner_name": "홍사장",
    "trade": TRADE,
    "area_type": "station",
    "years": 20,
    "closed_year": 2025,
}


def test_api_shop_region_is_stored_and_returned(
    tmp_path: Path,
    app_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    reviewer_headers: dict[str, str],
    admin_conn: psycopg.Connection,
) -> None:
    client = _region_client(tmp_path, app_dsn, monkeypatch, reviewer_headers)
    resp = client.post("/api/shops", json={**REGION_SHOP, "region_code": "1111000000"})
    assert resp.status_code == 201
    shop_id = resp.json()["shop_id"]
    assert resp.json()["region"] == "서울특별시 종로구"
    assert client.get(f"/api/shops/{shop_id}").json()["region"] == "서울특별시 종로구"
    row = admin_conn.execute(
        "SELECT region_code, region_sido, region_sigungu FROM public.shops WHERE shop_id = %s",
        (shop_id,),
    ).fetchone()
    assert row == ("1111000000", "서울특별시", "종로구")


def test_api_shop_region_sido_only_and_unknown(
    tmp_path: Path,
    app_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    reviewer_headers: dict[str, str],
) -> None:
    client = _region_client(tmp_path, app_dsn, monkeypatch, reviewer_headers)
    sejong = client.post("/api/shops", json={**REGION_SHOP, "region_code": "3600000000"})
    assert sejong.status_code == 201 and sejong.json()["region"] == "세종특별자치시"
    unknown = client.post("/api/shops", json={**REGION_SHOP, "region_code": "1234567890"})
    assert unknown.status_code == 422 and unknown.json()["detail"] == "unknown region"
    none = client.post("/api/shops", json=REGION_SHOP)
    assert none.status_code == 201 and none.json()["region"] is None
