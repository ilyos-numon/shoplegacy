"""E2E: region lookup and filters, card feedback, cafe label in the public list."""

import os
from collections.abc import Callable
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

MakeCard = Callable[..., tuple[str, str]]
PublishedCard = Callable[[str, dict[str, str]], str]


def _shot(page: Page, name: str) -> Path:
    out = Path(os.environ.get("ILLY_SHOT_DIR", "tests/e2e/screenshots"))
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    page.screenshot(path=str(path), full_page=True)
    return path


def _publish(base: str, headers: dict[str, str], card_id: str) -> None:
    res = httpx.post(f"{base}/api/cards/{card_id}/publish", timeout=10, headers=headers)
    assert res.status_code == 200, res.text


def test_regions_list_counts(server_url: str) -> None:
    sidos = httpx.get(f"{server_url}/api/regions", timeout=10).json()
    assert len(sidos) == 16
    assert all(r["sigungu"] is None for r in sidos)
    total = 0
    for sido in sidos:
        rows = httpx.get(f"{server_url}/api/regions", params={"sido": sido["sido"]}, timeout=10)
        assert rows.status_code == 200
        total += len(rows.json())
    assert total == 269


def test_region_shop_card_is_found_by_region_and_query(
    server_url: str, auth_headers: dict[str, str], make_card: MakeCard
) -> None:
    found = httpx.get(f"{server_url}/api/regions/search", params={"q": "종로"}, timeout=10).json()
    jongno = next(r for r in found if r["sido"] == "서울특별시" and r["sigungu"] == "종로구")
    _, card_id = make_card(server_url, auth_headers, region_code=jongno["code"])
    _publish(server_url, auth_headers, card_id)

    card = httpx.get(f"{server_url}/api/cards/{card_id}", timeout=10).json()
    assert card["tags"]["region"] == "서울특별시 종로구"
    by_sido = httpx.get(f"{server_url}/api/cards", params={"region": "11"}, timeout=10).json()
    assert card_id in [c["card_id"] for c in by_sido]
    by_code = httpx.get(
        f"{server_url}/api/cards", params={"region": jongno["code"]}, timeout=10
    ).json()
    assert card_id in [c["card_id"] for c in by_code]
    by_query = httpx.get(f"{server_url}/api/cards", params={"q": "종로"}, timeout=10).json()
    assert card_id in [c["card_id"] for c in by_query]
    elsewhere = httpx.get(f"{server_url}/api/cards", params={"region": "26"}, timeout=10).json()
    assert card_id not in [c["card_id"] for c in elsewhere]


def test_unknown_region_code_is_422(server_url: str, auth_headers: dict[str, str]) -> None:
    res = httpx.post(
        f"{server_url}/api/shops",
        json={
            "name": "가나다식당",
            "owner_name": "홍길동",
            "trade": "restaurant",
            "area_type": "residential",
            "years": 3,
            "closed_year": 2025,
            "region_code": "9999999999",
        },
        timeout=10,
        headers=auth_headers,
    )
    assert res.status_code == 422


def test_feedback_up_updates_page_and_passes_axe(
    server_url: str,
    auth_headers: dict[str, str],
    published_card: PublishedCard,
    anon_page: Page,
    axe_check: Callable[[Page], list[dict[str, object]]],
) -> None:
    card_id = published_card(server_url, auth_headers)
    anon_page.goto(f"{server_url}/cards/{card_id}")
    expect(anon_page.locator("#feedback-counts")).to_contain_text("도움됐어요 0")
    anon_page.click("#feedback-up")
    expect(anon_page.locator("#feedback-counts")).to_contain_text("도움됐어요 1")
    path = _shot(anon_page, "card-feedback.png")
    assert path.stat().st_size > 0
    assert axe_check(anon_page) == []


def test_same_visitor_second_vote_is_not_counted(
    server_url: str, auth_headers: dict[str, str], published_card: PublishedCard
) -> None:
    card_id = published_card(server_url, auth_headers)
    url = f"{server_url}/api/cards/{card_id}/feedback"
    first = httpx.post(url, json={"vote": "up"}, timeout=10)
    assert first.status_code == 201
    assert first.json() == {"up": 1, "down": 0, "counted": True}
    second = httpx.post(url, json={"vote": "up"}, timeout=10)
    assert second.status_code == 201
    assert second.json() == {"up": 1, "down": 0, "counted": False}


def test_feedback_on_unpublished_card_is_404(
    server_url: str, auth_headers: dict[str, str], make_card: MakeCard
) -> None:
    _, card_id = make_card(server_url, auth_headers)
    res = httpx.post(f"{server_url}/api/cards/{card_id}/feedback", json={"vote": "up"}, timeout=10)
    assert res.status_code == 404


def test_cafe_card_tag_in_public_list_is_korean(
    server_url: str, auth_headers: dict[str, str], make_card: MakeCard, anon_page: Page
) -> None:
    _, card_id = make_card(server_url, auth_headers, trade="cafe", name="마바사카페")
    _publish(server_url, auth_headers, card_id)
    anon_page.goto(f"{server_url}/cards")
    tags = anon_page.locator(f'article[data-card-id="{card_id}"] .card-tags')
    expect(tags).to_contain_text("카페")
    assert "cafe" not in (tags.text_content() or "")


def _region_code(base: str, query: str, sigungu: str) -> str:
    found = httpx.get(f"{base}/api/regions/search", params={"q": query}, timeout=10).json()
    row = next(r for r in found if r["sido"] == "서울특별시" and r["sigungu"] == sigungu)
    return str(row["code"])


def _published_in(base: str, headers: dict[str, str], make_card: MakeCard, code: str) -> str:
    _, card_id = make_card(base, headers, region_code=code)
    _publish(base, headers, card_id)
    return card_id


def test_sigungu_region_filter_returns_only_that_district(
    server_url: str, auth_headers: dict[str, str], make_card: MakeCard
) -> None:
    jongno = _region_code(server_url, "종로", "종로구")
    jung = _region_code(server_url, "중구", "중구")
    assert (jongno[:5], jung[:5]) == ("11110", "11140")
    jongno_card = _published_in(server_url, auth_headers, make_card, jongno)
    jung_card = _published_in(server_url, auth_headers, make_card, jung)
    res = httpx.get(f"{server_url}/api/cards", params={"region": jongno[:5]}, timeout=10)
    assert res.status_code == 200
    ids = [c["card_id"] for c in res.json()]
    assert jongno_card in ids
    assert jung_card not in ids


def test_sigungu_sido_prefix_returns_both_districts(
    server_url: str, auth_headers: dict[str, str], make_card: MakeCard
) -> None:
    jongno_card = _published_in(
        server_url, auth_headers, make_card, _region_code(server_url, "종로", "종로구")
    )
    jung_card = _published_in(
        server_url, auth_headers, make_card, _region_code(server_url, "중구", "중구")
    )
    ids = [
        c["card_id"]
        for c in httpx.get(f"{server_url}/api/cards", params={"region": "11"}, timeout=10).json()
    ]
    assert jongno_card in ids
    assert jung_card in ids
