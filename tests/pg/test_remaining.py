"""Job queue grants, transcription jobs, invoices, owner links and the public
card tags function against the migrated schema (needs `supabase start`)."""

import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest
from psycopg import errors


@contextmanager
def as_role(app_dsn: str, user_id: str | None) -> Iterator[psycopg.Connection]:
    """One transaction as the app role, switched like the Store does."""
    with psycopg.connect(app_dsn) as conn, conn.transaction():
        if user_id is None:
            conn.execute("SET LOCAL ROLE anon")
        else:
            conn.execute("SET LOCAL ROLE authenticated")
            claims = json.dumps({"sub": user_id, "role": "authenticated"})
            conn.execute("SELECT set_config('request.jwt.claims', %s, true)", (claims,))
        yield conn


@contextmanager
def as_app(app_dsn: str) -> Iterator[psycopg.Connection]:
    """One transaction as the app login role itself (worker, webhook, operator)."""
    with psycopg.connect(app_dsn) as conn, conn.transaction():
        yield conn


def create_org(app_dsn: str, user_id: str, name: str) -> str:
    org_id = str(uuid.uuid4())
    with as_role(app_dsn, user_id) as conn:
        conn.execute(
            "INSERT INTO orgs (org_id, name, created_by) VALUES (%s, %s, %s)",
            (org_id, name, user_id),
        )
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'owner')",
            (org_id, user_id),
        )
    return org_id


def insert_shop(conn: psycopg.Connection, shop_id: str, org_id: str, user_id: str) -> None:
    conn.execute(
        "INSERT INTO shops (shop_id, org_id, created_by, name, owner_name, trade, area_type,"
        " years, closed_year, supplier_names_json, created_at, region_sigungu)"
        " VALUES (%s, %s, %s, '가게', '사장', 'restaurant', 'residential', 20, 2025, '[]', %s,"
        " '종로구')",
        (shop_id, org_id, user_id, datetime.now(UTC)),
    )


def insert_card(
    conn: psycopg.Connection, card_id: str, org_id: str, shop_id: str, status: str
) -> None:
    conn.execute(
        "INSERT INTO cards (card_id, org_id, shop_id, status, sections_json, warnings_json,"
        " version, sessions, updated_at)"
        " VALUES (%s, %s, %s, %s, '{}', '[]', 1, 1, %s)",
        (card_id, org_id, shop_id, status, datetime.now(UTC)),
    )


def insert_session(conn: psycopg.Connection, session_id: str, org_id: str, shop_id: str) -> None:
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO sessions (session_id, org_id, shop_id, trade, number, started_at,"
        " deadline, node_index) VALUES (%s, %s, %s, 'restaurant', 1, %s, %s, 0)",
        (session_id, org_id, shop_id, now, now + timedelta(hours=1)),
    )


def insert_stt_job(
    conn: psycopg.Connection, job_id: str, org_id: str, session_id: str, user_id: str
) -> None:
    conn.execute(
        "INSERT INTO stt_jobs (job_id, org_id, session_id, created_by, status, audio_key,"
        " content_type, bytes) VALUES (%s, %s, %s, %s, 'queued', %s, 'audio/webm', 1234)",
        (job_id, org_id, session_id, user_id, f"audio/{job_id}"),
    )


def insert_owner_link(
    conn: psycopg.Connection,
    token_id: str,
    org_id: str,
    card_id: str,
    *,
    expires_at: datetime,
    revoked: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO owner_links (token_id, org_id, card_id, issued_at, expires_at, revoked_at)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (
            token_id,
            org_id,
            card_id,
            datetime.now(UTC) - timedelta(days=1),
            expires_at,
            datetime.now(UTC) if revoked else None,
        ),
    )


def issue_invoice(app_dsn: str, invoice_id: str, org_id: str, plan: str = "pilot") -> tuple:
    with as_app(app_dsn) as conn:
        row = conn.execute(
            "SELECT invoice_id, status, order_id, plan FROM invoice_issue(%s, %s, %s, %s, %s,"
            " 99000, NULL)",
            (invoice_id, org_id, plan, date(2026, 11, 1), date(2026, 12, 1)),
        ).fetchone()
    assert row is not None
    return row


def audit_actions(pg_dsn: str, org_id: str) -> list[tuple]:
    with psycopg.connect(pg_dsn) as conn:
        return conn.execute(
            "SELECT action, table_name, row_id, old_json, new_json FROM audit_log"
            " WHERE org_id = %s AND action IN ('invoice.issue', 'invoice.paid', 'org.plan',"
            " 'invoice.void') ORDER BY id",
            (org_id,),
        ).fetchall()


@pytest.fixture
def org(pg_dsn: str, app_dsn: str, make_user: Callable[[str, str], str]) -> dict[str, str]:
    """Org A (owner a) with a shop, a published card and a session; org B is empty."""
    a = make_user(pg_dsn, "a@example.test")
    b = make_user(pg_dsn, "b@example.test")
    org_a = create_org(app_dsn, a, "org A")
    org_b = create_org(app_dsn, b, "org B")
    with as_role(app_dsn, a) as conn:
        insert_shop(conn, "shop-a", org_a, a)
        insert_card(conn, "card-a", org_a, "shop-a", "published")
        insert_session(conn, "sess-a", org_a, "shop-a")
    return {"user_a": a, "user_b": b, "org_a": org_a, "org_b": org_b}


# --- job queue -----------------------------------------------------------------


@pytest.mark.parametrize("user", [None, "user_a"])
def test_procrastinate_jobs_denied_to_anon_and_authenticated(
    app_dsn: str, org: dict[str, str], user: str | None
) -> None:
    user_id = org[user] if user else None
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, user_id) as conn:
        conn.execute("SELECT count(*) FROM public.procrastinate_jobs")
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, user_id) as conn:
        conn.execute(
            "INSERT INTO public.procrastinate_jobs (queue_name, task_name) VALUES ('stt', 't')"
        )
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, user_id) as conn:
        conn.execute("SELECT procrastinate_register_worker_v1()")


def test_procrastinate_functions_not_executable_by_public(pg_dsn: str) -> None:
    with psycopg.connect(pg_dsn) as conn:
        rows = conn.execute(
            "SELECT p.proname,"
            " has_function_privilege('anon', p.oid, 'EXECUTE'),"
            " has_function_privilege('authenticated', p.oid, 'EXECUTE'),"
            " has_function_privilege('shoplegacy_app', p.oid, 'EXECUTE')"
            " FROM pg_proc p WHERE p.proname LIKE 'procrastinate%'"
        ).fetchall()
        tables = conn.execute(
            "SELECT c.relname,"
            " has_table_privilege('anon', c.oid, 'SELECT'),"
            " has_table_privilege('authenticated', c.oid, 'SELECT')"
            " FROM pg_class c WHERE c.relname LIKE 'procrastinate%' AND c.relkind IN ('r', 'S')"
        ).fetchall()
    assert len(rows) >= 10
    assert all(not anon and not auth and app for _, anon, auth, app in rows), rows
    assert len(tables) == 8
    assert all(not anon and not auth for _, anon, auth in tables), tables


def test_procrastinate_app_role_defers_and_fetches_job(app_dsn: str, org: dict[str, str]) -> None:
    with as_app(app_dsn) as conn:
        conn.execute(
            "INSERT INTO procrastinate_jobs (queue_name, task_name, args)"
            " VALUES ('stt', 'transcribe', %s)",
            (json.dumps({"job_id": "job-1"}),),
        )
        assert conn.execute(
            "SELECT queue_name, task_name, status::text FROM procrastinate_jobs"
        ).fetchall() == [("stt", "transcribe", "todo")]
        worker = conn.execute("SELECT procrastinate_register_worker_v1()").fetchone()
        assert worker is not None
        fetched = conn.execute(
            "SELECT task_name, args ->> 'job_id', status::text"
            " FROM procrastinate_fetch_job_v2(ARRAY['stt']::varchar[], %s)",
            (worker[0],),
        ).fetchone()
    assert fetched == ("transcribe", "job-1", "doing")


# --- transcription jobs --------------------------------------------------------


def test_stt_jobs_are_org_scoped(app_dsn: str, org: dict[str, str]) -> None:
    with as_role(app_dsn, org["user_a"]) as conn:
        insert_stt_job(conn, "job-1", org["org_a"], "sess-a", org["user_a"])
        assert conn.execute("SELECT job_id, status FROM stt_jobs").fetchall() == [
            ("job-1", "queued")
        ]
    with as_role(app_dsn, org["user_b"]) as conn:
        assert conn.execute("SELECT count(*) FROM stt_jobs").fetchone() == (0,)
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, org["user_b"]) as conn:
        insert_stt_job(conn, "job-x", org["org_a"], "sess-a", org["user_b"])
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, None) as conn:
        conn.execute("SELECT count(*) FROM stt_jobs")


def test_stt_job_claim_runs_once(app_dsn: str, org: dict[str, str]) -> None:
    with as_role(app_dsn, org["user_a"]) as conn:
        insert_stt_job(conn, "job-1", org["org_a"], "sess-a", org["user_a"])
    with as_app(app_dsn) as conn:
        first = conn.execute(
            "SELECT job_id, status, started_at IS NOT NULL FROM stt_job_claim('job-1')"
        ).fetchone()
        second = conn.execute("SELECT job_id, status FROM stt_job_claim('job-1')").fetchone()
        missing = conn.execute("SELECT job_id FROM stt_job_claim('missing')").fetchone()
    assert first == ("job-1", "running", True)
    assert second == (None, None)
    assert missing == (None,)


def test_stt_job_finish_and_fail(app_dsn: str, org: dict[str, str]) -> None:
    with as_role(app_dsn, org["user_a"]) as conn:
        insert_stt_job(conn, "job-ok", org["org_a"], "sess-a", org["user_a"])
        insert_stt_job(conn, "job-bad", org["org_a"], "sess-a", org["user_a"])
    with as_app(app_dsn) as conn:
        conn.execute("SELECT stt_job_claim('job-ok')")
        conn.execute("SELECT stt_job_finish('job-ok', 'enc-text', 3.5, 'fake')")
        conn.execute("SELECT stt_job_fail('job-bad', 'decode error')")
        # A finished job is not reopened by a late failure.
        conn.execute("SELECT stt_job_fail('job-ok', 'late')")
    with as_role(app_dsn, org["user_a"]) as conn:
        rows = conn.execute(
            "SELECT job_id, status, text, duration_s, provider, error, finished_at IS NOT NULL"
            " FROM stt_jobs ORDER BY job_id"
        ).fetchall()
    assert rows == [
        ("job-bad", "failed", None, None, None, "decode error", True),
        ("job-ok", "done", "enc-text", 3.5, "fake", None, True),
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT stt_job_claim('job-1')",
        "SELECT stt_job_finish('job-1', 't', 1.0, 'fake')",
        "SELECT stt_job_fail('job-1', 'e')",
        "SELECT invoice_by_order('sl-x')",
        "SELECT invoice_void('x')",
        "SELECT invoice_mark_paid('x', 'card', 'fake', NULL, now())",
        "SELECT invoice_issue('x', gen_random_uuid(), 'pilot', '2026-11-01', '2026-12-01', 1, NULL)",
    ],
)
def test_worker_and_billing_functions_not_callable_by_users(
    app_dsn: str, org: dict[str, str], sql: str
) -> None:
    for user in (None, org["user_a"]):
        with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, user) as conn:
            conn.execute(sql)


# --- invoices ------------------------------------------------------------------


def test_invoice_paid_upgrades_plan_and_audits_twice(
    pg_dsn: str, app_dsn: str, org: dict[str, str]
) -> None:
    org_a = org["org_a"]
    assert issue_invoice(app_dsn, "inv-1", org_a) == ("inv-1", "issued", "sl-inv-1", "pilot")
    paid_at = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    with as_app(app_dsn) as conn:
        row = conn.execute(
            "SELECT status, method, provider, provider_payment_key, paid_at"
            " FROM invoice_mark_paid('inv-1', 'card', 'fake', 'fake_123', %s)",
            (paid_at,),
        ).fetchone()
    assert row == ("paid", "card", "fake", "fake_123", paid_at)
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute("SELECT plan FROM orgs WHERE org_id = %s", (org_a,)).fetchone() == (
            "pilot",
        )
    audit = audit_actions(pg_dsn, org_a)
    assert [(a, t, r) for a, t, r, _, _ in audit] == [
        ("invoice.issue", "invoices", "inv-1"),
        ("invoice.paid", "invoices", "inv-1"),
        ("org.plan", "orgs", org_a),
    ]
    assert audit[2][3:] == ({"plan": "free"}, {"plan": "pilot"})


def test_invoice_paid_twice_adds_no_audit(pg_dsn: str, app_dsn: str, org: dict[str, str]) -> None:
    issue_invoice(app_dsn, "inv-1", org["org_a"], plan="org")
    first_at = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    with as_app(app_dsn) as conn:
        conn.execute("SELECT invoice_mark_paid('inv-1', 'card', 'fake', 'fake_1', %s)", (first_at,))
    before = audit_actions(pg_dsn, org["org_a"])
    with as_app(app_dsn) as conn:
        again = conn.execute(
            "SELECT status, provider_payment_key, paid_at FROM invoice_mark_paid('inv-1',"
            " 'contract', 'manual', 'other', now())"
        ).fetchone()
    assert again == ("paid", "fake_1", first_at)
    assert audit_actions(pg_dsn, org["org_a"]) == before
    assert len(before) == 3


def test_void_invoice_cannot_be_paid_and_paid_cannot_be_voided(
    pg_dsn: str, app_dsn: str, org: dict[str, str]
) -> None:
    issue_invoice(app_dsn, "inv-void", org["org_a"])
    with as_app(app_dsn) as conn:
        conn.execute("SELECT invoice_void('inv-void')")
    with pytest.raises(errors.RaiseException, match="invoice_not_payable"), as_app(app_dsn) as conn:
        conn.execute("SELECT invoice_mark_paid('inv-void', 'card', 'fake', 'fake_1', now())")
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute(
            "SELECT plan FROM orgs WHERE org_id = %s", (org["org_a"],)
        ).fetchone() == ("free",)
    issue_invoice(app_dsn, "inv-paid", org["org_a"], plan="org")
    with as_app(app_dsn) as conn:
        conn.execute("SELECT invoice_mark_paid('inv-paid', 'contract', 'manual', NULL, now())")
    with pytest.raises(errors.RaiseException, match="invoice_paid"), as_app(app_dsn) as conn:
        conn.execute("SELECT invoice_void('inv-paid')")
    assert [a for a, *_ in audit_actions(pg_dsn, org["org_a"])] == [
        "invoice.issue",
        "invoice.void",
        "invoice.issue",
        "invoice.paid",
        "org.plan",
    ]


def test_invoices_readable_by_own_org_only(app_dsn: str, org: dict[str, str]) -> None:
    issue_invoice(app_dsn, "inv-1", org["org_a"])
    with pytest.raises(errors.UniqueViolation):
        issue_invoice(app_dsn, "inv-2", org["org_a"])
    with as_role(app_dsn, org["user_a"]) as conn:
        assert conn.execute("SELECT invoice_id, status FROM invoices").fetchall() == [
            ("inv-1", "issued")
        ]
    with as_role(app_dsn, org["user_b"]) as conn:
        assert conn.execute("SELECT count(*) FROM invoices").fetchone() == (0,)
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, org["user_a"]) as conn:
        conn.execute("UPDATE invoices SET status = 'paid' WHERE invoice_id = 'inv-1'")
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, None) as conn:
        conn.execute("SELECT count(*) FROM invoices")


def test_invoice_by_order(app_dsn: str, org: dict[str, str]) -> None:
    issue_invoice(app_dsn, "inv-1", org["org_a"])
    with as_app(app_dsn) as conn:
        found = conn.execute(
            "SELECT invoice_id, org_id::text FROM invoice_by_order('sl-inv-1')"
        ).fetchone()
        missing = conn.execute("SELECT invoice_id FROM invoice_by_order('sl-none')").fetchone()
    assert found == ("inv-1", org["org_a"])
    assert missing == (None,)


# --- owner links ---------------------------------------------------------------


def test_owner_link_state_four_states(app_dsn: str, org: dict[str, str]) -> None:
    now = datetime.now(UTC)
    with as_role(app_dsn, org["user_a"]) as conn:
        insert_owner_link(conn, "jti-active", org["org_a"], "card-a", expires_at=now + timedelta(1))
        insert_owner_link(
            conn, "jti-expired", org["org_a"], "card-a", expires_at=now - timedelta(1)
        )
        insert_owner_link(
            conn, "jti-revoked", org["org_a"], "card-a", expires_at=now + timedelta(1), revoked=True
        )
    for user in (None, org["user_b"]):
        with as_role(app_dsn, user) as conn:
            states = {
                t: conn.execute("SELECT owner_link_state(%s)", (t,)).fetchone()
                for t in ("jti-active", "jti-expired", "jti-revoked", "jti-none")
            }
        assert states == {
            "jti-active": ("active",),
            "jti-expired": ("expired",),
            "jti-revoked": ("revoked",),
            "jti-none": ("unknown",),
        }


def test_owner_links_are_org_scoped(app_dsn: str, org: dict[str, str]) -> None:
    expires = datetime.now(UTC) + timedelta(days=7)
    with as_role(app_dsn, org["user_a"]) as conn:
        insert_owner_link(conn, "jti-1", org["org_a"], "card-a", expires_at=expires)
        cur = conn.execute(
            "UPDATE owner_links SET revoked_at = now(), revoked_by = %s WHERE token_id = 'jti-1'",
            (org["user_a"],),
        )
        assert cur.rowcount == 1
    with as_role(app_dsn, org["user_b"]) as conn:
        assert conn.execute("SELECT count(*) FROM owner_links").fetchone() == (0,)
        cur = conn.execute("UPDATE owner_links SET revoked_at = NULL WHERE token_id = 'jti-1'")
        assert cur.rowcount == 0
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, org["user_b"]) as conn:
        insert_owner_link(conn, "jti-x", org["org_a"], "card-a", expires_at=expires)
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, None) as conn:
        conn.execute("SELECT count(*) FROM owner_links")


# --- public card tags ----------------------------------------------------------


def test_public_card_tags_v1_dropped_and_v2_has_five_columns(
    pg_dsn: str, app_dsn: str, org: dict[str, str]
) -> None:
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM pg_proc WHERE proname = 'public_card_tags'"
        ).fetchone() == (0,)
    with pytest.raises(errors.UndefinedFunction), as_role(app_dsn, None) as conn:
        conn.execute("SELECT * FROM public_card_tags('shop-a')")
    with as_role(app_dsn, None) as conn:
        cur = conn.execute("SELECT * FROM public_card_tags_v2('shop-a')")
        assert len(cur.description or []) == 5
        assert cur.fetchall() == [("restaurant", "residential", 20, 2025, "종로구")]
