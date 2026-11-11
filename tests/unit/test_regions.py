import dataclasses
import hashlib
import hmac
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shoplegacy import regions
from shoplegacy.main import create_app
from shoplegacy.store import ANON, PRINCIPAL

SHOP = {
    "name": "가상 분식집",
    "owner_name": "박사장",
    "trade": "restaurant",
    "area_type": "station",
    "years": 15,
    "closed_year": 2025,
}
JONGNO = "1111000000"
HAEUNDAE = "2635000000"


# --- dataset ---------------------------------------------------------------


def test_sido_list_matches_source() -> None:
    sido = regions.list_sido()
    # The 2026-06-30 source has 16 sido: 광주 and 전남 became 전남광주통합특별시.
    assert len(sido) == 16
    names = {r.sido for r in sido}
    assert {"서울특별시", "세종특별자치시", "제주특별자치도", "전남광주통합특별시"} <= names
    assert all(r.sigungu is None and r.code.endswith("00000000") for r in sido)


def test_sigungu_count_and_every_sigungu_has_a_sido() -> None:
    rows = regions.load_regions()
    sigungu = [r for r in rows if r.sigungu is not None]
    assert len(sigungu) >= 220
    sido_names = {r.sido for r in regions.list_sido()}
    assert all(r.sido in sido_names for r in sigungu)
    assert len({r.code for r in rows}) == len(rows)


def test_sejong_has_no_sigungu_level_requirement() -> None:
    sejong = regions.find("3600000000")
    assert sejong is not None and sejong.sigungu is None
    assert len(regions.list_sigungu("세종특별자치시")) <= 1
    assert regions.find("0000000000") is None


def test_search_and_lookup() -> None:
    hits = regions.search("종로")
    assert any(r.code == JONGNO for r in hits)
    assert regions.find(JONGNO) == regions.Region(JONGNO, "서울특별시", "종로구")
    assert len(regions.search("시")) == 50
    assert regions.search("   ") == []
    assert [r.code for r in regions.list_sigungu("서울특별시")][:1] == [JONGNO]
    assert regions.list_sigungu("없는도") == []


# --- API -------------------------------------------------------------------


@pytest.fixture
def app(tmp_path: Path, app_dsn: str, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("SHOPLEGACY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", app_dsn)
    monkeypatch.setenv("SHOPLEGACY_JWT_SECRET", "test-secret")
    monkeypatch.setenv("SHOPLEGACY_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_STT_PROVIDER", "fake")
    return create_app()


def _shop(client: TestClient, region_code: str | None) -> str:
    resp = client.post("/api/shops", json={**SHOP, "region_code": region_code})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["shop_id"])


def _card(admin_conn: psycopg.Connection, shop_id: str, status: str) -> str:
    card_id = f"card-{shop_id}"
    admin_conn.execute(
        "INSERT INTO public.cards (card_id, org_id, shop_id, status, sections_json, "
        "warnings_json, sessions, updated_at) "
        "SELECT %s, org_id, shop_id, %s, '{}', '[]', 1, %s FROM public.shops WHERE shop_id = %s",
        (card_id, status, datetime.now(UTC), shop_id),
    )
    return card_id


def test_regions_api(app: FastAPI) -> None:
    client = TestClient(app)
    sido = client.get("/api/regions")
    assert sido.status_code == 200
    assert len(sido.json()) == len(regions.list_sido())
    assert {"code": "1100000000", "sido": "서울특별시", "sigungu": None} in sido.json()
    seoul = client.get("/api/regions", params={"sido": "서울특별시"}).json()
    assert {"code": JONGNO, "sido": "서울특별시", "sigungu": "종로구"} in seoul
    assert len(seoul) == 25
    found = client.get("/api/regions/search", params={"q": "종로"})
    assert found.status_code == 200 and found.json()[0]["code"] == JONGNO
    assert len(client.get("/api/regions/search", params={"q": "구"}).json()) == 50
    assert client.get("/api/regions/search", params={"q": ""}).status_code == 422
    assert client.get("/api/regions/search").status_code == 422


def test_unknown_region_code_is_422(app: FastAPI, reviewer_headers: dict[str, str]) -> None:
    client = TestClient(app, headers=reviewer_headers)
    bad = client.post("/api/shops", json={**SHOP, "region_code": "9999900000"})
    assert bad.status_code == 422 and bad.json()["detail"] == "unknown region"


def test_feedback_counts_once_per_fingerprint(
    app: FastAPI, reviewer_headers: dict[str, str], admin_conn: psycopg.Connection
) -> None:
    card_id = _card(
        admin_conn, _shop(TestClient(app, headers=reviewer_headers), JONGNO), "published"
    )
    anon = TestClient(app, headers={"User-Agent": "browser-a"})
    first = anon.post(f"/api/cards/{card_id}/feedback", json={"vote": "up"})
    assert first.status_code == 201
    assert first.json() == {"up": 1, "down": 0, "counted": True}
    again = anon.post(f"/api/cards/{card_id}/feedback", json={"vote": "down"})
    assert again.status_code == 201
    assert again.json() == {"up": 1, "down": 0, "counted": False}
    other = TestClient(app, headers={"User-Agent": "browser-a"}, client=("2001:db8::2", 50000))
    third = other.post(f"/api/cards/{card_id}/feedback", json={"vote": "down"})
    assert third.json() == {"up": 1, "down": 1, "counted": True}
    bad = anon.post(f"/api/cards/{card_id}/feedback", json={"vote": "meh"})
    assert bad.status_code == 422


def test_feedback_rotating_user_agent_does_not_count_twice(
    app: FastAPI, reviewer_headers: dict[str, str], admin_conn: psycopg.Connection
) -> None:
    card_id = _card(
        admin_conn, _shop(TestClient(app, headers=reviewer_headers), JONGNO), "published"
    )
    url = f"/api/cards/{card_id}/feedback"
    first = TestClient(app, headers={"User-Agent": "browser-a"}).post(url, json={"vote": "up"})
    assert first.json() == {"up": 1, "down": 0, "counted": True}
    second = TestClient(app, headers={"User-Agent": "browser-b"}).post(url, json={"vote": "up"})
    assert second.status_code == 201
    assert second.json() == {"up": 1, "down": 0, "counted": False}


def test_feedback_post_is_limited_to_ten_per_hour(
    app: FastAPI, reviewer_headers: dict[str, str], admin_conn: psycopg.Connection
) -> None:
    card_id = _card(
        admin_conn, _shop(TestClient(app, headers=reviewer_headers), JONGNO), "published"
    )
    anon = TestClient(app)
    url = f"/api/cards/{card_id}/feedback"
    for i in range(10):
        resp = anon.post(url, json={"vote": "up"}, headers={"User-Agent": f"agent-{i}"})
        assert resp.status_code == 201, (i, resp.text)
    eleventh = anon.post(url, json={"vote": "up"}, headers={"User-Agent": "agent-10"})
    assert eleventh.status_code == 429
    assert "rate limit exceeded" in eleventh.json()["detail"]
    elsewhere = TestClient(app, client=("2001:db8::3", 50000)).post(url, json={"vote": "up"})
    assert elsewhere.status_code == 201
    assert anon.get("/api/regions").status_code == 200


def test_feedback_on_draft_or_missing_card_is_404(
    app: FastAPI, reviewer_headers: dict[str, str], admin_conn: psycopg.Connection
) -> None:
    card_id = _card(admin_conn, _shop(TestClient(app, headers=reviewer_headers), None), "draft")
    anon = TestClient(app)
    draft = anon.post(f"/api/cards/{card_id}/feedback", json={"vote": "up"})
    assert draft.status_code == 404
    missing = anon.post("/api/cards/no-such-card/feedback", json={"vote": "up"})
    assert missing.status_code == 404
    count = admin_conn.execute("SELECT count(*) FROM public.card_feedback").fetchone()
    assert count is not None and count[0] == 0


def test_published_cards_filter_by_region(
    app: FastAPI, reviewer_headers: dict[str, str], admin_conn: psycopg.Connection
) -> None:
    client = TestClient(app, headers=reviewer_headers)
    seoul = _card(admin_conn, _shop(client, JONGNO), "published")
    busan = _card(admin_conn, _shop(client, HAEUNDAE), "published")
    _card(admin_conn, _shop(client, JONGNO), "draft")
    store = app.state.shop.store
    token = PRINCIPAL.set(ANON)
    try:
        assert [c.card_id for c in store.list_cards("published", region_code="11")] == [seoul]
        assert [c.card_id for c in store.list_cards("published", region_code=HAEUNDAE)] == [busan]
        assert [c.card_id for c in store.list_cards("published", q="종로")] == [seoul]
        assert {c.card_id for c in store.list_cards("published")} == {seoul, busan}
        assert store.list_cards("published", region_code="36") == []
    finally:
        PRINCIPAL.reset(token)


def _expected_fingerprint(secret: str, host: str) -> str:
    key = hmac.new(secret.encode(), b"shoplegacy-feedback-fingerprint", hashlib.sha256).digest()
    raw = f"{host}|{date.today().isoformat()}".encode()
    return hmac.new(key, raw, hashlib.sha256).hexdigest()[:32]


def test_feedback_fingerprint_uses_feedback_secret(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    reviewer_headers: dict[str, str],
    admin_conn: psycopg.Connection,
) -> None:
    monkeypatch.setenv("SHOPLEGACY_FEEDBACK_SECRET", "feedback-only-secret")
    app = create_app()
    card_id = _card(
        admin_conn, _shop(TestClient(app, headers=reviewer_headers), JONGNO), "published"
    )
    resp = TestClient(app).post(f"/api/cards/{card_id}/feedback", json={"vote": "up"})
    assert resp.status_code == 201, resp.text
    row = admin_conn.execute(
        "SELECT fingerprint FROM public.card_feedback WHERE card_id = %s", (card_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == _expected_fingerprint("feedback-only-secret", "testclient")
    assert row[0] != _expected_fingerprint("test-secret", "testclient")


def test_feedback_without_feedback_secret_is_unavailable(
    app: FastAPI, reviewer_headers: dict[str, str], admin_conn: psycopg.Connection
) -> None:
    card_id = _card(
        admin_conn, _shop(TestClient(app, headers=reviewer_headers), JONGNO), "published"
    )
    state = app.state.shop
    state.settings = dataclasses.replace(state.settings, feedback_secret="")
    resp = TestClient(app).post(f"/api/cards/{card_id}/feedback", json={"vote": "up"})
    assert resp.status_code == 503 and resp.json()["detail"] == "feedback_unavailable"
    count = admin_conn.execute("SELECT count(*) FROM public.card_feedback").fetchone()
    assert count is not None and count[0] == 0
