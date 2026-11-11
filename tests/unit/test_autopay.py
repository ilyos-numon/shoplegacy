from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Literal, cast

import psycopg
import pytest

from shoplegacy import autopay, maintenance
from shoplegacy.crypto import FernetCipher, generate_key
from shoplegacy.payments import (
    BillingCharge,
    PaymentError,
    PaymentProvider,
    PaymentResult,
    RefundResult,
)
from shoplegacy.store import ANON, PRINCIPAL, BillingKeyRow, InvoiceRow, Principal, Store

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
DUE = date(2026, 12, 1)


class StubProvider:
    """Billing charges only: approves, declines (PaymentError) or returns a status."""

    name = "fake"

    def __init__(
        self,
        decline: str | None = None,
        status: Literal["paid", "failed", "pending"] = "paid",
    ) -> None:
        self.decline = decline
        self.status = status
        self.charges: list[BillingCharge] = []

    def charge_billing_key(self, charge: BillingCharge) -> PaymentResult:
        self.charges.append(charge)
        if self.decline is not None:
            raise PaymentError(self.decline)
        return PaymentResult(
            payment_key=f"fake_{charge.order_id}",
            order_id=charge.order_id,
            amount_krw=charge.amount_krw,
            status=self.status,
            approved_at=NOW if self.status == "paid" else None,
        )


@contextmanager
def acting(p: Principal) -> Iterator[None]:
    token = PRINCIPAL.set(p)
    try:
        yield
    finally:
        PRINCIPAL.reset(token)


@pytest.fixture
def store(app_dsn: str) -> Iterator[Store]:
    instance = Store(app_dsn)
    yield instance
    instance.close()


@pytest.fixture
def owner(store: Store, pg_dsn: str, make_user: Callable[[str, str], str]) -> Principal:
    user_id = make_user(pg_dsn, "a@example.test")
    first = Principal(user_id=user_id, email="a@example.test", org_id=None, role=None, claims={})
    with acting(first):
        org = store.create_org("org A", user_id)
    return Principal(
        user_id=user_id, email="a@example.test", org_id=org.org_id, role="owner", claims={}
    )


@pytest.fixture
def org_id(owner: Principal) -> str:
    assert owner.org_id is not None
    return owner.org_id


def _key(org_id: str, user_id: str, next_on: date = DUE, charge_day: int = 1) -> BillingKeyRow:
    return BillingKeyRow(
        org_id=org_id,
        provider="fake",
        billing_key=f"fake_bk_sl-{org_id}",
        customer_key=f"sl-{org_id}",
        card_label="테스트카드 ****1234",
        plan="pilot",
        charge_day=charge_day,
        next_charge_on=next_on,
        status="active",
        failures=0,
        created_by=user_id,
        created_at=NOW,
        stopped_at=None,
    )


def _register(store: Store, owner: Principal, next_on: date = DUE) -> BillingKeyRow:
    assert owner.org_id is not None
    return store.set_billing_key(_key(owner.org_id, owner.user_id, next_on))


def _run(store: Store, provider: StubProvider, today: date = DUE) -> list[autopay.ChargeOutcome]:
    # The daily job and the CLI run with no signed-in user.
    assert PRINCIPAL.get() is ANON
    return autopay.charge_due(store, cast(PaymentProvider, provider), today)


def _invoices(store: Store, owner: Principal) -> list[InvoiceRow]:
    assert owner.org_id is not None
    with acting(owner):
        return store.list_invoices(owner.org_id)


def _audit_count(admin_conn: psycopg.Connection, org_id: str, action: str) -> int:
    row = admin_conn.execute(
        "SELECT count(*) FROM audit_log WHERE org_id = %s AND action = %s", (org_id, action)
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_period_for_covers_the_month_and_wraps_december() -> None:
    assert autopay.period_for(date(2026, 11, 17)) == (date(2026, 11, 1), date(2026, 12, 1))
    assert autopay.period_for(date(2026, 12, 31)) == (date(2026, 12, 1), date(2027, 1, 1))
    assert autopay.period_for(date(2027, 1, 1)) == (date(2027, 1, 1), date(2027, 2, 1))


def test_next_charge_after_is_the_charge_day_of_the_next_month() -> None:
    assert autopay.next_charge_after(date(2026, 12, 1), 1) == date(2027, 1, 1)
    assert autopay.next_charge_after(date(2026, 12, 28), 28) == date(2027, 1, 28)
    assert autopay.next_charge_after(date(2027, 1, 28), 28) == date(2027, 2, 28)
    assert autopay.next_charge_after(date(2027, 1, 31), 15) == date(2027, 2, 15)


def test_due_key_issues_and_pays_the_month(
    store: Store, owner: Principal, org_id: str, admin_conn: psycopg.Connection
) -> None:
    _register(store, owner)
    provider = StubProvider()
    [outcome] = _run(store, provider)
    [invoice] = _invoices(store, owner)
    assert outcome == autopay.ChargeOutcome(
        org_id=org_id, invoice_id=invoice.invoice_id, status="paid", code=None
    )
    assert (invoice.plan, invoice.period_start, invoice.period_end) == (
        "pilot",
        date(2026, 12, 1),
        date(2027, 1, 1),
    )
    assert (invoice.status, invoice.method, invoice.provider, invoice.amount_krw) == (
        "paid",
        "card",
        "fake",
        99000,
    )
    assert invoice.provider_payment_key == f"fake_{invoice.order_id}"
    assert invoice.memo == "autopay"
    [charge] = provider.charges
    assert charge.billing_key == f"fake_bk_sl-{org_id}"
    assert charge.customer_key == f"sl-{org_id}"
    assert (charge.order_id, charge.amount_krw) == (invoice.order_id, 99000)
    assert charge.order_name == "가게유산 pilot 2026-12"
    key = store.get_billing_key(org_id)
    assert key is not None and (key.next_charge_on, key.failures) == (date(2027, 1, 1), 0)
    assert _audit_count(admin_conn, org_id, "invoice.paid") == 1


def test_rerun_on_the_same_day_charges_nothing_more(
    store: Store, owner: Principal, org_id: str, admin_conn: psycopg.Connection
) -> None:
    _register(store, owner)
    provider = StubProvider()
    _run(store, provider)
    assert _run(store, provider) == []
    assert _run(store, provider, date(2026, 12, 31)) == []
    assert len(provider.charges) == 1
    assert len(_invoices(store, owner)) == 1
    assert _audit_count(admin_conn, org_id, "invoice.paid") == 1


def test_declined_charge_counts_a_failure_and_reuses_the_invoice(
    store: Store, owner: Principal, org_id: str
) -> None:
    _register(store, owner)
    provider = StubProvider(decline="DECLINED")
    [first] = _run(store, provider)
    [second] = _run(store, provider, date(2026, 12, 2))
    [invoice] = _invoices(store, owner)
    assert (first.status, first.code, first.invoice_id) == (
        "declined",
        "DECLINED",
        invoice.invoice_id,
    )
    assert (second.status, second.invoice_id) == ("declined", invoice.invoice_id)
    assert invoice.status == "issued"
    key = store.get_billing_key(org_id)
    assert key is not None
    assert (key.status, key.failures, key.next_charge_on) == ("active", 2, DUE)


def test_third_decline_stops_autopay(
    store: Store, owner: Principal, org_id: str, admin_conn: psycopg.Connection
) -> None:
    _register(store, owner)
    provider = StubProvider(decline="DECLINED")
    statuses = [_run(store, provider)[0].status for _ in range(3)]
    assert statuses == ["declined", "declined", "stopped"]
    key = store.get_billing_key(org_id)
    assert key is not None and (key.status, key.failures) == ("stopped", 3)
    assert _run(store, provider) == []
    assert len(provider.charges) == 3
    assert _audit_count(admin_conn, org_id, "billing_key.stop") == 1


def test_unpaid_result_is_a_failure(store: Store, owner: Principal, org_id: str) -> None:
    _register(store, owner)
    [outcome] = _run(store, StubProvider(status="pending"))
    assert (outcome.status, outcome.code) == ("declined", "pending")
    key = store.get_billing_key(org_id)
    assert key is not None and key.failures == 1
    assert _invoices(store, owner)[0].status == "issued"


def test_month_paid_already_only_advances(store: Store, owner: Principal, org_id: str) -> None:
    _register(store, owner)
    paid = store.issue_invoice(org_id, "pilot", DUE, date(2027, 1, 1), 99000, None)
    store.mark_invoice_paid(paid.invoice_id, "contract", "manual", None, NOW)
    provider = StubProvider()
    [outcome] = _run(store, provider)
    assert outcome == autopay.ChargeOutcome(
        org_id=org_id, invoice_id=paid.invoice_id, status="already_paid", code=None
    )
    assert provider.charges == []
    key = store.get_billing_key(org_id)
    assert key is not None and key.next_charge_on == date(2027, 1, 1)
    assert len(_invoices(store, owner)) == 1


def test_void_invoice_for_the_month_is_skipped(store: Store, owner: Principal, org_id: str) -> None:
    _register(store, owner)
    void = store.issue_invoice(org_id, "pilot", DUE, date(2027, 1, 1), 99000, None)
    store.void_invoice(void.invoice_id)
    provider = StubProvider()
    [outcome] = _run(store, provider)
    assert (outcome.status, outcome.code, outcome.invoice_id) == (
        "skipped",
        "void",
        void.invoice_id,
    )
    assert provider.charges == []
    key = store.get_billing_key(org_id)
    assert key is not None and (key.next_charge_on, key.failures) == (DUE, 0)


def test_keys_not_yet_due_or_stopped_are_left_alone(
    store: Store, owner: Principal, org_id: str
) -> None:
    _register(store, owner, date(2026, 12, 15))
    provider = StubProvider()
    assert _run(store, provider, date(2026, 12, 14)) == []
    store.stop_billing_key(org_id)
    assert _run(store, provider, date(2026, 12, 15)) == []
    assert provider.charges == []
    assert _invoices(store, owner) == []
    assert PRINCIPAL.get() is ANON


def _cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], dsn: str, *args: str
) -> tuple[int, str, str]:
    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", dsn)
    monkeypatch.setenv("SHOPLEGACY_JWT_SECRET", "autopay-cli-test-secret-0123456789abcdef")
    code = maintenance.main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_charge_due_and_stop_autopay(
    app_dsn: str,
    store: Store,
    owner: Principal,
    org_id: str,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    key = generate_key()
    monkeypatch.setenv("SHOPLEGACY_PII_KEYS", key)
    monkeypatch.setenv("SHOPLEGACY_BILLING_PROVIDER", "fake")
    enc_store = Store(app_dsn, cipher=FernetCipher([key]))
    try:
        _register(enc_store, owner)
    finally:
        enc_store.close()
    assert _cli(monkeypatch, capsys, pg_dsn, "charge-due", "--today", "2026-11-30") == (0, "", "")
    code, out, _ = _cli(monkeypatch, capsys, pg_dsn, "charge-due", "--today", "2026-12-01")
    [invoice] = _invoices(store, owner)
    assert (code, out) == (0, f"{org_id} paid {invoice.invoice_id} -\n")
    assert invoice.status == "paid" and invoice.method == "card"
    assert _cli(monkeypatch, capsys, pg_dsn, "stop-autopay", org_id.upper()) == (
        0,
        f"{org_id} autopay stopped\n",
        "",
    )
    key = store.get_billing_key(org_id)
    assert key is not None and key.status == "stopped"
    missing = "00000000-0000-0000-0000-000000000000"
    assert _cli(monkeypatch, capsys, pg_dsn, "stop-autopay", missing) == (
        1,
        "",
        "billing_key_not_found\n",
    )


def test_cli_refund_and_tax_invoice(
    store: Store,
    org_id: str,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SHOPLEGACY_BILLING_PROVIDER", "fake")
    issued = store.issue_invoice(org_id, "pilot", DUE, date(2027, 1, 1), 99000, None)
    iid = issued.invoice_id
    assert _cli(monkeypatch, capsys, pg_dsn, "refund-invoice", iid, "--reason", "x") == (
        1,
        "",
        "invoice_not_refundable\n",
    )
    assert _cli(monkeypatch, capsys, pg_dsn, "mark-tax-invoice", iid, "--ref", "r") == (
        1,
        "",
        "invoice_not_payable\n",
    )
    store.mark_invoice_paid(iid, "card", "fake", f"fake_{issued.order_id}", NOW)
    assert _cli(monkeypatch, capsys, pg_dsn, "refund-invoice", iid, "--reason", "중복 결제") == (
        0,
        f"{iid} refunded\n",
        "",
    )
    code, out, _ = _cli(
        monkeypatch,
        capsys,
        pg_dsn,
        "mark-tax-invoice",
        iid,
        "--ref",
        "20261104-0001",
        "--issued",
        "2026-11-04",
    )
    assert (code, out) == (0, f"{iid} tax_invoice 20261104-0001\n")
    row = store.invoice_by_order(issued.order_id)
    assert row is not None
    assert (row.status, row.refund_reason, row.tax_invoice_ref) == (
        "refunded",
        "중복 결제",
        "20261104-0001",
    )
    assert row.tax_invoice_issued_at == datetime(2026, 11, 4, tzinfo=UTC)
    assert _cli(monkeypatch, capsys, pg_dsn, "refund-invoice", iid, "--reason", "x") == (
        1,
        "",
        "invoice_not_refundable\n",
    )


def test_cli_void_refunded_invoice_fails(
    store: Store,
    org_id: str,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    issued = store.issue_invoice(org_id, "pilot", DUE, date(2027, 1, 1), 99000, None)
    iid = issued.invoice_id
    store.mark_invoice_paid(iid, "contract", "manual", None, NOW)
    store.refund_invoice(iid, "x", NOW)
    assert _cli(monkeypatch, capsys, pg_dsn, "void-invoice", iid) == (
        1,
        "",
        "invoice_not_voidable\n",
    )
    row = store.invoice_by_order(issued.order_id)
    assert row is not None and row.status == "refunded"


class _BrokenFirst(StubProvider):
    """Raises an unexpected error on the first charge only."""

    def charge_billing_key(self, charge: BillingCharge) -> PaymentResult:
        if not self.charges:
            self.charges.append(charge)
            raise RuntimeError("boom")
        return super().charge_billing_key(charge)


def test_unexpected_error_for_one_org_does_not_stop_the_others(
    store: Store, owner: Principal, pg_dsn: str, make_user: Callable[[str, str], str]
) -> None:
    other_id = make_user(pg_dsn, "b@example.test")
    first = Principal(user_id=other_id, email="b@example.test", org_id=None, role=None, claims={})
    with acting(first):
        other_org = store.create_org("org B", other_id).org_id
    other = Principal(
        user_id=other_id, email="b@example.test", org_id=other_org, role="owner", claims={}
    )
    _register(store, owner)
    _register(store, other)
    provider = _BrokenFirst()
    outcomes = _run(store, provider)
    assert len(outcomes) == 2 and len(provider.charges) == 2
    broken_org = provider.charges[0].customer_key.removeprefix("sl-")
    by_org = {o.org_id: o for o in outcomes}
    broken = by_org.pop(broken_org)
    assert (broken.status, broken.invoice_id, broken.code) == (
        "skipped",
        None,
        "error:RuntimeError",
    )
    [ok] = by_org.values()
    assert ok.status == "paid"
    key = store.get_billing_key(broken_org)
    assert key is not None and (key.next_charge_on, key.failures) == (DUE, 0)


class _CancelStub:
    name = "fake"

    def __init__(self, status: Literal["refunded", "failed"]) -> None:
        self.status = status
        self.cancels: list[str] = []

    def cancel(self, payment_key: str, reason: str) -> RefundResult:
        self.cancels.append(payment_key)
        return RefundResult(payment_key=payment_key, status=self.status, refunded_at=None)


def _paid_card_invoice(store: Store, org_id: str) -> InvoiceRow:
    issued = store.issue_invoice(org_id, "pilot", DUE, date(2027, 1, 1), 99000, None)
    return store.mark_invoice_paid(
        issued.invoice_id, "card", "fake", f"fake_{issued.order_id}", NOW
    )


def test_cli_refund_not_canceled_at_provider_records_nothing(
    store: Store,
    org_id: str,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paid = _paid_card_invoice(store, org_id)
    stub = _CancelStub("failed")
    monkeypatch.setattr(maintenance, "build_payment_provider", lambda _settings: stub)
    code, out, err = _cli(
        monkeypatch, capsys, pg_dsn, "refund-invoice", paid.invoice_id, "--reason", "x"
    )
    assert (code, out, err) == (2, "", "refund_failed:not_canceled\n")
    assert stub.cancels == [paid.provider_payment_key]
    row = store.invoice_by_order(paid.order_id)
    assert row is not None and (row.status, row.refunded_at, row.refund_reason) == (
        "paid",
        None,
        None,
    )


def test_cli_refund_canceled_but_not_recorded_says_so(
    store: Store,
    org_id: str,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paid = _paid_card_invoice(store, org_id)
    stub = _CancelStub("refunded")
    monkeypatch.setattr(maintenance, "build_payment_provider", lambda _settings: stub)

    def broken(self: Store, invoice_id: str, reason: str, refunded_at: datetime) -> InvoiceRow:
        raise psycopg.OperationalError("connection lost")

    monkeypatch.setattr(Store, "refund_invoice", broken)
    code, out, err = _cli(
        monkeypatch, capsys, pg_dsn, "refund-invoice", paid.invoice_id, "--reason", "x"
    )
    assert (code, out) == (3, "")
    assert paid.invoice_id in err
    assert "already canceled at the provider" in err and "record the refund manually" in err
    assert stub.cancels == [paid.provider_payment_key]
    monkeypatch.undo()
    row = store.invoice_by_order(paid.order_id)
    assert row is not None and row.status == "paid"
