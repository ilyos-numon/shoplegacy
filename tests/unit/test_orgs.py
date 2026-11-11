import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shoplegacy import routes_billing, routes_orgs
from shoplegacy.billing import PlanLimitError
from shoplegacy.config import Settings
from shoplegacy.store import Store

SECRET = "test-secret-that-is-long-enough-for-hs256"


@pytest.fixture
def ctx(app_dsn: str, tmp_path: Path) -> Iterator[tuple[TestClient, Store]]:
    settings = Settings(
        provider="fake",
        stt_provider="fake",
        data_dir=tmp_path,
        database_url=app_dsn,
        jwt_secret=SECRET,
        supabase_url="http://localhost:56321",
        supabase_anon_key="anon-key",
    )
    store = Store(app_dsn)
    app = FastAPI()
    app.state.shop = SimpleNamespace(settings=settings, store=store)
    app.include_router(routes_orgs.router, prefix="/api")
    try:
        yield TestClient(app), store
    finally:
        store.close()


@pytest.fixture
def client(ctx: tuple[TestClient, Store]) -> TestClient:
    return ctx[0]


def _h(user_id: str, email: str, org_id: str | None = None) -> dict[str, str]:
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


User = tuple[str, dict[str, str]]


@pytest.fixture
def user(pg_dsn: str, make_user: Callable[[str, str], str]) -> Callable[[str], User]:
    def _new(email: str) -> User:
        uid = make_user(pg_dsn, email)
        return uid, _h(uid, email)

    return _new


def test_auth_config_is_public(client: TestClient) -> None:
    r = client.get("/api/auth/config")
    assert r.status_code == 200
    assert r.json() == {"supabase_url": "http://localhost:56321", "anon_key": "anon-key"}


def test_me_requires_login(client: TestClient) -> None:
    r = client.get("/api/me")
    assert (r.status_code, r.json()) == (401, {"detail": "login required"})


def test_create_org_makes_owner(client: TestClient, user: Callable[[str], User]) -> None:
    uid, h = user("own@example.test")
    r = client.post("/api/orgs", json={"name": "Org A"}, headers=h)
    assert r.status_code == 201
    org = r.json()
    assert org["name"] == "Org A" and org["role"] == "owner"
    me = client.get("/api/me", headers=h).json()
    assert me == {
        "user_id": uid,
        "email": "own@example.test",
        "orgs": [{"org_id": org["org_id"], "name": "Org A", "role": "owner"}],
    }
    assert client.post("/api/orgs", json={"name": ""}, headers=h).status_code == 422


def test_add_member_is_admin_only(client: TestClient, user: Callable[[str], User]) -> None:
    _, owner_h = user("own@example.test")
    reviewer_id, reviewer_h = user("rev@example.test")
    user("new@example.test")
    org_id = client.post("/api/orgs", json={"name": "A"}, headers=owner_h).json()["org_id"]
    r = client.post(
        f"/api/orgs/{org_id}/members",
        json={"email": "rev@example.test", "role": "reviewer"},
        headers=owner_h,
    )
    assert r.status_code == 201
    assert r.json() == {"user_id": reviewer_id, "email": "rev@example.test", "role": "reviewer"}
    r = client.post(
        f"/api/orgs/{org_id}/members",
        json={"email": "new@example.test", "role": "interviewer"},
        headers=reviewer_h,
    )
    assert (r.status_code, r.json()) == (403, {"detail": "forbidden"})
    r = client.post(
        f"/api/orgs/{org_id}/members",
        json={"email": "rev@example.test", "role": "admin"},
        headers=owner_h,
    )
    assert r.status_code == 201
    r = client.post(
        f"/api/orgs/{org_id}/members",
        json={"email": "new@example.test", "role": "interviewer"},
        headers=reviewer_h,
    )
    assert r.status_code == 201


def test_other_org_members_forbidden(client: TestClient, user: Callable[[str], User]) -> None:
    _, a_h = user("a@example.test")
    _, b_h = user("b@example.test")
    org_a = client.post("/api/orgs", json={"name": "A"}, headers=a_h).json()["org_id"]
    org_b = client.post("/api/orgs", json={"name": "B"}, headers=b_h).json()["org_id"]
    r = client.get(f"/api/orgs/{org_a}/members", headers=b_h)
    assert (r.status_code, r.json()) == (403, {"detail": "forbidden"})
    r = client.get(f"/api/orgs/{org_a}/members", headers={**b_h, "X-Org-Id": org_b})
    assert r.status_code == 403
    r = client.post(
        f"/api/orgs/{org_a}/members",
        json={"email": "b@example.test", "role": "owner"},
        headers=b_h,
    )
    assert r.status_code == 403


def test_unknown_email_is_404(client: TestClient, user: Callable[[str], User]) -> None:
    _, h = user("own@example.test")
    org_id = client.post("/api/orgs", json={"name": "A"}, headers=h).json()["org_id"]
    r = client.post(
        f"/api/orgs/{org_id}/members",
        json={"email": "ghost@example.test", "role": "reviewer"},
        headers=h,
    )
    assert (r.status_code, r.json()) == (404, {"detail": "user not found"})


def test_members_list_includes_emails(client: TestClient, user: Callable[[str], User]) -> None:
    owner_id, h = user("own@example.test")
    rev_id, _ = user("rev@example.test")
    org_id = client.post("/api/orgs", json={"name": "A"}, headers=h).json()["org_id"]
    client.post(
        f"/api/orgs/{org_id}/members",
        json={"email": "REV@example.test", "role": "reviewer"},
        headers=h,
    )
    r = client.get(f"/api/orgs/{org_id}/members", headers=h)
    assert r.status_code == 200
    assert r.json() == [
        {"user_id": owner_id, "email": "own@example.test", "role": "owner"},
        {"user_id": rev_id, "email": "rev@example.test", "role": "reviewer"},
    ]


def test_only_owner_may_grant_owner(client: TestClient, user: Callable[[str], User]) -> None:
    _, owner_h = user("own@example.test")
    adm_id, adm_h = user("adm@example.test")
    mem_id, _ = user("mem@example.test")
    org_id = client.post("/api/orgs", json={"name": "A"}, headers=owner_h).json()["org_id"]
    for email, role in (("adm@example.test", "admin"), ("mem@example.test", "interviewer")):
        r = client.post(
            f"/api/orgs/{org_id}/members", json={"email": email, "role": role}, headers=owner_h
        )
        assert r.status_code == 201
    for email in ("mem@example.test", "adm@example.test"):
        r = client.post(
            f"/api/orgs/{org_id}/members", json={"email": email, "role": "owner"}, headers=adm_h
        )
        assert (r.status_code, r.json()) == (403, {"detail": "forbidden"})
    roles = {m["user_id"]: m["role"] for m in client.get(
        f"/api/orgs/{org_id}/members", headers=owner_h
    ).json()}
    assert roles[adm_id] == "admin" and roles[mem_id] == "interviewer"
    r = client.post(
        f"/api/orgs/{org_id}/members",
        json={"email": "mem@example.test", "role": "owner"},
        headers=owner_h,
    )
    assert (r.status_code, r.json()["role"]) == (201, "owner")


def test_existing_owner_role_is_protected(
    client: TestClient, user: Callable[[str], User]
) -> None:
    owner_id, owner_h = user("own@example.test")
    _, adm_h = user("adm@example.test")
    org_id = client.post("/api/orgs", json={"name": "A"}, headers=owner_h).json()["org_id"]
    r = client.post(
        f"/api/orgs/{org_id}/members",
        json={"email": "adm@example.test", "role": "admin"},
        headers=owner_h,
    )
    assert r.status_code == 201
    for role in ("interviewer", "admin"):
        r = client.post(
            f"/api/orgs/{org_id}/members",
            json={"email": "own@example.test", "role": role},
            headers=adm_h,
        )
        assert (r.status_code, r.json()) == (403, {"detail": "forbidden"})
    roles = {m["user_id"]: m["role"] for m in client.get(
        f"/api/orgs/{org_id}/members", headers=owner_h
    ).json()}
    assert roles[owner_id] == "owner"


def test_last_owner_cannot_be_demoted(client: TestClient, user: Callable[[str], User]) -> None:
    owner_id, owner_h = user("own@example.test")
    second_id, _ = user("two@example.test")
    org_id = client.post("/api/orgs", json={"name": "A"}, headers=owner_h).json()["org_id"]
    r = client.post(
        f"/api/orgs/{org_id}/members",
        json={"email": "own@example.test", "role": "admin"},
        headers=owner_h,
    )
    assert (r.status_code, r.json()) == (409, {"detail": "last_owner"})
    r = client.post(
        f"/api/orgs/{org_id}/members",
        json={"email": "own@example.test", "role": "owner"},
        headers=owner_h,
    )
    assert r.status_code == 201
    r = client.post(
        f"/api/orgs/{org_id}/members",
        json={"email": "two@example.test", "role": "owner"},
        headers=owner_h,
    )
    assert r.status_code == 201
    r = client.post(
        f"/api/orgs/{org_id}/members",
        json={"email": "two@example.test", "role": "reviewer"},
        headers=owner_h,
    )
    assert r.status_code == 201
    roles = {m["user_id"]: m["role"] for m in client.get(
        f"/api/orgs/{org_id}/members", headers=owner_h
    ).json()}
    assert roles == {owner_id: "owner", second_id: "reviewer"}


def _add(client: TestClient, org_id: str, email: str, role: str, h: dict[str, str]) -> None:
    r = client.post(
        f"/api/orgs/{org_id}/members", json={"email": email, "role": role}, headers=h
    )
    assert r.status_code == 201


def test_remove_member(client: TestClient, user: Callable[[str], User]) -> None:
    owner_id, owner_h = user("own@example.test")
    rev_id, rev_h = user("rev@example.test")
    org_id = client.post("/api/orgs", json={"name": "A"}, headers=owner_h).json()["org_id"]
    _add(client, org_id, "rev@example.test", "reviewer", owner_h)
    r = client.delete(f"/api/orgs/{org_id}/members/{owner_id}", headers=rev_h)
    assert (r.status_code, r.json()) == (403, {"detail": "forbidden"})
    r = client.delete(f"/api/orgs/{org_id}/members/{rev_id}", headers=owner_h)
    assert r.status_code == 204
    r = client.delete(f"/api/orgs/{org_id}/members/{rev_id}", headers=owner_h)
    assert (r.status_code, r.json()) == (404, {"detail": "member not found"})
    members = client.get(f"/api/orgs/{org_id}/members", headers=owner_h).json()
    assert [m["user_id"] for m in members] == [owner_id]


def test_last_owner_removal_is_409_from_db(
    client: TestClient, ctx: tuple[TestClient, Store], user: Callable[[str], User]
) -> None:
    owner_id, owner_h = user("own@example.test")
    org_id = client.post("/api/orgs", json={"name": "A"}, headers=owner_h).json()["org_id"]
    calls: list[str] = []
    store = ctx[1]
    real_remove = store.remove_member

    def spy(org: str, uid: str) -> None:
        calls.append(uid)
        real_remove(org, uid)

    store.remove_member = spy  # type: ignore[method-assign]
    r = client.delete(f"/api/orgs/{org_id}/members/{owner_id}", headers=owner_h)
    assert (r.status_code, r.json()) == (409, {"detail": "last_owner"})
    assert calls == [owner_id]
    roles = {m["user_id"]: m["role"] for m in client.get(
        f"/api/orgs/{org_id}/members", headers=owner_h
    ).json()}
    assert roles == {owner_id: "owner"}


def test_owner_removal_with_two_owners(client: TestClient, user: Callable[[str], User]) -> None:
    owner_id, owner_h = user("own@example.test")
    second_id, _ = user("two@example.test")
    _, adm_h = user("adm@example.test")
    org_id = client.post("/api/orgs", json={"name": "A"}, headers=owner_h).json()["org_id"]
    _add(client, org_id, "two@example.test", "owner", owner_h)
    _add(client, org_id, "adm@example.test", "admin", owner_h)
    r = client.delete(f"/api/orgs/{org_id}/members/{second_id}", headers=adm_h)
    assert (r.status_code, r.json()) == (403, {"detail": "forbidden"})
    r = client.delete(f"/api/orgs/{org_id}/members/{second_id}", headers=owner_h)
    assert r.status_code == 204
    roles = {m["user_id"]: m["role"] for m in client.get(
        f"/api/orgs/{org_id}/members", headers=owner_h
    ).json()}
    assert roles[owner_id] == "owner" and second_id not in roles


def test_member_add_plan_limit_is_402(
    client: TestClient, user: Callable[[str], User], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, owner_h = user("own@example.test")
    user("new@example.test")
    org_id = client.post("/api/orgs", json={"name": "A"}, headers=owner_h).json()["org_id"]

    def full(store: Store, org: str, resource: str) -> None:
        raise PlanLimitError("members")

    monkeypatch.setattr(routes_billing, "check_limit", full)
    r = client.post(
        f"/api/orgs/{org_id}/members",
        json={"email": "new@example.test", "role": "reviewer"},
        headers=owner_h,
    )
    assert (r.status_code, r.json()) == (402, {"detail": "plan_limit:members"})
