"""Postgres-backed store for orgs, shops, sessions, answers, cards and consents.

Every call runs in one transaction as the RLS-bound app role, switched to
``anon`` or ``authenticated`` from the current ``PRINCIPAL``, so Postgres RLS
keeps each org's rows apart even if a route forgets a check.
"""

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

import psycopg
from psycopg import errors
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from shoplegacy.crypto import Cipher, NullCipher
from shoplegacy.dashboard import DashboardStats
from shoplegacy.interview import AnswerEntry

AreaType = Literal["residential", "station", "office", "other"]
Role = Literal["owner", "admin", "reviewer", "interviewer"]
Plan = Literal["free", "pilot", "org"]


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str | None
    org_id: str | None
    role: Role | None
    claims: dict[str, Any]

    @property
    def is_anon(self) -> bool:
        return self.user_id == ""


ANON = Principal(user_id="", email=None, org_id=None, role=None, claims={})
PRINCIPAL: ContextVar[Principal] = ContextVar("shoplegacy_principal", default=ANON)


class OrgRequiredError(PermissionError):
    """A write needs an org but the current principal has none."""


class ParentNotFoundError(LookupError):
    """The parent row is missing, not visible, or belongs to another org."""


class LastOwnerError(RuntimeError):
    """The change would leave the org without an owner."""


class AlreadyDecidedError(RuntimeError):
    """The shop owner already decided on this card version with this link."""


class InvoiceNotPayableError(RuntimeError):
    """The invoice is void (cannot be paid) or already paid (cannot be voided)."""


class AlreadyRevokedError(RuntimeError):
    """The shop owner review link was already revoked."""


class InvoiceNotRefundableError(RuntimeError):
    """Only a paid invoice can be refunded."""


class BillingKeyNotFoundError(LookupError):
    """The org has no stored billing key."""


_SHOP_COLUMNS = (
    "shop_id, name, owner_name, trade, area_type, years, closed_year, "
    "supplier_names_json, created_at, org_id::text, region_code, region_sido, region_sigungu, "
    "retention_until"
)
_SESSION_COLUMNS = (
    "session_id, shop_id, trade, number, started_at, deadline, ended_at, node_index, "
    "current_json, org_id::text"
)
_ANSWER_COLUMNS = "session_id, question_id, axis, question, answer, created_at, session_number"
_CARD_COLUMNS = (
    "card_id, shop_id, status, sections_json, warnings_json, sessions, updated_at, "
    "org_id::text, evidence_json, version, note, llm_provider, draft_sections_json"
)
_CARD_VERSION_COLUMNS = (
    "card_id, version, org_id::text, status, sections_json, evidence_json, "
    "created_by::text, created_at"
)
_CONSENT_COLUMNS = (
    "consent_id, org_id::text, shop_id, kind, method, note, granted_by::text, granted_at, "
    "revoked_at, revoke_reason"
)
_OWNER_REVIEW_COLUMNS = (
    "review_id, org_id::text, shop_id, card_id, card_version, token_id, decision, comment, "
    "created_at"
)
_STT_JOB_COLUMNS = (
    "job_id, org_id::text, session_id, created_by::text, status, audio_key, content_type, "
    "bytes, text, duration_s, provider, error, created_at, started_at, finished_at"
)
_INVOICE_COLUMNS = (
    "invoice_id, org_id::text, plan, period_start, period_end, amount_krw, status, method, "
    "provider, provider_payment_key, order_id, memo, issued_at, paid_at, refunded_at, "
    "refund_reason, tax_invoice_ref, tax_invoice_issued_at"
)
_BILLING_KEY_COLUMNS = (
    "org_id::text, provider, billing_key, customer_key, card_label, plan, charge_day, "
    "next_charge_on, status, failures, created_by::text, created_at, stopped_at"
)
_OWNER_LINK_COLUMNS = (
    "token_id, org_id::text, card_id, issued_at, expires_at, revoked_at, revoked_by::text"
)


@dataclass
class ShopRow:
    shop_id: str
    name: str
    owner_name: str
    trade: str
    area_type: AreaType
    years: int
    closed_year: int
    supplier_names: list[str]
    created_at: datetime
    org_id: str = ""
    region_code: str | None = None
    region_sido: str | None = None
    region_sigungu: str | None = None
    # Filled by insert_shop from the org's retention period, so it is left out of
    # equality: a row read back equals the row that was written.
    retention_until: datetime | None = field(default=None, compare=False)


@dataclass
class SessionRow:
    session_id: str
    shop_id: str
    trade: str
    number: int
    started_at: datetime
    deadline: datetime
    ended_at: datetime | None
    node_index: int
    current_json: str | None
    org_id: str = ""


@dataclass
class CardRow:
    card_id: str
    shop_id: str
    status: str
    sections_json: str
    warnings_json: str
    sessions: int
    updated_at: datetime
    org_id: str = ""
    evidence_json: str = "{}"
    version: int = 0
    note: str | None = None
    llm_provider: str = "fake"
    draft_sections_json: str = "{}"


@dataclass
class OrgRow:
    org_id: str
    name: str
    created_by: str
    created_at: datetime
    plan: str = "free"
    retention_days: int = 1095


@dataclass
class MembershipRow:
    org_id: str
    name: str
    role: Role


@dataclass
class MemberRow:
    user_id: str
    email: str | None
    role: Role


@dataclass
class CardVersionRow:
    card_id: str
    version: int
    org_id: str
    status: str
    sections_json: str
    evidence_json: str
    created_by: str | None
    created_at: datetime


@dataclass
class ConsentRow:
    consent_id: str
    org_id: str
    shop_id: str
    kind: str
    method: str
    note: str | None
    granted_by: str
    granted_at: datetime
    revoked_at: datetime | None
    revoke_reason: str | None


@dataclass(frozen=True)
class FeedbackCounts:
    up: int
    down: int


@dataclass
class OwnerReviewRow:
    review_id: str
    org_id: str
    shop_id: str
    card_id: str
    card_version: int
    token_id: str
    decision: str
    comment: str | None
    created_at: datetime


@dataclass
class LlmUsageRow:
    org_id: str
    occurred_at: datetime
    kind: str
    provider: str
    tokens_in: int
    tokens_out: int
    fallback: bool


@dataclass(frozen=True)
class LlmUsageSummary:
    calls: int
    tokens_in: int
    tokens_out: int
    fallbacks: int


@dataclass
class ReportRow:
    shop_id: str
    trade: str
    region: str | None
    sessions: int
    last_interview_at: datetime | None
    card_status: str | None
    published_at: datetime | None
    consent_recording: str | None
    consent_storage: str | None
    consent_ai_processing: str | None
    consent_publication: str | None
    owner_decision: str | None
    created_at: datetime


@dataclass
class ShopExport:
    shop: ShopRow
    sessions: list[SessionRow]
    answers: list[AnswerEntry]
    cards: list[CardRow]
    card_versions: list[CardVersionRow]
    consents: list[ConsentRow]
    owner_reviews: list[OwnerReviewRow]
    audit: list[dict[str, Any]]


@dataclass(frozen=True)
class SttJobRow:
    job_id: str
    org_id: str
    session_id: str
    created_by: str
    status: str
    audio_key: str
    content_type: str
    bytes: int
    text: str | None
    duration_s: float | None
    provider: str | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class InvoiceRow:
    invoice_id: str
    org_id: str
    plan: str
    period_start: date
    period_end: date
    amount_krw: int
    status: str
    method: str | None
    provider: str | None
    provider_payment_key: str | None
    order_id: str
    memo: str | None
    issued_at: datetime
    paid_at: datetime | None
    refunded_at: datetime | None = None
    refund_reason: str | None = None
    tax_invoice_ref: str | None = None
    tax_invoice_issued_at: datetime | None = None


@dataclass(frozen=True)
class BillingKeyRow:
    org_id: str
    provider: str
    billing_key: str
    customer_key: str
    card_label: str | None
    plan: str
    charge_day: int
    next_charge_on: date
    status: str
    failures: int
    created_by: str | None
    created_at: datetime
    stopped_at: datetime | None


@dataclass(frozen=True)
class OwnerLinkRow:
    token_id: str
    org_id: str
    card_id: str
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    revoked_by: str | None


Row = tuple[Any, ...]

_CONSENT_KINDS = ("recording", "storage", "ai_processing", "publication")
_COUNT_ANSWERS_OF_SHOP = (
    "SELECT count(*) FROM answers a JOIN sessions s ON a.session_id = s.session_id "
    "WHERE s.shop_id = %s"
)
_COUNT_VERSIONS_OF_SHOP = (
    "SELECT count(*) FROM card_versions v JOIN cards c ON v.card_id = c.card_id "
    "WHERE c.shop_id = %s"
)
_SHOP_SECRET_KEYS = ("name", "owner_name", "supplier_names_json")


def _area(value: object) -> AreaType:
    text = str(value)
    if text == "residential":
        return "residential"
    if text == "station":
        return "station"
    if text == "office":
        return "office"
    return "other"


def _role(value: object) -> Role:
    text = str(value)
    if text == "owner":
        return "owner"
    if text == "admin":
        return "admin"
    if text == "reviewer":
        return "reviewer"
    if text == "interviewer":
        return "interviewer"
    raise ValueError(f"unknown role: {text}")


def _card_from(r: Row) -> CardRow:
    return CardRow(
        card_id=r[0],
        shop_id=r[1],
        status=r[2],
        sections_json=r[3],
        warnings_json=r[4],
        sessions=r[5],
        updated_at=r[6],
        org_id=r[7],
        evidence_json=r[8],
        version=r[9],
        note=r[10],
        llm_provider=r[11],
        draft_sections_json=r[12],
    )


def _card_version_from(r: Row) -> CardVersionRow:
    return CardVersionRow(*r)


def _consent_from(r: Row) -> ConsentRow:
    return ConsentRow(*r)


def _owner_review_from(r: Row) -> OwnerReviewRow:
    return OwnerReviewRow(*r)


def _invoice_from(r: Row) -> InvoiceRow:
    return InvoiceRow(*r)


def _owner_link_from(r: Row) -> OwnerLinkRow:
    return OwnerLinkRow(*r)


def _raised(exc: errors.RaiseException, message: str) -> bool:
    return exc.diag.message_primary == message


def _is_last_owner(exc: errors.CheckViolation) -> bool:
    return exc.diag.message_primary == "last_owner"


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _day_bounds(since: date, until: date) -> tuple[datetime, datetime]:
    """[since 00:00 UTC, the day after until 00:00 UTC): both dates inclusive."""
    lo = datetime.combine(since, time.min, tzinfo=UTC)
    hi = datetime.combine(until + timedelta(days=1), time.min, tzinfo=UTC)
    return lo, hi


def _apply_principal(conn: psycopg.Connection[Row], p: Principal) -> None:
    if p.is_anon:
        conn.execute("SET LOCAL ROLE anon")
        return
    conn.execute("SET LOCAL ROLE authenticated")
    claims = json.dumps({"sub": p.user_id, "role": "authenticated", "email": p.email})
    conn.execute("SELECT set_config('request.jwt.claims', %s, true)", (claims,))


class Store:
    def __init__(
        self,
        dsn: str,
        *,
        cipher: Cipher | None = None,
        min_size: int = 1,
        max_size: int = 5,
    ) -> None:
        self._cipher: Cipher = cipher if cipher is not None else NullCipher()
        self._pool: ConnectionPool[psycopg.Connection[Row]] = ConnectionPool(
            dsn, min_size=min_size, max_size=max_size, open=True
        )

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def _tx(self, principal: Principal | None = None) -> Iterator[psycopg.Connection[Row]]:
        with self._pool.connection() as conn, conn.transaction():
            _apply_principal(conn, principal if principal is not None else PRINCIPAL.get())
            yield conn

    @contextmanager
    def _app_tx(self) -> Iterator[psycopg.Connection[Row]]:
        """Runs as the app login role itself, without switching to anon/authenticated.

        Only for functions granted to that role alone (retention purge).
        """
        with self._pool.connection() as conn, conn.transaction():
            yield conn

    def ping(self) -> None:
        """Round-trip ``select 1``; psycopg errors propagate to the caller."""
        with self._app_tx() as conn:
            conn.execute("select 1")

    # field encryption
    def _enc(self, value: str) -> str:
        return self._cipher.encrypt(value)

    def _enc_opt(self, value: str | None) -> str | None:
        return None if value is None else self._cipher.encrypt(value)

    def _dec(self, value: str) -> str:
        return self._cipher.decrypt(value)

    def _dec_opt(self, value: str | None) -> str | None:
        return None if value is None else self._cipher.decrypt(value)

    def _shop_from(self, r: Row) -> ShopRow:
        return ShopRow(
            shop_id=r[0],
            name=self._dec(r[1]),
            owner_name=self._dec(r[2]),
            trade=r[3],
            area_type=_area(r[4]),
            years=r[5],
            closed_year=r[6],
            supplier_names=[str(s) for s in json.loads(self._dec(r[7]))],
            created_at=r[8],
            org_id=r[9],
            region_code=r[10],
            region_sido=r[11],
            region_sigungu=r[12],
            retention_until=r[13],
        )

    def _session_from(self, r: Row) -> SessionRow:
        return SessionRow(
            session_id=r[0],
            shop_id=r[1],
            trade=r[2],
            number=r[3],
            started_at=r[4],
            deadline=r[5],
            ended_at=r[6],
            node_index=r[7],
            current_json=self._dec_opt(r[8]),
            org_id=r[9],
        )

    def _entry_from(self, r: Row) -> AnswerEntry:
        return AnswerEntry.model_validate(
            {
                "answer_id": r[0],
                "session_id": r[1],
                "question_id": r[2],
                "axis": r[3],
                "question": r[4],
                "answer": self._dec(r[5]),
                "created_at": r[6],
                "session_number": r[7],
            }
        )

    def _decrypt_audit_json(self, value: object) -> object:
        if not isinstance(value, dict):
            return value
        out = dict(value)
        for key in _SHOP_SECRET_KEYS:
            if isinstance(out.get(key), str):
                out[key] = self._dec(out[key])
        return out

    def _fetch(self, sql: str, params: tuple[object, ...]) -> list[Row]:
        with self._tx() as conn:
            return conn.execute(sql, params).fetchall()

    @staticmethod
    def _org_id() -> str:
        org_id = PRINCIPAL.get().org_id
        if not org_id:
            raise OrgRequiredError("an org is required for this write")
        return org_id

    @staticmethod
    def _check_parent(
        conn: psycopg.Connection[Row], table: str, key: str, value: str, org_id: str
    ) -> None:
        # table/key are fixed identifiers from this module, never user input.
        row = conn.execute(
            f"SELECT org_id::text FROM {table} WHERE {key} = %s", (value,)
        ).fetchone()
        if row is None or row[0] != org_id:
            raise ParentNotFoundError(f"{table} {value} not found in org {org_id}")

    # orgs and members
    def create_org(self, name: str, created_by: str) -> OrgRow:
        org = OrgRow(str(uuid.uuid4()), name, created_by, datetime.now(UTC))
        # plan and retention_days take their column defaults, which match OrgRow's.
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO orgs (org_id, name, created_by, created_at) VALUES (%s, %s, %s, %s)",
                (org.org_id, org.name, org.created_by, org.created_at),
            )
            conn.execute(
                "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'owner')",
                (org.org_id, created_by),
            )
        return org

    def my_orgs(self, user_id: str) -> list[MembershipRow]:
        rows = self._fetch(
            "SELECT o.org_id::text, o.name, m.role FROM org_members m "
            "JOIN orgs o ON o.org_id = m.org_id WHERE m.user_id = %s "
            "ORDER BY o.created_at ASC, o.org_id ASC",
            (user_id,),
        )
        return [MembershipRow(r[0], r[1], _role(r[2])) for r in rows]

    def list_members(self, org_id: str) -> list[MemberRow]:
        # auth.users is not readable by the app role; the SECURITY DEFINER helper
        # resolves emails, and only for orgs the caller belongs to.
        rows = self._fetch(
            "SELECT user_id::text, email, role FROM org_member_emails(%s)",
            (org_id,),
        )
        return [MemberRow(r[0], r[1], _role(r[2])) for r in rows]

    def upsert_member(self, org_id: str, user_id: str, role: Role) -> None:
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, %s) "
                    "ON CONFLICT (org_id, user_id) DO UPDATE SET role = excluded.role",
                    (org_id, user_id, role),
                )
        except errors.CheckViolation as exc:
            if _is_last_owner(exc):
                raise LastOwnerError("last_owner") from None
            raise

    def remove_member(self, org_id: str, user_id: str) -> None:
        """Delete a membership; LookupError if none was visible to delete."""
        try:
            with self._tx() as conn:
                cur = conn.execute(
                    "DELETE FROM org_members WHERE org_id = %s AND user_id = %s",
                    (org_id, user_id),
                )
                deleted = cur.rowcount
        except errors.CheckViolation as exc:
            if _is_last_owner(exc):
                raise LastOwnerError("last_owner") from None
            raise
        if deleted == 0:
            raise LookupError(f"member {user_id} not found in org {org_id}")

    def count_members(self, org_id: str) -> int:
        rows = self._fetch("SELECT count(*) FROM org_members WHERE org_id = %s", (org_id,))
        return int(rows[0][0])

    def get_org(self, org_id: str) -> OrgRow:
        rows = self._fetch(
            "SELECT org_id::text, name, created_by::text, created_at, plan, retention_days "
            "FROM orgs WHERE org_id = %s",
            (org_id,),
        )
        if not rows:
            raise LookupError(f"org {org_id} not found")
        return OrgRow(*rows[0])

    def update_org(
        self, org_id: str, *, plan: Plan | None = None, retention_days: int | None = None
    ) -> OrgRow:
        """Change plan and/or retention; LookupError if the caller cannot update the org."""
        if plan is None and retention_days is None:
            return self.get_org(org_id)
        with self._tx() as conn:
            row = conn.execute(
                "UPDATE orgs SET plan = coalesce(%s, plan), "
                "retention_days = coalesce(%s, retention_days) WHERE org_id = %s "
                "RETURNING org_id::text, name, created_by::text, created_at, plan, retention_days",
                (plan, retention_days, org_id),
            ).fetchone()
        if row is None:
            raise LookupError(f"org {org_id} not found or not updatable")
        return OrgRow(*row)

    def user_id_by_email(self, email: str) -> str | None:
        rows = self._fetch("SELECT user_id_by_email(%s)::text", (email,))
        return rows[0][0] if rows else None

    # shops
    def insert_shop(self, row: ShopRow) -> None:
        """Insert a shop; retention_until defaults to created_at + the org's retention days."""
        org_id = self._org_id()
        with self._tx() as conn:
            retention_until = row.retention_until
            if retention_until is None:
                found = conn.execute(
                    "SELECT retention_days FROM orgs WHERE org_id = %s", (org_id,)
                ).fetchone()
                days = int(found[0]) if found else OrgRow.retention_days
                retention_until = row.created_at + timedelta(days=days)
            conn.execute(
                "INSERT INTO shops (shop_id, name, owner_name, trade, area_type, years, "
                "closed_year, supplier_names_json, created_at, org_id, created_by, "
                "region_code, region_sido, region_sigungu, retention_until) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    row.shop_id,
                    self._enc(row.name),
                    self._enc(row.owner_name),
                    row.trade,
                    row.area_type,
                    row.years,
                    row.closed_year,
                    self._enc(json.dumps(row.supplier_names, ensure_ascii=False)),
                    row.created_at,
                    org_id,
                    PRINCIPAL.get().user_id,
                    row.region_code,
                    row.region_sido,
                    row.region_sigungu,
                    retention_until,
                ),
            )
        row.retention_until = retention_until

    def get_shop(self, shop_id: str) -> ShopRow | None:
        rows = self._fetch(f"SELECT {_SHOP_COLUMNS} FROM shops WHERE shop_id = %s", (shop_id,))
        return self._shop_from(rows[0]) if rows else None

    def count_shops(self) -> int:
        """Shops of the current principal's org (all visible shops if it has none)."""
        org_id = PRINCIPAL.get().org_id
        if org_id:
            rows = self._fetch("SELECT count(*) FROM shops WHERE org_id = %s", (org_id,))
        else:
            rows = self._fetch("SELECT count(*) FROM shops", ())
        return int(rows[0][0])

    def delete_shop(self, shop_id: str) -> dict[str, int]:
        """Delete a shop and everything under it; returns deleted row counts per table."""
        org_id = self._org_id()
        with self._tx() as conn:
            self._check_parent(conn, "shops", "shop_id", shop_id, org_id)
            counts: dict[str, int] = {}
            for table, sql in (
                ("sessions", "SELECT count(*) FROM sessions WHERE shop_id = %s"),
                ("answers", _COUNT_ANSWERS_OF_SHOP),
                ("cards", "SELECT count(*) FROM cards WHERE shop_id = %s"),
                ("card_versions", _COUNT_VERSIONS_OF_SHOP),
                ("consents", "SELECT count(*) FROM consents WHERE shop_id = %s"),
                ("owner_reviews", "SELECT count(*) FROM owner_reviews WHERE shop_id = %s"),
            ):
                found = conn.execute(sql, (shop_id,)).fetchone()
                counts[table] = int(found[0]) if found else 0
            cur = conn.execute("DELETE FROM shops WHERE shop_id = %s", (shop_id,))
            counts["shops"] = cur.rowcount
            conn.execute(
                "INSERT INTO audit_log (org_id, actor_id, action, table_name, row_id, new_json) "
                "VALUES (%s, %s, 'shop.delete', 'shops', %s, %s)",
                (org_id, PRINCIPAL.get().user_id, shop_id, Jsonb(counts)),
            )
        return counts

    def export_shop(self, shop_id: str) -> ShopExport:
        """Everything recorded about one shop, decrypted. LookupError if not visible."""
        with self._tx() as conn:
            found = conn.execute(
                f"SELECT {_SHOP_COLUMNS} FROM shops WHERE shop_id = %s", (shop_id,)
            ).fetchone()
            if found is None:
                raise LookupError(f"shop {shop_id} not found")
            shop = self._shop_from(found)
            sessions = [
                self._session_from(r)
                for r in conn.execute(
                    f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE shop_id = %s "
                    "ORDER BY number ASC",
                    (shop_id,),
                ).fetchall()
            ]
            cols = ", ".join(f"a.{c.strip()}" for c in _ANSWER_COLUMNS.split(","))
            answers = [
                self._entry_from(r)
                for r in conn.execute(
                    f"SELECT a.id, {cols} FROM answers a JOIN sessions s "
                    "ON a.session_id = s.session_id WHERE s.shop_id = %s ORDER BY a.id ASC",
                    (shop_id,),
                ).fetchall()
            ]
            cards = [
                _card_from(r)
                for r in conn.execute(
                    f"SELECT {_CARD_COLUMNS} FROM cards WHERE shop_id = %s", (shop_id,)
                ).fetchall()
            ]
            card_ids = [c.card_id for c in cards]
            versions = [
                _card_version_from(r)
                for r in conn.execute(
                    f"SELECT {_CARD_VERSION_COLUMNS} FROM card_versions "
                    "WHERE card_id = ANY(%s) ORDER BY card_id ASC, version ASC",
                    (card_ids,),
                ).fetchall()
            ]
            consents = [
                _consent_from(r)
                for r in conn.execute(
                    f"SELECT {_CONSENT_COLUMNS} FROM consents WHERE shop_id = %s "
                    "ORDER BY granted_at ASC, consent_id ASC",
                    (shop_id,),
                ).fetchall()
            ]
            reviews = [
                _owner_review_from(r)
                for r in conn.execute(
                    f"SELECT {_OWNER_REVIEW_COLUMNS} FROM owner_reviews WHERE shop_id = %s "
                    "ORDER BY created_at ASC, review_id ASC",
                    (shop_id,),
                ).fetchall()
            ]
            row_ids = [
                shop_id,
                *card_ids,
                *(c.consent_id for c in consents),
                *(r.review_id for r in reviews),
            ]
            audit_rows = conn.execute(
                "SELECT actor_id::text, action, table_name, row_id, old_json, new_json, at "
                "FROM audit_log WHERE org_id = %s::uuid AND row_id = ANY(%s) ORDER BY id ASC",
                (shop.org_id, row_ids),
            ).fetchall()
        audit = [
            {
                "actor_id": r[0],
                "action": r[1],
                "table_name": r[2],
                "row_id": r[3],
                "old_json": self._decrypt_audit_json(r[4]) if r[2] == "shops" else r[4],
                "new_json": self._decrypt_audit_json(r[5]) if r[2] == "shops" else r[5],
                "at": r[6],
            }
            for r in audit_rows
        ]
        return ShopExport(shop, sessions, answers, cards, versions, consents, reviews, audit)

    def purge_expired(self, now: datetime) -> list[str]:
        """Delete shops whose retention ended before ``now``; returns their ids."""
        with self._app_tx() as conn:
            rows = conn.execute("SELECT * FROM purge_expired_shops(%s)", (now,)).fetchall()
        return [str(r[0]) for r in rows]

    def public_card_tags(self, shop_id: str) -> tuple[str, AreaType, int, int, str | None] | None:
        """(trade, area_type, years, closed_year, region_sigungu) of a shop with a
        published card."""
        rows = self._fetch(
            "SELECT trade, area_type, years, closed_year, region_sigungu "
            "FROM public_card_tags_v2(%s)",
            (shop_id,),
        )
        if not rows:
            return None
        r = rows[0]
        return str(r[0]), _area(r[1]), int(r[2]), int(r[3]), r[4]

    def public_card_region(self, card_id: str) -> tuple[str | None, str | None, str | None] | None:
        """(region_code, sido, sigungu) of a published card; None if not published."""
        rows = self._fetch(
            "SELECT region_code, region_sido, region_sigungu "
            "FROM published_card_regions() WHERE card_id = %s",
            (card_id,),
        )
        if not rows:
            return None
        code, sido, sigungu = rows[0]
        return (
            str(code) if code is not None else None,
            str(sido) if sido is not None else None,
            str(sigungu) if sigungu is not None else None,
        )

    def list_shops(self) -> list[ShopRow]:
        rows = self._fetch(f"SELECT {_SHOP_COLUMNS} FROM shops ORDER BY created_at ASC", ())
        return [self._shop_from(r) for r in rows]

    # sessions
    def insert_session(self, row: SessionRow) -> None:
        org_id = self._org_id()
        with self._tx() as conn:
            self._check_parent(conn, "shops", "shop_id", row.shop_id, org_id)
            conn.execute(
                "INSERT INTO sessions (session_id, shop_id, trade, number, started_at, "
                "deadline, ended_at, node_index, current_json, org_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    row.session_id,
                    row.shop_id,
                    row.trade,
                    row.number,
                    row.started_at,
                    row.deadline,
                    row.ended_at,
                    row.node_index,
                    self._enc_opt(row.current_json),
                    org_id,
                ),
            )

    def update_session(self, row: SessionRow) -> None:
        org_id = self._org_id()
        with self._tx() as conn:
            self._check_parent(conn, "shops", "shop_id", row.shop_id, org_id)
            conn.execute(
                "UPDATE sessions SET shop_id = %s, trade = %s, number = %s, started_at = %s, "
                "deadline = %s, ended_at = %s, node_index = %s, current_json = %s "
                "WHERE session_id = %s",
                (
                    row.shop_id,
                    row.trade,
                    row.number,
                    row.started_at,
                    row.deadline,
                    row.ended_at,
                    row.node_index,
                    self._enc_opt(row.current_json),
                    row.session_id,
                ),
            )

    def get_session(self, session_id: str) -> SessionRow | None:
        rows = self._fetch(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE session_id = %s", (session_id,)
        )
        return self._session_from(rows[0]) if rows else None

    def list_sessions(self, shop_id: str) -> list[SessionRow]:
        rows = self._fetch(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE shop_id = %s ORDER BY number ASC",
            (shop_id,),
        )
        return [self._session_from(r) for r in rows]

    # answers
    def add_answer(self, entry: AnswerEntry) -> int:
        org_id = self._org_id()
        with self._tx() as conn:
            self._check_parent(conn, "sessions", "session_id", entry.session_id, org_id)
            row = conn.execute(
                "INSERT INTO answers (session_id, question_id, axis, question, answer, "
                "created_at, session_number, org_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    entry.session_id,
                    entry.question_id,
                    entry.axis,
                    entry.question,
                    self._enc(entry.answer),
                    entry.created_at,
                    entry.session_number,
                    org_id,
                ),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def answers(self, session_id: str) -> list[AnswerEntry]:
        rows = self._fetch(
            f"SELECT id, {_ANSWER_COLUMNS} FROM answers WHERE session_id = %s ORDER BY id ASC",
            (session_id,),
        )
        return [self._entry_from(r) for r in rows]

    def shop_answers(self, shop_id: str, ended_only: bool) -> list[AnswerEntry]:
        cols = ", ".join(f"a.{c.strip()}" for c in _ANSWER_COLUMNS.split(","))
        sql = (
            f"SELECT a.id, {cols} FROM answers a JOIN sessions s ON a.session_id = s.session_id "
            "WHERE s.shop_id = %s"
        )
        if ended_only:
            sql += " AND s.ended_at IS NOT NULL"
        sql += " ORDER BY a.id ASC"
        return [self._entry_from(r) for r in self._fetch(sql, (shop_id,))]

    # cards
    def upsert_card(self, row: CardRow) -> None:
        org_id = self._org_id()
        with self._tx() as conn:
            self._check_parent(conn, "shops", "shop_id", row.shop_id, org_id)
            conn.execute(
                "INSERT INTO cards (card_id, shop_id, status, sections_json, warnings_json, "
                "sessions, updated_at, org_id, evidence_json, version, note, llm_provider, "
                "draft_sections_json) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (card_id) DO UPDATE SET shop_id = excluded.shop_id, "
                "status = excluded.status, sections_json = excluded.sections_json, "
                "warnings_json = excluded.warnings_json, sessions = excluded.sessions, "
                "updated_at = excluded.updated_at, evidence_json = excluded.evidence_json, "
                "version = excluded.version, note = excluded.note, "
                "llm_provider = excluded.llm_provider, "
                "draft_sections_json = excluded.draft_sections_json",
                (
                    row.card_id,
                    row.shop_id,
                    row.status,
                    row.sections_json,
                    row.warnings_json,
                    row.sessions,
                    row.updated_at,
                    org_id,
                    row.evidence_json,
                    row.version,
                    row.note,
                    row.llm_provider,
                    row.draft_sections_json,
                ),
            )

    def get_card(self, card_id: str) -> CardRow | None:
        rows = self._fetch(f"SELECT {_CARD_COLUMNS} FROM cards WHERE card_id = %s", (card_id,))
        return _card_from(rows[0]) if rows else None

    def card_for_shop(self, shop_id: str) -> CardRow | None:
        rows = self._fetch(f"SELECT {_CARD_COLUMNS} FROM cards WHERE shop_id = %s", (shop_id,))
        return _card_from(rows[0]) if rows else None

    def list_cards(
        self, status: str, *, region_code: str | None = None, q: str | None = None
    ) -> list[CardRow]:
        """Cards by status. ``region_code`` is a prefix of the shop's code as given (2
        digits for a si/do, 5 for a si/gun/gu); ``q`` is a case-insensitive substring of the shop's si/do or si/gun/gu.

        Published cards are filtered through ``published_card_regions()`` so anonymous
        callers can search too (they cannot read shops); other statuses join shops,
        which only org members can read.
        """
        sql = f"SELECT {_CARD_COLUMNS} FROM cards WHERE status = %s"
        params: list[object] = [status]
        if region_code or q:
            if status == "published":
                sql += (
                    " AND card_id IN (SELECT r.card_id FROM published_card_regions() r"
                    " WHERE true"
                )
            else:
                sql += " AND EXISTS (SELECT 1 FROM shops r WHERE r.shop_id = cards.shop_id"
            if region_code:
                sql += " AND r.region_code LIKE %s"
                params.append(_like_escape(region_code) + "%")
            if q:
                pattern = "%" + _like_escape(q) + "%"
                sql += " AND (r.region_sido ILIKE %s OR r.region_sigungu ILIKE %s)"
                params.extend([pattern, pattern])
            sql += ")"
        sql += " ORDER BY updated_at DESC"
        return [_card_from(r) for r in self._fetch(sql, tuple(params))]

    def add_card_version(self, row: CardVersionRow) -> None:
        org_id = self._org_id()
        with self._tx() as conn:
            self._check_parent(conn, "cards", "card_id", row.card_id, org_id)
            conn.execute(
                "INSERT INTO card_versions (card_id, version, org_id, status, sections_json, "
                "evidence_json, created_by, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    row.card_id,
                    row.version,
                    org_id,
                    row.status,
                    row.sections_json,
                    row.evidence_json,
                    row.created_by,
                    row.created_at,
                ),
            )

    def list_card_versions(self, card_id: str) -> list[CardVersionRow]:
        rows = self._fetch(
            f"SELECT {_CARD_VERSION_COLUMNS} FROM card_versions WHERE card_id = %s "
            "ORDER BY version ASC",
            (card_id,),
        )
        return [_card_version_from(r) for r in rows]

    # consents
    def insert_consent(self, row: ConsentRow) -> None:
        org_id = self._org_id()
        with self._tx() as conn:
            self._check_parent(conn, "shops", "shop_id", row.shop_id, org_id)
            conn.execute(
                "INSERT INTO consents (consent_id, org_id, shop_id, kind, method, note, "
                "granted_by, granted_at, revoked_at, revoke_reason) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    row.consent_id,
                    org_id,
                    row.shop_id,
                    row.kind,
                    row.method,
                    row.note,
                    row.granted_by,
                    row.granted_at,
                    row.revoked_at,
                    row.revoke_reason,
                ),
            )

    def update_consent(self, row: ConsentRow) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE consents SET kind = %s, method = %s, note = %s, revoked_at = %s, "
                "revoke_reason = %s WHERE consent_id = %s",
                (
                    row.kind,
                    row.method,
                    row.note,
                    row.revoked_at,
                    row.revoke_reason,
                    row.consent_id,
                ),
            )

    def get_consent(self, consent_id: str) -> ConsentRow | None:
        rows = self._fetch(
            f"SELECT {_CONSENT_COLUMNS} FROM consents WHERE consent_id = %s", (consent_id,)
        )
        return _consent_from(rows[0]) if rows else None

    def list_consents(self, shop_id: str) -> list[ConsentRow]:
        rows = self._fetch(
            f"SELECT {_CONSENT_COLUMNS} FROM consents WHERE shop_id = %s "
            "ORDER BY granted_at ASC, consent_id ASC",
            (shop_id,),
        )
        return [_consent_from(r) for r in rows]

    def active_consent_kinds(self, shop_id: str) -> set[str]:
        rows = self._fetch(
            "SELECT DISTINCT kind FROM consents WHERE shop_id = %s AND revoked_at IS NULL",
            (shop_id,),
        )
        return {str(r[0]) for r in rows}

    # audit
    def add_audit(self, action: str, table_name: str, row_id: str, meta: dict[str, Any]) -> None:
        org_id = self._org_id()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO audit_log (org_id, actor_id, action, table_name, row_id, new_json) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (org_id, PRINCIPAL.get().user_id, action, table_name, row_id, Jsonb(meta)),
            )

    # feedback
    def add_feedback(self, card_id: str, vote: str, fingerprint: str) -> bool:
        """Count a vote; False if this fingerprint already voted on the card today.

        ParentNotFoundError if the card is not published.
        """
        # No ON CONFLICT: its arbiter check needs SELECT on card_feedback, which
        # neither anon nor authenticated has.
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO card_feedback (card_id, vote, fingerprint) VALUES (%s, %s, %s)",
                    (card_id, vote, fingerprint),
                )
            return True
        except errors.UniqueViolation:
            return False
        except (errors.InsufficientPrivilege, errors.ForeignKeyViolation):
            raise ParentNotFoundError(f"published card {card_id} not found") from None

    def feedback_counts(self, card_id: str) -> FeedbackCounts:
        rows = self._fetch("SELECT up, down FROM card_feedback_counts(%s)", (card_id,))
        return FeedbackCounts(int(rows[0][0]), int(rows[0][1])) if rows else FeedbackCounts(0, 0)

    # owner reviews
    def owner_card_view(self, card_id: str) -> dict[str, Any] | None:
        """Card for the shop owner's review page. Bypasses RLS: verify the link first."""
        with self._tx(ANON) as conn:
            row = conn.execute("SELECT owner_card_view(%s)", (card_id,)).fetchone()
        if row is None or row[0] is None:
            return None
        view: dict[str, Any] = row[0]
        return view

    def add_owner_review(self, row: OwnerReviewRow) -> None:
        """Record a decision. org_id, shop_id and created_at are taken from the database."""
        try:
            with self._tx(ANON) as conn:
                conn.execute(
                    "SELECT owner_review_insert(%s, %s, %s, %s, %s, %s)",
                    (
                        row.review_id,
                        row.card_id,
                        row.card_version,
                        row.token_id,
                        row.decision,
                        row.comment,
                    ),
                )
        except errors.UniqueViolation:
            raise AlreadyDecidedError("already_decided") from None
        except errors.NoDataFound:
            raise ParentNotFoundError(f"card {row.card_id} not found") from None

    def latest_owner_review(self, card_id: str) -> OwnerReviewRow | None:
        rows = self._fetch(
            f"SELECT {_OWNER_REVIEW_COLUMNS} FROM owner_reviews WHERE card_id = %s "
            "ORDER BY created_at DESC, review_id DESC LIMIT 1",
            (card_id,),
        )
        return _owner_review_from(rows[0]) if rows else None

    # LLM usage
    def add_llm_usage(self, row: LlmUsageRow) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO llm_usage (org_id, occurred_at, kind, provider, tokens_in, "
                "tokens_out, fallback) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    row.org_id,
                    row.occurred_at,
                    row.kind,
                    row.provider,
                    row.tokens_in,
                    row.tokens_out,
                    row.fallback,
                ),
            )

    def llm_usage_month(self, org_id: str, month: date) -> LlmUsageSummary:
        rows = self._fetch(
            "SELECT calls, tokens_in, tokens_out, fallbacks FROM llm_usage_month(%s, %s)",
            (org_id, month),
        )
        if not rows:
            return LlmUsageSummary(0, 0, 0, 0)
        r = rows[0]
        return LlmUsageSummary(int(r[0]), int(r[1]), int(r[2]), int(r[3]))

    # dashboard and reports
    def dashboard_stats(self, org_id: str, since: date, until: date) -> DashboardStats:
        """Org activity between two dates, both inclusive (UTC days).

        shops: created in the period; interviews: sessions ended in the period;
        cards_by_status / published_cards: cards last updated in the period;
        consents: per kind, grants and revocations that happened in the period.
        """
        lo, hi = _day_bounds(since, until)
        with self._tx() as conn:
            shops = conn.execute(
                "SELECT count(*) FROM shops WHERE org_id = %s "
                "AND created_at >= %s AND created_at < %s",
                (org_id, lo, hi),
            ).fetchone()
            interviews = conn.execute(
                "SELECT count(*) FROM sessions WHERE org_id = %s "
                "AND ended_at >= %s AND ended_at < %s",
                (org_id, lo, hi),
            ).fetchone()
            by_status = conn.execute(
                "SELECT status, count(*) FROM cards WHERE org_id = %s "
                "AND updated_at >= %s AND updated_at < %s GROUP BY status",
                (org_id, lo, hi),
            ).fetchall()
            consent_rows = conn.execute(
                "SELECT kind, "
                "count(*) FILTER (WHERE granted_at >= %s AND granted_at < %s), "
                "count(*) FILTER (WHERE revoked_at >= %s AND revoked_at < %s) "
                "FROM consents WHERE org_id = %s GROUP BY kind",
                (lo, hi, lo, hi, org_id),
            ).fetchall()
        cards_by_status = {str(r[0]): int(r[1]) for r in by_status}
        consents = {kind: {"granted": 0, "revoked": 0} for kind in _CONSENT_KINDS}
        for r in consent_rows:
            consents[str(r[0])] = {"granted": int(r[1]), "revoked": int(r[2])}
        return DashboardStats(
            period_from=since,
            period_to=until,
            shops=int(shops[0]) if shops else 0,
            interviews=int(interviews[0]) if interviews else 0,
            published_cards=cards_by_status.get("published", 0),
            cards_by_status=cards_by_status,
            consents=consents,
        )

    def report_rows(self, org_id: str, since: date, until: date, limit: int) -> list[ReportRow]:
        """One row per shop created in the period (dates inclusive), oldest first.

        consent_<kind> is "granted" (an active consent exists), "revoked" (only
        revoked ones) or None; owner_decision is the latest owner review's decision.
        """
        lo, hi = _day_bounds(since, until)
        rows = self._fetch(
            "SELECT s.shop_id, s.trade, "
            "nullif(concat_ws(' ', s.region_sido, s.region_sigungu), ''), "
            "(SELECT count(*) FROM sessions x WHERE x.shop_id = s.shop_id), "
            "(SELECT max(x.started_at) FROM sessions x WHERE x.shop_id = s.shop_id), "
            "c.status, CASE WHEN c.status = 'published' THEN c.updated_at END, "
            "(SELECT CASE WHEN bool_or(k.revoked_at IS NULL) THEN 'granted' "
            "WHEN count(*) > 0 THEN 'revoked' END FROM consents k "
            "WHERE k.shop_id = s.shop_id AND k.kind = 'recording'), "
            "(SELECT CASE WHEN bool_or(k.revoked_at IS NULL) THEN 'granted' "
            "WHEN count(*) > 0 THEN 'revoked' END FROM consents k "
            "WHERE k.shop_id = s.shop_id AND k.kind = 'storage'), "
            "(SELECT CASE WHEN bool_or(k.revoked_at IS NULL) THEN 'granted' "
            "WHEN count(*) > 0 THEN 'revoked' END FROM consents k "
            "WHERE k.shop_id = s.shop_id AND k.kind = 'ai_processing'), "
            "(SELECT CASE WHEN bool_or(k.revoked_at IS NULL) THEN 'granted' "
            "WHEN count(*) > 0 THEN 'revoked' END FROM consents k "
            "WHERE k.shop_id = s.shop_id AND k.kind = 'publication'), "
            "(SELECT o.decision FROM owner_reviews o WHERE o.shop_id = s.shop_id "
            "ORDER BY o.created_at DESC, o.review_id DESC LIMIT 1), "
            "s.created_at "
            "FROM shops s LEFT JOIN cards c ON c.shop_id = s.shop_id "
            "WHERE s.org_id = %s AND s.created_at >= %s AND s.created_at < %s "
            "ORDER BY s.created_at ASC, s.shop_id ASC LIMIT %s",
            (org_id, lo, hi, limit),
        )
        return [
            ReportRow(
                shop_id=r[0],
                trade=r[1],
                region=r[2],
                sessions=int(r[3]),
                last_interview_at=r[4],
                card_status=r[5],
                published_at=r[6],
                consent_recording=r[7],
                consent_storage=r[8],
                consent_ai_processing=r[9],
                consent_publication=r[10],
                owner_decision=r[11],
                created_at=r[12],
            )
            for r in rows
        ]

    # transcription jobs
    def _stt_job_from(self, r: Row) -> SttJobRow:
        return SttJobRow(
            job_id=r[0],
            org_id=r[1],
            session_id=r[2],
            created_by=r[3],
            status=r[4],
            audio_key=r[5],
            content_type=r[6],
            bytes=int(r[7]),
            text=self._dec_opt(r[8]),
            duration_s=r[9],
            provider=r[10],
            error=r[11],
            created_at=r[12],
            started_at=r[13],
            finished_at=r[14],
        )

    def insert_stt_job(self, row: SttJobRow) -> None:
        org_id = self._org_id()
        with self._tx() as conn:
            self._check_parent(conn, "sessions", "session_id", row.session_id, org_id)
            conn.execute(
                "INSERT INTO stt_jobs (job_id, org_id, session_id, created_by, status, "
                "audio_key, content_type, bytes, text, duration_s, provider, error, "
                "created_at, started_at, finished_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    row.job_id,
                    org_id,
                    row.session_id,
                    row.created_by,
                    row.status,
                    row.audio_key,
                    row.content_type,
                    row.bytes,
                    self._enc_opt(row.text),
                    row.duration_s,
                    row.provider,
                    row.error,
                    row.created_at,
                    row.started_at,
                    row.finished_at,
                ),
            )

    def get_stt_job(self, job_id: str) -> SttJobRow | None:
        rows = self._fetch(f"SELECT {_STT_JOB_COLUMNS} FROM stt_jobs WHERE job_id = %s", (job_id,))
        return self._stt_job_from(rows[0]) if rows else None

    def list_stt_jobs(self, session_id: str) -> list[SttJobRow]:
        rows = self._fetch(
            f"SELECT {_STT_JOB_COLUMNS} FROM stt_jobs WHERE session_id = %s "
            "ORDER BY created_at DESC, job_id DESC",
            (session_id,),
        )
        return [self._stt_job_from(r) for r in rows]

    def stt_job_claim(self, job_id: str) -> SttJobRow | None:
        """queued -> running for the worker; None if missing or already taken."""
        with self._app_tx() as conn:
            row = conn.execute(
                f"SELECT {_STT_JOB_COLUMNS} FROM stt_job_claim(%s)", (job_id,)
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return self._stt_job_from(row)

    def stt_job_finish(
        self, job_id: str, text: str, duration_s: float | None, provider: str
    ) -> None:
        with self._app_tx() as conn:
            conn.execute(
                "SELECT stt_job_finish(%s, %s, %s, %s)",
                (job_id, self._enc(text), duration_s, provider),
            )

    def stt_job_fail(self, job_id: str, error: str) -> None:
        with self._app_tx() as conn:
            conn.execute("SELECT stt_job_fail(%s, %s)", (job_id, error[:300]))

    # invoices
    def list_invoices(self, org_id: str) -> list[InvoiceRow]:
        rows = self._fetch(
            f"SELECT {_INVOICE_COLUMNS} FROM invoices WHERE org_id = %s "
            "ORDER BY issued_at DESC, invoice_id DESC",
            (org_id,),
        )
        return [_invoice_from(r) for r in rows]

    def get_invoice(self, invoice_id: str) -> InvoiceRow | None:
        rows = self._fetch(
            f"SELECT {_INVOICE_COLUMNS} FROM invoices WHERE invoice_id = %s", (invoice_id,)
        )
        return _invoice_from(rows[0]) if rows else None

    def invoice_by_order(self, order_id: str) -> InvoiceRow | None:
        """Invoice by the payment provider's order id, with no user (webhooks)."""
        with self._app_tx() as conn:
            row = conn.execute(
                f"SELECT {_INVOICE_COLUMNS} FROM invoice_by_order(%s)", (order_id,)
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return _invoice_from(row)

    def issue_invoice(
        self,
        org_id: str,
        plan: str,
        period_start: date,
        period_end: date,
        amount_krw: int,
        memo: str | None,
    ) -> InvoiceRow:
        with self._app_tx() as conn:
            row = conn.execute(
                f"SELECT {_INVOICE_COLUMNS} FROM invoice_issue(%s, %s, %s, %s, %s, %s, %s)",
                (str(uuid.uuid4()), org_id, plan, period_start, period_end, amount_krw, memo),
            ).fetchone()
        assert row is not None
        return _invoice_from(row)

    def mark_invoice_paid(
        self,
        invoice_id: str,
        method: str,
        provider: str,
        payment_key: str | None,
        paid_at: datetime,
    ) -> InvoiceRow:
        """issued -> paid and the org moves to the invoice's plan. Paying a paid invoice
        returns it unchanged; a void one raises InvoiceNotPayableError."""
        try:
            with self._app_tx() as conn:
                row = conn.execute(
                    f"SELECT {_INVOICE_COLUMNS} FROM invoice_mark_paid(%s, %s, %s, %s, %s)",
                    (invoice_id, method, provider, payment_key, paid_at),
                ).fetchone()
        except errors.RaiseException as exc:
            if _raised(exc, "invoice_not_payable"):
                raise InvoiceNotPayableError("invoice_not_payable") from None
            raise
        except errors.NoDataFound:
            raise LookupError(f"invoice {invoice_id} not found") from None
        assert row is not None
        return _invoice_from(row)

    def void_invoice(self, invoice_id: str) -> None:
        """issued -> void. A paid or refunded invoice raises InvoiceNotPayableError."""
        try:
            with self._app_tx() as conn:
                row = conn.execute(
                    "SELECT status FROM invoice_by_order(%s)", (f"sl-{invoice_id}",)
                ).fetchone()
                if row is not None and row[0] == "refunded":
                    raise InvoiceNotPayableError("invoice_refunded")
                conn.execute("SELECT invoice_void(%s)", (invoice_id,))
        except errors.RaiseException as exc:
            if _raised(exc, "invoice_paid"):
                raise InvoiceNotPayableError("invoice_paid") from None
            raise
        except errors.NoDataFound:
            raise LookupError(f"invoice {invoice_id} not found") from None

    def refund_invoice(self, invoice_id: str, reason: str, refunded_at: datetime) -> InvoiceRow:
        """paid -> refunded (full refund). Any other status raises InvoiceNotRefundableError."""
        try:
            with self._app_tx() as conn:
                row = conn.execute(
                    f"SELECT {_INVOICE_COLUMNS} FROM invoice_refund(%s, %s, %s)",
                    (invoice_id, reason, refunded_at),
                ).fetchone()
        except errors.RaiseException as exc:
            if _raised(exc, "invoice_not_refundable"):
                raise InvoiceNotRefundableError("invoice_not_refundable") from None
            raise
        except errors.NoDataFound:
            raise LookupError(f"invoice {invoice_id} not found") from None
        assert row is not None
        return _invoice_from(row)

    def mark_tax_invoice(self, invoice_id: str, ref: str, issued_at: datetime) -> InvoiceRow:
        """Records the tax invoice number on a paid or refunded invoice."""
        try:
            with self._app_tx() as conn:
                row = conn.execute(
                    f"SELECT {_INVOICE_COLUMNS} FROM invoice_mark_tax(%s, %s, %s)",
                    (invoice_id, ref, issued_at),
                ).fetchone()
        except errors.RaiseException as exc:
            if _raised(exc, "invoice_not_payable"):
                raise InvoiceNotPayableError("invoice_not_payable") from None
            raise
        except errors.NoDataFound:
            raise LookupError(f"invoice {invoice_id} not found") from None
        assert row is not None
        return _invoice_from(row)

    # billing keys (app role only; billing_key is encrypted at rest)
    def _billing_key_from(self, r: Row) -> BillingKeyRow:
        return BillingKeyRow(
            org_id=r[0],
            provider=r[1],
            billing_key=self._dec(r[2]),
            customer_key=r[3],
            card_label=r[4],
            plan=r[5],
            charge_day=r[6],
            next_charge_on=r[7],
            status=r[8],
            failures=r[9],
            created_by=r[10],
            created_at=r[11],
            stopped_at=r[12],
        )

    def get_billing_key(self, org_id: str) -> BillingKeyRow | None:
        with self._app_tx() as conn:
            row = conn.execute(
                f"SELECT {_BILLING_KEY_COLUMNS} FROM billing_keys WHERE org_id = %s",
                (org_id,),
            ).fetchone()
        return None if row is None else self._billing_key_from(row)

    def set_billing_key(self, row: BillingKeyRow) -> BillingKeyRow:
        """Stores (or replaces) the org's billing key; it becomes active with no failures."""
        with self._app_tx() as conn:
            got = conn.execute(
                f"SELECT {_BILLING_KEY_COLUMNS} FROM billing_key_set("
                "%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    row.org_id,
                    row.provider,
                    self._enc(row.billing_key),
                    row.customer_key,
                    row.card_label,
                    row.plan,
                    row.charge_day,
                    row.next_charge_on,
                    row.created_by,
                ),
            ).fetchone()
        assert got is not None
        return self._billing_key_from(got)

    def stop_billing_key(self, org_id: str) -> None:
        try:
            with self._app_tx() as conn:
                conn.execute("SELECT billing_key_stop(%s)", (org_id,))
        except errors.RaiseException as exc:
            if _raised(exc, "billing_key_not_found"):
                raise BillingKeyNotFoundError(f"billing key for {org_id} not found") from None
            raise

    def billing_keys_due(self, today: date) -> list[BillingKeyRow]:
        with self._app_tx() as conn:
            rows = conn.execute(
                f"SELECT {_BILLING_KEY_COLUMNS} FROM billing_keys_due(%s)", (today,)
            ).fetchall()
        return [self._billing_key_from(r) for r in rows]

    def advance_billing_key(self, org_id: str, next_charge_on: date) -> None:
        try:
            with self._app_tx() as conn:
                conn.execute("SELECT billing_key_advance(%s, %s)", (org_id, next_charge_on))
        except errors.RaiseException as exc:
            if _raised(exc, "billing_key_not_found"):
                raise BillingKeyNotFoundError(f"billing key for {org_id} not found") from None
            raise

    def fail_billing_key(self, org_id: str) -> int:
        """One more declined charge; the third in a row stops the key. Returns the count."""
        try:
            with self._app_tx() as conn:
                row = conn.execute("SELECT billing_key_fail(%s)", (org_id,)).fetchone()
        except errors.RaiseException as exc:
            if _raised(exc, "billing_key_not_found"):
                raise BillingKeyNotFoundError(f"billing key for {org_id} not found") from None
            raise
        assert row is not None
        return int(row[0])

    # shop owner review links
    def insert_owner_link(self, row: OwnerLinkRow) -> None:
        org_id = self._org_id()
        with self._tx() as conn:
            self._check_parent(conn, "cards", "card_id", row.card_id, org_id)
            conn.execute(
                "INSERT INTO owner_links (token_id, org_id, card_id, issued_at, expires_at, "
                "revoked_at, revoked_by) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    row.token_id,
                    org_id,
                    row.card_id,
                    row.issued_at,
                    row.expires_at,
                    row.revoked_at,
                    row.revoked_by,
                ),
            )

    def list_owner_links(self, card_id: str) -> list[OwnerLinkRow]:
        rows = self._fetch(
            f"SELECT {_OWNER_LINK_COLUMNS} FROM owner_links WHERE card_id = %s "
            "ORDER BY issued_at DESC, token_id DESC",
            (card_id,),
        )
        return [_owner_link_from(r) for r in rows]

    def revoke_owner_link(self, card_id: str, token_id: str) -> None:
        """LookupError if the link is not this card's (or not visible);
        AlreadyRevokedError if it was revoked before."""
        with self._tx() as conn:
            updated = conn.execute(
                "UPDATE owner_links SET revoked_at = now(), revoked_by = %s "
                "WHERE card_id = %s AND token_id = %s AND revoked_at IS NULL "
                "RETURNING token_id",
                (PRINCIPAL.get().user_id, card_id, token_id),
            ).fetchone()
            if updated is not None:
                return
            existing = conn.execute(
                "SELECT 1 FROM owner_links WHERE card_id = %s AND token_id = %s",
                (card_id, token_id),
            ).fetchone()
        if existing is None:
            raise LookupError(f"owner link {token_id} not found")
        raise AlreadyRevokedError("already_revoked")

    def owner_link_state(self, token_id: str) -> str:
        """'revoked' | 'expired' | 'active' | 'unknown' (no row), for any principal."""
        rows = self._fetch("SELECT owner_link_state(%s)", (token_id,))
        return str(rows[0][0]) if rows else "unknown"
