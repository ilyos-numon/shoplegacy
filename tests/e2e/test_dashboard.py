"""E2E: org dashboard numbers, CSV/XLSX report, dashboard screen."""

import csv
import io
import os
from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx
from openpyxl import load_workbook
from playwright.sync_api import Page, expect

MakeCard = Callable[..., tuple[str, str]]


# Screens fade and slide in (GSAP); axe must judge the settled screen, not a frame mid-fade.
_SETTLED_JS = (
    "() => [document.querySelector('main'), ...document.querySelectorAll('[data-stagger]')]"
    ".every((e) => !e || getComputedStyle(e).opacity === '1')"
)


def _settled(pg: Page) -> Page:
    pg.wait_for_function(_SETTLED_JS)
    return pg


def _consent(base: str, headers: dict[str, str], shop_id: str, kind: str) -> None:
    res = httpx.post(
        f"{base}/api/shops/{shop_id}/consents",
        json={"kind": kind, "method": "verbal", "note": "e2e"},
        timeout=10,
        headers=headers,
    )
    assert res.status_code == 201, res.text


def _seed(base: str, reviewer: dict[str, str], make_card: MakeCard) -> None:
    """Two shops with one ended interview each, one published card, two consents."""
    shop_a, card_a = make_card(base, reviewer)
    shop_b, _ = make_card(base, reviewer)
    _consent(base, reviewer, shop_a, "publication")
    _consent(base, reviewer, shop_b, "recording")
    res = httpx.post(f"{base}/api/cards/{card_a}/publish", timeout=10, headers=reviewer)
    assert res.status_code == 200, res.text


def test_dashboard_api_matches_created_data(
    server_url: str,
    owner_headers: dict[str, str],
    reviewer_headers: dict[str, str],
    make_card: MakeCard,
) -> None:
    _seed(server_url, reviewer_headers, make_card)
    org_id = owner_headers["X-Org-Id"]
    res = httpx.get(f"{server_url}/api/orgs/{org_id}/dashboard", timeout=10, headers=owner_headers)
    assert res.status_code == 200, res.text
    stats = res.json()
    assert stats["shops"] == 2
    assert stats["interviews"] == 2
    assert stats["published_cards"] == 1
    assert stats["cards_by_status"]["published"] == 1
    assert stats["consents"]["publication"]["granted"] == 1
    assert stats["consents"]["recording"]["granted"] == 1
    assert stats["consents"]["storage"]["granted"] == 0


def test_dashboard_rejects_reversed_period_and_non_admin(
    server_url: str, owner_headers: dict[str, str], reviewer_headers: dict[str, str]
) -> None:
    org_id = owner_headers["X-Org-Id"]
    url = f"{server_url}/api/orgs/{org_id}/dashboard"
    reversed_period = httpx.get(
        url, params={"from": "2026-02-01", "to": "2026-01-01"}, timeout=10, headers=owner_headers
    )
    assert reversed_period.status_code == 422
    assert reversed_period.json()["detail"] == "from_after_to"
    assert httpx.get(url, timeout=10, headers=reviewer_headers).status_code == 403


def test_report_csv_and_xlsx(
    server_url: str,
    owner_headers: dict[str, str],
    reviewer_headers: dict[str, str],
    make_card: MakeCard,
) -> None:
    _seed(server_url, reviewer_headers, make_card)
    org_id = owner_headers["X-Org-Id"]
    base = f"{server_url}/api/orgs/{org_id}/report"
    res = httpx.get(base, params={"format": "csv"}, timeout=10, headers=owner_headers)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
    assert len(rows[0]) == 13
    assert rows[0][0] == "shop_id"
    assert len(rows) == 3

    res = httpx.get(base, params={"format": "xlsx"}, timeout=10, headers=owner_headers)
    assert res.status_code == 200, res.text
    sheet = load_workbook(io.BytesIO(res.content))["report"]
    assert [c.value for c in sheet[1]] == rows[0]
    assert sheet.max_row == 3


def test_dashboard_page_shows_numbers_and_passes_axe(
    server_url: str,
    reviewer_headers: dict[str, str],
    make_card: MakeCard,
    owner_page: Page,
    axe_check: Callable[[Page], list[dict[str, object]]],
) -> None:
    _seed(server_url, reviewer_headers, make_card)
    owner_page.goto(f"{server_url}/dashboard")
    expect(owner_page.locator("#stat-shops")).to_have_text("2")
    expect(owner_page.locator("#stat-interviews")).to_have_text("2")
    expect(owner_page.locator("#stat-published")).to_have_text("1")
    expect(owner_page.locator("#consent-table tbody tr")).to_have_count(4)
    expect(owner_page.locator('#consent-table tr[data-kind="publication"] td').first).to_have_text(
        "1"
    )
    expect(owner_page.locator('#status-table tr[data-status="published"] td')).to_have_text("1")
    expect(owner_page.locator('#org-subtabs a[href="/dashboard"]')).to_have_attribute(
        "aria-current", "page"
    )
    expect(owner_page.locator('#org-subtabs a[href="/billing"]')).to_be_visible()
    out = Path(os.environ.get("ILLY_SHOT_DIR", "tests/e2e/screenshots"))
    out.mkdir(parents=True, exist_ok=True)
    owner_page.screenshot(path=str(out / "dashboard.png"), full_page=True)
    assert axe_check(_settled(owner_page)) == []


def test_dashboard_page_denies_reviewer(server_url: str, page: Page) -> None:
    page.goto(f"{server_url}/dashboard")
    expect(page.locator("#dash-error")).to_contain_text("관리자")
    expect(page.locator('#org-subtabs a[href="/billing"]')).to_be_hidden()


def test_dashboard_period_chips_set_range_and_custom_dates(
    server_url: str, owner_page: Page
) -> None:
    owner_page.goto(f"{server_url}/dashboard")
    thirty = owner_page.locator('#dash-range button[data-days="30"]')
    expect(thirty).to_have_attribute("aria-pressed", "true")
    expect(owner_page.locator("#dash-from")).to_be_hidden()
    owner_page.locator('#dash-range button[data-days="90"]').click()
    expect(owner_page.locator('#dash-range button[data-days="90"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(owner_page.locator("#dash-period")).to_contain_text("~")
    from_value = owner_page.locator("#dash-from").input_value()
    to_value = owner_page.locator("#dash-to").input_value()
    span = date.fromisoformat(to_value) - date.fromisoformat(from_value)
    assert span.days == 89
    owner_page.locator('#dash-range button[data-days="custom"]').click()
    expect(owner_page.locator("#dash-from")).to_be_visible()
    owner_page.fill("#dash-from", "2026-02-01")
    owner_page.fill("#dash-to", "2026-01-01")
    owner_page.click("#dash-apply")
    expect(owner_page.locator("#dash-error")).to_contain_text("시작일이 종료일보다")
