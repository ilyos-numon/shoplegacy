"""UI E2E: login page, review page evidence + status buttons, public evidence badge."""

import os
import re
from collections.abc import Callable
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

MakeCard = Callable[[str, dict[str, str]], tuple[str, str]]


def _save(page: Page, name: str) -> None:
    out = Path(os.environ.get("ILLY_SHOT_DIR", "tests/e2e/screenshots"))
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    page.screenshot(path=str(path), full_page=True)
    assert path.stat().st_size > 0


def test_login_page_renders(server_url: str, page: Page) -> None:
    page.goto(f"{server_url}/login")
    expect(page.locator("form")).to_be_visible()
    _save(page, "login.png")


def test_review_sources_and_status_buttons(
    server_url: str, auth_headers: dict[str, str], make_card: MakeCard, page: Page
) -> None:
    _, card_id = make_card(server_url, auth_headers)
    page.goto(f"{server_url}/review/{card_id}")
    # The original answers sit in a bottom sheet opened from a sentence's evidence chip.
    page.locator(".evidence-badge").first.click()
    expect(page.locator("#sheet-source")).to_be_visible()
    sources = page.locator("#sources-panel li[data-answer-id]")
    expect(sources.first).to_be_visible()
    assert sources.count() >= 1
    expect(page.locator(".evidence-badge").first).to_be_attached()
    expect(page.locator("#card-status")).to_have_text("초안")
    _save(page, "review-sources.png")
    page.click("#sheet-source [data-close]")
    expect(page.locator("#sheet-source")).to_be_hidden()

    # Hold / reject / regenerate live in the "more" sheet.
    page.click("#more-btn")
    page.click("#hold-btn")
    expect(page.locator("#card-status")).to_have_text("보류")
    page.click("#publish-btn")
    # Publishing redirects to the public card; reopen the review page to see the status.
    page.wait_for_url(f"{server_url}/cards/{card_id}")
    page.goto(f"{server_url}/review/{card_id}")
    expect(page.locator("#card-status")).to_have_text("공개")


def test_public_card_shows_evidence_badge(
    server_url: str, auth_headers: dict[str, str], make_card: MakeCard, page: Page
) -> None:
    _, card_id = make_card(server_url, auth_headers)
    page.goto(f"{server_url}/review/{card_id}")
    page.click("#publish-btn")
    page.wait_for_url(f"{server_url}/cards/{card_id}")
    badge = page.locator("#evidence-badge")
    expect(badge).to_be_visible()
    expect(badge).to_have_text(re.compile(r"근거 \d+/\d+"))
    _save(page, "card-evidence.png")


def test_review_every_sentence_has_support(
    server_url: str, auth_headers: dict[str, str], make_card: MakeCard, page: Page
) -> None:
    _, card_id = make_card(server_url, auth_headers)
    res = httpx.get(f"{server_url}/api/cards/{card_id}", timeout=10, headers=auth_headers)
    assert res.status_code == 200, res.text
    card = res.json()
    assert card["quality"]["supported_ratio"] == 1.0
    items = [ev for axis_items in card["evidence"].values() for ev in axis_items]
    assert items, "card has no evidence items"
    for ev in items:
        assert ev["supported"] is True, ev
        assert ev["quote"], ev

    page.goto(f"{server_url}/review/{card_id}")
    supported = page.locator(".evidence-badge.supported")
    expect(supported.first).to_be_attached()
    assert supported.count() == len(items)
    assert page.locator(".evidence-badge.unsupported").count() == 0
    title = supported.first.get_attribute("title")
    assert title is not None and len(title) > 0
    _save(page, "review-evidence-supported.png")
