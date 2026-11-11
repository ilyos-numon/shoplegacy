import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shoplegacy.main import create_app

SHOP_NAME = "가나다식당"
OWNER = "홍길동"
ANSWER = f"{SHOP_NAME} {OWNER} 사장이 골목 손님을 맞았다"
BASE_URL = "https://owner.example.test"

INBOX_KEYS = {
    "card_id",
    "shop_id",
    "status",
    "tags",
    "version",
    "updated_at",
    "owner_decision",
    "owner_link_active",
    "support_ratio",
}


@dataclass
class Env:
    app: FastAPI
    client: TestClient
    org_id: str


@pytest.fixture
def env(
    tmp_path: Path,
    app_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    reviewer_headers: dict[str, str],
    jwt_secret: str,
) -> Iterator[Env]:
    monkeypatch.setenv("SHOPLEGACY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", app_dsn)
    monkeypatch.setenv("SHOPLEGACY_JWT_SECRET", jwt_secret)
    monkeypatch.setenv("SHOPLEGACY_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_STT_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_PUBLIC_BASE_URL", BASE_URL + "/")
    app = create_app()
    yield Env(app, TestClient(app, headers=reviewer_headers), reviewer_headers["X-Org-Id"])
    app.dependency_overrides.clear()


def _card(client: TestClient, name: str = SHOP_NAME) -> str:
    shop = {
        "name": name,
        "owner_name": OWNER,
        "trade": "restaurant",
        "area_type": "residential",
        "years": 20,
        "closed_year": 2025,
        "supplier_names": [],
    }
    created = client.post("/api/shops", json=shop)
    assert created.status_code == 201, created.text
    shop_id = created.json()["shop_id"]
    sid = client.post("/api/sessions", json={"shop_id": shop_id}).json()["session_id"]
    for _ in range(3):
        assert client.post(f"/api/sessions/{sid}/answer", json={"text": ANSWER}).status_code == 200
    end = client.post(f"/api/sessions/{sid}/end")
    assert end.status_code == 200, end.text
    card_id: str = end.json()["card_id"]
    return card_id


def _inbox(client: TestClient) -> list[dict[str, object]]:
    resp = client.get("/api/review/inbox")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"items"}
    items: list[dict[str, object]] = body["items"]
    return items


def test_inbox_empty(env: Env) -> None:
    assert _inbox(env.client) == []


def test_inbox_lists_draft_card_fields(env: Env) -> None:
    card_id = _card(env.client)
    items = _inbox(env.client)
    assert len(items) == 1
    item = items[0]
    assert set(item) == INBOX_KEYS
    card = env.client.get(f"/api/cards/{card_id}").json()
    assert item["card_id"] == card_id
    assert item["shop_id"] == card["shop_id"]
    assert item["status"] == "draft"
    assert item["tags"] == card["tags"]
    assert item["version"] == card["version"]
    assert item["updated_at"] == card["updated_at"]
    assert item["owner_decision"] is None
    assert item["owner_link_active"] is False
    assert item["support_ratio"] == card["quality"]["supported_ratio"]
    assert 0.0 <= float(str(item["support_ratio"])) <= 1.0


def test_inbox_owner_link_and_decision(env: Env) -> None:
    card_id = _card(env.client)
    link = env.client.post(f"/api/cards/{card_id}/owner-link", json={"phone": None})
    assert link.status_code == 201, link.text
    (item,) = _inbox(env.client)
    assert item["owner_link_active"] is True
    assert item["owner_decision"] is None

    token = link.json()["url"].rsplit("/", 1)[1]
    anon = TestClient(env.app)
    decided = anon.post(f"/api/owner/{token}/decision", json={"decision": "approve"})
    assert decided.status_code == 201, decided.text
    (item,) = _inbox(env.client)
    assert item["owner_decision"] == "approve"

    token_id = link.json()["token_id"]
    revoked = env.client.delete(f"/api/cards/{card_id}/owner-links/{token_id}")
    assert revoked.status_code == 204
    (item,) = _inbox(env.client)
    assert item["owner_link_active"] is False


def test_inbox_covers_all_statuses_newest_first(env: Env) -> None:
    first = _card(env.client, "첫째식당")
    second = _card(env.client, "둘째식당")
    held = env.client.post(f"/api/cards/{first}/status", json={"status": "on_hold"})
    assert held.status_code == 200, held.text
    items = _inbox(env.client)
    assert [i["card_id"] for i in items] == [first, second]
    assert [i["status"] for i in items] == ["on_hold", "draft"]
    stamps = [str(i["updated_at"]) for i in items]
    assert stamps == sorted(stamps, reverse=True)


def test_inbox_requires_reviewer(env: Env, org_fixture: tuple[str, str, str, str]) -> None:
    org_id, _, _, interviewer_token = org_fixture
    anon = TestClient(env.app)
    assert anon.get("/api/review/inbox").status_code == 401
    interviewer = {"Authorization": f"Bearer {interviewer_token}", "X-Org-Id": org_id}
    assert anon.get("/api/review/inbox", headers=interviewer).status_code == 403


def _bearer(user_id: str, email: str, secret: str) -> dict[str, str]:
    claims = {
        "sub": user_id,
        "email": email,
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + 300,
    }
    return {"Authorization": f"Bearer {jwt.encode(claims, secret, algorithm='HS256')}"}


def test_inbox_hides_other_org_cards(
    env: Env, make_user: Callable[[str, str], str], pg_dsn: str, jwt_secret: str
) -> None:
    _card(env.client)
    email = "other-owner@example.test"
    bearer = _bearer(make_user(pg_dsn, email), email, jwt_secret)
    other = TestClient(env.app, headers=bearer)
    org = other.post("/api/orgs", json={"name": "other org"})
    assert org.status_code == 201, org.text
    other.headers["X-Org-Id"] = org.json()["org_id"]
    assert _inbox(other) == []
