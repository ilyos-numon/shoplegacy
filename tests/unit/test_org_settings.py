import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shoplegacy import routes_orgs
from shoplegacy.config import Settings
from shoplegacy.store import Store

SECRET = "test-secret-that-is-long-enough-for-hs256"


def _app(app_dsn: str, tmp_path: Path, require_owner_approval: bool) -> tuple[FastAPI, Store]:
    settings = Settings(
        provider="fake",
        stt_provider="fake",
        data_dir=tmp_path,
        database_url=app_dsn,
        jwt_secret=SECRET,
        require_owner_approval=require_owner_approval,
    )
    store = Store(app_dsn)
    app = FastAPI()
    app.state.shop = SimpleNamespace(settings=settings, store=store)
    app.include_router(routes_orgs.router, prefix="/api")
    return app, store


@pytest.fixture
def client(app_dsn: str, tmp_path: Path) -> Iterator[TestClient]:
    app, store = _app(app_dsn, tmp_path, False)
    try:
        yield TestClient(app)
    finally:
        store.close()


def _h(user_id: str, email: str) -> dict[str, str]:
    claims: dict[str, Any] = {
        "sub": user_id,
        "email": email,
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + 300,
    }
    return {"Authorization": f"Bearer {jwt.encode(claims, SECRET, algorithm='HS256')}"}


User = tuple[str, dict[str, str]]


@pytest.fixture
def user(pg_dsn: str, make_user: Callable[[str, str], str]) -> Callable[[str], User]:
    def _new(email: str) -> User:
        uid = make_user(pg_dsn, email)
        return uid, _h(uid, email)

    return _new


def _org(client: TestClient, headers: dict[str, str], name: str = "서울 기록관") -> str:
    resp = client.post("/api/orgs", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    org_id: str = resp.json()["org_id"]
    return org_id


def test_owner_reads_settings(client: TestClient, user: Callable[[str], User]) -> None:
    _, owner = user("owner@example.test")
    org_id = _org(client, owner)
    resp = client.get(f"/api/orgs/{org_id}/settings", headers=owner)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "org_id": org_id,
        "name": "서울 기록관",
        "require_owner_approval": False,
    }


def test_settings_reflect_env_flag(
    app_dsn: str, tmp_path: Path, user: Callable[[str], User]
) -> None:
    app, store = _app(app_dsn, tmp_path, True)
    try:
        client = TestClient(app)
        _, owner = user("owner2@example.test")
        org_id = _org(client, owner)
        body = client.get(f"/api/orgs/{org_id}/settings", headers=owner).json()
        assert body["require_owner_approval"] is True
    finally:
        store.close()


def test_admin_reads_settings(client: TestClient, user: Callable[[str], User]) -> None:
    _, owner = user("owner3@example.test")
    _, admin = user("admin3@example.test")
    org_id = _org(client, owner)
    added = client.post(
        f"/api/orgs/{org_id}/members",
        json={"email": "admin3@example.test", "role": "admin"},
        headers=owner,
    )
    assert added.status_code == 201, added.text
    resp = client.get(f"/api/orgs/{org_id}/settings", headers=admin)
    assert resp.status_code == 200
    assert resp.json()["org_id"] == org_id


def test_settings_forbidden_below_admin(client: TestClient, user: Callable[[str], User]) -> None:
    _, owner = user("owner4@example.test")
    _, reviewer = user("reviewer4@example.test")
    _, stranger = user("stranger4@example.test")
    org_id = _org(client, owner)
    added = client.post(
        f"/api/orgs/{org_id}/members",
        json={"email": "reviewer4@example.test", "role": "reviewer"},
        headers=owner,
    )
    assert added.status_code == 201, added.text
    url = f"/api/orgs/{org_id}/settings"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=reviewer).status_code == 403
    assert client.get(url, headers=stranger).status_code == 403
