"""Shared Postgres fixtures (opt-in, never autouse).

A template DB is built once per session on the local Supabase Postgres
(auth stub + migrations), and each test that asks for ``pg_dsn`` gets a fresh
clone of it.
"""

import os
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import jwt
import psycopg
import pytest
from psycopg import Cursor
from pytest_postgresql import factories
from pytest_postgresql.janitor import DatabaseJanitor

from shoplegacy.crypto import FernetCipher
from shoplegacy.store import PRINCIPAL, InvoiceRow, Principal, Store

ROOT = Path(__file__).resolve().parents[1]
_ADMIN_DSN = os.environ.get(
    "SHOPLEGACY_TEST_PG_DSN", "postgresql://postgres:postgres@localhost:56322/postgres"
)
_DB_PREFIX = os.environ.get("SHOPLEGACY_TEST_DB_PREFIX", "sl_test")
_parts = urlsplit(_ADMIN_DSN)

APP_USER = "shoplegacy_app"
APP_PASSWORD = "shoplegacy_app"
TEST_JWT_SECRET = "test-secret"
# Fixed Fernet key (urlsafe base64 of 32 bytes) for tests that need real encryption.
TEST_PII_KEY = "c2hvcGxlZ2FjeS10ZXN0LXBpaS1rZXktMzJieXRlcyE="


def _terminate_client_backends(cur: Cursor, dbname: str) -> None:
    # The stock janitor terminates *every* backend on the DB, including autovacuum
    # workers. Those run as the bootstrap superuser, and the local Supabase
    # ``postgres`` role is not a superuser, so the call fails intermittently with
    # InsufficientPrivilege. CREATE/DROP DATABASE already cancel autovacuum
    # workers themselves, so only client backends need terminating here.
    cur.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = %s AND backend_type = 'client backend'",
        (dbname,),
    )


DatabaseJanitor._terminate_connection = staticmethod(_terminate_client_backends)  # type: ignore[method-assign]

sl_postgresql_noproc = factories.postgresql_noproc(
    host=_parts.hostname or "localhost",
    port=_parts.port or 5432,
    user=_parts.username or "postgres",
    password=_parts.password or "",
    dbname=_DB_PREFIX,
    load=[
        ROOT / "tests" / "pg" / "auth_stub.sql",
        *sorted((ROOT / "supabase" / "migrations").glob("*.sql")),
    ],
)
sl_postgresql = factories.postgresql("sl_postgresql_noproc")


def _dsn(user: str, password: str, host: str, port: int | str, dbname: str) -> str:
    return urlunsplit(("postgresql", f"{user}:{password}@{host}:{port}", f"/{dbname}", "", ""))


@pytest.fixture
def pg_dsn(sl_postgresql: psycopg.Connection) -> str:
    """Admin DSN of a fresh per-test clone of the migrated template DB."""
    info = sl_postgresql.info
    return _dsn(
        _parts.username or "postgres",
        _parts.password or "",
        info.host,
        info.port,
        info.dbname,
    )


@pytest.fixture
def admin_conn(pg_dsn: str) -> Iterator[psycopg.Connection]:
    """Autocommit connection as the admin user, bypassing RLS (to see stored bytes)."""
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture
def pii_cipher() -> FernetCipher:
    """A real Fernet cipher keyed with ``TEST_PII_KEY``."""
    return FernetCipher([TEST_PII_KEY])


@pytest.fixture
def app_dsn(pg_dsn: str) -> str:
    """Same DB as ``pg_dsn`` but logged in as the RLS-bound app role."""
    p = urlsplit(pg_dsn)
    return _dsn(APP_USER, APP_PASSWORD, p.hostname or "localhost", p.port or 5432, p.path[1:])


def _make_user(pg_dsn: str, email: str) -> str:
    user_id = str(uuid.uuid4())
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s)", (user_id, email))
    return user_id


@pytest.fixture
def make_user() -> Callable[[str, str], str]:
    """``make_user(pg_dsn, email) -> user_id``: inserts a row into auth.users."""
    return _make_user


@pytest.fixture
def org_principal(
    app_dsn: str, pg_dsn: str, make_user: Callable[[str, str], str]
) -> Iterator[Principal]:
    """A user who owns a fresh org, set as ``PRINCIPAL`` for the test's duration."""
    email = "owner@example.test"
    user_id = make_user(pg_dsn, email)
    store = Store(app_dsn)
    try:
        token = PRINCIPAL.set(
            Principal(user_id=user_id, email=email, org_id=None, role=None, claims={})
        )
        try:
            org = store.create_org("test org", user_id)
        finally:
            PRINCIPAL.reset(token)
    finally:
        store.close()
    principal = Principal(user_id=user_id, email=email, org_id=org.org_id, role="owner", claims={})
    token = PRINCIPAL.set(principal)
    try:
        yield principal
    finally:
        PRINCIPAL.reset(token)


def mint_token(user_id: str, email: str, secret: str) -> str:
    """An HS256 access token shaped like a Supabase one, valid for an hour."""
    claims = {
        "sub": user_id,
        "email": email,
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


@pytest.fixture
def jwt_secret() -> str:
    """HS256 secret the app under test is configured with (overridden by E2E)."""
    return TEST_JWT_SECRET


OrgTokens = tuple[str, str, str, str]


@pytest.fixture
def org_fixture(pg_dsn: str, jwt_secret: str) -> OrgTokens:
    """``(org_id, owner_token, reviewer_token, interviewer_token)`` for a fresh org."""
    tokens: dict[str, str] = {}
    org_id = str(uuid.uuid4())
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        for role in ("owner", "reviewer", "interviewer"):
            email = f"{role}-{org_id[:8]}@example.test"
            user_id = _make_user(pg_dsn, email)
            if role == "owner":
                conn.execute(
                    "INSERT INTO public.orgs (org_id, name, created_by) VALUES (%s, %s, %s)",
                    (org_id, "test org", user_id),
                )
            conn.execute(
                "INSERT INTO public.org_members (org_id, user_id, role) VALUES (%s, %s, %s)",
                (org_id, user_id, role),
            )
            tokens[role] = mint_token(user_id, email, jwt_secret)
    return org_id, tokens["owner"], tokens["reviewer"], tokens["interviewer"]


@pytest.fixture
def reviewer_headers(org_fixture: OrgTokens) -> dict[str, str]:
    """Bearer + X-Org-Id headers for a reviewer (can create shops and publish cards)."""
    org_id, _, reviewer_token, _ = org_fixture
    return {"Authorization": f"Bearer {reviewer_token}", "X-Org-Id": org_id}


@pytest.fixture
def stt_session(pg_dsn: str, org_fixture: OrgTokens) -> str:
    """Session id of a fresh shop and interview session in ``org_fixture``'s org."""
    org_id = org_fixture[0]
    shop_id = f"shop-{uuid.uuid4().hex[:8]}"
    session_id = f"sess-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO public.shops (shop_id, org_id, created_by, name, owner_name, trade, "
            "area_type, years, closed_year, supplier_names_json, created_at) "
            "SELECT %s, o.org_id, o.created_by, '테스트 식당', '김사장', 'tree_min', 'station', "
            "20, 2025, '[]', %s FROM public.orgs o WHERE o.org_id = %s",
            (shop_id, now, org_id),
        )
        conn.execute(
            "INSERT INTO public.sessions (session_id, org_id, shop_id, trade, number, "
            "started_at, deadline, node_index) VALUES (%s, %s, %s, 'tree_min', 1, %s, %s, 0)",
            (session_id, org_id, shop_id, now, now + timedelta(seconds=1200)),
        )
    return session_id


@pytest.fixture
def issued_invoice(app_dsn: str, org_fixture: OrgTokens) -> InvoiceRow:
    """A ``pilot`` invoice for November 2026 (99,000 KRW) issued to ``org_fixture``'s org."""
    store = Store(app_dsn)
    try:
        return store.issue_invoice(
            org_fixture[0], "pilot", date(2026, 11, 1), date(2026, 12, 1), 99000, None
        )
    finally:
        store.close()
