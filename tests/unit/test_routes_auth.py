"""Auth, roles and org ownership on the existing routes."""

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shoplegacy import pages, routes_cards, routes_interview, routes_orgs, routes_stt
from shoplegacy.app_state import build_state
from shoplegacy.config import Settings
from shoplegacy.routers import ALL_ROUTERS

SECRET = "test-secret-that-is-long-enough-for-hs256"
SHOP = {
    "name": "테스트가게",
    "owner_name": "김사장",
    "trade": "restaurant",
    "area_type": "office",
    "years": 12,
    "closed_year": 2025,
    "supplier_names": [],
}
Headers = dict[str, str]


@pytest.fixture
def client(app_dsn: str, tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        provider="fake",
        stt_provider="fake",
        data_dir=tmp_path,
        database_url=app_dsn,
        jwt_secret=SECRET,
        require_consent=False,
    )
    state = build_state(settings)
    app = FastAPI()
    app.state.shop = state
    for router in (routes_orgs, routes_interview, routes_stt, routes_cards):
        app.include_router(router.router, prefix="/api")
    app.include_router(pages.router)
    try:
        yield TestClient(app)
    finally:
        state.store.close()


def _h(user_id: str, email: str, org_id: str | None = None) -> Headers:
    claims: dict[str, Any] = {
        "sub": user_id,
        "email": email,
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + 300,
    }
    headers = {"Authorization": f"Bearer {jwt.encode(claims, SECRET, algorithm='HS256')}"}
    if org_id:
        headers["X-Org-Id"] = org_id
    return headers


@pytest.fixture
def user(pg_dsn: str, make_user: Callable[[str, str], str]) -> Callable[[str], Headers]:
    def _new(email: str) -> Headers:
        return _h(make_user(pg_dsn, email), email)

    return _new


def _org(client: TestClient, owner: Headers, name: str) -> str:
    r = client.post("/api/orgs", json={"name": name}, headers=owner)
    assert r.status_code == 201
    org_id: str = r.json()["org_id"]
    return org_id


def _add(client: TestClient, owner: Headers, org_id: str, email: str, role: str) -> None:
    r = client.post(
        f"/api/orgs/{org_id}/members", json={"email": email, "role": role}, headers=owner
    )
    assert r.status_code == 201


def _draft_card(client: TestClient, h: Headers) -> tuple[str, str]:
    r = client.post("/api/shops", json=SHOP, headers=h)
    assert r.status_code == 201, r.text
    shop_id: str = r.json()["shop_id"]
    r = client.post("/api/sessions", json={"shop_id": shop_id}, headers=h)
    assert r.status_code == 201, r.text
    session_id = r.json()["session_id"]
    r = client.post(
        f"/api/sessions/{session_id}/answer",
        json={"text": "역 앞이라 점심 손님이 많았습니다"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    r = client.post(f"/api/sessions/{session_id}/end", headers=h)
    assert r.status_code == 200, r.text
    card_id: str = r.json()["card_id"]
    return shop_id, card_id


def test_anonymous_create_shop_is_401(client: TestClient) -> None:
    r = client.post("/api/shops", json=SHOP)
    assert (r.status_code, r.json()) == (401, {"detail": "login required"})
    assert client.get("/api/shops").status_code == 401
    assert client.post("/api/sessions", json={"shop_id": "x"}).status_code == 401
    assert client.get("/api/sessions/x").status_code == 401


def test_anonymous_transcribe_is_401(client: TestClient) -> None:
    r = client.post(
        "/api/stt/transcribe", files={"file": ("a.webm", b"\x00" * 10, "audio/webm")}
    )
    assert r.status_code == 401


def test_interviewer_cannot_edit_or_publish_cards(
    client: TestClient, user: Callable[[str], Headers]
) -> None:
    owner = user("own@example.test")
    org_id = _org(client, owner, "A")
    interviewer = user("int@example.test")
    _add(client, owner, org_id, "int@example.test", "interviewer")
    _, card_id = _draft_card(client, interviewer)
    sections = {a: "x" for a in ("location", "suppliers", "menu", "pricing", "customers", "failures")}
    r = client.put(f"/api/cards/{card_id}", json={"sections": sections}, headers=interviewer)
    assert (r.status_code, r.json()) == (403, {"detail": "forbidden"})
    r = client.post(f"/api/cards/{card_id}/publish", headers=interviewer)
    assert r.status_code == 403
    # An interviewer reads a draft only as an anonymous visitor would: 404.
    assert client.get(f"/api/cards/{card_id}", headers=interviewer).status_code == 404
    assert client.get(f"/api/cards/{card_id}", headers=owner).status_code == 200


def test_other_org_cannot_see_draft_or_shop(
    client: TestClient, user: Callable[[str], Headers]
) -> None:
    owner_a = user("a@example.test")
    owner_b = user("b@example.test")
    _org(client, owner_a, "A")
    _org(client, owner_b, "B")
    shop_id, card_id = _draft_card(client, owner_a)
    assert client.get(f"/api/cards/{card_id}", headers=owner_a).status_code == 200
    r = client.get(f"/api/cards/{card_id}", headers=owner_b)
    assert (r.status_code, r.json()) == (404, {"detail": "card not found"})
    assert client.get(f"/api/cards/{card_id}").status_code == 404
    assert client.get(f"/api/shops/{shop_id}", headers=owner_b).status_code == 404
    assert client.get(f"/api/shops/{shop_id}/progress", headers=owner_b).status_code == 404
    assert client.get("/api/shops", headers=owner_b).json() == []
    assert [s["shop_id"] for s in client.get("/api/shops", headers=owner_a).json()] == [shop_id]
    r = client.post("/api/sessions", json={"shop_id": shop_id}, headers=owner_b)
    assert r.status_code == 404
    sections = {a: "x" for a in ("location", "suppliers", "menu", "pricing", "customers", "failures")}
    r = client.put(f"/api/cards/{card_id}", json={"sections": sections}, headers=owner_b)
    assert r.status_code == 404
    assert client.post(f"/api/cards/{card_id}/publish", headers=owner_b).status_code == 404


def test_published_card_is_public_and_frozen(
    client: TestClient, user: Callable[[str], Headers]
) -> None:
    owner = user("own@example.test")
    _org(client, owner, "A")
    _, card_id = _draft_card(client, owner)
    assert client.get(f"/cards/{card_id}").status_code == 404
    r = client.post(f"/api/cards/{card_id}/publish", headers=owner)
    assert r.status_code == 200, r.text
    r = client.get(f"/api/cards/{card_id}")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"
    assert [c["card_id"] for c in client.get("/api/cards?status=published").json()] == [card_id]
    assert client.get(f"/cards/{card_id}").status_code == 200
    sections = {a: "x" for a in ("location", "suppliers", "menu", "pricing", "customers", "failures")}
    r = client.put(f"/api/cards/{card_id}", json={"sections": sections}, headers=owner)
    assert (r.status_code, r.json()) == (409, {"detail": "card is published"})


def test_two_orgs_without_org_header_is_400(
    client: TestClient, user: Callable[[str], Headers]
) -> None:
    owner = user("own@example.test")
    org_a = _org(client, owner, "A")
    _org(client, owner, "B")
    r = client.get("/api/shops", headers=owner)
    assert (r.status_code, r.json()) == (400, {"detail": "org_required"})
    assert client.post("/api/shops", json=SHOP, headers=owner).status_code == 400
    r = client.get("/api/shops", headers={**owner, "X-Org-Id": org_a})
    assert (r.status_code, r.json()) == (200, [])


def test_pages_are_served_without_auth(client: TestClient) -> None:
    assert client.get("/login").status_code == 200
    assert client.get("/interview/missing").status_code == 200
    assert client.get("/review/missing").status_code == 200
    assert client.get("/billing/pay-test").status_code == 200
    assert client.get("/cards/missing").status_code == 404


def test_all_routers_registered() -> None:
    assert len(ALL_ROUTERS) == 15
