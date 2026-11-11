import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import psycopg
import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shoplegacy import routes_owner
from shoplegacy.config import load_settings
from shoplegacy.main import create_app
from shoplegacy.owner_review import (
    OWNER_SCOPE,
    LinkExpiredError,
    LinkInvalidError,
    issue_link,
    verify_link,
)
from shoplegacy.store import PRINCIPAL, Principal

SHOP_NAME = "가나다식당"
OWNER = "홍길동"
SUPPLIER = "정다운상회"
ANSWER = f"{SHOP_NAME} {OWNER} 사장이 {SUPPLIER} 에서 재료를 받아 골목 손님을 맞았다"
BASE_URL = "https://owner.example.test"
SECRET = "test-secret"


@dataclass
class Env:
    app: FastAPI
    client: TestClient
    card_id: str
    org_id: str
    reviewer_id: str


@pytest.fixture
def env(
    tmp_path: Path,
    app_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    reviewer_headers: dict[str, str],
) -> Iterator[Env]:
    monkeypatch.setenv("SHOPLEGACY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", app_dsn)
    monkeypatch.setenv("SHOPLEGACY_JWT_SECRET", SECRET)
    monkeypatch.setenv("SHOPLEGACY_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_STT_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_PUBLIC_BASE_URL", BASE_URL + "/")
    monkeypatch.setenv("SHOPLEGACY_OWNER_LINK_TTL_HOURS", "2")
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
    created = client.post("/api/shops", json=shop)
    assert created.status_code == 201, created.text
    shop_id = created.json()["shop_id"]
    sid = client.post("/api/sessions", json={"shop_id": shop_id}).json()["session_id"]
    for _ in range(3):
        assert client.post(f"/api/sessions/{sid}/answer", json={"text": ANSWER}).status_code == 200
    end = client.post(f"/api/sessions/{sid}/end")
    assert end.status_code == 200
    bearer = reviewer_headers["Authorization"].split(" ", 1)[1]
    reviewer_id = jwt.decode(bearer, options={"verify_signature": False})["sub"]
    yield Env(app, client, end.json()["card_id"], reviewer_headers["X-Org-Id"], reviewer_id)
    app.dependency_overrides.clear()


def _anon(env: Env) -> TestClient:
    return TestClient(env.app)


def _link(env: Env, phone: str | None = None) -> dict[str, str]:
    resp = env.client.post(f"/api/cards/{env.card_id}/owner-link", json={"phone": phone})
    assert resp.status_code == 201, resp.text
    body: dict[str, str] = resp.json()
    return body


def _token(url: str) -> str:
    return url.rsplit("/owner/", 1)[1]


def _set_clock(env: Env, now: datetime) -> None:
    clock: Callable[[], datetime] = lambda: now
    env.app.dependency_overrides[routes_owner.get_clock] = lambda: clock


def _audit(pg_dsn: str, action: str, row_id: str) -> list[dict[str, object]]:
    with psycopg.connect(pg_dsn) as conn:
        rows = conn.execute(
            "SELECT new_json FROM public.audit_log WHERE action = %s AND row_id = %s",
            (action, row_id),
        ).fetchall()
    return [r[0] for r in rows]


def test_create_link_returns_public_url_and_audits(env: Env, pg_dsn: str) -> None:
    body = _link(env)
    assert body["url"].startswith(f"{BASE_URL}/owner/")
    assert body["delivery"] == "log"
    assert body["token_id"]
    expires = datetime.fromisoformat(body["expires_at"])
    remaining = expires - datetime.now(UTC)
    assert timedelta(hours=1, minutes=58) < remaining <= timedelta(hours=2)
    audits = _audit(pg_dsn, "card.owner_link", env.card_id)
    assert len(audits) == 1 and audits[0]["token_id"] == body["token_id"]
    assert "phone" not in audits[0]


def test_create_link_with_phone_logs_sms(env: Env, pg_dsn: str) -> None:
    with structlog.testing.capture_logs() as logs:
        body = _link(env, "010-1234-5678")
    assert body["delivery"] == "log"
    sent = [e for e in logs if e["event"] == "sms_log"]
    assert len(sent) == 1 and sent[0]["to"] == "5678"
    assert "01012345678" not in repr(_audit(pg_dsn, "card.owner_link", env.card_id))


def test_create_link_rejects_bad_phone_and_unknown_card(env: Env) -> None:
    bad = env.client.post(f"/api/cards/{env.card_id}/owner-link", json={"phone": "02-123-4567"})
    assert bad.status_code == 422 and bad.json()["detail"] == "invalid phone"
    missing = env.client.post("/api/cards/missing/owner-link", json={"phone": None})
    assert missing.status_code == 404
    anon = _anon(env).post(f"/api/cards/{env.card_id}/owner-link", json={"phone": None})
    assert anon.status_code == 401


def test_view_masks_shop_identity(env: Env) -> None:
    body = _link(env)
    resp = _anon(env).get(f"/api/owner/{_token(body['url'])}")
    assert resp.status_code == 200, resp.text
    view = resp.json()
    assert view["card_id"] == env.card_id
    assert view["trade"] == "restaurant" and view["status"] == "draft"
    assert view["version"] == 0 and view["decision"] is None
    assert view["sections"] and any(view["sections"].values())
    assert datetime.fromisoformat(view["expires_at"]) == datetime.fromisoformat(body["expires_at"])
    for secret in (SHOP_NAME, OWNER, SUPPLIER):
        assert secret not in resp.text
    assert "shop_id" not in view and "org_id" not in view


def test_expired_link_is_gone(env: Env) -> None:
    _set_clock(env, datetime.now(UTC) - timedelta(hours=3))
    token = _token(_link(env)["url"])
    anon = _anon(env)
    view = anon.get(f"/api/owner/{token}")
    assert view.status_code == 410 and view.json()["detail"] == "link_expired"
    decide = anon.post(f"/api/owner/{token}/decision", json={"decision": "approve"})
    assert decide.status_code == 410


def test_tampered_or_foreign_tokens_are_invalid(env: Env) -> None:
    token = _token(_link(env)["url"])
    anon = _anon(env)
    head, payload, sig = token.split(".")
    forged = f"{head}.{payload}.{sig[:-2]}{'AA' if sig[-2:] != 'AA' else 'BB'}"
    other_key = jwt.encode(jwt.decode(token, SECRET, algorithms=["HS256"]), "other", "HS256")
    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    wrong_scope = jwt.encode({**claims, "scope": "login"}, SECRET, algorithm="HS256")
    for bad in (forged, other_key, wrong_scope, "not-a-token"):
        resp = anon.get(f"/api/owner/{bad}")
        assert resp.status_code == 401 and resp.json()["detail"] == "link_invalid"
    decide = anon.post(f"/api/owner/{wrong_scope}/decision", json={"decision": "approve"})
    assert decide.status_code == 401
    gone = jwt.encode({**claims, "card_id": "missing"}, SECRET, algorithm="HS256")
    assert anon.get(f"/api/owner/{gone}").status_code == 404


def test_approve_then_view_shows_decision(env: Env, pg_dsn: str) -> None:
    token = _token(_link(env)["url"])
    anon = _anon(env)
    resp = anon.post(
        f"/api/owner/{token}/decision", json={"decision": "approve", "comment": "좋아요"}
    )
    assert resp.status_code == 201, resp.text
    review = resp.json()
    assert review["card_id"] == env.card_id and review["card_version"] == 0
    assert review["decision"] == "approve" and review["comment"] == "좋아요"
    decision = anon.get(f"/api/owner/{token}").json()["decision"]
    assert decision["decision"] == "approve" and decision["comment"] == "좋아요"
    assert len(_audit(pg_dsn, "owner_review.decide", review["review_id"])) == 1


def test_decision_body_is_validated(env: Env) -> None:
    token = _token(_link(env)["url"])
    anon = _anon(env)
    bad = anon.post(f"/api/owner/{token}/decision", json={"decision": "maybe"})
    assert bad.status_code == 422
    long = anon.post(
        f"/api/owner/{token}/decision", json={"decision": "approve", "comment": "가" * 501}
    )
    assert long.status_code == 422


def test_same_version_decided_twice_conflicts(env: Env) -> None:
    token = _token(_link(env)["url"])
    anon = _anon(env)
    first = anon.post(f"/api/owner/{token}/decision", json={"decision": "request_changes"})
    assert first.status_code == 201
    again = anon.post(f"/api/owner/{token}/decision", json={"decision": "approve"})
    assert again.status_code == 409 and again.json()["detail"] == "already_decided"


def test_new_version_can_be_decided_again(env: Env, admin_conn: psycopg.Connection) -> None:
    token = _token(_link(env)["url"])
    anon = _anon(env)
    assert (
        anon.post(f"/api/owner/{token}/decision", json={"decision": "approve"}).status_code == 201
    )
    admin_conn.execute(
        "UPDATE public.cards SET version = version + 1 WHERE card_id = %s", (env.card_id,)
    )
    view = anon.get(f"/api/owner/{token}").json()
    assert view["version"] == 1 and view["decision"] is None
    second = anon.post(
        f"/api/owner/{token}/decision", json={"decision": "request_changes", "comment": "메뉴"}
    )
    assert second.status_code == 201 and second.json()["card_version"] == 1

    state = env.app.state.shop
    principal = Principal(
        user_id=env.reviewer_id, email=None, org_id=env.org_id, role="reviewer", claims={}
    )
    reset = PRINCIPAL.set(principal)
    try:
        latest = state.store.latest_owner_review(env.card_id)
    finally:
        PRINCIPAL.reset(reset)
    assert latest is not None
    assert latest.card_version == 1 and latest.decision == "request_changes"
    assert latest.comment == "메뉴"


def test_issue_and_verify_round_trip() -> None:
    settings = load_settings(
        {"SHOPLEGACY_DATABASE_URL": "postgresql://x/y", "SHOPLEGACY_JWT_SECRET": SECRET}
    )
    now = datetime.now(UTC)
    link = issue_link("c1", "s1", "o1", settings, now)
    claims = verify_link(link.token, settings)
    assert claims["scope"] == OWNER_SCOPE and claims["jti"] == link.token_id
    assert (claims["card_id"], claims["shop_id"], claims["org_id"]) == ("c1", "s1", "o1")
    old = issue_link("c1", "s1", "o1", settings, now - timedelta(hours=100))
    with pytest.raises(LinkExpiredError):
        verify_link(old.token, settings)
    other = load_settings(
        {"SHOPLEGACY_DATABASE_URL": "postgresql://x/y", "SHOPLEGACY_JWT_SECRET": "other"}
    )
    with pytest.raises(LinkInvalidError):
        verify_link(link.token, other)
    jwks_only = load_settings(
        {"SHOPLEGACY_DATABASE_URL": "postgresql://x/y", "SHOPLEGACY_JWKS_URL": "https://jwks.test"}
    )
    with pytest.raises(RuntimeError, match="SHOPLEGACY_JWT_SECRET"):
        issue_link("c1", "s1", "o1", jwks_only, now)


def _links(env: Env) -> list[dict[str, str]]:
    resp = env.client.get(f"/api/cards/{env.card_id}/owner-links")
    assert resp.status_code == 200, resp.text
    body: list[dict[str, str]] = resp.json()
    return body


def _revoke(env: Env, token_id: str) -> int:
    return env.client.delete(f"/api/cards/{env.card_id}/owner-links/{token_id}").status_code


def _foreign_reviewer(pg_dsn: str, jwt_secret: str) -> dict[str, str]:
    org_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
    email = f"foreign-{org_id[:8]}@example.test"
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s)", (user_id, email))
        conn.execute(
            "INSERT INTO public.orgs (org_id, name, created_by) VALUES (%s, 'foreign', %s)",
            (org_id, user_id),
        )
        conn.execute(
            "INSERT INTO public.org_members (org_id, user_id, role) VALUES (%s, %s, 'reviewer')",
            (org_id, user_id),
        )
    claims = {
        "sub": user_id,
        "email": email,
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + 3600,
    }
    return {
        "Authorization": f"Bearer {jwt.encode(claims, jwt_secret, algorithm='HS256')}",
        "X-Org-Id": org_id,
    }


def test_create_link_stores_owner_link(env: Env, pg_dsn: str) -> None:
    body = _link(env)
    with psycopg.connect(pg_dsn) as conn:
        rows = conn.execute(
            "SELECT org_id::text, card_id, expires_at, revoked_at FROM public.owner_links "
            "WHERE token_id = %s",
            (body["token_id"],),
        ).fetchall()
    assert len(rows) == 1
    org_id, card_id, expires_at, revoked_at = rows[0]
    assert (org_id, card_id, revoked_at) == (env.org_id, env.card_id, None)
    assert expires_at == datetime.fromisoformat(body["expires_at"])


def test_owner_links_list_states(env: Env, pg_dsn: str, jwt_secret: str) -> None:
    _set_clock(env, datetime.now(UTC) - timedelta(hours=3))
    expired = _link(env)["token_id"]
    _set_clock(env, datetime.now(UTC) - timedelta(minutes=2))
    revoked = _link(env)["token_id"]
    _set_clock(env, datetime.now(UTC))
    active = _link(env)["token_id"]
    assert _revoke(env, revoked) == 204
    links = _links(env)
    assert [(link["token_id"], link["state"]) for link in links] == [
        (active, "active"),
        (revoked, "revoked"),
        (expired, "expired"),
    ]
    assert links[1]["revoked_at"] is not None and links[0]["revoked_at"] is None
    assert set(links[0]) == {"token_id", "issued_at", "expires_at", "revoked_at", "state"}
    url = f"/api/cards/{env.card_id}/owner-links"
    assert _anon(env).get(url).status_code == 401
    foreign = TestClient(env.app, headers=_foreign_reviewer(pg_dsn, jwt_secret))
    assert foreign.get(url).status_code == 404
    assert foreign.delete(f"{url}/{active}").status_code == 404
    assert env.client.get("/api/cards/missing/owner-links").status_code == 404


def test_revoke_owner_link_204_409_404(env: Env, pg_dsn: str) -> None:
    token_id = _link(env)["token_id"]
    assert _revoke(env, token_id) == 204
    again = env.client.delete(f"/api/cards/{env.card_id}/owner-links/{token_id}")
    assert again.status_code == 409 and again.json()["detail"] == "already_revoked"
    assert _revoke(env, "no-such-token") == 404
    audits = _audit(pg_dsn, "card.owner_link.revoke", env.card_id)
    assert len(audits) == 1 and audits[0]["token_id"] == token_id


def test_revoked_link_is_gone(env: Env) -> None:
    body = _link(env)
    token = _token(body["url"])
    anon = _anon(env)
    assert anon.get(f"/api/owner/{token}").status_code == 200
    assert _revoke(env, body["token_id"]) == 204
    view = anon.get(f"/api/owner/{token}")
    assert view.status_code == 410 and view.json()["detail"] == "link_revoked"
    decide = anon.post(f"/api/owner/{token}/decision", json={"decision": "approve"})
    assert decide.status_code == 410 and decide.json()["detail"] == "link_revoked"


def test_link_without_owner_links_row_still_works(env: Env) -> None:
    claims = jwt.decode(_token(_link(env)["url"]), SECRET, algorithms=["HS256"])
    unknown = jwt.encode({**claims, "jti": uuid.uuid4().hex}, SECRET, algorithm="HS256")
    anon = _anon(env)
    view = anon.get(f"/api/owner/{unknown}")
    assert view.status_code == 200, view.text
    decide = anon.post(f"/api/owner/{unknown}/decision", json={"decision": "approve"})
    assert decide.status_code == 201, decide.text
