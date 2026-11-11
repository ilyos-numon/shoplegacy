"""Monthly card charges against stored billing keys."""

from datetime import UTC, date, datetime
from typing import Literal

import structlog
from psycopg import errors
from pydantic import BaseModel

from shoplegacy.payments import PLAN_PRICES_KRW, BillingCharge, PaymentError, PaymentProvider
from shoplegacy.store import PRINCIPAL, BillingKeyRow, InvoiceRow, Principal, Store

MAX_FAILURES = 3
AUTOPAY_MEMO = "autopay"

log = structlog.get_logger("shoplegacy")


class ChargeOutcome(BaseModel):
    org_id: str
    invoice_id: str | None
    status: Literal["paid", "already_paid", "declined", "stopped", "skipped"]
    code: str | None


def period_for(day: date) -> tuple[date, date]:
    """(first day of ``day``'s month, first day of the next month)."""
    start = day.replace(day=1)
    if start.month == 12:
        return start, date(start.year + 1, 1, 1)
    return start, date(start.year, start.month + 1, 1)


def next_charge_after(day: date, charge_day: int) -> date:
    """``charge_day`` of the month after ``day`` (charge days are 1..28, always valid)."""
    return period_for(day)[1].replace(day=charge_day)


def _period_invoice(store: Store, key: BillingKeyRow, start: date) -> InvoiceRow | None:
    """The org's invoice for this plan and period, if one was issued already.

    Invoices are readable only by org members, so this reads as the member who
    registered the key; with no such member nothing is found.
    """
    if key.created_by is None:
        return None
    reader = Principal(
        user_id=key.created_by, email=None, org_id=key.org_id, role="owner", claims={}
    )
    token = PRINCIPAL.set(reader)
    try:
        rows = store.list_invoices(key.org_id)
    finally:
        PRINCIPAL.reset(token)
    return next((r for r in rows if r.plan == key.plan and r.period_start == start), None)


def _declined(store: Store, key: BillingKeyRow, invoice_id: str, code: str) -> ChargeOutcome:
    failures = store.fail_billing_key(key.org_id)
    status: Literal["declined", "stopped"] = "stopped" if failures >= MAX_FAILURES else "declined"
    return ChargeOutcome(org_id=key.org_id, invoice_id=invoice_id, status=status, code=code)


def _charge_one(store: Store, provider: PaymentProvider, key: BillingKeyRow) -> ChargeOutcome:
    start, end = period_for(key.next_charge_on)
    invoice = _period_invoice(store, key, start)
    if invoice is None:
        try:
            invoice = store.issue_invoice(
                key.org_id, key.plan, start, end, PLAN_PRICES_KRW[key.plan], AUTOPAY_MEMO
            )
        except errors.UniqueViolation:
            return ChargeOutcome(
                org_id=key.org_id, invoice_id=None, status="skipped", code="invoice_exists"
            )
    following = next_charge_after(key.next_charge_on, key.charge_day)
    if invoice.status == "paid":
        store.advance_billing_key(key.org_id, following)
        return ChargeOutcome(
            org_id=key.org_id, invoice_id=invoice.invoice_id, status="already_paid", code=None
        )
    if invoice.status != "issued":
        return ChargeOutcome(
            org_id=key.org_id, invoice_id=invoice.invoice_id, status="skipped", code=invoice.status
        )
    charge = BillingCharge(
        billing_key=key.billing_key,
        customer_key=key.customer_key,
        order_id=invoice.order_id,
        amount_krw=invoice.amount_krw,
        order_name=f"가게유산 {key.plan} {start:%Y-%m}",
    )
    try:
        result = provider.charge_billing_key(charge)
    except PaymentError as exc:
        return _declined(store, key, invoice.invoice_id, exc.code)
    if result.status != "paid":
        return _declined(store, key, invoice.invoice_id, result.status)
    store.mark_invoice_paid(
        invoice.invoice_id,
        "card",
        provider.name,
        result.payment_key,
        result.approved_at or datetime.now(UTC),
    )
    store.advance_billing_key(key.org_id, following)
    return ChargeOutcome(org_id=key.org_id, invoice_id=invoice.invoice_id, status="paid", code=None)


def charge_due(store: Store, provider: PaymentProvider, today: date) -> list[ChargeOutcome]:
    """Charges every active billing key due on or before ``today``.

    One invoice per org, plan and month: a rerun finds the month's invoice instead
    of issuing another, and a paid one only moves the key to its next charge day.
    A declined charge counts as a failure; the third in a row stops the key.
    Any other error is logged and reported as ``skipped`` with code ``error:<type>``.
    """
    outcomes: list[ChargeOutcome] = []
    for key in store.billing_keys_due(today):
        try:
            outcomes.append(_charge_one(store, provider, key))
        except Exception as exc:
            # One org's failure must not hold up the others; the next run retries it.
            log.exception("autopay_charge_error", org_id=key.org_id)
            outcomes.append(
                ChargeOutcome(
                    org_id=key.org_id,
                    invoice_id=None,
                    status="skipped",
                    code=f"error:{type(exc).__name__}",
                )
            )
    return outcomes
