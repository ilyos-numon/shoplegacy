"""E2E: owner review link, owner page, decision shown to the reviewer."""

import os
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import jwt
from playwright.sync_api import Page, expect

MakeCard = Callable[..., tuple[str, str]]


def _link(
    base: str, headers: dict[str, str], card_id: str, phone: str | None = None
) -> dict[str, str]:
    res = httpx.post(
        f"{base}/api/cards/{card_id}/owner-link", json={"phone": phone}, timeout=10, headers=headers
    )
    assert res.status_code == 201, res.text
    return dict(res.json())


def _token(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def test_owner_opens_link_reads_large_text_and_approves(
    server_url: str,
    auth_headers: dict[str, str],
    make_card: MakeCard,
    anon_page: Page,
    axe_check: Callable[[Page], list[dict[str, object]]],
) -> None:
    _, card_id = make_card(server_url, auth_headers)
    link = _link(server_url, auth_headers, card_id)
    assert link["delivery"] == "log"
    assert link["url"].startswith(f"{server_url}/owner/")

    anon_page.goto(link["url"])
    sections = anon_page.locator("#owner-card section[data-axis]")
    expect(sections.first).to_be_visible()
    assert sections.count() >= 1
    size = anon_page.locator("#owner-card section[data-axis] .body").first.evaluate(
        "el => parseFloat(getComputedStyle(el).fontSize)"
    )
    assert size >= 20
    out = Path(os.environ.get("ILLY_SHOT_DIR", "tests/e2e/screenshots"))
    out.mkdir(parents=True, exist_ok=True)
    anon_page.screenshot(path=str(out / "owner-review.png"), full_page=True)
    assert axe_check(anon_page) == []

    anon_page.click("#owner-approve")
    anon_page.click("#owner-submit")
    expect(anon_page.locator("#owner-result")).to_contain_text("고맙습니다")

    card = httpx.get(f"{server_url}/api/cards/{card_id}", timeout=10, headers=auth_headers).json()
    assert card["owner_decision"]["decision"] == "approve"


def test_reviewer_page_shows_owner_approval(
    server_url: str, auth_headers: dict[str, str], make_card: MakeCard, page: Page
) -> None:
    _, card_id = make_card(server_url, auth_headers)
    page.goto(f"{server_url}/review/{card_id}")
    expect(page.locator("#owner-decision")).to_have_text("사장님 검토 전")
    page.click("#owner-link-btn")
    expect(page.locator("#owner-link-url")).to_contain_text("/owner/")
    token = _token(page.locator("#owner-link-url").inner_text())
    res = httpx.post(
        f"{server_url}/api/owner/{token}/decision", json={"decision": "approve"}, timeout=10
    )
    assert res.status_code == 201, res.text
    page.reload()
    expect(page.locator("#owner-decision")).to_have_text("사장님 승인")


def test_owner_asks_for_changes_with_comment(
    server_url: str, auth_headers: dict[str, str], make_card: MakeCard, anon_page: Page
) -> None:
    _, card_id = make_card(server_url, auth_headers)
    link = _link(server_url, auth_headers, card_id)
    anon_page.goto(link["url"])
    expect(anon_page.locator("#owner-card")).to_be_visible()
    expect(anon_page.locator("#owner-comment")).to_be_hidden()
    anon_page.click("#owner-changes")
    anon_page.fill("#owner-comment", "메뉴 가격을 다시 봐 주세요")
    anon_page.click("#owner-submit")
    expect(anon_page.locator("#owner-result")).to_contain_text("고칠 게 있어요")
    card = httpx.get(f"{server_url}/api/cards/{card_id}", timeout=10, headers=auth_headers).json()
    assert card["owner_decision"]["decision"] == "request_changes"
    assert card["owner_decision"]["comment"] == "메뉴 가격을 다시 봐 주세요"


def test_submit_without_choice_shows_error(
    server_url: str, auth_headers: dict[str, str], make_card: MakeCard, anon_page: Page
) -> None:
    _, card_id = make_card(server_url, auth_headers)
    link = _link(server_url, auth_headers, card_id)
    anon_page.goto(link["url"])
    expect(anon_page.locator("#owner-decide")).to_be_visible()
    anon_page.click("#owner-submit")
    expect(anon_page.locator("#owner-error")).to_be_visible()


def test_tampered_token_is_401(
    server_url: str, auth_headers: dict[str, str], make_card: MakeCard, anon_page: Page
) -> None:
    _, card_id = make_card(server_url, auth_headers)
    token = _token(_link(server_url, auth_headers, card_id)["url"])
    bad = token[:-3] + ("abc" if not token.endswith("abc") else "xyz")
    assert httpx.get(f"{server_url}/api/owner/{bad}", timeout=10).status_code == 401
    anon_page.goto(f"{server_url}/owner/{bad}")
    expect(anon_page.locator("#owner-error")).to_be_visible()


def test_expired_token_is_410(
    server_url: str, auth_headers: dict[str, str], make_card: MakeCard, jwt_secret: str
) -> None:
    _, card_id = make_card(server_url, auth_headers)
    token = _token(_link(server_url, auth_headers, card_id)["url"])
    claims = jwt.decode(token, options={"verify_signature": False})
    claims["iat"] = int(time.time()) - 7200
    claims["exp"] = int(time.time()) - 3600
    expired = jwt.encode(claims, jwt_secret, algorithm="HS256")
    assert httpx.get(f"{server_url}/api/owner/{expired}", timeout=10).status_code == 410
    res = httpx.post(
        f"{server_url}/api/owner/{expired}/decision", json={"decision": "approve"}, timeout=10
    )
    assert res.status_code == 410


def test_owner_link_requires_reviewer_and_known_card(
    server_url: str,
    auth_headers: dict[str, str],
    interviewer_headers: dict[str, str],
    make_card: MakeCard,
) -> None:
    _, card_id = make_card(server_url, auth_headers)
    denied = httpx.post(
        f"{server_url}/api/cards/{card_id}/owner-link",
        json={},
        timeout=10,
        headers=interviewer_headers,
    )
    assert denied.status_code == 403
    missing = httpx.post(
        f"{server_url}/api/cards/no-such-card/owner-link", json={}, timeout=10, headers=auth_headers
    )
    assert missing.status_code == 404
