"""E2E: invoices issued by the operator, contract and fake-card payment, webhook."""

import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import psycopg
from playwright.sync_api import Page, expect

from tests.e2e.conftest import PROJECT_ROOT, TEST_JWT_SECRET, TEST_PII_KEY

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


def cli(
    pg_dsn: str, args: list[str], extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the operator CLI and return the finished process, whatever its exit code."""
    env = {
        **os.environ,
        "SHOPLEGACY_DATABASE_URL": pg_dsn,
        "SHOPLEGACY_JWT_SECRET": TEST_JWT_SECRET,
        "SHOPLEGACY_PII_KEYS": TEST_PII_KEY,
        "SHOPLEGACY_BILLING_PROVIDER": "fake",
        **(extra_env or {}),
    }
    return subprocess.run(
        [sys.executable, "-m", "shoplegacy.maintenance", *args],
        check=False,
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _issue(maintenance: Maintenance, org_id: str, month: str = "2026-11") -> str:
    out = maintenance(["issue-invoice", org_id, "pilot", month])
    return out.strip().splitlines()[-1]


def _invoices(base: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    res = httpx.get(f"{base}/api/orgs/{headers['X-Org-Id']}/invoices", timeout=10, headers=headers)
    assert res.status_code == 200, res.text
    return list(res.json())


def _checkout(base: str, headers: dict[str, str], invoice_id: str) -> httpx.Response:
    return httpx.post(
        f"{base}/api/orgs/{headers['X-Org-Id']}/invoices/{invoice_id}/checkout",
        timeout=10,
        headers=headers,
    )


def _audit_count(conn: psycopg.Connection, org_id: str, action: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM public.audit_log WHERE org_id = %s AND action = %s",
        (org_id, action),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_issue_invoice_lists_one_issued_invoice(
    server_url: str, owner_headers: dict[str, str], maintenance: Maintenance
) -> None:
    invoice_id = _issue(maintenance, owner_headers["X-Org-Id"])
    rows = _invoices(server_url, owner_headers)
    assert len(rows) == 1
    assert rows[0]["invoice_id"] == invoice_id
    assert rows[0]["amount_krw"] == 99000
    assert rows[0]["status"] == "issued"


def test_contract_payment_marks_paid_and_moves_plan(
    server_url: str,
    owner_headers: dict[str, str],
    maintenance: Maintenance,
    admin_conn: psycopg.Connection,
) -> None:
    org_id = owner_headers["X-Org-Id"]
    invoice_id = _issue(maintenance, org_id)
    out = maintenance(["mark-paid", invoice_id, "--method", "contract"])
    assert f"{invoice_id} paid" in out
    row = _invoices(server_url, owner_headers)[0]
    assert row["status"] == "paid"
    assert row["method"] == "contract"
    plan = httpx.get(f"{server_url}/api/orgs/{org_id}/plan", timeout=10, headers=owner_headers)
    assert plan.json()["plan"] == "pilot"
    assert _audit_count(admin_conn, org_id, "invoice.paid") == 1
    assert _audit_count(admin_conn, org_id, "org.plan") == 1


def test_owner_checkout_returns_pay_url_and_confirm_pays(
    server_url: str, owner_headers: dict[str, str], maintenance: Maintenance
) -> None:
    invoice_id = _issue(maintenance, owner_headers["X-Org-Id"])
    res = _checkout(server_url, owner_headers, invoice_id)
    assert res.status_code == 201, res.text
    session = res.json()
    assert "/billing/pay-test" in session["checkout_url"]
    confirm = httpx.post(
        f"{server_url}/api/billing/confirm",
        json={
            "payment_key": f"fake_{session['order_id']}",
            "order_id": session["order_id"],
            "amount_krw": 99000,
        },
        timeout=10,
        headers=owner_headers,
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["status"] == "paid"
    assert confirm.json()["method"] == "card"


def test_confirm_with_wrong_amount_is_409(
    server_url: str, owner_headers: dict[str, str], maintenance: Maintenance
) -> None:
    invoice_id = _issue(maintenance, owner_headers["X-Org-Id"])
    session = _checkout(server_url, owner_headers, invoice_id).json()
    res = httpx.post(
        f"{server_url}/api/billing/confirm",
        json={
            "payment_key": f"fake_{session['order_id']}",
            "order_id": session["order_id"],
            "amount_krw": 1,
        },
        timeout=10,
        headers=owner_headers,
    )
    assert res.status_code == 409
    assert res.json()["detail"] == "amount_mismatch"
    assert _invoices(server_url, owner_headers)[0]["status"] == "issued"


def test_webhook_twice_pays_once(
    server_url: str,
    owner_headers: dict[str, str],
    maintenance: Maintenance,
    admin_conn: psycopg.Connection,
) -> None:
    org_id = owner_headers["X-Org-Id"]
    invoice_id = _issue(maintenance, org_id)
    order_id = _checkout(server_url, owner_headers, invoice_id).json()["order_id"]
    payload = {
        "eventType": "PAYMENT_STATUS_CHANGED",
        "data": {"paymentKey": f"fake_{order_id}", "orderId": order_id, "status": "DONE"},
    }
    for _ in range(2):
        res = httpx.post(f"{server_url}/api/billing/webhook", json=payload, timeout=10)
        assert res.status_code == 200, res.text
        assert res.json()["ok"] is True
    assert _invoices(server_url, owner_headers)[0]["status"] == "paid"
    assert _audit_count(admin_conn, org_id, "invoice.paid") == 1


def test_non_owner_checkout_is_403(
    server_url: str,
    owner_headers: dict[str, str],
    auth_headers: dict[str, str],
    maintenance: Maintenance,
) -> None:
    invoice_id = _issue(maintenance, owner_headers["X-Org-Id"])
    assert _checkout(server_url, auth_headers, invoice_id).status_code == 403


def test_unknown_invoice_checkout_is_404(server_url: str, owner_headers: dict[str, str]) -> None:
    res = _checkout(server_url, owner_headers, "00000000-0000-0000-0000-000000000000")
    assert res.status_code == 404


def test_owner_pays_from_billing_screen(
    server_url: str,
    owner_headers: dict[str, str],
    maintenance: Maintenance,
    owner_page: Page,
    shot_dir: Path,
    axe_check: Axe,
) -> None:
    invoice_id = _issue(maintenance, owner_headers["X-Org-Id"])
    owner_page.goto(f"{server_url}/billing")
    row = owner_page.locator(f'#invoice-table tbody tr[data-invoice-id="{invoice_id}"]')
    expect(row.locator(".invoice-status")).to_have_text("미결제")
    expect(row.locator(".invoice-amount")).to_contain_text("99,000")
    row.locator("button.invoice-pay").click()

    owner_page.wait_for_url(re.compile(r"/billing/pay-test\?"))
    expect(owner_page.locator("#pay-amount")).to_contain_text("99,000")
    expect(owner_page.locator("#pay-confirm")).to_have_text("결제 완료")
    owner_page.screenshot(path=str(shot_dir / "pay-test.png"), full_page=True)
    assert axe_check(_settled(owner_page)) == []

    owner_page.click("#pay-confirm")
    expect(owner_page.locator("#pay-result")).to_have_text("결제 완료")
    owner_page.wait_for_url(re.compile(r"/billing\?paid="))
    expect(owner_page.locator("#billing-message")).to_contain_text("결제가 완료됐어요")
    row = owner_page.locator(f'#invoice-table tbody tr[data-invoice-id="{invoice_id}"]')
    expect(row.locator(".invoice-status")).to_have_text("결제 완료")
    expect(row.locator(".invoice-plan")).to_be_visible()
    owner_page.screenshot(path=str(shot_dir / "billing-invoices.png"), full_page=True)
    assert axe_check(_settled(owner_page)) == []


def test_refund_marks_invoice_refunded_and_shows_on_screen(
    server_url: str,
    owner_headers: dict[str, str],
    maintenance: Maintenance,
    owner_page: Page,
    shot_dir: Path,
) -> None:
    invoice_id = _issue(maintenance, owner_headers["X-Org-Id"])
    maintenance(["mark-paid", invoice_id, "--method", "contract"])
    maintenance(["mark-tax-invoice", invoice_id, "--ref", "20261104-0001"])
    out = maintenance(["refund-invoice", invoice_id, "--reason", "중복 결제"])
    assert f"{invoice_id} refunded" in out
    row = _invoices(server_url, owner_headers)[0]
    assert row["status"] == "refunded"
    assert row["refund_reason"] == "중복 결제"
    assert row["tax_invoice_ref"] == "20261104-0001"

    owner_page.goto(f"{server_url}/billing")
    line = owner_page.locator(f'#invoice-table tbody tr[data-invoice-id="{invoice_id}"]')
    expect(line.locator(".invoice-status")).to_have_text("환불됨")
    expect(line.locator(".invoice-tax")).to_have_text("20261104-0001")
    expect(line.locator("button.invoice-pay")).to_have_count(0)
    owner_page.screenshot(path=str(shot_dir / "billing-refunded.png"), full_page=True)


def test_refund_of_issued_invoice_is_rejected(
    pg_dsn: str, server_url: str, owner_headers: dict[str, str], maintenance: Maintenance
) -> None:
    invoice_id = _issue(maintenance, owner_headers["X-Org-Id"])
    done = cli(pg_dsn, ["refund-invoice", invoice_id, "--reason", "x"])
    assert done.returncode == 1
    assert "invoice_not_refundable" in done.stderr
    assert _invoices(server_url, owner_headers)[0]["status"] == "issued"


def test_mark_tax_invoice_records_reference(
    server_url: str, owner_headers: dict[str, str], maintenance: Maintenance
) -> None:
    invoice_id = _issue(maintenance, owner_headers["X-Org-Id"])
    maintenance(["mark-paid", invoice_id, "--method", "contract"])
    out = maintenance(["mark-tax-invoice", invoice_id, "--ref", "20261104-0001"])
    assert "tax_invoice 20261104-0001" in out
    row = _invoices(server_url, owner_headers)[0]
    assert row["tax_invoice_ref"] == "20261104-0001"
    assert row["tax_invoice_issued_at"] is not None


def test_mark_tax_invoice_on_unpaid_invoice_is_rejected(
    pg_dsn: str, server_url: str, owner_headers: dict[str, str], maintenance: Maintenance
) -> None:
    invoice_id = _issue(maintenance, owner_headers["X-Org-Id"])
    done = cli(pg_dsn, ["mark-tax-invoice", invoice_id, "--ref", "20261104-0001"])
    assert done.returncode == 1
    assert "invoice_not_payable" in done.stderr
    assert _invoices(server_url, owner_headers)[0]["tax_invoice_ref"] is None


def test_fake_autopay_registers_card_and_charge_due_pays_once(
    pg_dsn: str,
    server_url: str,
    owner_headers: dict[str, str],
    maintenance: Maintenance,
    owner_page: Page,
    admin_conn: psycopg.Connection,
) -> None:
    org_id = owner_headers["X-Org-Id"]
    owner_page.goto(f"{server_url}/billing")
    expect(owner_page.locator("#autopay-status")).to_have_text("등록된 카드가 없어요")
    owner_page.select_option("#autopay-plan", "pilot")
    expect(owner_page.locator("#autopay-register")).to_be_enabled()
    owner_page.click("#autopay-register")

    owner_page.wait_for_url(re.compile(r"/billing/pay-test\?"))
    expect(owner_page.locator("#pay-confirm")).to_have_text("카드 등록")
    owner_page.click("#pay-confirm")
    owner_page.wait_for_url(re.compile(r"/billing\?billing_auth=1"))
    expect(owner_page.locator("#autopay-status")).to_have_text("사용 중")
    expect(owner_page.locator("#autopay-next")).to_have_text(re.compile(r"\d{4}-\d{2}-\d{2}"))

    res = httpx.get(
        f"{server_url}/api/orgs/{org_id}/billing-key", timeout=10, headers=owner_headers
    )
    assert res.status_code == 200, res.text
    key = res.json()
    assert key["status"] == "active"
    assert key["plan"] == "pilot"
    assert "billing_key" not in key
    due = key["next_charge_on"]

    first = maintenance(["charge-due", "--today", due])
    assert " paid " in first
    rows = _invoices(server_url, owner_headers)
    assert len(rows) == 1
    assert rows[0]["status"] == "paid"
    assert rows[0]["method"] == "card"
    moved = httpx.get(
        f"{server_url}/api/orgs/{org_id}/billing-key", timeout=10, headers=owner_headers
    )
    assert date.fromisoformat(moved.json()["next_charge_on"]) > date.fromisoformat(due)

    maintenance(["charge-due", "--today", due])
    assert len(_invoices(server_url, owner_headers)) == 1
    assert _audit_count(admin_conn, org_id, "invoice.paid") == 1


def test_fake_autopay_three_declines_stop_the_key(
    pg_dsn: str,
    server_url: str,
    owner_headers: dict[str, str],
    admin_conn: psycopg.Connection,
) -> None:
    org_id = owner_headers["X-Org-Id"]
    auth = httpx.post(
        f"{server_url}/api/orgs/{org_id}/billing-key/auth", timeout=10, headers=owner_headers
    )
    assert auth.status_code == 201, auth.text
    customer_key = auth.json()["customer_key"]
    reg = httpx.post(
        f"{server_url}/api/orgs/{org_id}/billing-key",
        json={
            "auth_key": f"fake_auth_{customer_key}",
            "customer_key": customer_key,
            "plan": "pilot",
            "charge_day": 1,
        },
        timeout=10,
        headers=owner_headers,
    )
    assert reg.status_code == 201, reg.text
    due = reg.json()["next_charge_on"]
    for _ in range(3):
        done = cli(
            pg_dsn,
            ["charge-due", "--today", due],
            {"SHOPLEGACY_FAKE_DECLINE_BILLING": "1"},
        )
        assert done.returncode == 0, done.stderr
    state = httpx.get(
        f"{server_url}/api/orgs/{org_id}/billing-key", timeout=10, headers=owner_headers
    )
    assert state.json()["status"] == "stopped"
    assert state.json()["failures"] == 3
    assert _audit_count(admin_conn, org_id, "billing_key.stop") == 1
