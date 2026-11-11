"""E2E: plan limits, operator plan change, billing screen."""

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# Screens fade and slide in (GSAP); axe must judge the settled screen, not a frame mid-fade.
_SETTLED_JS = (
    "() => [document.querySelector('main'), ...document.querySelectorAll('[data-stagger]')]"
    ".every((e) => !e || getComputedStyle(e).opacity === '1')"
)


def _settled(pg: Page) -> Page:
    pg.wait_for_function(_SETTLED_JS)
    return pg


def _shop(base: str, headers: dict[str, str], n: int) -> httpx.Response:
    return httpx.post(
        f"{base}/api/shops",
        json={
            "name": f"가게{n}",
            "owner_name": "홍길동",
            "trade": "restaurant",
            "area_type": "residential",
            "years": 5,
            "closed_year": 2025,
        },
        timeout=10,
        headers=headers,
    )


def _set_plan(pg_dsn: str, org_id: str, plan: str) -> None:
    env = {**os.environ, "SHOPLEGACY_DATABASE_URL": pg_dsn}
    done = subprocess.run(
        [sys.executable, "-m", "shoplegacy.maintenance", "set-plan", org_id, plan],
        check=False,
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr


def test_fourth_shop_on_free_plan_is_402_until_operator_sets_pilot(
    server_url: str, reviewer_headers: dict[str, str], pg_dsn: str
) -> None:
    for n in range(3):
        assert _shop(server_url, reviewer_headers, n).status_code == 201
    blocked = _shop(server_url, reviewer_headers, 3)
    assert blocked.status_code == 402
    assert blocked.json()["detail"] == "plan_limit:shops"
    _set_plan(pg_dsn, reviewer_headers["X-Org-Id"], "pilot")
    assert _shop(server_url, reviewer_headers, 3).status_code == 201


def test_plan_cannot_be_changed_over_http(server_url: str, owner_headers: dict[str, str]) -> None:
    org_id = owner_headers["X-Org-Id"]
    res = httpx.put(
        f"{server_url}/api/orgs/{org_id}/plan",
        json={"plan": "pilot"},
        timeout=10,
        headers=owner_headers,
    )
    assert res.status_code == 405
    info = httpx.get(f"{server_url}/api/orgs/{org_id}/plan", timeout=10, headers=owner_headers)
    assert info.json()["plan"] == "free"


def test_plan_of_other_org_is_forbidden(
    server_url: str, owner_headers: dict[str, str], second_org: dict[str, str]
) -> None:
    res = httpx.get(
        f"{server_url}/api/orgs/{second_org['X-Org-Id']}/plan", timeout=10, headers=owner_headers
    )
    assert res.status_code == 403


def test_billing_page_shows_plan_and_usage_and_passes_axe(
    server_url: str,
    reviewer_headers: dict[str, str],
    pg_dsn: str,
    owner_page: Page,
    axe_check: Callable[[Page], list[dict[str, object]]],
) -> None:
    _set_plan(pg_dsn, reviewer_headers["X-Org-Id"], "pilot")
    for n in range(4):
        assert _shop(server_url, reviewer_headers, n).status_code == 201
    owner_page.goto(f"{server_url}/billing")
    expect(owner_page.locator("#plan-name")).to_have_text("시범 사업")
    expect(owner_page.locator("#usage-shops")).to_have_text("4 / 30")
    expect(owner_page.locator("#plan-contact")).to_contain_text("계약·세금계산서 문의")
    expect(owner_page.locator('#org-subtabs a[href="/billing"]')).to_have_attribute(
        "aria-current", "page"
    )
    expect(owner_page.locator("#usage-shops-bar")).to_be_attached()
    expect(owner_page.locator("#retention-save")).to_have_count(0)
    out = Path(os.environ.get("ILLY_SHOT_DIR", "tests/e2e/screenshots"))
    out.mkdir(parents=True, exist_ok=True)
    owner_page.screenshot(path=str(out / "billing.png"), full_page=True)
    assert axe_check(_settled(owner_page)) == []


def test_settings_page_retention_save_is_enabled_for_owner(
    server_url: str, owner_page: Page
) -> None:
    owner_page.goto(f"{server_url}/settings")
    expect(owner_page.locator("#retention-save")).to_be_enabled()


def test_settings_page_retention_below_minimum_is_rejected(
    server_url: str, owner_page: Page
) -> None:
    owner_page.goto(f"{server_url}/settings")
    expect(owner_page.locator("#retention-save")).to_be_enabled()
    owner_page.fill("#retention-days", "10")
    owner_page.click("#retention-save")
    expect(owner_page.locator("#retention-msg")).to_contain_text("30일 이상")
