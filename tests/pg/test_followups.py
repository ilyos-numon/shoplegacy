"""Refunds, tax-invoice records and billing keys against the migrated schema
(needs `supabase start`)."""

import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime

import psycopg
import pytest
from psycopg import errors


@contextmanager
def as_role(app_dsn: str, user_id: str | None) -> Iterator[psycopg.Connection]:
    """One transaction as anon (None) or an authenticated user, like the Store does."""
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
    """One transaction as the app login role itself (worker, operator)."""
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


def issue_invoice(app_dsn: str, invoice_id: str, org_id: str) -> None:
    with as_app(app_dsn) as conn:
        conn.execute(
            "SELECT invoice_issue(%s, %s, 'pilot', %s, %s, 99000, NULL)",
            (invoice_id, org_id, date(2026, 11, 1), date(2026, 12, 1)),
        )


def pay_invoice(app_dsn: str, invoice_id: str) -> None:
    with as_app(app_dsn) as conn:
        conn.execute("SELECT invoice_mark_paid(%s, 'card', 'fake', 'fake_1', now())", (invoice_id,))


def set_key(app_dsn: str, org_id: str, user_id: str | None, next_on: date) -> tuple:
    with as_app(app_dsn) as conn:
        row = conn.execute(
            "SELECT org_id::text, status, failures, next_charge_on FROM billing_key_set("
            "%s, 'fake', 'enc-secret-key', %s, '테스트카드 ****1234', 'pilot', 1, %s, %s)",
            (org_id, f"sl-{org_id}", next_on, user_id),
        ).fetchone()
    assert row is not None
    return row


def audit(pg_dsn: str, org_id: str, action: str) -> list[tuple]:
    with psycopg.connect(pg_dsn) as conn:
        return conn.execute(
            "SELECT action, old_json, new_json FROM audit_log WHERE org_id = %s AND action = %s"
            " ORDER BY id",
            (org_id, action),
        ).fetchall()


@pytest.fixture
def org(pg_dsn: str, app_dsn: str, make_user: Callable[[str, str], str]) -> dict[str, str]:
    """Two orgs, A and B, each with its owner."""
    a = make_user(pg_dsn, "a@example.test")
    b = make_user(pg_dsn, "b@example.test")
    return {
        "user_a": a,
        "user_b": b,
        "org_a": create_org(app_dsn, a, "org A"),
        "org_b": create_org(app_dsn, b, "org B"),
    }


# --- invoices ------------------------------------------------------------------


def test_invoice_status_allows_refunded(pg_dsn: str, app_dsn: str, org: dict[str, str]) -> None:
    issue_invoice(app_dsn, "inv-1", org["org_a"])
    with psycopg.connect(pg_dsn) as conn:
        conn.execute("UPDATE invoices SET status = 'refunded' WHERE invoice_id = 'inv-1'")
        with pytest.raises(errors.CheckViolation):
            conn.execute("UPDATE invoices SET status = 'cancelled' WHERE invoice_id = 'inv-1'")


def test_invoice_refund_paid_to_refunded_with_one_audit(
    pg_dsn: str, app_dsn: str, org: dict[str, str]
) -> None:
    issue_invoice(app_dsn, "inv-1", org["org_a"])
    pay_invoice(app_dsn, "inv-1")
    at = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)
    with as_app(app_dsn) as conn:
        row = conn.execute(
            "SELECT status, refunded_at, refund_reason FROM invoice_refund('inv-1', '중복 결제', %s)",
            (at,),
        ).fetchone()
    assert row == ("refunded", at, "중복 결제")
    rows = audit(pg_dsn, org["org_a"], "invoice.refund")
    assert len(rows) == 1
    assert rows[0][1]["status"] == "paid"
    assert rows[0][2]["status"] == "refunded"
    # Refunding twice is refused.
    with (
        pytest.raises(errors.RaiseException, match="invoice_not_refundable"),
        as_app(app_dsn) as conn,
    ):
        conn.execute("SELECT invoice_refund('inv-1', 'again', now())")


def test_invoice_refund_of_issued_invoice_is_refused(
    pg_dsn: str, app_dsn: str, org: dict[str, str]
) -> None:
    issue_invoice(app_dsn, "inv-1", org["org_a"])
    with (
        pytest.raises(errors.RaiseException, match="invoice_not_refundable"),
        as_app(app_dsn) as conn,
    ):
        conn.execute("SELECT invoice_refund('inv-1', 'x', now())")
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute(
            "SELECT status, refunded_at FROM invoices WHERE invoice_id = 'inv-1'"
        ).fetchone() == ("issued", None)
    assert audit(pg_dsn, org["org_a"], "invoice.refund") == []


def test_invoice_mark_tax_on_issued_invoice_is_refused(app_dsn: str, org: dict[str, str]) -> None:
    issue_invoice(app_dsn, "inv-1", org["org_a"])
    with pytest.raises(errors.RaiseException, match="invoice_not_payable"), as_app(app_dsn) as conn:
        conn.execute("SELECT invoice_mark_tax('inv-1', '20261104-0001', now())")


def test_invoice_mark_tax_on_paid_and_refunded(
    pg_dsn: str, app_dsn: str, org: dict[str, str]
) -> None:
    issue_invoice(app_dsn, "inv-1", org["org_a"])
    pay_invoice(app_dsn, "inv-1")
    at = datetime(2026, 11, 4, tzinfo=UTC)
    with as_app(app_dsn) as conn:
        row = conn.execute(
            "SELECT status, tax_invoice_ref, tax_invoice_issued_at"
            " FROM invoice_mark_tax('inv-1', '20261104-0001', %s)",
            (at,),
        ).fetchone()
    assert row == ("paid", "20261104-0001", at)
    with as_app(app_dsn) as conn:
        conn.execute("SELECT invoice_refund('inv-1', 'x', now())")
        row = conn.execute(
            "SELECT status, tax_invoice_ref FROM invoice_mark_tax('inv-1', '20261120-0002', NULL)"
        ).fetchone()
    assert row == ("refunded", "20261120-0002")
    assert len(audit(pg_dsn, org["org_a"], "invoice.tax_invoice")) == 2


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT invoice_refund('x', 'r', now())",
        "SELECT invoice_mark_tax('x', 'r', now())",
        (
            "SELECT billing_key_set(gen_random_uuid(), 'fake', 'k', 'c', NULL, 'pilot', 1,"
            " '2026-12-01', NULL)"
        ),
        "SELECT billing_key_stop(gen_random_uuid())",
        "SELECT * FROM billing_keys_due('2026-12-01')",
        "SELECT billing_key_advance(gen_random_uuid(), '2026-12-01')",
        "SELECT billing_key_fail(gen_random_uuid())",
    ],
)
def test_followup_functions_not_callable_by_users(
    app_dsn: str, org: dict[str, str], sql: str
) -> None:
    for user in (None, org["user_a"]):
        with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, user) as conn:
            conn.execute(sql)


# --- billing keys --------------------------------------------------------------


@pytest.mark.parametrize("user", [None, "user_a"])
def test_billing_keys_denied_to_anon_and_authenticated(
    app_dsn: str, org: dict[str, str], user: str | None
) -> None:
    set_key(app_dsn, org["org_a"], org["user_a"], date(2026, 12, 1))
    user_id = org[user] if user else None
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, user_id) as conn:
        conn.execute("SELECT count(*) FROM billing_keys")
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, user_id) as conn:
        conn.execute("UPDATE billing_keys SET status = 'stopped'")


def test_billing_key_set_twice_keeps_one_active_row_and_audits_without_key(
    pg_dsn: str, app_dsn: str, org: dict[str, str]
) -> None:
    org_a, user_a = org["org_a"], org["user_a"]
    assert set_key(app_dsn, org_a, user_a, date(2026, 12, 1)) == (
        org_a,
        "active",
        0,
        date(2026, 12, 1),
    )
    with as_app(app_dsn) as conn:
        conn.execute("SELECT billing_key_fail(%s)", (org_a,))
        conn.execute("SELECT billing_key_stop(%s)", (org_a,))
    assert set_key(app_dsn, org_a, user_a, date(2027, 1, 1)) == (
        org_a,
        "active",
        0,
        date(2027, 1, 1),
    )
    with psycopg.connect(pg_dsn) as conn:
        rows = conn.execute(
            "SELECT status, failures, stopped_at FROM billing_keys WHERE org_id = %s", (org_a,)
        ).fetchall()
    assert rows == [("active", 0, None)]
    audits = audit(pg_dsn, org_a, "billing_key.set")
    assert len(audits) == 2
    for _, old_json, new_json in audits:
        assert old_json is None
        assert set(new_json) == {"card_label", "plan", "next_charge_on"}
        assert "enc-secret-key" not in json.dumps(new_json)


def test_billing_key_fail_three_times_stops_with_one_audit(
    pg_dsn: str, app_dsn: str, org: dict[str, str]
) -> None:
    org_a = org["org_a"]
    set_key(app_dsn, org_a, org["user_a"], date(2026, 12, 1))
    counts = []
    for _ in range(3):
        with as_app(app_dsn) as conn:
            row = conn.execute("SELECT billing_key_fail(%s)", (org_a,)).fetchone()
            assert row is not None
            counts.append(row[0])
    assert counts == [1, 2, 3]
    with psycopg.connect(pg_dsn) as conn:
        row = conn.execute(
            "SELECT status, failures, stopped_at IS NOT NULL FROM billing_keys WHERE org_id = %s",
            (org_a,),
        ).fetchone()
    assert row == ("stopped", 3, True)
    assert len(audit(pg_dsn, org_a, "billing_key.stop")) == 1


def test_billing_key_advance_resets_failures(
    pg_dsn: str, app_dsn: str, org: dict[str, str]
) -> None:
    org_a = org["org_a"]
    set_key(app_dsn, org_a, org["user_a"], date(2026, 12, 1))
    with as_app(app_dsn) as conn:
        conn.execute("SELECT billing_key_fail(%s)", (org_a,))
        conn.execute("SELECT billing_key_advance(%s, %s)", (org_a, date(2027, 1, 1)))
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute(
            "SELECT next_charge_on, failures FROM billing_keys WHERE org_id = %s", (org_a,)
        ).fetchone() == (date(2027, 1, 1), 0)


def test_billing_key_stop_and_missing_key(pg_dsn: str, app_dsn: str, org: dict[str, str]) -> None:
    org_a = org["org_a"]
    with (
        pytest.raises(errors.RaiseException, match="billing_key_not_found"),
        as_app(app_dsn) as conn,
    ):
        conn.execute("SELECT billing_key_stop(%s)", (org_a,))
    set_key(app_dsn, org_a, org["user_a"], date(2026, 12, 1))
    with as_app(app_dsn) as conn:
        conn.execute("SELECT billing_key_stop(%s)", (org_a,))
        conn.execute("SELECT billing_key_stop(%s)", (org_a,))
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute(
            "SELECT status FROM billing_keys WHERE org_id = %s", (org_a,)
        ).fetchone() == ("stopped",)
    assert len(audit(pg_dsn, org_a, "billing_key.stop")) == 1


def test_billing_keys_due_filters_by_date_and_status(
    app_dsn: str, pg_dsn: str, org: dict[str, str], make_user: Callable[[str, str], str]
) -> None:
    org_a, org_b = org["org_a"], org["org_b"]
    c = make_user(pg_dsn, "c@example.test")
    org_c = create_org(app_dsn, c, "org C")
    set_key(app_dsn, org_a, org["user_a"], date(2026, 12, 1))
    set_key(app_dsn, org_b, org["user_b"], date(2026, 12, 15))
    set_key(app_dsn, org_c, c, date(2026, 11, 1))
    with as_app(app_dsn) as conn:
        conn.execute("SELECT billing_key_stop(%s)", (org_c,))

    def due(today: date) -> list[str]:
        with as_app(app_dsn) as conn:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT org_id::text FROM billing_keys_due(%s)", (today,)
                ).fetchall()
            ]

    assert due(date(2026, 11, 30)) == []
    assert due(date(2026, 12, 1)) == [org_a]
    assert due(date(2026, 12, 31)) == sorted([org_a, org_b])


def test_app_role_reads_billing_keys(app_dsn: str, org: dict[str, str]) -> None:
    set_key(app_dsn, org["org_a"], org["user_a"], date(2026, 12, 1))
    with as_app(app_dsn) as conn:
        assert conn.execute("SELECT org_id::text, billing_key FROM billing_keys").fetchall() == [
            (org["org_a"], "enc-secret-key")
        ]
