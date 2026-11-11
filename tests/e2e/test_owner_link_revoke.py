"""E2E: reviewer cancels an owner link; the owner can no longer open or answer it."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Page, expect

MakeCard = Callable[..., tuple[str, str]]


def _make_link(base: str, headers: dict[str, str], card_id: str) -> str:
    res = httpx.post(f"{base}/api/cards/{card_id}/owner-link", json={}, timeout=10, headers=headers)
    assert res.status_code == 201, res.text
    return str(res.json()["url"])


def _links(base: str, headers: dict[str, str], card_id: str) -> list[dict[str, Any]]:
    res = httpx.get(f"{base}/api/cards/{card_id}/owner-links", timeout=10, headers=headers)
    assert res.status_code == 200, res.text
    return list(res.json())


def _revoke(base: str, headers: dict[str, str], card_id: str, token_id: str) -> httpx.Response:
    return httpx.delete(
        f"{base}/api/cards/{card_id}/owner-links/{token_id}", timeout=10, headers=headers
    )


def test_new_link_is_listed_as_active(
    server_url: str, auth_headers: dict[str, str], make_card: MakeCard
) -> None:
    _, card_id = make_card(server_url, auth_headers)
    _make_link(server_url, auth_headers, card_id)
    links = _links(server_url, auth_headers, card_id)
    assert len(links) == 1
    assert links[0]["state"] == "active"


def test_revoked_link_is_410_for_card_and_decision(
    server_url: str, auth_headers: dict[str, str], make_card: MakeCard
) -> None:
    _, card_id = make_card(server_url, auth_headers)
    url = _make_link(server_url, auth_headers, card_id)
    token = url.rstrip("/").rsplit("/", 1)[-1]
    token_id = _links(server_url, auth_headers, card_id)[0]["token_id"]
    assert _revoke(server_url, auth_headers, card_id, token_id).status_code == 204
    assert _links(server_url, auth_headers, card_id)[0]["state"] == "revoked"
    got = httpx.get(f"{server_url}/api/owner/{token}", timeout=10)
    assert got.status_code == 410
    assert got.json()["detail"] == "link_revoked"
    decided = httpx.post(
        f"{server_url}/api/owner/{token}/decision", json={"decision": "approve"}, timeout=10
    )
    assert decided.status_code == 410


def test_second_revoke_is_409_and_unknown_link_is_404(
    server_url: str, auth_headers: dict[str, str], make_card: MakeCard
) -> None:
    _, card_id = make_card(server_url, auth_headers)
    _make_link(server_url, auth_headers, card_id)
    token_id = _links(server_url, auth_headers, card_id)[0]["token_id"]
    assert _revoke(server_url, auth_headers, card_id, token_id).status_code == 204
    again = _revoke(server_url, auth_headers, card_id, token_id)
    assert again.status_code == 409
    assert again.json()["detail"] == "already_revoked"
    assert _revoke(server_url, auth_headers, card_id, "no-such-token").status_code == 404


def test_other_org_cannot_list_or_revoke(
    server_url: str,
    auth_headers: dict[str, str],
    second_org: dict[str, str],
    make_card: MakeCard,
) -> None:
    _, card_id = make_card(server_url, auth_headers)
    _make_link(server_url, auth_headers, card_id)
    token_id = _links(server_url, auth_headers, card_id)[0]["token_id"]
    listed = httpx.get(
        f"{server_url}/api/cards/{card_id}/owner-links", timeout=10, headers=second_org
    )
    assert listed.status_code == 404
    assert _revoke(server_url, second_org, card_id, token_id).status_code == 404


def test_reviewer_screen_cancels_link_and_owner_page_shows_error(
    server_url: str,
    auth_headers: dict[str, str],
    make_card: MakeCard,
    page: Page,
    shot_dir: Path,
) -> None:
    _, card_id = make_card(server_url, auth_headers)
    page.goto(f"{server_url}/review/{card_id}")
    page.click("#owner-link-btn")
    expect(page.locator("#owner-link-url")).to_contain_text("/owner/")
    link_url = page.locator("#owner-link-url").inner_text().strip()
    row = page.locator("#owner-links ul li")
    expect(row).to_have_count(1)
    expect(row.locator(".owner-link-state")).to_have_text("유효")
    row.locator("button.owner-link-revoke").click()
    expect(row.locator(".owner-link-state")).to_have_text("취소됨")
    expect(row.locator("button.owner-link-revoke")).to_be_hidden()

    page.goto(link_url)
    expect(page.locator("#owner-error")).to_be_visible()
    expect(page.locator("#owner-error")).to_contain_text("링크")
    expect(page.locator('#owner-error[data-reason="revoked"]')).to_be_visible()
    expect(page.locator("#owner-error")).to_contain_text("취소")
    expect(page.locator("#owner-error")).not_to_contain_text("만료")
    page.screenshot(path=str(shot_dir / "owner-link-revoked.png"), full_page=True)
