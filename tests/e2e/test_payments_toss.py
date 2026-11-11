"""E2E: Toss widget checkout, card registration and automatic charges against a local stub."""

import os
import re
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import _start_server
from tests.e2e.test_payments import cli
from tests.e2e.toss_stub import STUB_CLIENT, STUB_SECRET

Maintenance = Callable[[list[str]], str]
Axe = Callable[[Page], list[dict[str, Any]]]


# Screens fade and slide in (GSAP); axe must judge the settled screen, not a frame mid-fade.
_SETTLED_JS = (
    "() => [document.querySelector('main'), ...document.querySelectorAll('[data-stagger]')]"
    ".every((e) => !e || getComputedStyle(e).opacity === '1')"
)


def _settled(pg: Page) -> Page:
    pg.wait_for_function(_SETTLED_JS)
    return pg


SDK_URL = "https://js.tosspayments.com/v2/standard"
STUB_SDK = """
window.TossPayments = function (clientKey) {
  return {
    widgets: function () {
      var amount = 0;
      return {
        setAmount: function (a) { amount = a.value; return Promise.resolve(); },
        renderPaymentMethods: function (o) {
          document.querySelector(o.selector).innerHTML = "<div data-stub='widget'>카드</div>";
          return Promise.resolve();
        },
        renderAgreement: function () { return Promise.resolve(); },
        requestPayment: function (o) {
          location.href = o.successUrl + "&paymentKey=stub_" + o.orderId +
            "&orderId=" + o.orderId + "&amount=" + amount;
          return new Promise(function () {});
        }
      };
    },
    payment: function (o) {
      return {
        requestBillingAuth: function (b) {
          location.href = b.successUrl + "&customerKey=" + o.customerKey +
            "&authKey=stub_auth_" + o.customerKey;
          return new Promise(function () {});
        }
      };
    }
  };
};
"""


def _stub_sdk(page: Page) -> None:
    page.route(
        SDK_URL,
        lambda route: route.fulfill(
            status=200, content_type="application/javascript", body=STUB_SDK
        ),
    )


def _csp_errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on(
        "console",
        lambda msg: errors.append(msg.text) if "Content Security Policy" in msg.text else None,
    )
    return errors


def _toss_env(toss_stub: str) -> dict[str, str]:
    return {
        "SHOPLEGACY_BILLING_PROVIDER": "toss",
        "SHOPLEGACY_TOSS_CLIENT_KEY": STUB_CLIENT,
        "SHOPLEGACY_TOSS_SECRET_KEY": STUB_SECRET,
        "SHOPLEGACY_TOSS_API_BASE": toss_stub,
    }


def _issue(maintenance: Maintenance, org_id: str) -> str:
    return maintenance(["issue-invoice", org_id, "pilot", "2026-11"]).strip().splitlines()[-1]


def _invoices(base: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    res = httpx.get(f"{base}/api/orgs/{headers['X-Org-Id']}/invoices", timeout=10, headers=headers)
    assert res.status_code == 200, res.text
    return list(res.json())


def _register_key(base: str, headers: dict[str, str]) -> dict[str, Any]:
    org_id = headers["X-Org-Id"]
    auth = httpx.post(f"{base}/api/orgs/{org_id}/billing-key/auth", timeout=10, headers=headers)
    assert auth.status_code == 201, auth.text
    customer_key = auth.json()["customer_key"]
    res = httpx.post(
        f"{base}/api/orgs/{org_id}/billing-key",
        json={
            "auth_key": f"stub_auth_{customer_key}",
            "customer_key": customer_key,
            "plan": "pilot",
            "charge_day": 1,
        },
        timeout=10,
        headers=headers,
    )
    assert res.status_code == 201, res.text
    body: dict[str, Any] = res.json()
    return body


def _audit_count(conn: psycopg.Connection, org_id: str, action: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM public.audit_log WHERE org_id = %s AND action = %s",
        (org_id, action),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_widget_checkout_pays_invoice_without_csp_errors(
    server_url_toss: str,
    owner_headers: dict[str, str],
    maintenance: Maintenance,
    owner_page: Page,
    shot_dir: Path,
) -> None:
    invoice_id = _issue(maintenance, owner_headers["X-Org-Id"])
    _stub_sdk(owner_page)
    csp = _csp_errors(owner_page)
    owner_page.goto(f"{server_url_toss}/billing")
    row = owner_page.locator(f'#invoice-table tbody tr[data-invoice-id="{invoice_id}"]')
    expect(row.locator(".invoice-status")).to_have_text("미결제")
    row.locator("button.invoice-pay").click()

    expect(owner_page.locator("#payment-method [data-stub='widget']")).to_be_visible()
    expect(owner_page.locator("#widget-pay")).to_have_text("결제하기")
    expect(owner_page.locator("#widget-pay")).to_be_enabled()
    owner_page.screenshot(path=str(shot_dir / "billing-widget.png"), full_page=True)

    owner_page.click("#widget-pay")
    owner_page.wait_for_url(re.compile(r"/billing\?.*paymentKey=stub_"))
    expect(owner_page.locator("#billing-message")).to_contain_text("결제가 완료됐어요")
    row = owner_page.locator(f'#invoice-table tbody tr[data-invoice-id="{invoice_id}"]')
    expect(row.locator(".invoice-status")).to_have_text("결제 완료")
    owner_page.screenshot(path=str(shot_dir / "billing-paid.png"), full_page=True)
    assert csp == []
    assert _invoices(server_url_toss, owner_headers)[0]["method"] == "card"


def test_widget_confirm_with_wrong_amount_leaves_invoice_unpaid(
    server_url_toss: str, owner_headers: dict[str, str], maintenance: Maintenance
) -> None:
    invoice_id = _issue(maintenance, owner_headers["X-Org-Id"])
    session = httpx.post(
        f"{server_url_toss}/api/orgs/{owner_headers['X-Org-Id']}/invoices/{invoice_id}/checkout",
        timeout=10,
        headers=owner_headers,
    )
    assert session.status_code == 201, session.text
    body = session.json()
    assert body["client_key"] == STUB_CLIENT
    res = httpx.post(
        f"{server_url_toss}/api/billing/confirm",
        json={"payment_key": "stub_x", "order_id": body["order_id"], "amount_krw": 1},
        timeout=10,
        headers=owner_headers,
    )
    assert res.status_code == 409
    assert _invoices(server_url_toss, owner_headers)[0]["status"] == "issued"


def test_payment_script_sources_are_allowed_only_on_billing_pages(server_url_toss: str) -> None:
    billing = httpx.get(f"{server_url_toss}/billing", timeout=10)
    assert "https://js.tosspayments.com" in billing.headers["content-security-policy"]
    for path in ("/", "/cards"):
        res = httpx.get(f"{server_url_toss}{path}", timeout=10)
        assert "tosspayments" not in res.headers["content-security-policy"]


def test_autopay_card_registration_through_widget_sdk(
    server_url_toss: str,
    owner_headers: dict[str, str],
    owner_page: Page,
    shot_dir: Path,
) -> None:
    _stub_sdk(owner_page)
    csp = _csp_errors(owner_page)
    owner_page.goto(f"{server_url_toss}/billing")
    expect(owner_page.locator("#autopay-register")).to_be_enabled()
    owner_page.select_option("#autopay-plan", "org")
    owner_page.click("#autopay-register")
    owner_page.wait_for_url(re.compile(r"/billing\?billing_auth=1"))
    expect(owner_page.locator("#autopay-status")).to_have_text("사용 중")
    expect(owner_page.locator("#autopay-card")).to_contain_text("5678")
    expect(owner_page.locator("#autopay-next")).to_have_text(re.compile(r"\d{4}-\d{2}-\d{2}"))
    owner_page.screenshot(path=str(shot_dir / "autopay-registered.png"), full_page=True)
    assert csp == []

    res = httpx.get(
        f"{server_url_toss}/api/orgs/{owner_headers['X-Org-Id']}/billing-key",
        timeout=10,
        headers=owner_headers,
    )
    assert res.json()["plan"] == "org"


def test_billing_key_routes_reject_non_owner_and_hide_the_key(
    server_url_toss: str, owner_headers: dict[str, str], auth_headers: dict[str, str]
) -> None:
    org_id = owner_headers["X-Org-Id"]
    denied = httpx.post(
        f"{server_url_toss}/api/orgs/{org_id}/billing-key/auth", timeout=10, headers=auth_headers
    )
    assert denied.status_code == 403
    key = _register_key(server_url_toss, owner_headers)
    assert key["status"] == "active"
    assert key["card_label"]
    for headers in (owner_headers, auth_headers):
        res = httpx.get(
            f"{server_url_toss}/api/orgs/{org_id}/billing-key", timeout=10, headers=headers
        )
        assert res.status_code == 200, res.text
        assert "billing_key" not in res.json()
        assert "stub_bk_" not in res.text


def test_billing_key_with_wrong_customer_key_is_422(
    server_url_toss: str, owner_headers: dict[str, str]
) -> None:
    res = httpx.post(
        f"{server_url_toss}/api/orgs/{owner_headers['X-Org-Id']}/billing-key",
        json={
            "auth_key": "stub_auth_x",
            "customer_key": "sl-other",
            "plan": "pilot",
            "charge_day": 1,
        },
        timeout=10,
        headers=owner_headers,
    )
    assert res.status_code == 422


def test_charge_due_bills_card_once_per_month(
    pg_dsn: str,
    toss_stub: str,
    server_url_toss: str,
    owner_headers: dict[str, str],
    admin_conn: psycopg.Connection,
) -> None:
    org_id = owner_headers["X-Org-Id"]
    due = _register_key(server_url_toss, owner_headers)["next_charge_on"]
    for _ in range(2):
        done = cli(pg_dsn, ["charge-due", "--today", due], _toss_env(toss_stub))
        assert done.returncode == 0, done.stderr
    rows = _invoices(server_url_toss, owner_headers)
    assert len(rows) == 1
    assert rows[0]["status"] == "paid"
    assert rows[0]["method"] == "card"
    assert _audit_count(admin_conn, org_id, "invoice.paid") == 1
    after = httpx.get(
        f"{server_url_toss}/api/orgs/{org_id}/billing-key", timeout=10, headers=owner_headers
    ).json()
    assert date.fromisoformat(after["next_charge_on"]) > date.fromisoformat(due)


def test_three_declines_stop_autopay(
    pg_dsn: str,
    toss_stub: str,
    server_url_toss: str,
    owner_headers: dict[str, str],
    admin_conn: psycopg.Connection,
) -> None:
    org_id = owner_headers["X-Org-Id"]
    due = _register_key(server_url_toss, owner_headers)["next_charge_on"]
    assert httpx.post(f"{toss_stub}/__stub/decline", json={"decline": True}).status_code == 200
    for _ in range(3):
        done = cli(pg_dsn, ["charge-due", "--today", due], _toss_env(toss_stub))
        assert done.returncode == 0, done.stderr
    state = httpx.get(
        f"{server_url_toss}/api/orgs/{org_id}/billing-key", timeout=10, headers=owner_headers
    ).json()
    assert state["failures"] == 3
    assert state["status"] == "stopped"
    assert _audit_count(admin_conn, org_id, "billing_key.stop") == 1
    assert all(r["status"] != "paid" for r in _invoices(server_url_toss, owner_headers))


def test_billing_page_in_widget_mode_has_no_serious_accessibility_violations(
    server_url_toss: str,
    owner_headers: dict[str, str],
    maintenance: Maintenance,
    owner_page: Page,
    axe_check: Axe,
) -> None:
    _issue(maintenance, owner_headers["X-Org-Id"])
    _stub_sdk(owner_page)
    owner_page.goto(f"{server_url_toss}/billing")
    owner_page.locator("button.invoice-pay").click()
    expect(owner_page.locator("#payment-method [data-stub='widget']")).to_be_visible()
    assert axe_check(_settled(owner_page)) == []


def test_real_sdk_loads_under_csp(
    tmp_path: Path,
    app_dsn: str,
    request: pytest.FixtureRequest,
    toss_stub: str,
    owner_headers: dict[str, str],
    maintenance: Maintenance,
    owner_page: Page,
) -> None:
    client_key = os.environ.get("SHOPLEGACY_TOSS_CLIENT_KEY")
    if not client_key:
        pytest.skip("SHOPLEGACY_TOSS_CLIENT_KEY not set")
    extra = {**_toss_env(toss_stub), "SHOPLEGACY_TOSS_CLIENT_KEY": client_key}
    servers = _start_server(tmp_path, app_dsn, request, require_consent=False, extra_env=extra)
    base = next(servers)
    try:
        _issue(maintenance, owner_headers["X-Org-Id"])
        csp = _csp_errors(owner_page)
        owner_page.goto(f"{base}/billing")
        owner_page.locator("button.invoice-pay").click()
        expect(owner_page.locator("#payment-method iframe").first).to_be_attached(timeout=20000)
        assert csp == []
    finally:
        servers.close()
