import io
import json
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jwt
import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shoplegacy import maintenance, routes_data, routes_orgs
from shoplegacy.config import Settings
from shoplegacy.crypto import ENC_PREFIX, FernetCipher
from shoplegacy.store import PRINCIPAL, Principal, Role, ShopRow, Store

SECRET = "test-secret-that-is-long-enough-for-hs256"
NOW = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
EXPORT_FILES = {
    "shop.json",
    "sessions.json",
    "answers.json",
    "cards.json",
    "card_versions.json",
    "consents.json",
    "owner_reviews.json",
    "audit.json",
}


def _headers(user_id: str, email: str, org_id: str) -> dict[str, str]:
    claims: dict[str, Any] = {
        "sub": user_id,
        "email": email,
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + 300,
    }
    token = jwt.encode(claims, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}", "X-Org-Id": org_id}


@dataclass
class Member:
    user_id: str
    email: str
    org_id: str
    role: Role

    @property
    def headers(self) -> dict[str, str]:
        return _headers(self.user_id, self.email, self.org_id)

    @property
    def principal(self) -> Principal:
        return Principal(
            user_id=self.user_id, email=self.email, org_id=self.org_id, role=self.role, claims={}
        )


@dataclass
class World:
    client: TestClient
    store: Store
    owner: Member
    admin: Member
    interviewer: Member
    stranger: Member


@contextmanager
def acting(p: Principal) -> Iterator[None]:
    token = PRINCIPAL.set(p)
    try:
        yield
    finally:
        PRINCIPAL.reset(token)


def _org(
    pg_dsn: str, make_user: Callable[[str, str], str], name: str, roles: tuple[Role, ...]
) -> list[Member]:
    members: list[Member] = []
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        for role in roles:
            email = f"{role}-{name}@example.test"
            user_id = make_user(pg_dsn, email)
            if not members:
                row = conn.execute(
                    "INSERT INTO public.orgs (name, created_by) VALUES (%s, %s) "
                    "RETURNING org_id::text",
                    (name, user_id),
                ).fetchone()
                assert row is not None
                org_id = str(row[0])
            conn.execute(
                "INSERT INTO public.org_members (org_id, user_id, role) VALUES (%s, %s, %s)",
                (org_id, user_id, role),
            )
            members.append(Member(user_id, email, org_id, role))
    return members


@pytest.fixture
def world(
    app_dsn: str,
    pg_dsn: str,
    tmp_path: Path,
    pii_cipher: FernetCipher,
    make_user: Callable[[str, str], str],
) -> Iterator[World]:
    settings = Settings(
        provider="fake",
        stt_provider="fake",
        data_dir=tmp_path,
        database_url=app_dsn,
        jwt_secret=SECRET,
    )
    store = Store(app_dsn, cipher=pii_cipher)
    app = FastAPI()
    app.state.shop = SimpleNamespace(settings=settings, store=store)
    app.include_router(routes_orgs.router, prefix="/api")
    app.include_router(routes_data.router, prefix="/api")
    owner, admin, interviewer = _org(pg_dsn, make_user, "a", ("owner", "admin", "interviewer"))
    (stranger,) = _org(pg_dsn, make_user, "b", ("owner",))
    try:
        yield World(TestClient(app), store, owner, admin, interviewer, stranger)
    finally:
        store.close()


def _add_shop(w: World, shop_id: str, retention_until: datetime | None = None) -> None:
    row = ShopRow(
        shop_id=shop_id,
        name="테스트 식당",
        owner_name="김사장",
        trade="tree_min",
        area_type="station",
        years=20,
        closed_year=2025,
        supplier_names=["가상 정육"],
        created_at=NOW,
        retention_until=retention_until,
    )
    with acting(w.owner.principal):
        w.store.insert_shop(row)


def _shop_count(pg_dsn: str, shop_id: str) -> int:
    with psycopg.connect(pg_dsn) as conn:
        row = conn.execute("SELECT count(*) FROM shops WHERE shop_id = %s", (shop_id,)).fetchone()
    assert row is not None
    return int(row[0])


def test_export_zip_has_all_files_decrypted(world: World, pg_dsn: str) -> None:
    _add_shop(world, "s1")
    with psycopg.connect(pg_dsn) as conn:
        stored = conn.execute("SELECT owner_name FROM shops WHERE shop_id = 's1'").fetchone()
    assert stored is not None and str(stored[0]).startswith(ENC_PREFIX)
    r = world.client.get("/api/shops/s1/export", headers=world.admin.headers)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert set(zf.namelist()) == EXPORT_FILES
        shop = json.loads(zf.read("shop.json"))
        audit = json.loads(zf.read("audit.json"))
        sessions = json.loads(zf.read("sessions.json"))
    assert shop["shop_id"] == "s1"
    assert shop["owner_name"] == "김사장"
    assert shop["supplier_names"] == ["가상 정육"]
    assert sessions == []
    assert any(a["row_id"] == "s1" for a in audit)


def test_export_writes_audit_with_shop_and_actor(world: World, pg_dsn: str) -> None:
    _add_shop(world, "s1")
    r = world.client.get("/api/shops/s1/export", headers=world.admin.headers)
    assert r.status_code == 200
    with psycopg.connect(pg_dsn) as conn:
        rows = conn.execute(
            "SELECT actor_id::text, org_id::text, table_name, new_json FROM audit_log "
            "WHERE action = 'shop.export' AND row_id = 's1'"
        ).fetchall()
    assert rows == [
        (
            world.admin.user_id,
            world.admin.org_id,
            "shops",
            {"shop_id": "s1", "by": world.admin.user_id},
        )
    ]


def test_delete_then_gone_with_audit(world: World, pg_dsn: str) -> None:
    _add_shop(world, "s1")
    r = world.client.delete("/api/shops/s1", headers=world.admin.headers)
    assert r.status_code == 204
    assert _shop_count(pg_dsn, "s1") == 0
    r = world.client.get("/api/shops/s1/export", headers=world.admin.headers)
    assert (r.status_code, r.json()) == (404, {"detail": "shop not found"})
    r = world.client.delete("/api/shops/s1", headers=world.admin.headers)
    assert r.status_code == 404
    with psycopg.connect(pg_dsn) as conn:
        rows = conn.execute(
            "SELECT actor_id::text, org_id::text FROM audit_log "
            "WHERE action = 'shop.delete' AND row_id = 's1'"
        ).fetchall()
    assert rows == [(world.admin.user_id, world.admin.org_id)]


def test_interviewer_cannot_delete_or_export(world: World, pg_dsn: str) -> None:
    _add_shop(world, "s1")
    for method, path in (("DELETE", "/api/shops/s1"), ("GET", "/api/shops/s1/export")):
        r = world.client.request(method, path, headers=world.interviewer.headers)
        assert (r.status_code, r.json()) == (403, {"detail": "forbidden"})
    assert _shop_count(pg_dsn, "s1") == 1


def test_other_org_gets_404(world: World, pg_dsn: str) -> None:
    _add_shop(world, "s1")
    h = world.stranger.headers
    assert world.client.delete("/api/shops/s1", headers=h).status_code == 404
    assert world.client.get("/api/shops/s1/export", headers=h).status_code == 404
    assert _shop_count(pg_dsn, "s1") == 1
    r = world.client.get(f"/api/orgs/{world.owner.org_id}/retention", headers=h)
    assert r.status_code == 403


def test_retention_owner_only_and_minimum(world: World) -> None:
    _add_shop(world, "old", retention_until=NOW - timedelta(days=400))
    _add_shop(world, "new")
    path = f"/api/orgs/{world.owner.org_id}/retention"
    r = world.client.get(path, headers=world.admin.headers)
    assert (r.status_code, r.json()) == (200, {"days": 1095, "next_purge_count": 1})
    r = world.client.put(path, json={"days": 60}, headers=world.admin.headers)
    assert (r.status_code, r.json()) == (403, {"detail": "forbidden"})
    r = world.client.put(path, json={"days": 29}, headers=world.owner.headers)
    assert r.status_code == 422
    r = world.client.put(path, json={"days": 30}, headers=world.owner.headers)
    assert (r.status_code, r.json()) == (200, {"days": 30, "next_purge_count": 1})
    r = world.client.get(path, headers=world.admin.headers)
    assert r.json()["days"] == 30
    r = world.client.get(path, headers=world.interviewer.headers)
    assert r.status_code == 403


def test_purge_dry_run_then_purge(
    world: World, app_dsn: str, pg_dsn: str, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _add_shop(world, "expired", retention_until=NOW - timedelta(days=1))
    _add_shop(world, "kept", retention_until=datetime.now(UTC) + timedelta(days=30))
    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", app_dsn)
    assert maintenance.main(["purge", "--dry-run"]) == 0
    assert capsys.readouterr().out.splitlines() == ["expired"]
    assert _shop_count(pg_dsn, "expired") == 1
    assert maintenance.main(["purge"]) == 0
    assert capsys.readouterr().out.splitlines() == ["expired"]
    assert _shop_count(pg_dsn, "expired") == 0
    assert _shop_count(pg_dsn, "kept") == 1
    with psycopg.connect(pg_dsn) as conn:
        row = conn.execute(
            "SELECT count(*) FROM audit_log WHERE action = 'shop.purge' AND row_id = 'expired'"
        ).fetchone()
    assert row is not None and row[0] == 1


def test_generate_key_is_fernet_key(capsys: Any) -> None:
    assert maintenance.main(["generate-key"]) == 0
    key = capsys.readouterr().out.strip()
    assert len(key) == 44
    token = Fernet(key.encode()).encrypt(b"x")
    assert FernetCipher([key]).decrypt(ENC_PREFIX + token.decode()) == "x"


def test_purge_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHOPLEGACY_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        maintenance.main(["purge", "--dry-run"])
