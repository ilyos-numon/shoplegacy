"""Invoice and payment routes (mounted under ``/api``)."""

from datetime import UTC, date, datetime
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from shoplegacy.app_state import State, get_state
from shoplegacy.auth import UserDep
from shoplegacy.payments import CUSTOMER_KEY_PREFIX, CheckoutSession, PaymentError
from shoplegacy.routes_billing import PathMemberDep
from shoplegacy.store import (
    BillingKeyNotFoundError,
    BillingKeyRow,
    InvoiceNotPayableError,
    InvoiceRow,
    Principal,
)

router = APIRouter()
StateDep = Annotated[State, Depends(get_state)]


class InvoiceOut(BaseModel):
    invoice_id: str
    org_id: str
    plan: str
    period_start: date
    period_end: date
    amount_krw: int
    status: str
    method: str | None
    provider: str | None
    order_id: str
    memo: str | None
    issued_at: datetime
    paid_at: datetime | None
    refunded_at: datetime | None
    refund_reason: str | None
    tax_invoice_ref: str | None
    tax_invoice_issued_at: datetime | None


class ConfirmIn(BaseModel):
    payment_key: str
    order_id: str
    amount_krw: int


class BillingAuthSession(BaseModel):
    provider: str
    client_key: str | None
    customer_key: str
    auth_url: str | None
    success_url: str
    fail_url: str


class BillingKeyIn(BaseModel):
    auth_key: str
    customer_key: str
    plan: Literal["pilot", "org"]
    charge_day: int = Field(default=1, ge=1, le=28)


class BillingKeyOut(BaseModel):
    """A billing key as members see it: never the key itself."""

    org_id: str
    provider: str
    card_label: str | None
    plan: str
    charge_day: int
    next_charge_on: date
    status: str
    failures: int
    created_at: datetime


class WebhookData(BaseModel):
    paymentKey: str
    orderId: str
    status: str | None = None


class WebhookIn(BaseModel):
    eventType: str
    data: WebhookData


def _out(row: InvoiceRow) -> InvoiceOut:
    return InvoiceOut(
        invoice_id=row.invoice_id,
        org_id=row.org_id,
        plan=row.plan,
        period_start=row.period_start,
        period_end=row.period_end,
        amount_krw=row.amount_krw,
        status=row.status,
        method=row.method,
        provider=row.provider,
        order_id=row.order_id,
        memo=row.memo,
        issued_at=row.issued_at,
        paid_at=row.paid_at,
        refunded_at=row.refunded_at,
        refund_reason=row.refund_reason,
        tax_invoice_ref=row.tax_invoice_ref,
        tax_invoice_issued_at=row.tax_invoice_issued_at,
    )


def _key_out(row: BillingKeyRow) -> BillingKeyOut:
    return BillingKeyOut(
        org_id=row.org_id,
        provider=row.provider,
        card_label=row.card_label,
        plan=row.plan,
        charge_day=row.charge_day,
        next_charge_on=row.next_charge_on,
        status=row.status,
        failures=row.failures,
        created_at=row.created_at,
    )


def _require_owner(member: Principal) -> None:
    if member.role != "owner":
        raise HTTPException(status_code=403, detail="forbidden")


def _customer_key(org_id: str) -> str:
    return f"{CUSTOMER_KEY_PREFIX}{org_id.lower()}"


def first_charge_on(today: date, charge_day: int) -> date:
    """This month's ``charge_day`` if it has not passed yet, otherwise next month's."""
    if today.day <= charge_day:
        return today.replace(day=charge_day)
    if today.month == 12:
        return date(today.year + 1, 1, charge_day)
    return date(today.year, today.month + 1, charge_day)


def _mark_paid(
    state: State, invoice: InvoiceRow, payment_key: str, paid_at: datetime | None
) -> InvoiceRow:
    try:
        return state.store.mark_invoice_paid(
            invoice.invoice_id,
            "card",
            state.payments.name,
            payment_key,
            paid_at or datetime.now(UTC),
        )
    except InvoiceNotPayableError:
        raise HTTPException(status_code=409, detail="invoice_not_payable") from None


@router.get("/orgs/{org_id}/invoices")
def list_invoices(org_id: str, _: PathMemberDep, state: StateDep) -> list[InvoiceOut]:
    return [_out(r) for r in state.store.list_invoices(org_id.lower())]


@router.post("/orgs/{org_id}/invoices/{invoice_id}/checkout", status_code=201)
def checkout(
    org_id: str, invoice_id: str, member: PathMemberDep, state: StateDep
) -> CheckoutSession:
    if member.role != "owner":
        raise HTTPException(status_code=403, detail="forbidden")
    invoice = state.store.get_invoice(invoice_id)
    if invoice is None or invoice.org_id.lower() != org_id.lower():
        raise HTTPException(status_code=404, detail="invoice not found")
    if invoice.status != "issued":
        raise HTTPException(status_code=409, detail="invoice_not_payable")
    base = state.settings.public_base_url.rstrip("/")
    return state.payments.checkout(
        invoice,
        success_url=f"{base}/billing?paid={invoice.invoice_id}",
        fail_url=f"{base}/billing?failed={invoice.invoice_id}",
    )


@router.post("/billing/confirm")
def confirm(body: ConfirmIn, p: UserDep, state: StateDep) -> InvoiceOut:
    invoice = state.store.invoice_by_order(body.order_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    wanted = invoice.org_id.lower()
    role = next(
        (m.role for m in state.store.my_orgs(p.user_id) if m.org_id.lower() == wanted), None
    )
    if role != "owner":
        raise HTTPException(status_code=403, detail="forbidden")
    if body.amount_krw != invoice.amount_krw:
        raise HTTPException(status_code=409, detail="amount_mismatch")
    if invoice.status == "paid":
        return _out(invoice)
    if invoice.status != "issued":
        raise HTTPException(status_code=409, detail="invoice_not_payable")
    try:
        result = state.payments.confirm(body.payment_key, body.order_id, body.amount_krw)
    except PaymentError as exc:
        raise HTTPException(status_code=402, detail=f"payment_failed:{exc.code}") from None
    if result.status != "paid":
        raise HTTPException(status_code=402, detail=f"payment_failed:{result.status}")
    return _out(_mark_paid(state, invoice, result.payment_key, result.approved_at))


@router.post("/billing/webhook")
def webhook(body: WebhookIn, state: StateDep) -> dict[str, object]:
    invoice = state.store.invoice_by_order(body.data.orderId)
    if invoice is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    try:
        result = state.payments.lookup(body.data.paymentKey)
    except PaymentError as exc:
        return {"ok": False, "reason": f"lookup_failed:{exc.code}"}
    if result.order_id != invoice.order_id:
        return {"ok": False, "reason": "order_mismatch"}
    if result.status != "paid":
        return {"ok": False, "reason": f"status:{result.status}"}
    if result.amount_krw != invoice.amount_krw:
        return {"ok": False, "reason": "amount_mismatch"}
    try:
        _mark_paid(state, invoice, result.payment_key, result.approved_at)
    except HTTPException:
        return {"ok": False, "reason": "invoice_not_payable"}
    return {"ok": True}


@router.post("/orgs/{org_id}/billing-key/auth", status_code=201)
def billing_key_auth(org_id: str, member: PathMemberDep, state: StateDep) -> BillingAuthSession:
    _require_owner(member)
    base = state.settings.public_base_url.rstrip("/")
    customer_key = _customer_key(org_id)
    success_url = f"{base}/billing?billing_auth=1"
    fail_url = f"{base}/billing?billing_failed=1"
    if state.payments.name == "toss":
        client_key: str | None = state.settings.toss_client_key
        auth_url: str | None = None
    else:
        client_key = None
        query = urlencode(
            {"mode": "billing", "customerKey": customer_key, "successUrl": success_url}
        )
        auth_url = f"{base}/billing/pay-test?{query}"
    return BillingAuthSession(
        provider=state.payments.name,
        client_key=client_key,
        customer_key=customer_key,
        auth_url=auth_url,
        success_url=success_url,
        fail_url=fail_url,
    )


@router.post("/orgs/{org_id}/billing-key", status_code=201)
def set_billing_key(
    org_id: str, body: BillingKeyIn, member: PathMemberDep, state: StateDep
) -> BillingKeyOut:
    _require_owner(member)
    wanted = org_id.lower()
    customer_key = _customer_key(wanted)
    if body.customer_key != customer_key:
        raise HTTPException(status_code=422, detail="customer_key_mismatch")
    try:
        issued = state.payments.issue_billing_key(body.auth_key, customer_key)
    except PaymentError as exc:
        raise HTTPException(status_code=402, detail=f"billing_auth_failed:{exc.code}") from None
    now = datetime.now(UTC)
    row = state.store.set_billing_key(
        BillingKeyRow(
            org_id=wanted,
            provider=state.payments.name,
            billing_key=issued.billing_key,
            customer_key=customer_key,
            card_label=issued.card_label,
            plan=body.plan,
            charge_day=body.charge_day,
            next_charge_on=first_charge_on(now.date(), body.charge_day),
            status="active",
            failures=0,
            created_by=member.user_id,
            created_at=now,
            stopped_at=None,
        )
    )
    return _key_out(row)


@router.get("/orgs/{org_id}/billing-key")
def get_billing_key(org_id: str, _: PathMemberDep, state: StateDep) -> BillingKeyOut:
    row = state.store.get_billing_key(org_id.lower())
    if row is None:
        raise HTTPException(status_code=404, detail="billing key not set")
    return _key_out(row)


@router.delete("/orgs/{org_id}/billing-key", status_code=204)
def stop_billing_key(org_id: str, member: PathMemberDep, state: StateDep) -> Response:
    _require_owner(member)
    try:
        state.store.stop_billing_key(org_id.lower())
    except BillingKeyNotFoundError:
        raise HTTPException(status_code=404, detail="billing key not set") from None
    return Response(status_code=204)
