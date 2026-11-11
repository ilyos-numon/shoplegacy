"""RLS isolation tests against the migrated schema (needs `supabase start`)."""

import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

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
        " years, closed_year, supplier_names_json, created_at)"
        " VALUES (%s, %s, %s, '가게', '사장', 'restaurant', 'residential', 20, 2025, '[]', %s)",
        (shop_id, org_id, user_id, datetime.now(UTC)),
    )


def insert_card(
    conn: psycopg.Connection, card_id: str, org_id: str, shop_id: str, status: str
) -> None:
    conn.execute(
        "INSERT INTO cards (card_id, org_id, shop_id, status, sections_json, warnings_json,"
        " sessions, updated_at) VALUES (%s, %s, %s, %s, '{}', '[]', 1, %s)",
        (card_id, org_id, shop_id, status, datetime.now(UTC)),
    )


@pytest.fixture
def two_orgs(pg_dsn: str, app_dsn: str, make_user: Callable[[str, str], str]) -> dict[str, str]:
    a = make_user(pg_dsn, "a@example.test")
    b = make_user(pg_dsn, "b@example.test")
    return {
        "user_a": a,
        "user_b": b,
        "org_a": create_org(app_dsn, a, "org A"),
        "org_b": create_org(app_dsn, b, "org B"),
    }


def test_org_b_cannot_see_org_a_shop(app_dsn: str, two_orgs: dict[str, str]) -> None:
    with as_role(app_dsn, two_orgs["user_a"]) as conn:
        insert_shop(conn, "shop-a", two_orgs["org_a"], two_orgs["user_a"])
    with as_role(app_dsn, two_orgs["user_a"]) as conn:
        assert conn.execute("SELECT count(*) FROM shops").fetchone() == (1,)
    with as_role(app_dsn, two_orgs["user_b"]) as conn:
        assert conn.execute("SELECT count(*) FROM shops").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM orgs").fetchone() == (1,)


def test_org_b_cannot_insert_into_org_a(app_dsn: str, two_orgs: dict[str, str]) -> None:
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, two_orgs["user_b"]) as conn:
        insert_shop(conn, "shop-x", two_orgs["org_a"], two_orgs["user_b"])


def test_anon_reads_only_published_cards(app_dsn: str, two_orgs: dict[str, str]) -> None:
    with as_role(app_dsn, two_orgs["user_a"]) as conn:
        insert_shop(conn, "shop-1", two_orgs["org_a"], two_orgs["user_a"])
        insert_shop(conn, "shop-2", two_orgs["org_a"], two_orgs["user_a"])
        insert_card(conn, "card-1", two_orgs["org_a"], "shop-1", "published")
        insert_card(conn, "card-2", two_orgs["org_a"], "shop-2", "draft")
    with as_role(app_dsn, None) as conn:
        rows = conn.execute("SELECT card_id FROM cards ORDER BY card_id").fetchall()
    assert rows == [("card-1",)]
    with as_role(app_dsn, two_orgs["user_b"]) as conn:
        rows = conn.execute("SELECT card_id FROM cards ORDER BY card_id").fetchall()
    assert rows == [("card-1",)]


def test_anon_insert_is_denied(app_dsn: str, two_orgs: dict[str, str]) -> None:
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, None) as conn:
        insert_shop(conn, "shop-anon", two_orgs["org_a"], two_orgs["user_a"])
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, None) as conn:
        conn.execute("INSERT INTO orgs (name, created_by) VALUES ('x', %s)", (two_orgs["user_a"],))


def test_shop_insert_writes_audit_row(app_dsn: str, two_orgs: dict[str, str]) -> None:
    with as_role(app_dsn, two_orgs["user_a"]) as conn:
        insert_shop(conn, "shop-audit", two_orgs["org_a"], two_orgs["user_a"])
    with as_role(app_dsn, two_orgs["user_a"]) as conn:
        rows = conn.execute(
            "SELECT org_id::text, actor_id::text, action, row_id, new_json ->> 'name'"
            " FROM audit_log WHERE table_name = 'shops'"
        ).fetchall()
    assert rows == [(two_orgs["org_a"], two_orgs["user_a"], "INSERT", "shop-audit", "가게")]
    with as_role(app_dsn, two_orgs["user_b"]) as conn:
        assert conn.execute("SELECT count(*) FROM audit_log").fetchone() == (1,)
        # B sees only its own org's member row audit, never org A's shop.
        assert conn.execute(
            "SELECT count(*) FROM audit_log WHERE table_name = 'shops'"
        ).fetchone() == (0,)


def test_has_role(
    pg_dsn: str, app_dsn: str, two_orgs: dict[str, str], make_user: Callable[[str, str], str]
) -> None:
    reviewer = make_user(pg_dsn, "r@example.test")
    with as_role(app_dsn, two_orgs["user_a"]) as conn:
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'reviewer')",
            (two_orgs["org_a"], reviewer),
        )
    a, org_a, org_b = two_orgs["user_a"], two_orgs["org_a"], two_orgs["org_b"]
    with as_role(app_dsn, a) as conn:
        assert conn.execute("SELECT has_role(%s, '{owner,admin}')", (org_a,)).fetchone() == (True,)
        assert conn.execute("SELECT has_role(%s, '{owner}')", (org_b,)).fetchone() == (False,)
    with as_role(app_dsn, reviewer) as conn:
        assert conn.execute("SELECT has_role(%s, '{reviewer}')", (org_a,)).fetchone() == (True,)
        assert conn.execute("SELECT has_role(%s, '{owner,admin}')", (org_a,)).fetchone() == (False,)
        # A reviewer cannot add members (admin_write policy).
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, reviewer) as conn:
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'admin')",
            (org_a, reviewer),
        )


def test_non_creator_cannot_claim_ownership(app_dsn: str, two_orgs: dict[str, str]) -> None:
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, two_orgs["user_b"]) as conn:
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'owner')",
            (two_orgs["org_a"], two_orgs["user_b"]),
        )


def test_user_id_by_email_and_current_org_ids(app_dsn: str, two_orgs: dict[str, str]) -> None:
    with as_role(app_dsn, two_orgs["user_a"]) as conn:
        assert conn.execute("SELECT user_id_by_email('B@example.test')::text").fetchone() == (
            two_orgs["user_b"],
        )
        assert conn.execute("SELECT current_org_ids()::text").fetchall() == [(two_orgs["org_a"],)]


def test_anon_gets_tags_of_published_card_shop(app_dsn: str, two_orgs: dict[str, str]) -> None:
    with as_role(app_dsn, two_orgs["user_a"]) as conn:
        insert_shop(conn, "shop-pub", two_orgs["org_a"], two_orgs["user_a"])
        insert_card(conn, "card-pub", two_orgs["org_a"], "shop-pub", "published")
    with as_role(app_dsn, None) as conn:
        cur = conn.execute("SELECT * FROM public.public_card_tags_v2('shop-pub')")
        assert [d.name for d in cur.description or []] == [
            "trade",
            "area_type",
            "years",
            "closed_year",
            "region_sigungu",
        ]
        assert cur.fetchall() == [("restaurant", "residential", 20, 2025, None)]


def test_anon_gets_no_tags_for_draft_only_shop(app_dsn: str, two_orgs: dict[str, str]) -> None:
    with as_role(app_dsn, two_orgs["user_a"]) as conn:
        insert_shop(conn, "shop-draft", two_orgs["org_a"], two_orgs["user_a"])
        insert_card(conn, "card-draft", two_orgs["org_a"], "shop-draft", "draft")
        insert_shop(conn, "shop-none", two_orgs["org_a"], two_orgs["user_a"])
    with as_role(app_dsn, None) as conn:
        for shop_id in ("shop-draft", "shop-none", "missing"):
            rows = conn.execute(
                "SELECT * FROM public.public_card_tags_v2(%s)", (shop_id,)
            ).fetchall()
            assert rows == []
