import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shoplegacy import routes_consent
from shoplegacy.config import Settings
from shoplegacy.store import Store

OrgTokens = tuple[str, str, str, str]


def mint_token(user_id: str, email: str, secret: str) -> str:
    claims = {
        "sub": user_id,
        "email": email,
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


@pytest.fixture
def client(app_dsn: str, jwt_secret: str, tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        provider="fake",
        stt_provider="fake",
        data_dir=tmp_path,
        database_url=app_dsn,
        jwt_secret=jwt_secret,
    )
    store = Store(app_dsn)
    app = FastAPI()
    app.state.shop = SimpleNamespace(settings=settings, store=store)
    app.include_router(routes_consent.router, prefix="/api")
    try:
        yield TestClient(app)
    finally:
        store.close()


def _shop(pg_dsn: str, org_id: str) -> str:
    shop_id = uuid.uuid4().hex
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO public.shops (shop_id, org_id, created_by, name, owner_name, trade, "
            "area_type, years, closed_year, supplier_names_json, created_at) "
            "VALUES (%s, %s, %s, 'shop', 'owner', 'bakery', 'residential', 10, 2024, '[]', now())",
            (shop_id, org_id, str(uuid.uuid4())),
        )
    return shop_id


@pytest.fixture
def ih(org_fixture: OrgTokens) -> dict[str, str]:
    org_id, _, _, interviewer_token = org_fixture
    return {"Authorization": f"Bearer {interviewer_token}", "X-Org-Id": org_id}


@pytest.fixture
def shop_id(pg_dsn: str, org_fixture: OrgTokens) -> str:
    return _shop(pg_dsn, org_fixture[0])


@pytest.fixture
def other_org(pg_dsn: str, jwt_secret: str) -> tuple[dict[str, str], str]:
    """Headers of an interviewer in a second org, plus a shop of that org."""
    org_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
    email = f"other-{org_id[:8]}@example.test"
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s)", (user_id, email))
        conn.execute(
            "INSERT INTO public.orgs (org_id, name, created_by) VALUES (%s, 'other', %s)",
            (org_id, user_id),
        )
        conn.execute(
            "INSERT INTO public.org_members (org_id, user_id, role) "
            "VALUES (%s, %s, 'interviewer')",
            (org_id, user_id),
        )
    headers = {
        "Authorization": f"Bearer {mint_token(user_id, email, jwt_secret)}",
        "X-Org-Id": org_id,
    }
    return headers, _shop(pg_dsn, org_id)


def _grant(
    client: TestClient, shop_id: str, h: dict[str, str], kind: str = "recording"
) -> httpx.Response:
    return client.post(
        f"/api/shops/{shop_id}/consents", json={"kind": kind, "method": "verbal"}, headers=h
    )


def test_grant_returns_201(client: TestClient, ih: dict[str, str], shop_id: str) -> None:
    r = _grant(client, shop_id, ih)
    assert r.status_code == 201
    body = r.json()
    assert body["shop_id"] == shop_id
    assert (body["kind"], body["method"]) == ("recording", "verbal")
    assert body["revoked_at"] is None and body["revoke_reason"] is None


def test_kinds_are_recorded_separately(
    client: TestClient, ih: dict[str, str], shop_id: str
) -> None:
    for kind in ("recording", "storage", "ai_processing", "publication"):
        assert _grant(client, shop_id, ih, kind).status_code == 201
    r = client.get(f"/api/shops/{shop_id}/consents", headers=ih)
    assert r.status_code == 200
    assert sorted(c["kind"] for c in r.json()) == [
        "ai_processing",
        "publication",
        "recording",
        "storage",
    ]


def test_duplicate_active_consent_is_409(
    client: TestClient, ih: dict[str, str], shop_id: str
) -> None:
    assert _grant(client, shop_id, ih).status_code == 201
    r = _grant(client, shop_id, ih)
    assert (r.status_code, r.json()) == (409, {"detail": "consent already active"})


def test_revoke_then_regrant(client: TestClient, ih: dict[str, str], shop_id: str) -> None:
    cid = _grant(client, shop_id, ih).json()["consent_id"]
    r = client.post(f"/api/consents/{cid}/revoke", json={"reason": "owner asked"}, headers=ih)
    assert r.status_code == 200
    body = r.json()
    assert body["revoked_at"] is not None and body["revoke_reason"] == "owner asked"
    assert _grant(client, shop_id, ih).status_code == 201


def test_re_revoke_is_409(client: TestClient, ih: dict[str, str], shop_id: str) -> None:
    cid = _grant(client, shop_id, ih).json()["consent_id"]
    url = f"/api/consents/{cid}/revoke"
    assert client.post(url, json={"reason": "a"}, headers=ih).status_code == 200
    r = client.post(url, json={"reason": "b"}, headers=ih)
    assert (r.status_code, r.json()) == (409, {"detail": "consent already revoked"})


def test_other_org_gets_404(
    client: TestClient,
    ih: dict[str, str],
    shop_id: str,
    other_org: tuple[dict[str, str], str],
) -> None:
    oh, other_shop = other_org
    cid = _grant(client, shop_id, ih).json()["consent_id"]
    assert _grant(client, shop_id, oh).status_code == 404
    assert client.get(f"/api/shops/{shop_id}/consents", headers=oh).status_code == 404
    r = client.post(f"/api/consents/{cid}/revoke", json={"reason": "x"}, headers=oh)
    assert (r.status_code, r.json()) == (404, {"detail": "consent not found"})
    assert _grant(client, other_shop, ih).status_code == 404


def test_unknown_consent_is_404(client: TestClient, ih: dict[str, str]) -> None:
    r = client.post("/api/consents/nope/revoke", json={"reason": "x"}, headers=ih)
    assert r.status_code == 404


def test_requires_login(client: TestClient, shop_id: str) -> None:
    r = client.post(
        f"/api/shops/{shop_id}/consents", json={"kind": "recording", "method": "verbal"}
    )
    assert r.status_code == 401


def test_audit_log_has_grant_and_revoke_once(
    client: TestClient, ih: dict[str, str], shop_id: str, pg_dsn: str
) -> None:
    cid = _grant(client, shop_id, ih).json()["consent_id"]
    client.post(f"/api/consents/{cid}/revoke", json={"reason": "owner asked"}, headers=ih)
    with psycopg.connect(pg_dsn) as conn:
        rows = conn.execute(
            "SELECT action, actor_id::text, new_json FROM public.audit_log "
            "WHERE row_id = %s AND action LIKE 'consent.%%' ORDER BY id",
            (cid,),
        ).fetchall()
    assert [r[0] for r in rows] == ["consent.grant", "consent.revoke"]
    assert rows[0][1] is not None
    assert rows[1][2]["reason"] == "owner asked"
