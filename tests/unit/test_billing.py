import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import jwt
import psycopg
import pytest
import structlog
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from handoff.contracts import ClaimVerdict

from shoplegacy import maintenance, routes_billing
from shoplegacy.billing import (
    PLANS,
    MeteredLLM,
    PlanLimitError,
    PlanLimits,
    check_limit,
    metered_llm,
)
from shoplegacy.cards import CardTags
from shoplegacy.config import Settings
from shoplegacy.question_tree import Axis, FollowUp
from shoplegacy.store import Principal, ShopRow, Store

OrgTokens = tuple[str, str, str, str]
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0


class CountingLLM:
    """A ShopLLM that counts calls and reports a fixed provider and token usage."""

    def __init__(self, provider: str = "qwen") -> None:
        self.calls = 0
        self.provider = provider

    def last_provider(self) -> str:
        return self.provider

    def last_usage(self) -> Usage:
        return Usage(tokens_in=11, tokens_out=7)

    def follow_up(self, question: str, answer: str, candidates: list[FollowUp]) -> FollowUp | None:
        self.calls += 1
        return None

    def draft_sections(
        self, answers_by_axis: dict[Axis, list[str]], tags: CardTags
    ) -> dict[Axis, str]:
        self.calls += 1
        return {"menu": "LLM 문단"}

    def verify_claims(
        self, question: str, claims: list[str], excerpts: list[str]
    ) -> list[ClaimVerdict]:
        self.calls += 1
        return []


TAGS = CardTags(trade="cafe", area_type="residential", years=3, closed_year=2025)


@pytest.fixture
def store(app_dsn: str) -> Iterator[Store]:
    s = Store(app_dsn)
    try:
        yield s
    finally:
        s.close()


def _shop(org_id: str) -> ShopRow:
    return ShopRow(
        shop_id=str(uuid.uuid4()),
        name="가게",
        owner_name="사장",
        trade="cafe",
        area_type="residential",
        years=3,
        closed_year=2025,
        supplier_names=[],
        created_at=NOW,
        org_id=org_id,
    )


def _add_members(pg_dsn: str, make_user: Callable[[str, str], str], org_id: str, n: int) -> None:
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        for _ in range(n):
            uid = make_user(pg_dsn, f"m-{uuid.uuid4().hex[:8]}@example.test")
            conn.execute(
                "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'interviewer')",
                (org_id, uid),
            )


def _set_plan(pg_dsn: str, org_id: str, plan: str) -> None:
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("UPDATE orgs SET plan = %s WHERE org_id = %s", (plan, org_id))


def test_plans_values() -> None:
    assert PLANS == {
        "free": PlanLimits(shops=3, llm_calls_month=200, members=3),
        "pilot": PlanLimits(shops=30, llm_calls_month=3000, members=10),
        "org": PlanLimits(shops=300, llm_calls_month=30000, members=50),
    }


def test_check_limit_shops(store: Store, org_principal: Principal) -> None:
    org_id = org_principal.org_id or ""
    for _ in range(2):
        store.insert_shop(_shop(org_id))
    check_limit(store, org_id, "shops")  # 2 of 3: passes
    store.insert_shop(_shop(org_id))
    with pytest.raises(PlanLimitError) as err:
        check_limit(store, org_id, "shops")
    assert err.value.resource == "shops"
    assert str(err.value) == "plan_limit:shops"


def test_check_limit_members_and_plan_upgrade(
    store: Store,
    org_principal: Principal,
    pg_dsn: str,
    make_user: Callable[[str, str], str],
) -> None:
    org_id = org_principal.org_id or ""
    _add_members(pg_dsn, make_user, org_id, 1)
    check_limit(store, org_id, "members")  # 2 of 3
    _add_members(pg_dsn, make_user, org_id, 1)
    with pytest.raises(PlanLimitError) as err:
        check_limit(store, org_id, "members")
    assert err.value.resource == "members"
    _set_plan(pg_dsn, org_id, "pilot")
    check_limit(store, org_id, "members")  # 3 of 10


def test_check_limit_rejects_unknown_resource(store: Store, org_principal: Principal) -> None:
    with pytest.raises(ValueError):
        check_limit(store, org_principal.org_id or "", "cards")


def test_metered_llm_records_usage(store: Store, org_principal: Principal) -> None:
    org_id = org_principal.org_id or ""
    inner = CountingLLM()
    llm = MeteredLLM(inner, store, lambda: NOW)
    assert llm.draft_sections({"menu": ["아메리카노"]}, TAGS) == {"menu": "LLM 문단"}
    llm.verify_claims("q", ["c"], ["e"])
    llm.follow_up("q", "a", [])
    assert inner.calls == 3
    assert llm.last_provider() == "qwen"
    assert llm.last_usage().tokens_in == 11
    usage = store.llm_usage_month(org_id, NOW.date())
    assert (usage.calls, usage.tokens_in, usage.tokens_out, usage.fallbacks) == (3, 33, 21, 0)


def test_metered_llm_counts_fallback(
    store: Store, org_principal: Principal, admin_conn: psycopg.Connection
) -> None:
    org_id = org_principal.org_id or ""
    llm = MeteredLLM(CountingLLM(provider="fallback"), store, lambda: NOW)
    llm.draft_sections({"menu": ["라떼"]}, TAGS)
    assert llm.last_provider() == "fallback"
    assert store.llm_usage_month(org_id, NOW.date()).fallbacks == 1
    row = admin_conn.execute("SELECT kind, provider, fallback FROM llm_usage").fetchall()
    assert row == [("draft", "fallback", True)]


def test_metered_llm_over_quota_returns_fake_draft(
    store: Store, org_principal: Principal, admin_conn: psycopg.Connection
) -> None:
    org_id = org_principal.org_id or ""
    admin_conn.execute(
        "INSERT INTO llm_usage (org_id, occurred_at, kind, provider) "
        "SELECT %s, %s, 'draft', 'qwen' FROM generate_series(1, 200)",
        (org_id, NOW),
    )
    inner = CountingLLM()
    llm = MeteredLLM(inner, store, lambda: NOW)
    with structlog.testing.capture_logs() as logs:
        sections = llm.draft_sections({"menu": ["아메리카노 3천원"]}, TAGS)
    assert inner.calls == 0
    assert sections["menu"] == "아메리카노 3천원"
    assert llm.last_provider() == "quota"
    assert any(e["event"] == "llm_quota_exceeded" and e["log_level"] == "warning" for e in logs)
    # the blocked call is not counted
    assert store.llm_usage_month(org_id, NOW.date()).calls == 200


def test_metered_llm_quota_is_per_month(
    store: Store, org_principal: Principal, admin_conn: psycopg.Connection
) -> None:
    org_id = org_principal.org_id or ""
    admin_conn.execute(
        "INSERT INTO llm_usage (org_id, occurred_at, kind, provider) "
        "SELECT %s, %s, 'draft', 'qwen' FROM generate_series(1, 200)",
        (org_id, datetime(2026, 10, 15, tzinfo=UTC)),
    )
    inner = CountingLLM()
    MeteredLLM(inner, store, lambda: NOW).draft_sections({"menu": ["x"]}, TAGS)
    assert inner.calls == 1


def test_metered_llm_without_org_passes_through(store: Store) -> None:
    inner = CountingLLM()
    llm = metered_llm(inner, store)
    assert isinstance(llm, MeteredLLM)
    llm.draft_sections({"menu": ["x"]}, TAGS)
    assert inner.calls == 1
    assert llm.last_provider() == "qwen"


# routes


@pytest.fixture
def client(app_dsn: str, tmp_path: Path, jwt_secret: str) -> Iterator[TestClient]:
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
    app.include_router(routes_billing.router, prefix="/api")
    try:
        yield TestClient(app)
    finally:
        store.close()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_get_plan_reports_usage(
    client: TestClient, org_fixture: OrgTokens, admin_conn: psycopg.Connection
) -> None:
    org_id, _, reviewer, _ = org_fixture
    admin_conn.execute(
        "INSERT INTO shops (shop_id, name, owner_name, trade, area_type, years, closed_year, "
        "supplier_names_json, created_at, org_id, created_by) "
        "SELECT 's1', 'n', 'o', 'cafe', 'residential', 1, 2025, '[]', now(), org_id, created_by "
        "FROM orgs WHERE org_id = %s",
        (org_id,),
    )
    admin_conn.execute(
        "INSERT INTO llm_usage (org_id, kind, provider) VALUES (%s, 'draft', 'fake'), "
        "(%s, 'verify', 'fake')",
        (org_id, org_id),
    )
    r = client.get(f"/api/orgs/{org_id}/plan", headers=_auth(reviewer))
    assert r.status_code == 200
    assert r.json() == {
        "plan": "free",
        "limits": {"shops": 3, "llm_calls_month": 200, "members": 3},
        "usage": {"shops": 1, "llm_calls_month": 2, "members": 3},
        "period": datetime.now(UTC).strftime("%Y-%m"),
    }


def test_get_plan_forbidden_for_non_member(
    client: TestClient,
    org_fixture: OrgTokens,
    pg_dsn: str,
    jwt_secret: str,
    make_user: Callable[[str, str], str],
) -> None:
    other = make_user(pg_dsn, "stranger@example.test")
    claims = {
        "sub": other,
        "email": "stranger@example.test",
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + 300,
    }
    token = jwt.encode(claims, jwt_secret, algorithm="HS256")
    r = client.get(f"/api/orgs/{org_fixture[0]}/plan", headers=_auth(token))
    assert (r.status_code, r.json()) == (403, {"detail": "forbidden"})


def test_put_plan_is_gone(client: TestClient, org_fixture: OrgTokens) -> None:
    org_id, owner, _, _ = org_fixture
    r = client.put(f"/api/orgs/{org_id}/plan", json={"plan": "pilot"}, headers=_auth(owner))
    assert r.status_code == 405
    r = client.get(f"/api/orgs/{org_id}/plan", headers=_auth(owner))
    assert r.json()["plan"] == "free"


def test_cli_set_plan_updates_and_audits(
    org_fixture: OrgTokens,
    pg_dsn: str,
    admin_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    org_id = org_fixture[0]
    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", pg_dsn)
    assert maintenance.main(["set-plan", org_id, "pilot"]) == 0
    assert capsys.readouterr().out.strip() == f"{org_id} free -> pilot"
    row = admin_conn.execute("SELECT plan FROM orgs WHERE org_id = %s", (org_id,)).fetchone()
    assert row is not None and row[0] == "pilot"
    audit = admin_conn.execute(
        "SELECT org_id::text, action, table_name, row_id, new_json FROM audit_log "
        "WHERE action = 'org.plan'"
    ).fetchall()
    assert audit == [(org_id, "org.plan", "orgs", org_id, {"from": "free", "to": "pilot"})]


def test_cli_set_plan_rejects_unknown_plan_and_org(
    org_fixture: OrgTokens, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", pg_dsn)
    with pytest.raises(SystemExit):
        maintenance.main(["set-plan", org_fixture[0], "enterprise"])
    with pytest.raises(SystemExit):
        maintenance.main(["set-plan", str(uuid.uuid4()), "pilot"])


def test_enforce_limit_maps_to_402(store: Store, org_principal: Principal) -> None:
    org_id = org_principal.org_id or ""
    routes_billing.enforce_limit(store, org_id, "shops")
    for _ in range(3):
        store.insert_shop(_shop(org_id))
    with pytest.raises(HTTPException) as err:
        routes_billing.enforce_limit(store, org_id, "shops")
    assert (err.value.status_code, err.value.detail) == (402, "plan_limit:shops")
