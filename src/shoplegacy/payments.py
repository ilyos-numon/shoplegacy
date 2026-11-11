"""Payment providers for invoice checkout."""

import base64
import os
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlencode

import httpx
from pydantic import BaseModel

from shoplegacy.config import Settings
from shoplegacy.store import InvoiceRow

PLAN_PRICES_KRW: dict[str, int] = {"pilot": 99000, "org": 490000}
CUSTOMER_KEY_PREFIX = "sl-"


class PaymentError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CheckoutSession(BaseModel):
    provider: str
    order_id: str
    amount_krw: int
    client_key: str | None
    checkout_url: str | None
    success_url: str
    fail_url: str


class PaymentResult(BaseModel):
    payment_key: str
    order_id: str
    amount_krw: int
    status: Literal["paid", "failed", "pending"]
    approved_at: datetime | None


class BillingKeyIssued(BaseModel):
    billing_key: str
    card_label: str | None


class BillingCharge(BaseModel):
    billing_key: str
    customer_key: str
    order_id: str
    amount_krw: int
    order_name: str


class RefundResult(BaseModel):
    payment_key: str
    status: Literal["refunded", "failed"]
    refunded_at: datetime | None


class PaymentProvider(Protocol):
    name: str

    def checkout(self, invoice: InvoiceRow, success_url: str, fail_url: str) -> CheckoutSession: ...

    def confirm(self, payment_key: str, order_id: str, amount_krw: int) -> PaymentResult: ...

    def lookup(self, payment_key: str) -> PaymentResult: ...

    def issue_billing_key(self, auth_key: str, customer_key: str) -> BillingKeyIssued: ...

    def charge_billing_key(self, charge: BillingCharge) -> PaymentResult: ...

    def cancel(self, payment_key: str, reason: str) -> RefundResult: ...


class FakePaymentProvider:
    """Test checkout: a payment key ``fake_<order_id>`` is always approved."""

    name = "fake"

    def __init__(self, public_base_url: str) -> None:
        self._base = public_base_url.rstrip("/")
        self._amounts: dict[str, int] = {}

    def checkout(self, invoice: InvoiceRow, success_url: str, fail_url: str) -> CheckoutSession:
        self._amounts[invoice.order_id] = invoice.amount_krw
        query = urlencode(
            {
                "orderId": invoice.order_id,
                "amount": invoice.amount_krw,
                "successUrl": success_url,
            }
        )
        return CheckoutSession(
            provider=self.name,
            order_id=invoice.order_id,
            amount_krw=invoice.amount_krw,
            client_key=None,
            checkout_url=f"{self._base}/billing/pay-test?{query}",
            success_url=success_url,
            fail_url=fail_url,
        )

    def confirm(self, payment_key: str, order_id: str, amount_krw: int) -> PaymentResult:
        if not payment_key.startswith("fake_") or payment_key[5:] != order_id:
            raise PaymentError("invalid_key")
        self._amounts[order_id] = amount_krw
        return PaymentResult(
            payment_key=payment_key,
            order_id=order_id,
            amount_krw=amount_krw,
            status="paid",
            approved_at=datetime.now(UTC),
        )

    def lookup(self, payment_key: str) -> PaymentResult:
        if not payment_key.startswith("fake_") or len(payment_key) <= 5:
            raise PaymentError("not_found")
        order_id = payment_key[5:]
        return PaymentResult(
            payment_key=payment_key,
            order_id=order_id,
            amount_krw=self._amounts.get(order_id, 0),
            status="paid",
            approved_at=datetime.now(UTC),
        )

    def issue_billing_key(self, auth_key: str, customer_key: str) -> BillingKeyIssued:
        if not auth_key.startswith("fake_auth_"):
            raise PaymentError("invalid_auth_key")
        return BillingKeyIssued(
            billing_key=f"fake_bk_{customer_key}", card_label="테스트카드 ****1234"
        )

    def charge_billing_key(self, charge: BillingCharge) -> PaymentResult:
        if not charge.billing_key.startswith("fake_bk_"):
            raise PaymentError("invalid_billing_key")
        if os.environ.get("SHOPLEGACY_FAKE_DECLINE_BILLING") == "1":
            raise PaymentError("DECLINED")
        self._amounts[charge.order_id] = charge.amount_krw
        return PaymentResult(
            payment_key=f"fake_{charge.order_id}",
            order_id=charge.order_id,
            amount_krw=charge.amount_krw,
            status="paid",
            approved_at=datetime.now(UTC),
        )

    def cancel(self, payment_key: str, reason: str) -> RefundResult:
        if not payment_key.startswith("fake_"):
            raise PaymentError("not_found")
        return RefundResult(
            payment_key=payment_key, status="refunded", refunded_at=datetime.now(UTC)
        )


# Toss payment statuses that will never become DONE.
_TOSS_FAILED = frozenset({"CANCELED", "PARTIAL_CANCELED", "ABORTED", "EXPIRED"})


class TossPaymentProvider:
    """Toss Payments card checkout: the browser widget pays, the server confirms."""

    name = "toss"

    def __init__(
        self,
        client_key: str,
        secret_key: str,
        api_base: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client_key = client_key
        token = base64.b64encode(f"{secret_key}:".encode()).decode("ascii")
        self._client = httpx.Client(
            base_url=api_base.rstrip("/"),
            timeout=10,
            headers={"Authorization": f"Basic {token}"},
            transport=transport,
        )

    def checkout(self, invoice: InvoiceRow, success_url: str, fail_url: str) -> CheckoutSession:
        return CheckoutSession(
            provider=self.name,
            order_id=invoice.order_id,
            amount_krw=invoice.amount_krw,
            client_key=self._client_key,
            checkout_url=None,
            success_url=success_url,
            fail_url=fail_url,
        )

    def confirm(self, payment_key: str, order_id: str, amount_krw: int) -> PaymentResult:
        body = self._request(
            "POST",
            "/v1/payments/confirm",
            json={"paymentKey": payment_key, "orderId": order_id, "amount": amount_krw},
        )
        return _toss_result(body)

    def lookup(self, payment_key: str) -> PaymentResult:
        return _toss_result(self._request("GET", f"/v1/payments/{quote(payment_key, safe='')}"))

    def issue_billing_key(self, auth_key: str, customer_key: str) -> BillingKeyIssued:
        body = self._request(
            "POST",
            "/v1/billing/authorizations/issue",
            json={"authKey": auth_key, "customerKey": customer_key},
        )
        billing_key = body.get("billingKey")
        if not isinstance(billing_key, str) or not billing_key:
            raise PaymentError("invalid_response")
        return BillingKeyIssued(billing_key=billing_key, card_label=_card_label(body))

    def charge_billing_key(self, charge: BillingCharge) -> PaymentResult:
        body = self._request(
            "POST",
            f"/v1/billing/{quote(charge.billing_key, safe='')}",
            json={
                "customerKey": charge.customer_key,
                "amount": charge.amount_krw,
                "orderId": charge.order_id,
                "orderName": charge.order_name,
            },
        )
        return _toss_result(body)

    def cancel(self, payment_key: str, reason: str) -> RefundResult:
        body = self._request(
            "POST",
            f"/v1/payments/{quote(payment_key, safe='')}/cancel",
            json={"cancelReason": reason},
        )
        # Only full refunds are supported: PARTIAL_CANCELED is not a refund.
        if str(body.get("status", "")) != "CANCELED":
            return RefundResult(payment_key=payment_key, status="failed", refunded_at=None)
        canceled_at: datetime | None = None
        cancels = body.get("cancels")
        if isinstance(cancels, list) and cancels and isinstance(cancels[-1], dict):
            raw = cancels[-1].get("canceledAt")
            if isinstance(raw, str) and raw:
                try:
                    canceled_at = datetime.fromisoformat(raw)
                except ValueError:
                    canceled_at = None
        return RefundResult(
            payment_key=str(body.get("paymentKey") or payment_key),
            status="refunded",
            refunded_at=canceled_at or datetime.now(UTC),
        )

    def _request(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, json=json)
        except httpx.HTTPError:
            raise PaymentError("provider_unavailable") from None
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code >= 400:
            code = body.get("code") if isinstance(body, dict) else None
            raise PaymentError(str(code) if code else f"http_{response.status_code}")
        if not isinstance(body, dict):
            raise PaymentError("invalid_response")
        return body


def _card_label(body: dict[str, Any]) -> str | None:
    """``"<company> ****<last 4>"`` from a billing-key response, or whatever part is there."""
    card = body.get("card")
    card = card if isinstance(card, dict) else {}
    company = card.get("company") or body.get("cardCompany")
    company = str(company) if company else None
    number = card.get("number")
    digits = str(number)[-4:] if number else ""
    if company and digits:
        return f"{company} ****{digits}"
    if company:
        return company
    return f"****{digits}" if digits else None


def _toss_result(body: dict[str, Any]) -> PaymentResult:
    raw = str(body.get("status", ""))
    status: Literal["paid", "failed", "pending"] = (
        "paid" if raw == "DONE" else "failed" if raw in _TOSS_FAILED else "pending"
    )
    approved = body.get("approvedAt")
    try:
        return PaymentResult(
            payment_key=str(body["paymentKey"]),
            order_id=str(body["orderId"]),
            amount_krw=int(body["totalAmount"]),
            status=status,
            approved_at=datetime.fromisoformat(approved) if approved else None,
        )
    except (KeyError, TypeError, ValueError):
        raise PaymentError("invalid_response") from None


def build_payment_provider(settings: Settings) -> PaymentProvider:
    if settings.billing_provider == "toss":
        return TossPaymentProvider(
            settings.toss_client_key, settings.toss_secret_key, settings.toss_api_base
        )
    return FakePaymentProvider(settings.public_base_url)
