"""E2E: reviewer-written sentences and the AI draft badge on the review screen."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Page, expect

MakeCard = Callable[..., tuple[str, str]]


def test_reviewer_sentence_gets_badge_and_card_can_publish(
    server_url: str,
    auth_headers: dict[str, str],
    make_card: MakeCard,
    page: Page,
) -> None:
    _, card_id = make_card(server_url, auth_headers)
    card = httpx.get(f"{server_url}/api/cards/{card_id}", timeout=10, headers=auth_headers).json()
    sections = dict(card["sections"])
    sections["menu"] = (sections.get("menu", "") + " 검수자가 덧붙인 새 문장입니다.").strip()
    put = httpx.put(
        f"{server_url}/api/cards/{card_id}",
        json={"sections": sections},
        timeout=10,
        headers=auth_headers,
    )
    assert put.status_code == 200, put.text

    page.goto(f"{server_url}/review/{card_id}")
    badge = page.locator(".evidence-badge.badge-reviewer")
    expect(badge.first).to_be_visible()
    expect(badge.first).to_have_text("검수자 작성")

    published = httpx.post(
        f"{server_url}/api/cards/{card_id}/publish", timeout=10, headers=auth_headers
    )
    assert published.status_code == 200, published.text
    page.goto(f"{server_url}/cards/{card_id}")
    expect(page.locator("#reviewer-marker")).to_contain_text("검수자 작성")


def test_fake_provider_draft_is_labelled(
    server_url: str,
    auth_headers: dict[str, str],
    make_card: MakeCard,
    page: Page,
    axe_check: Callable[[Page], list[dict[str, Any]]],
    shot_dir: Path,
) -> None:
    _, card_id = make_card(server_url, auth_headers)
    page.goto(f"{server_url}/review/{card_id}")
    expect(page.locator("#llm-badge")).to_have_text("AI 초안 없음 (가짜 초안)")
    expect(page.locator("#llm-warning")).to_be_hidden()
    page.screenshot(path=str(shot_dir / "review-axe.png"), full_page=True)
    assert axe_check(page) == []
