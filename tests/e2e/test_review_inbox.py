"""E2E: review inbox lists the org's cards by status, filters, badges and opens a card."""

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Page, expect, sync_playwright

MakeCard = Callable[..., tuple[str, str]]
PRIVATE_WORDS = ("가나다식당", "홍길동", "라마바정육")


@contextmanager
def _mobile_page(headers: dict[str, str]) -> Iterator[Page]:
    token = headers["Authorization"].removeprefix("Bearer ")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 390, "height": 844})
        context.add_init_script(
            f"window.localStorage.setItem('sl_access_token', {json.dumps(token)});"
            f"window.localStorage.setItem('sl_org_id', {json.dumps(headers['X-Org-Id'])});"
        )
        pg = context.new_page()
        try:
            yield pg
        finally:
            context.close()
            browser.close()


def test_inbox_lists_filters_badges_and_opens_card(
    server_url: str,
    auth_headers: dict[str, str],
    make_card: MakeCard,
    axe_check: Callable[[Page], list[dict[str, Any]]],
    shot_dir: Path,
) -> None:
    _, draft_id = make_card(server_url, auth_headers)
    _, pub_id = make_card(server_url, auth_headers)
    published = httpx.post(
        f"{server_url}/api/cards/{pub_id}/publish", timeout=10, headers=auth_headers
    )
    assert published.status_code == 200, published.text

    with _mobile_page(auth_headers) as page:
        page.goto(f"{server_url}/review")
        rows = page.locator("#inbox-list li[data-card-id]")
        expect(rows).to_have_count(2)
        expect(page.locator(f'#inbox-list li[data-card-id="{draft_id}"]')).to_be_visible()
        expect(page.locator(f'#inbox-list li[data-card-id="{pub_id}"]')).to_be_visible()
        expect(page.locator("#inbox-empty")).to_be_hidden()
        expect(page.locator("#inbox-list .meter").first).to_contain_text("근거 있는 문장")

        nav = page.locator('#app-nav a[data-nav="inbox"]')
        expect(nav).to_be_visible()
        expect(nav).to_have_attribute("aria-current", "page")
        expect(page.locator("#inbox-badge")).to_have_text("1")
        expect(page.locator("#inbox-badge")).to_be_visible()

        body = page.locator("main").inner_text()
        for word in PRIVATE_WORDS:
            assert word not in body, word

        page.wait_for_timeout(700)
        page.screenshot(path=str(shot_dir / "s7-inbox-all.png"), full_page=True)
        assert axe_check(page) == []

        chips = page.locator("#inbox-filter button[data-f]")
        expect(chips).to_have_count(5)
        draft_chip = page.locator('#inbox-filter button[data-f="draft"]')
        draft_chip.click()
        expect(draft_chip).to_have_attribute("aria-pressed", "true")
        expect(page.locator('#inbox-filter button[data-f="all"]')).to_have_attribute(
            "aria-pressed", "false"
        )
        expect(rows).to_have_count(1)
        expect(rows.first).to_have_attribute("data-card-id", draft_id)
        page.screenshot(path=str(shot_dir / "s7-inbox-draft.png"), full_page=True)

        page.locator('#inbox-filter button[data-f="pub"]').click()
        expect(rows).to_have_count(1)
        expect(rows.first).to_have_attribute("data-card-id", pub_id)

        page.locator('#inbox-filter button[data-f="hold"]').click()
        expect(rows).to_have_count(0)
        expect(page.locator("#inbox-empty")).to_be_visible()
        page.screenshot(path=str(shot_dir / "s7-inbox-empty.png"), full_page=True)

        page.locator('#inbox-filter button[data-f="draft"]').click()
        rows.first.click()
        page.wait_for_url(re.compile(rf"/review/{re.escape(draft_id)}$"))
        expect(page.locator("#card-status")).to_be_visible()


def test_interviewer_has_no_inbox_menu(
    server_url: str,
    interviewer_headers: dict[str, str],
    shot_dir: Path,
) -> None:
    with _mobile_page(interviewer_headers) as page:
        page.goto(f"{server_url}/")
        expect(page.locator('#app-nav a[data-nav="home"]')).to_be_visible()
        expect(page.locator('#app-nav a[data-nav="inbox"]')).to_be_hidden()

        page.goto(f"{server_url}/review")
        expect(page.locator('#app-nav a[data-nav="inbox"]')).to_be_hidden()
        expect(page.locator("#inbox-error")).to_be_visible()
        expect(page.locator("#inbox-list li")).to_have_count(0)
        page.screenshot(path=str(shot_dir / "s7-inbox-interviewer.png"), full_page=True)
