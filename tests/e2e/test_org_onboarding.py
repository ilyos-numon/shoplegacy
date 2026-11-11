"""UI E2E: a signed-in user with no organisation is sent to onboarding, creates one, registers a shop."""

import json
import os
import re
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import expect as pw_expect

from tests.conftest import mint_token

ORG_NAME = "테스트 센터"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _save(page: Page, name: str) -> None:
    out = Path(os.environ.get("ILLY_SHOT_DIR", "tests/e2e/screenshots"))
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    page.screenshot(path=str(path), full_page=True)
    assert path.stat().st_size > 0


@pytest.fixture
def orgless_token(pg_dsn: str, jwt_secret: str, make_user: Callable[[str, str], str]) -> str:
    """Access token of a freshly created user who belongs to no organisation."""
    email = f"orgless-{uuid.uuid4().hex[:8]}@example.test"
    user_id = make_user(pg_dsn, email)
    return mint_token(user_id, email, jwt_secret)


@pytest.fixture
def orgless_page(orgless_token: str) -> Iterator[Page]:
    """Mobile-sized browser page holding only an access token (no sl_org_id)."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 390, "height": 844})
        context.add_init_script(
            f"window.localStorage.setItem('sl_access_token', {json.dumps(orgless_token)});"
        )
        pg = context.new_page()
        try:
            yield pg
        finally:
            context.close()
            browser.close()


def test_no_org_user_is_sent_to_onboarding_and_registers_shop(
    server_url: str, orgless_page: Page
) -> None:
    page = orgless_page
    page.goto(f"{server_url}/")
    page.wait_for_url(f"{server_url}/onboarding?next=%2F")
    pw_expect(page.locator("#org-name")).to_be_visible()
    _save(page, "onboarding.png")

    page.fill("#org-name", ORG_NAME)
    page.click("#create-org")
    page.wait_for_url(f"{server_url}/")
    org_id = page.evaluate("window.localStorage.getItem('sl_org_id')")
    assert org_id and UUID_RE.match(org_id), org_id
    _save(page, "onboarding-done.png")

    page.click("#new-shop-btn")
    page.fill("#shop-name", "가나다식당")
    page.fill("#owner-name", "홍길동")
    page.fill("#years", "18")
    page.fill("#closed-year", "2025")
    page.click("#start-btn")
    page.wait_for_url(re.compile(r"/shops/[^/]+/consent$"))
    page.click("#consent-save")
    page.wait_for_url(re.compile(r"/interview/[^/]+$"))


def test_empty_org_name_shows_message(server_url: str, orgless_page: Page) -> None:
    page = orgless_page
    page.goto(f"{server_url}/onboarding")
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url) if "/api/orgs" in r.url else None)
    page.click("#create-org")
    error = page.locator("#onboarding-error")
    pw_expect(error).to_be_visible()
    pw_expect(error).to_contain_text("기관 이름을 입력해 주세요.")
    assert requests == []


def test_shop_without_org_is_400_then_201_after_org(server_url: str, orgless_token: str) -> None:
    auth = {"Authorization": f"Bearer {orgless_token}"}
    shop = {
        "name": "가나다식당",
        "owner_name": "홍길동",
        "trade": "restaurant",
        "area_type": "residential",
        "years": 18,
        "closed_year": 2025,
    }
    res = httpx.post(f"{server_url}/api/shops", json=shop, headers=auth, timeout=10)
    assert res.status_code == 400, res.text
    assert res.json() == {"detail": "org_required"}

    org = httpx.post(f"{server_url}/api/orgs", json={"name": ORG_NAME}, headers=auth, timeout=10)
    assert org.status_code == 201, org.text
    org_id = org.json()["org_id"]

    res = httpx.post(
        f"{server_url}/api/shops", json=shop, headers={**auth, "X-Org-Id": org_id}, timeout=10
    )
    assert res.status_code == 201, res.text


def test_onboarding_page_needs_login(server_url: str, anon_page: Page) -> None:
    anon_page.goto(f"{server_url}/onboarding")
    anon_page.wait_for_url(f"{server_url}/login?next=%2Fonboarding")


def _page_with_stale_org(token: str, stale_org: str) -> Iterator[Page]:
    """Browser page holding a token and an sl_org_id seeded once (survives no redirect)."""
    seed = (
        "if (!window.sessionStorage.getItem('sl_seeded')) {"
        f"window.localStorage.setItem('sl_access_token', {json.dumps(token)});"
        f"window.localStorage.setItem('sl_org_id', {json.dumps(stale_org)});"
        "window.sessionStorage.setItem('sl_seeded', '1');}"
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 390, "height": 844})
        context.add_init_script(seed)
        pg = context.new_page()
        try:
            yield pg
        finally:
            context.close()
            browser.close()


def test_stale_org_without_membership_goes_to_onboarding(
    server_url: str, orgless_token: str
) -> None:
    for page in _page_with_stale_org(orgless_token, str(uuid.uuid4())):
        page.goto(f"{server_url}/")
        page.wait_for_url(f"{server_url}/onboarding?next=%2F")
        pw_expect(page.locator("#org-name")).to_be_visible()
        assert page.evaluate("window.localStorage.getItem('sl_org_id')") is None


def test_stale_org_is_replaced_by_real_membership(
    server_url: str, org_fixture: tuple[str, str, str, str]
) -> None:
    org_id, owner_token, _, _ = org_fixture
    for page in _page_with_stale_org(owner_token, str(uuid.uuid4())):
        page.goto(f"{server_url}/")
        page.click("#new-shop-btn")
        page.fill("#shop-name", "가나다식당")
        page.fill("#owner-name", "홍길동")
        page.fill("#years", "18")
        page.fill("#closed-year", "2025")
        page.click("#start-btn")
        page.wait_for_url(re.compile(r"/shops/[^/]+/consent$"))
        assert page.evaluate("window.localStorage.getItem('sl_org_id')") == org_id
