import base64
import dataclasses
import json
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import psycopg
import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shoplegacy import maintenance, routes_payments
from shoplegacy.config import Settings
from shoplegacy.payments import (
    PLAN_PRICES_KRW,
    BillingCharge,
    FakePaymentProvider,
    PaymentError,
    TossPaymentProvider,
    build_payment_provider,
)
from shoplegacy.routes_payments import first_charge_on
from shoplegacy.store import InvoiceRow, Store

OrgTokens = tuple[str, str, str, str]
BASE = "http://shop.example.test"


def _invoice(**changes: object) -> InvoiceRow:
    values: dict[str, object] = {
        "invoice_id": "inv-1",
        "org_id": str(uuid.uuid4()),
        "plan": "pilot",
        "period_start": date(2026, 11, 1),
        "period_end": date(2026, 12, 1),
        "amount_krw": 99000,
        "status": "issued",
        "method": None,
        "provider": None,
        "provider_payment_key": None,
        "order_id": "sl-inv-1",
        "memo": None,
        "issued_at": datetime(2026, 11, 1, tzinfo=UTC),
        "paid_at": None,
    }
    values.update(changes)
    return InvoiceRow(**values)  # type: ignore[arg-type]


def _token(user_id: str, email: str, secret: str) -> str:
    claims = {
        "sub": user_id,
        "email": email,
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# providers


def test_plan_prices() -> None:
    assert PLAN_PRICES_KRW == {"pilot": 99000, "org": 490000}


def test_fake_checkout_url() -> None:
    provider = FakePaymentProvider(BASE + "/")
    session = provider.checkout(_invoice(), f"{BASE}/billing?paid=inv-1", f"{BASE}/billing?x")
    assert (session.provider, session.order_id, session.amount_krw) == ("fake", "sl-inv-1", 99000)
    assert session.client_key is None and session.checkout_url is not None
    url = urlsplit(session.checkout_url)
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{BASE}/billing/pay-test"
    assert parse_qs(url.query) == {
        "orderId": ["sl-inv-1"],
        "amount": ["99000"],
        "successUrl": [f"{BASE}/billing?paid=inv-1"],
    }


def test_fake_confirm_and_lookup() -> None:
    provider = FakePaymentProvider(BASE)
    result = provider.confirm("fake_sl-inv-1", "sl-inv-1", 99000)
    assert (result.status, result.order_id, result.amount_krw) == ("paid", "sl-inv-1", 99000)
    assert provider.lookup("fake_sl-inv-1").amount_krw == 99000
    with pytest.raises(PaymentError) as err:
        provider.confirm("fake_sl-other", "sl-inv-1", 99000)
    assert err.value.code == "invalid_key"
    with pytest.raises(PaymentError) as err:
        provider.confirm("real_sl-inv-1", "sl-inv-1", 99000)
    assert err.value.code == "invalid_key"
    with pytest.raises(PaymentError) as err:
        provider.lookup("nope")
    assert err.value.code == "not_found"


def _toss(handler: Callable[[httpx.Request], httpx.Response]) -> TossPaymentProvider:
    return TossPaymentProvider(
        "test_ck_x",
        "test_sk_x",
        "https://toss.example.test/",
        transport=httpx.MockTransport(handler),
    )


def test_toss_confirm_done() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "paymentKey": "pk_1",
                "orderId": "sl-inv-1",
                "status": "DONE",
                "totalAmount": 99000,
                "approvedAt": "2026-11-03T10:00:00+09:00",
            },
        )

    result = _toss(handler).confirm("pk_1", "sl-inv-1", 99000)
    assert result.status == "paid" and result.amount_krw == 99000
    assert result.approved_at == datetime(2026, 11, 3, 1, 0, tzinfo=UTC)
    (request,) = seen
    assert (request.method, str(request.url)) == (
        "POST",
        "https://toss.example.test/v1/payments/confirm",
    )
    expected = base64.b64encode(b"test_sk_x:").decode()
    assert request.headers["authorization"] == f"Basic {expected}"
    assert json.loads(request.content) == {
        "paymentKey": "pk_1",
        "orderId": "sl-inv-1",
        "amount": 99000,
    }


def test_toss_confirm_error_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": "REJECT_CARD_PAYMENT", "message": "한도 초과"})

    with pytest.raises(PaymentError) as err:
        _toss(handler).confirm("pk_1", "sl-inv-1", 99000)
    assert err.value.code == "REJECT_CARD_PAYMENT"


def test_toss_lookup_status_and_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        key = request.url.path.rsplit("/", 1)[-1]
        if key == "missing":
            return httpx.Response(404, json={"code": "NOT_FOUND_PAYMENT", "message": "없음"})
        if key == "broken":
            return httpx.Response(500, text="oops")
        status = {"pk_wait": "IN_PROGRESS", "pk_cancel": "CANCELED"}[key]
        return httpx.Response(
            200,
            json={"paymentKey": key, "orderId": "o", "status": status, "totalAmount": 1},
        )

    toss = _toss(handler)
    assert toss.lookup("pk_wait").status == "pending"
    assert toss.lookup("pk_cancel").status == "failed"
    assert toss.lookup("pk_wait").approved_at is None
    with pytest.raises(PaymentError) as err:
        toss.lookup("missing")
    assert err.value.code == "NOT_FOUND_PAYMENT"
    with pytest.raises(PaymentError) as err:
        toss.lookup("broken")
    assert err.value.code == "http_500"


def test_toss_checkout_and_build(tmp_path: Path) -> None:
    settings = Settings(
        provider="fake",
        stt_provider="fake",
        database_url="",
        data_dir=tmp_path,
        billing_provider="toss",
        toss_client_key="test_ck_x",
        toss_secret_key="test_sk_x",
    )
    provider = build_payment_provider(settings)
    assert isinstance(provider, TossPaymentProvider)
    session = provider.checkout(_invoice(), "s", "f")
    assert (session.provider, session.client_key, session.checkout_url) == (
        "toss",
        "test_ck_x",
        None,
    )
    fake = build_payment_provider(
        Settings(provider="fake", stt_provider="fake", database_url="", data_dir=tmp_path)
    )
    assert isinstance(fake, FakePaymentProvider)


def _charge(billing_key: str = "fake_bk_sl-org") -> BillingCharge:
    return BillingCharge(
        billing_key=billing_key,
        customer_key="sl-org",
        order_id="sl-inv-7",
        amount_krw=99000,
        order_name="가게유산 pilot",
    )


def test_fake_issue_billing_key() -> None:
    provider = FakePaymentProvider(BASE)
    issued = provider.issue_billing_key("fake_auth_1", "sl-org")
    assert (issued.billing_key, issued.card_label) == ("fake_bk_sl-org", "테스트카드 ****1234")
    with pytest.raises(PaymentError) as err:
        provider.issue_billing_key("stub_auth_1", "sl-org")
    assert err.value.code == "invalid_auth_key"


def test_fake_charge_billing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHOPLEGACY_FAKE_DECLINE_BILLING", raising=False)
    provider = FakePaymentProvider(BASE)
    result = provider.charge_billing_key(_charge())
    assert (result.payment_key, result.order_id, result.amount_krw, result.status) == (
        "fake_sl-inv-7",
        "sl-inv-7",
        99000,
        "paid",
    )
    assert result.approved_at is not None
    assert provider.lookup("fake_sl-inv-7").amount_krw == 99000
    with pytest.raises(PaymentError) as err:
        provider.charge_billing_key(_charge("bk_real"))
    assert err.value.code == "invalid_billing_key"


def test_fake_charge_declines_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOPLEGACY_FAKE_DECLINE_BILLING", "1")
    with pytest.raises(PaymentError) as err:
        FakePaymentProvider(BASE).charge_billing_key(_charge())
    assert err.value.code == "DECLINED"
    monkeypatch.setenv("SHOPLEGACY_FAKE_DECLINE_BILLING", "0")
    assert FakePaymentProvider(BASE).charge_billing_key(_charge()).status == "paid"


def test_fake_cancel() -> None:
    provider = FakePaymentProvider(BASE)
    refund = provider.cancel("fake_sl-inv-1", "고객 요청")
    assert (refund.payment_key, refund.status) == ("fake_sl-inv-1", "refunded")
    assert refund.refunded_at is not None
    with pytest.raises(PaymentError) as err:
        provider.cancel("pk_real", "x")
    assert err.value.code == "not_found"


def test_toss_issue_billing_key_request_and_card_label() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "billingKey": "bk_1",
                "customerKey": "sl-org",
                "card": {"company": "테스트카드", "number": "1234****5678"},
            },
        )

    issued = _toss(handler).issue_billing_key("auth_1", "sl-org")
    assert (issued.billing_key, issued.card_label) == ("bk_1", "테스트카드 ****5678")
    (request,) = seen
    assert (request.method, str(request.url)) == (
        "POST",
        "https://toss.example.test/v1/billing/authorizations/issue",
    )
    assert json.loads(request.content) == {"authKey": "auth_1", "customerKey": "sl-org"}


def test_toss_issue_billing_key_errors_and_label_fallbacks() -> None:
    bodies: list[httpx.Response] = [
        httpx.Response(400, json={"code": "NOT_FOUND_BILLING_AUTH", "message": "없음"}),
        httpx.Response(200, json={"customerKey": "sl-org"}),
        httpx.Response(200, json={"billingKey": "bk_2", "cardCompany": "국민"}),
        httpx.Response(200, json={"billingKey": "bk_3", "card": {"number": "4330****1111"}}),
        httpx.Response(200, json={"billingKey": "bk_4"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return bodies.pop(0)

    toss = _toss(handler)
    with pytest.raises(PaymentError) as err:
        toss.issue_billing_key("auth_x", "sl-org")
    assert err.value.code == "NOT_FOUND_BILLING_AUTH"
    with pytest.raises(PaymentError) as err:
        toss.issue_billing_key("auth_x", "sl-org")
    assert err.value.code == "invalid_response"
    assert toss.issue_billing_key("a", "sl-org").card_label == "국민"
    assert toss.issue_billing_key("a", "sl-org").card_label == "****1111"
    assert toss.issue_billing_key("a", "sl-org").card_label is None


def test_toss_charge_billing_key_request_and_mapping() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("declined"):
            return httpx.Response(400, json={"code": "REJECT_CARD_PAYMENT", "message": "거절"})
        return httpx.Response(
            200,
            json={
                "paymentKey": "pay_7",
                "orderId": "sl-inv-7",
                "status": "DONE",
                "totalAmount": 99000,
                "approvedAt": "2026-11-03T12:00:00+09:00",
            },
        )

    toss = _toss(handler)
    result = toss.charge_billing_key(_charge("bk/1"))
    assert (result.payment_key, result.status, result.amount_krw) == ("pay_7", "paid", 99000)
    assert result.approved_at == datetime(2026, 11, 3, 3, 0, tzinfo=UTC)
    request = seen[0]
    assert request.method == "POST"
    assert request.url.raw_path == b"/v1/billing/bk%2F1"
    assert json.loads(request.content) == {
        "customerKey": "sl-org",
        "amount": 99000,
        "orderId": "sl-inv-7",
        "orderName": "가게유산 pilot",
    }
    with pytest.raises(PaymentError) as err:
        toss.charge_billing_key(_charge("declined"))
    assert err.value.code == "REJECT_CARD_PAYMENT"


def test_toss_cancel_request_and_mapping() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = request.url.path.split("/")[-2]
        if key == "missing":
            return httpx.Response(404, json={"code": "NOT_FOUND_PAYMENT", "message": "없음"})
        if key == "pk_done":
            return httpx.Response(200, json={"paymentKey": key, "status": "DONE"})
        if key == "pk_nocancels":
            return httpx.Response(200, json={"paymentKey": key, "status": "CANCELED"})
        if key == "pk_partial":
            return httpx.Response(
                200,
                json={
                    "paymentKey": key,
                    "status": "PARTIAL_CANCELED",
                    "cancels": [{"cancelReason": "x", "canceledAt": "2026-11-03T09:00:00+09:00"}],
                },
            )
        return httpx.Response(
            200,
            json={
                "paymentKey": key,
                "status": "CANCELED",
                "cancels": [
                    {"cancelReason": "old", "canceledAt": "2026-11-01T09:00:00+09:00"},
                    {"cancelReason": "환불", "canceledAt": "2026-11-03T09:00:00+09:00"},
                ],
            },
        )

    toss = _toss(handler)
    refund = toss.cancel("pk_1", "환불")
    assert (refund.payment_key, refund.status) == ("pk_1", "refunded")
    assert refund.refunded_at == datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    assert (seen[0].method, str(seen[0].url)) == (
        "POST",
        "https://toss.example.test/v1/payments/pk_1/cancel",
    )
    assert json.loads(seen[0].content) == {"cancelReason": "환불"}
    no_cancels = toss.cancel("pk_nocancels", "x")
    assert no_cancels.status == "refunded" and no_cancels.refunded_at is not None
    # only full refunds are supported, so a partial cancel is never a refund
    partial = toss.cancel("pk_partial", "x")
    assert (partial.status, partial.refunded_at) == ("failed", None)
    failed = toss.cancel("pk_done", "x")
    assert (failed.status, failed.refunded_at) == ("failed", None)
    with pytest.raises(PaymentError) as err:
        toss.cancel("missing", "x")
    assert err.value.code == "NOT_FOUND_PAYMENT"


def test_first_charge_on() -> None:
    assert first_charge_on(date(2026, 11, 3), 5) == date(2026, 11, 5)
    assert first_charge_on(date(2026, 11, 5), 5) == date(2026, 11, 5)
    assert first_charge_on(date(2026, 11, 6), 5) == date(2026, 12, 5)
    assert first_charge_on(date(2026, 12, 29), 1) == date(2027, 1, 1)
    assert first_charge_on(date(2027, 1, 31), 28) == date(2027, 2, 28)


# routes


@pytest.fixture
def client(app_dsn: str, tmp_path: Path, jwt_secret: str) -> Iterator[TestClient]:
    settings = Settings(
        provider="fake",
        stt_provider="fake",
        data_dir=tmp_path,
        database_url=app_dsn,
        jwt_secret=jwt_secret,
        public_base_url=BASE,
    )
    store = Store(app_dsn)
    app = FastAPI()
    app.state.shop = SimpleNamespace(
        settings=settings, store=store, payments=FakePaymentProvider(BASE)
    )
    app.include_router(routes_payments.router, prefix="/api")
    try:
        yield TestClient(app)
    finally:
        store.close()


def _plan(admin_conn: psycopg.Connection, org_id: str) -> str:
    row = admin_conn.execute("SELECT plan FROM orgs WHERE org_id = %s", (org_id,)).fetchone()
    assert row is not None
    return str(row[0])


def _checkout(client: TestClient, org_id: str, invoice_id: str, token: str) -> httpx.Response:
    return client.post(f"/api/orgs/{org_id}/invoices/{invoice_id}/checkout", headers=_auth(token))


def test_list_invoices_for_members(
    client: TestClient, org_fixture: OrgTokens, issued_invoice: InvoiceRow
) -> None:
    org_id, _, _, interviewer = org_fixture
    r = client.get(f"/api/orgs/{org_id}/invoices", headers=_auth(interviewer))
    assert r.status_code == 200
    (row,) = r.json()
    assert row["invoice_id"] == issued_invoice.invoice_id
    assert (row["plan"], row["amount_krw"], row["status"]) == ("pilot", 99000, "issued")
    assert (row["period_start"], row["period_end"]) == ("2026-11-01", "2026-12-01")
    assert row["order_id"] == f"sl-{issued_invoice.invoice_id}"


def test_checkout_owner_gets_fake_session(
    client: TestClient, org_fixture: OrgTokens, issued_invoice: InvoiceRow
) -> None:
    org_id, owner, _, _ = org_fixture
    r = _checkout(client, org_id, issued_invoice.invoice_id, owner)
    assert r.status_code == 201
    body = r.json()
    assert body["provider"] == "fake"
    assert body["order_id"] == issued_invoice.order_id
    assert body["success_url"] == f"{BASE}/billing?paid={issued_invoice.invoice_id}"
    assert body["fail_url"] == f"{BASE}/billing?failed={issued_invoice.invoice_id}"
    assert body["checkout_url"].startswith(f"{BASE}/billing/pay-test?orderId=")


def test_checkout_admin_and_reviewer_forbidden(
    client: TestClient,
    org_fixture: OrgTokens,
    issued_invoice: InvoiceRow,
    pg_dsn: str,
    admin_conn: psycopg.Connection,
    make_user: Callable[[str, str], str],
    jwt_secret: str,
) -> None:
    org_id, _, reviewer, _ = org_fixture
    admin_id = make_user(pg_dsn, "admin@example.test")
    admin_conn.execute(
        "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'admin')",
        (org_id, admin_id),
    )
    admin = _token(admin_id, "admin@example.test", jwt_secret)
    for token in (admin, reviewer):
        r = _checkout(client, org_id, issued_invoice.invoice_id, token)
        assert (r.status_code, r.json()) == (403, {"detail": "forbidden"})


def test_checkout_other_org_invoice_is_404(
    client: TestClient,
    org_fixture: OrgTokens,
    app_dsn: str,
    pg_dsn: str,
    admin_conn: psycopg.Connection,
    make_user: Callable[[str, str], str],
) -> None:
    org_id, owner, _, _ = org_fixture
    other_org = str(uuid.uuid4())
    other_user = make_user(pg_dsn, "other@example.test")
    admin_conn.execute(
        "INSERT INTO orgs (org_id, name, created_by) VALUES (%s, 'other', %s)",
        (other_org, other_user),
    )
    store = Store(app_dsn)
    try:
        other = store.issue_invoice(
            other_org, "org", date(2026, 11, 1), date(2026, 12, 1), 490000, None
        )
    finally:
        store.close()
    r = _checkout(client, org_id, other.invoice_id, owner)
    assert r.status_code == 404
    r = _checkout(client, org_id, "no-such-invoice", owner)
    assert r.status_code == 404
    r = client.get(f"/api/orgs/{org_id}/invoices", headers=_auth(owner))
    assert r.json() == []


def test_checkout_void_invoice_is_409(
    client: TestClient, org_fixture: OrgTokens, issued_invoice: InvoiceRow, app_dsn: str
) -> None:
    store = Store(app_dsn)
    try:
        store.void_invoice(issued_invoice.invoice_id)
    finally:
        store.close()
    r = _checkout(client, org_fixture[0], issued_invoice.invoice_id, org_fixture[1])
    assert (r.status_code, r.json()) == (409, {"detail": "invoice_not_payable"})


def _confirm(
    client: TestClient, token: str, invoice: InvoiceRow, key: str | None = None, amount: int = 0
) -> httpx.Response:
    return client.post(
        "/api/billing/confirm",
        json={
            "payment_key": key if key is not None else f"fake_{invoice.order_id}",
            "order_id": invoice.order_id,
            "amount_krw": amount or invoice.amount_krw,
        },
        headers=_auth(token),
    )


def test_confirm_marks_paid_and_upgrades_plan(
    client: TestClient,
    org_fixture: OrgTokens,
    issued_invoice: InvoiceRow,
    admin_conn: psycopg.Connection,
) -> None:
    org_id, owner, _, _ = org_fixture
    assert _plan(admin_conn, org_id) == "free"
    r = _confirm(client, owner, issued_invoice)
    assert r.status_code == 200
    body = r.json()
    assert (body["status"], body["method"], body["provider"]) == ("paid", "card", "fake")
    assert body["paid_at"] is not None
    assert _plan(admin_conn, org_id) == "pilot"
    again = _confirm(client, owner, issued_invoice)
    assert (again.status_code, again.json()) == (200, body)
    actions = admin_conn.execute(
        "SELECT action FROM audit_log WHERE org_id = %s AND action IN ('invoice.paid', 'org.plan')",
        (org_id,),
    ).fetchall()
    assert sorted(a[0] for a in actions) == ["invoice.paid", "org.plan"]


def test_confirm_failures(
    client: TestClient,
    org_fixture: OrgTokens,
    issued_invoice: InvoiceRow,
    admin_conn: psycopg.Connection,
) -> None:
    org_id, owner, reviewer, _ = org_fixture
    r = _confirm(client, owner, issued_invoice, key="fake_sl-wrong")
    assert (r.status_code, r.json()) == (402, {"detail": "payment_failed:invalid_key"})
    r = _confirm(client, owner, issued_invoice, amount=1000)
    assert (r.status_code, r.json()) == (409, {"detail": "amount_mismatch"})
    r = _confirm(client, reviewer, issued_invoice)
    assert (r.status_code, r.json()) == (403, {"detail": "forbidden"})
    r = client.post(
        "/api/billing/confirm",
        json={"payment_key": "fake_sl-x", "order_id": "sl-x", "amount_krw": 1},
        headers=_auth(owner),
    )
    assert r.status_code == 404
    assert _plan(admin_conn, org_id) == "free"


def _webhook(client: TestClient, invoice: InvoiceRow, key: str | None = None) -> httpx.Response:
    return client.post(
        "/api/billing/webhook",
        json={
            "eventType": "PAYMENT_STATUS_CHANGED",
            "data": {
                "paymentKey": key or f"fake_{invoice.order_id}",
                "orderId": invoice.order_id,
                "status": "DONE",
            },
        },
    )


def test_webhook_is_idempotent(
    client: TestClient,
    org_fixture: OrgTokens,
    issued_invoice: InvoiceRow,
    admin_conn: psycopg.Connection,
) -> None:
    org_id, owner, _, _ = org_fixture
    assert _checkout(client, org_id, issued_invoice.invoice_id, owner).status_code == 201
    for _ in range(2):
        r = _webhook(client, issued_invoice)
        assert (r.status_code, r.json()) == (200, {"ok": True})
    assert _plan(admin_conn, org_id) == "pilot"
    paid = admin_conn.execute(
        "SELECT count(*) FROM audit_log WHERE org_id = %s AND action = 'invoice.paid'", (org_id,)
    ).fetchone()
    assert paid is not None and paid[0] == 1
    # confirm after the webhook already paid it returns the paid invoice
    r = _confirm(client, owner, issued_invoice)
    assert (r.status_code, r.json()["status"]) == (200, "paid")


def test_webhook_rejects_unknown_and_unverified(
    client: TestClient, issued_invoice: InvoiceRow, admin_conn: psycopg.Connection
) -> None:
    r = client.post(
        "/api/billing/webhook",
        json={"eventType": "X", "data": {"paymentKey": "fake_sl-x", "orderId": "sl-x"}},
    )
    assert r.status_code == 404
    r = _webhook(client, issued_invoice, key="bogus")
    assert (r.status_code, r.json()) == (200, {"ok": False, "reason": "lookup_failed:not_found"})
    # no checkout in this process, so the fake provider cannot vouch for the amount
    r = _webhook(client, issued_invoice)
    assert (r.status_code, r.json()) == (200, {"ok": False, "reason": "amount_mismatch"})
    assert _plan(admin_conn, issued_invoice.org_id) == "free"


# billing-key routes


def _admin_token(
    org_id: str,
    pg_dsn: str,
    admin_conn: psycopg.Connection,
    make_user: Callable[[str, str], str],
    jwt_secret: str,
) -> str:
    admin_id = make_user(pg_dsn, "admin@example.test")
    admin_conn.execute(
        "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'admin')",
        (org_id, admin_id),
    )
    return _token(admin_id, "admin@example.test", jwt_secret)


def _register(
    client: TestClient, org_id: str, token: str, **changes: object
) -> httpx.Response:
    body: dict[str, object] = {
        "auth_key": "fake_auth_1",
        "customer_key": f"sl-{org_id.lower()}",
        "plan": "pilot",
        "charge_day": 1,
    }
    body.update(changes)
    return client.post(f"/api/orgs/{org_id}/billing-key", json=body, headers=_auth(token))


def test_billing_key_auth_fake_session(client: TestClient, org_fixture: OrgTokens) -> None:
    org_id, owner, _, _ = org_fixture
    r = client.post(f"/api/orgs/{org_id}/billing-key/auth", headers=_auth(owner))
    assert r.status_code == 201
    body = r.json()
    customer_key = f"sl-{org_id.lower()}"
    assert (body["provider"], body["client_key"], body["customer_key"]) == (
        "fake",
        None,
        customer_key,
    )
    assert body["success_url"] == f"{BASE}/billing?billing_auth=1"
    assert body["fail_url"] == f"{BASE}/billing?billing_failed=1"
    url = urlsplit(body["auth_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{BASE}/billing/pay-test"
    assert parse_qs(url.query) == {
        "mode": ["billing"],
        "customerKey": [customer_key],
        "successUrl": [f"{BASE}/billing?billing_auth=1"],
    }


def test_billing_key_auth_toss_session(client: TestClient, org_fixture: OrgTokens) -> None:
    org_id, owner, _, _ = org_fixture
    shop = client.app.state.shop  # type: ignore[attr-defined]
    shop.settings = dataclasses.replace(shop.settings, toss_client_key="test_ck_x")
    shop.payments = _toss(lambda request: httpx.Response(500))
    r = client.post(f"/api/orgs/{org_id}/billing-key/auth", headers=_auth(owner))
    assert r.status_code == 201
    body = r.json()
    assert (body["provider"], body["client_key"], body["auth_url"]) == ("toss", "test_ck_x", None)
    assert body["customer_key"] == f"sl-{org_id.lower()}"


def test_billing_key_owner_only(
    client: TestClient,
    org_fixture: OrgTokens,
    pg_dsn: str,
    admin_conn: psycopg.Connection,
    make_user: Callable[[str, str], str],
    jwt_secret: str,
) -> None:
    org_id, _, reviewer, _ = org_fixture
    admin = _admin_token(org_id, pg_dsn, admin_conn, make_user, jwt_secret)
    for token in (admin, reviewer):
        r = client.post(f"/api/orgs/{org_id}/billing-key/auth", headers=_auth(token))
        assert (r.status_code, r.json()) == (403, {"detail": "forbidden"})
        r = _register(client, org_id, token)
        assert (r.status_code, r.json()) == (403, {"detail": "forbidden"})
        r = client.delete(f"/api/orgs/{org_id}/billing-key", headers=_auth(token))
        assert (r.status_code, r.json()) == (403, {"detail": "forbidden"})
    r = client.get(f"/api/orgs/{org_id}/billing-key", headers=_auth(admin))
    assert (r.status_code, r.json()) == (404, {"detail": "billing key not set"})


def test_billing_key_register_get_stop(
    client: TestClient, org_fixture: OrgTokens, admin_conn: psycopg.Connection
) -> None:
    org_id, owner, _, interviewer = org_fixture
    r = _register(client, org_id, owner, plan="org", charge_day=28)
    assert r.status_code == 201
    body = r.json()
    assert "billing_key" not in body and "customer_key" not in body
    assert (body["org_id"], body["provider"], body["plan"], body["charge_day"]) == (
        org_id.lower(),
        "fake",
        "org",
        28,
    )
    assert (body["status"], body["failures"]) == ("active", 0)
    assert body["card_label"] == "테스트카드 ****1234"
    today = datetime.now(UTC).date()
    assert body["next_charge_on"] == first_charge_on(today, 28).isoformat()
    stored = admin_conn.execute(
        "SELECT billing_key FROM billing_keys WHERE org_id = %s", (org_id,)
    ).fetchone()
    # this Store has no cipher; encryption at rest is covered by the store tests
    assert stored is not None and stored[0] == f"fake_bk_sl-{org_id.lower()}"

    r = client.get(f"/api/orgs/{org_id}/billing-key", headers=_auth(interviewer))
    assert r.status_code == 200
    assert "billing_key" not in r.json() and "fake_bk_" not in r.text
    assert r.json()["status"] == "active"

    r = client.delete(f"/api/orgs/{org_id}/billing-key", headers=_auth(owner))
    assert (r.status_code, r.content) == (204, b"")
    r = client.get(f"/api/orgs/{org_id}/billing-key", headers=_auth(owner))
    assert (r.status_code, r.json()["status"]) == (200, "stopped")


def test_billing_key_register_failures(client: TestClient, org_fixture: OrgTokens) -> None:
    org_id, owner, _, _ = org_fixture
    r = _register(client, org_id, owner, customer_key="sl-someone-else")
    assert (r.status_code, r.json()) == (422, {"detail": "customer_key_mismatch"})
    r = _register(client, org_id, owner, auth_key="bad")
    assert (r.status_code, r.json()) == (402, {"detail": "billing_auth_failed:invalid_auth_key"})
    r = _register(client, org_id, owner, charge_day=29)
    assert r.status_code == 422
    r = _register(client, org_id, owner, plan="free")
    assert r.status_code == 422
    r = client.get(f"/api/orgs/{org_id}/billing-key", headers=_auth(owner))
    assert (r.status_code, r.json()) == (404, {"detail": "billing key not set"})
    r = client.delete(f"/api/orgs/{org_id}/billing-key", headers=_auth(owner))
    assert (r.status_code, r.json()) == (404, {"detail": "billing key not set"})


def test_invoice_out_has_refund_and_tax_fields(
    client: TestClient, org_fixture: OrgTokens, issued_invoice: InvoiceRow
) -> None:
    org_id, owner, _, _ = org_fixture
    (row,) = client.get(f"/api/orgs/{org_id}/invoices", headers=_auth(owner)).json()
    for key in ("refunded_at", "refund_reason", "tax_invoice_ref", "tax_invoice_issued_at"):
        assert row[key] is None


# operator CLI


def test_cli_issue_mark_paid_void(
    org_fixture: OrgTokens,
    pg_dsn: str,
    admin_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    org_id = org_fixture[0]
    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", pg_dsn)

    assert maintenance.main(["issue-invoice", org_id, "org", "2026-12", "--memo", "연간"]) == 0
    first = capsys.readouterr().out.strip()
    row = admin_conn.execute(
        "SELECT plan, period_start, period_end, amount_krw, status, memo "
        "FROM invoices WHERE invoice_id = %s",
        (first,),
    ).fetchone()
    assert row == ("org", date(2026, 12, 1), date(2027, 1, 1), 490000, "issued", "연간")

    assert maintenance.main(["mark-paid", first, "--method", "contract", "--memo", "계약"]) == 0
    assert capsys.readouterr().out.strip() == f"{first} paid"
    row = admin_conn.execute(
        "SELECT status, method, memo FROM invoices WHERE invoice_id = %s", (first,)
    ).fetchone()
    assert row == ("paid", "contract", "계약")
    assert _plan(admin_conn, org_id) == "org"

    assert maintenance.main(["issue-invoice", org_id, "pilot", "2027-01", "--amount", "5"]) == 0
    second = capsys.readouterr().out.strip()
    assert maintenance.main(["void-invoice", second]) == 0
    assert capsys.readouterr().out.strip() == f"{second} void"
    row = admin_conn.execute(
        "SELECT status, amount_krw FROM invoices WHERE invoice_id = %s", (second,)
    ).fetchone()
    assert row == ("void", 5)

    with pytest.raises(SystemExit):
        maintenance.main(["void-invoice", first])
    with pytest.raises(SystemExit):
        maintenance.main(["mark-paid", second, "--method", "card"])
    with pytest.raises(SystemExit):
        maintenance.main(["mark-paid", "no-such", "--method", "card"])
    with pytest.raises(SystemExit):
        maintenance.main(["issue-invoice", org_id, "pilot", "2027-13"])


def test_cli_invoice_commands_call_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[object, ...]] = []

    class FakeStore:
        def __init__(self, dsn: str, **_: object) -> None:
            calls.append(("init", dsn))

        def issue_invoice(self, *args: object) -> SimpleNamespace:
            calls.append(("issue", *args))
            return SimpleNamespace(invoice_id="inv-9")

        def mark_invoice_paid(self, *args: object) -> None:
            calls.append(("paid", *args[:4]))

        def void_invoice(self, invoice_id: str) -> None:
            calls.append(("void", invoice_id))

        def close(self) -> None:
            calls.append(("close",))

    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(maintenance, "Store", FakeStore)
    assert maintenance.main(["issue-invoice", "ORG-1", "pilot", "2026-02"]) == 0
    assert maintenance.main(["mark-paid", "inv-9", "--method", "card"]) == 0
    assert maintenance.main(["void-invoice", "inv-9"]) == 0
    assert capsys.readouterr().out.splitlines() == ["inv-9", "inv-9 paid", "inv-9 void"]
    assert [c for c in calls if c[0] not in ("init", "close")] == [
        ("issue", "org-1", "pilot", date(2026, 2, 1), date(2026, 3, 1), 99000, None),
        ("paid", "inv-9", "card", "manual", None),
        ("void", "inv-9"),
    ]


def test_cli_logs_go_to_stderr_not_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeStore:
        def __init__(self, dsn: str, **_: object) -> None:
            pass

        def void_invoice(self, invoice_id: str) -> None:
            structlog.get_logger("shoplegacy").warning("pii_encryption_disabled")

        def close(self) -> None:
            pass

    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(maintenance, "Store", FakeStore)
    assert maintenance.main(["void-invoice", "inv-9"]) == 0
    out, err = capsys.readouterr()
    assert out == "inv-9 void\n"
    assert "pii_encryption_disabled" in err
