import csv
import io
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook  # type: ignore[import-untyped]

from shoplegacy import routes_dashboard
from shoplegacy.config import Settings
from shoplegacy.dashboard import REPORT_COLUMNS, report_csv
from shoplegacy.store import (
    PRINCIPAL,
    CardRow,
    ConsentRow,
    Principal,
    ReportRow,
    SessionRow,
    ShopRow,
    Store,
)

SECRET = "test-secret-that-is-long-enough-for-hs256"
NOW = datetime.now(UTC) - timedelta(hours=1)


@contextmanager
def acting(p: Principal) -> Iterator[None]:
    token = PRINCIPAL.set(p)
    try:
        yield
    finally:
        PRINCIPAL.reset(token)


def _h(user_id: str, email: str, org_id: str) -> dict[str, str]:
    claims: dict[str, Any] = {
        "sub": user_id,
        "email": email,
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + 300,
    }
    token = jwt.encode(claims, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}", "X-Org-Id": org_id}


class Ctx:
    def __init__(self, client: TestClient, store: Store, settings: Settings) -> None:
        self.client = client
        self.store = store
        self.settings = settings
        self.org_id = ""
        self.owner: Principal | None = None
        self.admin: dict[str, str] = {}
        self.reviewer: dict[str, str] = {}

    def url(self, kind: str, query: str = "") -> str:
        return f"/api/orgs/{self.org_id}/{kind}{query}"


@pytest.fixture
def ctx(
    app_dsn: str, pg_dsn: str, tmp_path: Path, make_user: Callable[[str, str], str]
) -> Iterator[Ctx]:
    settings = Settings(
        provider="fake",
        stt_provider="fake",
        data_dir=tmp_path,
        database_url=app_dsn,
        jwt_secret=SECRET,
        report_max_rows=2,
    )
    store = Store(app_dsn)
    app = FastAPI()
    app.state.shop = SimpleNamespace(settings=settings, store=store)
    app.include_router(routes_dashboard.router, prefix="/api")
    c = Ctx(TestClient(app), store, settings)
    owner_id = make_user(pg_dsn, "owner@example.test")
    with acting(Principal(owner_id, "owner@example.test", None, None, {})):
        org = store.create_org("org", owner_id)
    c.org_id = org.org_id
    c.owner = Principal(owner_id, "owner@example.test", org.org_id, "owner", {})
    admin_id = make_user(pg_dsn, "admin@example.test")
    reviewer_id = make_user(pg_dsn, "reviewer@example.test")
    with acting(c.owner):
        store.upsert_member(org.org_id, admin_id, "admin")
        store.upsert_member(org.org_id, reviewer_id, "reviewer")
    c.admin = _h(admin_id, "admin@example.test", org.org_id)
    c.reviewer = _h(reviewer_id, "reviewer@example.test", org.org_id)
    try:
        yield c
    finally:
        store.close()


def _shop(shop_id: str, at: datetime) -> ShopRow:
    return ShopRow(
        shop_id=shop_id,
        name="=테스트 식당",
        owner_name="김사장",
        trade="tree_min",
        area_type="station",
        years=20,
        closed_year=2025,
        supplier_names=[],
        created_at=at,
    )


def _session(session_id: str, shop_id: str, number: int) -> SessionRow:
    return SessionRow(
        session_id=session_id,
        shop_id=shop_id,
        trade="tree_min",
        number=number,
        started_at=NOW,
        deadline=NOW + timedelta(seconds=1200),
        ended_at=NOW,
        node_index=0,
        current_json=None,
    )


def _consent(consent_id: str, shop_id: str, user_id: str, revoked: bool) -> ConsentRow:
    return ConsentRow(
        consent_id=consent_id,
        org_id="",
        shop_id=shop_id,
        kind="recording",
        method="verbal",
        note=None,
        granted_by=user_id,
        granted_at=NOW,
        revoked_at=NOW if revoked else None,
        revoke_reason="요청" if revoked else None,
    )


@pytest.fixture
def seeded(ctx: Ctx) -> Ctx:
    """2 shops, 3 ended sessions, 1 published card, 2 recording consents (1 revoked)."""
    assert ctx.owner is not None
    with acting(ctx.owner):
        ctx.store.insert_shop(_shop("s1", NOW))
        ctx.store.insert_shop(_shop("s2", NOW + timedelta(minutes=1)))
        ctx.store.insert_session(_session("a", "s1", 1))
        ctx.store.insert_session(_session("b", "s1", 2))
        ctx.store.insert_session(_session("c", "s2", 1))
        ctx.store.upsert_card(CardRow("c1", "s1", "published", "{}", "[]", 2, NOW, version=1))
        ctx.store.upsert_card(CardRow("c2", "s2", "draft", "{}", "[]", 1, NOW))
        ctx.store.insert_consent(_consent("k1", "s1", ctx.owner.user_id, False))
        ctx.store.insert_consent(_consent("k2", "s2", ctx.owner.user_id, True))
    return ctx


def test_default_period_is_last_30_days(ctx: Ctx) -> None:
    r = ctx.client.get(ctx.url("dashboard"), headers=ctx.admin)
    assert r.status_code == 200
    body = r.json()
    today = datetime.now(UTC).date()
    assert body["period_to"] == today.isoformat()
    assert body["period_from"] == (today - timedelta(days=29)).isoformat()
    assert body["shops"] == 0
    assert body["consents"]["recording"] == {"granted": 0, "revoked": 0}


def test_from_after_to_is_422(ctx: Ctx) -> None:
    for kind in ("dashboard", "report"):
        r = ctx.client.get(ctx.url(kind, "?from=2026-10-02&to=2026-10-01"), headers=ctx.admin)
        assert (r.status_code, r.json()) == (422, {"detail": "from_after_to"})
    r = ctx.client.get(ctx.url("dashboard", "?from=not-a-date"), headers=ctx.admin)
    assert r.status_code == 422


def test_dashboard_counts_match(seeded: Ctx) -> None:
    r = seeded.client.get(seeded.url("dashboard"), headers=seeded.admin)
    assert r.status_code == 200
    body = r.json()
    assert (body["shops"], body["interviews"], body["published_cards"]) == (2, 3, 1)
    assert body["cards_by_status"] == {"published": 1, "draft": 1}
    assert body["consents"]["recording"] == {"granted": 2, "revoked": 1}
    assert body["consents"]["publication"] == {"granted": 0, "revoked": 0}
    past = seeded.client.get(
        seeded.url("dashboard", "?from=2020-01-01&to=2020-01-31"), headers=seeded.admin
    ).json()
    assert (past["shops"], past["interviews"], past["published_cards"]) == (0, 0, 0)


def test_reviewer_and_anon_are_rejected(ctx: Ctx) -> None:
    for kind in ("dashboard", "report"):
        assert ctx.client.get(ctx.url(kind), headers=ctx.reviewer).status_code == 403
        assert ctx.client.get(ctx.url(kind)).status_code == 401


def test_csv_report_has_bom_header_and_rows(seeded: Ctx) -> None:
    today = datetime.now(UTC).date()
    since, until = (today - timedelta(days=1)).isoformat(), today.isoformat()
    r = seeded.client.get(
        seeded.url("report", f"?from={since}&to={until}&format=csv"), headers=seeded.admin
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert r.headers["content-disposition"] == (
        f'attachment; filename="report-{since}-{until}.csv"'
    )
    assert r.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0] == REPORT_COLUMNS and len(rows[0]) == 13
    assert len(rows) == 1 + 2
    first = dict(zip(rows[0], rows[1], strict=True))
    assert (first["shop_id"], first["sessions"], first["card_status"]) == ("s1", "2", "published")
    assert first["consent_recording"] == "granted"
    assert "김사장" not in r.content.decode("utf-8-sig")


def test_xlsx_report_opens_with_report_sheet(seeded: Ctx) -> None:
    r = seeded.client.get(seeded.url("report", "?format=xlsx"), headers=seeded.admin)
    assert r.status_code == 200
    assert r.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert r.headers["content-disposition"].endswith('.xlsx"')
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["report"]
    values = list(wb["report"].iter_rows(values_only=True))
    assert list(values[0]) == REPORT_COLUMNS
    assert values[1][0] == "s1"
    assert values[1][3] == 2
    assert isinstance(values[1][12], datetime)


def test_report_max_rows_is_applied(seeded: Ctx) -> None:
    assert seeded.settings.report_max_rows == 2
    with acting(seeded.owner):  # type: ignore[arg-type]
        seeded.store.insert_shop(_shop("s3", NOW + timedelta(minutes=2)))
    for fmt in ("csv", "xlsx"):
        r = seeded.client.get(seeded.url("report", f"?format={fmt}"), headers=seeded.admin)
        assert r.status_code == 200
        if fmt == "csv":
            rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
        else:
            rows = list(load_workbook(io.BytesIO(r.content))["report"].iter_rows(values_only=True))
        assert len(rows) == 1 + 2
        assert [row[0] for row in rows[1:]] == ["s1", "s2"]


def test_csv_guards_against_formula_injection() -> None:
    row = ReportRow(
        shop_id="=cmd()",
        trade="+tree",
        region="-서울",
        sessions=0,
        last_interview_at=None,
        card_status="@x",
        published_at=None,
        consent_recording=None,
        consent_storage=None,
        consent_ai_processing=None,
        consent_publication=None,
        owner_decision=None,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    rows = list(csv.reader(io.StringIO(report_csv([row]).decode("utf-8-sig"))))
    assert rows[1][:4] == ["'=cmd()", "'+tree", "'-서울", "0"]
    assert rows[1][5] == "'@x"
    assert rows[1][12] == "2026-10-01T00:00:00+00:00"
    assert date.fromisoformat(rows[1][12][:10]) == date(2026, 10, 1)
