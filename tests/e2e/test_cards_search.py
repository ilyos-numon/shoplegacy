"""E2E: published card list with filters, next-round actions, resume list."""

import os
import re
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

SHOP = "가나다식당"
OWNER = "홍길동"
PHONE = "010-0000-0000"
SUPPLIER = "라마바정육"
SECRETS = (SHOP, OWNER, PHONE, SUPPLIER)
ANSWER = f"{SHOP} {OWNER} {PHONE} {SUPPLIER}"


def _create_shop(base: str, headers: dict[str, str], area_type: str) -> str:
    res = httpx.post(
        f"{base}/api/shops",
        json={
            "name": SHOP,
            "owner_name": OWNER,
            "trade": "restaurant",
            "area_type": area_type,
            "years": 18,
            "closed_year": 2025,
            "supplier_names": [SUPPLIER],
        },
        timeout=10,
        headers=headers,
    )
    assert res.status_code == 201, res.text
    return str(res.json()["shop_id"])


def _short_round(base: str, headers: dict[str, str], shop_id: str) -> tuple[str, str]:
    """Start, answer main + follow-up, end. Returns (session_id, card_id)."""
    res = httpx.post(f"{base}/api/sessions", json={"shop_id": shop_id}, timeout=10, headers=headers)
    assert res.status_code == 201, res.text
    session_id = str(res.json()["session_id"])
    for _ in range(2):
        ans = httpx.post(
            f"{base}/api/sessions/{session_id}/answer",
            json={"text": ANSWER},
            timeout=10,
            headers=headers,
        )
        assert ans.status_code == 200, ans.text
    end = httpx.post(f"{base}/api/sessions/{session_id}/end", timeout=30, headers=headers)
    assert end.status_code == 200, end.text
    return session_id, str(end.json()["card_id"])


def _publish(base: str, headers: dict[str, str], card_id: str) -> None:
    card = httpx.get(f"{base}/api/cards/{card_id}", timeout=10, headers=headers)
    assert card.status_code == 200, card.text
    put = httpx.put(
        f"{base}/api/cards/{card_id}",
        json={"sections": card.json()["sections"]},
        timeout=10,
        headers=headers,
    )
    assert put.status_code == 200, put.text
    pub = httpx.post(f"{base}/api/cards/{card_id}/publish", timeout=10, headers=headers)
    assert pub.status_code == 200, pub.text


def _seed(base: str, headers: dict[str, str]) -> dict[str, str]:
    """Two published cards (residential, station). Returns ids keyed by area."""
    out: dict[str, str] = {}
    for area in ("residential", "station"):
        shop_id = _create_shop(base, headers, area)
        session_id, card_id = _short_round(base, headers, shop_id)
        _publish(base, headers, card_id)
        out[area] = card_id
        out[f"{area}_shop"] = shop_id
        out[f"{area}_session"] = session_id
    return out


def _save(name: str, page: Page) -> None:
    out = Path(os.environ.get("ILLY_SHOT_DIR", "tests/e2e/screenshots"))
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    page.screenshot(path=str(path), full_page=True)
    assert path.stat().st_size > 0


def _no_secrets(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


def test_cards_api_filters(server_url: str, auth_headers: dict[str, str]) -> None:
    seeded = _seed(server_url, auth_headers)

    res = httpx.get(f"{server_url}/api/cards", params={"status": "published"}, timeout=10)
    assert res.status_code == 200, res.text
    assert len(res.json()) == 2
    _no_secrets(res.text)

    res = httpx.get(
        f"{server_url}/api/cards",
        params={"status": "published", "area_type": "station"},
        timeout=10,
    )
    assert res.status_code == 200, res.text
    assert len(res.json()) == 1
    assert res.json()[0]["tags"]["area_type"] == "station"
    assert seeded["station"] in res.text
    _no_secrets(res.text)

    res = httpx.get(
        f"{server_url}/api/cards",
        params={"status": "published", "area_type": "mall"},
        timeout=10,
    )
    assert res.status_code == 422

    res = httpx.get(
        f"{server_url}/api/cards", params={"status": "published", "trade": "cafe"}, timeout=10
    )
    assert res.status_code == 200, res.text
    assert res.json() == []


def test_cards_api_drafts_are_not_listed(server_url: str, auth_headers: dict[str, str]) -> None:
    shop_id = _create_shop(server_url, auth_headers, "residential")
    _short_round(server_url, auth_headers, shop_id)
    res = httpx.get(
        f"{server_url}/api/cards",
        params={"status": "published", "trade": "restaurant"},
        timeout=10,
    )
    assert res.status_code == 200, res.text
    assert res.json() == []
    _no_secrets(res.text)


def test_cards_list_to_next_round_ui(
    server_url: str, page: Page, auth_headers: dict[str, str]
) -> None:
    seeded = _seed(server_url, auth_headers)

    page.goto(f"{server_url}/cards")
    items = page.locator("article.card-item")
    expect(items).to_have_count(2)
    expect(page.locator("#result-count")).to_have_text("카드 2장")
    expect(page.locator(".card-tags").first).to_contain_text("년 운영")
    _save("cards-list.png", page)
    _no_secrets(page.locator("body").inner_text())

    page.select_option("#filter-area", "station")
    page.click("#region-btn")
    expect(page.locator("#sheet-region")).to_be_visible()
    page.click("#search-btn")
    expect(page.locator("#sheet-region")).to_be_hidden()
    expect(items).to_have_count(1)
    expect(page.locator("#result-count")).to_have_text("카드 1장")
    expect(page.locator(".card-tags").first).to_contain_text("역세권")
    assert "area_type=station" in page.url
    _save("cards-list-filtered.png", page)

    page.click("a.card-link")
    page.wait_for_url(f"{server_url}/cards/{seeded['station']}")
    actions = page.locator("#next-actions")
    expect(actions.locator("#cards-link")).to_be_visible()
    expect(actions.locator("#next-round-btn")).to_be_visible()
    expect(actions.locator("#review-link")).to_be_visible()
    _save("card-next-actions.png", page)
    _no_secrets(page.locator("body").inner_text())

    page.click("#next-round-btn")
    page.wait_for_url(re.compile(r"/interview/[^/]+$"))
    expect(page.locator("#session-number")).to_contain_text("2회차")
    expect(page.locator("#question-text")).not_to_have_text(re.compile(r"불러오는 중"))
    _save("interview-round2.png", page)
    _no_secrets(page.locator("body").inner_text())
    round2_id = page.url.rstrip("/").rsplit("/", 1)[1]
    end2 = httpx.post(
        f"{server_url}/api/sessions/{round2_id}/end", timeout=30, headers=auth_headers
    )
    assert end2.status_code == 200, end2.text

    page.goto(f"{server_url}/interview/{seeded['station_session']}")
    review = page.locator("#review-link")
    expect(review).to_be_visible()
    expect(review).to_have_attribute("href", re.compile(r"/review/"))

    page.goto(f"{server_url}/")
    row = page.locator(f'li[data-shop-id="{seeded["station_shop"]}"]')
    expect(row).to_be_visible()
    expect(row.locator(".shop-progress")).to_contain_text("2/5회")
    expect(row.locator("button.continue-btn")).to_have_text("3회차 시작")
    _save("index-resume.png", page)

    _no_secrets(page.locator("body").inner_text())


def test_cards_list_empty_state(server_url: str, page: Page) -> None:
    page.goto(f"{server_url}/cards")
    expect(page.locator("#empty")).to_be_visible()
    expect(page.locator("article.card-item")).to_have_count(0)
