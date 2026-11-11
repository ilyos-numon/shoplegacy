"""Feedback, owner reviews, LLM usage, last-owner guard and purge against the
migrated schema (needs `supabase start`)."""

import json
import threading
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


def add_member(app_dsn: str, owner_id: str, org_id: str, user_id: str, role: str) -> None:
    with as_role(app_dsn, owner_id) as conn:
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, %s)",
            (org_id, user_id, role),
        )


def insert_shop(
    conn: psycopg.Connection,
    shop_id: str,
    org_id: str,
    user_id: str,
    *,
    region_sigungu: str | None = None,
    retention_until: datetime | None = None,
) -> None:
    conn.execute(
        "INSERT INTO shops (shop_id, org_id, created_by, name, owner_name, trade, area_type,"
        " years, closed_year, supplier_names_json, created_at, region_sigungu, retention_until)"
        " VALUES (%s, %s, %s, '가게', '사장', 'restaurant', 'residential', 20, 2025, '[]', %s,"
        " %s, %s)",
        (shop_id, org_id, user_id, datetime.now(UTC), region_sigungu, retention_until),
    )


def insert_card(
    conn: psycopg.Connection, card_id: str, org_id: str, shop_id: str, status: str
) -> None:
    conn.execute(
        "INSERT INTO cards (card_id, org_id, shop_id, status, sections_json, warnings_json,"
        " version, sessions, updated_at)"
        " VALUES (%s, %s, %s, %s, '{\"origin\": \"시작\"}', '[]', 1, 1, %s)",
        (card_id, org_id, shop_id, status, datetime.now(UTC)),
    )


def add_feedback(conn: psycopg.Connection, card_id: str, vote: str, fingerprint: str) -> None:
    conn.execute(
        "INSERT INTO card_feedback (card_id, vote, fingerprint) VALUES (%s, %s, %s)",
        (card_id, vote, fingerprint),
    )


def owner_count(pg_dsn: str, org_id: str) -> int:
    with psycopg.connect(pg_dsn) as conn:
        row = conn.execute(
            "SELECT count(*) FROM org_members WHERE org_id = %s AND role = 'owner'", (org_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture
def org(pg_dsn: str, app_dsn: str, make_user: Callable[[str, str], str]) -> dict[str, str]:
    """Org A (owner a) with a published and a draft card; org B (owner b) is empty."""
    a = make_user(pg_dsn, "a@example.test")
    b = make_user(pg_dsn, "b@example.test")
    org_a = create_org(app_dsn, a, "org A")
    org_b = create_org(app_dsn, b, "org B")
    with as_role(app_dsn, a) as conn:
        insert_shop(conn, "shop-pub", org_a, a, region_sigungu="종로구")
        insert_shop(conn, "shop-draft", org_a, a)
        insert_card(conn, "card-pub", org_a, "shop-pub", "published")
        insert_card(conn, "card-draft", org_a, "shop-draft", "draft")
    return {"user_a": a, "user_b": b, "org_a": org_a, "org_b": org_b}


def test_anon_feedback_on_published_card_only(
    pg_dsn: str, app_dsn: str, org: dict[str, str]
) -> None:
    with as_role(app_dsn, None) as conn:
        add_feedback(conn, "card-pub", "up", "fp-1")
    # A published card exists, so this also proves the policy compares the new
    # row's own card_id rather than any published card.
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, None) as conn:
        add_feedback(conn, "card-draft", "up", "fp-1")
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute("SELECT card_id FROM card_feedback").fetchall() == [("card-pub",)]


def test_same_fingerprint_same_day_is_unique(app_dsn: str, org: dict[str, str]) -> None:
    with as_role(app_dsn, None) as conn:
        add_feedback(conn, "card-pub", "up", "fp-dup")
    with pytest.raises(errors.UniqueViolation), as_role(app_dsn, None) as conn:
        add_feedback(conn, "card-pub", "down", "fp-dup")
    with as_role(app_dsn, None) as conn:
        add_feedback(conn, "card-pub", "down", "fp-other")


def test_card_feedback_counts_as_anon(pg_dsn: str, app_dsn: str, org: dict[str, str]) -> None:
    with as_role(app_dsn, None) as conn:
        add_feedback(conn, "card-pub", "up", "fp-1")
        add_feedback(conn, "card-pub", "up", "fp-2")
        add_feedback(conn, "card-pub", "down", "fp-3")
    with psycopg.connect(pg_dsn) as conn:
        add_feedback(conn, "card-draft", "up", "fp-admin")
    with as_role(app_dsn, None) as conn:
        assert conn.execute("SELECT * FROM card_feedback_counts('card-pub')").fetchall() == [(2, 1)]
        assert conn.execute("SELECT * FROM card_feedback_counts('card-draft')").fetchall() == [
            (0, 0)
        ]
        assert conn.execute("SELECT * FROM card_feedback_counts('missing')").fetchall() == [(0, 0)]


def test_owner_card_view_as_anon(app_dsn: str, org: dict[str, str]) -> None:
    with as_role(app_dsn, None) as conn:
        row = conn.execute("SELECT owner_card_view('card-draft')").fetchone()
        missing = conn.execute("SELECT owner_card_view('missing')").fetchone()
    assert row is not None
    assert row[0] == {
        "card_id": "card-draft",
        "shop_id": "shop-draft",
        "org_id": org["org_a"],
        "trade": "restaurant",
        "status": "draft",
        "version": 1,
        "sections": {"origin": "시작"},
        "decision": None,
        "comment": None,
        "decided_at": None,
    }
    assert "가게" not in json.dumps(row[0], ensure_ascii=False)
    assert missing == (None,)


def test_owner_review_insert_writes_review_and_audit(app_dsn: str, org: dict[str, str]) -> None:
    with as_role(app_dsn, None) as conn:
        conn.execute(
            "SELECT owner_review_insert('rev-1', 'card-draft', 1, 'jti-1', 'request_changes',"
            " '이름 빼주세요')"
        )
        view = conn.execute("SELECT owner_card_view('card-draft')").fetchone()
    assert view is not None
    assert view[0]["decision"] == "request_changes"
    assert view[0]["comment"] == "이름 빼주세요"
    assert view[0]["decided_at"] is not None
    with pytest.raises(errors.UniqueViolation), as_role(app_dsn, None) as conn:
        conn.execute(
            "SELECT owner_review_insert('rev-2', 'card-draft', 1, 'jti-1', 'approve', NULL)"
        )
    with pytest.raises(errors.NoDataFound), as_role(app_dsn, None) as conn:
        conn.execute("SELECT owner_review_insert('rev-3', 'missing', 1, 'jti-1', 'approve', NULL)")
    with as_role(app_dsn, org["user_a"]) as conn:
        audit = conn.execute(
            "SELECT org_id::text, actor_id, table_name, row_id, new_json ->> 'decision'"
            " FROM audit_log WHERE action = 'owner_review.decide'"
        ).fetchall()
        reviews = conn.execute("SELECT review_id, shop_id FROM owner_reviews").fetchall()
    assert audit == [(org["org_a"], None, "owner_reviews", "rev-1", "request_changes")]
    assert reviews == [("rev-1", "shop-draft")]
    with as_role(app_dsn, org["user_b"]) as conn:
        assert conn.execute("SELECT count(*) FROM owner_reviews").fetchone() == (0,)
    # A direct INSERT bypassing the function is not allowed.
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, org["user_a"]) as conn:
        conn.execute(
            "INSERT INTO owner_reviews (review_id, org_id, shop_id, card_id, card_version,"
            " token_id, decision) VALUES ('rev-x', %s, 'shop-draft', 'card-draft', 1, 'j', 'approve')",
            (org["org_a"],),
        )


def test_llm_usage_is_org_scoped(app_dsn: str, org: dict[str, str]) -> None:
    now = datetime.now(UTC)
    with as_role(app_dsn, org["user_a"]) as conn:
        for kind, tin, tout, fb in (("draft", 100, 50, False), ("verify", 10, 5, True)):
            conn.execute(
                "INSERT INTO llm_usage (org_id, occurred_at, kind, provider, tokens_in,"
                " tokens_out, fallback) VALUES (%s, %s, %s, 'qwen', %s, %s, %s)",
                (org["org_a"], now, kind, tin, tout, fb),
            )
        conn.execute(
            "INSERT INTO llm_usage (org_id, occurred_at, kind, provider)"
            " VALUES (%s, %s, 'draft', 'qwen')",
            (org["org_a"], now - timedelta(days=62)),
        )
        month = conn.execute(
            "SELECT * FROM llm_usage_month(%s, %s)", (org["org_a"], date(now.year, now.month, 1))
        ).fetchall()
    assert month == [(2, 110, 55, 1)]
    with as_role(app_dsn, org["user_b"]) as conn:
        assert conn.execute("SELECT count(*) FROM llm_usage").fetchone() == (0,)
        assert conn.execute(
            "SELECT * FROM llm_usage_month(%s, %s)", (org["org_a"], now.date())
        ).fetchall() == [(0, 0, 0, 0)]
    with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, org["user_b"]) as conn:
        conn.execute(
            "INSERT INTO llm_usage (org_id, kind, provider) VALUES (%s, 'draft', 'qwen')",
            (org["org_a"],),
        )


def test_last_owner_cannot_be_demoted_or_removed(
    pg_dsn: str, app_dsn: str, org: dict[str, str], make_user: Callable[[str, str], str]
) -> None:
    admin = make_user(pg_dsn, "admin@example.test")
    add_member(app_dsn, org["user_a"], org["org_a"], admin, "admin")
    for actor in (org["user_a"], admin):
        with (
            pytest.raises(errors.CheckViolation, match="last_owner"),
            as_role(app_dsn, actor) as conn,
        ):
            conn.execute(
                "UPDATE org_members SET role = 'admin' WHERE org_id = %s AND user_id = %s",
                (org["org_a"], org["user_a"]),
            )
        with (
            pytest.raises(errors.CheckViolation, match="last_owner"),
            as_role(app_dsn, actor) as conn,
        ):
            conn.execute(
                "DELETE FROM org_members WHERE org_id = %s AND user_id = %s",
                (org["org_a"], org["user_a"]),
            )
    assert owner_count(pg_dsn, org["org_a"]) == 1


def test_demote_succeeds_with_two_owners(
    pg_dsn: str, app_dsn: str, org: dict[str, str], make_user: Callable[[str, str], str]
) -> None:
    second = make_user(pg_dsn, "second@example.test")
    add_member(app_dsn, org["user_a"], org["org_a"], second, "owner")
    with as_role(app_dsn, second) as conn:
        cur = conn.execute(
            "UPDATE org_members SET role = 'reviewer' WHERE org_id = %s AND user_id = %s",
            (org["org_a"], org["user_a"]),
        )
        assert cur.rowcount == 1
    assert owner_count(pg_dsn, org["org_a"]) == 1


def test_concurrent_demotions_exactly_one_fails(
    pg_dsn: str, app_dsn: str, org: dict[str, str], make_user: Callable[[str, str], str]
) -> None:
    second = make_user(pg_dsn, "second@example.test")
    admin = make_user(pg_dsn, "admin@example.test")
    add_member(app_dsn, org["user_a"], org["org_a"], second, "owner")
    add_member(app_dsn, org["user_a"], org["org_a"], admin, "admin")
    barrier = threading.Barrier(2)
    outcomes: dict[str, BaseException | None] = {}

    def demote(target: str) -> None:
        try:
            with as_role(app_dsn, admin) as conn:
                barrier.wait(timeout=10)
                conn.execute(
                    "UPDATE org_members SET role = 'admin' WHERE org_id = %s AND user_id = %s",
                    (org["org_a"], target),
                )
                # Hold the transaction open so the other demotion overlaps it.
                conn.execute("SELECT pg_sleep(0.5)")
            outcomes[target] = None
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            outcomes[target] = exc

    threads = [threading.Thread(target=demote, args=(u,)) for u in (org["user_a"], second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    failures = [exc for exc in outcomes.values() if exc is not None]
    assert len(outcomes) == 2
    assert len(failures) == 1
    assert isinstance(failures[0], errors.CheckViolation)
    assert "last_owner" in str(failures[0])
    assert owner_count(pg_dsn, org["org_a"]) == 1


def test_purge_expired_shops(pg_dsn: str, app_dsn: str, org: dict[str, str]) -> None:
    now = datetime.now(UTC)
    a, org_a = org["user_a"], org["org_a"]
    with as_role(app_dsn, a) as conn:
        insert_shop(conn, "shop-old", org_a, a, retention_until=now - timedelta(days=1))
        insert_shop(conn, "shop-new", org_a, a, retention_until=now + timedelta(days=1))
        insert_card(conn, "card-old", org_a, "shop-old", "published")
        conn.execute(
            "INSERT INTO sessions (session_id, org_id, shop_id, trade, number, started_at,"
            " deadline, node_index) VALUES ('sess-old', %s, 'shop-old', 'restaurant', 1, %s, %s, 0)",
            (org_a, now, now),
        )
        conn.execute(
            "INSERT INTO answers (org_id, session_id, question_id, axis, question, answer,"
            " created_at) VALUES (%s, 'sess-old', 'q1', 'origin', '질문', '답', %s)",
            (org_a, now),
        )
        conn.execute(
            "INSERT INTO consents (consent_id, org_id, shop_id, kind, method, granted_by)"
            " VALUES ('c-old', %s, 'shop-old', 'storage', 'verbal', %s)",
            (org_a, a),
        )
        conn.execute(
            "INSERT INTO card_versions (card_id, version, org_id, status, sections_json,"
            " evidence_json) VALUES ('card-old', 1, %s, 'published', '{}', '{}')",
            (org_a,),
        )
    with as_role(app_dsn, None) as conn:
        add_feedback(conn, "card-old", "up", "fp-1")
        conn.execute("SELECT owner_review_insert('rev-old', 'card-old', 1, 'j', 'approve', NULL)")
    # Not callable from a user or anonymous context.
    for user in (None, a):
        with pytest.raises(errors.InsufficientPrivilege), as_role(app_dsn, user) as conn:
            conn.execute("SELECT * FROM purge_expired_shops(%s)", (now,))
    with psycopg.connect(app_dsn) as conn:
        purged = conn.execute("SELECT * FROM purge_expired_shops(%s)", (now,)).fetchall()
    assert purged == [("shop-old",)]
    with psycopg.connect(pg_dsn) as conn:
        for table, where in (
            ("shops", "shop_id = 'shop-old'"),
            ("sessions", "shop_id = 'shop-old'"),
            ("answers", "session_id = 'sess-old'"),
            ("cards", "shop_id = 'shop-old'"),
            ("card_versions", "card_id = 'card-old'"),
            ("consents", "shop_id = 'shop-old'"),
            ("card_feedback", "card_id = 'card-old'"),
            ("owner_reviews", "shop_id = 'shop-old'"),
        ):
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone() == (0,), (
                table
            )
        remaining = conn.execute("SELECT shop_id FROM shops ORDER BY shop_id").fetchall()
        audit = conn.execute(
            "SELECT org_id::text, row_id FROM audit_log WHERE action = 'shop.purge'"
        ).fetchall()
    assert remaining == [("shop-draft",), ("shop-new",), ("shop-pub",)]
    assert audit == [(org_a, "shop-old")]


def test_public_card_tags_v2_returns_region_sigungu(app_dsn: str, org: dict[str, str]) -> None:
    with as_role(app_dsn, None) as conn:
        cur = conn.execute("SELECT * FROM public_card_tags_v2('shop-pub')")
        assert [d.name for d in cur.description or []] == [
            "trade",
            "area_type",
            "years",
            "closed_year",
            "region_sigungu",
        ]
        assert cur.fetchall() == [("restaurant", "residential", 20, 2025, "종로구")]
        assert conn.execute("SELECT * FROM public_card_tags_v2('shop-draft')").fetchall() == []


def test_only_owner_updates_plan_and_retention(
    pg_dsn: str, app_dsn: str, org: dict[str, str], make_user: Callable[[str, str], str]
) -> None:
    admin = make_user(pg_dsn, "admin@example.test")
    add_member(app_dsn, org["user_a"], org["org_a"], admin, "admin")
    with as_role(app_dsn, admin) as conn:
        cur = conn.execute("UPDATE orgs SET plan = 'org' WHERE org_id = %s", (org["org_a"],))
        assert cur.rowcount == 0
    with as_role(app_dsn, org["user_a"]) as conn:
        cur = conn.execute(
            "UPDATE orgs SET plan = 'pilot', retention_days = 365 WHERE org_id = %s",
            (org["org_a"],),
        )
        assert cur.rowcount == 1
        assert conn.execute(
            "SELECT plan, retention_days FROM orgs WHERE org_id = %s", (org["org_a"],)
        ).fetchone() == ("pilot", 365)
    with pytest.raises(errors.CheckViolation), as_role(app_dsn, org["user_a"]) as conn:
        conn.execute("UPDATE orgs SET retention_days = 29 WHERE org_id = %s", (org["org_a"],))


def test_published_card_regions_filters_anonymously(app_dsn: str, org: dict[str, str]) -> None:
    with as_role(app_dsn, org["user_a"]) as conn:
        conn.execute(
            "UPDATE shops SET region_code = '1111000000', region_sido = '서울특별시' "
            "WHERE shop_id IN ('shop-pub', 'shop-draft')"
        )
    with as_role(app_dsn, None) as conn:
        rows = conn.execute(
            "SELECT c.card_id FROM cards c WHERE c.status = 'published' AND c.card_id IN "
            "(SELECT r.card_id FROM published_card_regions() r "
            "WHERE r.region_code LIKE %s AND r.region_sigungu ILIKE %s)",
            ("11%", "%종로%"),
        ).fetchall()
        assert rows == [("card-pub",)]


def test_published_card_regions_hides_drafts_and_pii(app_dsn: str, org: dict[str, str]) -> None:
    with as_role(app_dsn, None) as conn:
        cur = conn.execute("SELECT * FROM published_card_regions()")
        columns = [d.name for d in cur.description or []]
        rows = cur.fetchall()
    assert columns == [
        "card_id",
        "region_code",
        "region_sido",
        "region_sigungu",
        "trade",
        "area_type",
    ]
    assert not {"name", "owner_name", "supplier_names_json"} & set(columns)
    assert [r[0] for r in rows] == ["card-pub"]
    assert all("가게" not in map(str, r) and "사장" not in map(str, r) for r in rows)
