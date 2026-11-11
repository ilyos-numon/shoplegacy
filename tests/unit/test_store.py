from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest
from psycopg import errors

from shoplegacy.crypto import ENC_PREFIX, FernetCipher
from shoplegacy.interview import AnswerEntry
from shoplegacy.store import (
    ANON,
    PRINCIPAL,
    AlreadyDecidedError,
    AlreadyRevokedError,
    BillingKeyNotFoundError,
    BillingKeyRow,
    CardRow,
    CardVersionRow,
    ConsentRow,
    FeedbackCounts,
    InvoiceNotPayableError,
    InvoiceNotRefundableError,
    InvoiceRow,
    LastOwnerError,
    LlmUsageRow,
    LlmUsageSummary,
    MemberRow,
    MembershipRow,
    OrgRequiredError,
    OwnerLinkRow,
    OwnerReviewRow,
    ParentNotFoundError,
    Principal,
    SessionRow,
    ShopRow,
    Store,
    SttJobRow,
)

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)


@contextmanager
def acting(p: Principal) -> Iterator[None]:
    token = PRINCIPAL.set(p)
    try:
        yield
    finally:
        PRINCIPAL.reset(token)


def _user(user_id: str, email: str, org_id: str | None = None) -> Principal:
    return Principal(
        user_id=user_id,
        email=email,
        org_id=org_id,
        role="owner" if org_id else None,
        claims={},
    )


@pytest.fixture
def raw_store(app_dsn: str) -> Iterator[Store]:
    instance = Store(app_dsn)
    yield instance
    instance.close()


@pytest.fixture
def owner(raw_store: Store, pg_dsn: str, make_user: Callable[[str, str], str]) -> Principal:
    user_id = make_user(pg_dsn, "a@example.test")
    with acting(_user(user_id, "a@example.test")):
        org = raw_store.create_org("org A", user_id)
    return _user(user_id, "a@example.test", org.org_id)


@pytest.fixture
def other(raw_store: Store, pg_dsn: str, make_user: Callable[[str, str], str]) -> Principal:
    user_id = make_user(pg_dsn, "b@example.test")
    with acting(_user(user_id, "b@example.test")):
        org = raw_store.create_org("org B", user_id)
    return _user(user_id, "b@example.test", org.org_id)


@pytest.fixture
def store(raw_store: Store, owner: Principal) -> Iterator[Store]:
    with acting(owner):
        yield raw_store


@pytest.fixture
def org_id(owner: Principal) -> str:
    assert owner.org_id is not None
    return owner.org_id


def _shop(shop_id: str = "s1", org_id: str = "") -> ShopRow:
    return ShopRow(
        shop_id=shop_id,
        name="테스트 식당",
        owner_name="김사장",
        trade="tree_min",
        area_type="station",
        years=20,
        closed_year=2025,
        supplier_names=["가상 정육", "가상 채소"],
        created_at=NOW,
        org_id=org_id,
    )


def _session(session_id: str, number: int, ended: bool = False, org_id: str = "") -> SessionRow:
    return SessionRow(
        session_id=session_id,
        shop_id="s1",
        trade="tree_min",
        number=number,
        started_at=NOW,
        deadline=NOW + timedelta(seconds=1200),
        ended_at=NOW if ended else None,
        node_index=0,
        current_json=None,
        org_id=org_id,
    )


def _answer(session_id: str, text: str) -> AnswerEntry:
    return AnswerEntry(
        session_id=session_id,
        question_id="loc-01",
        axis="location",
        question="입지 질문",
        answer=text,
        created_at=NOW,
        session_number=1,
    )


def _consent(consent_id: str, kind: str, user_id: str, shop_id: str = "s1") -> ConsentRow:
    return ConsentRow(
        consent_id=consent_id,
        org_id="",
        shop_id=shop_id,
        kind=kind,
        method="verbal",
        note=None,
        granted_by=user_id,
        granted_at=NOW,
        revoked_at=None,
        revoke_reason=None,
    )


def test_shop_roundtrip(store: Store, org_id: str) -> None:
    store.insert_shop(_shop())
    got = store.get_shop("s1")
    assert got == _shop(org_id=org_id)
    assert store.get_shop("missing") is None
    assert [s.shop_id for s in store.list_shops()] == ["s1"]


def test_sessions_ordered_and_updated(store: Store, org_id: str) -> None:
    store.insert_shop(_shop())
    store.insert_session(_session("b", 2))
    store.insert_session(_session("a", 1))
    assert [r.number for r in store.list_sessions("s1")] == [1, 2]
    row = _session("a", 1, org_id=org_id)
    row.ended_at = NOW
    row.node_index = 3
    row.current_json = '{"x": 1}'
    store.update_session(row)
    assert store.get_session("a") == row
    assert store.get_session("zzz") is None


def test_shop_answers_ended_only(store: Store) -> None:
    store.insert_shop(_shop())
    store.insert_session(_session("done", 1, ended=True))
    store.insert_session(_session("open", 2))
    store.add_answer(_answer("done", "끝난 답"))
    store.add_answer(_answer("open", "열린 답"))
    assert [a.answer for a in store.answers("open")] == ["열린 답"]
    assert [a.answer for a in store.shop_answers("s1", ended_only=True)] == ["끝난 답"]
    assert len(store.shop_answers("s1", ended_only=False)) == 2


def test_card_upsert(store: Store, org_id: str) -> None:
    store.insert_shop(_shop())
    card = CardRow("c1", "s1", "draft", "{}", "[]", 1, NOW, org_id=org_id)
    store.upsert_card(card)
    card.status = "published"
    card.sessions = 2
    store.upsert_card(card)
    assert store.get_card("c1") == card
    assert store.card_for_shop("s1") == card
    assert [c.card_id for c in store.list_cards("published")] == ["c1"]
    assert store.list_cards("draft") == []


def test_add_answer_returns_id(store: Store) -> None:
    store.insert_shop(_shop())
    store.insert_session(_session("a", 1))
    first = store.add_answer(_answer("a", "하나"))
    second = store.add_answer(_answer("a", "둘"))
    assert second > first
    assert [a.answer_id for a in store.answers("a")] == [first, second]
    assert [a.answer_id for a in store.shop_answers("s1", ended_only=False)] == [first, second]


def test_card_extra_fields_roundtrip(store: Store, org_id: str) -> None:
    store.insert_shop(_shop())
    card = CardRow("c1", "s1", "on_hold", "{}", "[]", 1, NOW, org_id, '{"a": 1}', 3, "보류")
    store.upsert_card(card)
    assert store.get_card("c1") == card


def test_create_org_and_memberships(
    raw_store: Store, owner: Principal, pg_dsn: str, make_user: Callable[[str, str], str]
) -> None:
    reviewer = make_user(pg_dsn, "r@example.test")
    assert owner.org_id is not None
    with acting(owner):
        assert raw_store.my_orgs(owner.user_id) == [MembershipRow(owner.org_id, "org A", "owner")]
        assert raw_store.user_id_by_email("R@example.test") == reviewer
        assert raw_store.user_id_by_email("nobody@example.test") is None
        raw_store.upsert_member(owner.org_id, reviewer, "interviewer")
        raw_store.upsert_member(owner.org_id, reviewer, "reviewer")
        members = raw_store.list_members(owner.org_id)
    assert MemberRow(owner.user_id, "a@example.test", "owner") in members
    assert MemberRow(reviewer, "r@example.test", "reviewer") in members
    assert len(members) == 2
    with acting(_user(reviewer, "r@example.test")):
        assert raw_store.my_orgs(reviewer) == [MembershipRow(owner.org_id, "org A", "reviewer")]


def test_create_org_requires_matching_creator(
    raw_store: Store, owner: Principal, pg_dsn: str, make_user: Callable[[str, str], str]
) -> None:
    stranger = make_user(pg_dsn, "s@example.test")
    with acting(owner), pytest.raises(errors.InsufficientPrivilege):
        raw_store.create_org("spoof", stranger)


def test_card_versions(store: Store, owner: Principal, org_id: str) -> None:
    store.insert_shop(_shop())
    store.upsert_card(CardRow("c1", "s1", "draft", "{}", "[]", 1, NOW))
    for version in (2, 1):
        store.add_card_version(
            CardVersionRow("c1", version, "", "draft", "{}", "{}", owner.user_id, NOW)
        )
    versions = store.list_card_versions("c1")
    assert [v.version for v in versions] == [1, 2]
    assert versions[0] == CardVersionRow("c1", 1, org_id, "draft", "{}", "{}", owner.user_id, NOW)
    assert store.list_card_versions("other") == []


def test_consents_and_active_kinds(store: Store, owner: Principal, org_id: str) -> None:
    store.insert_shop(_shop())
    store.insert_consent(_consent("k1", "recording", owner.user_id))
    store.insert_consent(_consent("k2", "publication", owner.user_id))
    assert store.active_consent_kinds("s1") == {"recording", "publication"}
    got = store.get_consent("k2")
    assert got == replace(_consent("k2", "publication", owner.user_id), org_id=org_id)
    assert got is not None
    got.revoked_at = NOW + timedelta(days=1)
    got.revoke_reason = "사장님 요청"
    store.update_consent(got)
    assert store.get_consent("k2") == got
    assert store.active_consent_kinds("s1") == {"recording"}
    assert [c.consent_id for c in store.list_consents("s1")] == ["k1", "k2"]
    assert store.get_consent("missing") is None


def test_add_audit_visible_to_owner(store: Store, owner: Principal, org_id: str) -> None:
    store.add_audit("card.publish", "cards", "c1", {"from": "draft", "to": "published"})
    with store._tx() as conn:
        rows = conn.execute(
            "SELECT org_id::text, actor_id::text, action, row_id, new_json ->> 'to'"
            " FROM audit_log WHERE table_name = 'cards'"
        ).fetchall()
    assert rows == [(org_id, owner.user_id, "card.publish", "c1", "published")]


def test_other_org_sees_nothing(store: Store, other: Principal) -> None:
    store.insert_shop(_shop())
    store.insert_session(_session("a", 1))
    store.upsert_card(CardRow("c1", "s1", "draft", "{}", "[]", 1, NOW))
    with acting(other):
        assert store.get_shop("s1") is None
        assert store.list_shops() == []
        assert store.get_session("a") is None
        assert store.get_card("c1") is None
        assert store.list_consents("s1") == []


def test_anon_reads_only_published_and_cannot_write(store: Store) -> None:
    store.insert_shop(_shop())
    store.insert_shop(_shop("s2"))
    store.upsert_card(CardRow("c1", "s1", "published", "{}", "[]", 1, NOW))
    store.upsert_card(CardRow("c2", "s2", "draft", "{}", "[]", 1, NOW))
    with acting(ANON):
        assert [c.card_id for c in store.list_cards("published")] == ["c1"]
        assert store.get_card("c2") is None
        # anon has no grant on shops at all (only published cards are public).
        with pytest.raises(errors.InsufficientPrivilege):
            store.get_shop("s1")
        with pytest.raises(OrgRequiredError):
            store.insert_shop(_shop("s3"))


def test_write_without_org_is_rejected(raw_store: Store, owner: Principal) -> None:
    with acting(replace(owner, org_id=None)), pytest.raises(OrgRequiredError):
        raw_store.insert_shop(_shop())


@pytest.mark.parametrize("kind", ["session", "answer", "card", "card_version", "consent"])
def test_cross_org_parent_is_rejected(
    store: Store, owner: Principal, other: Principal, kind: str
) -> None:
    store.insert_shop(_shop())
    store.insert_session(_session("a", 1))
    store.upsert_card(CardRow("c1", "s1", "draft", "{}", "[]", 1, NOW))
    with acting(other), pytest.raises(ParentNotFoundError):
        if kind == "session":
            store.insert_session(_session("x", 9))
        elif kind == "answer":
            store.add_answer(_answer("a", "남의 답"))
        elif kind == "card":
            store.upsert_card(CardRow("c9", "s1", "draft", "{}", "[]", 1, NOW))
        elif kind == "card_version":
            store.add_card_version(
                CardVersionRow("c1", 1, "", "draft", "{}", "{}", other.user_id, NOW)
            )
        else:
            store.insert_consent(_consent("k9", "recording", other.user_id))
    # Nothing leaked into org A.
    assert len(store.list_sessions("s1")) == 1
    assert store.answers("a") == []
    assert store.list_card_versions("c1") == []
    assert store.list_consents("s1") == []


def test_missing_parent_is_rejected(store: Store) -> None:
    with pytest.raises(ParentNotFoundError):
        store.insert_session(_session("a", 1))
    with pytest.raises(ParentNotFoundError):
        store.add_answer(_answer("nope", "답"))


# --- encryption, lifecycle, feedback, owner reviews, usage, dashboard ---


@pytest.fixture
def enc_store(app_dsn: str, owner: Principal, pii_cipher: FernetCipher) -> Iterator[Store]:
    instance = Store(app_dsn, cipher=pii_cipher)
    try:
        with acting(owner):
            yield instance
    finally:
        instance.close()


def _region_shop(shop_id: str, code: str, sido: str, sigungu: str) -> ShopRow:
    return replace(_shop(shop_id), region_code=code, region_sido=sido, region_sigungu=sigungu)


def _review(
    review_id: str, card_id: str = "c1", version: int = 1, token: str = "t1"
) -> OwnerReviewRow:
    return OwnerReviewRow(review_id, "", "", card_id, version, token, "approve", "좋아요", NOW)


def test_encrypted_columns_are_ciphertext_at_rest(
    enc_store: Store, admin_conn: psycopg.Connection
) -> None:
    enc_store.insert_shop(_shop())
    session = _session("a", 1)
    session.current_json = '{"q": "비밀"}'
    enc_store.insert_session(session)
    enc_store.add_answer(_answer("a", "김사장 이야기"))
    shop_raw = admin_conn.execute(
        "SELECT name, owner_name, supplier_names_json FROM shops WHERE shop_id = 's1'"
    ).fetchone()
    assert shop_raw is not None
    assert all(str(v).startswith(ENC_PREFIX) for v in shop_raw)
    answer_raw = admin_conn.execute("SELECT answer FROM answers").fetchone()
    session_raw = admin_conn.execute("SELECT current_json FROM sessions").fetchone()
    assert answer_raw is not None and answer_raw[0].startswith(ENC_PREFIX)
    assert session_raw is not None and session_raw[0].startswith(ENC_PREFIX)
    got = enc_store.get_shop("s1")
    assert got is not None
    assert (got.name, got.owner_name, got.supplier_names) == (
        "테스트 식당",
        "김사장",
        ["가상 정육", "가상 채소"],
    )
    assert [a.answer for a in enc_store.answers("a")] == ["김사장 이야기"]
    got_session = enc_store.get_session("a")
    assert got_session is not None and got_session.current_json == '{"q": "비밀"}'


def test_plaintext_legacy_rows_still_read(store: Store, enc_store: Store) -> None:
    store.insert_shop(_shop())  # written by a store without keys
    store.insert_session(_session("a", 1))
    store.add_answer(_answer("a", "평문 답"))
    got = enc_store.get_shop("s1")
    assert got is not None and got.name == "테스트 식당"
    assert [a.answer for a in enc_store.answers("a")] == ["평문 답"]


def test_insert_shop_fills_retention_and_region(store: Store) -> None:
    store.update_org(_org_of(store), retention_days=365)
    store.insert_shop(_region_shop("s1", "1111000000", "서울특별시", "종로구"))
    got = store.get_shop("s1")
    assert got is not None
    assert got.retention_until == NOW + timedelta(days=365)
    assert (got.region_code, got.region_sido, got.region_sigungu) == (
        "1111000000",
        "서울특별시",
        "종로구",
    )
    assert store.count_shops() == 1


def _org_of(store: Store) -> str:
    org_id = PRINCIPAL.get().org_id
    assert org_id is not None
    return org_id


def test_card_provider_and_draft_roundtrip(store: Store, org_id: str) -> None:
    store.insert_shop(_shop())
    card = CardRow(
        "c1", "s1", "draft", "{}", "[]", 1, NOW, org_id, "{}", 1, None, "qwen", '{"a": 1}'
    )
    store.upsert_card(card)
    assert store.get_card("c1") == card
    assert store.card_for_shop("s1") == card


def test_list_cards_region_prefix_and_query(store: Store) -> None:
    store.insert_shop(_region_shop("s1", "1111000000", "서울특별시", "종로구"))
    store.insert_shop(_region_shop("s2", "2611000000", "부산광역시", "중구"))
    store.insert_shop(_shop("s3"))
    for i, shop_id in enumerate(("s1", "s2", "s3"), start=1):
        store.upsert_card(
            CardRow(f"c{i}", shop_id, "published", "{}", "[]", 1, NOW + timedelta(minutes=i))
        )
    assert [c.card_id for c in store.list_cards("published")] == ["c3", "c2", "c1"]
    assert [c.card_id for c in store.list_cards("published", region_code="11")] == ["c1"]
    # The code is a prefix as given, never cut down to its si/do digits.
    assert store.list_cards("published", region_code="1100000000") == []
    assert [c.card_id for c in store.list_cards("published", q="종로")] == ["c1"]
    assert [c.card_id for c in store.list_cards("published", q="부산")] == ["c2"]
    assert store.list_cards("published", region_code="11", q="부산") == []
    assert store.list_cards("published", q="%") == []


def test_public_card_region_published_only(store: Store) -> None:
    store.insert_shop(_region_shop("s1", "1111000000", "서울특별시", "종로구"))
    store.insert_shop(_shop("s2"))
    store.insert_shop(_region_shop("s3", "2611000000", "부산광역시", "중구"))
    store.upsert_card(CardRow("c1", "s1", "published", "{}", "[]", 1, NOW))
    store.upsert_card(CardRow("c2", "s2", "published", "{}", "[]", 1, NOW))
    store.upsert_card(CardRow("c3", "s3", "draft", "{}", "[]", 1, NOW))
    with acting(ANON):
        assert store.public_card_region("c1") == ("1111000000", "서울특별시", "종로구")
        assert store.public_card_region("c2") == (None, None, None)
        assert store.public_card_region("c3") is None
        assert store.public_card_region("missing") is None


def test_delete_shop_counts_rows_and_audits(
    store: Store, owner: Principal, admin_conn: psycopg.Connection
) -> None:
    store.insert_shop(_shop())
    store.insert_session(_session("a", 1))
    store.add_answer(_answer("a", "하나"))
    store.add_answer(_answer("a", "둘"))
    store.upsert_card(CardRow("c1", "s1", "draft", "{}", "[]", 1, NOW))
    store.add_card_version(CardVersionRow("c1", 1, "", "draft", "{}", "{}", owner.user_id, NOW))
    store.insert_consent(_consent("k1", "recording", owner.user_id))
    counts = store.delete_shop("s1")
    assert counts == {
        "sessions": 1,
        "answers": 2,
        "cards": 1,
        "card_versions": 1,
        "consents": 1,
        "owner_reviews": 0,
        "shops": 1,
    }
    assert store.get_shop("s1") is None
    assert admin_conn.execute("SELECT count(*) FROM answers").fetchone() == (0,)
    audit = admin_conn.execute(
        "SELECT actor_id::text, new_json ->> 'answers' FROM audit_log WHERE action = 'shop.delete'"
    ).fetchall()
    assert audit == [(owner.user_id, "2")]
    with pytest.raises(ParentNotFoundError):
        store.delete_shop("s1")


def test_export_shop_is_decrypted_and_complete(enc_store: Store, owner: Principal) -> None:
    enc_store.insert_shop(_shop())
    enc_store.insert_session(_session("a", 1))
    enc_store.add_answer(_answer("a", "비법 이야기"))
    enc_store.upsert_card(CardRow("c1", "s1", "published", "{}", "[]", 1, NOW, version=1))
    enc_store.insert_consent(_consent("k1", "storage", owner.user_id))
    enc_store.add_owner_review(_review("r1"))
    export = enc_store.export_shop("s1")
    assert export.shop.owner_name == "김사장"
    assert [s.session_id for s in export.sessions] == ["a"]
    assert [a.answer for a in export.answers] == ["비법 이야기"]
    assert [c.card_id for c in export.cards] == ["c1"]
    assert [c.consent_id for c in export.consents] == ["k1"]
    assert [r.review_id for r in export.owner_reviews] == ["r1"]
    shop_audit = [a for a in export.audit if a["table_name"] == "shops"]
    assert shop_audit and shop_audit[0]["new_json"]["owner_name"] == "김사장"
    assert {a["action"] for a in export.audit} >= {"INSERT", "owner_review.decide"}
    with pytest.raises(LookupError):
        enc_store.export_shop("missing")


def test_purge_expired_removes_only_past_retention(store: Store) -> None:
    store.insert_shop(replace(_shop("old"), retention_until=NOW - timedelta(days=1)))
    store.insert_shop(replace(_shop("new"), retention_until=NOW + timedelta(days=1)))
    with acting(ANON):
        assert store.purge_expired(NOW) == ["old"]
    assert [s.shop_id for s in store.list_shops()] == ["new"]
    assert store.purge_expired(NOW) == []


def test_feedback_once_per_fingerprint_and_counts(store: Store) -> None:
    store.insert_shop(_shop())
    store.insert_shop(_shop("s2"))
    store.upsert_card(CardRow("c1", "s1", "published", "{}", "[]", 1, NOW))
    store.upsert_card(CardRow("c2", "s2", "draft", "{}", "[]", 1, NOW))
    with acting(ANON):
        assert store.add_feedback("c1", "up", "fp1") is True
        assert store.add_feedback("c1", "down", "fp1") is False
        assert store.add_feedback("c1", "down", "fp2") is True
        assert store.feedback_counts("c1") == FeedbackCounts(1, 1)
        with pytest.raises(ParentNotFoundError):
            store.add_feedback("c2", "up", "fp1")
        assert store.feedback_counts("c2") == FeedbackCounts(0, 0)


def test_owner_card_view_runs_as_anon(store: Store, org_id: str) -> None:
    store.insert_shop(_shop())
    store.upsert_card(CardRow("c1", "s1", "draft", '{"a": "본문"}', "[]", 1, NOW, version=2))
    view = store.owner_card_view("c1")  # draft card, still visible through the function
    assert view is not None
    assert view["org_id"] == org_id
    assert view["version"] == 2
    assert view["sections"] == {"a": "본문"}
    assert view["decision"] is None
    assert "테스트 식당" not in str(view) and "김사장" not in str(view)
    assert store.owner_card_view("missing") is None


def test_owner_review_repeat_is_already_decided(store: Store, org_id: str) -> None:
    store.insert_shop(_shop())
    store.upsert_card(CardRow("c1", "s1", "draft", "{}", "[]", 1, NOW, version=1))
    with acting(ANON):
        store.add_owner_review(_review("r1"))
        with pytest.raises(AlreadyDecidedError):
            store.add_owner_review(_review("r2"))
        with pytest.raises(ParentNotFoundError):
            store.add_owner_review(_review("r3", card_id="missing"))
    latest = store.latest_owner_review("c1")
    assert latest is not None
    assert (latest.review_id, latest.org_id, latest.shop_id, latest.decision) == (
        "r1",
        org_id,
        "s1",
        "approve",
    )
    view = store.owner_card_view("c1")
    assert view is not None and view["decision"] == "approve"
    assert store.latest_owner_review("other") is None


def test_llm_usage_month_sums(store: Store, org_id: str, other: Principal) -> None:
    oct_1 = datetime(2026, 10, 1, tzinfo=UTC)
    for at, tin, tout, fb in (
        (oct_1, 10, 20, False),
        (oct_1 + timedelta(days=30), 1, 2, True),
        (datetime(2026, 11, 1, tzinfo=UTC), 100, 100, False),
    ):
        store.add_llm_usage(LlmUsageRow(org_id, at, "draft", "qwen", tin, tout, fb))
    assert store.llm_usage_month(org_id, date(2026, 10, 15)) == LlmUsageSummary(2, 11, 22, 1)
    assert store.llm_usage_month(org_id, date(2026, 9, 1)) == LlmUsageSummary(0, 0, 0, 0)
    with acting(other):
        assert store.llm_usage_month(org_id, date(2026, 10, 1)) == LlmUsageSummary(0, 0, 0, 0)


def test_dashboard_stats_period_boundaries(store: Store, owner: Principal, org_id: str) -> None:
    first = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    last = datetime(2026, 10, 31, 23, 59, tzinfo=UTC)
    after = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    for shop_id, at in (("s1", first), ("s2", last), ("s3", after)):
        store.insert_shop(replace(_shop(shop_id), created_at=at))
    s1 = replace(_session("a", 1), ended_at=last)
    s2 = replace(_session("b", 2), ended_at=after)
    store.insert_session(s1)
    store.insert_session(s2)
    store.upsert_card(CardRow("c1", "s1", "published", "{}", "[]", 1, first))
    store.upsert_card(CardRow("c2", "s2", "draft", "{}", "[]", 1, last))
    store.upsert_card(CardRow("c3", "s3", "published", "{}", "[]", 1, after))
    k1 = replace(_consent("k1", "recording", owner.user_id), granted_at=first)
    k2 = replace(
        _consent("k2", "recording", owner.user_id, "s2"),
        granted_at=first,
        revoked_at=last,
        revoke_reason="요청",
    )
    store.insert_consent(k1)
    store.insert_consent(k2)
    stats = store.dashboard_stats(org_id, date(2026, 10, 1), date(2026, 10, 31))
    assert (stats.period_from, stats.period_to) == (date(2026, 10, 1), date(2026, 10, 31))
    assert stats.shops == 2
    assert stats.interviews == 1
    assert stats.published_cards == 1
    assert stats.cards_by_status == {"published": 1, "draft": 1}
    assert stats.consents["recording"] == {"granted": 2, "revoked": 1}
    assert stats.consents["publication"] == {"granted": 0, "revoked": 0}
    nov = store.dashboard_stats(org_id, date(2026, 11, 1), date(2026, 11, 1))
    assert (nov.shops, nov.interviews, nov.published_cards) == (1, 1, 1)


def test_report_rows_fields_and_limit(store: Store, owner: Principal, org_id: str) -> None:
    store.insert_shop(_region_shop("s1", "1111000000", "서울특별시", "종로구"))
    store.insert_shop(replace(_shop("s2"), created_at=NOW + timedelta(hours=1)))
    store.insert_session(replace(_session("a", 1), ended_at=NOW))
    store.upsert_card(CardRow("c1", "s1", "published", "{}", "[]", 1, NOW, version=1))
    store.insert_consent(_consent("k1", "recording", owner.user_id))
    revoked = replace(
        _consent("k2", "publication", owner.user_id), revoked_at=NOW, revoke_reason="요청"
    )
    store.insert_consent(revoked)
    with acting(ANON):
        store.add_owner_review(_review("r1"))
    rows = store.report_rows(org_id, date(2026, 10, 30), date(2026, 10, 30), 10)
    assert [r.shop_id for r in rows] == ["s1", "s2"]
    first = rows[0]
    assert first.region == "서울특별시 종로구"
    assert (first.sessions, first.last_interview_at) == (1, NOW)
    assert (first.card_status, first.published_at) == ("published", NOW)
    assert (first.consent_recording, first.consent_publication) == ("granted", "revoked")
    assert first.consent_storage is None
    assert first.owner_decision == "approve"
    assert rows[1].region is None and rows[1].card_status is None
    assert [
        r.shop_id for r in store.report_rows(org_id, date(2026, 10, 30), date(2026, 10, 30), 1)
    ] == ["s1"]
    assert store.report_rows(org_id, date(2026, 10, 31), date(2026, 11, 30), 10) == []


def test_update_org_plan_and_retention(
    raw_store: Store, owner: Principal, pg_dsn: str, make_user: Callable[[str, str], str]
) -> None:
    assert owner.org_id is not None
    with acting(owner):
        org = raw_store.get_org(owner.org_id)
        assert (org.plan, org.retention_days) == ("free", 1095)
        updated = raw_store.update_org(owner.org_id, plan="pilot", retention_days=365)
        assert (updated.plan, updated.retention_days) == ("pilot", 365)
        assert raw_store.get_org(owner.org_id) == updated
        assert raw_store.update_org(owner.org_id, plan="org").retention_days == 365
        with pytest.raises(errors.CheckViolation):
            raw_store.update_org(owner.org_id, retention_days=29)
        admin = make_user(pg_dsn, "adm@example.test")
        raw_store.upsert_member(owner.org_id, admin, "admin")
    admin_p = Principal(admin, "adm@example.test", owner.org_id, "admin", {})
    with acting(admin_p), pytest.raises(LookupError):
        raw_store.update_org(owner.org_id, plan="free")
    with acting(owner), pytest.raises(LookupError):
        raw_store.get_org("00000000-0000-0000-0000-000000000000")


def test_last_owner_cannot_be_removed_or_demoted(
    raw_store: Store, owner: Principal, pg_dsn: str, make_user: Callable[[str, str], str]
) -> None:
    assert owner.org_id is not None
    second = make_user(pg_dsn, "second@example.test")
    with acting(owner):
        with pytest.raises(LastOwnerError):
            raw_store.remove_member(owner.org_id, owner.user_id)
        with pytest.raises(LastOwnerError):
            raw_store.upsert_member(owner.org_id, owner.user_id, "admin")
        raw_store.upsert_member(owner.org_id, second, "owner")
        assert raw_store.count_members(owner.org_id) == 2
        raw_store.upsert_member(owner.org_id, second, "reviewer")
        raw_store.remove_member(owner.org_id, second)
        assert raw_store.count_members(owner.org_id) == 1
        with pytest.raises(LookupError):
            raw_store.remove_member(owner.org_id, second)


def test_anon_list_cards_region_and_query(store: Store) -> None:
    store.insert_shop(_region_shop("s1", "1111000000", "서울특별시", "종로구"))
    store.insert_shop(_region_shop("s2", "2611000000", "부산광역시", "중구"))
    store.insert_shop(_region_shop("s3", "1168000000", "서울특별시", "강남구"))
    store.upsert_card(CardRow("c1", "s1", "published", "{}", "[]", 1, NOW))
    store.upsert_card(CardRow("c2", "s2", "published", "{}", "[]", 1, NOW))
    store.upsert_card(CardRow("c3", "s3", "draft", "{}", "[]", 1, NOW))
    with acting(ANON):
        assert [c.card_id for c in store.list_cards("published", region_code="11")] == ["c1"]
        assert [c.card_id for c in store.list_cards("published", q="부산")] == ["c2"]
        assert store.list_cards("published", q="강남") == []  # draft card


# --- region prefix, transcription jobs, invoices, owner links ---


def _region_cards(store: Store) -> None:
    store.insert_shop(_region_shop("s1", "1111000000", "서울특별시", "종로구"))
    store.insert_shop(_region_shop("s2", "1111012345", "서울특별시", "종로구"))
    store.insert_shop(_region_shop("s3", "1168000000", "서울특별시", "강남구"))
    store.insert_shop(_region_shop("s4", "2611000000", "부산광역시", "중구"))
    for i in range(1, 5):
        store.upsert_card(
            CardRow(f"c{i}", f"s{i}", "published", "{}", "[]", 1, NOW + timedelta(minutes=i))
        )


def test_list_cards_region_prefix_two_digits(store: Store) -> None:
    _region_cards(store)
    assert [c.card_id for c in store.list_cards("published", region_code="11")] == [
        "c3",
        "c2",
        "c1",
    ]
    with acting(ANON):
        assert [c.card_id for c in store.list_cards("published", region_code="26")] == ["c4"]
        assert store.list_cards("published", region_code="36") == []


def test_list_cards_region_prefix_five_digits(store: Store) -> None:
    _region_cards(store)
    assert [c.card_id for c in store.list_cards("published", region_code="11110")] == [
        "c2",
        "c1",
    ]
    with acting(ANON):
        assert [c.card_id for c in store.list_cards("published", region_code="11680")] == ["c3"]
        assert [
            c.card_id for c in store.list_cards("published", region_code="11110", q="종로")
        ] == ["c2", "c1"]
        assert store.list_cards("published", region_code="11110", q="강남") == []
        assert store.list_cards("published", region_code="11740") == []


def _job(
    job_id: str, user_id: str, session_id: str = "a", minutes: int = 0, text: str | None = None
) -> SttJobRow:
    return SttJobRow(
        job_id=job_id,
        org_id="",
        session_id=session_id,
        created_by=user_id,
        status="queued",
        audio_key=f"org/{job_id}.wav",
        content_type="audio/wav",
        bytes=1234,
        text=text,
        duration_s=None,
        provider=None,
        error=None,
        created_at=NOW + timedelta(minutes=minutes),
        started_at=None,
        finished_at=None,
    )


def test_stt_job_roundtrip_and_list_order(store: Store, owner: Principal, org_id: str) -> None:
    store.insert_shop(_shop())
    store.insert_session(_session("a", 1))
    store.insert_session(_session("b", 2))
    store.insert_stt_job(_job("j1", owner.user_id))
    store.insert_stt_job(_job("j2", owner.user_id, minutes=1))
    store.insert_stt_job(_job("j3", owner.user_id, session_id="b"))
    got = store.get_stt_job("j1")
    assert got == replace(_job("j1", owner.user_id), org_id=org_id)
    assert [j.job_id for j in store.list_stt_jobs("a")] == ["j2", "j1"]
    assert [j.job_id for j in store.list_stt_jobs("b")] == ["j3"]
    assert store.get_stt_job("missing") is None


def test_stt_job_claim_finish_once(store: Store, owner: Principal) -> None:
    store.insert_shop(_shop())
    store.insert_session(_session("a", 1))
    store.insert_stt_job(_job("j1", owner.user_id))
    claimed = store.stt_job_claim("j1")
    assert claimed is not None and claimed.status == "running"
    assert claimed.started_at is not None
    assert store.stt_job_claim("j1") is None  # already taken
    assert store.stt_job_claim("missing") is None
    store.stt_job_finish("j1", "받아 적은 말", 3.5, "fake")
    done = store.get_stt_job("j1")
    assert done is not None
    assert (done.status, done.text, done.duration_s, done.provider) == (
        "done",
        "받아 적은 말",
        3.5,
        "fake",
    )
    assert done.finished_at is not None
    store.stt_job_fail("j1", "too late")  # a finished job stays done
    after = store.get_stt_job("j1")
    assert after is not None and (after.status, after.error) == ("done", None)


def test_stt_job_fail_truncates_error(store: Store, owner: Principal) -> None:
    store.insert_shop(_shop())
    store.insert_session(_session("a", 1))
    store.insert_stt_job(_job("j1", owner.user_id))
    store.stt_job_fail("j1", "x" * 1000)
    failed = store.get_stt_job("j1")
    assert failed is not None
    assert failed.status == "failed" and failed.error == "x" * 300
    assert failed.text is None and failed.finished_at is not None
    assert store.stt_job_claim("j1") is None


def test_stt_job_text_encrypted_at_rest(
    enc_store: Store, owner: Principal, admin_conn: psycopg.Connection
) -> None:
    enc_store.insert_shop(_shop())
    enc_store.insert_session(_session("a", 1))
    enc_store.insert_stt_job(_job("j1", owner.user_id))
    enc_store.stt_job_claim("j1")
    enc_store.stt_job_finish("j1", "김사장 이야기", None, "fake")
    raw = admin_conn.execute("SELECT text FROM stt_jobs WHERE job_id = 'j1'").fetchone()
    assert raw is not None and raw[0].startswith(ENC_PREFIX)
    assert "김사장" not in raw[0]
    got = enc_store.get_stt_job("j1")
    assert got is not None and got.text == "김사장 이야기"
    assert [j.text for j in enc_store.list_stt_jobs("a")] == ["김사장 이야기"]


def test_stt_job_other_org_cannot_insert_or_read(
    store: Store, owner: Principal, other: Principal
) -> None:
    store.insert_shop(_shop())
    store.insert_session(_session("a", 1))
    store.insert_stt_job(_job("j1", owner.user_id))
    with acting(other):
        assert store.get_stt_job("j1") is None
        assert store.list_stt_jobs("a") == []
        with pytest.raises(ParentNotFoundError):
            store.insert_stt_job(_job("j2", other.user_id))
    with acting(replace(owner, org_id=None)), pytest.raises(OrgRequiredError):
        store.insert_stt_job(_job("j3", owner.user_id))
    with pytest.raises(ParentNotFoundError):
        store.insert_stt_job(_job("j4", owner.user_id, session_id="missing"))
    assert [j.job_id for j in store.list_stt_jobs("a")] == ["j1"]


def _audit_actions(admin_conn: psycopg.Connection, org_id: str) -> list[str]:
    rows = admin_conn.execute(
        "SELECT action FROM audit_log WHERE org_id = %s ORDER BY id", (org_id,)
    ).fetchall()
    return [str(r[0]) for r in rows]


def _issue(store: Store, org_id: str, plan: str = "pilot", month: int = 11) -> InvoiceRow:
    return store.issue_invoice(
        org_id, plan, date(2026, month, 1), date(2026, month + 1, 1), 99000, "첫 달"
    )


def test_invoice_issue_and_list(store: Store, org_id: str, admin_conn: psycopg.Connection) -> None:
    first = _issue(store, org_id)
    second = _issue(store, org_id, plan="org", month=10)
    assert first.org_id == org_id and first.plan == "pilot"
    assert (first.status, first.method, first.paid_at) == ("issued", None, None)
    assert first.order_id == f"sl-{first.invoice_id}"
    assert (first.period_start, first.period_end) == (date(2026, 11, 1), date(2026, 12, 1))
    assert (first.amount_krw, first.memo) == (99000, "첫 달")
    assert store.get_invoice(first.invoice_id) == first
    assert {i.invoice_id for i in store.list_invoices(org_id)} == {
        first.invoice_id,
        second.invoice_id,
    }
    assert store.invoice_by_order(first.order_id) == first
    assert store.invoice_by_order("sl-missing") is None
    assert store.get_invoice("missing") is None
    with pytest.raises(errors.UniqueViolation):
        _issue(store, org_id)  # one per org, plan and period
    assert _audit_actions(admin_conn, org_id).count("invoice.issue") == 2


def test_invoice_mark_paid_moves_plan_once(
    store: Store, org_id: str, admin_conn: psycopg.Connection
) -> None:
    invoice = _issue(store, org_id)
    assert store.get_org(org_id).plan == "free"
    paid = store.mark_invoice_paid(invoice.invoice_id, "card", "fake", "fake_key", NOW)
    assert (paid.status, paid.method, paid.provider) == ("paid", "card", "fake")
    assert (paid.provider_payment_key, paid.paid_at) == ("fake_key", NOW)
    assert store.get_org(org_id).plan == "pilot"
    again = store.mark_invoice_paid(
        invoice.invoice_id, "contract", "x", None, NOW + timedelta(days=1)
    )
    assert again == paid  # webhook and confirm may both arrive
    actions = _audit_actions(admin_conn, org_id)
    assert actions.count("invoice.paid") == 1 and actions.count("org.plan") == 1
    with pytest.raises(LookupError):
        store.mark_invoice_paid("missing", "card", "fake", None, NOW)


def test_invoice_void_and_not_payable(
    store: Store, org_id: str, admin_conn: psycopg.Connection
) -> None:
    voided = _issue(store, org_id)
    store.void_invoice(voided.invoice_id)
    store.void_invoice(voided.invoice_id)  # twice does nothing
    got = store.get_invoice(voided.invoice_id)
    assert got is not None and got.status == "void"
    with pytest.raises(InvoiceNotPayableError):
        store.mark_invoice_paid(voided.invoice_id, "card", "fake", None, NOW)
    paid = _issue(store, org_id, month=10)
    store.mark_invoice_paid(paid.invoice_id, "contract", "manual", None, NOW)
    with pytest.raises(InvoiceNotPayableError):
        store.void_invoice(paid.invoice_id)
    with pytest.raises(LookupError):
        store.void_invoice("missing")
    assert store.get_org(org_id).plan == "pilot"
    assert _audit_actions(admin_conn, org_id).count("invoice.void") == 1


def test_invoice_other_org_cannot_see_or_write(store: Store, org_id: str, other: Principal) -> None:
    invoice = _issue(store, org_id)
    with acting(other):
        assert other.org_id is not None
        assert store.list_invoices(org_id) == []
        assert store.get_invoice(invoice.invoice_id) is None
        assert store.list_invoices(other.org_id) == []
        with store._tx() as conn, pytest.raises(errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE invoices SET status = 'paid' WHERE invoice_id = %s",
                (invoice.invoice_id,),
            )
    with store._tx() as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute(
            "SELECT public.invoice_issue('x', %s, 'org', %s, %s, 1, NULL)",
            (org_id, date(2026, 1, 1), date(2026, 2, 1)),
        )
    assert [i.invoice_id for i in store.list_invoices(org_id)] == [invoice.invoice_id]


def test_invoice_refund_round_trip(
    store: Store, org_id: str, admin_conn: psycopg.Connection
) -> None:
    invoice = _issue(store, org_id)
    assert (invoice.refunded_at, invoice.refund_reason) == (None, None)
    with pytest.raises(InvoiceNotRefundableError):
        store.refund_invoice(invoice.invoice_id, "중복 결제", NOW)
    store.mark_invoice_paid(invoice.invoice_id, "card", "fake", "fake_key", NOW)
    at = NOW + timedelta(days=2)
    refunded = store.refund_invoice(invoice.invoice_id, "중복 결제", at)
    assert (refunded.status, refunded.refunded_at, refunded.refund_reason) == (
        "refunded",
        at,
        "중복 결제",
    )
    assert refunded.provider_payment_key == "fake_key"
    assert store.get_invoice(invoice.invoice_id) == refunded
    assert store.list_invoices(org_id) == [refunded]
    with pytest.raises(InvoiceNotRefundableError):
        store.refund_invoice(invoice.invoice_id, "again", at)
    with pytest.raises(LookupError):
        store.refund_invoice("missing", "x", at)
    with pytest.raises(InvoiceNotPayableError):
        store.mark_invoice_paid(invoice.invoice_id, "card", "fake", "fake_key", at)
    assert _audit_actions(admin_conn, org_id).count("invoice.refund") == 1


def test_refunded_invoice_cannot_be_voided(
    store: Store, org_id: str, admin_conn: psycopg.Connection
) -> None:
    invoice = _issue(store, org_id)
    store.mark_invoice_paid(invoice.invoice_id, "card", "fake", "fake_key", NOW)
    store.refund_invoice(invoice.invoice_id, "중복 결제", NOW)
    with pytest.raises(InvoiceNotPayableError):
        store.void_invoice(invoice.invoice_id)
    got = store.get_invoice(invoice.invoice_id)
    assert got is not None and got.status == "refunded"
    assert _audit_actions(admin_conn, org_id).count("invoice.void") == 0


def test_invoice_mark_tax_invoice(
    store: Store, org_id: str, admin_conn: psycopg.Connection
) -> None:
    invoice = _issue(store, org_id)
    with pytest.raises(InvoiceNotPayableError):
        store.mark_tax_invoice(invoice.invoice_id, "20261104-0001", NOW)
    store.mark_invoice_paid(invoice.invoice_id, "contract", "manual", None, NOW)
    marked = store.mark_tax_invoice(invoice.invoice_id, "20261104-0001", NOW)
    assert (marked.status, marked.tax_invoice_ref, marked.tax_invoice_issued_at) == (
        "paid",
        "20261104-0001",
        NOW,
    )
    assert store.get_invoice(invoice.invoice_id) == marked
    with pytest.raises(LookupError):
        store.mark_tax_invoice("missing", "x", NOW)
    assert _audit_actions(admin_conn, org_id).count("invoice.tax_invoice") == 1


def _billing_key(
    org_id: str, user_id: str | None, next_on: date = date(2026, 12, 1)
) -> BillingKeyRow:
    return BillingKeyRow(
        org_id=org_id,
        provider="fake",
        billing_key=f"fake_bk_sl-{org_id}",
        customer_key=f"sl-{org_id}",
        card_label="테스트카드 ****1234",
        plan="pilot",
        charge_day=1,
        next_charge_on=next_on,
        status="active",
        failures=0,
        created_by=user_id,
        created_at=NOW,
        stopped_at=None,
    )


def test_billing_key_set_get_is_encrypted_at_rest(
    enc_store: Store, owner: Principal, admin_conn: psycopg.Connection
) -> None:
    org_id = owner.org_id
    assert org_id is not None
    assert enc_store.get_billing_key(org_id) is None
    want = _billing_key(org_id, owner.user_id)
    saved = enc_store.set_billing_key(want)
    assert saved.billing_key == want.billing_key
    assert (saved.status, saved.failures, saved.stopped_at) == ("active", 0, None)
    assert (saved.card_label, saved.plan, saved.next_charge_on) == (
        want.card_label,
        "pilot",
        date(2026, 12, 1),
    )
    assert saved.created_by == owner.user_id
    assert enc_store.get_billing_key(org_id) == saved
    raw = admin_conn.execute(
        "SELECT billing_key FROM billing_keys WHERE org_id = %s", (org_id,)
    ).fetchone()
    assert raw is not None
    assert raw[0] != want.billing_key and raw[0].startswith(ENC_PREFIX)
    audit = admin_conn.execute(
        "SELECT new_json::text FROM audit_log WHERE org_id = %s AND action = 'billing_key.set'",
        (org_id,),
    ).fetchall()
    assert len(audit) == 1 and want.billing_key not in audit[0][0]
    assert raw[0] not in audit[0][0]


def test_billing_key_stop_and_not_found(store: Store, owner: Principal) -> None:
    org_id = owner.org_id
    assert org_id is not None
    with pytest.raises(BillingKeyNotFoundError):
        store.stop_billing_key(org_id)
    store.set_billing_key(_billing_key(org_id, owner.user_id))
    store.stop_billing_key(org_id)
    got = store.get_billing_key(org_id)
    assert got is not None and got.status == "stopped" and got.stopped_at is not None
    again = store.set_billing_key(_billing_key(org_id, owner.user_id, date(2027, 1, 1)))
    assert (again.status, again.stopped_at, again.next_charge_on) == (
        "active",
        None,
        date(2027, 1, 1),
    )
    missing = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(BillingKeyNotFoundError):
        store.fail_billing_key(missing)
    with pytest.raises(BillingKeyNotFoundError):
        store.advance_billing_key(missing, date(2027, 1, 1))


def test_billing_keys_due_and_advance(store: Store, owner: Principal, other: Principal) -> None:
    assert owner.org_id is not None and other.org_id is not None
    store.set_billing_key(_billing_key(owner.org_id, owner.user_id, date(2026, 12, 1)))
    store.set_billing_key(_billing_key(other.org_id, other.user_id, date(2026, 12, 15)))
    assert store.billing_keys_due(date(2026, 11, 30)) == []
    due = store.billing_keys_due(date(2026, 12, 1))
    assert [k.org_id for k in due] == [owner.org_id]
    assert due[0].billing_key == f"fake_bk_sl-{owner.org_id}"
    assert {k.org_id for k in store.billing_keys_due(date(2026, 12, 31))} == {
        owner.org_id,
        other.org_id,
    }
    store.fail_billing_key(owner.org_id)
    store.advance_billing_key(owner.org_id, date(2027, 1, 1))
    got = store.get_billing_key(owner.org_id)
    assert got is not None and (got.next_charge_on, got.failures) == (date(2027, 1, 1), 0)
    assert [k.org_id for k in store.billing_keys_due(date(2026, 12, 31))] == [other.org_id]
    store.stop_billing_key(other.org_id)
    assert store.billing_keys_due(date(2026, 12, 31)) == []


def test_billing_key_fail_three_times_stops(
    store: Store, owner: Principal, admin_conn: psycopg.Connection
) -> None:
    org_id = owner.org_id
    assert org_id is not None
    store.set_billing_key(_billing_key(org_id, owner.user_id))
    assert [store.fail_billing_key(org_id) for _ in range(3)] == [1, 2, 3]
    got = store.get_billing_key(org_id)
    assert got is not None and (got.status, got.failures) == ("stopped", 3)
    assert store.billing_keys_due(date(2027, 1, 1)) == []
    assert _audit_actions(admin_conn, org_id).count("billing_key.stop") == 1


def test_billing_keys_hidden_from_members(store: Store, owner: Principal) -> None:
    assert owner.org_id is not None
    store.set_billing_key(_billing_key(owner.org_id, owner.user_id))
    with store._tx() as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute("SELECT billing_key FROM billing_keys")


def _link(token_id: str, card_id: str = "c1", minutes: int = 0, hours: int = 72) -> OwnerLinkRow:
    issued = NOW + timedelta(minutes=minutes)
    return OwnerLinkRow(token_id, "", card_id, issued, issued + timedelta(hours=hours), None, None)


def test_owner_link_insert_list_and_state(store: Store, org_id: str) -> None:
    store.insert_shop(_shop())
    store.upsert_card(CardRow("c1", "s1", "draft", "{}", "[]", 1, NOW))
    now = datetime.now(UTC)
    store.insert_owner_link(_link("t1"))  # expired long ago
    store.insert_owner_link(OwnerLinkRow("t2", "", "c1", now, now + timedelta(hours=1), None, None))
    links = store.list_owner_links("c1")
    assert [link.token_id for link in links] == ["t2", "t1"]
    assert links[1] == replace(_link("t1"), org_id=org_id)
    assert store.owner_link_state("t1") == "expired"
    assert store.owner_link_state("t2") == "active"
    assert store.owner_link_state("never-issued") == "unknown"
    with acting(ANON):
        assert store.owner_link_state("t2") == "active"
        # anon has no grant on owner_links; it only learns a link's state.
        with pytest.raises(errors.InsufficientPrivilege):
            store.list_owner_links("c1")


def test_owner_link_revoke_once(store: Store, owner: Principal) -> None:
    store.insert_shop(_shop())
    store.insert_shop(_shop("s2"))
    store.upsert_card(CardRow("c1", "s1", "draft", "{}", "[]", 1, NOW))
    store.upsert_card(CardRow("c2", "s2", "draft", "{}", "[]", 1, NOW))
    now = datetime.now(UTC)
    store.insert_owner_link(OwnerLinkRow("t1", "", "c1", now, now + timedelta(hours=1), None, None))
    with pytest.raises(LookupError):
        store.revoke_owner_link("c2", "t1")  # another card's link
    with pytest.raises(LookupError):
        store.revoke_owner_link("c1", "missing")
    store.revoke_owner_link("c1", "t1")
    revoked = store.list_owner_links("c1")[0]
    assert revoked.revoked_at is not None and revoked.revoked_by == owner.user_id
    assert store.owner_link_state("t1") == "revoked"
    with acting(ANON):
        assert store.owner_link_state("t1") == "revoked"
    with pytest.raises(AlreadyRevokedError):
        store.revoke_owner_link("c1", "t1")


def test_owner_link_other_org_cannot_insert_or_revoke(
    store: Store, owner: Principal, other: Principal
) -> None:
    store.insert_shop(_shop())
    store.upsert_card(CardRow("c1", "s1", "draft", "{}", "[]", 1, NOW))
    store.insert_owner_link(_link("t1"))
    with acting(other):
        assert store.list_owner_links("c1") == []
        with pytest.raises(LookupError):
            store.revoke_owner_link("c1", "t1")
        with pytest.raises(ParentNotFoundError):
            store.insert_owner_link(_link("t2"))
    with acting(replace(owner, org_id=None)), pytest.raises(OrgRequiredError):
        store.insert_owner_link(_link("t3"))
    only = store.list_owner_links("c1")
    assert [link.token_id for link in only] == ["t1"] and only[0].revoked_at is None


def test_public_card_tags_v2_five_tuple(store: Store) -> None:
    store.insert_shop(_region_shop("s1", "1111000000", "서울특별시", "종로구"))
    store.insert_shop(_shop("s2"))
    store.insert_shop(_shop("s3"))
    store.upsert_card(CardRow("c1", "s1", "published", "{}", "[]", 1, NOW))
    store.upsert_card(CardRow("c2", "s2", "published", "{}", "[]", 1, NOW))
    store.upsert_card(CardRow("c3", "s3", "draft", "{}", "[]", 1, NOW))
    with acting(ANON):
        assert store.public_card_tags("s1") == ("tree_min", "station", 20, 2025, "종로구")
        assert store.public_card_tags("s2") == ("tree_min", "station", 20, 2025, None)
        assert store.public_card_tags("s3") is None
        assert store.public_card_tags("missing") is None


def test_stt_job_and_invoice_shared_fixtures(
    app_dsn: str,
    org_fixture: tuple[str, str, str, str],
    stt_session: str,
    issued_invoice: InvoiceRow,
    admin_conn: psycopg.Connection,
) -> None:
    org_id = org_fixture[0]
    assert (issued_invoice.org_id, issued_invoice.status) == (org_id, "issued")
    assert (issued_invoice.plan, issued_invoice.amount_krw) == ("pilot", 99000)
    owner_id = admin_conn.execute(
        "SELECT created_by::text FROM orgs WHERE org_id = %s", (org_id,)
    ).fetchone()
    assert owner_id is not None
    member = Principal(owner_id[0], None, org_id, "owner", {})
    instance = Store(app_dsn)
    try:
        with acting(member):
            assert instance.get_session(stt_session) is not None
            instance.insert_stt_job(_job("j1", owner_id[0], session_id=stt_session))
            assert [j.job_id for j in instance.list_stt_jobs(stt_session)] == ["j1"]
            assert instance.list_invoices(org_id) == [issued_invoice]
    finally:
        instance.close()
